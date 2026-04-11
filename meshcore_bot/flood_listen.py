"""On-air flood repeat detection via RX_LOG_DATA (RF log), not companion ACK."""

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


def rx_log_matches_flood_repeat(
    ld: dict[str, Any],
    chan_hash: str | None,
    sent_msg: str,
    sent_ts: int,
) -> bool:
    """Loose match: group text on channel + time + text overlap (firmware/log variants differ)."""
    pt = ld.get("payload_typename") or ""
    pty = ld.get("payload_type")
    if pt != "GRP_TXT" and pty != 5:
        return False
    # Do not filter by route_typename — some builds label repeats differently.

    ld_ch = ld.get("chan_hash")
    if chan_hash is not None and ld_ch is not None:
        if str(ld_ch).lower() != str(chan_hash).lower():
            return False
    # If we know our chan_hash but the log has no chan_hash, still try text/time below.

    st = ld.get("sender_timestamp")
    if st is not None and sent_ts:
        try:
            if abs(int(st) - int(sent_ts)) > 900:
                return False
        except (TypeError, ValueError):
            pass

    body = (ld.get("message") or "").strip()
    sm = sent_msg.strip()
    if not sm:
        return False

    if not body:
        if chan_hash and ld_ch is not None:
            return str(ld_ch).lower() == str(chan_hash).lower()
        if chan_hash is None and st is not None and sent_ts:
            try:
                return abs(int(st) - int(sent_ts)) <= 300
            except (TypeError, ValueError):
                return False
        return False

    if sm == body or sm in body or body in sm or body.endswith(sm):
        return True
    nb, ns = _norm_txt(body), _norm_txt(sm)
    if ns in nb or nb in ns:
        return True
    # Prefix: weather and long lines often differ only by prefix (Nick: …)
    for n in (8, 16, 24, 32):
        if len(sm) >= n and sm[:n] in body:
            return True
    if len(ns) >= 8 and ns[:24] in nb:
        return True
    return False


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


async def listen_flood_repeater_rx(
    mesh: MeshCore,
    channel_idx: int,
    sent_msg: str,
    sent_ts: int,
    duration_sec: float,
) -> int:
    """
    For ``duration_sec``, collect RX_LOG_DATA packets that look like our flood channel
    message (decrypted GRP_TXT, FLOOD). Logs each path, last hop (typical forwarder),
    and aggregate counts. Requires ``mesh.set_decrypt_channel_logs(True)`` and
    channel slots loaded (e.g. get_channel) so the parser can decrypt and match.
    """
    if duration_sec <= 0:
        return 0

    chan_hash = channel_hash_for_idx(mesh, channel_idx)
    received: list[dict[str, Any]] = []

    async def on_rx(event: Event) -> None:
        ld = event.payload if isinstance(event.payload, dict) else {}
        if not rx_log_matches_flood_repeat(ld, chan_hash, sent_msg, sent_ts):
            return
        received.append(ld)

    sub = mesh.subscribe(EventType.RX_LOG_DATA, on_rx)
    try:
        await asyncio.sleep(duration_sec)
    finally:
        sub.unsubscribe()

    seen_last: dict[str, int] = {}
    for ld in received:
        path_hex = str(ld.get("path") or "")
        pl = int(ld.get("path_len") or 0)
        phs = int(ld.get("path_hash_size") or 1)
        segs = path_segments(path_hex, pl, phs)
        last = segs[-1] if segs else ""
        if last:
            seen_last[last] = seen_last.get(last, 0) + 1

    logger.info(
        "flood air listen: channel_idx=%s duration_s=%.1f matches=%s chan_hash=%s",
        channel_idx,
        duration_sec,
        len(received),
        chan_hash,
    )

    for i, ld in enumerate(received):
        path_hex = str(ld.get("path") or "")
        pl = int(ld.get("path_len") or 0)
        phs = int(ld.get("path_hash_size") or 1)
        segs = path_segments(path_hex, pl, phs)
        last = segs[-1] if segs else ""
        path_disp = "/".join(segs) if segs else path_hex or "(empty)"
        logger.info(
            "flood air listen: rx[%s] path=%s last_hop=%s snr=%s rssi=%s",
            i,
            path_disp,
            repeater_line(mesh, last),
            ld.get("snr"),
            ld.get("rssi"),
        )

    if seen_last:
        parts = [f"{repeater_line(mesh, h)}:x{n}" for h, n in sorted(seen_last.items())]
        logger.info("flood air listen: last-hop repeater totals: %s", " ".join(parts))

    return len(received)
