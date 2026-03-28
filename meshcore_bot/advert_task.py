"""Periodic device advertisement (send_advert) for mesh visibility."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from meshcore import EventType

if TYPE_CHECKING:
    from meshcore import MeshCore

logger = logging.getLogger(__name__)


async def run_periodic_advert(
    mesh: MeshCore,
    shutdown: asyncio.Event,
    interval_hours: float,
    flood: bool,
) -> None:
    """Sleep `interval_hours`, then call `send_advert` in a loop until shutdown."""
    if interval_hours <= 0:
        return

    interval_sec = interval_hours * 3600.0
    trace = os.environ.get("MESHCORE_BOT_ADVERT_TRACE", "").lower() in ("1", "true", "yes")

    logger.info(
        "Periodic advert: every %.3g h (flood=%s); first send after the first full interval",
        interval_hours,
        flood,
    )

    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_sec)
            return
        except asyncio.TimeoutError:
            pass
        if shutdown.is_set():
            return
        try:
            r = await mesh.commands.send_advert(flood=flood)
            if r.type == EventType.ERROR:
                logger.warning("send_advert failed: %s", r.payload)
            elif trace:
                logger.info("send_advert ok (flood=%s) payload=%s", flood, r.payload)
            else:
                logger.info("send_advert ok (flood=%s)", flood)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("send_advert failed")
