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
        cr = await mesh.commands.get_contacts()
        if cr.type == EventType.ERROR:
            logger.warning("get_contacts: %s", cr.payload)
    except Exception:
        logger.exception("get_contacts failed")

    # Subscribe before polling so CHANNEL_MSG_RECV handlers are registered first.
    svc = BotService(cfg, mesh, i18n, blacklist, shutdown)
    svc.attach()
    # Continuous get_msg — see message_poll.py (start_auto_message_fetching alone can stall).
    poll_task = asyncio.create_task(
        run_serial_message_poll(mesh, shutdown, frozenset(cfg.channels_enabled))
    )

    try:
        ch_table = await fetch_channel_table(mesh, cfg.channels_enabled)
    except Exception:
        logger.exception("fetch_channel_table failed; logging config indices only")
        ch_table = {}

    logger.info(
        "Bot running (locale=%s, dm=%s)",
        cfg.locale,
        cfg.dm_enabled,
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
