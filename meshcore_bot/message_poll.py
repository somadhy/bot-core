"""Continuous get_msg polling for serial companions.

meshcore's ``start_auto_message_fetching`` only runs ``get_msg`` in a tight loop until
``NO_MORE_MSGS``, then stops until ``MESSAGES_WAITING``. Some firmware builds rarely
emit ``MESSAGES_WAITING`` for channel RX, so nothing is ever pulled from the device
queue after the first empty read — the bot stays silent even though RF traffic exists.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from meshcore import EventType

if TYPE_CHECKING:
    from meshcore import MeshCore

logger = logging.getLogger(__name__)

_DEFAULT_IDLE_SEC = 0.7
_DEFAULT_GET_MSG_TIMEOUT = 4.0


def _short_payload(r) -> str:
    pl = r.payload
    if not isinstance(pl, dict):
        return repr(pl)[:200]
    keys = list(pl.keys())[:12]
    return "{" + ", ".join(f"{k}:…" for k in keys) + "}"


async def run_serial_message_poll(
    mesh: MeshCore,
    shutdown: asyncio.Event,
    enabled_channel_indices: frozenset[int],
) -> None:
    idle = float(os.environ.get("MESHCORE_BOT_POLL_IDLE_SEC", _DEFAULT_IDLE_SEC))
    idle = max(0.2, min(idle, 30.0))
    timeout = float(os.environ.get("MESHCORE_BOT_GET_MSG_TIMEOUT", _DEFAULT_GET_MSG_TIMEOUT))
    timeout = max(0.5, min(timeout, 120.0))

    trace = os.environ.get("MESHCORE_BOT_TRACE_POLL", "").lower() in ("1", "true", "yes")

    logger.info(
        "Message poll: continuous get_msg (timeout=%.1fs, idle when empty=%.2fs); "
        "set MESHCORE_BOT_POLL_IDLE_SEC / MESHCORE_BOT_GET_MSG_TIMEOUT to tune; "
        "MESHCORE_BOT_TRACE_POLL=1 logs every get_msg result",
        timeout,
        idle,
    )

    last_rx_wall = time.monotonic()
    while not shutdown.is_set():
        try:
            r = await mesh.commands.get_msg(timeout=timeout)
            if trace:
                logger.info("get_msg → %s payload=%s", r.type, _short_payload(r))

            if r.type == EventType.CHANNEL_MSG_RECV:
                pl = r.payload if isinstance(r.payload, dict) else {}
                idx = pl.get("channel_idx")
                try:
                    ch = int(idx) if idx is not None else -1
                except (TypeError, ValueError):
                    ch = -1
                preview = (str(pl.get("text", "")))[:120]
                if ch in enabled_channel_indices:
                    logger.info(
                        "get_msg returned CHANNEL_MSG_RECV idx=%s preview=%r",
                        ch,
                        preview,
                    )
                else:
                    logger.debug(
                        "get_msg returned CHANNEL_MSG_RECV idx=%s preview=%r (not in enabled_indices)",
                        ch,
                        preview,
                    )
                last_rx_wall = time.monotonic()
            elif r.type == EventType.CONTACT_MSG_RECV:
                pl = r.payload if isinstance(r.payload, dict) else {}
                logger.info(
                    "get_msg returned CONTACT_MSG_RECV from=%s preview=%r",
                    pl.get("pubkey_prefix"),
                    (str(pl.get("text", "")))[:120],
                )
                last_rx_wall = time.monotonic()

            if r.type == EventType.NO_MORE_MSGS:
                if time.monotonic() - last_rx_wall > 90.0:
                    logger.warning(
                        "No CHANNEL/CONTACT message from companion via get_msg() for 90s "
                        "(only NO_MORE_MSGS/idle). Traffic may not reach this USB node, or "
                        "another app holds the serial port. Try: MESHCORE_BOT_TRACE_POLL=1 "
                        "and confirm only one process uses the radio."
                    )
                    last_rx_wall = time.monotonic()
                await asyncio.sleep(idle)
                continue
            if r.type == EventType.ERROR:
                pl = r.payload if isinstance(r.payload, dict) else {}
                reason = pl.get("reason")
                if reason in ("timeout", "no_event_received"):
                    if time.monotonic() - last_rx_wall > 90.0:
                        logger.warning(
                            "get_msg only timeouts/no_event for 90s — check USB companion "
                            "and that this container is the only user of the serial device."
                        )
                        last_rx_wall = time.monotonic()
                    await asyncio.sleep(idle)
                    continue
                logger.warning("get_msg error: %s", r.payload)
                await asyncio.sleep(1.0)
                continue
            # Message delivered to subscribers; drain quickly if more queued.
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("message poll loop")
            await asyncio.sleep(2.0)
