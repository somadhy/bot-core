"""MeshCore subscriptions and command dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from meshcore import EventType, MeshCore
from meshcore.events import Event

from meshcore_bot.auth import is_admin
from meshcore_bot.blacklist import Blacklist
from meshcore_bot.commands.router import CmdKind, parse_incoming
from meshcore_bot.commands.weather_cmd import fetch_weather_line
from meshcore_bot.textutil import clip_utf8_bytes

if TYPE_CHECKING:
    from meshcore_bot.config import BotConfig
    from meshcore_bot.i18n import I18n

logger = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 220
# Полный UTF-8 ответ погоды с @[nick].
WEATHER_REPLY_MAX_BYTES = 140


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

    async def _send_chan(self, channel_idx: int, text: str) -> None:
        d = self._cfg.reply_delay_sec
        if d > 0:
            await asyncio.sleep(d)
        msg = _clip(text)
        try:
            r = await self._mesh.commands.send_chan_msg(channel_idx, msg)
            if r.type == EventType.ERROR:
                logger.warning("send_chan_msg error: %s", r.payload)
        except Exception:
            logger.exception("send_chan_msg failed")

    async def _send_dm(self, dst: Any, text: str) -> None:
        if dst is None:
            logger.warning("No destination for DM reply")
            return
        d = self._cfg.reply_delay_sec
        if d > 0:
            await asyncio.sleep(d)
        msg = _clip(text)
        try:
            r = await self._mesh.commands.send_msg(dst, msg)
            if r.type == EventType.ERROR:
                logger.warning("send_msg error: %s", r.payload)
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
        parsed = parse_incoming(text)
        if parsed.kind == CmdKind.NONE:
            logger.debug(
                "channel idx=%s: not a bot command, ignored (text=%r)",
                ch,
                text[:120],
            )
            return
        # Channel packets do not carry sender pubkey; admin stop is DM-only.
        if parsed.kind == CmdKind.STOP:
            return

        if parsed.kind == CmdKind.HELP:
            body = self._i18n.t("help.body")
            out = _reply_mention(nick, body)
            logger.info("help on channel_idx=%s → reply (%d chars)", ch, len(out))
            await self._send_chan(ch, out)
            return

        if parsed.kind == CmdKind.WEATHER:
            city = (parsed.arg or "").strip() or (self._cfg.weather_default_city or "").strip()
            if not city:
                await self._send_chan(
                    ch, _weather_reply(nick, self._i18n.t("errors.no_default_city"))
                )
                return
            line = await fetch_weather_line(city, self._cfg, self._i18n)
            await self._send_chan(ch, _weather_reply(nick, line))

    async def _on_contact_msg(self, event: Event) -> None:
        payload = event.payload or {}
        text = str(payload.get("text", ""))
        pubkey_prefix = payload.get("pubkey_prefix")
        contact = (
            self._mesh.get_contact_by_key_prefix(pubkey_prefix) if pubkey_prefix else None
        )
        public_key = (contact or {}).get("public_key") if contact else None

        if self._blacklist.is_blocked(public_key, pubkey_prefix):
            return

        nick = _dm_sender_label(contact, pubkey_prefix)
        parsed = parse_incoming(text)
        if parsed.kind == CmdKind.NONE:
            return

        dst = self._resolve_dm_dst(pubkey_prefix)

        if parsed.kind == CmdKind.STOP:
            if not is_admin(public_key, self._cfg.admin_public_keys):
                return
            await self._send_dm(
                dst, _reply_mention(nick, self._i18n.t("admin.shutdown_ok"))
            )
            self._shutdown.set()
            return

        if parsed.kind == CmdKind.HELP:
            await self._send_dm(dst, _reply_mention(nick, self._i18n.t("help.body")))
            return

        if parsed.kind == CmdKind.WEATHER:
            city = (parsed.arg or "").strip() or (self._cfg.weather_default_city or "").strip()
            if not city:
                await self._send_dm(
                    dst,
                    _weather_reply(nick, self._i18n.t("errors.no_default_city")),
                )
                return
            line = await fetch_weather_line(city, self._cfg, self._i18n)
            await self._send_dm(dst, _weather_reply(nick, line))
