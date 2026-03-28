"""Load and validate bot configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

Locale = Literal["ru", "en"]


@dataclass
class BotConfig:
    serial_device: str
    serial_baudrate: int
    channels_enabled: list[int]
    dm_enabled: bool
    locale: Locale
    weather_provider: str
    weather_default_city: str
    blacklist_path: Path
    reply_delay_sec: float
    advert_interval_hours: float
    advert_flood: bool
    admin_public_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base_dir: Path) -> BotConfig:
        serial = raw.get("serial") or {}
        channels = raw.get("channels") or {}
        dm = raw.get("dm") or {}
        weather = raw.get("weather") or {}
        blacklist = raw.get("blacklist") or {}
        admins = raw.get("admins") or {}
        advert = raw.get("advert") or {}

        loc = str(raw.get("locale", "en")).lower()
        if loc not in ("ru", "en"):
            raise ValueError(f"locale must be ru or en, got {loc!r}")

        bl_path = blacklist.get("path", "data/blacklist.json")
        blacklist_path = (base_dir / bl_path).resolve()

        keys = [str(k).strip().lower() for k in (admins.get("public_keys") or []) if str(k).strip()]

        try:
            reply_delay_sec = float(raw.get("reply_delay_sec", 0) or 0)
        except (TypeError, ValueError):
            reply_delay_sec = 0.0
        reply_delay_sec = max(0.0, min(reply_delay_sec, 600.0))

        try:
            advert_interval_hours = float(advert.get("interval_hours", 0) or 0)
        except (TypeError, ValueError):
            advert_interval_hours = 0.0
        if advert_interval_hours < 0:
            advert_interval_hours = 0.0
        # Cap to avoid accidental huge values (e.g. typo); ~1 year
        if advert_interval_hours > 8760.0:
            advert_interval_hours = 8760.0

        return cls(
            serial_device=str(serial.get("device", "/dev/ttyUSB0")),
            serial_baudrate=int(serial.get("baudrate", 115200)),
            channels_enabled=list(channels.get("enabled_indices") or []),
            dm_enabled=bool(dm.get("enabled", True)),
            locale=loc,  # type: ignore[arg-type]
            weather_provider=str(weather.get("provider", "openmeteo")),
            weather_default_city=str(
                weather.get("default_city") or weather.get("city") or ""
            ).strip(),
            blacklist_path=blacklist_path,
            reply_delay_sec=reply_delay_sec,
            advert_interval_hours=advert_interval_hours,
            advert_flood=bool(advert.get("flood", False)),
            admin_public_keys=keys,
        )


def load_config() -> BotConfig:
    path = os.environ.get("MESHCORE_BOT_CONFIG", "config.yaml")
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"Config not found: {p}. Set MESHCORE_BOT_CONFIG or copy config.example.yaml to config.yaml."
        )
    base_dir = p.parent
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return BotConfig.from_dict(raw, base_dir)
