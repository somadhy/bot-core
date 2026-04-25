"""MeshCore subscriptions and command dispatch."""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
import logging
import time
from typing import TYPE_CHECKING, Any

from meshcore import EventType, MeshCore
from meshcore.events import Event

from meshcore_bot.auth import is_admin
from meshcore_bot.blacklist import Blacklist
from meshcore_bot.channel_info import fetch_channel_table
from meshcore_bot.commands.router import CmdKind, parse_incoming
from meshcore_bot.commands.weather_cmd import WeatherPayload, fetch_weather_payload
from meshcore_bot.node_advert_store import NodeAdvertRecord, NodeAdvertStore
from meshcore_bot.textutil import clip_utf8_bytes, pack_lines_utf8_chunks

if TYPE_CHECKING:
    from meshcore_bot.config import BotConfig
    from meshcore_bot.i18n import I18n

logger = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 220
# Full UTF-8 weather reply with @[nick].
WEATHER_REPLY_MAX_BYTES = 140
# Admin DM channel list: each message with @[nick] fits in this UTF-8 byte budget.
CHANNELS_REPLY_MAX_BYTES = 150
NODE_REPLY_MAX_BYTES = 140
# Minimum gap between chunks of a long list when reply_delay_sec is 0.
_CHANNELS_PART_MIN_GAP_SEC = 0.35
_NODE_PART_MIN_GAP_SEC = 0.35
_NODE_MIN_PREFIX_HEX_LEN = 2
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_REPEATER_TYPE_MARKERS = ("repeater", "relay", "router", "2")
_UNKNOWN_PATHINFO = "🔢?, 🪜?, 🧭?"
_UNKNOWN_SIGNAL_QUALITY = "📶?"
_TRACE_RX_LOG = os.environ.get("MESHCORE_BOT_TRACE_RX_LOG", "").lower() in ("1", "true", "yes")
_TRACE_CHANNEL_RAW = os.environ.get("MESHCORE_BOT_TRACE_CHANNEL_RAW", "").lower() in ("1", "true", "yes")


def _channel_idx_from_event(event: Event) -> int:
    """channel_idx may live in payload and/or attributes depending on firmware / meshcore version."""
    pl = event.payload if isinstance(event.payload, dict) else {}
    attrs = event.attributes or {}
    for key in ("channel_idx", "chan", "channel"):
        for bag in (pl, attrs):
            v = bag.get(key)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return -1


def _clip(text: str, max_len: int = MAX_MESSAGE_LEN) -> str:
    t = text.strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 2] + ".."


def _expected_ack_hex(payload: dict[str, Any]) -> str:
    exp = payload.get("expected_ack")
    if isinstance(exp, (bytes, bytearray)):
        return exp.hex()
    return ""


def _suggested_timeout_ms(payload: dict[str, Any]) -> int | None:
    v = payload.get("suggested_timeout")
    if isinstance(v, int):
        return v
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ack_log_bits(event: Event) -> str:
    """Short ACK event dump for logs (meshcore puts `code` in payload and attributes)."""
    pl = event.payload if isinstance(event.payload, dict) else {}
    attrs = event.attributes if isinstance(event.attributes, dict) else {}
    return f"payload={pl} attrs={attrs}"


def _channel_sender_label(raw_text: str) -> str:
    """Part before ``:`` in ``Name: text`` channel lines; else ``?``."""
    t = (raw_text or "").strip()
    if ":" not in t:
        return "?"
    label = t.split(":", 1)[0].strip()
    return label if label else "?"


def _dm_sender_label(contact: dict[str, Any] | None, pubkey_prefix: str | None) -> str:
    if contact:
        name = str(contact.get("adv_name") or "").strip()
        if name:
            return name
    if pubkey_prefix:
        return str(pubkey_prefix)[:12]
    return "?"


def _reply_mention(nick: str, body: str) -> str:
    n = (nick or "").strip() or "?"
    return f"@[{n}] {body}"


def _dm_mention_body_budget(nick: str, total_max: int) -> int:
    overhead = len(_reply_mention(nick, "").encode("utf-8"))
    return max(0, total_max - overhead)


def _weather_reply(nick: str, body: str) -> str:
    return clip_utf8_bytes(_reply_mention(nick, body), WEATHER_REPLY_MAX_BYTES)


def _local_hhmm_from_offset(tz_offset_seconds: int | None) -> str | None:
    if tz_offset_seconds is None:
        return None
    tz = _dt.timezone(_dt.timedelta(seconds=int(tz_offset_seconds)))
    return _dt.datetime.now(tz).strftime("%H:%M")


def _append_weather_local_time(payload: WeatherPayload) -> str:
    body = payload.weather_body
    local_hhmm = _local_hhmm_from_offset(payload.tz_offset_seconds)
    if local_hhmm is None:
        return body
    return f"{body}\n🕒{local_hhmm}"


def _time_line(city: str, tz_offset_seconds: int | None) -> str:
    local_hhmm = _local_hhmm_from_offset(tz_offset_seconds)
    city_show = city.strip() or "?"
    if local_hhmm is None:
        return f"{city_show}\n🕒?"
    return f"{city_show}\n🕒{local_hhmm}"


def _parse_rx_log_data(raw_payload: Any) -> dict[str, Any]:
    """Parse RX_LOG_DATA payload to extract path info."""
    result: dict[str, Any] = {}
    hex_str: str | None = None

    if isinstance(raw_payload, dict):
        raw = raw_payload.get("payload") or raw_payload.get("raw_hex")
        if isinstance(raw, bytes):
            hex_str = raw.hex()
        elif raw is not None:
            hex_str = str(raw)
    elif isinstance(raw_payload, bytes):
        hex_str = raw_payload.hex()
    elif raw_payload is not None:
        hex_str = str(raw_payload)

    if not hex_str:
        return result

    hex_str = hex_str.lower().replace(" ", "").replace("\n", "").replace("\r", "")
    if len(hex_str) < 4:
        return result

    try:
        path_len = int(hex_str[2:4], 16)
    except ValueError:
        return {}

    path_start = 4
    path_end = path_start + (path_len * 2)
    if len(hex_str) < path_end:
        return {}

    path_hex = hex_str[path_start:path_end]
    result["path_len"] = path_len
    result["path_nodes"] = [path_hex[i : i + 2] for i in range(0, len(path_hex), 2)]
    tail = hex_str[path_end:]
    # Many firmwares append a packet/channel hash after path bytes.
    if tail:
        result["raw_tail_hex"] = tail
    return result


def _parse_hex_nodes(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        out = [str(x).strip().lower() for x in raw if str(x).strip()]
        return out or None
    s = str(raw).strip().lower().replace("0x", "")
    s = "".join(ch for ch in s if ch in "0123456789abcdef")
    if len(s) < 2:
        return None
    if len(s) % 2 != 0:
        s = s[:-1]
    nodes = [s[i : i + 2] for i in range(0, len(s), 2)]
    return nodes or None


def _extract_path_nodes(
    payload: dict[str, Any] | None,
    attrs: dict[str, Any] | None = None,
) -> list[str] | None:
    keys = ("path_nodes", "path", "path_hex", "route", "route_hex")
    for bag in (payload, attrs):
        if not isinstance(bag, dict):
            continue
        for key in keys:
            nodes = _parse_hex_nodes(bag.get(key))
            if nodes:
                return nodes
    return None


def _coerce_hash_len(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return max(0, min(3, value))
    s = str(value).strip().lower()
    if not s:
        return None
    if s.endswith("b"):
        s = s[:-1].strip()
    try:
        n = int(s, 10)
    except ValueError:
        return None
    return max(0, min(3, n))


def _extract_hash_len(*bags: dict[str, Any] | None) -> int | None:
    keys = (
        "hash_len",
        "path_hash_size",
        "path_hash_len",
        "packet_hash_len",
        "path_hash_mode",
        "hash_length",
        "path_hash_length",
    )
    for bag in bags:
        if not isinstance(bag, dict):
            continue
        for key in keys:
            raw_val = bag.get(key)
            v = _coerce_hash_len(raw_val)
            if v is not None:
                if key == "path_hash_mode":
                    mode_to_bytes = {0: 1, 1: 2, 2: 3}
                    return mode_to_bytes.get(v, v)
                return v
        # Hex hash forms like "2b" or "a1b2".
        for key in ("path_hash", "packet_hash", "channel_hash"):
            raw = bag.get(key)
            if raw is None:
                continue
            s = str(raw).strip().lower().replace("0x", "")
            s = "".join(ch for ch in s if ch in "0123456789abcdef")
            if not s:
                continue
            bytes_len = (len(s) + 1) // 2
            return max(0, min(3, bytes_len))
    return None


def _format_pathinfo(
    path_len: int | None,
    path_nodes: list[str] | None = None,
    *,
    hash_len: int | None = None,
) -> str:
    """Return compact, emoji-first path info."""
    if path_len is None:
        return _UNKNOWN_PATHINFO
    if path_len == 0:
        h = "?" if hash_len is None else str(hash_len)
        return f"🔢{h}, ↔️direct"
    nodes = path_nodes or []
    if hash_len is not None:
        path_hash_len = hash_len
    elif nodes:
        path_hash_len = 3 if len(nodes) >= 3 else len(nodes)
    else:
        path_hash_len = 3 if path_len >= 3 else max(1, path_len)
    if nodes:
        group_bytes = max(1, min(3, int(path_hash_len)))
        groups: list[str] = []
        for i in range(0, len(nodes), group_bytes):
            chunk = nodes[i : i + group_bytes]
            if len(chunk) < group_bytes:
                break
            groups.append("".join(chunk))
        path_show = ":".join(groups) if groups else "?"
        return f"🔢{path_hash_len}, 🪜{path_len}, 🧭{path_show}"
    return f"🔢{path_hash_len}, 🪜{path_len}"


def _extract_signal_quality(payload: dict[str, Any] | None, attrs: dict[str, Any] | None = None) -> str | None:
    """Extract signal quality string from known payload fields."""
    def _values_recursive(obj: Any) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        if not isinstance(obj, dict):
            return out
        for k, v in obj.items():
            ks = str(k)
            out.append((ks, v))
            if isinstance(v, dict):
                for nk, nv in _values_recursive(v):
                    out.append((f"{ks}.{nk}", nv))
        return out

    def _pick_metric(bag: dict[str, Any] | None, tokens: tuple[str, ...]) -> Any:
        if not isinstance(bag, dict):
            return None
        # 1) Exact known keys first.
        known_keys = (
            "snr",
            "SNR",
            "rx_snr",
            "last_snr",
            "snr_db",
            "lora_snr",
        )
        low = {str(k).lower(): v for k, v in bag.items()}
        for k in known_keys:
            if k.lower() in low and low[k.lower()] is not None:
                lk = k.lower()
                if any(t in lk for t in tokens):
                    return low[k.lower()]
        # 2) Flexible recursive search by token in key path.
        for key_path, val in _values_recursive(bag):
            if val is None:
                continue
            kp = key_path.lower()
            if any(t in kp for t in tokens):
                return val
        return None

    snr = None
    for bag in (payload, attrs):
        if snr is None:
            snr = _pick_metric(bag, ("snr",))
    return f"📶{snr}" if snr is not None else "📶?"


def _split_signal_quality(quality: str | None) -> str | None:
    if not quality:
        return None
    for part in [p.strip() for p in str(quality).split(",")]:
        if part.startswith("📶"):
            return part[2:].strip()
    return None


def _merge_signal_quality(prev_quality: str | None, new_quality: str | None) -> str:
    prev_snr = _split_signal_quality(prev_quality)
    new_snr = _split_signal_quality(new_quality)
    snr = new_snr if new_snr not in (None, "", "?") else prev_snr
    return f"📶{snr}" if snr not in (None, "") else "📶?"


def _ping_reply_body(prefix: str, pathinfo: str) -> str:
    lead = (prefix or "").strip()
    tail = (pathinfo or "").strip()
    if lead and tail:
        return f"{lead} {tail}"
    return lead or tail


def _packet_age_part(payload: dict[str, Any], attrs: dict[str, Any] | None = None) -> str | None:
    """Return packet age as emoji label from sender_timestamp, e.g. ``⏱3s``."""
    raw = payload.get("sender_timestamp")
    if raw is None and isinstance(attrs, dict):
        raw = attrs.get("sender_timestamp")
    if raw is None:
        return None
    try:
        sender_ts = int(raw)
    except (TypeError, ValueError):
        return None
    recv_raw = payload.get("recv_time")
    if recv_raw is None and isinstance(attrs, dict):
        recv_raw = attrs.get("recv_time")
    if recv_raw is not None:
        try:
            base_ts = int(recv_raw)
        except (TypeError, ValueError):
            base_ts = int(time.time())
    else:
        base_ts = int(time.time())
    age_sec = base_ts - sender_ts
    if age_sec <= 0:
        return None
    return f"⏱{age_sec}s"


def _pathinfo_from_message_payload(
    payload: dict[str, Any],
    signal_quality: str | None = None,
    attrs: dict[str, Any] | None = None,
    rx_hash_len: int | None = None,
) -> str | None:
    raw = payload.get("path_len")
    if raw is None:
        return None
    try:
        path_len = int(raw)
    except (TypeError, ValueError):
        return None
    hash_len = _extract_hash_len(payload, attrs)
    if hash_len is None:
        hash_len = rx_hash_len
    nodes = _extract_path_nodes(payload, attrs)
    age_part = _packet_age_part(payload, attrs)
    quality = signal_quality or _UNKNOWN_SIGNAL_QUALITY
    if path_len == 0:
        if age_part:
            return f"{_format_pathinfo(path_len, hash_len=hash_len)}, {quality}, {age_part}"
        return f"{_format_pathinfo(path_len, hash_len=hash_len)}, {quality}"
    base = _format_pathinfo(path_len, nodes, hash_len=hash_len)
    parts = [base]
    if age_part:
        parts.append(age_part)
    return ", ".join(parts)


def _key_preview(pubkey_hex: str, key_bytes: int) -> str:
    n = max(1, min(4, int(key_bytes))) * 2
    return (pubkey_hex or "").lower()[:n]


def _node_type_emoji(node_type: str) -> str:
    t = (node_type or "").strip().lower()
    if t in ("1", "chat") or "chat" in t:
        return "👤"
    if t in ("2", "repeater") or "repeater" in t or "router" in t or "relay" in t:
        return "📡"
    if t in ("3", "room") or "room" in t:
        return "🏠"
    if t in ("4", "sensor") or "sensor" in t:
        return "📟"
    return "📦"


def _node_last_advert_ddmm(ts: float) -> str:
    if ts <= 0:
        return "--.--"
    return _dt.datetime.fromtimestamp(ts).strftime("%d-%m")


def _format_node_line(rec: NodeAdvertRecord, key_preview_bytes: int) -> str:
    key = _key_preview(rec.public_key, key_preview_bytes) or "?"
    emoji = _node_type_emoji(rec.node_type)
    name = rec.node_name or "?"
    return f"🔑{key} {emoji} {name}"


class BotService:
    def __init__(
        self,
        cfg: BotConfig,
        mesh: MeshCore,
        i18n: I18n,
        blacklist: Blacklist,
        shutdown: asyncio.Event,
    ) -> None:
        self._cfg = cfg
        self._mesh = mesh
        self._i18n = i18n
        self._blacklist = blacklist
        self._shutdown = shutdown
        self._node_store = NodeAdvertStore(
            cfg.node_advert_store_path,
            cfg.node_advert_retention_days,
            cfg.node_advert_max_stored,
        )
        self._node_store.load()
        self._latest_pathinfo_str = _UNKNOWN_PATHINFO
        self._latest_signal_quality_str = _UNKNOWN_SIGNAL_QUALITY
        self._latest_hash_len: int | None = None
        self._latest_path_nodes: list[str] | None = None

    def _is_channel_command_allowed(self, kind: CmdKind, channel_idx: int) -> bool:
        key_by_kind = {
            CmdKind.WEATHER: "weather",
            CmdKind.TIME: "time",
            CmdKind.PING: "ping",
            CmdKind.HELP: "help",
            CmdKind.STOP: "stop",
            CmdKind.CHANNELS: "channels",
            CmdKind.MSG: "msg",
            CmdKind.NODE: "node",
        }
        cmd_key = key_by_kind.get(kind)
        if not cmd_key:
            return True
        limits = getattr(self._cfg, "command_channel_indices", {}) or {}
        allowed = limits.get(cmd_key) or []
        # Empty list -> no additional channel limitation.
        if not allowed:
            return True
        return channel_idx in allowed

    def attach(self) -> None:
        self._mesh.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        rx_log_data_event = getattr(EventType, "RX_LOG_DATA", None)
        if rx_log_data_event is not None:
            self._mesh.subscribe(rx_log_data_event, self._on_rx_log_data)
        if self._cfg.dm_enabled:
            self._mesh.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)

    async def _on_rx_log_data(self, event: Event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else event.payload
        attrs = event.attributes if isinstance(event.attributes, dict) else {}
        if _TRACE_RX_LOG:
            logger.info("RX_LOG_DATA raw payload=%r attrs=%r", payload, attrs)
        if isinstance(payload, dict):
            quality = _extract_signal_quality(payload, attrs)
            self._latest_signal_quality_str = _merge_signal_quality(
                self._latest_signal_quality_str, quality
            )
            rx_hash_len = _extract_hash_len(payload, attrs)
            if rx_hash_len is not None:
                self._latest_hash_len = rx_hash_len
            rx_nodes = _extract_path_nodes(payload, attrs)
            if rx_nodes:
                self._latest_path_nodes = rx_nodes
        parsed = _parse_rx_log_data(payload)
        payload_bag = payload if isinstance(payload, dict) else {}
        path_len_raw = payload_bag.get("path_len", attrs.get("path_len"))
        path_len: int | None = None
        try:
            if path_len_raw is not None:
                path_len = int(path_len_raw)
        except (TypeError, ValueError):
            path_len = None
        if path_len is None and parsed:
            parsed_len = parsed.get("path_len")
            if isinstance(parsed_len, int):
                path_len = parsed_len

        path_nodes = self._latest_path_nodes
        if path_nodes is None and parsed:
            parsed_nodes = parsed.get("path_nodes")
            if isinstance(parsed_nodes, list) and parsed_nodes:
                path_nodes = [str(x).strip().lower() for x in parsed_nodes if str(x).strip()]
                self._latest_path_nodes = path_nodes

        if self._latest_hash_len is None:
            tail_hex = str((parsed or {}).get("raw_tail_hex") or "")
            if tail_hex:
                self._latest_hash_len = max(0, min(3, (len(tail_hex) + 1) // 2))
        if isinstance(path_len, int):
            if path_len == 0:
                self._latest_pathinfo_str = (
                    f"{_format_pathinfo(path_len, path_nodes, hash_len=self._latest_hash_len)}, "
                    f"{self._latest_signal_quality_str}"
                )
            else:
                self._latest_pathinfo_str = _format_pathinfo(
                    path_len, path_nodes, hash_len=self._latest_hash_len
                )

    async def refresh_node_store_from_contacts(self) -> int:
        rows: list[dict[str, Any]] = []
        # meshcore API differs by version: some builds expose mesh.get_contacts(),
        # others only mesh.commands.get_contacts() + internal _contacts cache.
        direct = getattr(self._mesh, "get_contacts", None)
        if callable(direct):
            try:
                contacts = direct() or []
                rows = [x for x in contacts if isinstance(x, dict)]
            except Exception:
                logger.exception("mesh.get_contacts() failed while syncing node store")
        if not rows:
            try:
                ev = await self._mesh.commands.get_contacts()
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                data = payload.get("contacts", payload)
                if isinstance(data, list):
                    rows = [x for x in data if isinstance(x, dict)]
            except Exception:
                logger.exception("mesh.commands.get_contacts() failed while syncing node store")
        if not rows:
            cached = getattr(self._mesh, "_contacts", None)
            if isinstance(cached, list):
                rows = [x for x in cached if isinstance(x, dict)]
            elif isinstance(cached, dict):
                rows = [x for x in cached.values() if isinstance(x, dict)]
        type_counts: dict[str, int] = {}
        for row in rows:
            t = str(row.get("type") or row.get("node_type") or row.get("kind") or "unknown").strip().lower()
            type_counts[t] = type_counts.get(t, 0) + 1
        changed = self._node_store.upsert_contacts_snapshot(rows)
        self._node_store.save()
        logger.info(
            "node store contact snapshot: total=%s stored=%s types=%s",
            len(rows),
            changed,
            type_counts,
        )
        if rows and not any(
            any(marker in node_type for marker in _REPEATER_TYPE_MARKERS)
            for node_type in type_counts
        ):
            logger.warning(
                "contact snapshot has no repeater-like nodes (markers=%s); types=%s",
                _REPEATER_TYPE_MARKERS,
                type_counts,
            )
        return changed

    async def _send_chan(
        self, channel_idx: int, text: str, *, kind: str | None = None
    ) -> bool:
        d = self._cfg.reply_delay_sec
        if d > 0:
            await asyncio.sleep(d)
        msg = _clip(text)
        try:
            r = await self._mesh.commands.send_chan_msg(channel_idx, msg)
            if r.type == EventType.ERROR:
                logger.warning("channel send error: %s", r.payload)
                return False
            logger.info(
                "reply sent kind=%s channel_idx=%s len=%s",
                kind or "reply",
                channel_idx,
                len(msg),
            )
            return True
        except Exception:
            logger.exception("send_chan_msg failed")
            return False

    async def _send_dm(
        self,
        dst: Any,
        text: str,
        *,
        kind: str | None = None,
        delay_sec: float | None = None,
    ) -> None:
        if dst is None:
            logger.warning("No destination for DM reply")
            return
        d = self._cfg.reply_delay_sec if delay_sec is None else delay_sec
        if d > 0:
            await asyncio.sleep(d)
        msg = _clip(text)
        cfg = self._cfg
        ts = int(time.time())
        try:
            for attempt in range(cfg.dm_delivery_max_attempts):
                r = await self._mesh.commands.send_msg(dst, msg, timestamp=ts, attempt=attempt)
                if r.type == EventType.ERROR:
                    logger.warning("DM send: send_msg ERROR kind=%s payload=%s", kind or "reply", r.payload)
                    return
                pl = r.payload if isinstance(r.payload, dict) else {}
                exp_hex = _expected_ack_hex(pl)
                st_ms = _suggested_timeout_ms(pl)
                if not exp_hex:
                    logger.info(
                        "DM send: MSG_SENT without expected_ack kind=%s len=%s payload=%s",
                        kind or "reply",
                        len(msg),
                        pl,
                    )
                    return
                logger.info(
                    "DM send: MSG_SENT kind=%s len=%s attempt=%s/%s expected_ack=%s "
                    "suggested_timeout_ms=%s",
                    kind or "reply",
                    len(msg),
                    attempt + 1,
                    cfg.dm_delivery_max_attempts,
                    exp_hex,
                    st_ms,
                )
                wait_sec = cfg.dm_delivery_wait_sec
                if wait_sec <= 0:
                    logger.info(
                        "DM send: delivery ACK wait disabled (wait_sec=0) kind=%s expected_ack=%s",
                        kind or "reply",
                        exp_hex,
                    )
                    return
                logger.info(
                    "DM send: waiting for delivery ACK expected_ack=%s timeout=%.1fs attempt=%s/%s",
                    exp_hex,
                    wait_sec,
                    attempt + 1,
                    cfg.dm_delivery_max_attempts,
                )
                ack = await self._mesh.wait_for_event(
                    EventType.ACK,
                    attribute_filters={"code": exp_hex},
                    timeout=wait_sec,
                )
                if ack is not None:
                    logger.info(
                        "DM send: delivery ACK received kind=%s len=%s expected_ack=%s %s",
                        kind or "reply",
                        len(msg),
                        exp_hex,
                        _ack_log_bits(ack),
                    )
                    return
                logger.warning(
                    "DM send: delivery ACK timeout expected_ack=%s waited_s=%.1fs attempt=%s/%s "
                    "kind=%s",
                    exp_hex,
                    wait_sec,
                    attempt + 1,
                    cfg.dm_delivery_max_attempts,
                    kind or "reply",
                )
            logger.warning(
                "DM send: delivery not confirmed after %s attempts kind=%s",
                cfg.dm_delivery_max_attempts,
                kind or "reply",
            )
        except Exception:
            logger.exception("send_msg failed")

    def _resolve_dm_dst(self, pubkey_prefix: str | None) -> Any:
        if not pubkey_prefix:
            return None
        c = self._mesh.get_contact_by_key_prefix(pubkey_prefix)
        if c:
            return c
        return pubkey_prefix

    async def _on_channel_msg(self, event: Event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        attrs = event.attributes if isinstance(event.attributes, dict) else {}
        if _TRACE_CHANNEL_RAW:
            logger.info("CHANNEL_MSG_RECV raw payload=%r attrs=%r", payload, attrs)
        ch = _channel_idx_from_event(event)
        text = str(payload.get("text", ""))
        if ch not in self._cfg.channels_enabled:
            logger.debug(
                "CHANNEL_MSG_RECV idx=%s (not in enabled_indices=%s) txt_type=%s preview=%r",
                ch,
                self._cfg.channels_enabled,
                payload.get("txt_type"),
                text[:120],
            )
            return
        logger.info(
            "CHANNEL_MSG_RECV idx=%s txt_type=%s len=%s preview=%r",
            ch,
            payload.get("txt_type"),
            len(text),
            text[:120],
        )
        nick = _channel_sender_label(text)
        parsed = parse_incoming(text, self._cfg)
        msg_quality = _extract_signal_quality(payload, attrs)
        self._latest_signal_quality_str = _merge_signal_quality(
            self._latest_signal_quality_str, msg_quality
        )
        msg_hash_len = _extract_hash_len(payload, attrs)
        if msg_hash_len is not None:
            self._latest_hash_len = msg_hash_len
        msg_nodes = _extract_path_nodes(payload, attrs)
        if msg_nodes:
            self._latest_path_nodes = msg_nodes
        msg_pathinfo = _pathinfo_from_message_payload(
            payload,
            msg_quality or self._latest_signal_quality_str,
            attrs,
            self._latest_hash_len,
        )
        if msg_pathinfo:
            self._latest_pathinfo_str = msg_pathinfo
        if parsed.kind == CmdKind.NONE:
            logger.debug(
                "channel idx=%s: not a bot command, ignored (text=%r)",
                ch,
                text[:120],
            )
            return
        if not self._is_channel_command_allowed(parsed.kind, ch):
            logger.debug(
                "channel idx=%s: command %s blocked by commands.<name>.channel_indices",
                ch,
                parsed.kind.name,
            )
            return
        if parsed.kind == CmdKind.STOP:
            if ch not in self._cfg.admin_channel_indices:
                return
            out = _reply_mention(nick, self._i18n.t("admin.shutdown_ok"))
            await self._send_chan(ch, out, kind="stop")
            self._shutdown.set()
            return

        if parsed.kind == CmdKind.HELP:
            body = self._i18n.t("help.body")
            out = _reply_mention(nick, body)
            await self._send_chan(ch, out, kind="help")
            return

        if parsed.kind == CmdKind.PING:
            out = _reply_mention(
                nick, _ping_reply_body(self._i18n.t("ping.pong"), self._latest_pathinfo_str)
            )
            await self._send_chan(ch, out, kind="ping")
            return

        if parsed.kind == CmdKind.NODE:
            await self._handle_node_query_channel(ch, nick, parsed.arg)
            return

        if parsed.kind == CmdKind.WEATHER:
            city = (parsed.arg or "").strip() or (self._cfg.weather_default_city or "").strip()
            if not city:
                await self._send_chan(
                    ch,
                    _weather_reply(nick, self._i18n.t("errors.no_default_city")),
                    kind="weather",
                )
                return
            payload = await fetch_weather_payload(city, self._cfg, self._i18n, use_cache=True)
            line = _append_weather_local_time(payload)
            await self._send_chan(ch, _weather_reply(nick, line), kind="weather")
            return

        if parsed.kind == CmdKind.TIME:
            city = (parsed.arg or "").strip() or (self._cfg.weather_default_city or "").strip()
            if not city:
                await self._send_chan(
                    ch,
                    _weather_reply(nick, self._i18n.t("errors.no_default_city")),
                    kind="time",
                )
                return
            payload = await fetch_weather_payload(city, self._cfg, self._i18n, use_cache=False)
            line = _time_line(city, payload.tz_offset_seconds)
            await self._send_chan(ch, _weather_reply(nick, line), kind="time")

    async def _on_contact_msg(self, event: Event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        attrs = event.attributes if isinstance(event.attributes, dict) else {}
        text = str(payload.get("text", ""))
        pubkey_prefix = payload.get("pubkey_prefix") or attrs.get("pubkey_prefix")
        contact = (
            self._mesh.get_contact_by_key_prefix(pubkey_prefix) if pubkey_prefix else None
        )
        if isinstance(contact, dict):
            try:
                if self._node_store.upsert_contact(contact):
                    self._node_store.purge_and_trim()
                    self._node_store.save()
            except Exception:
                logger.exception("failed to update node advert store from DM contact")
        public_key = (contact or {}).get("public_key") if contact else None

        if self._blacklist.is_blocked(public_key, pubkey_prefix):
            return

        nick = _dm_sender_label(contact, pubkey_prefix)
        parsed = parse_incoming(text, self._cfg)
        if parsed.kind == CmdKind.NONE:
            logger.debug(
                "CONTACT_MSG_RECV: not a bot command, ignored (preview=%r pubkey_prefix=%s)",
                text[:120],
                pubkey_prefix,
            )
            return

        dst = self._resolve_dm_dst(pubkey_prefix)
        if dst is None:
            logger.warning(
                "CONTACT_MSG_RECV: command %s but no pubkey_prefix (cannot reply); preview=%r",
                parsed.kind.name,
                text[:120],
            )
            return

        if parsed.kind == CmdKind.STOP:
            if not is_admin(public_key, self._cfg.admin_public_keys):
                return
            await self._send_dm(
                dst, _reply_mention(nick, self._i18n.t("admin.shutdown_ok")), kind="stop"
            )
            self._shutdown.set()
            return

        if parsed.kind == CmdKind.MSG:
            if not is_admin(public_key, self._cfg.admin_public_keys):
                return
            raw_arg = (parsed.arg or "").strip()
            if ":" not in raw_arg:
                await self._send_dm(
                    dst,
                    _reply_mention(nick, self._i18n.t("admin.msg_bad_format")),
                    kind="admin_msg",
                )
                return
            idx_s, body = raw_arg.split(":", 1)
            try:
                chan_idx = int(idx_s.strip())
            except (TypeError, ValueError):
                await self._send_dm(
                    dst,
                    _reply_mention(nick, self._i18n.t("admin.msg_bad_index")),
                    kind="admin_msg",
                )
                return
            body = body.strip()
            if not body:
                await self._send_dm(
                    dst,
                    _reply_mention(nick, self._i18n.t("admin.msg_empty")),
                    kind="admin_msg",
                )
                return
            if chan_idx not in self._cfg.channels_enabled:
                await self._send_dm(
                    dst,
                    _reply_mention(
                        nick, self._i18n.t("admin.msg_channel_not_enabled", idx=chan_idx)
                    ),
                    kind="admin_msg",
                )
                return
            ok = await self._send_chan(chan_idx, body, kind="admin_msg")
            if ok:
                await self._send_dm(
                    dst,
                    _reply_mention(nick, self._i18n.t("admin.msg_ok", idx=chan_idx)),
                    kind="admin_msg",
                )
            else:
                await self._send_dm(
                    dst,
                    _reply_mention(nick, self._i18n.t("admin.msg_send_failed")),
                    kind="admin_msg",
                )
            return

        if parsed.kind == CmdKind.NODE:
            await self._handle_node_query_dm(dst, nick, parsed.arg)
            return

        if parsed.kind == CmdKind.CHANNELS:
            if not is_admin(public_key, self._cfg.admin_public_keys):
                return
            enabled = sorted(set(self._cfg.channels_enabled))
            if not enabled:
                await self._send_dm(
                    dst, _reply_mention(nick, self._i18n.t("admin.channels_none")), kind="channels"
                )
                return
            table = await fetch_channel_table(self._mesh, self._cfg.channels_enabled)
            lines = [f"{idx}: {table.get(idx, '(?)')}" for idx in enabled]
            body_budget = max(
                1, _dm_mention_body_budget(nick, CHANNELS_REPLY_MAX_BYTES)
            )
            parts = pack_lines_utf8_chunks(lines, body_budget)
            between = max(self._cfg.reply_delay_sec, _CHANNELS_PART_MIN_GAP_SEC)
            for i, body in enumerate(parts):
                delay_sec = between if i > 0 else None
                full = _reply_mention(nick, body)
                if len(full.encode("utf-8")) > CHANNELS_REPLY_MAX_BYTES:
                    full = clip_utf8_bytes(full, CHANNELS_REPLY_MAX_BYTES)
                await self._send_dm(dst, full, kind="channels", delay_sec=delay_sec)
            return

        if parsed.kind == CmdKind.HELP:
            await self._send_dm(
                dst, _reply_mention(nick, self._i18n.t("help.body")), kind="help"
            )
            return

        if parsed.kind == CmdKind.PING:
            msg_quality = _extract_signal_quality(payload, attrs)
            self._latest_signal_quality_str = _merge_signal_quality(
                self._latest_signal_quality_str, msg_quality
            )
            msg_hash_len = _extract_hash_len(payload, attrs)
            if msg_hash_len is not None:
                self._latest_hash_len = msg_hash_len
            msg_nodes = _extract_path_nodes(payload, attrs)
            if msg_nodes:
                self._latest_path_nodes = msg_nodes
            msg_pathinfo = _pathinfo_from_message_payload(
                payload,
                msg_quality or self._latest_signal_quality_str,
                attrs,
                self._latest_hash_len,
            )
            if msg_pathinfo:
                self._latest_pathinfo_str = msg_pathinfo
            await self._send_dm(
                dst,
                _reply_mention(
                    nick,
                    _ping_reply_body(self._i18n.t("ping.pong"), self._latest_pathinfo_str),
                ),
                kind="ping",
            )
            return

        if parsed.kind == CmdKind.WEATHER:
            city = (parsed.arg or "").strip() or (self._cfg.weather_default_city or "").strip()
            if not city:
                await self._send_dm(
                    dst,
                    _weather_reply(nick, self._i18n.t("errors.no_default_city")),
                    kind="weather",
                )
                return
            payload = await fetch_weather_payload(city, self._cfg, self._i18n, use_cache=True)
            line = _append_weather_local_time(payload)
            await self._send_dm(dst, _weather_reply(nick, line), kind="weather")
            return

        if parsed.kind == CmdKind.TIME:
            city = (parsed.arg or "").strip() or (self._cfg.weather_default_city or "").strip()
            if not city:
                await self._send_dm(
                    dst,
                    _weather_reply(nick, self._i18n.t("errors.no_default_city")),
                    kind="time",
                )
                return
            payload = await fetch_weather_payload(city, self._cfg, self._i18n, use_cache=False)
            line = _time_line(city, payload.tz_offset_seconds)
            await self._send_dm(dst, _weather_reply(nick, line), kind="time")

    async def _send_node_parts_dm(self, dst: Any, nick: str, lines: list[str]) -> None:
        body_budget = max(1, _dm_mention_body_budget(nick, NODE_REPLY_MAX_BYTES))
        parts = pack_lines_utf8_chunks(lines, body_budget)
        between = max(self._cfg.reply_delay_sec, _NODE_PART_MIN_GAP_SEC)
        for i, body in enumerate(parts):
            delay_sec = between if i > 0 else None
            full = _reply_mention(nick, body)
            if len(full.encode("utf-8")) > NODE_REPLY_MAX_BYTES:
                full = clip_utf8_bytes(full, NODE_REPLY_MAX_BYTES)
            await self._send_dm(dst, full, kind="node", delay_sec=delay_sec)

    async def _send_node_parts_channel(self, channel_idx: int, nick: str, lines: list[str]) -> None:
        body_budget = max(1, NODE_REPLY_MAX_BYTES - len(_reply_mention(nick, "").encode("utf-8")))
        parts = pack_lines_utf8_chunks(lines, body_budget)
        between = max(self._cfg.reply_delay_sec, _NODE_PART_MIN_GAP_SEC)
        for i, body in enumerate(parts):
            if i > 0 and between > 0:
                await asyncio.sleep(between)
            full = _reply_mention(nick, body)
            if len(full.encode("utf-8")) > NODE_REPLY_MAX_BYTES:
                full = clip_utf8_bytes(full, NODE_REPLY_MAX_BYTES)
            await self._send_chan(channel_idx, full, kind="node")

    def _validate_node_prefix(self, arg: str) -> str | None:
        p = (arg or "").strip().lower()
        if len(p) < _NODE_MIN_PREFIX_HEX_LEN:
            return None
        if not _HEX_RE.match(p):
            return None
        return p

    def _node_query_lines(self, key_prefix: str) -> list[str]:
        self._node_store.purge_and_trim()
        rows = self._node_store.find_by_prefix(key_prefix)
        if not rows:
            return [self._i18n.t("node.not_found", prefix=key_prefix)]
        rows = sorted(rows, key=lambda x: ((x.node_name or "").casefold(), x.public_key))
        lines = [_format_node_line(x, self._cfg.node_key_preview_bytes) for x in rows]
        if len(lines) <= 1:
            return lines
        return [f"{i}: {line}" for i, line in enumerate(lines, start=1)]

    async def _handle_node_query_channel(self, channel_idx: int, nick: str, arg: str) -> None:
        prefix = self._validate_node_prefix(arg)
        if prefix is None:
            msg = self._i18n.t("node.bad_prefix", min_hex=_NODE_MIN_PREFIX_HEX_LEN)
            await self._send_chan(channel_idx, _reply_mention(nick, msg), kind="node")
            return
        lines = self._node_query_lines(prefix)
        await self._send_node_parts_channel(channel_idx, nick, lines)

    async def _handle_node_query_dm(self, dst: Any, nick: str, arg: str) -> None:
        prefix = self._validate_node_prefix(arg)
        if prefix is None:
            msg = self._i18n.t("node.bad_prefix", min_hex=_NODE_MIN_PREFIX_HEX_LEN)
            await self._send_dm(dst, _reply_mention(nick, msg), kind="node")
            return
        lines = self._node_query_lines(prefix)
        await self._send_node_parts_dm(dst, nick, lines)
