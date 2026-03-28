"""Persistent blocklist of public keys (hex)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def _normalize_hex(s: str) -> str:
    return s.strip().lower().replace(" ", "")


class Blacklist:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._keys: set[str] = set()
        self.load()

    def load(self) -> None:
        self._keys.clear()
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for e in raw.get("blocked_keys", []):
                h = _normalize_hex(str(e))
                if h:
                    self._keys.add(h)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not load blacklist %s: %s", self._path, e)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"blocked_keys": sorted(self._keys)}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def is_blocked(self, public_key_hex: str | None, pubkey_prefix: str | None) -> bool:
        """Match full key or 12-hex (6-byte) prefix used by MeshCore."""
        pk = _normalize_hex(public_key_hex) if public_key_hex else ""
        pf = _normalize_hex(pubkey_prefix) if pubkey_prefix else ""
        if pk and pk in self._keys:
            return True
        if pf and pf in self._keys:
            return True
        if pk:
            for entry in self._keys:
                if len(entry) < 64 and pk.startswith(entry):
                    return True
        return False

    def add(self, keys: Iterable[str]) -> None:
        for k in keys:
            h = _normalize_hex(k)
            if h:
                self._keys.add(h)
        self.save()

    def remove(self, keys: Iterable[str]) -> None:
        for k in keys:
            self._keys.discard(_normalize_hex(k))
        self.save()
