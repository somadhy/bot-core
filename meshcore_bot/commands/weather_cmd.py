"""Weather: Open-Meteo (no key, RU-friendly) and OpenWeatherMap (API key)."""

from __future__ import annotations

import asyncio
import csv
import datetime as _dt
import gzip
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

import httpx

from meshcore_bot.textutil import clip_utf8_bytes

if TYPE_CHECKING:
    from meshcore_bot.config import BotConfig
    from meshcore_bot.i18n import I18n

logger = logging.getLogger(__name__)

_weather_lock = asyncio.Lock()
# (provider, locale, city_lower) -> (expires_monotonic, payload)
_weather_cache: dict[tuple[str, str, str], tuple[float, "WeatherPayload"]] = {}

_meteostat_lock = asyncio.Lock()
_meteostat_db_local_path: str | None = None

_ErrKind = Literal[
    "city_not_found",
    "not_configured",
    "unsupported_provider",
    "provider_error",
]

@dataclass(frozen=True)
class WeatherPayload:
    weather_body: str
    tz_offset_seconds: int | None = None


@dataclass(frozen=True)
class _GeocodeResult:
    lat: float
    lon: float
    name: str
    tz_offset_seconds: int | None = None


def _weather_cache_key(city: str, provider_norm: str, locale: str) -> tuple[str, str, str]:
    return (
        provider_norm,
        locale.strip().lower(),
        city.strip().lower(),
    )


def _weather_prune_expired(now: float) -> None:
    dead = [k for k, (exp, _) in _weather_cache.items() if exp <= now]
    for k in dead:
        del _weather_cache[k]

# Reply body (before @mention) should stay compact; full message clipped in service to 140 bytes UTF-8.
_CITY_MAX_BYTES = 28

OWM_GEOCODE_URL = "https://api.openweathermap.org/geo/1.0/direct"
OWM_WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"

OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "meshcore-bot/1.0 (weather geocoding fallback)"

WTTR_IN_BASE = "https://wttr.in"
WTTR_IN_ALT_BASE = "https://v2.wttr.in"
WTTR_IN_USER_AGENT = "curl/8.5.0"
WTTR_IN_ENDPOINTS = (WTTR_IN_ALT_BASE, WTTR_IN_BASE)

OPEN_METEO_HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
OPEN_METEO_GEOCODE_TIMEOUT = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)
WTTR_HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=20.0, write=8.0, pool=8.0)
WTTR_J1_MIN_PARTIAL_BYTES = 400

METEOSTAT_STATIONS_DB_URL = "https://data.meteostat.net/stations.db"
METEOSTAT_BULK_BASE = "https://data.meteostat.net"
METEOSTAT_RAPIDAPI_BASE = "https://meteostat.p.rapidapi.com"
METEOSTAT_RAPIDAPI_HOST = "meteostat.p.rapidapi.com"
METEOSTAT_STATIONS_DB_MIN_BYTES = 1024 * 1024
METEOSTAT_STATIONS_DB_REFRESH_SEC = 30.0 * 24.0 * 3600.0
METEOSTAT_STATIONS_DB_DEFAULT_PATH = Path("data/meteostat_stations.db")
METEOSTAT_STATIONS_DB_BUNDLED_PATH = Path("/app/var/meteostat_stations.db")
METEOSTAT_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)
METEOSTAT_DOWNLOAD_RETRIES = 3

# WMO code → one condition emoji (locale-independent)
_WMO_EMOJI: dict[int, str] = {
    0: "☀️",
    1: "🌤️",
    2: "⛅",
    3: "☁️",
    45: "🌫️",
    48: "🌫️",
    51: "🌦️",
    53: "🌦️",
    55: "🌦️",
    56: "🌨️",
    57: "🌨️",
    61: "🌧️",
    63: "🌧️",
    65: "🌧️",
    66: "🌧️",
    67: "🌧️",
    71: "❄️",
    73: "❄️",
    75: "❄️",
    77: "❄️",
    80: "🌧️",
    81: "🌧️",
    82: "🌧️",
    85: "🌨️",
    86: "🌨️",
    95: "⛈️",
    96: "⛈️",
    99: "⛈️",
}


def _wmo_emoji(code: int) -> str:
    return _WMO_EMOJI.get(int(code), "🌡️")


def _fmt_num(v: float, decimals: int = 1) -> str:
    if decimals == 0:
        return str(int(round(v)))
    s = f"{v:.{decimals}f}"
    s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_pressure_hpa(hpa: float) -> str:
    """Sea-level pressure in hPa (integer, typical for forecasts)."""
    return str(int(round(float(hpa))))


def _request_error_detail(exc: Exception, *, locale: str = "en") -> str:
    """Human-readable short error for mesh replies; httpx timeouts often have empty str()."""
    ru = locale == "ru"
    if isinstance(exc, httpx.TimeoutException):
        return "таймаут" if ru else "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return str(exc.response.status_code)
    if isinstance(exc, httpx.RequestError):
        msg = str(exc).strip()
        if msg:
            return msg[:80]
        name = type(exc).__name__
        if "Timeout" in name:
            return "таймаут" if ru else "timeout"
        if "Connect" in name:
            return "сеть" if ru else "network"
        return name[:80]
    msg = str(exc).strip()
    if msg:
        return msg[:80]
    return type(exc).__name__[:80]


def _format_weather_line(
    city: str,
    temp_str: str,
    wmo_code: int,
    humidity: int | float,
    wind_ms: float,
    precip_mm: float,
    pressure_hpa: float | None,
    locale: str,
) -> str:
    lang = "ru" if locale == "ru" else "en"
    city_show = clip_utf8_bytes(city.strip(), _CITY_MAX_BYTES)
    sky = _wmo_emoji(int(wmo_code))
    hum = int(round(float(humidity)))
    ws = _fmt_num(float(wind_ms), 1)
    pr = _fmt_num(float(precip_mm), 1) if precip_mm >= 0.05 else "0"
    ph = _fmt_pressure_hpa(pressure_hpa) if pressure_hpa is not None else None

    # Столбик: город, небо (эмодзи), 🌡️, метрики (полный ответ — до 140 байт UTF-8 с @mention в service).
    if lang == "ru":
        lines = [
            city_show,
            sky,
            f"🌡️{temp_str}°",
            f"💧{hum}%",
            f"💨{ws}м/с",
            f"🌧️{pr}мм",
        ]
        if ph is not None:
            lines.append(f"📊{ph}гПа")
        line = "\n".join(lines)
    else:
        lines = [
            city_show,
            sky,
            f"🌡️{temp_str}°",
            f"💧{hum}%",
            f"💨{ws}m/s",
            f"🌧️{pr}mm",
        ]
        if ph is not None:
            lines.append(f"📊{ph}hPa")
        line = "\n".join(lines)

    return line


async def fetch_weather_line(city: str, cfg: BotConfig, i18n: I18n) -> str:
    payload = await fetch_weather_payload(city, cfg, i18n)
    return payload.weather_body


async def fetch_weather_payload(
    city: str,
    cfg: BotConfig,
    i18n: I18n,
    *,
    use_cache: bool = True,
) -> WeatherPayload:
    def _norm(p: str) -> str:
        p = (p or "").strip().lower()
        if p in ("openmeteo", "open-meteo"):
            return "openmeteo"
        if p in ("openweathermap", "owm"):
            return "openweathermap"
        if p in ("meteostat", "meteostat.net"):
            return "meteostat"
        if p in ("meteostat_rapidapi", "meteostat-rapidapi", "meteostat_rapid", "meteostat-rapid"):
            return "meteostat_rapidapi"
        if p in ("wttr.in", "wttrin", "wttr_in", "wttr"):
            return "wttr"
        return p

    primary_raw = cfg.weather_provider or "openmeteo"
    fallback_raw = getattr(cfg, "weather_provider_fallback", "") or ""

    primary = _norm(primary_raw)
    fallback = _norm(fallback_raw)

    providers: list[str] = []
    if primary:
        providers.append(primary)
    if fallback and fallback != primary:
        providers.append(fallback)

    ttl_min = float(cfg.weather_cache_ttl_minutes or 0)

    last_payload: WeatherPayload | None = None
    city_not_found_failures = 0
    for idx, provider_norm in enumerate(providers):
        if use_cache and ttl_min > 0:
            key = _weather_cache_key(city, provider_norm, str(cfg.locale))
            async with _weather_lock:
                now = time.monotonic()
                _weather_prune_expired(now)
                hit = _weather_cache.get(key)
                if hit is not None:
                    exp, payload = hit
                    if now < exp:
                        logger.debug(
                            "weather cache hit city=%r provider=%s", city.strip(), provider_norm
                        )
                        return payload

        if provider_norm == "openmeteo":
            ok, payload, err = await _fetch_open_meteo(city, cfg, i18n)
        elif provider_norm == "openweathermap":
            ok, payload, err = await _fetch_open_weather_map(city, cfg, i18n)
        elif provider_norm == "meteostat":
            ok, payload, err = await _fetch_meteostat(city, cfg, i18n)
        elif provider_norm == "meteostat_rapidapi":
            ok, payload, err = await _fetch_meteostat_rapidapi(city, cfg, i18n)
        elif provider_norm == "wttr":
            ok, payload, err = await _fetch_wttr_in(city, cfg, i18n)
        else:
            ok, payload, err = (
                False,
                WeatherPayload(i18n.t("errors.weather_failed", detail="unsupported provider")),
                "unsupported_provider",
            )

        if ok:
            if use_cache and ttl_min > 0:
                key = _weather_cache_key(city, provider_norm, str(cfg.locale))
                async with _weather_lock:
                    now = time.monotonic()
                    _weather_prune_expired(now)
                    _weather_cache[key] = (now + ttl_min * 60.0, payload)
                    logger.debug("weather cache store key=%s ttl_min=%s", key, ttl_min)
            if idx > 0:
                logger.info(
                    "weather provider fallback used primary=%s fallback=%s city=%r",
                    primary,
                    provider_norm,
                    city.strip(),
                )
            return payload

        if err != "not_configured":
            last_payload = payload
        else:
            logger.warning(
                "weather provider not configured, skipping: provider=%s",
                provider_norm,
            )
        if err == "city_not_found":
            city_not_found_failures += 1
        if idx + 1 < len(providers):
            logger.warning(
                "weather provider failed, trying fallback: provider=%s city=%r",
                provider_norm,
                city.strip(),
            )

    if providers and city_not_found_failures == len(providers):
        return WeatherPayload(i18n.t("errors.city_not_found_all"))
    return last_payload or WeatherPayload(i18n.t("errors.weather_failed", detail="no providers configured"))


def _meteostat_coco_to_wmo_bucket(coco: int | None) -> int:
    # Meteostat weather condition codes: https://dev.meteostat.net/formats.html
    if coco is None:
        return 3
    c = int(coco)
    if c == 1:
        return 0
    if c == 2:
        return 1
    if c == 3:
        return 2
    if c == 4:
        return 3
    if c in (5, 6):
        return 45
    if 7 <= c <= 11:
        return 63
    if c in (12, 13, 19, 20):
        return 80
    if c in (14, 15, 16, 21, 22):
        return 71
    if c in (23, 24, 25, 26, 27):
        return 95
    return 3


def _meteostat_kmh_to_ms(kmh: float) -> float:
    return float(kmh) / 3.6


async def _geocode_open_meteo(
    city: str,
    cfg: BotConfig,
    i18n: I18n,
    client: httpx.AsyncClient,
) -> tuple[bool, _GeocodeResult | None, _ErrKind]:
    lang_geo = "ru" if cfg.locale == "ru" else "en"
    try:
        gr = await client.get(
            OPEN_METEO_GEOCODE,
            params={"name": city, "count": 1, "language": lang_geo, "format": "json"},
        )
        gr.raise_for_status()
        data = gr.json()
        results = data.get("results") or []
        if not results:
            return False, None, "city_not_found"
        g0 = results[0]
        tz_offset = g0.get("utc_offset_seconds")
        return (
            True,
            _GeocodeResult(
                lat=float(g0["latitude"]),
                lon=float(g0["longitude"]),
                name=str(g0.get("name") or city),
                tz_offset_seconds=int(tz_offset) if tz_offset is not None else None,
            ),
            "provider_error",
        )
    except httpx.HTTPStatusError as e:
        logger.warning("Open-Meteo geocode HTTP error: %r", e)
        return (
            False,
            None,
            "city_not_found" if e.response.status_code == 404 else "provider_error",
        )
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Open-Meteo geocode failed: %r", e)
        return False, None, "provider_error"


async def _geocode_nominatim(
    city: str,
    cfg: BotConfig,
    client: httpx.AsyncClient,
) -> tuple[bool, _GeocodeResult | None, _ErrKind]:
    lang_geo = "ru" if cfg.locale == "ru" else "en"
    try:
        gr = await client.get(
            NOMINATIM_SEARCH,
            params={
                "q": city,
                "format": "json",
                "limit": 1,
                "accept-language": lang_geo,
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT},
        )
        gr.raise_for_status()
        results = gr.json()
        if not results:
            return False, None, "city_not_found"
        g0 = results[0]
        name = str(g0.get("name") or city).strip()
        if not name:
            display = str(g0.get("display_name") or city)
            name = display.split(",")[0].strip() or city
        return (
            True,
            _GeocodeResult(
                lat=float(g0["lat"]),
                lon=float(g0["lon"]),
                name=name,
                tz_offset_seconds=None,
            ),
            "provider_error",
        )
    except httpx.HTTPStatusError as e:
        logger.warning("Nominatim geocode HTTP error: %r", e)
        return (
            False,
            None,
            "city_not_found" if e.response.status_code == 404 else "provider_error",
        )
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Nominatim geocode failed: %r", e)
        return False, None, "provider_error"


async def _geocode_city(
    city: str,
    cfg: BotConfig,
    i18n: I18n,
    *,
    client: httpx.AsyncClient,
) -> tuple[bool, _GeocodeResult | None, _ErrKind]:
    ok, geo, err = await _geocode_open_meteo(city, cfg, i18n, client)
    if ok and geo is not None:
        return ok, geo, err
    if err == "city_not_found":
        return False, None, "city_not_found"

    logger.warning("Open-Meteo geocode unavailable for %r, trying Nominatim", city.strip())
    ok2, geo2, err2 = await _geocode_nominatim(city, cfg, client)
    if ok2 and geo2 is not None:
        return ok2, geo2, err2
    if err2 == "city_not_found":
        return False, None, "city_not_found"
    return False, None, "provider_error"


def _rapidapi_headers(host: str) -> dict[str, str] | None:
    """
    Common RapidAPI auth headers.

    We use a shared RAPIDAPI_KEY to allow adding more RapidAPI providers later.
    """
    key = (os.environ.get("RAPIDAPI_KEY") or "").strip()
    if not key:
        return None
    return {"x-rapidapi-key": key, "x-rapidapi-host": host}


async def _meteostat_ensure_stations_db() -> str:
    """Return path to Meteostat stations.db, downloading or copying it once if needed."""
    global _meteostat_db_local_path
    if _meteostat_db_local_path is not None:
        return _meteostat_db_local_path

    async with _meteostat_lock:
        if _meteostat_db_local_path is not None:
            return _meteostat_db_local_path

        path = _meteostat_stations_db_path()
        ready = _meteostat_stations_db_is_valid(path)
        if ready:
            _meteostat_db_local_path = str(path)
            return str(path)

        bundled = METEOSTAT_STATIONS_DB_BUNDLED_PATH
        if bundled != path and _meteostat_stations_db_is_valid(bundled):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundled, path)
                logger.info("Meteostat stations.db copied from bundled image to %s", path)
                _meteostat_db_local_path = str(path)
                return str(path)
            except OSError as e:
                logger.warning("Meteostat bundled stations.db copy failed: %r", e)
                _meteostat_db_local_path = str(bundled)
                return str(bundled)

        last_err: Exception | None = None
        for attempt in range(1, METEOSTAT_DOWNLOAD_RETRIES + 1):
            try:
                logger.info(
                    "Meteostat stations.db download attempt %s/%s -> %s",
                    attempt,
                    METEOSTAT_DOWNLOAD_RETRIES,
                    path,
                )
                await _meteostat_download_stations_db(path)
                _meteostat_db_local_path = str(path)
                logger.info(
                    "Meteostat stations.db ready (%s bytes) at %s",
                    path.stat().st_size,
                    path,
                )
                return str(path)
            except (httpx.HTTPError, OSError, sqlite3.Error) as e:
                last_err = e
                logger.warning(
                    "Meteostat stations.db download attempt %s failed: %r",
                    attempt,
                    e,
                )

        if _meteostat_stations_db_is_valid(bundled):
            logger.warning("Meteostat stations.db download failed; using bundled copy")
            _meteostat_db_local_path = str(bundled)
            return str(bundled)

        raise last_err or OSError("stations.db download failed")


def _meteostat_stations_db_path() -> Path:
    raw = (os.environ.get("MESHCORE_BOT_METEOSTAT_DB") or "").strip()
    return Path(raw) if raw else METEOSTAT_STATIONS_DB_DEFAULT_PATH


def _meteostat_stations_db_is_valid(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size < METEOSTAT_STATIONS_DB_MIN_BYTES:
        return False
    if (time.time() - float(st.st_mtime)) >= METEOSTAT_STATIONS_DB_REFRESH_SEC:
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT COUNT(*) FROM stations").fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return bool(row and int(row[0]) > 0)


async def _meteostat_download_stations_db(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = str(dest.parent)
    fd, tmp_name = tempfile.mkstemp(prefix="meshcore_bot_meteostat_", suffix=".db", dir=tmp_dir)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        async with httpx.AsyncClient(timeout=METEOSTAT_DOWNLOAD_TIMEOUT) as client:
            async with client.stream("GET", METEOSTAT_STATIONS_DB_URL) as response:
                response.raise_for_status()
                with tmp_path.open("wb") as handle:
                    async for chunk in response.aiter_bytes(65536):
                        handle.write(chunk)
        if tmp_path.stat().st_size < METEOSTAT_STATIONS_DB_MIN_BYTES:
            raise OSError(f"stations.db too small ({tmp_path.stat().st_size} bytes)")
        con = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            con.execute("SELECT COUNT(*) FROM stations").fetchone()
        finally:
            con.close()
        tmp_path.replace(dest)
    finally:
        tmp_path.unlink(missing_ok=True)


def _meteostat_station_candidates(db_path: str, lat: float, lon: float, limit: int = 12) -> list[str]:
    dlat = 2.0
    dlon = 3.0
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(
            """
            SELECT id, latitude, longitude
            FROM stations
            WHERE latitude BETWEEN ? AND ?
              AND longitude BETWEEN ? AND ?
            LIMIT 500
            """,
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    def _dist2(r: sqlite3.Row) -> float:
        la = float(r["latitude"])
        lo = float(r["longitude"])
        x = (lo - lon) * math.cos(math.radians(lat))
        y = (la - lat)
        return x * x + y * y

    rows.sort(key=_dist2)
    out: list[str] = []
    for r in rows:
        sid = str(r["id"]).strip()
        if sid:
            out.append(sid)
        if len(out) >= limit:
            break
    return out


async def _meteostat_fetch_latest_hour(station_id: str) -> dict[str, float | int | None] | None:
    years = [int(_dt.date.today().year), int(_dt.date.today().year) - 1]
    async with httpx.AsyncClient(timeout=25.0) as client:
        for year in years:
            url = f"{METEOSTAT_BULK_BASE}/hourly/{year}/{station_id}.csv.gz"
            try:
                r = await client.get(url)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
            except httpx.HTTPError:
                continue

            try:
                gz = gzip.GzipFile(fileobj=io.BytesIO(r.content))
                text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
                reader = csv.DictReader(text)
                last: dict[str, str] | None = None
                for row in reader:
                    if row:
                        last = row
                if not last:
                    continue
            except (OSError, UnicodeError, csv.Error):
                continue

            def _f(key: str) -> float | None:
                v = last.get(key)
                if v is None:
                    return None
                s = str(v).strip()
                if not s:
                    return None
                try:
                    return float(s)
                except ValueError:
                    return None

            def _i(key: str) -> int | None:
                v = last.get(key)
                if v is None:
                    return None
                s = str(v).strip()
                if not s:
                    return None
                try:
                    return int(float(s))
                except ValueError:
                    return None

            return {
                "temp": _f("temp"),
                "rhum": _f("rhum"),
                "prcp": _f("prcp"),
                "wspd": _f("wspd"),
                "pres": _f("pres"),
                "coco": _i("coco"),
            }
    return None


async def _fetch_meteostat(
    city: str, cfg: BotConfig, i18n: I18n
) -> tuple[bool, WeatherPayload, _ErrKind]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            ok, geo, err = await _geocode_city(city, cfg, i18n, client=client)
            if not ok or geo is None:
                if err == "city_not_found":
                    return False, WeatherPayload(i18n.t("errors.city_not_found")), err
                return (
                    False,
                    WeatherPayload(i18n.t("errors.weather_failed", detail="geocode failed")),
                    "provider_error",
                )
            lat, lon, name, tz_offset = geo.lat, geo.lon, geo.name, geo.tz_offset_seconds
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Meteostat geocode failed: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=_request_error_detail(e, locale=cfg.locale))),
            "provider_error",
        )

    try:
        db_path = await _meteostat_ensure_stations_db()
    except (httpx.HTTPError, OSError, sqlite3.Error) as e:
        logger.warning("Meteostat stations.db download failed: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail="stations db download failed")),
            "provider_error",
        )

    try:
        station_ids = _meteostat_station_candidates(db_path, lat, lon, limit=12)
    except Exception as e:  # noqa: BLE001
        logger.warning("Meteostat stations.db query failed: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail="stations db query failed")),
            "provider_error",
        )

    if not station_ids:
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail="no stations nearby")),
            "provider_error",
        )

    for sid in station_ids:
        rec = await _meteostat_fetch_latest_hour(sid)
        if not rec:
            continue
        temp = rec.get("temp")
        if temp is None:
            continue
        hum = rec.get("rhum") or 0.0
        prcp = rec.get("prcp") or 0.0
        wspd = rec.get("wspd") or 0.0
        pres = rec.get("pres")
        coco = rec.get("coco")

        wind_ms = _meteostat_kmh_to_ms(float(wspd))
        pressure_hpa = float(pres) if pres is not None else None
        wmo_like = _meteostat_coco_to_wmo_bucket(int(coco) if coco is not None else None)

        tf = float(temp)
        t_str = f"{tf:.0f}" if abs(tf - round(tf)) < 0.05 else f"{tf:.1f}"
        return (
            True,
            WeatherPayload(
                _format_weather_line(
                    name,
                    t_str,
                    wmo_like,
                    float(hum),
                    float(wind_ms),
                    float(prcp),
                    pressure_hpa,
                    cfg.locale,
                ),
                int(tz_offset) if tz_offset is not None else None,
            ),
            "provider_error",
        )

    return (
        False,
        WeatherPayload(i18n.t("errors.weather_failed", detail="no recent station data")),
        "provider_error",
    )


async def _fetch_meteostat_rapidapi(
    city: str, cfg: BotConfig, i18n: I18n
) -> tuple[bool, WeatherPayload, _ErrKind]:
    """
    Meteostat JSON API via RapidAPI (requires RAPIDAPI_KEY).
    Docs: https://dev.meteostat.net/api
    """
    headers = _rapidapi_headers(METEOSTAT_RAPIDAPI_HOST)
    if headers is None:
        return False, WeatherPayload(i18n.t("errors.rapidapi_not_configured")), "not_configured"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            ok, geo, err = await _geocode_city(city, cfg, i18n, client=client)
            if not ok or geo is None:
                if err == "city_not_found":
                    return False, WeatherPayload(i18n.t("errors.city_not_found")), err
                return (
                    False,
                    WeatherPayload(i18n.t("errors.weather_failed", detail="geocode failed")),
                    "provider_error",
                )
            lat, lon, name, tz_offset = geo.lat, geo.lon, geo.name, geo.tz_offset_seconds
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Meteostat(RapidAPI) geocode failed: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=_request_error_detail(e, locale=cfg.locale))),
            "provider_error",
        )

    try:
        async with httpx.AsyncClient(timeout=25.0, headers=headers) as client:
            nr = await client.get(
                f"{METEOSTAT_RAPIDAPI_BASE}/stations/nearby",
                params={"lat": lat, "lon": lon, "limit": 1},
            )
            nr.raise_for_status()
            nj = nr.json()
            stations = nj.get("data") or []
            if not stations:
                return (
                    False,
                    WeatherPayload(i18n.t("errors.weather_failed", detail="no stations nearby")),
                    "provider_error",
                )
            station_id = str((stations[0] or {}).get("id") or "").strip()
            if not station_id:
                return (
                    False,
                    WeatherPayload(i18n.t("errors.weather_failed", detail="bad station id")),
                    "provider_error",
                )

            end = _dt.date.today()
            start = end - _dt.timedelta(days=2)
            hr = await client.get(
                f"{METEOSTAT_RAPIDAPI_BASE}/stations/hourly",
                params={
                    "station": station_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "units": "metric",
                    "model": "true",
                },
            )
            hr.raise_for_status()
            hj = hr.json()
            rows = hj.get("data") or []
            if not rows:
                return (
                    False,
                    WeatherPayload(i18n.t("errors.weather_failed", detail="no hourly data")),
                    "provider_error",
                )

            last = rows[-1] or {}
            temp = last.get("temp")
            if temp is None:
                return (
                    False,
                    WeatherPayload(i18n.t("errors.weather_failed", detail="no temp")),
                    "provider_error",
                )

            hum = last.get("rhum") or 0.0
            prcp = last.get("prcp") or 0.0
            wspd = last.get("wspd") or 0.0  # km/h in metric
            pres = last.get("pres")
            coco = last.get("coco")

    except httpx.HTTPStatusError as e:
        logger.warning("Meteostat(RapidAPI) HTTP error: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=e.response.status_code)),
            "provider_error",
        )
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Meteostat(RapidAPI) request failed: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=_request_error_detail(e, locale=cfg.locale))),
            "provider_error",
        )

    wind_ms = _meteostat_kmh_to_ms(float(wspd))
    pressure_hpa = float(pres) if pres is not None else None
    wmo_like = _meteostat_coco_to_wmo_bucket(int(coco) if coco is not None else None)

    tf = float(temp)
    t_str = f"{tf:.0f}" if abs(tf - round(tf)) < 0.05 else f"{tf:.1f}"
    return (
        True,
        WeatherPayload(
            _format_weather_line(
                name,
                t_str,
                wmo_like,
                float(hum),
                float(wind_ms),
                float(prcp),
                pressure_hpa,
                cfg.locale,
            ),
            int(tz_offset) if tz_offset is not None else None,
        ),
        "provider_error",
    )


async def _fetch_wttr_in(
    city: str, cfg: BotConfig, i18n: I18n
) -> tuple[bool, WeatherPayload, _ErrKind]:
    """wttr.in JSON API (no key, one lightweight HTTP request)."""
    city_q = quote(city.strip())
    if not city_q:
        return False, WeatherPayload(i18n.t("errors.city_not_found")), "city_not_found"

    last_payload = WeatherPayload(
        i18n.t("errors.weather_failed", detail="таймаут" if cfg.locale == "ru" else "timeout")
    )
    for base in WTTR_IN_ENDPOINTS:
        ok, payload, err = await _fetch_wttr_in_once(city, city_q, cfg, i18n, base)
        if ok:
            return ok, payload, err
        last_payload = payload
        if err == "city_not_found":
            return False, payload, err
        logger.warning("wttr.in request failed via %s for %r", base, city.strip())

    return False, last_payload, "provider_error"


def _wttr_in_request_url(base_url: str, city_q: str, lang: str) -> str:
    return f"{base_url}/{city_q}?format=j1&lang={lang}"


def _wttr_j1_partial_ready(text: str) -> bool:
    return (
        '"temp_C"' in text
        and '"humidity"' in text
        and '"windspeedKmph"' in text
        and '"weatherCode"' in text
    )


def _wttr_j1_field(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _wttr_j1_area_name(text: str) -> str | None:
    match = re.search(
        r'"areaName"\s*:\s*\[\s*\{\s*"value"\s*:\s*"([^"]+)"',
        text,
    )
    return match.group(1) if match else None


def _parse_wttr_j1_partial(text: str, *, fallback_city: str) -> tuple[str, dict[str, str]]:
    if not _wttr_j1_partial_ready(text):
        raise ValueError("wttr j1 partial body incomplete")

    cur = {
        "temp_C": _wttr_j1_field(text, "temp_C"),
        "humidity": _wttr_j1_field(text, "humidity"),
        "windspeedKmph": _wttr_j1_field(text, "windspeedKmph"),
        "precipMM": _wttr_j1_field(text, "precipMM"),
        "pressure": _wttr_j1_field(text, "pressure"),
        "weatherCode": _wttr_j1_field(text, "weatherCode"),
    }
    if cur["temp_C"] is None:
        raise ValueError("wttr j1 partial body missing temp_C")

    name = _wttr_j1_area_name(text) or fallback_city
    return name, cur


async def _wttr_fetch_j1_text(url: str) -> str:
    try:
        return await _wttr_fetch_j1_text_httpx(url)
    except httpx.RequestError as exc:
        logger.warning("wttr.in httpx failed for %s: %r; trying curl", url, exc)
        return await _wttr_fetch_j1_text_curl(url)


async def _wttr_fetch_j1_text_httpx(url: str) -> str:
    headers = {
        "User-Agent": WTTR_IN_USER_AGENT,
        "Accept": "application/json",
    }
    buf = bytearray()
    try:
        async with httpx.AsyncClient(timeout=WTTR_HTTP_TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    raise httpx.HTTPStatusError(
                        "not found",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                async for chunk in response.aiter_bytes(4096):
                    buf.extend(chunk)
                    if _wttr_j1_partial_ready(buf.decode("utf-8", errors="replace")):
                        logger.debug("wttr.in partial j1 ready after %s bytes (httpx)", len(buf))
                        break
    except httpx.HTTPStatusError:
        raise
    except httpx.ReadTimeout:
        logger.warning("wttr.in httpx read timeout after %s bytes", len(buf))

    text = bytes(buf).decode("utf-8", errors="replace")
    if len(text) < WTTR_J1_MIN_PARTIAL_BYTES or not _wttr_j1_partial_ready(text):
        raise httpx.ReadTimeout("wttr partial body too short")
    return text


async def _wttr_fetch_j1_text_curl(url: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "curl",
        "-4",
        "-fsS",
        "--max-time",
        "20",
        "-A",
        WTTR_IN_USER_AGENT,
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    text = (stdout or b"").decode("utf-8", errors="replace")
    if _wttr_j1_partial_ready(text):
        if proc.returncode not in (0, 28):
            logger.warning("wttr.in curl exit %s but partial j1 is usable", proc.returncode)
        return text

    err = (stderr or b"").decode("utf-8", errors="replace").strip()[:120]
    raise OSError(err or f"curl exit {proc.returncode}")


async def _fetch_wttr_in_once(
    city: str,
    city_q: str,
    cfg: BotConfig,
    i18n: I18n,
    base_url: str,
) -> tuple[bool, WeatherPayload, _ErrKind]:
    lang = "ru" if cfg.locale == "ru" else "en"
    url = _wttr_in_request_url(base_url, city_q, lang)
    try:
        body = await _wttr_fetch_j1_text(url)
        name, cur = _parse_wttr_j1_partial(body, fallback_city=city)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return False, WeatherPayload(i18n.t("errors.city_not_found")), "city_not_found"
        logger.warning("wttr.in HTTP error via %s: %r", base_url, e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=e.response.status_code)),
            "provider_error",
        )
    except (httpx.RequestError, OSError, ValueError, TypeError) as e:
        logger.warning("wttr.in request failed via %s: %r", base_url, e)
        return (
            False,
            WeatherPayload(
                i18n.t("errors.weather_failed", detail=_request_error_detail(e, locale=cfg.locale))
            ),
            "provider_error",
        )

    try:
        temp = cur.get("temp_C")
        if temp is None or str(temp).strip() == "":
            return (
                False,
                WeatherPayload(i18n.t("errors.weather_failed", detail="no temp")),
                "provider_error",
            )

        hum = float(cur.get("humidity") or 0)
        wind_kmh = float(cur.get("windspeedKmph") or 0)
        wind_ms = wind_kmh / 3.6
        prec = float(cur.get("precipMM") or 0)
        raw_p = cur.get("pressure")
        pressure_hpa = float(raw_p) if raw_p not in (None, "") else None
        wcode = _wttr_code_to_wmo_bucket(int(cur.get("weatherCode") or 0))
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("wttr.in parse failed: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail="bad wttr response")),
            "provider_error",
        )

    tf = float(temp)
    t_str = f"{tf:.0f}" if abs(tf - round(tf)) < 0.05 else f"{tf:.1f}"
    return (
        True,
        WeatherPayload(
            _format_weather_line(
                name,
                t_str,
                wcode,
                hum,
                wind_ms,
                prec,
                pressure_hpa,
                cfg.locale,
            ),
            None,
        ),
        "provider_error",
    )


def _wttr_code_to_wmo_bucket(code: int) -> int:
    """Map wttr.in / WorldWeatherOnline codes to WMO-like buckets for emoji labels."""
    c = int(code)
    if c == 113:
        return 0
    if c == 116:
        return 1
    if c in (119, 122):
        return 3
    if c in (143, 248, 260):
        return 45
    if c in (176, 263, 266, 293, 296, 299, 353):
        return 51
    if c in (302, 305, 308, 356, 359):
        return 63
    if c in (386, 389, 200):
        return 95
    if c in (179, 227, 230, 323, 326, 329, 332, 335, 338, 368, 371, 392, 395):
        return 71
    if c in (182, 185, 281, 284, 311, 314, 317, 320, 362, 365):
        return 80
    return 3


async def _fetch_open_meteo(
    city: str, cfg: BotConfig, i18n: I18n
) -> tuple[bool, WeatherPayload, _ErrKind]:
    try:
        async with httpx.AsyncClient(timeout=OPEN_METEO_GEOCODE_TIMEOUT) as client:
            ok, geo, err = await _geocode_city(city, cfg, i18n, client=client)
            if not ok or geo is None:
                if err == "city_not_found":
                    return False, WeatherPayload(i18n.t("errors.city_not_found")), err
                return (
                    False,
                    WeatherPayload(i18n.t("errors.weather_failed", detail="geocode failed")),
                    "provider_error",
                )
            lat, lon, name = geo.lat, geo.lon, geo.name

        async with httpx.AsyncClient(timeout=OPEN_METEO_HTTP_TIMEOUT) as client:
            wr = await client.get(
                OPEN_METEO_FORECAST,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": (
                        "temperature_2m,relative_humidity_2m,precipitation,"
                        "weather_code,wind_speed_10m,pressure_msl"
                    ),
                    "timezone": "auto",
                    "windspeed_unit": "ms",
                },
            )
            wr.raise_for_status()
            wj = wr.json()
            cur = wj.get("current") or {}
            temp = cur.get("temperature_2m")
            if temp is None:
                return (
                    False,
                    WeatherPayload(i18n.t("errors.weather_failed", detail="no current weather")),
                    "provider_error",
                )
            wcode = int(cur.get("weather_code") or 0)
            hum = cur.get("relative_humidity_2m")
            wind_ms = cur.get("wind_speed_10m")
            prec = cur.get("precipitation")
            if hum is None:
                hum = 0.0
            if wind_ms is None:
                wind_ms = 0.0
            if prec is None:
                prec = 0.0
            p_msl = cur.get("pressure_msl")
            pressure_hpa = float(p_msl) if p_msl is not None else None
            tz_offset = wj.get("utc_offset_seconds")
            if tz_offset is None:
                tz_offset = geo.tz_offset_seconds
    except httpx.HTTPStatusError as e:
        logger.warning("Open-Meteo HTTP error: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=e.response.status_code)),
            "provider_error",
        )
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Open-Meteo request failed: %r", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=_request_error_detail(e, locale=cfg.locale))),
            "provider_error",
        )

    tf = float(temp)
    t_str = f"{tf:.0f}" if abs(tf - round(tf)) < 0.05 else f"{tf:.1f}"
    return (
        True,
        WeatherPayload(
            _format_weather_line(
                name,
                t_str,
                wcode,
                float(hum),
                float(wind_ms),
                float(prec),
                pressure_hpa,
                cfg.locale,
            ),
            int(tz_offset) if tz_offset is not None else None,
        ),
        "provider_error",
    )


async def _fetch_open_weather_map(
    city: str, cfg: BotConfig, i18n: I18n
) -> tuple[bool, WeatherPayload, _ErrKind]:
    key = (os.environ.get("WEATHER_API_KEY") or "").strip()
    if not key:
        return False, WeatherPayload(i18n.t("errors.weather_not_configured")), "not_configured"

    lang = "ru" if cfg.locale == "ru" else "en"
    params_geo = {"q": city, "limit": 1, "appid": key}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            gr = await client.get(OWM_GEOCODE_URL, params=params_geo)
            gr.raise_for_status()
            geo = gr.json()
            if not geo:
                return False, WeatherPayload(i18n.t("errors.city_not_found")), "city_not_found"
            lat, lon = geo[0]["lat"], geo[0]["lon"]
            name = geo[0].get("local_names", {}).get(lang) or geo[0].get("name", city)

            params_w = {
                "lat": lat,
                "lon": lon,
                "appid": key,
                "units": "metric",
                "lang": lang,
            }
            wr = await client.get(OWM_WEATHER_URL, params=params_w)
            wr.raise_for_status()
            data = wr.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Weather HTTP error: %s", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=e.response.status_code)),
            "provider_error",
        )
    except (httpx.RequestError, KeyError, IndexError, ValueError) as e:
        logger.warning("Weather request failed: %s", e)
        return (
            False,
            WeatherPayload(i18n.t("errors.weather_failed", detail=_request_error_detail(e, locale=cfg.locale))),
            "provider_error",
        )

    temp = data["main"]["temp"]
    hum = float(data["main"].get("humidity", 0))
    wind_ms = float(data.get("wind", {}).get("speed", 0))
    rain = data.get("rain") or {}
    snow = data.get("snow") or {}
    prec = float(rain.get("1h", 0) or 0) + float(snow.get("1h", 0) or 0)

    wids = [w.get("id", 0) for w in data.get("weather", [])]
    owm_code = int(wids[0]) if wids else 0
    # Map OWM condition id roughly to WMO-like bucket for short label
    wmo_like = _owm_id_to_wmo_bucket(owm_code)

    raw_p = data["main"].get("pressure")
    pressure_hpa = float(raw_p) if raw_p is not None else None
    tz_offset = data.get("timezone")

    t_str = f"{temp:.0f}" if abs(temp - round(temp)) < 0.05 else f"{temp:.1f}"
    return (
        True,
        WeatherPayload(
            _format_weather_line(name, t_str, wmo_like, hum, wind_ms, prec, pressure_hpa, cfg.locale),
            int(tz_offset) if tz_offset is not None else None,
        ),
        "provider_error",
    )


def _owm_id_to_wmo_bucket(owm_id: int) -> int:
    """Rough mapping OpenWeather condition code → WMO-style code for labels."""
    if owm_id == 800:
        return 0
    if owm_id == 801:
        return 1
    if owm_id == 802:
        return 2
    if owm_id in (803, 804):
        return 3
    if owm_id in (701, 721, 741):
        return 45
    if 200 <= owm_id <= 232:
        return 95
    if 300 <= owm_id <= 321:
        return 51
    if 500 <= owm_id <= 504:
        return 63
    if 511 <= owm_id <= 531:
        return 80
    if 600 <= owm_id <= 622:
        return 71
    return 3
