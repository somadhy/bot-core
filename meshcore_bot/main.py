"""Entry point: serial MeshCore bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from meshcore import EventType, MeshCore

from meshcore_bot.blacklist import Blacklist
from meshcore_bot.channel_info import (
    fetch_channel_table,
    format_companion_table_line,
    format_listening_line,
)
from meshcore_bot.advert_task import run_periodic_advert
from meshcore_bot.config import load_config
from meshcore_bot.i18n import I18n
from meshcore_bot.message_poll import run_serial_message_poll
from meshcore_bot.service import BotService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
if os.environ.get("MESHCORE_BOT_DEBUG", "").lower() in ("1", "true", "yes"):
    logging.getLogger("meshcore_bot.service").setLevel(logging.DEBUG)
    logging.getLogger("meshcore_bot.message_poll").setLevel(logging.DEBUG)
logger = logging.getLogger("meshcore_bot")


async def _ensure_companion_contacts_persistence(mesh: MeshCore) -> None:
    """Try to enable companion contact autosave/rotation if API is available."""
    commands = getattr(mesh, "commands", None)
    if commands is None:
        return
    # Different meshcore versions may expose different names.
    toggles = (
        ("enable_contacts_autosave", (), {}),
        ("set_contacts_autosave", (True,), {}),
        ("set_contact_autosave", (True,), {}),
    )
    rotation_calls = (
        ("enable_contacts_rotation", (), {}),
        ("set_contacts_rotation", (True,), {}),
    )
    for name, args, kwargs in toggles:
        fn = getattr(commands, name, None)
        if callable(fn):
            try:
                await fn(*args, **kwargs)
                logger.info("Companion contacts autosave enabled via %s()", name)
                break
            except Exception:
                logger.warning("Could not enable contacts autosave via %s()", name, exc_info=True)
    for name, args, kwargs in rotation_calls:
        fn = getattr(commands, name, None)
        if callable(fn):
            try:
                await fn(*args, **kwargs)
                logger.info("Companion contacts rotation enabled via %s()", name)
                break
            except Exception:
                logger.warning("Could not enable contacts rotation via %s()", name, exc_info=True)


async def async_main() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        logger.error("%s", e)
        return 1

    i18n = I18n(cfg.locale)
    blacklist = Blacklist(cfg.blacklist_path)

    shutdown = asyncio.Event()
    mesh_debug = os.environ.get("MESHCORE_BOT_MESH_DEBUG", "").lower() in ("1", "true", "yes")
    mesh = await MeshCore.create_serial(cfg.serial_device, cfg.serial_baudrate, debug=mesh_debug)
    if mesh is None:
        logger.error("Could not connect to MeshCore on %s", cfg.serial_device)
        return 1

    try:
        await _ensure_companion_contacts_persistence(mesh)
        cr = await mesh.commands.get_contacts()
        if cr.type == EventType.ERROR:
            logger.warning("get_contacts: %s", cr.payload)
    except Exception:
        logger.exception("get_contacts failed")

    # Subscribe before polling so CHANNEL_MSG_RECV handlers are registered first.
    svc = BotService(cfg, mesh, i18n, blacklist, shutdown)
    try:
        refreshed = await svc.refresh_node_store_from_contacts()
        logger.info("Node advert store synced from contacts: %s rows", refreshed)
    except Exception:
        logger.exception("initial node advert store sync failed")
    svc.attach()
    # Continuous get_msg — see message_poll.py (start_auto_message_fetching alone can stall).
    poll_task = asyncio.create_task(
        run_serial_message_poll(
            mesh,
            shutdown,
            frozenset(cfg.channels_enabled),
            keepalive_sec=cfg.poll_keepalive_sec,
            keepalive_only_when_idle_sec=cfg.poll_keepalive_only_when_idle_sec,
        )
    )
    advert_task: asyncio.Task[None] | None = None
    if cfg.advert_interval_hours > 0:
        advert_task = asyncio.create_task(
            run_periodic_advert(mesh, shutdown, cfg.advert_interval_hours, cfg.advert_flood)
        )

    try:
        ch_table = await fetch_channel_table(mesh, cfg.channels_enabled)
    except Exception:
        logger.exception("fetch_channel_table failed; logging config indices only")
        ch_table = {}

    logger.info(
        "Bot running (locale=%s, dm=%s, advert=%s)",
        cfg.locale,
        cfg.dm_enabled,
        (
            f"every {cfg.advert_interval_hours:.3g} h (flood={cfg.advert_flood})"
            if cfg.advert_interval_hours > 0
            else "off"
        ),
    )
    logger.info(
        "Channels the bot listens to (from config channels.enabled_indices): %s",
        format_listening_line(cfg.channels_enabled, ch_table),
    )
    logger.info(
        "Companion channel slots (device): %s",
        format_companion_table_line(ch_table),
    )

    await shutdown.wait()

    if advert_task is not None:
        advert_task.cancel()
        try:
            await advert_task
        except asyncio.CancelledError:
            pass
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    await mesh.disconnect()
    logger.info("Disconnected.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="MeshCore Companion bot")
    parser.add_argument(
        "--diagnose",
        "-d",
        action="store_true",
        help="Show device info and channel indices, then exit (does not run the bot).",
    )
    args = parser.parse_args()
    if args.diagnose:
        from meshcore_bot.diagnose import main as diagnose_main

        diagnose_main()
        return

    try:
        code = asyncio.run(async_main())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
