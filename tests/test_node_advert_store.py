"""Tests for node advert store contact snapshot sync."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meshcore_bot.node_advert_store import NodeAdvertStore


class NodeAdvertStoreSnapshotTests(unittest.TestCase):
    def test_upsert_contacts_snapshot_tracks_new_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = NodeAdvertStore(Path(tmp) / "nodes.json", retention_days=7, max_stored=100)
            store.load()
            stats = store.upsert_contacts_snapshot(
                [
                    {"public_key": "aa", "name": "A", "type": "repeater"},
                    {"name": "no-key"},
                    {"public_key": "bb", "name": "B", "type": "client"},
                ]
            )
            self.assertEqual(stats.upserted, 2)
            self.assertEqual(stats.new, 2)
            self.assertEqual(stats.skipped_no_key, 1)
            self.assertEqual(store.count(), 2)

            stats2 = store.upsert_contacts_snapshot(
                [
                    {"public_key": "aa", "name": "A2", "type": "repeater"},
                    {"public_key": "cc", "name": "C", "type": "repeater"},
                ]
            )
            self.assertEqual(stats2.upserted, 2)
            self.assertEqual(stats2.new, 1)
            self.assertEqual(stats2.skipped_no_key, 0)
            self.assertEqual(store.count(), 3)


if __name__ == "__main__":
    unittest.main()
