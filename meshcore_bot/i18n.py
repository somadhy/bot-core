"""Localized strings for ru / en."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from meshcore_bot.config import Locale


def _load_bundle(locale: Locale) -> dict[str, Any]:
    pkg = "meshcore_bot.locales"
    name = f"{locale}.json"
    text = resources.files(pkg).joinpath(name).read_text(encoding="utf-8")
    return json.loads(text)


class I18n:
    def __init__(self, locale: Locale) -> None:
        self._locale = locale
        self._data = _load_bundle(locale)

    def t(self, key: str, **kwargs: Any) -> str:
        cur: Any = self._data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return key
            cur = cur[part]
        if not isinstance(cur, str):
            return key
        try:
            return cur.format(**kwargs) if kwargs else cur
        except KeyError:
            return cur
