"""Parse incoming commands (no prefix); unknown lines ignored."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshcore_bot.config import BotConfig


class CmdKind(Enum):
    NONE = auto()
    WEATHER = auto()
    TIME = auto()
    PING = auto()
    HELP = auto()
    STOP = auto()
    CHANNELS = auto()
    MSG = auto()
    NODE = auto()


@dataclass
class ParsedCommand:
    kind: CmdKind
    arg: str = ""  # city for weather/time, else empty


def _normalize_cmd_text(text: str) -> str:
    """Strip whitespace and invisible chars some clients prepend to channel text."""
    t = (text or "").strip()
    t = t.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    for ch in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060"):
        t = t.replace(ch, "")
    while "  " in t:
        t = t.replace("  ", " ")
    return t.strip()


def _body_after_channel_label(text: str) -> str:
    """Mesh channel UIs often send ``DisplayName: message``; commands live after the first ``:``."""
    t = _normalize_cmd_text(text)
    if ":" not in t:
        return t
    after = t.split(":", 1)[1].strip()
    return after if after else t


def _parse_command_tokens(s: str) -> ParsedCommand:
    """Backward-compat wrapper for config-less parsing (kept for callers without config)."""
    return _parse_command_tokens_with_cfg(s, None)


def _parse_command_tokens_with_cfg(s: str, cfg: "BotConfig | None") -> ParsedCommand:
    """First token is the command; optional trailing ``:`` is stripped (``погода: Москва``).

    If cfg is provided, also considers aliases from cfg.command_aliases.
    """
    if not s:
        return ParsedCommand(CmdKind.NONE)
    parts = s.split(maxsplit=1)
    name = parts[0].strip().casefold().rstrip(":")
    arg = (parts[1].strip() if len(parts) > 1 else "").strip()

    base_aliases = {
        CmdKind.WEATHER: {"weather", "погода"},
        CmdKind.TIME: {"time", "время"},
        CmdKind.PING: {"ping", "пинг"},
        CmdKind.HELP: {"help", "помощь"},
        CmdKind.STOP: {"stop", "стоп"},
        CmdKind.CHANNELS: {"channels", "каналы"},
        CmdKind.MSG: {"msg", "мсг"},
        CmdKind.NODE: {"node", "узел"},
    }

    if cfg is not None:
        aliases_cfg = getattr(cfg, "command_aliases", {}) or {}

        def _extend(kind: CmdKind, key: str) -> set[str]:
            vals = aliases_cfg.get(key) or []
            extra = {str(v).strip().casefold().rstrip(":") for v in vals if str(v).strip()}
            return base_aliases[kind] | {a for a in extra if a}

        weather_names = _extend(CmdKind.WEATHER, "weather")
        time_names = _extend(CmdKind.TIME, "time")
        ping_names = _extend(CmdKind.PING, "ping")
        help_names = _extend(CmdKind.HELP, "help")
        stop_names = _extend(CmdKind.STOP, "stop")
        channels_names = _extend(CmdKind.CHANNELS, "channels")
        msg_names = _extend(CmdKind.MSG, "msg")
        node_names = _extend(CmdKind.NODE, "node")
    else:
        weather_names = base_aliases[CmdKind.WEATHER]
        time_names = base_aliases[CmdKind.TIME]
        ping_names = base_aliases[CmdKind.PING]
        help_names = base_aliases[CmdKind.HELP]
        stop_names = base_aliases[CmdKind.STOP]
        channels_names = base_aliases[CmdKind.CHANNELS]
        msg_names = base_aliases[CmdKind.MSG]
        node_names = base_aliases[CmdKind.NODE]

    if name in weather_names:
        return ParsedCommand(CmdKind.WEATHER, arg)
    if name in time_names:
        return ParsedCommand(CmdKind.TIME, arg)
    if name in ping_names:
        return ParsedCommand(CmdKind.PING)
    if name in help_names:
        return ParsedCommand(CmdKind.HELP)
    if name in stop_names:
        return ParsedCommand(CmdKind.STOP)
    if name in channels_names:
        return ParsedCommand(CmdKind.CHANNELS)
    if name in msg_names:
        return ParsedCommand(CmdKind.MSG, arg)
    if name in node_names:
        return ParsedCommand(CmdKind.NODE, arg)
    return ParsedCommand(CmdKind.NONE)


def parse_incoming(text: str, cfg: "BotConfig | None" = None) -> ParsedCommand:
    """Parse ``погода``, ``погода Москва``, ``погода: Москва``, and channel-style ``Nick: погода``."""
    t = _normalize_cmd_text(text)
    p = _parse_command_tokens_with_cfg(t, cfg)
    if p.kind != CmdKind.NONE:
        return p
    after = _body_after_channel_label(text)
    if after != t:
        return _parse_command_tokens_with_cfg(after, cfg)
    return ParsedCommand(CmdKind.NONE)
