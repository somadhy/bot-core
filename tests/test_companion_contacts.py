"""Tests for companion contact autosave setup."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from meshcore_bot.companion_contacts import (
    AUTO_ADD_ALL_TYPES,
    ensure_companion_contacts_from_adverts,
)


class CompanionContactsAutosaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_enable_calls_device_commands_and_client_flag(self) -> None:
        mesh = MagicMock()
        mesh.auto_update_contacts = False
        mesh.commands.send_appstart = AsyncMock(
            side_effect=[
                MagicMock(type="self_info", payload={"manual_add_contacts": True}),
                MagicMock(type="self_info", payload={"manual_add_contacts": False}),
            ]
        )
        mesh.commands.get_autoadd_config = AsyncMock(
            side_effect=[
                MagicMock(type="autoadd_config", payload={"config": 0}),
                MagicMock(type="autoadd_config", payload={"config": AUTO_ADD_ALL_TYPES}),
            ]
        )
        mesh.commands.set_manual_add_contacts = AsyncMock(
            return_value=MagicMock(type="ok", payload={})
        )
        mesh.commands.set_autoadd_config = AsyncMock(
            return_value=MagicMock(type="ok", payload={})
        )

        await ensure_companion_contacts_from_adverts(mesh)

        mesh.commands.set_manual_add_contacts.assert_awaited_once_with(False)
        mesh.commands.set_autoadd_config.assert_awaited_once_with(AUTO_ADD_ALL_TYPES)
        self.assertTrue(mesh.auto_update_contacts)


if __name__ == "__main__":
    unittest.main()
