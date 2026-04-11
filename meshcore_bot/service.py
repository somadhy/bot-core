"""MeshCore subscriptions and command dispatch."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from meshcore import EventType, MeshCore
from meshcore.events import Event

from meshcore_bot.auth import is_admin
from meshcore_bot.blacklist import Blacklist
from meshcore_bot.channel_info import fetch_channel_table
from meshcore_bot.commands.router import CmdKind, parse_incoming
from meshcore_bot.commands.weather_cmd import fetch_weather_line
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
# Minimum gap between chunks of a long list when reply_delay_sec is 0.
_CHANNELS_PART_MIN_GAP_SEC = 0.35


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

    def attach(self) -> None:
        self._mesh.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        if self._cfg.dm_enabled:
            self._mesh.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)

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
        if parsed.kind == CmdKind.NONE:
            logger.debug(
                "channel idx=%s: not a bot command, ignored (text=%r)",
                ch,
                text[:120],
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

        if parsed.kind == CmdKind.WEATHER:
            city = (parsed.arg or "").strip() or (self._cfg.weather_default_city or "").strip()
            if not city:
                await self._send_chan(
                    ch,
                    _weather_reply(nick, self._i18n.t("errors.no_default_city")),
                    kind="weather",
                )
                return
            line = await fetch_weather_line(city, self._cfg, self._i18n)
            await self._send_chan(ch, _weather_reply(nick, line), kind="weather")

    async def _on_contact_msg(self, event: Event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        attrs = event.attributes if isinstance(event.attributes, dict) else {}
        text = str(payload.get("text", ""))
        pubkey_prefix = payload.get("pubkey_prefix") or attrs.get("pubkey_prefix")
        contact = (
            self._mesh.get_contact_by_key_prefix(pubkey_prefix) if pubkey_prefix else None
        )
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

        if parsed.kind == CmdKind.WEATHER:
            city = (parsed.arg or "").strip() or (self._cfg.weather_default_city or "").strip()
            if not city:
                await self._send_dm(
                    dst,
                    _weather_reply(nick, self._i18n.t("errors.no_default_city")),
                    kind="weather",
                )
                return
            line = await fetch_weather_line(city, self._cfg, self._i18n)
            await self._send_dm(dst, _weather_reply(nick, line), kind="weather")
