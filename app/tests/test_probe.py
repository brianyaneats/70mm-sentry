"""app.probe -- the owner's one-request live check, driven here with a
fake http_get (the real run happens on the VPS; see deploy/README.md)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import probe  # noqa: E402
from engine import poller  # noqa: E402


def _month(states):
    return {"campsites": {
        sid: {"site": sid, "campsite_type": "STANDARD",
              "availabilities": {"2026-11-14T00:00:00Z": st}}
        for sid, st in states.items()}}


class ProbeTest(unittest.TestCase):
    def test_healthy_payload_reports_sites_and_availability(self):
        calls = []

        def http_get(url, headers):
            calls.append(url)
            return _month({"1": "Available", "2": "Reserved", "3": "Available"})

        out = probe.probe("232447", "2026-11-14", http_get)
        self.assertEqual(len(calls), 1)
        self.assertIn("/campground/232447/month", calls[0])
        self.assertEqual((out["ok"], out["sites"], out["available"]), (True, 3, 2))
        # first pass only seeds the ledger: a probe never "alerts"
        self.assertEqual(out["events"], 0)

    def test_refusal_is_reported_not_raised(self):
        def http_get(url, headers):
            raise poller.UpstreamRefused(403)

        out = probe.probe("232447", "2026-11-14", http_get)
        self.assertFalse(out["ok"])
        self.assertEqual(out["problems"], ["REFUSED"])

    def test_cli_usage(self):
        self.assertEqual(probe.main(["not-a-number"]), 2)


if __name__ == "__main__":
    unittest.main()
