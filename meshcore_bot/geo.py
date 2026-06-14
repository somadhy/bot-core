"""Geographic helpers for ping distance estimation."""

from __future__ import annotations

import math
from typing import Any, Mapping, Protocol, Sequence

_COORD_KEY_PAIRS = (
    ("adv_lat", "adv_lon"),
    ("gps_lat", "gps_lon"),
    ("latitude", "longitude"),
)


class _HasCoords(Protocol):
    adv_lat: float | None
    adv_lon: float | None
    node_type: str


def is_repeater_node_type(node_type: str) -> bool:
    t = (node_type or "").strip().lower()
    return (
        t in ("2", "repeater")
        or "repeater" in t
        or "router" in t
        or "relay" in t
    )


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_coord_pair(lat: float, lon: float) -> tuple[float, float] | None:
    if abs(lat) > 90.0 or abs(lon) > 180.0:
        if abs(lat) > 90.0 and abs(lat) <= 90_000_000.0:
            lat /= 1_000_000.0
        if abs(lon) > 180.0 and abs(lon) <= 180_000_000.0:
            lon /= 1_000_000.0
    if abs(lat) > 90.0 or abs(lon) > 180.0:
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    return lat, lon


def parse_coords_from_mapping(data: Mapping[str, Any] | None) -> tuple[float, float] | None:
    if not data:
        return None
    for lat_k, lon_k in _COORD_KEY_PAIRS:
        lat = _to_float(data.get(lat_k))
        lon = _to_float(data.get(lon_k))
        if lat is None or lon is None:
            continue
        normalized = _normalize_coord_pair(lat, lon)
        if normalized is not None:
            return normalized
    return None


def coords_from_record(rec: _HasCoords) -> tuple[float, float] | None:
    lat = rec.adv_lat
    lon = rec.adv_lon
    if lat is None or lon is None:
        return None
    return _normalize_coord_pair(lat, lon)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def format_distance_km(km: float, *, approx: bool, locale: str) -> str:
    if km < 10:
        text = f"{km:.1f}"
    elif km < 100:
        text = f"{km:.0f}"
    else:
        text = f"{km:.0f}"
    unit = "км" if locale == "ru" else "km"
    prefix = "📏~" if approx else "📏"
    return f"{prefix}{text} {unit}"


def group_path_hops(path_nodes: Sequence[str], hash_len: int | None) -> list[str]:
    group_bytes = max(1, min(3, int(hash_len if hash_len is not None else 1)))
    nodes = [str(x).strip().lower() for x in path_nodes if str(x).strip()]
    hops: list[str] = []
    for i in range(0, len(nodes), group_bytes):
        chunk = nodes[i : i + group_bytes]
        if len(chunk) < group_bytes:
            break
        hops.append("".join(chunk))
    return hops


def compute_ping_distance_suffix(
    *,
    bot_coords: tuple[float, float] | None,
    sender_coords: tuple[float, float] | None,
    path_len: int | None,
    path_nodes: Sequence[str] | None,
    hash_len: int | None,
    resolve_hop: Any,
    locale: str,
) -> str | None:
    """Return distance suffix for ping reply, or None if unavailable."""
    if bot_coords and sender_coords:
        km = haversine_km(bot_coords[0], bot_coords[1], sender_coords[0], sender_coords[1])
        return format_distance_km(km, approx=False, locale=locale)

    if not path_len or path_len <= 0 or not path_nodes:
        return None

    coord_repeaters: list[tuple[float, float]] = []
    for hop in group_path_hops(path_nodes, hash_len):
        resolved = resolve_hop(hop)
        if resolved is None:
            continue
        node_type = ""
        coords: tuple[float, float] | None = None
        if isinstance(resolved, Mapping):
            node_type = str(
                resolved.get("type")
                or resolved.get("node_type")
                or resolved.get("kind")
                or ""
            )
            coords = parse_coords_from_mapping(resolved)
        else:
            node_type = str(getattr(resolved, "node_type", "") or "")
            coords = coords_from_record(resolved)
        if not is_repeater_node_type(node_type) or coords is None:
            continue
        coord_repeaters.append(coords)

    if len(coord_repeaters) < 2:
        return None

    first = coord_repeaters[0]
    last = coord_repeaters[-1]
    km = haversine_km(first[0], first[1], last[0], last[1])
    return format_distance_km(km, approx=True, locale=locale)
