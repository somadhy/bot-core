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
    poll_keepalive_sec: float
    poll_keepalive_only_when_idle_sec: float
    weather_provider: str
    weather_default_city: str
    weather_cache_ttl_minutes: float
    blacklist_path: Path
    reply_delay_sec: float
    advert_interval_hours: float
    advert_flood: bool
    admin_public_keys: list[str] = field(default_factory=list)
    # Channel indices where `stop` is allowed for any sender (no pubkey; channel-only).
    admin_channel_indices: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base_dir: Path) -> BotConfig:
        serial = raw.get("serial") or {}
        channels = raw.get("channels") or {}
        dm = raw.get("dm") or {}
        weather = raw.get("weather") or {}
        blacklist = raw.get("blacklist") or {}
        admins = raw.get("admins") or {}
        advert = raw.get("advert") or {}
        poll = raw.get("poll") or {}

        loc = str(raw.get("locale", "en")).lower()
        if loc not in ("ru", "en"):
            raise ValueError(f"locale must be ru or en, got {loc!r}")

        bl_path = blacklist.get("path", "data/blacklist.json")
        blacklist_path = (base_dir / bl_path).resolve()

        keys = [str(k).strip().lower() for k in (admins.get("public_keys") or []) if str(k).strip()]

        enabled = list(channels.get("enabled_indices") or [])
        admin_chans: list[int] = []
        for x in admins.get("channel_indices") or []:
            try:
                admin_chans.append(int(x))
            except (TypeError, ValueError):
                raise ValueError(
                    f"admins.channel_indices must be a list of integers, got invalid entry: {x!r}"
                ) from None
        for idx in admin_chans:
            if idx not in enabled:
                raise ValueError(
                    f"admins.channel_indices entry {idx} must be listed in channels.enabled_indices "
                    f"(currently {enabled!r})"
                )

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

        _ttl_raw = weather.get("cache_ttl_minutes")
        if _ttl_raw is None:
            weather_cache_ttl_minutes = 15.0
        else:
            try:
                weather_cache_ttl_minutes = float(_ttl_raw)
            except (TypeError, ValueError):
                weather_cache_ttl_minutes = 15.0
        if weather_cache_ttl_minutes < 0:
            weather_cache_ttl_minutes = 0.0
        # Cap ~7 days
        if weather_cache_ttl_minutes > 10080.0:
            weather_cache_ttl_minutes = 10080.0

        def _f(name: str, default: float) -> float:
            try:
                return float(poll.get(name, default))
            except (TypeError, ValueError):
                return default

        poll_keepalive_sec = _f("keepalive_sec", 60.0)
        if poll_keepalive_sec < 0:
            poll_keepalive_sec = 0.0
        if poll_keepalive_sec > 3600.0:
            poll_keepalive_sec = 3600.0

        poll_keepalive_only_when_idle_sec = _f("keepalive_only_when_idle_sec", 30.0)
        if poll_keepalive_only_when_idle_sec < 0:
            poll_keepalive_only_when_idle_sec = 0.0
        if poll_keepalive_only_when_idle_sec > 3600.0:
            poll_keepalive_only_when_idle_sec = 3600.0

        return cls(
            serial_device=str(serial.get("device", "/dev/ttyUSB0")),
            serial_baudrate=int(serial.get("baudrate", 115200)),
            channels_enabled=enabled,
            dm_enabled=bool(dm.get("enabled", True)),
            locale=loc,  # type: ignore[arg-type]
            poll_keepalive_sec=poll_keepalive_sec,
            poll_keepalive_only_when_idle_sec=poll_keepalive_only_when_idle_sec,
            weather_provider=str(weather.get("provider", "openmeteo")),
            weather_default_city=str(
                weather.get("default_city") or weather.get("city") or ""
            ).strip(),
            weather_cache_ttl_minutes=weather_cache_ttl_minutes,
            blacklist_path=blacklist_path,
            reply_delay_sec=reply_delay_sec,
            advert_interval_hours=advert_interval_hours,
            advert_flood=bool(advert.get("flood", False)),
            admin_public_keys=keys,
            admin_channel_indices=admin_chans,
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
