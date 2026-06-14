"""Tests for ping distance geo helpers."""

from __future__ import annotations

import unittest

from meshcore_bot.geo import (
    compute_ping_distance_suffix,
    format_distance_km,
    group_path_hops,
    haversine_km,
    is_repeater_node_type,
    parse_coords_from_mapping,
)
from meshcore_bot.node_advert_store import NodeAdvertRecord


class GeoHelpersTests(unittest.TestCase):
    def test_haversine_moscow_spb(self) -> None:
        km = haversine_km(55.7558, 37.6173, 59.9343, 30.3351)
        self.assertGreater(km, 620)
        self.assertLess(km, 660)

    def test_parse_coords_float(self) -> None:
        out = parse_coords_from_mapping({"adv_lat": 55.75, "adv_lon": 37.62})
        self.assertEqual(out, (55.75, 37.62))

    def test_parse_coords_microdegrees(self) -> None:
        out = parse_coords_from_mapping({"adv_lat": 55750000, "adv_lon": 37620000})
        self.assertEqual(out, (55.75, 37.62))

    def test_parse_coords_rejects_zero_zero(self) -> None:
        self.assertIsNone(parse_coords_from_mapping({"adv_lat": 0, "adv_lon": 0}))

    def test_group_path_hops_two_byte_mode(self) -> None:
        hops = group_path_hops(["aa", "aa", "bb", "bb", "cc", "cc"], hash_len=2)
        self.assertEqual(hops, ["aaaa", "bbbb", "cccc"])

    def test_format_distance_exact_ru(self) -> None:
        self.assertEqual(format_distance_km(12.4, approx=False, locale="ru"), "📏12 км")

    def test_format_distance_approx_en(self) -> None:
        self.assertEqual(format_distance_km(18.2, approx=True, locale="en"), "📏~18 km")

    def test_is_repeater_node_type(self) -> None:
        self.assertTrue(is_repeater_node_type("repeater"))
        self.assertTrue(is_repeater_node_type("2"))
        self.assertFalse(is_repeater_node_type("chat"))


class PingDistanceSuffixTests(unittest.TestCase):
    def test_endpoints_distance_when_both_have_coords(self) -> None:
        store: dict[str, NodeAdvertRecord] = {}

        def resolve(_hop: str) -> NodeAdvertRecord | None:
            return store.get(_hop)

        out = compute_ping_distance_suffix(
            bot_coords=(55.0, 37.0),
            sender_coords=(56.0, 38.0),
            path_len=3,
            path_nodes=["aa", "bb", "cc"],
            hash_len=1,
            resolve_hop=resolve,
            locale="ru",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("📏"))
        self.assertNotIn("~", out)

    def test_repeater_fallback_when_endpoints_missing(self) -> None:
        store = {
            "aa": NodeAdvertRecord(
                public_key="aa1122",
                node_name="R1",
                node_type="repeater",
                last_advert_ts=1.0,
                adv_lat=55.0,
                adv_lon=37.0,
            ),
            "cc": NodeAdvertRecord(
                public_key="cc3344",
                node_name="R2",
                node_type="repeater",
                last_advert_ts=2.0,
                adv_lat=56.0,
                adv_lon=38.0,
            ),
        }

        def resolve(hop: str) -> NodeAdvertRecord | None:
            return store.get(hop)

        out = compute_ping_distance_suffix(
            bot_coords=None,
            sender_coords=None,
            path_len=3,
            path_nodes=["aa", "bb", "cc"],
            hash_len=1,
            resolve_hop=resolve,
            locale="ru",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("📏~"))

    def test_direct_without_coords_returns_none(self) -> None:
        out = compute_ping_distance_suffix(
            bot_coords=None,
            sender_coords=None,
            path_len=0,
            path_nodes=[],
            hash_len=1,
            resolve_hop=lambda _h: None,
            locale="ru",
        )
        self.assertIsNone(out)

    def test_single_repeater_with_coords_returns_none(self) -> None:
        store = {
            "aa": NodeAdvertRecord(
                public_key="aa1122",
                node_name="R1",
                node_type="repeater",
                last_advert_ts=1.0,
                adv_lat=55.0,
                adv_lon=37.0,
            ),
        }

        out = compute_ping_distance_suffix(
            bot_coords=None,
            sender_coords=None,
            path_len=2,
            path_nodes=["aa", "bb"],
            hash_len=1,
            resolve_hop=lambda hop: store.get(hop),
            locale="ru",
        )
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
