"""UTF-8 safe string clipping."""

from __future__ import annotations


def clip_utf8_bytes(text: str, max_bytes: int) -> str:
    """Truncate so that UTF-8 encoding length is at most ``max_bytes``."""
    if max_bytes <= 0:
        return ""
    b = text.encode("utf-8")
    if len(b) <= max_bytes:
        return text
    n = max_bytes
    while n > 0:
        try:
            return b[:n].decode("utf-8")
        except UnicodeDecodeError:
            n -= 1
    return ""
