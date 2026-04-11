"""On-air flood repeat detection: RX_LOG_DATA (RF log) + CHANNEL_MSG_RECV (companion queue)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from meshcore import EventType, MeshCore
from meshcore.events import Event

logger = logging.getLogger(__name__)


def channel_hash_for_idx(mesh: MeshCore, idx: int) -> str | None:
    ch = mesh._reader.packet_parser.channels
    if idx < len(ch) and ch[idx] and ch[idx].get("channel_hash"):
        return str(ch[idx]["channel_hash"])
    return None


def path_segments(path_hex: str, path_len: int, path_hash_size: int) -> list[str]:
    step = path_hash_size * 2
    if step <= 0 or path_len <= 0:
        return []
    need = step * path_len
    if len(path_hex) < need:
        return []
    return [path_hex[i : i + step] for i in range(0, need, step)]


def _norm_txt(s: str) -> str:
    return " ".join(s.split()).strip().casefold()


def text_matches_sent(sent_msg: str, body: str) -> bool:
    """Loose match for channel text (prefix nick, UTF-8, firmware quirks)."""
    sm = sent_msg.strip()
    body = body.strip()
    if not sm or not body:
        return False
    if sm == body or sm in body or body in sm or body.endswith(sm):
        return True
    nb, ns = _norm_txt(body), _norm_txt(sm)
    if ns in nb or nb in ns:
        return True
    for n in (8, 16, 24, 32):
        if len(sm) >= n and sm[:n] in body:
            return True
    if len(ns) >= 8 and ns[:24] in nb:
        return True
    return False


def rx_log_matches_flood_repeat(
    ld: dict[str, Any],
    chan_hash: str | None,
    sent_msg: str,
    sent_ts: int,
) -> bool:
    pt = ld.get("payload_typename") or ""
    pty = ld.get("payload_type")
    if pt != "GRP_TXT" and pty != 5:
        return False

    ld_ch = ld.get("chan_hash")
    if chan_hash is not None and ld_ch is not None:
        if str(ld_ch).lower() != str(chan_hash).lower():
            return False

    body = (ld.get("message") or "").strip()
    sm = sent_msg.strip()
    if not sm:
        return False

    if not body:
        st = ld.get("sender_timestamp")
        if chan_hash and ld_ch is not None:
            return str(ld_ch).lower() == str(chan_hash).lower()
        if chan_hash is None and st is not None and sent_ts:
            try:
                return abs(int(st) - int(sent_ts)) <= 300
            except (TypeError, ValueError):
                return False
        return False

    if not text_matches_sent(sm, body):
        return False
    st = ld.get("sender_timestamp")
    if st is not None and sent_ts:
        try:
            if abs(int(st) - int(sent_ts)) > 86400:
                return False
        except (TypeError, ValueError):
            pass
    return True


def channel_msg_matches_flood_repeat(
    pl: dict[str, Any],
    channel_idx: int,
    sent_msg: str,
    sent_ts: int,
) -> bool:
    """Match companion CHANNEL_MSG_RECV (what many firmwares send instead of PACKET_LOG_DATA)."""
    idx = pl.get("channel_idx")
    try:
        ch = int(idx) if idx is not None else -1
    except (TypeError, ValueError):
        return False
    if ch != channel_idx:
        return False

    text = (pl.get("text") or "").strip()
    sm = sent_msg.strip()
    if not sm:
        return False
    if not text:
        return False
    if not text_matches_sent(sm, text):
        return False
    st = pl.get("sender_timestamp")
    if st is not None and sent_ts:
        try:
            if abs(int(st) - int(sent_ts)) > 86400:
                return False
        except (TypeError, ValueError):
            pass
    return True


def _dedup_key(rec: dict[str, Any]) -> tuple[Any, ...]:
    ts = rec.get("sender_timestamp")
    t = rec.get("text") or rec.get("message") or ""
    src = rec.get("_wire", "")
    p = rec.get("path") or ""
    return (ts, t[:240], src, str(p)[:80])


def repeater_line(mesh: MeshCore, path_tail_hex: str) -> str:
    if not path_tail_hex:
        return "?"
    c = mesh.get_contact_by_key_prefix(path_tail_hex)
    if c:
        name = (c.get("adv_name") or "").strip()
        if name:
            return f"{name}({path_tail_hex})"
        return f"contact({path_tail_hex})"
    return path_tail_hex


def _path_from_record(rec: dict[str, Any]) -> tuple[str, str]:
    """Return (path_display, last_hop_hex) for logging."""
    if rec.get("path_hash_mode") == -1:
        return "(direct)", ""
    path_hex = str(rec.get("path") or "")
    pl = int(rec.get("path_len") or 0)
    phm = rec.get("path_hash_mode")
    if phm is not None and isinstance(phm, int) and phm >= 0:
        phs = (phm & 3) + 1
    else:
        phs = int(rec.get("path_hash_size") or 1)
    if path_hex and pl > 0 and pl != 255:
        segs = path_segments(path_hex, pl, phs)
        last = segs[-1] if segs else ""
        disp = "/".join(segs) if segs else path_hex
        return disp, last
    return "(no path)", ""


async def listen_flood_repeater_rx(
    mesh: MeshCore,
    channel_idx: int,
    sent_msg: str,
    sent_ts: int,
    duration_sec: float,
) -> int:
    """
    Collect flood repeats for ``duration_sec`` from:

    - ``RX_LOG_DATA`` — raw RF log (0x88), if the radio pushes it.
    - ``CHANNEL_MSG_RECV`` — normal companion path for received channel lines (often the only one).

    Dedupes (timestamp, text, source, path) so the same packet does not count twice.
    """
    if duration_sec <= 0:
        return 0

    chan_hash = channel_hash_for_idx(mesh, channel_idx)
    received: list[dict[str, Any]] = []
    seen_keys: set[tuple[Any, ...]] = set()

    def _add(rec: dict[str, Any], wire: str) -> None:
        rec = {**rec, "_wire": wire}
        k = _dedup_key(rec)
        if k in seen_keys:
            return
        seen_keys.add(k)
        received.append(rec)

    async def on_rx_log(event: Event) -> None:
        ld = event.payload if isinstance(event.payload, dict) else {}
        if not rx_log_matches_flood_repeat(ld, chan_hash, sent_msg, sent_ts):
            return
        _add(ld, "RX_LOG_DATA")

    async def on_chan_msg(event: Event) -> None:
        pl = event.payload if isinstance(event.payload, dict) else {}
        if not channel_msg_matches_flood_repeat(pl, channel_idx, sent_msg, sent_ts):
            return
        _add(pl, "CHANNEL_MSG_RECV")

    sub_log = mesh.subscribe(EventType.RX_LOG_DATA, on_rx_log)
    sub_chan = mesh.subscribe(EventType.CHANNEL_MSG_RECV, on_chan_msg)
    try:
        await asyncio.sleep(duration_sec)
    finally:
        sub_log.unsubscribe()
        sub_chan.unsubscribe()

    seen_last: dict[str, int] = {}
    for rec in received:
        _, last = _path_from_record(rec)
        if last:
            seen_last[last] = seen_last.get(last, 0) + 1

    logger.info(
        "flood air listen: channel_idx=%s duration_s=%.1f matches=%s (RX_LOG + CHANNEL_MSG) chan_hash=%s",
        channel_idx,
        duration_sec,
        len(received),
        chan_hash,
    )

    for i, rec in enumerate(received):
        path_disp, last = _path_from_record(rec)
        wire = rec.get("_wire", "?")
        logger.info(
            "flood air listen: rx[%s] wire=%s path=%s last_hop=%s snr=%s rssi=%s SNR=%s",
            i,
            wire,
            path_disp,
            repeater_line(mesh, last),
            rec.get("snr"),
            rec.get("rssi"),
            rec.get("SNR"),
        )

    if seen_last:
        parts = [f"{repeater_line(mesh, h)}:x{n}" for h, n in sorted(seen_last.items())]
        logger.info("flood air listen: last-hop repeater totals: %s", " ".join(parts))

    return len(received)
