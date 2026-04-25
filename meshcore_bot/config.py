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
    weather_provider_fallback: str
    weather_default_city: str
    weather_cache_ttl_minutes: float
    blacklist_path: Path
    reply_delay_sec: float
    advert_interval_hours: float
    advert_flood: bool
    node_advert_retention_days: float
    node_advert_max_stored: int
    node_advert_store_path: Path
    node_key_preview_bytes: int
    # Private replies: wait for delivery ACK, else retry (separate limits).
    dm_delivery_wait_sec: float
    dm_delivery_max_attempts: int
    admin_public_keys: list[str] = field(default_factory=list)
    # Channel indices where `stop` is allowed for any sender (no pubkey; channel-only).
    admin_channel_indices: list[int] = field(default_factory=list)
    # Extra aliases for commands, loaded from config.yaml. Keys are canonical command
    # names ("weather", "time", "help", "stop", "channels", "msg");
    # values are lists of additional trigger words.
    command_aliases: dict[str, list[str]] = field(default_factory=dict)
    # Optional per-command channel constraints for CHANNEL messages.
    # Empty list means command is allowed in any enabled channel.
    command_channel_indices: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base_dir: Path) -> BotConfig:
        serial = raw.get("serial") or {}
        channels = raw.get("channels") or {}
        dm = raw.get("dm") or {}
        weather = raw.get("weather") or {}
        blacklist = raw.get("blacklist") or {}
        admins = raw.get("admins") or {}
        advert = raw.get("advert") or {}
        nodes = raw.get("nodes") or {}
        dm_delivery = raw.get("dm_delivery") or {}
        poll = raw.get("poll") or {}
        commands = raw.get("commands") or {}

        loc = str(raw.get("locale", "en")).lower()
        if loc not in ("ru", "en"):
            raise ValueError(f"locale must be ru or en, got {loc!r}")

        bl_path = blacklist.get("path", "data/blacklist.json")
        blacklist_path = (base_dir / bl_path).resolve()
        node_store_path_raw = str(nodes.get("store_path", "data/node_adverts.json") or "data/node_adverts.json")
        node_advert_store_path = (base_dir / node_store_path_raw).resolve()

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
        try:
            node_advert_retention_days = float(nodes.get("advert_retention_days", 7) or 7)
        except (TypeError, ValueError):
            node_advert_retention_days = 7.0
        if node_advert_retention_days < 0:
            node_advert_retention_days = 0.0
        if node_advert_retention_days > 3650.0:
            node_advert_retention_days = 3650.0
        try:
            node_advert_max_stored = int(nodes.get("max_stored", 5000) or 5000)
        except (TypeError, ValueError):
            node_advert_max_stored = 5000
        if node_advert_max_stored < 1:
            node_advert_max_stored = 1
        if node_advert_max_stored > 1_000_000:
            node_advert_max_stored = 1_000_000
        try:
            node_key_preview_bytes = int(nodes.get("key_preview_bytes", 2) or 2)
        except (TypeError, ValueError):
            node_key_preview_bytes = 2
        if node_key_preview_bytes < 1:
            node_key_preview_bytes = 1
        if node_key_preview_bytes > 4:
            node_key_preview_bytes = 4

        try:
            dm_delivery_wait_sec = float(dm_delivery.get("wait_sec", 10) or 10)
        except (TypeError, ValueError):
            dm_delivery_wait_sec = 10.0
        if dm_delivery_wait_sec < 0:
            dm_delivery_wait_sec = 0.0
        if dm_delivery_wait_sec > 600.0:
            dm_delivery_wait_sec = 600.0

        try:
            dm_delivery_max_attempts = int(dm_delivery.get("max_attempts", 2) or 2)
        except (TypeError, ValueError):
            dm_delivery_max_attempts = 2
        if dm_delivery_max_attempts < 1:
            dm_delivery_max_attempts = 1
        if dm_delivery_max_attempts > 50:
            dm_delivery_max_attempts = 50

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

        # Optional command aliases:
        # commands:
        #   weather:
        #     aliases: [wz, w]
        #   time:
        #     aliases: [tm]
        #   help:
        #     aliases: [h]
        #   stop:
        #     aliases: []
        #   channels:
        #     aliases: []
        #   msg:
        #     aliases: []
        #   node:
        #     aliases: []
        def _aliases_for(cmd_name: str) -> list[str]:
            bag = commands.get(cmd_name) or {}
            raw_aliases = bag.get("aliases") or []
            result: list[str] = []
            for val in raw_aliases:
                s = str(val).strip()
                if not s:
                    continue
                # Normalize like in router: casefold and strip trailing ":".
                s_norm = s.casefold().rstrip(":")
                if s_norm:
                    result.append(s_norm)
            return result

        command_aliases = {
            "weather": _aliases_for("weather"),
            "time": _aliases_for("time"),
            "help": _aliases_for("help"),
            "stop": _aliases_for("stop"),
            "channels": _aliases_for("channels"),
            "msg": _aliases_for("msg"),
            "node": _aliases_for("node"),
        }

        def _channel_indices_for(cmd_name: str) -> list[int]:
            bag = commands.get(cmd_name) or {}
            raw = bag.get("channel_indices")
            if raw is None:
                return []
            if not isinstance(raw, list):
                raise ValueError(
                    f"commands.{cmd_name}.channel_indices must be a list of integers"
                )
            out: list[int] = []
            for x in raw:
                try:
                    idx = int(x)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"commands.{cmd_name}.channel_indices must contain integers, got {x!r}"
                    ) from None
                if idx not in enabled:
                    raise ValueError(
                        f"commands.{cmd_name}.channel_indices entry {idx} must be listed in "
                        f"channels.enabled_indices (currently {enabled!r})"
                    )
                out.append(idx)
            return out

        command_channel_indices = {
            "weather": _channel_indices_for("weather"),
            "time": _channel_indices_for("time"),
            "help": _channel_indices_for("help"),
            "stop": _channel_indices_for("stop"),
            "channels": _channel_indices_for("channels"),
            "msg": _channel_indices_for("msg"),
            "node": _channel_indices_for("node"),
        }

        return cls(
            serial_device=str(serial.get("device", "/dev/ttyUSB0")),
            serial_baudrate=int(serial.get("baudrate", 115200)),
            channels_enabled=enabled,
            dm_enabled=bool(dm.get("enabled", True)),
            locale=loc,  # type: ignore[arg-type]
            poll_keepalive_sec=poll_keepalive_sec,
            poll_keepalive_only_when_idle_sec=poll_keepalive_only_when_idle_sec,
            weather_provider=str(weather.get("provider", "openmeteo")),
            weather_provider_fallback=str(
                weather.get("fallback_provider") or weather.get("fallback") or ""
            ).strip(),
            weather_default_city=str(
                weather.get("default_city") or weather.get("city") or ""
            ).strip(),
            weather_cache_ttl_minutes=weather_cache_ttl_minutes,
            blacklist_path=blacklist_path,
            reply_delay_sec=reply_delay_sec,
            advert_interval_hours=advert_interval_hours,
            advert_flood=bool(advert.get("flood", False)),
            node_advert_retention_days=node_advert_retention_days,
            node_advert_max_stored=node_advert_max_stored,
            node_advert_store_path=node_advert_store_path,
            node_key_preview_bytes=node_key_preview_bytes,
            dm_delivery_wait_sec=dm_delivery_wait_sec,
            dm_delivery_max_attempts=dm_delivery_max_attempts,
            admin_public_keys=keys,
            admin_channel_indices=admin_chans,
            command_aliases=command_aliases,
            command_channel_indices=command_channel_indices,
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
