"""One-shot serial probe: device info and channel table (no bot loop)."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from meshcore import EventType, MeshCore

from meshcore_bot.config import load_config

# If firmware omits max_channels, probe this many indices.
_FALLBACK_CHANNEL_PROBE = 16
_MAX_CHANNEL_PROBE_CAP = 32


def _print(title: str, lines: list[str]) -> None:
    print(title)
    for line in lines:
        print(f"  {line}")
    print()


async def async_diagnose() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    print("MeshCore companion diagnostic")
    print("=" * 40)
    _print("Connection", [f"Port: {cfg.serial_device}", f"Baud: {cfg.serial_baudrate}"])

    mesh = await MeshCore.create_serial(cfg.serial_device, cfg.serial_baudrate, debug=False)
    if mesh is None:
        print("FAILED: no response from device (check USB, companion firmware, port).", file=sys.stderr)
        return 1

    try:
        si = mesh.self_info or {}
        if si:
            pk = si.get("public_key", "")
            pk_short = f"{pk[:12]}..." if len(pk) > 12 else pk
            _print(
                "Device (SELF_INFO) — companion is alive",
                [
                    f"Name: {si.get('name', '').strip() or '(empty)'}",
                    f"Public key (prefix): {pk_short}",
                    f"TX power: {si.get('tx_power', '?')} (max {si.get('max_tx_power', '?')})",
                    f"Radio: {si.get('radio_freq', '?')} MHz, SF{si.get('radio_sf', '?')}, "
                    f"BW {si.get('radio_bw', '?')}, CR {si.get('radio_cr', '?')}",
                ],
            )
        else:
            _print("Device (SELF_INFO)", ["(not received yet — unusual)"])

        dq = await mesh.commands.send_device_query()
        if dq.type == EventType.ERROR:
            _print("DEVICE_INFO", [f"query failed: {dq.payload}"])
            max_ch = None
        else:
            pl = dq.payload or {}
            lines = [
                f"Firmware class byte: {pl.get('fw ver', '?')}",
                f"Model: {pl.get('model', '').strip() or '(n/a)'}",
                f"Version string: {pl.get('ver', '').strip() or '(n/a)'}",
                f"Build: {pl.get('fw_build', '').strip() or '(n/a)'}",
            ]
            if "max_channels" in pl:
                lines.append(f"Max channel slots: {pl['max_channels']}")
            if "max_contacts" in pl:
                lines.append(f"Max contacts: {pl['max_contacts']}")
            if "repeat" in pl:
                lines.append(f"Repeater mode: {pl['repeat']}")
            _print("DEVICE_INFO", lines)
            max_ch = pl.get("max_channels")

        br = await mesh.commands.get_bat()
        if br.type == EventType.ERROR:
            _print("Battery", [f"unavailable: {br.payload}"])
        else:
            bp = br.payload or {}
            bat_line = f"Level: {bp.get('level', '?')}"
            if "used_kb" in bp:
                bat_line += f", storage {bp.get('used_kb')}/{bp.get('total_kb')} KB"
            _print("Battery", [bat_line])

        n_slots = int(max_ch) if max_ch is not None else _FALLBACK_CHANNEL_PROBE
        n_slots = max(1, min(n_slots, _MAX_CHANNEL_PROBE_CAP))

        print("Channels (index = channel_idx for config channels.enabled_indices)")
        print("-" * 40)
        any_ok = False
        for idx in range(n_slots):
            ev = await mesh.commands.get_channel(idx)
            if ev.type == EventType.ERROR:
                print(f"  [{idx:2d}]  (no data / error: {ev.payload})")
                continue
            payload: dict[str, Any] = ev.payload or {}
            name = (payload.get("channel_name") or "").strip() or "(unnamed)"
            chash = payload.get("channel_hash", "")
            fp = chash if chash else "??"
            print(f"  [{idx:2d}]  {name!r}  hash_prefix={fp}")
            any_ok = True
        if not any_ok:
            print("  (no channel slots returned OK — check firmware or permissions)")
        print()

        _print(
            "Next steps",
            [
                "Use channel indices above in config: channels.enabled_indices",
                "Run bot: python -m meshcore_bot",
            ],
        )
        return 0
    finally:
        await mesh.disconnect()


def main() -> None:
    import logging

    logging.basicConfig(level=logging.WARNING)
    try:
        code = asyncio.run(async_diagnose())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
