"""Weather: Open-Meteo (no key, RU-friendly) and OpenWeatherMap (API key)."""

from __future__ import annotations

import asyncio
import csv
import datetime as _dt
import gzip
import io
import logging
import math
import os
import sqlite3
import time
from typing import TYPE_CHECKING

import httpx

from meshcore_bot.textutil import clip_utf8_bytes

if TYPE_CHECKING:
    from meshcore_bot.config import BotConfig
    from meshcore_bot.i18n import I18n

logger = logging.getLogger(__name__)

_weather_lock = asyncio.Lock()
# (provider, locale, city_lower) -> (expires_monotonic, line)
_weather_cache: dict[tuple[str, str, str], tuple[float, str]] = {}

_meteostat_lock = asyncio.Lock()
_meteostat_db_local_path: str | None = None


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

METEOSTAT_STATIONS_DB_URL = "https://data.meteostat.net/stations.db"
METEOSTAT_BULK_BASE = "https://data.meteostat.net"

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
    def _norm(p: str) -> str:
        p = (p or "").strip().lower()
        if p in ("openmeteo", "open-meteo"):
            return "openmeteo"
        if p in ("openweathermap", "owm"):
            return "openweathermap"
        if p in ("meteostat", "meteostat.net"):
            return "meteostat"
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

    last_line: str | None = None
    for idx, provider_norm in enumerate(providers):
        if ttl_min > 0:
            key = _weather_cache_key(city, provider_norm, str(cfg.locale))
            async with _weather_lock:
                now = time.monotonic()
                _weather_prune_expired(now)
                hit = _weather_cache.get(key)
                if hit is not None:
                    exp, line = hit
                    if now < exp:
                        logger.debug(
                            "weather cache hit city=%r provider=%s", city.strip(), provider_norm
                        )
                        return line

        if provider_norm == "openmeteo":
            ok, line = await _fetch_open_meteo(city, cfg, i18n)
        elif provider_norm == "openweathermap":
            ok, line = await _fetch_open_weather_map(city, cfg, i18n)
        elif provider_norm == "meteostat":
            ok, line = await _fetch_meteostat(city, cfg, i18n)
        else:
            ok, line = False, i18n.t("errors.weather_failed", detail="unsupported provider")

        if ok:
            if ttl_min > 0:
                key = _weather_cache_key(city, provider_norm, str(cfg.locale))
                async with _weather_lock:
                    now = time.monotonic()
                    _weather_prune_expired(now)
                    _weather_cache[key] = (now + ttl_min * 60.0, line)
                    logger.debug("weather cache store key=%s ttl_min=%s", key, ttl_min)
            if idx > 0:
                logger.info(
                    "weather provider fallback used primary=%s fallback=%s city=%r",
                    primary,
                    provider_norm,
                    city.strip(),
                )
            return line

        last_line = line
        if idx + 1 < len(providers):
            logger.warning(
                "weather provider failed, trying fallback: provider=%s city=%r",
                provider_norm,
                city.strip(),
            )

    return last_line or i18n.t("errors.weather_failed", detail="no providers configured")


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


async def _meteostat_ensure_stations_db() -> str:
    """Download stations.db once and reuse it between requests."""
    global _meteostat_db_local_path
    if _meteostat_db_local_path is not None:
        return _meteostat_db_local_path

    path = "/tmp/meshcore_bot_meteostat_stations.db"
    try:
        st = os.stat(path)
        # Refresh roughly monthly
        if (time.time() - float(st.st_mtime)) < 30.0 * 24.0 * 3600.0 and st.st_size > 1024 * 1024:
            _meteostat_db_local_path = path
            return path
    except FileNotFoundError:
        pass
    except OSError:
        pass

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(METEOSTAT_STATIONS_DB_URL)
        r.raise_for_status()
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, path)

    _meteostat_db_local_path = path
    return path


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


async def _fetch_meteostat(city: str, cfg: BotConfig, i18n: I18n) -> tuple[bool, str]:
    # Use Open-Meteo geocoding to get coordinates (no API key).
    lang = "ru" if cfg.locale == "ru" else "en"
    lang_geo = "ru" if lang == "ru" else "en"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            gr = await client.get(
                OPEN_METEO_GEOCODE,
                params={"name": city, "count": 1, "language": lang_geo, "format": "json"},
            )
            gr.raise_for_status()
            data = gr.json()
            results = data.get("results") or []
            if not results:
                return False, i18n.t("errors.city_not_found")
            g0 = results[0]
            lat, lon = float(g0["latitude"]), float(g0["longitude"])
            name = str(g0.get("name") or city)
    except httpx.HTTPStatusError as e:
        logger.warning("Meteostat geocode HTTP error: %s", e)
        return False, i18n.t("errors.weather_failed", detail=e.response.status_code)
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Meteostat geocode failed: %s", e)
        return False, i18n.t("errors.weather_failed", detail=str(e)[:80])

    async with _meteostat_lock:
        try:
            db_path = await _meteostat_ensure_stations_db()
        except (httpx.HTTPError, OSError) as e:
            logger.warning("Meteostat stations.db download failed: %s", e)
            return False, i18n.t("errors.weather_failed", detail="stations db download failed")

    try:
        station_ids = _meteostat_station_candidates(db_path, lat, lon, limit=12)
    except Exception as e:  # noqa: BLE001
        logger.warning("Meteostat stations.db query failed: %s", e)
        return False, i18n.t("errors.weather_failed", detail="stations db query failed")

    if not station_ids:
        return False, i18n.t("errors.weather_failed", detail="no stations nearby")

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
        return True, _format_weather_line(
            name,
            t_str,
            wmo_like,
            float(hum),
            float(wind_ms),
            float(prcp),
            pressure_hpa,
            cfg.locale,
        )

    return False, i18n.t("errors.weather_failed", detail="no recent station data")


async def _fetch_open_meteo(city: str, cfg: BotConfig, i18n: I18n) -> tuple[bool, str]:
    lang = "ru" if cfg.locale == "ru" else "en"
    lang_geo = "ru" if lang == "ru" else "en"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            gr = await client.get(
                OPEN_METEO_GEOCODE,
                params={"name": city, "count": 1, "language": lang_geo, "format": "json"},
            )
            gr.raise_for_status()
            data = gr.json()
            results = data.get("results") or []
            if not results:
                return False, i18n.t("errors.city_not_found")
            g0 = results[0]
            lat, lon = float(g0["latitude"]), float(g0["longitude"])
            name = str(g0.get("name") or city)

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
                return False, i18n.t("errors.weather_failed", detail="no current weather")
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
    except httpx.HTTPStatusError as e:
        logger.warning("Open-Meteo HTTP error: %s", e)
        return False, i18n.t("errors.weather_failed", detail=e.response.status_code)
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Open-Meteo request failed: %s", e)
        return False, i18n.t("errors.weather_failed", detail=str(e)[:80])

    tf = float(temp)
    t_str = f"{tf:.0f}" if abs(tf - round(tf)) < 0.05 else f"{tf:.1f}"
    return True, _format_weather_line(
        name,
        t_str,
        wcode,
        float(hum),
        float(wind_ms),
        float(prec),
        pressure_hpa,
        cfg.locale,
    )


async def _fetch_open_weather_map(city: str, cfg: BotConfig, i18n: I18n) -> tuple[bool, str]:
    key = (os.environ.get("WEATHER_API_KEY") or "").strip()
    if not key:
        return False, i18n.t("errors.weather_not_configured")

    lang = "ru" if cfg.locale == "ru" else "en"
    params_geo = {"q": city, "limit": 1, "appid": key}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            gr = await client.get(OWM_GEOCODE_URL, params=params_geo)
            gr.raise_for_status()
            geo = gr.json()
            if not geo:
                return False, i18n.t("errors.city_not_found")
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
        return False, i18n.t("errors.weather_failed", detail=e.response.status_code)
    except (httpx.RequestError, KeyError, IndexError, ValueError) as e:
        logger.warning("Weather request failed: %s", e)
        return False, i18n.t("errors.weather_failed", detail=str(e)[:80])

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

    t_str = f"{temp:.0f}" if abs(temp - round(temp)) < 0.05 else f"{temp:.1f}"
    return True, _format_weather_line(
        name, t_str, wmo_like, hum, wind_ms, prec, pressure_hpa, cfg.locale
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
