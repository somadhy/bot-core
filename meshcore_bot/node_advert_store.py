"""Persistent store of node adverts built from companion contacts snapshots."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _now_ts() -> float:
    return time.time()


def _normalize_hex(s: str) -> str:
    return (s or "").strip().lower()


def _pick_first_str(contact: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = contact.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


@dataclass(frozen=True)
class ContactSnapshotStats:
    upserted: int = 0
    new: int = 0
    skipped_no_key: int = 0


@dataclass
class NodeAdvertRecord:
    public_key: str
    node_name: str
    node_type: str
    last_advert_ts: float


class NodeAdvertStore:
    def __init__(self, path: Path, retention_days: float, max_stored: int) -> None:
        self._path = path
        self._retention_sec = max(0.0, float(retention_days) * 86400.0)
        self._max_stored = max(1, int(max_stored))
        self._items: dict[str, NodeAdvertRecord] = {}
        self._loaded = False

    def count(self) -> int:
        return len(self._items)

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        items = raw.get("items")
        if not isinstance(items, list):
            return
        for row in items:
            if not isinstance(row, dict):
                continue
            key = _normalize_hex(str(row.get("public_key") or ""))
            if not key:
                continue
            try:
                ts = float(row.get("last_advert_ts", 0) or 0)
            except (TypeError, ValueError):
                ts = 0.0
            self._items[key] = NodeAdvertRecord(
                public_key=key,
                node_name=str(row.get("node_name") or "").strip(),
                node_type=str(row.get("node_type") or "").strip(),
                last_advert_ts=ts,
            )
        self.purge_and_trim()

    def save(self) -> None:
        if not self._loaded:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [asdict(v) for v in self._items.values()]
        rows.sort(key=lambda x: float(x.get("last_advert_ts", 0) or 0), reverse=True)
        payload = {"items": rows}
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def upsert_contact(self, contact: dict[str, Any], now_ts: float | None = None) -> bool:
        key = _normalize_hex(
            _pick_first_str(
                contact,
                (
                    "public_key",
                    "pubkey",
                    "node_pubkey",
                    "key",
                ),
            )
        )
        if not key:
            return False
        ts = float(now_ts if now_ts is not None else _now_ts())
        rec = NodeAdvertRecord(
            public_key=key,
            node_name=_pick_first_str(contact, ("adv_name", "name", "node_name", "display_name")),
            node_type=_pick_first_str(contact, ("type", "node_type", "kind", "role")),
            last_advert_ts=ts,
        )
        self._items[key] = rec
        return True

    def upsert_contacts_snapshot(self, contacts: list[dict[str, Any]]) -> ContactSnapshotStats:
        upserted = 0
        new = 0
        skipped_no_key = 0
        now = _now_ts()
        for c in contacts:
            key = _normalize_hex(
                _pick_first_str(
                    c,
                    (
                        "public_key",
                        "pubkey",
                        "node_pubkey",
                        "key",
                    ),
                )
            )
            if not key:
                skipped_no_key += 1
                continue
            is_new = key not in self._items
            if self.upsert_contact(c, now_ts=now):
                upserted += 1
                if is_new:
                    new += 1
        self.purge_and_trim()
        return ContactSnapshotStats(
            upserted=upserted,
            new=new,
            skipped_no_key=skipped_no_key,
        )

    def purge_and_trim(self) -> None:
        if self._retention_sec > 0:
            min_ts = _now_ts() - self._retention_sec
            self._items = {k: v for k, v in self._items.items() if v.last_advert_ts >= min_ts}
        if len(self._items) <= self._max_stored:
            return
        ordered = sorted(self._items.values(), key=lambda x: x.last_advert_ts, reverse=True)
        keep = ordered[: self._max_stored]
        self._items = {x.public_key: x for x in keep}

    def find_by_prefix(self, key_prefix_hex: str) -> list[NodeAdvertRecord]:
        p = _normalize_hex(key_prefix_hex)
        if not p:
            return []
        out = [v for k, v in self._items.items() if k.startswith(p)]
        out.sort(key=lambda x: x.last_advert_ts, reverse=True)
        return out
