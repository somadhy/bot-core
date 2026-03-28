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


def parse_incoming(text: str) -> ParsedCommand:
    s = _body_after_channel_label(text)
    if not s:
        return ParsedCommand(CmdKind.NONE)

    parts = s.split(maxsplit=1)

    name = parts[0].strip().casefold()
    arg = (parts[1].strip() if len(parts) > 1 else "").strip()

    if name in ("weather", "погода"):
        return ParsedCommand(CmdKind.WEATHER, arg)
    if name in ("help", "помощь"):
        return ParsedCommand(CmdKind.HELP)
    if name in ("stop", "стоп"):
        return ParsedCommand(CmdKind.STOP)
    return ParsedCommand(CmdKind.NONE)
