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

if TYPE_CHECKING:
    from meshcore_bot.config import BotConfig
    from meshcore_bot.i18n import I18n

logger = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 220


def _clip(text: str, max_len: int = MAX_MESSAGE_LEN) -> str:
    t = text.strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 2] + ".."


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
        self._prefix = cfg.command_prefix

    def attach(self) -> None:
        self._mesh.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        if self._cfg.dm_enabled:
            self._mesh.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)

    async def _send_chan(self, channel_idx: int, text: str) -> None:
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
        payload = event.payload or {}
        ch = int(payload.get("channel_idx", -1))
        if ch not in self._cfg.channels_enabled:
            return
        text = str(payload.get("text", ""))
        parsed = parse_incoming(text, self._prefix)
        if parsed.kind == CmdKind.NONE:
            return
        # Channel packets do not carry sender pubkey; admin stop is DM-only.
        if parsed.kind == CmdKind.STOP:
            return

        if parsed.kind == CmdKind.HELP:
            await self._send_chan(ch, self._i18n.t("help.body", p=self._prefix))
            return

        if parsed.kind == CmdKind.WEATHER:
            city = parsed.arg or self._cfg.weather_default_city
            if not city:
                await self._send_chan(ch, self._i18n.t("errors.no_default_city"))
                return
            line = await fetch_weather_line(city, self._cfg, self._i18n)
            await self._send_chan(ch, line)

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

        parsed = parse_incoming(text, self._prefix)
        if parsed.kind == CmdKind.NONE:
            return

        dst = self._resolve_dm_dst(pubkey_prefix)

        if parsed.kind == CmdKind.STOP:
            if not is_admin(public_key, self._cfg.admin_public_keys):
                return
            await self._send_dm(dst, self._i18n.t("admin.shutdown_ok"))
            self._shutdown.set()
            return

        if parsed.kind == CmdKind.HELP:
            await self._send_dm(dst, self._i18n.t("help.body", p=self._prefix))
            return

        if parsed.kind == CmdKind.WEATHER:
            city = parsed.arg or self._cfg.weather_default_city
            if not city:
                await self._send_dm(dst, self._i18n.t("errors.no_default_city"))
                return
            line = await fetch_weather_line(city, self._cfg, self._i18n)
            await self._send_dm(dst, line)
