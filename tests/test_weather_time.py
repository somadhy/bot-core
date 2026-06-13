from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import tempfile
import types
import unittest
import unittest.mock
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

    def test_pathinfo_direct_includes_signal_quality_and_hash_len(self) -> None:
        out = service._pathinfo_from_message_payload(
            {"path_len": 0, "hash_len": "2b"},
            "📶7.5",
        )
        self.assertEqual(out, "🔢2, ↔️direct, 📶7.5")

    def test_format_path_by_mode_0_one_byte_groups(self) -> None:
        out = service._format_pathinfo(3, ["aa", "bb", "cc"], hash_len=1)
        self.assertEqual(out, "🔢1, 🪜3, 🧭aa:bb:cc")

    def test_format_path_by_mode_1_two_byte_groups(self) -> None:
        out = service._format_pathinfo(3, ["aa", "aa", "bb", "bb", "cc", "cc"], hash_len=2)
        self.assertEqual(out, "🔢2, 🪜3, 🧭aaaa:bbbb:cccc")

    def test_format_path_by_mode_2_three_byte_groups(self) -> None:
        out = service._format_pathinfo(
            3,
            ["aa", "aa", "aa", "bb", "bb", "bb", "cc", "cc", "cc"],
            hash_len=3,
        )
        self.assertEqual(out, "🔢3, 🪜3, 🧭aaaaaa:bbbbbb:cccccc")

    def test_extract_signal_quality_accepts_uppercase_keys(self) -> None:
        out = service._extract_signal_quality({"SNR": 12.25, "RSSI": -101}, None)
        self.assertEqual(out, "📶12.25")

    def test_extract_signal_quality_supports_nested_metrics(self) -> None:
        out = service._extract_signal_quality({"radio": {"snr_db": 9.5, "rssi_dbm": -96}}, None)
        self.assertEqual(out, "📶9.5")

    def test_path_hash_mode_is_used_as_hash_len(self) -> None:
        out = service._pathinfo_from_message_payload({"path_len": 0, "path_hash_mode": 1}, "📶12.25")
        self.assertEqual(out, "🔢2, ↔️direct, 📶12.25")

    def test_path_hash_mode_zero_maps_to_one_byte(self) -> None:
        out = service._pathinfo_from_message_payload({"path_len": 0, "path_hash_mode": 0}, "📶12.25")
        self.assertEqual(out, "🔢1, ↔️direct, 📶12.25")

    def test_merge_signal_quality_keeps_previous_snr(self) -> None:
        merged = service._merge_signal_quality("📶11.0", "📶?")
        self.assertEqual(merged, "📶11.0")

    def test_pathinfo_includes_packet_age_from_sender_timestamp(self) -> None:
        out = service._pathinfo_from_message_payload(
            {"path_len": 0, "path_hash_mode": 1, "sender_timestamp": 997, "recv_time": 1000},
            "📶12.25",
        )
        self.assertEqual(out, "🔢2, ↔️direct, 📶12.25, ⏱3s")

    def test_pathinfo_non_direct_omits_snr(self) -> None:
        out = service._pathinfo_from_message_payload(
            {"path_len": 3, "path_hash_mode": 1, "path": "aaaabbbbcccc"},
            "📶10.5",
        )
        self.assertEqual(out, "🔢2, 🪜3, 🧭aaaa:bbbb:cccc")

    def test_pathinfo_non_direct_hides_route_when_missing(self) -> None:
        out = service._pathinfo_from_message_payload({"path_len": 3, "path_hash_mode": 1}, "📶10.5")
        self.assertEqual(out, "🔢2, 🪜3")

    def test_pathinfo_reads_path_len_from_attrs(self) -> None:
        out = service._pathinfo_from_message_payload(
            {},
            "📶10.5",
            {"path_len": 3, "path_hash_mode": 1, "path": "aaaabbbbcccc"},
        )
        self.assertEqual(out, "🔢2, 🪜3, 🧭aaaa:bbbb:cccc")

    def test_pathinfo_rejects_sentinel_255_hops(self) -> None:
        out = service._pathinfo_from_message_payload({"path_len": 255, "path_hash_mode": 1}, "📶1")
        self.assertIsNone(out)

    def test_pathinfo_rejects_implausible_hop_count(self) -> None:
        out = service._pathinfo_from_message_payload({"path_len": 100}, "📶1")
        self.assertIsNone(out)

    def test_parse_rx_log_text_msg_direct_uses_path_len_not_body_prefix(self) -> None:
        d = {
            "raw_hex": "2dcc0a8070ee89e71a471f88421f7dbb68c46674e7b3b66d",
            "payload": "0a8070ee89e71a471f88421f7dbb68c46674e7b3b66d",
            "path_len": 0,
            "path_hash_size": 3,
            "path": "",
            "payload_typename": "TEXT_MSG",
            "route_typename": "DIRECT",
            "payload_type": 2,
            "route_type": 2,
        }
        out = service._parse_rx_log_data(d)
        self.assertEqual(out, {"path_len": 0})

    def test_parse_rx_log_text_msg_no_structured_does_not_flood_parse(self) -> None:
        d = {
            "payload": "0a8070ee89e71a471f88421f7dbb68c46674e7b3b66d",
            "payload_typename": "TEXT_MSG",
        }
        self.assertEqual(service._parse_rx_log_data(d), {})

    def test_parse_rx_log_flood_body_when_no_structured_path(self) -> None:
        out = service._parse_rx_log_data({"payload": "0001aa"})
        self.assertEqual(out.get("path_len"), 1)
        self.assertEqual(out.get("path_nodes"), ["aa"])

    def test_pathinfo_hides_zero_or_negative_age(self) -> None:
        with patch("meshcore_bot.service.time.time", return_value=1000):
            out = service._pathinfo_from_message_payload(
                {"path_len": 0, "path_hash_mode": 1, "sender_timestamp": 1000},
                "📶12.25",
            )
        self.assertEqual(out, "🔢2, ↔️direct, 📶12.25")


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

    def test_parse_incoming_ping_base_aliases(self) -> None:
        parsed_ru = parse_incoming("пинг")
        parsed_en = parse_incoming("ping")
        self.assertEqual(parsed_ru.kind, CmdKind.PING)
        self.assertEqual(parsed_en.kind, CmdKind.PING)


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
            channel_delivery_wait_sec=10,
            channel_delivery_min_rx_repeats=1,
            channel_delivery_max_attempts=3,
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

    async def test_contact_ping_replies_without_racket(self) -> None:
        cfg = types.SimpleNamespace(
            dm_enabled=True,
            weather_default_city="Moscow",
            channels_enabled=[0],
            admin_public_keys=[],
            admin_channel_indices=[],
            reply_delay_sec=0,
            dm_delivery_wait_sec=0,
            dm_delivery_max_attempts=1,
            channel_delivery_wait_sec=10,
            channel_delivery_min_rx_repeats=1,
            channel_delivery_max_attempts=3,
            node_advert_store_path=Path("/tmp/test-node-adverts.json"),
            node_advert_retention_days=7,
            node_advert_max_stored=5000,
        )
        mesh = types.SimpleNamespace(get_contact_by_key_prefix=lambda _p: None)
        blacklist = types.SimpleNamespace(is_blocked=lambda *_a, **_k: False)
        svc = service.BotService(
            cfg=cfg,
            mesh=mesh,
            i18n=types.SimpleNamespace(t=lambda k, **_kw: "" if k == "ping.pong" else k),
            blacklist=blacklist,
            shutdown=types.SimpleNamespace(set=lambda: None),
        )
        svc._send_dm = AsyncMock()  # type: ignore[method-assign]
        event = types.SimpleNamespace(
            payload={"text": "ping", "pubkey_prefix": "abc123"},
            attributes={},
        )
        with patch("meshcore_bot.service.parse_incoming", return_value=ParsedCommand(CmdKind.PING, "")):
            await svc._on_contact_msg(event)

        svc._send_dm.assert_awaited_once()  # type: ignore[attr-defined]
        sent_text = svc._send_dm.await_args.args[1]  # type: ignore[attr-defined]
        self.assertNotIn("🏓", sent_text)
        self.assertIn("🪜", sent_text)

    async def test_rx_log_prefers_payload_path_and_hash_size(self) -> None:
        cfg = types.SimpleNamespace(
            dm_enabled=True,
            weather_default_city="Moscow",
            channels_enabled=[0],
            admin_public_keys=[],
            admin_channel_indices=[],
            reply_delay_sec=0,
            dm_delivery_wait_sec=0,
            dm_delivery_max_attempts=1,
            channel_delivery_wait_sec=10,
            channel_delivery_min_rx_repeats=1,
            channel_delivery_max_attempts=3,
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

        event = types.SimpleNamespace(
            payload={
                "path_len": 3,
                "path_hash_size": 2,
                "path": "aaaabbbbcccc",
                "snr": 11.75,
            },
            attributes={},
        )
        await svc._on_rx_log_data(event)

        self.assertEqual(svc._latest_pathinfo_str, "🔢2, 🪜3, 🧭aaaa:bbbb:cccc")

    async def test_rx_log_text_msg_direct_includes_path_hash_size_and_direct(self) -> None:
        cfg = types.SimpleNamespace(
            dm_enabled=True,
            weather_default_city="Moscow",
            channels_enabled=[0],
            admin_public_keys=[],
            admin_channel_indices=[],
            reply_delay_sec=0,
            dm_delivery_wait_sec=0,
            dm_delivery_max_attempts=1,
            channel_delivery_wait_sec=10,
            channel_delivery_min_rx_repeats=1,
            channel_delivery_max_attempts=3,
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
        event = types.SimpleNamespace(
            payload={
                "path_len": 0,
                "path_hash_size": 3,
                "path": "",
                "snr": 12.0,
                "payload_typename": "TEXT_MSG",
                "route_typename": "DIRECT",
                "route_type": 2,
            },
            attributes={"path_len": 0, "path": ""},
        )
        await svc._on_rx_log_data(event)
        s = svc._latest_pathinfo_str
        self.assertIn("🔢3", s)
        self.assertIn("↔️direct", s)
        self.assertIn("📶12", s)


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

    async def test_fetch_weather_payload_uses_fallback_when_primary_fails(self) -> None:
        cfg = types.SimpleNamespace(
            weather_provider="openmeteo",
            weather_provider_fallback="meteostat",
            weather_cache_ttl_minutes=0.0,
            locale="ru",
        )
        i18n = _I18nStub()
        primary = AsyncMock(
            return_value=(
                False,
                WeatherPayload("errors.weather_failed", tz_offset_seconds=None),
                "provider_error",
            )
        )
        fallback = AsyncMock(
            return_value=(True, WeatherPayload("Moscow\n☀️", tz_offset_seconds=10800), "provider_error")
        )
        with (
            patch("meshcore_bot.commands.weather_cmd._fetch_open_meteo", primary),
            patch("meshcore_bot.commands.weather_cmd._fetch_meteostat", fallback),
        ):
            payload = await weather_cmd.fetch_weather_payload("Moscow", cfg, i18n, use_cache=False)

        self.assertEqual(payload.weather_body, "Moscow\n☀️")
        primary.assert_awaited_once()
        fallback.assert_awaited_once()

    async def test_fetch_weather_payload_skips_unconfigured_fallback(self) -> None:
        cfg = types.SimpleNamespace(
            weather_provider="openmeteo",
            weather_provider_fallback="meteostat_rapidapi",
            weather_cache_ttl_minutes=0.0,
            locale="ru",
        )
        i18n = _I18nStub()
        primary = AsyncMock(
            return_value=(
                False,
                WeatherPayload("errors.weather_failed:451", tz_offset_seconds=None),
                "provider_error",
            )
        )
        fallback = AsyncMock(
            return_value=(
                False,
                WeatherPayload("errors.rapidapi_not_configured", tz_offset_seconds=None),
                "not_configured",
            )
        )
        with (
            patch("meshcore_bot.commands.weather_cmd._fetch_open_meteo", primary),
            patch("meshcore_bot.commands.weather_cmd._fetch_meteostat_rapidapi", fallback),
        ):
            payload = await weather_cmd.fetch_weather_payload("Moscow", cfg, i18n, use_cache=False)

        self.assertEqual(payload.weather_body, "errors.weather_failed:451")
        primary.assert_awaited_once()
        fallback.assert_awaited_once()

    async def test_geocode_city_falls_back_to_nominatim(self) -> None:
        cfg = types.SimpleNamespace(locale="ru")
        i18n = _I18nStub()
        client = AsyncMock()
        open_meteo = AsyncMock(return_value=(False, None, "provider_error"))
        nominatim = AsyncMock(
            return_value=(
                True,
                weather_cmd._GeocodeResult(
                    lat=55.625578,
                    lon=37.6063916,
                    name="Москва",
                    tz_offset_seconds=None,
                ),
                "provider_error",
            )
        )
        with (
            patch("meshcore_bot.commands.weather_cmd._geocode_open_meteo", open_meteo),
            patch("meshcore_bot.commands.weather_cmd._geocode_nominatim", nominatim),
        ):
            ok, geo, err = await weather_cmd._geocode_city("Moscow", cfg, i18n, client=client)

        self.assertTrue(ok)
        self.assertIsNotNone(geo)
        assert geo is not None
        self.assertEqual(geo.name, "Москва")
        self.assertEqual(err, "provider_error")
        open_meteo.assert_awaited_once()
        nominatim.assert_awaited_once()

    def test_request_error_detail_maps_connect_timeout(self) -> None:
        err = weather_cmd._request_error_detail(weather_cmd.httpx.ConnectTimeout(""), locale="ru")
        self.assertEqual(err, "таймаут")

    def test_wttr_in_request_url_matches_curl_format(self) -> None:
        url = weather_cmd._wttr_in_request_url(
            "https://v2.wttr.in",
            weather_cmd.quote("Moscow"),
            "ru",
        )
        self.assertEqual(url, "https://v2.wttr.in/Moscow?format=j1&lang=ru")

    def test_parse_wttr_j1_partial_from_truncated_body(self) -> None:
        body = (
            '{"current_condition":[{"FeelsLikeC":"30","humidity":"43","precipMM":"0.0",'
            '"pressure":"1008","temp_C":"28","weatherCode":"200","windspeedKmph":"15"}],'
            '"nearest_area":[{"areaName":[{"value":"Москва"}]}'
        )
        name, cur = weather_cmd._parse_wttr_j1_partial(body, fallback_city="Moscow")
        self.assertEqual(name, "Москва")
        self.assertEqual(cur["temp_C"], "28")
        self.assertEqual(cur["weatherCode"], "200")

    async def test_wttr_fetch_j1_text_curl_accepts_partial_on_timeout(self) -> None:
        body = (
            '{"current_condition":[{"humidity":"43","precipMM":"0.0","pressure":"1008",'
            '"temp_C":"28","weatherCode":"200","windspeedKmph":"15"}],'
            '"nearest_area":[{"areaName":[{"value":"Moscow"}]}'
        )
        proc = unittest.mock.MagicMock()
        proc.returncode = 28
        proc.communicate = AsyncMock(return_value=(body.encode(), b"curl: (28) timeout"))

        with patch("meshcore_bot.commands.weather_cmd.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            text = await weather_cmd._wttr_fetch_j1_text_curl("https://v2.wttr.in/Moscow?format=j1&lang=ru")

        self.assertIn('"temp_C":"28"', text)

    async def test_wttr_fetch_falls_back_to_curl_on_httpx_error(self) -> None:
        body = (
            '{"current_condition":[{"humidity":"43","precipMM":"0.0","pressure":"1008",'
            '"temp_C":"28","weatherCode":"200","windspeedKmph":"15"}],'
            '"nearest_area":[{"areaName":[{"value":"Moscow"}]}'
        )

        with (
            patch(
                "meshcore_bot.commands.weather_cmd._wttr_fetch_j1_text_httpx",
                AsyncMock(side_effect=weather_cmd.httpx.ConnectTimeout("")),
            ),
            patch(
                "meshcore_bot.commands.weather_cmd._wttr_fetch_j1_text_curl",
                AsyncMock(return_value=body),
            ) as curl_mock,
        ):
            text = await weather_cmd._wttr_fetch_j1_text("https://v2.wttr.in/Moscow?format=j1&lang=ru")

        self.assertIn('"temp_C":"28"', text)
        curl_mock.assert_awaited_once()

    async def test_fetch_weather_payload_uses_wttr_fallback_when_primary_fails(self) -> None:
        cfg = types.SimpleNamespace(
            weather_provider="openmeteo",
            weather_provider_fallback="wttr.in",
            weather_cache_ttl_minutes=0.0,
            locale="ru",
        )
        i18n = _I18nStub()
        primary = AsyncMock(
            return_value=(
                False,
                WeatherPayload("errors.weather_failed:451", tz_offset_seconds=None),
                "provider_error",
            )
        )
        fallback = AsyncMock(
            return_value=(True, WeatherPayload("Moscow\n⛈️", tz_offset_seconds=None), "provider_error")
        )
        with (
            patch("meshcore_bot.commands.weather_cmd._fetch_open_meteo", primary),
            patch("meshcore_bot.commands.weather_cmd._fetch_wttr_in", fallback),
        ):
            payload = await weather_cmd.fetch_weather_payload("Moscow", cfg, i18n, use_cache=False)

        self.assertEqual(payload.weather_body, "Moscow\n⛈️")
        primary.assert_awaited_once()
        fallback.assert_awaited_once()

    def test_meteostat_stations_db_is_valid_rejects_small_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stations.db"
            path.write_bytes(b"tiny")
            self.assertFalse(weather_cmd._meteostat_stations_db_is_valid(path))

    def test_meteostat_stations_db_is_valid_accepts_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stations.db"
            con = sqlite3.connect(path)
            try:
                con.execute("CREATE TABLE stations (id TEXT, latitude REAL, longitude REAL)")
                con.execute("INSERT INTO stations VALUES ('x', 55.0, 37.0)")
                con.commit()
            finally:
                con.close()
            with patch.object(weather_cmd, "METEOSTAT_STATIONS_DB_MIN_BYTES", 1):
                self.assertTrue(weather_cmd._meteostat_stations_db_is_valid(path))


class ChannelDeliveryRetryTests(unittest.IsolatedAsyncioTestCase):
    def _make_service(self) -> service.BotService:
        cfg = types.SimpleNamespace(
            dm_enabled=True,
            weather_default_city="Moscow",
            channels_enabled=[0],
            admin_public_keys=[],
            admin_channel_indices=[],
            reply_delay_sec=0,
            dm_delivery_wait_sec=0,
            dm_delivery_max_attempts=1,
            channel_delivery_wait_sec=0,
            channel_delivery_min_rx_repeats=1,
            channel_delivery_max_attempts=3,
            node_advert_store_path=Path("/tmp/test-node-adverts.json"),
            node_advert_retention_days=7,
            node_advert_max_stored=5000,
        )
        mesh = types.SimpleNamespace(
            get_contact_by_key_prefix=lambda _p: None,
            commands=types.SimpleNamespace(send_chan_msg=AsyncMock()),
        )
        blacklist = types.SimpleNamespace(is_blocked=lambda *_a, **_k: False)
        return service.BotService(
            cfg=cfg,
            mesh=mesh,
            i18n=_I18nStub(),
            blacklist=blacklist,
            shutdown=types.SimpleNamespace(set=lambda: None),
        )

    async def test_send_chan_succeeds_on_first_attempt_when_threshold_met(self) -> None:
        svc = self._make_service()
        svc._mesh.commands.send_chan_msg.return_value = types.SimpleNamespace(type="OK", payload={})
        svc._resolve_channel_name = AsyncMock(return_value="👽")  # type: ignore[method-assign]
        svc._count_heard_repeats_since = AsyncMock(return_value=1)  # type: ignore[method-assign]
        with patch.object(service, "EventType", types.SimpleNamespace(ERROR="ERROR")):
            ok = await svc._send_chan(0, "hello", kind="test")
        self.assertTrue(ok)
        self.assertEqual(svc._mesh.commands.send_chan_msg.await_count, 1)

    async def test_send_chan_retries_until_threshold_met(self) -> None:
        svc = self._make_service()
        svc._mesh.commands.send_chan_msg.return_value = types.SimpleNamespace(type="OK", payload={})
        svc._resolve_channel_name = AsyncMock(return_value="👽")  # type: ignore[method-assign]
        svc._count_heard_repeats_since = AsyncMock(side_effect=[0, 1])  # type: ignore[method-assign]
        with patch.object(service, "EventType", types.SimpleNamespace(ERROR="ERROR")):
            ok = await svc._send_chan(0, "hello", kind="test")
        self.assertTrue(ok)
        self.assertEqual(svc._mesh.commands.send_chan_msg.await_count, 2)

    async def test_send_chan_fails_when_threshold_never_met(self) -> None:
        svc = self._make_service()
        svc._mesh.commands.send_chan_msg.return_value = types.SimpleNamespace(type="OK", payload={})
        svc._resolve_channel_name = AsyncMock(return_value="👽")  # type: ignore[method-assign]
        svc._count_heard_repeats_since = AsyncMock(return_value=0)  # type: ignore[method-assign]
        with patch.object(service, "EventType", types.SimpleNamespace(ERROR="ERROR")):
            ok = await svc._send_chan(0, "hello", kind="test")
        self.assertFalse(ok)
        self.assertEqual(svc._mesh.commands.send_chan_msg.await_count, 3)

    async def test_send_chan_accepts_chan_hash_fallback_for_grp_txt(self) -> None:
        svc = self._make_service()
        svc._mesh.commands.send_chan_msg.return_value = types.SimpleNamespace(
            type="OK", payload={"chan_hash": "ea"}
        )
        svc._resolve_channel_name = AsyncMock(return_value="👽")  # type: ignore[method-assign]
        svc._count_heard_repeats_since = AsyncMock(return_value=1)  # type: ignore[method-assign]
        with patch.object(service, "EventType", types.SimpleNamespace(ERROR="ERROR")):
            ok = await svc._send_chan(0, "hello", kind="test")
        self.assertTrue(ok)
        self.assertEqual(svc._mesh.commands.send_chan_msg.await_count, 1)

    async def test_rx_log_uses_meshcore_decoder_for_grp_txt_payload(self) -> None:
        svc = self._make_service()
        svc._mesh.decode_rx_log_data = AsyncMock(return_value={"channel_idx": 0, "text": "hello"})  # type: ignore[attr-defined]
        event = types.SimpleNamespace(
            payload={
                "payload_typename": "GRP_TXT",
                "chan_hash": "1f",
                "pkt_payload": b"\x00\x01",
                "crypted": "abcd",
            },
            attributes={},
        )
        await svc._on_rx_log_data(event)
        count = await svc._count_heard_repeats_since(
            0,
            0,
            sent_chan_hash="1f",
            sent_chan_name="",
        )
        self.assertEqual(count, 1)

    async def test_chan_hash_count_is_deduplicated_by_pkt_hash(self) -> None:
        svc = self._make_service()
        await svc._register_rx_log_group_event(3, "ea", "👽", "12345")
        await svc._register_rx_log_group_event(3, "ea", "👽", "12345")
        await svc._register_rx_log_group_event(3, "ea", "👽", "99999")
        count = await svc._count_heard_repeats_since(
            3,
            0,
            sent_chan_hash="ea",
            sent_chan_name="👽",
        )
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
