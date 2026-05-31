"""Periodic sync of companion contacts into the node advert store."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshcore_bot.service import BotService

logger = logging.getLogger(__name__)


async def run_periodic_node_sync(
    svc: BotService,
    shutdown: asyncio.Event,
    interval_minutes: float,
) -> None:
    """Refresh node store from companion contacts every `interval_minutes` until shutdown."""
    if interval_minutes <= 0:
        return

    interval_sec = interval_minutes * 60.0
    logger.info(
        "Periodic node contact sync: every %.3g min; first sync after the first full interval",
        interval_minutes,
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
            await svc.refresh_node_store_from_contacts()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("periodic node contact sync failed")
