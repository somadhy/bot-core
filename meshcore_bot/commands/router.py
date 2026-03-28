"""Parse incoming commands (no prefix); unknown lines ignored."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class CmdKind(Enum):
    NONE = auto()
    WEATHER = auto()
    HELP = auto()
    STOP = auto()


@dataclass
class ParsedCommand:
    kind: CmdKind
    arg: str = ""  # city for weather, else empty


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
    """First token is the command; optional trailing ``:`` is stripped (``погода: Москва``)."""
    if not s:
        return ParsedCommand(CmdKind.NONE)
    parts = s.split(maxsplit=1)
    name = parts[0].strip().casefold().rstrip(":")
    arg = (parts[1].strip() if len(parts) > 1 else "").strip()

    if name in ("weather", "погода"):
        return ParsedCommand(CmdKind.WEATHER, arg)
    if name in ("help", "помощь"):
        return ParsedCommand(CmdKind.HELP)
    if name in ("stop", "стоп"):
        return ParsedCommand(CmdKind.STOP)
    return ParsedCommand(CmdKind.NONE)


def parse_incoming(text: str) -> ParsedCommand:
    """Parse ``погода``, ``погода Москва``, ``погода: Москва``, and channel-style ``Nick: погода``."""
    t = _normalize_cmd_text(text)
    p = _parse_command_tokens(t)
    if p.kind != CmdKind.NONE:
        return p
    after = _body_after_channel_label(text)
    if after != t:
        return _parse_command_tokens(after)
    return ParsedCommand(CmdKind.NONE)
