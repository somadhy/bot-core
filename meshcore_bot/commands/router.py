"""Parse prefixed commands."""

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


def parse_incoming(text: str, prefix: str) -> ParsedCommand:
    s = (text or "").strip()
    if not s.startswith(prefix):
        return ParsedCommand(CmdKind.NONE)
    rest = s[len(prefix) :].strip()
    if not rest:
        return ParsedCommand(CmdKind.NONE)
    parts = rest.split(maxsplit=1)
    name = parts[0].casefold()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if name in ("weather", "погода"):
        return ParsedCommand(CmdKind.WEATHER, arg)
    if name in ("help", "помощь"):
        return ParsedCommand(CmdKind.HELP)
    if name in ("stop", "стоп"):
        return ParsedCommand(CmdKind.STOP)
    return ParsedCommand(CmdKind.NONE)
