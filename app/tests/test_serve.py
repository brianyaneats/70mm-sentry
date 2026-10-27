"""app.serve -- the production entrypoint. No real network: the
redirect test runs a local http.server on an ephemeral port."""

import datetime as dt
import json
import os
import tempfile
import unittest

from app import serve
from engine import core, db


class LoadCatalogTest(unittest.TestCase):
    def test_registers_drops_and_is_idempotent_across_boots(self):
        tmp = tempfile.mkdtemp()
        cat = os.path.join(tmp, "catalog.json")
        with open(cat, "w") as fh:
            json.dump([{"id": "d1", "title": "Drop one", "source": "x",
                        "risk_tier": "green"}], fh)
        conn = db.connect(os.path.join(tmp, "db.sqlite3"))
        self.assertEqual(serve.load_catalog(conn, cat), 1)
        self.assertEqual(serve.load_catalog(conn, cat), 1)  # second boot
        self.assertEqual(len(core.list_drops(conn)), 1)
        self.assertEqual(core.get_drop(conn, "d1")["title"], "Drop one")
        conn.close()


class BuildChannelsTest(unittest.TestCase):
    def test_every_selectable_channel_has_a_real_sender(self):
        # telegram is disabled in the UI and deliberately absent here
        self.assertEqual(set(serve.build_channels()),
                         {"discord", "ntfy", "webhook"})


class LoadWatchesTest(unittest.TestCase):
    def test_window_days_rolls_from_today_and_plain_drops_are_skipped(self):
        path = os.path.join(tempfile.mkdtemp(), "c.json")
        with open(path, "w") as fh:
            json.dump([{"id": "a", "title": "A", "adapter": "recreation_gov",
                        "target": {"campground_id": "1"}, "window_days": 3},
                       {"id": "b", "title": "B"}], fh)
        watches = serve.load_watches(path, today=dt.date(2026, 11, 30))
        self.assertEqual([w["drop_id"] for w in watches], ["a"])
        self.assertEqual(watches[0]["dates"],
                         ["2026-11-30", "2026-12-01", "2026-12-02"])


if __name__ == "__main__":
    unittest.main()
