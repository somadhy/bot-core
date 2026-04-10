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


def pack_lines_utf8_chunks(lines: list[str], max_bytes: int) -> list[str]:
    """Group ``lines`` into newline-joined chunks, each at most ``max_bytes`` UTF-8.

    Keeps whole lines when possible; a single line longer than ``max_bytes`` is clipped.
    """
    if max_bytes <= 0:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0

    for raw in lines:
        line = raw
        line_b = line.encode("utf-8")
        if len(line_b) > max_bytes:
            flush()
            chunks.append(clip_utf8_bytes(line, max_bytes))
            continue
        if not current:
            current = [line]
            current_len = len(line_b)
            continue
        join_len = current_len + 1 + len(line_b)
        if join_len <= max_bytes:
            current.append(line)
            current_len = join_len
        else:
            flush()
            current = [line]
            current_len = len(line_b)
    flush()
    return chunks
