from __future__ import annotations

import datetime as dt
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# service.py imports meshcore at module import time; provide a lightweight stub
# so these tests can run without hardware stack dependencies.
_meshcore_stub = types.ModuleType("meshcore")
_meshcore_stub.EventType = object()
_meshcore_stub.MeshCore = object
sys.modules.setdefault("meshcore", _meshcore_stub)

_meshcore_events_stub = types.ModuleType("meshcore.events")
_meshcore_events_stub.Event = object
sys.modules.setdefault("meshcore.events", _meshcore_events_stub)

from meshcore_bot import service
from meshcore_bot.commands.router import CmdKind, ParsedCommand, parse_incoming
from meshcore_bot.commands import weather_cmd
from meshcore_bot.commands.weather_cmd import WeatherPayload


class _I18nStub:
    def t(self, key: str, **kwargs: object) -> str:
        return key


class WeatherLocalTimeTests(unittest.TestCase):
    def test_append_weather_local_time_skips_when_tz_absent(self) -> None:
        payload = WeatherPayload(weather_body="Moscow\n☀️", tz_offset_seconds=None)
        self.assertEqual(service._append_weather_local_time(payload), "Moscow\n☀️")

    def test_append_weather_local_time_uses_hhmm_format(self) -> None:
        class _FakeDateTime(dt.datetime):
            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                base = dt.datetime(2026, 4, 17, 12, 34, tzinfo=dt.timezone.utc)
                return base.astimezone(tz) if tz is not None else base

        payload = WeatherPayload(weather_body="Yekaterinburg\n☁️", tz_offset_seconds=5 * 3600)
        with patch("meshcore_bot.service._dt.datetime", _FakeDateTime):
            out = service._append_weather_local_time(payload)

        self.assertTrue(out.startswith("Yekaterinburg\n☁️\n🕒"))
        self.assertRegex(out, r"\n🕒\d{2}:\d{2}$")
        self.assertTrue(out.endswith("🕒17:34"))

    def test_time_line_uses_unknown_when_tz_absent(self) -> None:
        self.assertEqual(service._time_line("Moscow", None), "Moscow\n🕒?")

    def test_time_line_uses_hhmm_when_offset_exists(self) -> None:
        class _FakeDateTime(dt.datetime):
            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                base = dt.datetime(2026, 4, 17, 8, 0, tzinfo=dt.timezone.utc)
                return base.astimezone(tz) if tz is not None else base

        with patch("meshcore_bot.service._dt.datetime", _FakeDateTime):
            out = service._time_line("Tokyo", 9 * 3600)
        self.assertEqual(out, "Tokyo\n🕒17:00")


class RouterTimeTests(unittest.TestCase):
    def test_parse_incoming_time_base_aliases(self) -> None:
        parsed = parse_incoming("время Тюмень")
        self.assertEqual(parsed.kind, CmdKind.TIME)
        self.assertEqual(parsed.arg, "Тюмень")

    def test_parse_incoming_time_custom_alias(self) -> None:
        cfg = types.SimpleNamespace(command_aliases={"time": ["tm"]})
        parsed = parse_incoming("tm London", cfg)
        self.assertEqual(parsed.kind, CmdKind.TIME)
        self.assertEqual(parsed.arg, "London")

    def test_parse_incoming_node_base_alias(self) -> None:
        parsed = parse_incoming("узел 12ab")
        self.assertEqual(parsed.kind, CmdKind.NODE)
        self.assertEqual(parsed.arg, "12ab")


class TimeCommandServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_contact_time_uses_default_city_when_arg_missing(self) -> None:
        cfg = types.SimpleNamespace(
            dm_enabled=True,
            weather_default_city="Moscow",
            channels_enabled=[0],
            admin_public_keys=[],
            admin_channel_indices=[],
            reply_delay_sec=0,
            dm_delivery_wait_sec=0,
            dm_delivery_max_attempts=1,
            node_advert_store_path=Path("/tmp/test-node-adverts.json"),
            node_advert_retention_days=7,
            node_advert_max_stored=5000,
        )
        mesh = types.SimpleNamespace(get_contact_by_key_prefix=lambda _p: None)
        blacklist = types.SimpleNamespace(is_blocked=lambda *_a, **_k: False)
        svc = service.BotService(
            cfg=cfg,
            mesh=mesh,
            i18n=_I18nStub(),
            blacklist=blacklist,
            shutdown=types.SimpleNamespace(set=lambda: None),
        )
        svc._send_dm = AsyncMock()  # type: ignore[method-assign]
        event = types.SimpleNamespace(
            payload={"text": "time", "pubkey_prefix": "abc123"},
            attributes={},
        )
        mocked_fetch = AsyncMock(return_value=WeatherPayload("Moscow\n☀️", tz_offset_seconds=10800))
        with (
            patch("meshcore_bot.service.parse_incoming", return_value=ParsedCommand(CmdKind.TIME, "")),
            patch("meshcore_bot.service.fetch_weather_payload", mocked_fetch),
        ):
            await svc._on_contact_msg(event)

        mocked_fetch.assert_awaited_once_with("Moscow", cfg, svc._i18n, use_cache=False)
        svc._send_dm.assert_awaited_once()  # type: ignore[attr-defined]


class WeatherPayloadFetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        weather_cmd._weather_cache.clear()

    async def test_fetch_weather_payload_uses_cache(self) -> None:
        cfg = types.SimpleNamespace(
            weather_provider="openmeteo",
            weather_provider_fallback="",
            weather_cache_ttl_minutes=15.0,
            locale="en",
        )
        i18n = _I18nStub()

        mocked_fetch = AsyncMock(
            return_value=(True, WeatherPayload("Berlin\n☀️", tz_offset_seconds=7200), "provider_error")
        )
        with patch("meshcore_bot.commands.weather_cmd._fetch_open_meteo", mocked_fetch):
            p1 = await weather_cmd.fetch_weather_payload("Berlin", cfg, i18n)
            p2 = await weather_cmd.fetch_weather_payload("Berlin", cfg, i18n)

        self.assertEqual(p1, p2)
        mocked_fetch.assert_awaited_once()

    async def test_fetch_weather_line_returns_only_weather_body(self) -> None:
        cfg = types.SimpleNamespace(
            weather_provider="openmeteo",
            weather_provider_fallback="",
            weather_cache_ttl_minutes=0.0,
            locale="en",
        )
        i18n = _I18nStub()

        mocked_fetch = AsyncMock(
            return_value=(True, WeatherPayload("Paris\n🌧️", tz_offset_seconds=3600), "provider_error")
        )
        with patch("meshcore_bot.commands.weather_cmd._fetch_open_meteo", mocked_fetch):
            line = await weather_cmd.fetch_weather_line("Paris", cfg, i18n)

        self.assertEqual(line, "Paris\n🌧️")

    async def test_fetch_weather_payload_bypasses_cache_when_requested(self) -> None:
        cfg = types.SimpleNamespace(
            weather_provider="openmeteo",
            weather_provider_fallback="",
            weather_cache_ttl_minutes=15.0,
            locale="en",
        )
        i18n = _I18nStub()
        mocked_fetch = AsyncMock(
            return_value=(True, WeatherPayload("Rome\n☀️", tz_offset_seconds=3600), "provider_error")
        )
        with patch("meshcore_bot.commands.weather_cmd._fetch_open_meteo", mocked_fetch):
            await weather_cmd.fetch_weather_payload("Rome", cfg, i18n, use_cache=False)
            await weather_cmd.fetch_weather_payload("Rome", cfg, i18n, use_cache=False)

        self.assertEqual(mocked_fetch.await_count, 2)


if __name__ == "__main__":
    unittest.main()
