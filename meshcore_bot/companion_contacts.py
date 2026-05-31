"""Enable companion firmware + meshcore client to auto-save contacts from adverts."""

from __future__ import annotations

import logging
from typing import Any

from meshcore import EventType, MeshCore

logger = logging.getLogger(__name__)

# MeshCore companion firmware autoadd_config bitmask (see companion_radio/MyMesh.cpp).
AUTO_ADD_OVERWRITE_OLDEST = 0x01
AUTO_ADD_CHAT = 0x02
AUTO_ADD_REPEATER = 0x04
AUTO_ADD_ROOM_SERVER = 0x08
AUTO_ADD_SENSOR = 0x10
AUTO_ADD_ALL_TYPES = (
    AUTO_ADD_OVERWRITE_OLDEST
    | AUTO_ADD_CHAT
    | AUTO_ADD_REPEATER
    | AUTO_ADD_ROOM_SERVER
    | AUTO_ADD_SENSOR
)


def _manual_add_flag(payload: dict[str, Any] | None) -> bool | None:
    if not isinstance(payload, dict):
        return None
    if "manual_add_contacts" in payload:
        return bool(payload["manual_add_contacts"])
    return None


def _autoadd_config_byte(payload: dict[str, Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    if "config" in payload:
        try:
            return int(payload["config"]) & 0xFF
        except (TypeError, ValueError):
            return None
    return None


def _autoadd_max_hops(payload: dict[str, Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    if "max_hops" in payload:
        try:
            return int(payload["max_hops"]) & 0xFF
        except (TypeError, ValueError):
            return None
    return None


def _format_autoadd_mask(value: int | None) -> str:
    if value is None:
        return "?"
    parts: list[str] = []
    if value & AUTO_ADD_OVERWRITE_OLDEST:
        parts.append("overwrite_oldest")
    if value & AUTO_ADD_CHAT:
        parts.append("chat")
    if value & AUTO_ADD_REPEATER:
        parts.append("repeater")
    if value & AUTO_ADD_ROOM_SERVER:
        parts.append("room")
    if value & AUTO_ADD_SENSOR:
        parts.append("sensor")
    if not parts:
        parts.append("none")
    return f"0x{value:02x}({','.join(parts)})"


async def _read_companion_contact_autosave_state(mesh: MeshCore) -> dict[str, Any]:
    state: dict[str, Any] = {
        "manual_add_contacts": None,
        "autoadd_config": None,
        "autoadd_max_hops": None,
    }
    commands = getattr(mesh, "commands", None)
    if commands is None:
        return state

    appstart = getattr(commands, "send_appstart", None)
    if callable(appstart):
        try:
            ev = await appstart()
            if ev.type != EventType.ERROR:
                state["manual_add_contacts"] = _manual_add_flag(ev.payload)
        except Exception:
            logger.debug("send_appstart failed while reading contact autosave state", exc_info=True)

    get_autoadd = getattr(commands, "get_autoadd_config", None)
    if callable(get_autoadd):
        try:
            ev = await get_autoadd()
            if ev.type != EventType.ERROR:
                state["autoadd_config"] = _autoadd_config_byte(ev.payload)
                state["autoadd_max_hops"] = _autoadd_max_hops(ev.payload)
        except Exception:
            logger.debug("get_autoadd_config failed while reading contact autosave state", exc_info=True)

    return state


async def ensure_companion_contacts_from_adverts(mesh: MeshCore) -> None:
    """Force companion to persist all advert contacts and keep meshcore cache in sync."""
    commands = getattr(mesh, "commands", None)
    if commands is None:
        logger.warning("Companion contact autosave: mesh.commands unavailable, skipped")
        return

    before = await _read_companion_contact_autosave_state(mesh)
    before_client_auto = bool(getattr(mesh, "auto_update_contacts", False))

    applied: list[str] = []
    warnings: list[str] = []

    set_manual = getattr(commands, "set_manual_add_contacts", None)
    if callable(set_manual):
        try:
            ev = await set_manual(False)
            if ev.type == EventType.ERROR:
                warnings.append(f"set_manual_add_contacts(False): {ev.payload}")
            else:
                applied.append("manual_add_contacts=False")
        except Exception:
            logger.exception("Companion contact autosave: set_manual_add_contacts(False) failed")
    else:
        warnings.append("set_manual_add_contacts unavailable")

    set_autoadd = getattr(commands, "set_autoadd_config", None)
    if callable(set_autoadd):
        try:
            ev = await set_autoadd(AUTO_ADD_ALL_TYPES)
            if ev.type == EventType.ERROR:
                warnings.append(
                    f"set_autoadd_config(0x{AUTO_ADD_ALL_TYPES:02x}): {ev.payload}"
                )
            else:
                applied.append(f"autoadd_config=0x{AUTO_ADD_ALL_TYPES:02x}")
        except Exception:
            logger.exception(
                "Companion contact autosave: set_autoadd_config(0x%02x) failed",
                AUTO_ADD_ALL_TYPES,
            )
    else:
        warnings.append("set_autoadd_config unavailable")

    try:
        mesh.auto_update_contacts = True
        applied.append("mesh.auto_update_contacts=True")
    except Exception:
        logger.exception("Companion contact autosave: could not set mesh.auto_update_contacts")

    after = await _read_companion_contact_autosave_state(mesh)
    after_client_auto = bool(getattr(mesh, "auto_update_contacts", False))

    logger.info(
        "Companion contact autosave enabled: applied=%s manual_add_contacts %s -> %s "
        "autoadd_config %s -> %s autoadd_max_hops %s -> %s mesh.auto_update_contacts %s -> %s",
        applied or ["none"],
        before.get("manual_add_contacts"),
        after.get("manual_add_contacts"),
        _format_autoadd_mask(before.get("autoadd_config")),
        _format_autoadd_mask(after.get("autoadd_config")),
        before.get("autoadd_max_hops"),
        after.get("autoadd_max_hops"),
        before_client_auto,
        after_client_auto,
    )
    for msg in warnings:
        logger.warning("Companion contact autosave: %s", msg)
