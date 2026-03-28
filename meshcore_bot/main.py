"""Entry point: serial MeshCore bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from meshcore import EventType, MeshCore

from meshcore_bot.blacklist import Blacklist
from meshcore_bot.config import load_config
from meshcore_bot.i18n import I18n
from meshcore_bot.service import BotService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
    mesh = await MeshCore.create_serial(cfg.serial_device, cfg.serial_baudrate)
    if mesh is None:
        logger.error("Could not connect to MeshCore on %s", cfg.serial_device)
        return 1

    try:
        cr = await mesh.commands.get_contacts()
        if cr.type == EventType.ERROR:
            logger.warning("get_contacts: %s", cr.payload)
    except Exception:
        logger.exception("get_contacts failed")

    await mesh.start_auto_message_fetching()
    svc = BotService(cfg, mesh, i18n, blacklist, shutdown)
    svc.attach()

    logger.info(
        "Bot running (locale=%s, channels=%s, dm=%s)",
        cfg.locale,
        cfg.channels_enabled,
        cfg.dm_enabled,
    )

    await shutdown.wait()

    try:
        await mesh.stop_auto_message_fetching()
    except Exception:
        logger.debug("stop_auto_message_fetching", exc_info=True)
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
