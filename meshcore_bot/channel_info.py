"""Resolve channel indices to names via MeshCore (same protocol as diagnose)."""

from __future__ import annotations

from typing import Any

from meshcore import EventType, MeshCore

# Match diagnose.py: probe range when firmware omits max_channels.
_FALLBACK_CHANNEL_PROBE = 16
_MAX_CHANNEL_PROBE_CAP = 32


async def fetch_channel_table(mesh: MeshCore, enabled_indices: list[int]) -> dict[int, str]:
    """Return index -> display name (from device). Missing/error slots omitted unless in enabled_indices."""
    dq = await mesh.commands.send_device_query()
    max_ch: int | None = None
    if dq.type != EventType.ERROR:
        max_ch = (dq.payload or {}).get("max_channels")

    base = int(max_ch) if max_ch is not None else _FALLBACK_CHANNEL_PROBE
    need = max(enabled_indices) + 1 if enabled_indices else 0
    n_slots = max(base, need, 1)
    n_slots = min(n_slots, _MAX_CHANNEL_PROBE_CAP)

    out: dict[int, str] = {}

    async def one(idx: int) -> None:
        ev = await mesh.commands.get_channel(idx)
        if ev.type == EventType.ERROR:
            return
        pl: dict[str, Any] = ev.payload or {}
        name = (pl.get("channel_name") or "").strip() or "(unnamed)"
        out[idx] = name

    for idx in range(n_slots):
        await one(idx)

    for idx in sorted(set(enabled_indices)):
        if idx not in out:
            await one(idx)

    return out


def format_listening_line(enabled_indices: list[int], table: dict[int, str]) -> str:
    """Config indices with resolved names; clarifies bot vs companion full table."""
    if not enabled_indices:
        return "(none — add channels.enabled_indices to respond on channels)"
    parts: list[str] = []
    for idx in sorted(set(enabled_indices)):
        if idx in table:
            parts.append(f"{idx}={table[idx]!r}")
        else:
            parts.append(f"{idx}=(no name from device)")
    return ", ".join(parts)


def format_companion_table_line(table: dict[int, str]) -> str:
    if not table:
        return "(no channel slots returned OK)"
    parts = [f"{idx}={name!r}" for idx, name in sorted(table.items())]
    return ", ".join(parts)
