"""OpenWeatherMap current weather."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from meshcore_bot.config import BotConfig
    from meshcore_bot.i18n import I18n

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://api.openweathermap.org/geo/1.0/direct"
WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"


async def fetch_weather_line(city: str, cfg: BotConfig, i18n: I18n) -> str:
    key = (os.environ.get("WEATHER_API_KEY") or "").strip()
    if not key:
        return i18n.t("errors.weather_not_configured")

    if cfg.weather_provider != "openweathermap":
        return i18n.t("errors.weather_failed", detail="unsupported provider")

    lang = "ru" if cfg.locale == "ru" else "en"
    params_geo = {"q": city, "limit": 1, "appid": key}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            gr = await client.get(GEOCODE_URL, params=params_geo)
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
            wr = await client.get(WEATHER_URL, params=params_w)
            wr.raise_for_status()
            data = wr.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Weather HTTP error: %s", e)
        return i18n.t("errors.weather_failed", detail=e.response.status_code)
    except (httpx.RequestError, KeyError, IndexError, ValueError) as e:
        logger.warning("Weather request failed: %s", e)
        return i18n.t("errors.weather_failed", detail=str(e)[:80])

    temp = data["main"]["temp"]
    t_str = f"{temp:.0f}" if abs(temp - round(temp)) < 0.05 else f"{temp:.1f}"
    desc = (data["weather"][0].get("description") or "").strip()
    return i18n.t("weather.line", city=name, temp=t_str, desc=desc)
