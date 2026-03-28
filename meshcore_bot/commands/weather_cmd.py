"""Weather: Open-Meteo (no key, RU-friendly) and OpenWeatherMap (API key)."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import httpx

from meshcore_bot.textutil import clip_utf8_bytes

if TYPE_CHECKING:
    from meshcore_bot.config import BotConfig
    from meshcore_bot.i18n import I18n

logger = logging.getLogger(__name__)

# Reply body (before @mention) should stay compact; full message clipped in service to 140 bytes UTF-8.
_CITY_MAX_BYTES = 28

OWM_GEOCODE_URL = "https://api.openweathermap.org/geo/1.0/direct"
OWM_WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"

OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

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
    provider = (cfg.weather_provider or "openmeteo").strip().lower()
    if provider in ("openmeteo", "open-meteo"):
        return await _fetch_open_meteo(city, cfg, i18n)
    if provider in ("openweathermap", "owm"):
        return await _fetch_open_weather_map(city, cfg, i18n)
    return i18n.t("errors.weather_failed", detail="unsupported provider")


async def _fetch_open_meteo(city: str, cfg: BotConfig, i18n: I18n) -> str:
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
                return i18n.t("errors.city_not_found")
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
                return i18n.t("errors.weather_failed", detail="no current weather")
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
        return i18n.t("errors.weather_failed", detail=e.response.status_code)
    except (httpx.RequestError, KeyError, ValueError, TypeError) as e:
        logger.warning("Open-Meteo request failed: %s", e)
        return i18n.t("errors.weather_failed", detail=str(e)[:80])

    tf = float(temp)
    t_str = f"{tf:.0f}" if abs(tf - round(tf)) < 0.05 else f"{tf:.1f}"
    return _format_weather_line(
        name,
        t_str,
        wcode,
        float(hum),
        float(wind_ms),
        float(prec),
        pressure_hpa,
        cfg.locale,
    )


async def _fetch_open_weather_map(city: str, cfg: BotConfig, i18n: I18n) -> str:
    key = (os.environ.get("WEATHER_API_KEY") or "").strip()
    if not key:
        return i18n.t("errors.weather_not_configured")

    lang = "ru" if cfg.locale == "ru" else "en"
    params_geo = {"q": city, "limit": 1, "appid": key}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            gr = await client.get(OWM_GEOCODE_URL, params=params_geo)
            gr.raise_for_status()
            geo = gr.json()
            if not geo:
                return i18n.t("errors.city_not_found")
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
        return i18n.t("errors.weather_failed", detail=e.response.status_code)
    except (httpx.RequestError, KeyError, IndexError, ValueError) as e:
        logger.warning("Weather request failed: %s", e)
        return i18n.t("errors.weather_failed", detail=str(e)[:80])

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
    return _format_weather_line(
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
