"""platformd.poller -- transition detection, governor gating, and the
failure tags. Fake adapter + fake http_get; no network, explicit clocks."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapters.base import SourceAdapter, observation  # noqa: E402
from adapters.recreation_gov import RecreationGovAdapter  # noqa: E402
from platformd import core, db, log, poller, serve  # noqa: E402

DROP = "camp"
T0 = 1_700_000_000.0


class FakeAdapter(SourceAdapter):
    """Serves whatever the test puts in self.states: {unit_id: state}."""
    name = "fake"
    min_spacing_seconds = 0
    daily_request_budget = 1000

    def __init__(self):
        self.states = {}
        self.fetches = 0
        self.healthy = True
        self.parse_error = None

    def fetch(self, target, date, http_get):
        self.fetches += 1
        return http_get("https://fake.test/%s" % date, {})

    def parse(self, raw, target, date):
        if self.parse_error:
            raise self.parse_error
        return [observation(u, date, s, label=u, meta={"url": "https://x/" + u})
                for u, s in self.states.items()]

    def health_check(self, raw):
        return self.healthy, "ok" if self.healthy else "empty"


def _ok_get(url, headers):
    return {}


class _Base(unittest.TestCase):
    def setUp(self):
        log.clear()
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Camp", risk_tier="green", now=T0)
        self.adapter = FakeAdapter()
        self.watch = {"drop_id": DROP, "adapter": "fake", "target": {},
                      "dates": ["2026-11-14"]}

    def poll(self, now, http_get=_ok_get):
        return poller.poll_watch(self.conn, self.watch, self.adapter,
                                 http_get, clock=lambda: now,
                                 sleep=self.fail_on_sleep)

    def fail_on_sleep(self, seconds):
        raise AssertionError("unexpected sleep(%s)" % seconds)

    def events(self):
        return self.conn.execute(
            "SELECT kind, unit_id, payload_json FROM events ORDER BY id").fetchall()


class DetectionTest(_Base):
    def test_first_pass_seeds_without_alerting(self):
        self.adapter.states = {"A1": "available", "A2": "soldout"}
        self.assertEqual(self.poll(T0)["events"], 0)
        self.assertEqual(self.events(), [])

    def test_soldout_to_available_fires_once(self):
        self.adapter.states = {"A1": "soldout"}
        self.poll(T0)
        self.adapter.states = {"A1": "available"}
        self.assertEqual(self.poll(T0 + 60)["events"], 1)
        self.assertEqual(self.poll(T0 + 120)["events"], 0)  # still open: no repeat
        kind, unit, payload = self.events()[0]
        self.assertEqual((kind, unit), ("opened", "A1"))
        self.assertEqual(json.loads(payload)["url"], "https://x/A1")

    def test_reopening_after_rebook_fires_again(self):
        self.adapter.states = {"A1": "soldout"}
        self.poll(T0)
        for i, state in enumerate(["available", "soldout", "available"], 1):
            self.adapter.states = {"A1": state}
            self.poll(T0 + 60 * i)
        self.assertEqual(len(self.events()), 2)

    def test_unit_new_after_baseline_is_a_new_listing(self):
        self.adapter.states = {"A1": "soldout"}
        self.poll(T0)
        self.adapter.states = {"A1": "soldout", "B9": "available"}
        self.poll(T0 + 60)
        self.assertEqual(self.events()[0]["kind"], "new_listing")

    def test_event_queues_outbox_for_active_subscribers(self):
        sub = core.subscribe(self.conn, DROP, "ntfy", "topic-x", "free", T0)
        core.verify(self.conn, sub["id"], sub["verify_code"], T0)
        self.adapter.states = {"A1": "soldout"}
        self.poll(T0)
        self.adapter.states = {"A1": "available"}
        self.poll(T0 + 60)
        n = self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.assertEqual(n, 1)


    def test_first_pass_over_many_dates_is_silent(self):
        # regression: live run 2026-10-27 -- seeding date 1 made date 2's
        # units look like new listings within the same first pass
        self.watch["dates"] = ["2026-11-14", "2026-11-15"]
        self.adapter.states = {"A1": "available"}
        self.adapter.parse = lambda raw, t, d: [
            observation("A1:" + d, d, "available", label="A1")]
        self.poll(T0)
        self.assertEqual(self.events(), [])

    def test_date_rolling_into_window_is_baselined_not_alerted(self):
        self.adapter.parse = lambda raw, t, d: [
            observation("A1:" + d, d, "available", label="A1")]
        self.poll(T0)
        self.watch["dates"] = ["2026-11-15"]  # tomorrow enters the window
        self.poll(T0 + 86400)
        self.assertEqual(self.events(), [])


class GovernorTest(_Base):
    def test_spacing_denial_is_waited_out_within_a_pass(self):
        # regression: live run -- recreation.gov's 2s floor denied the
        # second month's fetch and the pass silently skipped it
        self.adapter.min_spacing_seconds = 2
        self.adapter.fetch_key = lambda t, d: d
        self.watch["dates"] = ["2026-11-14", "2026-12-14"]
        clock = [T0]
        stats = poller.poll_watch(
            self.conn, self.watch, self.adapter, _ok_get,
            clock=lambda: clock[0],
            sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.assertEqual(stats["fetched"], 2)
        self.assertEqual(clock[0], T0 + 2)

    def test_budget_denial_ends_the_pass_without_sleeping(self):
        self.adapter.daily_request_budget = 1
        self.poll(T0)
        stats = self.poll(T0 + 1)
        self.assertEqual(stats["denied"], "budget-exhausted")
        self.assertEqual(self.adapter.fetches, 1)

    def test_dates_sharing_a_fetch_key_cost_one_request(self):
        rg = RecreationGovAdapter()
        rg.min_spacing_seconds = 0
        urls = []

        def get(url, headers):
            urls.append(url)
            return {"campsites": {}}

        watch = {"drop_id": DROP, "adapter": rg.name,
                 "target": {"campground_id": "232447"},
                 "dates": ["2026-11-01", "2026-11-02", "2026-12-01"]}
        poller.poll_watch(self.conn, watch, rg, get, clock=lambda: T0)
        self.assertEqual(len(urls), 2)


class FailureTagTest(_Base):
    def test_refused_is_tagged_and_counts_toward_breaker(self):
        def refuse(url, headers):
            raise poller.UpstreamRefused(429)
        self.poll(T0, refuse)
        self.assertEqual(len(log.recent(log.TAG_REFUSED)), 1)
        fails = self.conn.execute(
            "SELECT breaker_fails FROM governor WHERE source='fake'").fetchone()[0]
        self.assertEqual(fails, 1)

    def test_socket_error_is_pipe(self):
        def reset(url, headers):
            raise ConnectionResetError()
        self.poll(T0, reset)
        self.assertEqual(log.recent(log.TAG_PIPE)[0].fields["exc_class"],
                         "ConnectionResetError")

    def test_blind_payload_skips_detection(self):
        self.adapter.states = {"A1": "available"}
        self.adapter.healthy = False
        self.poll(T0)
        self.assertEqual(len(log.recent(log.TAG_BLIND)), 1)
        self.assertEqual(poller._ledger_states(self.conn, DROP), {})

    def test_parse_drift_is_tagged_not_raised(self):
        self.adapter.parse_error = ValueError("shape changed")
        self.poll(T0)
        self.assertEqual(len(log.recent(log.TAG_PARSE)), 1)


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
