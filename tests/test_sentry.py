"""Unit tests for 70mm Sentry. Run from ~/Developer/70mm-sentry:

    python3 -m unittest discover -s tests -v

All network I/O is mocked; nothing here touches Fandango, ntfy, or WhatsApp.
"""

import contextlib
import copy
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import zoneinfo
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentry  # noqa: E402
import preflight  # noqa: E402

TEST_CFG = {
    "channels": {
        "whatsapp": {"enabled": True, "provider": "callmebot",
                     "phone": "+15551234567", "apikey": "SECRET",
                     "meta": {"token": "", "phone_number_id": "", "to": "",
                              "template": ""}},
        "telegram": {"enabled": False, "token": "BOT_TOKEN",
                     "chat_id": "424242", "extra_chat_ids": []},
        "ntfy": {"enabled": True, "server": "https://ntfy.example",
                 "topic": "test-topic"},
    },
    "healthchecks_url": "",
    "movie_match": "odyssey",
    "format_match": "70MM",
    "venues": {
        "AABQI": {"name": "Lincoln Square", "referer": "https://ref.example/ls",
                  "link": "https://link.example/ls", "tz_label": "ET"},
    },
    "blind_streak_alert": 3,
    "capture_last_response": False,
    "target_dates": [],
    "target_after_hour": 18,
    "target_every_seconds": 120,
    "restock_watch_dates": ["2026-08-15"],
    "restock_every_seconds": 300,
    "restock_burst_seconds": 60,
    "burst_windows": [],
    "burst_interval_seconds": 20,
    "base_interval_seconds": 60,
    "seed_horizon": "2026-08-19",
    "sweep_days": 14,
    "sweep_stop_after_empty": 3,
    "sweep_spacing_seconds": 0,
    "daily_request_budget": 250,
    "reconcile_days": 21,
    "quiet_start_hour": 23,
    "quiet_end_hour": 7,
    "self_test_every_days": 7,
}


def showtime(type_="available", fmts=("IMAX 70MM",), expired=False,
             time_="6:00p", url=None):
    return {"date": time_, "type": type_, "expired": expired,
            "ticketingJumpPageURL": url,
            "filmFormat": [{"filterName": f} for f in fmts]}


def viewmodel(showtimes, title="The Odyssey (2026)"):
    return {"date": "2026-08-20", "movies": [{
        "title": title,
        "variants": [{"filmFormatHeader": "Premium Format",
                      "amenityGroups": [{"showtimes": list(showtimes)}]}]}]}


class FakeResp:
    status = 200

    def __init__(self, body=b"{}"):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SentryTest(unittest.TestCase):
    def setUp(self):
        self._cfg = sentry.CFG
        self._log = sentry.LOG_PATH
        self._hist = sentry.HISTORY_PATH
        sentry.CFG = copy.deepcopy(TEST_CFG)
        fd, sentry.LOG_PATH = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        # fetch_day records history as a side effect, and the fixtures use a
        # real-looking venue/date — without this redirect the suite appends
        # fabricated rows to the PRODUCTION history.jsonl (it did: 16 of them)
        fd, sentry.HISTORY_PATH = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.st = sentry.default_state()

    def tearDown(self):
        os.unlink(sentry.LOG_PATH)
        for p in (sentry.HISTORY_PATH, sentry.HISTORY_PATH + ".1"):
            if os.path.exists(p):
                os.unlink(p)
        sentry.CFG = self._cfg
        sentry.LOG_PATH = self._log
        sentry.HISTORY_PATH = self._hist


class TestDetection(SentryTest):
    def test_counts_imax_70mm_and_plain_70mm(self):
        vm = viewmodel([showtime(fmts=("IMAX 70MM", "IMAX")),
                        showtime(fmts=("70MM",))])
        self.assertEqual(len(sentry.odyssey_70mm(vm)), 2)

    def test_excludes_past_expired_and_non_70mm(self):
        vm = viewmodel([showtime(type_="pastshowtime"),
                        showtime(expired=True),
                        showtime(fmts=("Dolby Cinema",)),
                        showtime(fmts=())])
        self.assertEqual(sentry.odyssey_70mm(vm), [])

    def test_excludes_other_movies(self):
        vm = viewmodel([showtime()], title="Some Other Film")
        self.assertEqual(sentry.odyssey_70mm(vm), [])

    def test_soldout_and_restricted_detected_with_types(self):
        vm = viewmodel([showtime(), showtime(type_="soldout"),
                        showtime(type_="restricted")])
        hits = sentry.odyssey_70mm(vm)
        self.assertEqual(len(hits), 3)
        self.assertEqual(
            sorted(h["type"] for h in hits),
            ["available", "restricted", "soldout"])


class TestHandleDrop(SentryTest):
    FOUND = {"2026-08-20": {"total": 4, "open": 3},
             "2026-08-21": {"total": 4, "open": 4}}

    def test_delivered_push_advances_horizon_and_records(self):
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.handle_drop(self.st, "AABQI", dict(self.FOUND))
        self.assertEqual(self.st["horizon"]["AABQI"], "2026-08-21")
        self.assertEqual(self.st["alerted"]["AABQI"],
                         ["2026-08-20", "2026-08-21"])
        self.assertEqual(self.st["dropLog"][0]["openSeats"], 7)
        body = p.call_args.args[1]
        self.assertIn("8 showtimes", body)
        self.assertIn("7 with open seats, 1 sold out", body)

    def test_failed_push_leaves_state_untouched_for_retry(self):
        with mock.patch.object(sentry, "push", return_value=False):
            sentry.handle_drop(self.st, "AABQI", dict(self.FOUND))
        self.assertEqual(self.st["horizon"]["AABQI"], "2026-08-19")
        self.assertEqual(self.st["alerted"]["AABQI"], [])
        self.assertEqual(self.st["dropLog"], [])

    def test_dates_at_or_before_horizon_or_already_alerted_ignored(self):
        self.st["alerted"]["AABQI"] = ["2026-08-21"]
        found = {"2026-08-19": {"total": 2, "open": 2},  # == horizon
                 "2026-08-21": {"total": 2, "open": 2}}  # already alerted
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.handle_drop(self.st, "AABQI", found)
        p.assert_not_called()

    def test_all_open_message_omits_sold_out_clause(self):
        found = {"2026-08-20": {"total": 3, "open": 3}}
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.handle_drop(self.st, "AABQI", found)
        body = p.call_args.args[1]
        self.assertIn("3 with open seats", body)
        self.assertNotIn("sold out", body)


class TestScanDates(SentryTest):
    def test_stops_after_consecutive_empties_and_counts_open(self):
        data = {"d1": [showtime(), showtime(type_="soldout")],
                "d2": [], "d3": [], "d4": [],
                "d5": [showtime()]}  # never reached: 3 empties before it
        with mock.patch.object(
                sentry, "fetch_day",
                side_effect=lambda st, v, d: viewmodel(data[d])):
            found = sentry.scan_dates(self.st, "AABQI", list(data))
        self.assertEqual(found, {"d1": {"total": 2, "open": 1,
                                        "times": ["6:00p"]}})

    def test_fetch_failure_returns_partial_and_trips_breaker(self):
        seq = {"d1": viewmodel([showtime()]), "d2": None}
        with mock.patch.object(sentry, "fetch_day",
                               side_effect=lambda st, v, d: seq[d]):
            found = sentry.scan_dates(self.st, "AABQI", ["d1", "d2", "d3"])
        self.assertEqual(list(found), ["d1"])
        self.assertEqual(self.st["breaker"]["failures"], 1)


class TestBudget(SentryTest):
    def test_rollover_resets_daily_spend(self):
        self.st["budget"] = {"date": "2001-01-01", "spent": 999}
        self.assertTrue(sentry.budget_ok(self.st))
        self.assertEqual(self.st["budget"]["spent"], 0)

    def test_exhausted_budget_blocks_fetch_without_http(self):
        sentry.budget_ok(self.st)  # roll to today
        self.st["budget"]["spent"] = sentry.CFG["daily_request_budget"]
        with mock.patch.object(sentry.urllib.request, "urlopen") as u, \
                mock.patch.object(sentry, "push", return_value=True):
            self.assertIs(sentry.fetch_day(self.st, "AABQI", "2026-08-20"),
                          sentry.BUDGET)
        u.assert_not_called()


class TestBreaker(SentryTest):
    def test_opens_after_three_failures_and_recovers(self):
        for _ in range(3):
            sentry.breaker_fail(self.st)
        self.assertEqual(self.st["breaker"]["state"], "open")
        self.assertTrue(sentry.breaker_open(self.st))
        self.st["breaker"]["openUntil"] = 0  # backoff elapsed
        self.assertFalse(sentry.breaker_open(self.st))  # half-open probe
        sentry.breaker_success(self.st)
        self.assertEqual(self.st["breaker"]["state"], "closed")
        self.assertEqual(self.st["breaker"]["failures"], 0)

    def test_degraded_alert_sent_exactly_once_at_six(self):
        with mock.patch.object(sentry, "push", return_value=True) as p:
            for _ in range(8):
                sentry.breaker_fail(self.st)
        self.assertEqual(p.call_count, 1)
        self.assertIn("degraded", p.call_args.args[0].lower()
                      + p.call_args.args[1].lower())

    def test_lost_degraded_alert_retries_on_next_failure(self):
        """On a full cutover this alert is a primary signal — a push lost
        to a channel outage must not mark itself delivered."""
        with mock.patch.object(sentry, "push", return_value=False) as p:
            for _ in range(6):
                sentry.breaker_fail(self.st)
        self.assertEqual(p.call_count, 1)
        self.assertFalse(self.st["breaker"]["degradedSent"])
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.breaker_fail(self.st)          # failure #7 retries
        p.assert_called_once()
        self.assertTrue(self.st["breaker"]["degradedSent"])
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.breaker_fail(self.st)          # delivered — no re-ring
        p.assert_not_called()


class TestNotifier(SentryTest):
    def test_fanout_delivers_when_one_channel_fails(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            calls.append(url)
            if "callmebot" in url:
                raise OSError("relay down")
            return FakeResp()

        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            self.assertTrue(sentry.push("t", "b", priority="urgent"))
        self.assertEqual(len(calls), 2)  # whatsapp attempted, ntfy delivered

    def test_all_channels_fail_returns_false(self):
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=OSError("net down")):
            self.assertFalse(sentry.push("t", "b", priority="low"))

    def test_disabled_channels_are_skipped(self):
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        with mock.patch.object(sentry.urllib.request, "urlopen") as u:
            self.assertFalse(sentry.push("t", "b", priority="low"))
        u.assert_not_called()

    def test_callmebot_url_carries_phone_key_and_urgency_prefix(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req if isinstance(req, str) else req.full_url
            return FakeResp()

        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            self.assertTrue(sentry.push("DROP", "body text",
                                        priority="urgent"))
        self.assertIn("api.callmebot.com/whatsapp.php", seen["url"])
        self.assertIn("phone=%2B15551234567", seen["url"])
        self.assertIn("apikey=SECRET", seen["url"])
        self.assertIn("%F0%9F%9A%A8", seen["url"])  # 🚨 urgency prefix

    def test_ntfy_json_payload_utf8_title_and_priority(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["payload"] = json.loads(req.data.decode())
            return FakeResp()

        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            self.assertTrue(sentry.push("70mm DROP — Lincoln Square", "b",
                                        priority="urgent"))
        self.assertEqual(seen["payload"]["topic"], "test-topic")
        self.assertEqual(seen["payload"]["priority"], 5)
        self.assertIn("—", seen["payload"]["title"])  # the latin-1 bug guard

    def test_telegram_payload_rings_for_drops_silent_for_notes(self):
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        sentry.CFG["channels"]["telegram"]["enabled"] = True
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append((req.full_url, json.loads(req.data.decode())))
            return FakeResp(b'{"ok": true}')

        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            self.assertTrue(sentry.push("DROP", "b", priority="urgent"))
            self.assertTrue(sentry.push("note", "b", priority="low"))
        url, drop = seen[0]
        self.assertIn("api.telegram.org/botBOT_TOKEN/sendMessage", url)
        self.assertEqual(drop["chat_id"], "424242")
        self.assertFalse(drop["disable_notification"])  # drops ring
        self.assertTrue(seen[1][1]["disable_notification"])  # notes silent
        self.assertTrue(drop["text"].startswith("🚨"))

    def test_telegram_broadcasts_to_extra_chats_best_effort(self):
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        sentry.CFG["channels"]["telegram"]["enabled"] = True
        sentry.CFG["channels"]["telegram"]["extra_chat_ids"] = \
            ["@odyssey70mm", "-100555"]
        seen = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            seen.append(body["chat_id"])
            if body["chat_id"] == "@odyssey70mm":
                return FakeResp(b'{"ok": false}')  # one subscriber feed down
            return FakeResp(b'{"ok": true}')

        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            self.assertTrue(sentry.push("DROP", "b", priority="urgent",
                                        audience="all"))
        # primary first, then every extra despite the middle one failing
        self.assertEqual(seen, ["424242", "@odyssey70mm", "-100555"])

    def test_telegram_primary_failure_still_fails_channel(self):
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        sentry.CFG["channels"]["telegram"]["enabled"] = True
        sentry.CFG["channels"]["telegram"]["extra_chat_ids"] = ["-100555"]
        with mock.patch.object(
                sentry.urllib.request, "urlopen",
                return_value=FakeResp(b'{"ok": false}')):
            self.assertFalse(sentry.push("t", "b", priority="urgent"))

    def test_telegram_api_level_error_counts_as_channel_failure(self):
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        sentry.CFG["channels"]["telegram"]["enabled"] = True
        with mock.patch.object(
                sentry.urllib.request, "urlopen",
                return_value=FakeResp(b'{"ok": false, "error_code": 400}')):
            self.assertFalse(sentry.push("t", "b", priority="urgent"))

    def test_telegram_setup_discovers_chat_id_and_enables(self):
        updates = {"result": [  # same person twice: unambiguous, auto-binds
            {"message": {"chat": {"id": 987654, "first_name": "Brian"}}},
            {"message": {"chat": {"id": 987654, "first_name": "Brian"}}}]}
        writes = {}

        def fake_urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if "getUpdates" in url:
                return FakeResp(json.dumps(updates).encode())
            writes["send"] = json.loads(req.data.decode())
            return FakeResp(b'{"ok": true}')

        fd, tmpcfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        old_path = sentry.CONFIG_PATH
        sentry.CONFIG_PATH = tmpcfg
        try:
            with mock.patch.object(sentry.urllib.request, "urlopen",
                                   side_effect=fake_urlopen):
                self.assertEqual(sentry.telegram_setup(), 0)
            ch = sentry.CFG["channels"]["telegram"]
            self.assertEqual(ch["chat_id"], "987654")  # newest chat wins
            self.assertTrue(ch["enabled"])
            saved = json.load(open(tmpcfg))
            self.assertEqual(
                saved["channels"]["telegram"]["chat_id"], "987654")
            self.assertIn("send", writes)  # confirmation message sent
        finally:
            sentry.CONFIG_PATH = old_path
            os.unlink(tmpcfg)

    def test_meta_provider_uses_template_payload(self):
        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        wa = sentry.CFG["channels"]["whatsapp"]
        wa["provider"] = "meta"
        wa["meta"] = {"token": "TOK", "phone_number_id": "12345",
                      "to": "15551234567", "template": "drop_alert"}
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            seen["payload"] = json.loads(req.data.decode())
            return FakeResp()

        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            self.assertTrue(sentry.push("t", "b", priority="urgent"))
        self.assertIn("graph.facebook.com", seen["url"])
        self.assertIn("/12345/messages", seen["url"])
        self.assertEqual(seen["auth"], "Bearer TOK")
        self.assertEqual(seen["payload"]["template"]["name"], "drop_alert")


class TestInstantAlert(SentryTest):
    def test_rings_once_per_probe_date(self):
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.maybe_instant_alert(self.st, "AABQI", "2026-08-20")
            sentry.maybe_instant_alert(self.st, "AABQI", "2026-08-20")
        self.assertEqual(p.call_count, 1)

    def test_failed_ring_retries_next_cycle(self):
        with mock.patch.object(sentry, "push", return_value=False):
            sentry.maybe_instant_alert(self.st, "AABQI", "2026-08-20")
        self.assertNotIn("AABQI", self.st["instantAlerted"])
        with mock.patch.object(sentry, "push", return_value=True):
            sentry.maybe_instant_alert(self.st, "AABQI", "2026-08-20")
        self.assertEqual(self.st["instantAlerted"]["AABQI"], "2026-08-20")


class TestRestock(SentryTest):
    def run_check(self, hits):
        with mock.patch.object(sentry, "fetch_day",
                               return_value=viewmodel(hits)), \
             mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.restock_check(self.st, "AABQI", ["2026-08-15"])
        return p

    def test_first_observation_baselines_silently(self):
        p = self.run_check([showtime(type_="soldout", time_="6:00p")])
        p.assert_not_called()
        self.assertEqual(
            self.st["seenAvailability"]["AABQI|2026-08-15|6:00p"], "soldout")

    def test_soldout_to_available_alerts(self):
        self.st["seenAvailability"]["AABQI|2026-08-15|6:00p"] = "soldout"
        p = self.run_check([showtime(type_="available", time_="6:00p",
                                     url="https://tickets.example/jump?y=2")])
        p.assert_called_once()
        self.assertIn("Seats just opened", p.call_args.args[0])
        self.assertIn("Sat, Aug 15, 6:00 PM", p.call_args.args[1])
        self.assertIn("https://tickets.example/jump?y=2",
                      p.call_args.args[1])

    def test_available_to_soldout_updates_without_alert(self):
        self.st["seenAvailability"]["AABQI|2026-08-15|6:00p"] = "available"
        p = self.run_check([showtime(type_="soldout", time_="6:00p")])
        p.assert_not_called()
        self.assertEqual(
            self.st["seenAvailability"]["AABQI|2026-08-15|6:00p"], "soldout")


class TestRestockWatchlist(SentryTest):
    def test_merges_config_and_auto_and_prunes_past(self):
        self.st["autoRestockDates"] = ["2026-08-01", "2026-08-20"]
        wl = sentry.restock_watchlist(self.st, today="2026-08-10")
        self.assertEqual(wl, ["2026-08-15", "2026-08-20"])

    def test_soldout_dates_flags_partial_and_full_sellouts(self):
        found = {"2026-08-01": {"total": 5, "open": 5},
                 "2026-08-02": {"total": 5, "open": 3},
                 "2026-08-03": {"total": 2, "open": 0}}
        self.assertEqual(sentry.soldout_dates(found),
                         ["2026-08-02", "2026-08-03"])

    def test_due_dates_use_burst_clock_near_show_relaxed_far_out(self):
        now = dt.datetime(2026, 8, 14, 12, 0)
        sentry.CFG["restock_watch_dates"] = ["2026-08-15", "2026-08-19"]
        # never polled: both due
        self.assertEqual(sentry.restock_due_dates(self.st, now),
                         ["2026-08-15", "2026-08-19"])
        # polled 90s ago: tomorrow's date (60s clock) due again, the far
        # date (300s clock) not yet
        stamp = (now - dt.timedelta(seconds=90)).isoformat()
        self.st["restockLastPoll"] = {"2026-08-15": stamp,
                                      "2026-08-19": stamp}
        self.assertEqual(sentry.restock_due_dates(self.st, now),
                         ["2026-08-15"])

    def test_due_dates_drops_past_dates(self):
        now = dt.datetime(2026, 8, 16, 12, 0)
        sentry.CFG["restock_watch_dates"] = ["2026-08-15"]
        self.assertEqual(sentry.restock_due_dates(self.st, now), [])


class TestSlotFilter(SentryTest):
    def test_slot_hour_parses_fandango_labels(self):
        self.assertEqual(sentry.slot_hour("6:00p"), 18.0)
        self.assertEqual(sentry.slot_hour("12:30a"), 0.5)
        self.assertEqual(sentry.slot_hour("12:00p"), 12.0)
        self.assertAlmostEqual(sentry.slot_hour("6:05a"), 6 + 5 / 60)
        self.assertIsNone(sentry.slot_hour("matinee"))

    def test_everything_qualifies_outside_target_mode(self):
        self.assertTrue(sentry.slot_qualifies("10:00a"))

    def test_target_mode_keeps_evening_and_late_night_only(self):
        sentry.CFG["target_dates"] = ["2026-08-21"]
        self.assertTrue(sentry.slot_qualifies("6:00p"))
        self.assertTrue(sentry.slot_qualifies("11:00p"))
        self.assertTrue(sentry.slot_qualifies("2:00a"))
        self.assertFalse(sentry.slot_qualifies("10:00a"))
        self.assertFalse(sentry.slot_qualifies("5:00p"))
        # unparseable labels pass rather than silently muting alerts
        self.assertTrue(sentry.slot_qualifies("???"))


class TestTargetCheck(SentryTest):
    NOW = dt.datetime(2026, 8, 1, 12, 0)

    def setUp(self):
        super().setUp()
        sentry.CFG["target_dates"] = ["2026-08-21"]

    def run_target(self, hits, push_ok=True):
        with mock.patch.object(sentry, "fetch_day",
                               return_value=viewmodel(hits)) as f, \
             mock.patch.object(sentry, "push", return_value=push_ok) as p:
            sentry.target_check(self.st, self.NOW)
        return f, p

    def test_unlisted_date_stays_quiet(self):
        f, p = self.run_target([])
        f.assert_called_once()
        p.assert_not_called()
        self.assertEqual(self.st["targetAlerted"], [])

    def test_matinee_only_listing_does_not_ring(self):
        f, p = self.run_target([showtime(time_="10:00a")])
        p.assert_not_called()

    def test_evening_listing_rings_and_dedupes(self):
        f, p = self.run_target([showtime(time_="7:00p")])
        p.assert_called_once()
        self.assertIn("Fri, Aug 21", p.call_args.args[0])
        self.assertIn("7:00 PM", p.call_args.args[1])
        self.assertEqual(self.st["targetAlerted"], ["2026-08-21"])
        # already alerted → no further fetches
        f2, p2 = self.run_target([showtime(time_="7:00p")])
        f2.assert_not_called()

    def test_alert_includes_amc_cross_check_link(self):
        sentry.CFG["venues"]["AABQI"]["amc_link"] = \
            "https://amc.example/showtimes?date={date}"
        f, p = self.run_target([showtime(time_="7:00p")])
        self.assertIn("https://amc.example/showtimes?date=2026-08-21",
                      p.call_args.args[1])

    def test_open_slot_alert_carries_showtime_deep_link(self):
        f, p = self.run_target(
            [showtime(time_="7:00p", url="https://tickets.example/jump?x=1")])
        self.assertIn("7:00 PM", p.call_args.args[1])
        self.assertIn("https://tickets.example/jump?x=1",
                      p.call_args.args[1])

    def test_missing_deep_link_falls_back_to_venue_page(self):
        f, p = self.run_target([showtime(time_="7:00p")])
        self.assertIn("https://link.example/ls", p.call_args.args[1])

    def test_soldout_evening_rings_and_arms_refund_sniper(self):
        f, p = self.run_target([showtime(time_="7:00p", type_="soldout")])
        p.assert_called_once()
        self.assertEqual(self.st["autoRestockDates"], ["2026-08-21"])

    def test_poll_clock_throttles_refetch(self):
        self.run_target([])
        with mock.patch.object(sentry, "fetch_day") as f:
            sentry.target_check(
                self.st, self.NOW + dt.timedelta(seconds=60))
        f.assert_not_called()

    def test_failed_push_retries_next_pass(self):
        f, p = self.run_target([showtime(time_="7:00p")], push_ok=False)
        self.assertEqual(self.st["targetAlerted"], [])
        self.st["targetLastPoll"] = {}
        f2, p2 = self.run_target([showtime(time_="7:00p")])
        self.assertEqual(self.st["targetAlerted"], ["2026-08-21"])


class TestCadence(SentryTest):
    def test_base_interval_outside_windows(self):
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 23, 15, 0)), 60)

    def test_overnight_stretches_to_hourly(self):
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 23, 23, 30)), 3600)
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 24, 3, 0)), 3600)

    def test_overnight_interval_is_configurable(self):
        sentry.CFG["overnight_interval_seconds"] = 30
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 24, 3, 0)), 30)

    def test_burst_window_tightens_interval(self):
        sentry.CFG["burst_windows"] = [
            {"days": ["Thu"], "start_hour": 9, "end_hour": 12}]
        # 2026-07-23 is a Thursday
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 23, 10, 0)), 20)
        # end hour is exclusive
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 23, 12, 0)), 60)
        # wrong day
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 24, 10, 0)), 60)

    def test_burst_overrides_overnight(self):
        sentry.CFG["burst_windows"] = [
            {"days": ["Thu"], "start_hour": 23, "end_hour": 24}]
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 23, 23, 30)), 20)


class TestAnnounceOnce(SentryTest):
    """One alert per date, whichever path reaches it first."""

    def test_target_alert_suppresses_later_drop_alert(self):
        sentry.CFG["target_dates"] = ["2026-08-21"]
        with mock.patch.object(sentry, "fetch_day",
                               return_value=viewmodel([showtime(
                                   time_="7:00p")])), \
             mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.target_check(self.st, dt.datetime(2026, 8, 1, 12, 0))
        self.assertEqual(p.call_count, 1)
        self.assertIn("2026-08-21", self.st["announced"])
        # the sweep later covers the same date — must stay silent
        with mock.patch.object(sentry, "push", return_value=True) as p2:
            sentry.handle_drop(self.st, "AABQI",
                               {"2026-08-21": {"total": 3, "open": 3}})
        p2.assert_not_called()

    def test_horizon_still_advances_past_suppressed_dates(self):
        """Otherwise the probe re-hits that date forever."""
        self.st["announced"] = ["2026-08-21"]
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.handle_drop(self.st, "AABQI",
                               {"2026-08-21": {"total": 3, "open": 3}})
        p.assert_not_called()
        self.assertEqual(self.st["horizon"]["AABQI"], "2026-08-21")

    def test_drop_alert_announces_every_date_it_covered(self):
        with mock.patch.object(sentry, "push", return_value=True):
            sentry.handle_drop(self.st, "AABQI",
                               {"2026-08-20": {"total": 2, "open": 2},
                                "2026-08-21": {"total": 3, "open": 1}})
        self.assertEqual(self.st["announced"], ["2026-08-20", "2026-08-21"])

    def test_failed_drop_push_leaves_dates_unannounced(self):
        with mock.patch.object(sentry, "push", return_value=False):
            sentry.handle_drop(self.st, "AABQI",
                               {"2026-08-20": {"total": 2, "open": 2}})
        self.assertEqual(self.st["announced"], [])

    def test_instant_ping_suppressed_for_announced_date(self):
        self.st["announced"] = ["2026-08-20"]
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.maybe_instant_alert(self.st, "AABQI", "2026-08-20")
        p.assert_not_called()

    def test_instant_ping_can_be_disabled(self):
        sentry.CFG["instant_alert"] = False
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.maybe_instant_alert(self.st, "AABQI", "2026-08-20")
        p.assert_not_called()

    def test_restock_rings_once_per_showtime(self):
        sentry.CFG["restock_watch_dates"] = ["2026-08-15"]
        self.st["seenAvailability"]["AABQI|2026-08-15|6:00p"] = "soldout"
        with mock.patch.object(sentry, "fetch_day",
                               return_value=viewmodel([showtime(
                                   type_="available", time_="6:00p")])), \
             mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.restock_check(self.st, "AABQI", ["2026-08-15"])
        p.assert_called_once()
        # seat flaps sold-out then open again — no second ring
        self.st["seenAvailability"]["AABQI|2026-08-15|6:00p"] = "soldout"
        with mock.patch.object(sentry, "fetch_day",
                               return_value=viewmodel([showtime(
                                   type_="available", time_="6:00p")])), \
             mock.patch.object(sentry, "push", return_value=True) as p2:
            sentry.restock_check(self.st, "AABQI", ["2026-08-15"])
        p2.assert_not_called()


class TestScheduleTimezone(SentryTest):
    def test_windows_follow_theater_clock_not_machine(self):
        sentry.CFG["schedule_tz"] = "America/New_York"
        sentry.CFG["burst_windows"] = [
            {"days": ["Wed"], "start_hour": 12, "end_hour": 18}]
        pacific = zoneinfo.ZoneInfo("America/Los_Angeles")
        # 10:00 Wed Pacific == 13:00 Wed Eastern -> inside the ET window
        self.assertEqual(sentry.poll_interval(
            dt.datetime(2026, 7, 29, 10, 0, tzinfo=pacific)), 20)
        # 06:00 Wed Pacific == 09:00 Wed Eastern -> outside it
        self.assertEqual(sentry.poll_interval(
            dt.datetime(2026, 7, 29, 6, 0, tzinfo=pacific)), 60)

    def test_quiet_hours_use_theater_clock(self):
        sentry.CFG["schedule_tz"] = "America/New_York"
        pacific = zoneinfo.ZoneInfo("America/Los_Angeles")
        # 21:00 Pacific == 00:00 Eastern -> overnight tier
        self.assertEqual(sentry.poll_interval(
            dt.datetime(2026, 7, 29, 21, 0, tzinfo=pacific)), 3600)

    def test_bad_timezone_falls_back_to_local(self):
        sentry.CFG["schedule_tz"] = "Not/AZone"
        self.assertEqual(
            sentry.poll_interval(dt.datetime(2026, 7, 29, 15, 0)), 60)


class TestAudienceRouting(SentryTest):
    """Friends subscribed to hear about tickets, not about launchd."""

    def setUp(self):
        super().setUp()
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        tg = sentry.CFG["channels"]["telegram"]
        tg["enabled"] = True
        tg["extra_chat_ids"] = ["-100999"]

    def recipients(self, **push_kw):
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(json.loads(req.data.decode())["chat_id"])
            return FakeResp(b'{"ok": true}')

        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            sentry.push("t", "b", **push_kw)
        return seen

    def test_default_is_private_so_new_alerts_cannot_leak(self):
        self.assertEqual(self.recipients(), ["424242"])

    def test_operational_alert_never_reaches_the_channel(self):
        self.assertEqual(self.recipients(audience="private"), ["424242"])

    def test_ticket_alert_reaches_you_and_the_channel(self):
        self.assertEqual(self.recipients(audience="all"),
                         ["424242", "-100999"])

    def test_watchdog_alerts_are_private(self):
        import watchdog
        src = open(watchdog.__file__).read()
        # every push in the watchdog must stay out of the subscriber channel
        self.assertNotIn('audience="all"', src)

    def test_startup_check_alerts_are_private(self):
        import startup_check
        self.assertNotIn('audience="all"', open(startup_check.__file__).read())


class TestFriendlyCopy(SentryTest):
    def test_dates_and_times_are_human_readable(self):
        self.assertEqual(sentry.pretty_date("2026-08-21"), "Fri, Aug 21")
        self.assertEqual(sentry.pretty_time("7:00p"), "7:00 PM")
        self.assertEqual(sentry.pretty_time("11:00a"), "11:00 AM")
        self.assertEqual(sentry.pretty_date("garbage"), "garbage")
        self.assertEqual(sentry.pretty_time("garbage"), "garbage")

    def test_target_alert_has_no_iso_dates_or_jargon(self):
        sentry.CFG["target_dates"] = ["2026-08-21"]
        with mock.patch.object(sentry, "fetch_day",
                               return_value=viewmodel([
                                   showtime(time_="7:00p",
                                            url="https://tix.example/x")])), \
             mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.target_check(self.st, dt.datetime(2026, 8, 1, 12, 0))
        title, body = p.call_args.args[0], p.call_args.args[1]
        self.assertIn("Fri, Aug 21", title)
        self.assertIn("7:00 PM", body)
        self.assertNotIn("2026-08-21", body)
        self.assertNotIn("TARGET LIVE", title)
        self.assertEqual(p.call_args.kwargs["audience"], "all")

    def test_drop_alert_is_broadcast_and_readable(self):
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.handle_drop(self.st, "AABQI",
                               {"2026-08-20": {"total": 4, "open": 4}})
        self.assertEqual(p.call_args.kwargs["audience"], "all")
        self.assertNotIn("unconfirmed · per Fandango", p.call_args.args[1])


class TestEnvOverrides(SentryTest):
    """Secrets come from the orchestrator on a VPS, not from a file on disk."""

    def apply(self, **env):
        base = {"channels": {"telegram": {"enabled": False, "token": "FILE",
                                          "chat_id": "1"},
                             "ntfy": {"enabled": False, "topic": "file-topic"}},
                "healthchecks_url": "", "target_dates": []}
        with mock.patch.dict(os.environ, env, clear=False):
            return sentry.apply_env(copy.deepcopy(base))

    def test_env_token_wins_and_enables_channel(self):
        cfg = self.apply(TELEGRAM_TOKEN="ENVTOKEN", TELEGRAM_CHAT_ID="999")
        self.assertEqual(cfg["channels"]["telegram"]["token"], "ENVTOKEN")
        self.assertEqual(cfg["channels"]["telegram"]["chat_id"], "999")
        self.assertTrue(cfg["channels"]["telegram"]["enabled"])

    def test_config_file_used_when_env_absent(self):
        cfg = self.apply()
        self.assertEqual(cfg["channels"]["telegram"]["token"], "FILE")
        self.assertFalse(cfg["channels"]["telegram"]["enabled"])

    def test_comma_lists_are_split_and_trimmed(self):
        cfg = self.apply(TELEGRAM_EXTRA_CHAT_IDS=" -100123 , @chan ",
                         TARGET_DATES="2026-08-21,2026-08-22")
        self.assertEqual(cfg["channels"]["telegram"]["extra_chat_ids"],
                         ["-100123", "@chan"])
        self.assertEqual(cfg["target_dates"],
                         ["2026-08-21", "2026-08-22"])

    def test_ntfy_topic_from_env_enables_with_default_server(self):
        cfg = self.apply(NTFY_TOPIC="envtopic")
        self.assertEqual(cfg["channels"]["ntfy"]["topic"], "envtopic")
        self.assertEqual(cfg["channels"]["ntfy"]["server"], "https://ntfy.sh")
        self.assertTrue(cfg["channels"]["ntfy"]["enabled"])

    def test_healthchecks_and_tz_from_env(self):
        cfg = self.apply(HEALTHCHECKS_URL="https://hc.example/ping",
                         SCHEDULE_TZ="America/Chicago")
        self.assertEqual(cfg["healthchecks_url"], "https://hc.example/ping")
        self.assertEqual(cfg["schedule_tz"], "America/Chicago")


class TestUnconfiguredChannelFails(SentryTest):
    """push() returning True advances the horizon — a channel that is not
    actually configured must never contribute to that."""

    def test_blank_ntfy_topic_raises_instead_of_reporting_success(self):
        sentry.CFG["channels"]["ntfy"]["topic"] = ""
        with self.assertRaises(RuntimeError):
            sentry.send_ntfy(sentry.CFG["channels"]["ntfy"], "t", "b", "urgent")

    def test_push_is_false_when_every_channel_is_unconfigured(self):
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["telegram"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["topic"] = ""
        with mock.patch.object(sentry.urllib.request, "urlopen") as u:
            self.assertFalse(sentry.push("DROP", "body", priority="urgent"))
        u.assert_not_called()

    def test_horizon_holds_when_the_only_channel_is_misconfigured(self):
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["telegram"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["topic"] = ""
        with mock.patch.object(sentry.urllib.request, "urlopen"):
            sentry.handle_drop(self.st, "AABQI",
                               {"2026-08-20": {"total": 2, "open": 2}})
        self.assertEqual(self.st["horizon"]["AABQI"], "2026-08-19")
        self.assertEqual(self.st["announced"], [])


class TestBlockDetection(SentryTest):
    def test_degraded_alert_names_ip_blocking_on_403(self):
        self.st["lastFetch"] = {"ok": False, "error": "HTTP Error 403: Forbidden"}
        self.st["breaker"]["failures"] = 5
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.breaker_fail(self.st)
        self.assertIn("IP-level blocking", p.call_args.args[1])
        self.assertIn("403", p.call_args.args[1])

    def test_generic_failure_does_not_blame_the_ip(self):
        self.st["lastFetch"] = {"ok": False, "error": "timed out"}
        self.st["breaker"]["failures"] = 5
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.breaker_fail(self.st)
        self.assertNotIn("IP-level blocking", p.call_args.args[1])


class TestSingleFlight(SentryTest):
    def setUp(self):
        super().setUp()
        self._lock, self._state = sentry.LOCK_PATH, sentry.STATE_PATH
        self.tmp = tempfile.mkdtemp()
        sentry.LOCK_PATH = os.path.join(self.tmp, ".lock")
        sentry.STATE_PATH = os.path.join(self.tmp, "state.json")
        sentry._LOCK_FH = None

    def tearDown(self):
        if sentry._LOCK_FH:
            sentry._LOCK_FH.close()
            sentry._LOCK_FH = None
        sentry.LOCK_PATH, sentry.STATE_PATH = self._lock, self._state
        super().tearDown()

    def test_second_holder_is_refused(self):
        self.assertTrue(sentry.acquire_lock())
        first = sentry._LOCK_FH
        sentry._LOCK_FH = None
        self.assertFalse(sentry.acquire_lock())  # same process, 2nd handle
        first.close()

    def test_lock_frees_when_holder_closes(self):
        self.assertTrue(sentry.acquire_lock())
        sentry._LOCK_FH.close()
        sentry._LOCK_FH = None
        self.assertTrue(sentry.acquire_lock())

    def test_overlapping_run_exits_without_touching_state(self):
        """A cycle that starts mid-sweep must not fetch, alert, or save."""
        self.assertTrue(sentry.acquire_lock())  # stand in for the sweeper
        held = sentry._LOCK_FH
        sentry._LOCK_FH = None
        with mock.patch.object(sentry, "fetch_day") as f, \
             mock.patch.object(sentry, "push") as p, \
             mock.patch.object(sentry, "save_state") as s:
            sentry.main()
        f.assert_not_called()
        p.assert_not_called()
        s.assert_not_called()
        held.close()

    def test_cycle_claims_slot_on_disk_before_long_work(self):
        """lastPollAt must hit disk before the sweep, or the next launchd
        fire reads a stale timestamp and starts a duplicate cycle."""
        saves = []

        def fake_scan(st, venue, dates):
            saves.append(json.load(open(sentry.STATE_PATH))["lastPollAt"])
            return {}

        with mock.patch.object(sentry, "fetch_day",
                               return_value=viewmodel([showtime()])), \
             mock.patch.object(sentry, "scan_dates", side_effect=fake_scan), \
             mock.patch.object(sentry, "push", return_value=True), \
             mock.patch.object(sentry, "target_check"):
            sentry.main()
        self.assertTrue(saves, "sweep never ran")
        self.assertIsNotNone(saves[0], "lastPollAt was not persisted pre-sweep")


class TestResponseTelemetry(SentryTest):
    def fetch(self, payload):
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               return_value=FakeResp(payload)), \
             mock.patch.object(sentry, "push", return_value=True) as p:
            vm = sentry.fetch_day(self.st, "AABQI", "2026-08-20")
        return vm, p

    def good(self):
        return json.dumps({"viewModel": viewmodel([showtime()])}).encode()

    def empty(self):
        return json.dumps({"viewModel": {"movies": []}}).encode()

    def test_successful_fetch_records_response_stats(self):
        body = self.good()
        self.fetch(body)
        lf = self.st["lastFetch"]
        self.assertTrue(lf["ok"])
        self.assertEqual(lf["status"], 200)
        self.assertEqual(lf["bytes"], len(body))
        self.assertEqual(lf["movies"], 1)
        self.assertEqual(lf["hits"], 1)

    def test_failed_fetch_records_the_error(self):
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=OSError("connection reset")):
            self.assertIsNone(sentry.fetch_day(self.st, "AABQI", "2026-08-20"))
        lf = self.st["lastFetch"]
        self.assertFalse(lf["ok"])
        self.assertIn("connection reset", lf["error"])

    def test_empty_payload_increments_blind_streak(self):
        self.fetch(self.empty())
        self.assertEqual(self.st["blindStreak"], 1)

    def test_good_payload_resets_blind_streak(self):
        self.st["blindStreak"] = 7
        self.fetch(self.good())
        self.assertEqual(self.st["blindStreak"], 0)

    def test_blind_streak_alerts_once_at_threshold(self):
        for _ in range(2):
            _, p = self.fetch(self.empty())
            p.assert_not_called()          # below threshold of 3
        _, p = self.fetch(self.empty())    # third empty response
        p.assert_called_once()
        self.assertEqual(p.call_args.args[0], "SENTRY BLIND")
        _, p = self.fetch(self.empty())    # already alerted; stays quiet
        p.assert_not_called()

    def test_blind_alert_retries_when_push_fails(self):
        self.st["blindStreak"] = 5
        with mock.patch.object(sentry, "push", return_value=False):
            sentry.maybe_blind_alert(self.st)
        self.assertNotIn("blindAlerted", self.st)


class TestFetchDay(SentryTest):
    def test_sends_referer_and_parses_viewmodel(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["referer"] = req.headers.get("Referer")
            return FakeResp(json.dumps(
                {"viewModel": viewmodel([showtime()])}).encode())

        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            vm = sentry.fetch_day(self.st, "AABQI", "2026-08-20")
        self.assertEqual(seen["referer"], "https://ref.example/ls")
        self.assertEqual(len(sentry.odyssey_70mm(vm)), 1)
        self.assertEqual(self.st["budget"]["spent"], 1)

    def test_http_error_returns_none(self):
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=OSError("403")):
            self.assertIsNone(sentry.fetch_day(self.st, "AABQI", "2026-08-20"))


class TestInstanceName(SentryTest):
    """Ops alerts name the box they came from; ticket alerts stay clean.
    Applied inside push() so no call site can get it wrong."""

    def setUp(self):
        super().setUp()
        sentry.CFG["channels"]["whatsapp"]["enabled"] = False
        sentry.CFG["channels"]["ntfy"]["enabled"] = False
        tg = sentry.CFG["channels"]["telegram"]
        tg["enabled"] = True
        tg["extra_chat_ids"] = ["-100999"]
        sentry.CFG["instance_name"] = "vps-nyc3"

    def sent(self, **push_kw):
        seen = []

        def fake_urlopen(req, timeout=None):
            p = json.loads(req.data.decode())
            seen.append((p["chat_id"], p["text"]))
            return FakeResp(b'{"ok": true}')

        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            sentry.push("Sentry degraded", "body", **push_kw)
        return seen

    def test_ops_alert_title_names_the_instance(self):
        (chat, text), = self.sent()
        self.assertEqual(chat, "424242")
        self.assertIn("Sentry degraded [vps-nyc3]", text)

    def test_ticket_alert_stays_clean_for_you_and_the_channel(self):
        for chat, text in self.sent(audience="all"):
            self.assertNotIn("vps-nyc3", text)

    def test_unnamed_instance_leaves_titles_untouched(self):
        sentry.CFG["instance_name"] = ""
        (chat, text), = self.sent()
        self.assertNotIn("[", text.split("\n")[0])

    def test_env_beats_config_beats_hostname(self):
        base = {"channels": {}, "instance_name": "from-config"}
        with mock.patch.dict(os.environ, {"INSTANCE_NAME": "from-env"}):
            self.assertEqual(sentry.apply_env(copy.deepcopy(base))
                             ["instance_name"], "from-env")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INSTANCE_NAME", None)
            self.assertEqual(sentry.apply_env(copy.deepcopy(base))
                             ["instance_name"], "from-config")
            with mock.patch.object(sentry.socket, "gethostname",
                                   return_value="the-mac.local"):
                self.assertEqual(sentry.apply_env({"channels": {}})
                                 ["instance_name"], "the-mac.local")


class TestPreflight(SentryTest):
    """The cutover gate: refusals, blind payloads, zero hits, and a cadence
    that cannot fit the daily budget must all block the deploy."""

    def run_preflight(self, lastfetches):
        """Feed fetch_day a scripted sequence; return (exit_code, output)."""
        seq = iter(lastfetches)

        def fake_fetch(st, venue, date):
            lf = dict(next(seq), venue=venue, date=date)
            st["lastFetch"] = lf
            return {"movies": ["m"]} if lf.get("ok") else None

        out = io.StringIO()
        with mock.patch.object(sentry, "fetch_day", side_effect=fake_fetch), \
             mock.patch.object(preflight, "project_budget",
                               return_value=(100, "  (mocked)")), \
             contextlib.redirect_stdout(out):
            rc = preflight.run()
        return rc, out.getvalue()

    def good(self, hits=1):
        return {"ok": True, "status": 200, "bytes": 15000, "movies": 3,
                "hits": hits}

    def test_healthy_endpoint_passes(self):
        rc, out = self.run_preflight([self.good()] * 4)
        self.assertEqual(rc, 0)
        self.assertIn("PREFLIGHT PASS", out)

    def test_refusal_fails_and_names_the_ip(self):
        rc, out = self.run_preflight(
            [self.good(),
             {"ok": False, "error": "HTTP Error 403: Forbidden"},
             self.good(), self.good()])
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", out)
        self.assertIn("blocked", out)

    def test_blind_payload_fails(self):
        rc, out = self.run_preflight(
            [dict(self.good(), movies=0, hits=0)] + [self.good()] * 3)
        self.assertEqual(rc, 1)
        self.assertIn("BLIND", out)

    def test_zero_hits_across_all_dates_fails(self):
        rc, out = self.run_preflight([self.good(hits=0)] * 4)
        self.assertEqual(rc, 1)
        self.assertIn("PREFLIGHT FAIL", out)

    def test_sampled_dates_stay_inside_the_reconcile_window(self):
        dates = preflight.sample_dates()
        today = dt.date.today()
        for d in dates:
            offset = (dt.date.fromisoformat(d) - today).days
            self.assertGreaterEqual(offset, 1)
            self.assertLessEqual(offset, sentry.CFG["reconcile_days"])
        self.assertEqual(len(dates), len(set(dates)))

    def laptop_cadence(self, targets):
        """The production config: every tier at 30s."""
        sentry.CFG.update(base_interval_seconds=30,
                          overnight_interval_seconds=30,
                          burst_interval_seconds=30, burst_windows=[],
                          target_every_seconds=30, restock_watch_dates=[],
                          daily_request_budget=15000, target_dates=targets)

    def test_budget_projection_blocks_five_targets_at_30s(self):
        self.laptop_cadence(["2026-08-2%d" % i for i in range(1, 6)])
        total, _ = preflight.project_budget()
        self.assertGreater(total, 15000)

    def test_budget_projection_allows_three_targets_at_30s(self):
        self.laptop_cadence(["2026-08-21", "2026-08-22", "2026-08-26"])
        total, _ = preflight.project_budget()
        self.assertLessEqual(total, 15000)

    def test_over_budget_config_fails_the_run(self):
        self.laptop_cadence(["2026-08-2%d" % i for i in range(1, 6)])
        seq = [self.good()] * 4

        def fake_fetch(st, venue, date):
            st["lastFetch"] = dict(seq.pop(0), venue=venue, date=date)
            return {"movies": ["m"]}

        out = io.StringIO()
        with mock.patch.object(sentry, "fetch_day", side_effect=fake_fetch), \
             contextlib.redirect_stdout(out):
            rc = preflight.run()
        self.assertEqual(rc, 1)
        self.assertIn("exceeds daily_request_budget", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestDropAlertTimes(SentryTest):
    def test_body_lists_per_date_showtimes(self):
        found = {"2026-08-20": {"total": 2, "open": 2,
                                "times": ["3:30p", "7:00p"]},
                 "2026-08-21": {"total": 1, "open": 1, "times": ["6:00p"]}}
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.handle_drop(self.st, "AABQI", found)
        body = p.call_args.args[1]
        self.assertIn("Thu, Aug 20: 3:30 PM, 7:00 PM", body)
        self.assertIn("Fri, Aug 21: 6:00 PM", body)

    def test_caps_dates_and_times_so_a_big_drop_fits_one_push(self):
        found = {f"2026-09-{d:02d}": {"total": 10, "open": 10,
                                      "times": ["6:00p"] * 10}
                 for d in range(1, 9)}  # 8 dates, 10 showtimes each
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.handle_drop(self.st, "AABQI", found)
        body = p.call_args.args[1]
        self.assertIn("(+2 more)", body)          # times capped at 8
        self.assertIn("…and 2 more dates", body)  # dates capped at 6

    def test_found_without_times_key_still_alerts(self):
        found = {"2026-08-20": {"total": 2, "open": 2}}
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.handle_drop(self.st, "AABQI", found)
        self.assertIn("2 showtimes", p.call_args.args[1])


class TestHeartbeatPublish(SentryTest):
    def setUp(self):
        super().setUp()
        sentry.CFG["heartbeat_topic"] = "hb-secret"
        sentry.CFG["heartbeat_server"] = "https://ntfy.example"
        sentry.CFG["instance_name"] = "mac"

    def test_publishes_when_due_and_records_time(self):
        with mock.patch.object(sentry.urllib.request, "urlopen") as u:
            sentry.heartbeat(self.st)
        u.assert_called_once()
        payload = json.loads(u.call_args.args[0].data)
        self.assertEqual(payload["topic"], "hb-secret")
        self.assertEqual(payload["title"], "hb")
        self.assertEqual(payload["priority"], 1)  # must never ring
        self.assertIn("mac", payload["message"])
        self.assertIn("lastHeartbeat", self.st)

    def test_rate_limited_between_publishes(self):
        self.st["lastHeartbeat"] = sentry.time.time()
        with mock.patch.object(sentry.urllib.request, "urlopen") as u:
            sentry.heartbeat(self.st)
        u.assert_not_called()

    def test_no_topic_means_no_publish(self):
        sentry.CFG["heartbeat_topic"] = ""
        with mock.patch.object(sentry.urllib.request, "urlopen") as u:
            sentry.heartbeat(self.st)
        u.assert_not_called()

    def test_publish_failure_is_swallowed_and_retried_next_cycle(self):
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=OSError("boom")):
            sentry.heartbeat(self.st)  # must not raise
        self.assertNotIn("lastHeartbeat", self.st)

    def test_healthchecks_url_still_pinged_alongside(self):
        sentry.CFG["healthchecks_url"] = "https://hc.example/ping"
        with mock.patch.object(sentry.urllib.request, "urlopen") as u:
            sentry.heartbeat(self.st)
        self.assertEqual(u.call_count, 2)


class TestRotateLog(SentryTest):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        sentry.CFG["log_max_bytes"] = 10

    def test_rotates_both_logs_at_cap(self):
        with open(sentry.LOG_PATH, "w") as f:
            f.write("x" * 20)
        launchd = os.path.join(self.tmp, "launchd.log")
        with open(launchd, "w") as f:
            f.write("x" * 20)
        with mock.patch.object(sentry, "DATA", self.tmp):
            sentry.rotate_log()
        self.assertTrue(os.path.exists(sentry.LOG_PATH + ".1"))
        self.assertFalse(os.path.exists(sentry.LOG_PATH))
        self.assertTrue(os.path.exists(launchd + ".1"))
        os.unlink(sentry.LOG_PATH + ".1")
        open(sentry.LOG_PATH, "w").close()  # tearDown unlinks it

    def test_under_cap_untouched(self):
        with open(sentry.LOG_PATH, "w") as f:
            f.write("x" * 5)
        with mock.patch.object(sentry, "DATA", self.tmp):
            sentry.rotate_log()
        self.assertTrue(os.path.exists(sentry.LOG_PATH))
        self.assertFalse(os.path.exists(sentry.LOG_PATH + ".1"))


class TestSnapshotState(SentryTest):
    def test_writes_daily_snapshot_and_prunes_old(self):
        tmp = tempfile.mkdtemp()
        state_path = os.path.join(tmp, "state.json")
        snap_dir = os.path.join(tmp, "snapshots")
        os.makedirs(snap_dir)
        for d in ("2020-01-01", "2020-01-02", "2020-01-03"):
            open(os.path.join(snap_dir, f"state-{d}.json"), "w").close()
        with mock.patch.object(sentry, "STATE_PATH", state_path):
            sentry.snapshot_state(self.st, keep=3)
        snaps = sorted(os.listdir(snap_dir))
        self.assertEqual(len(snaps), 3)
        self.assertIn(f"state-{dt.date.today().isoformat()}.json", snaps)
        self.assertNotIn("state-2020-01-01.json", snaps)
        body = json.load(open(os.path.join(snap_dir, snaps[-1])))
        self.assertEqual(body["horizon"], self.st["horizon"])

    def test_failure_never_raises(self):
        with mock.patch.object(sentry, "STATE_PATH", "/nonexistent/x/state"):
            sentry.snapshot_state(self.st)  # must not raise


class TestDailyDigest(SentryTest):
    def test_content_covers_targets_horizon_and_health(self):
        sentry.CFG["target_dates"] = ["2026-08-21", "2026-08-22"]
        sentry.CFG["heartbeat_topic"] = "hb"
        self.st["targetAlerted"] = ["2026-08-21"]
        self.st["budgetYesterday"] = 123
        body = sentry.build_digest(self.st)
        self.assertIn("Waiting on: Sat, Aug 22", body)
        self.assertIn("Already on sale: Fri, Aug 21", body)
        self.assertIn("Lincoln Square schedule runs through", body)
        self.assertIn("Yesterday: 123 requests", body)
        self.assertIn("Off-box dead-man: armed.", body)

    def test_first_day_has_no_yesterday_count(self):
        body = sentry.build_digest(self.st)
        self.assertIn("First day on this box", body)

    def test_open_breaker_is_reported(self):
        self.st["breaker"]["state"] = "open"
        self.assertIn("breaker open", sentry.build_digest(self.st))

    def test_budget_rollover_stashes_yesterday(self):
        self.st["budget"] = {"date": "2001-01-01", "spent": 999}
        sentry.budget_ok(self.st)
        self.assertEqual(self.st["budgetYesterday"], 999)


class TestMovieExclude(SentryTest):
    def test_space_odyssey_revival_is_not_a_drop(self):
        sentry.CFG["movie_match"] = "the odyssey"
        sentry.CFG["movie_exclude"] = ["space odyssey"]
        vm = viewmodel([showtime()], title="2001: A Space Odyssey")
        self.assertEqual(sentry.odyssey_70mm(vm), [])

    def test_target_film_still_matches_with_exclude_set(self):
        sentry.CFG["movie_match"] = "the odyssey"
        sentry.CFG["movie_exclude"] = ["space odyssey"]
        vm = viewmodel([showtime()], title="The Odyssey (2026)")
        self.assertEqual(len(sentry.odyssey_70mm(vm)), 1)

    def test_no_exclude_key_keeps_old_behavior(self):
        vm = viewmodel([showtime()], title="2001: A Space Odyssey")
        self.assertEqual(len(sentry.odyssey_70mm(vm)), 1)  # match: "odyssey"


class TestBudgetSentinel(SentryTest):
    def _exhaust(self):
        sentry.budget_ok(self.st)  # roll to today
        self.st["budget"]["spent"] = sentry.CFG["daily_request_budget"]

    def test_exhaustion_returns_sentinel_not_none(self):
        self._exhaust()
        with mock.patch.object(sentry, "push", return_value=True):
            self.assertIs(sentry.fetch_day(self.st, "AABQI", "2026-08-20"),
                          sentry.BUDGET)

    def test_exhaustion_alerts_once_per_day(self):
        self._exhaust()
        with mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.fetch_day(self.st, "AABQI", "2026-08-20")
            sentry.fetch_day(self.st, "AABQI", "2026-08-21")
        self.assertEqual(p.call_count, 1)
        self.assertIn("budget", p.call_args.args[0].lower())

    def test_failed_budget_alert_retries(self):
        self._exhaust()
        with mock.patch.object(sentry, "push", return_value=False) as p:
            sentry.fetch_day(self.st, "AABQI", "2026-08-20")
            sentry.fetch_day(self.st, "AABQI", "2026-08-21")
        self.assertEqual(p.call_count, 2)  # not marked alerted until delivered

    def test_scan_dates_stops_without_tripping_breaker(self):
        self._exhaust()
        with mock.patch.object(sentry, "push", return_value=True):
            found = sentry.scan_dates(self.st, "AABQI", ["d1", "d2"])
        self.assertEqual(found, {})
        self.assertEqual(self.st["breaker"]["failures"], 0)
        self.assertEqual(self.st["breaker"]["state"], "closed")

    def test_restock_check_returns_without_tripping_breaker(self):
        self._exhaust()
        with mock.patch.object(sentry, "push", return_value=True):
            sentry.restock_check(self.st, "AABQI", ["2026-08-20"])
        self.assertEqual(self.st["breaker"]["failures"], 0)


class TestBlindRearm(SentryTest):
    def test_movies_reappearing_rearms_blind_alert(self):
        self.st["blindStreak"] = 25
        self.st["blindAlerted"] = True
        body = json.dumps({"viewModel": viewmodel([showtime()])}).encode()
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               return_value=FakeResp(body)):
            vm = sentry.fetch_day(self.st, "AABQI", "2026-08-20")
        self.assertIsNotNone(vm)
        self.assertEqual(self.st["blindStreak"], 0)
        self.assertFalse(self.st["blindAlerted"])


class TestTargetBeforeProbe(SentryTest):
    def test_target_check_runs_before_probe_loop_in_main(self):
        # regression tripwire for the announce race: the target path must
        # claim shared dates first or targetAlerted is never recorded
        import inspect
        src = inspect.getsource(sentry.main)
        self.assertLess(src.index("target_check(st, now)"),
                        src.index("steady state"))

    def test_digest_counts_announced_dates_as_live(self):
        sentry.CFG["target_dates"] = ["2026-08-21", "2026-08-22"]
        self.st["announced"] = ["2026-08-21"]  # drop path claimed it
        body = sentry.build_digest(self.st)
        self.assertIn("Already on sale: Fri, Aug 21", body)
        self.assertIn("Waiting on: Sat, Aug 22", body)


class TestAvailabilitySeeding(SentryTest):
    def test_scan_seeds_first_sight_but_never_clobbers(self):
        self.st["seenAvailability"]["AABQI|d1|6:00p"] = "soldout"
        data = {"d1": [showtime(), showtime(time_="9:00p",
                                            type_="soldout")]}
        with mock.patch.object(
                sentry, "fetch_day",
                side_effect=lambda st, v, d: viewmodel(data[d])):
            sentry.scan_dates(self.st, "AABQI", ["d1"])
        # pre-existing soldout preserved (pending flip stays detectable)...
        self.assertEqual(self.st["seenAvailability"]["AABQI|d1|6:00p"],
                         "soldout")
        # ...new key seeded at first sight
        self.assertEqual(self.st["seenAvailability"]["AABQI|d1|9:00p"],
                         "soldout")

    def test_seeded_baseline_lets_first_restock_poll_catch_flip(self):
        # reconcile saw it soldout; seat returned before the first dedicated
        # restock poll — previously treated as baseline, flip lost
        self.st["seenAvailability"]["AABQI|2026-08-20|6:00p"] = "soldout"
        vm = viewmodel([showtime(url="https://buy.example/x")])
        with mock.patch.object(sentry, "fetch_day", return_value=vm), \
                mock.patch.object(sentry, "push", return_value=True) as p:
            sentry.restock_check(self.st, "AABQI", ["2026-08-20"])
        p.assert_called_once()
        self.assertIn("reopened", p.call_args.args[1])


class TestElapsedSeconds(SentryTest):
    def test_naive_iso_with_aware_now(self):
        now = dt.datetime.now().astimezone()
        naive_iso = (dt.datetime.now()
                     - dt.timedelta(seconds=120)).isoformat()
        self.assertAlmostEqual(
            sentry.elapsed_seconds(now, naive_iso), 120, delta=5)

    def test_aware_iso_with_naive_now(self):
        now = dt.datetime.now()
        aware_iso = (dt.datetime.now().astimezone()
                     - dt.timedelta(seconds=60)).isoformat()
        self.assertAlmostEqual(
            sentry.elapsed_seconds(now, aware_iso), 60, delta=5)

    def test_cross_zone_wall_clocks_compare_correctly(self):
        eastern = zoneinfo.ZoneInfo("America/New_York")
        pacific = zoneinfo.ZoneInfo("America/Los_Angeles")
        base = dt.datetime(2026, 7, 25, 12, 0, tzinfo=eastern)
        same_instant = base.astimezone(pacific)
        self.assertEqual(
            sentry.elapsed_seconds(base, same_instant.isoformat()), 0)


class TestTelegramSetupGuard(SentryTest):
    UPDATES = {"result": [
        {"message": {"chat": {"id": 111, "first_name": "Friend"}}},
        {"message": {"chat": {"id": 987654, "first_name": "Brian"}}}]}

    def _run(self, pick=None):
        def fake_urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if "getUpdates" in url:
                return FakeResp(json.dumps(self.UPDATES).encode())
            return FakeResp(b'{"ok": true}')
        fd, tmpcfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        old = sentry.CONFIG_PATH
        sentry.CONFIG_PATH = tmpcfg
        try:
            with mock.patch.object(sentry.urllib.request, "urlopen",
                                   side_effect=fake_urlopen):
                return sentry.telegram_setup(pick)
        finally:
            sentry.CONFIG_PATH = old
            os.unlink(tmpcfg)

    def test_multiple_chats_refuses_to_guess(self):
        sentry.CFG["channels"]["telegram"]["token"] = "T"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self._run(), 1)
        self.assertIn("987654", out.getvalue())
        self.assertIn("111", out.getvalue())

    def test_explicit_pick_binds_that_chat(self):
        sentry.CFG["channels"]["telegram"]["token"] = "T"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(pick="111"), 0)
        self.assertEqual(
            sentry.CFG["channels"]["telegram"]["chat_id"], "111")

    def test_unknown_pick_rejected(self):
        sentry.CFG["channels"]["telegram"]["token"] = "T"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(pick="424242"), 1)


class TestHistoryRecorder(SentryTest):
    def setUp(self):
        super().setUp()          # already redirects HISTORY_PATH to a temp file
        self.hist = sentry.HISTORY_PATH
        os.unlink(self.hist)     # start absent, like a fresh install

    def rows(self):
        if not os.path.exists(self.hist):
            return []
        return [json.loads(l) for l in open(self.hist) if l.strip()]

    def test_first_sighting_then_only_changes(self):
        hits = [{"time": "6:00p", "type": "soldout"}]
        sentry.record_history(self.st, "AABQI", "2026-08-15", hits)
        sentry.record_history(self.st, "AABQI", "2026-08-15", hits)  # no change
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["from"])
        self.assertEqual(rows[0]["to"], "soldout")

    def test_flip_is_recorded_with_previous_status(self):
        sentry.record_history(self.st, "AABQI", "2026-08-15",
                              [{"time": "6:00p", "type": "soldout"}])
        sentry.record_history(self.st, "AABQI", "2026-08-15",
                              [{"time": "6:00p", "type": "available"}])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]["from"], rows[1]["to"]),
                         ("soldout", "available"))

    def test_respects_slot_filter(self):
        sentry.CFG["target_dates"] = ["2026-08-21"]  # enables slot_qualifies
        sentry.CFG["target_after_hour"] = 18
        sentry.record_history(self.st, "AABQI", "2026-08-21",
                              [{"time": "10:00a", "type": "available"},
                               {"time": "7:00p", "type": "available"}])
        self.assertEqual([r["time"] for r in self.rows()], ["7:00p"])

    def test_disabled_by_config(self):
        sentry.CFG["history_enabled"] = False
        sentry.record_history(self.st, "AABQI", "2026-08-15",
                              [{"time": "6:00p", "type": "soldout"}])
        self.assertEqual(self.rows(), [])

    def test_write_failure_never_raises(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            sentry.record_history(self.st, "AABQI", "2026-08-15",
                                  [{"time": "6:00p", "type": "soldout"}])

    def test_rotates_at_cap(self):
        sentry.CFG["history_max_bytes"] = 10
        with open(self.hist, "w") as f:
            f.write("x" * 50)
        sentry.record_history(self.st, "AABQI", "2026-08-15",
                              [{"time": "6:00p", "type": "soldout"}])
        self.assertTrue(os.path.exists(self.hist + ".1"))
        self.assertEqual(len(self.rows()), 1)

    def test_fetch_day_records_without_extra_requests(self):
        body = json.dumps({"viewModel": viewmodel(
            [showtime(type_="soldout")])}).encode()
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               return_value=FakeResp(body)) as u:
            sentry.fetch_day(self.st, "AABQI", "2026-08-15")
        self.assertEqual(u.call_count, 1)      # no second fetch for history
        self.assertEqual(len(self.rows()), 1)


class TestStudyDates(SentryTest):
    def setUp(self):
        super().setUp()
        self.now = dt.datetime(2026, 7, 26, 12, 0).astimezone()
        self.st["horizon"]["AABQI"] = "2026-07-28"

    def test_disabled_when_study_days_zero(self):
        sentry.CFG["study_days"] = 0
        self.assertEqual(sentry.study_due_dates(self.st, self.now), [])

    def test_stops_at_horizon(self):
        sentry.CFG["study_days"] = 7
        self.assertEqual(sentry.study_due_dates(self.st, self.now),
                         ["2026-07-26", "2026-07-27", "2026-07-28"])

    def test_respects_interval(self):
        sentry.CFG["study_days"] = 2
        sentry.CFG["study_every_seconds"] = 600
        self.st["studyLastPoll"]["2026-07-26"] = self.now.isoformat()
        self.assertEqual(sentry.study_due_dates(self.st, self.now),
                         ["2026-07-27"])

    def test_due_again_once_interval_elapsed(self):
        sentry.CFG["study_days"] = 1
        sentry.CFG["study_every_seconds"] = 600
        self.st["studyLastPoll"]["2026-07-26"] = (
            self.now - dt.timedelta(minutes=11)).isoformat()
        self.assertEqual(sentry.study_due_dates(self.st, self.now),
                         ["2026-07-26"])


class TestExpiredDateResponse(SentryTest):
    """Fandango answers {"date": null} — no viewModel — for a date whose
    showtimes have all passed. Valid, not a failure."""
    BODY = json.dumps({"date": None}).encode()

    def test_returns_empty_viewmodel_not_failure(self):
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               return_value=FakeResp(self.BODY)):
            vm = sentry.fetch_day(self.st, "AABQI", "2026-07-26")
        self.assertEqual(vm, {})
        self.assertIsNotNone(vm)          # None would trip the breaker
        self.assertEqual(sentry.odyssey_70mm(vm), [])
        self.assertTrue(self.st["lastFetch"]["ok"])

    def test_does_not_inflate_blind_streak(self):
        self.st["blindStreak"] = 7
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               return_value=FakeResp(self.BODY)):
            sentry.fetch_day(self.st, "AABQI", "2026-07-26")
        self.assertEqual(self.st["blindStreak"], 7)  # unchanged, either way

    def test_scan_dates_treats_it_as_an_empty_day(self):
        with mock.patch.object(sentry, "fetch_day", return_value={}):
            found = sentry.scan_dates(self.st, "AABQI", ["d1", "d2", "d3"])
        self.assertEqual(found, {})
        self.assertEqual(self.st["breaker"]["failures"], 0)

    def test_malformed_json_is_still_a_real_failure(self):
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               return_value=FakeResp(b"<html>nope</html>")):
            self.assertIsNone(sentry.fetch_day(self.st, "AABQI", "2026-07-26"))


class TestSuiteDoesNotTouchProductionData(SentryTest):
    """Regression guard: the suite once appended fabricated rows to the real
    history.jsonl because fetch_day records as a side effect. Every writable
    path the tests can reach must point somewhere disposable."""

    def test_history_and_log_paths_are_redirected(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for path in (sentry.HISTORY_PATH, sentry.LOG_PATH):
            self.assertFalse(
                os.path.abspath(path).startswith(repo + os.sep),
                f"{path} points inside the repo — tests would clobber real data")

    def test_a_recording_fetch_writes_only_to_the_temp_file(self):
        real = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "history.jsonl")
        before = os.path.getsize(real) if os.path.exists(real) else None
        body = json.dumps({"viewModel": viewmodel([showtime()])}).encode()
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               return_value=FakeResp(body)):
            sentry.fetch_day(self.st, "AABQI", "2026-08-20")
        after = os.path.getsize(real) if os.path.exists(real) else None
        self.assertEqual(before, after)
        self.assertTrue(os.path.getsize(sentry.HISTORY_PATH) > 0)


RT_WINDOW = [{"days": ["Tue"], "start_hour": 6, "end_hour": 12,
              "interval_seconds": 6}]


def et(y, m, d, h, minute=0):
    """A wall-clock moment in the theater's timezone."""
    return dt.datetime(y, m, d, h, minute,
                       tzinfo=zoneinfo.ZoneInfo("America/New_York"))


class TestRealtimeWindow(SentryTest):
    def setUp(self):
        super().setUp()
        sentry.CFG["realtime_windows"] = RT_WINDOW
        sentry.CFG["schedule_tz"] = "America/New_York"

    def test_inside_tuesday_morning(self):
        self.assertEqual(sentry.realtime_interval(et(2026, 7, 28, 6, 0)), 6)
        self.assertEqual(sentry.realtime_interval(et(2026, 7, 28, 11, 59)), 6)

    def test_outside_by_hour_and_by_day(self):
        self.assertIsNone(sentry.realtime_interval(et(2026, 7, 28, 5, 59)))
        self.assertIsNone(sentry.realtime_interval(et(2026, 7, 28, 12, 0)))
        self.assertIsNone(sentry.realtime_interval(et(2026, 7, 29, 8, 0)))

    def test_evaluated_in_theater_time_not_host_time(self):
        # 06:30 ET is 03:30 PT — a host-clock check would miss the window
        pacific = et(2026, 7, 28, 6, 30).astimezone(
            zoneinfo.ZoneInfo("America/Los_Angeles"))
        self.assertEqual(pacific.hour, 3)
        self.assertEqual(sentry.realtime_interval(pacific), 6)

    def test_survives_a_dst_shift(self):
        # first Tuesday after the 2026 fall-back; ET is UTC-5 by then
        nov = dt.datetime(2026, 11, 3, 7, 0,
                          tzinfo=zoneinfo.ZoneInfo("America/New_York"))
        self.assertEqual(nov.strftime("%a"), "Tue")
        self.assertEqual(sentry.realtime_interval(nov), 6)

    def test_poll_interval_defers_to_realtime(self):
        self.assertEqual(sentry.poll_interval(et(2026, 7, 28, 7, 0)), 6)

    def test_interval_floor_protects_against_a_typo(self):
        sentry.CFG["realtime_windows"] = [dict(RT_WINDOW[0],
                                               interval_seconds=0)]
        self.assertEqual(sentry.realtime_interval(et(2026, 7, 28, 7, 0)), 3)

    def test_seconds_left_counts_down_to_window_close(self):
        left = sentry.realtime_seconds_left(et(2026, 7, 28, 11, 30))
        self.assertAlmostEqual(left, 1800, delta=5)
        self.assertEqual(sentry.realtime_seconds_left(et(2026, 7, 29, 8, 0)), 0)

    def test_no_windows_configured_is_inert(self):
        sentry.CFG["realtime_windows"] = []
        self.assertIsNone(sentry.realtime_interval(et(2026, 7, 28, 7, 0)))


class TestRealtimeLoop(SentryTest):
    def setUp(self):
        super().setUp()
        sentry.CFG["realtime_windows"] = RT_WINDOW
        sentry.CFG["schedule_tz"] = "America/New_York"
        self.now = dt.datetime.now().astimezone()

    # the loop floors its sleep at 0.5s (never poll faster than twice a
    # second, whatever the config says), so a meaningful slice spans several
    def _run(self, slice_s=1.8, **patches):
        with mock.patch.object(sentry, "REALTIME_SLICE", slice_s), \
                mock.patch.object(sentry, "realtime_interval",
                                  return_value=patches.pop("interval", 0.05)), \
                mock.patch.object(sentry, "realtime_seconds_left",
                                  return_value=9999), \
                mock.patch.object(sentry, "target_check"), \
                mock.patch.object(sentry, "heartbeat"), \
                mock.patch.object(sentry, "save_state"), \
                mock.patch.object(sentry, "RealtimeSession"), \
                mock.patch.object(sentry, "probe_once",
                                  **patches) as probe:
            sentry.realtime_loop(self.st, self.now)
        return probe

    def test_polls_repeatedly_within_the_slice(self):
        probe = self._run(return_value=False)
        self.assertGreater(probe.call_count, 2)

    def test_updates_last_poll_so_the_watchdog_sees_life(self):
        self._run(return_value=False)
        age = sentry.elapsed_seconds(dt.datetime.now().astimezone(),
                                     self.st["lastPollAt"])
        self.assertLess(age, 5)

    def test_stops_when_budget_is_exhausted(self):
        sentry.budget_ok(self.st)
        self.st["budget"]["spent"] = sentry.CFG["daily_request_budget"]
        probe = self._run(return_value=False)
        probe.assert_not_called()

    def test_stops_when_breaker_opens(self):
        self.st["breaker"] = {"state": "open", "failures": 9,
                              "openUntil": sentry.time.time() + 999,
                              "degradedSent": False}
        probe = self._run(return_value=False)
        probe.assert_not_called()

    def test_a_drop_persists_state_immediately(self):
        with mock.patch.object(sentry, "REALTIME_SLICE", 0.2), \
                mock.patch.object(sentry, "realtime_interval",
                                  return_value=0.05), \
                mock.patch.object(sentry, "realtime_seconds_left",
                                  return_value=9999), \
                mock.patch.object(sentry, "target_check"), \
                mock.patch.object(sentry, "heartbeat"), \
                mock.patch.object(sentry, "RealtimeSession"), \
                mock.patch.object(sentry, "probe_once", return_value=True), \
                mock.patch.object(sentry, "save_state") as save:
            sentry.realtime_loop(self.st, self.now)
        self.assertGreater(save.call_count, 1)

    def test_session_is_always_closed(self):
        with mock.patch.object(sentry, "REALTIME_SLICE", 0.1), \
                mock.patch.object(sentry, "realtime_interval",
                                  return_value=0.05), \
                mock.patch.object(sentry, "realtime_seconds_left",
                                  return_value=9999), \
                mock.patch.object(sentry, "target_check"), \
                mock.patch.object(sentry, "heartbeat"), \
                mock.patch.object(sentry, "save_state"), \
                mock.patch.object(sentry, "probe_once",
                                  side_effect=RuntimeError("boom")), \
                mock.patch.object(sentry, "RealtimeSession") as S:
            with self.assertRaises(RuntimeError):
                sentry.realtime_loop(self.st, self.now)
        S.return_value.close.assert_called_once()


class TestRealtimeSession(SentryTest):
    def test_reuses_one_connection_across_polls(self):
        with mock.patch.object(sentry.http.client, "HTTPSConnection") as C:
            C.return_value.getresponse.return_value = mock.Mock(
                status=200, read=mock.Mock(return_value=b"{}"))
            s = sentry.RealtimeSession()
            for _ in range(4):
                s.get("AABQI", "2026-08-20")
        self.assertEqual(C.call_count, 1)      # one handshake, four requests
        self.assertEqual(s.reconnects, 1)

    def test_reconnects_once_after_a_dropped_socket(self):
        with mock.patch.object(sentry.http.client, "HTTPSConnection") as C:
            good = mock.Mock(status=200, read=mock.Mock(return_value=b"{}"))
            C.return_value.getresponse.side_effect = [OSError("closed"), good]
            s = sentry.RealtimeSession()
            status, body = s.get("AABQI", "2026-08-20")
        self.assertEqual((status, body), (200, b"{}"))
        self.assertEqual(s.reconnects, 2)

    def test_http_error_raises_so_the_breaker_sees_it(self):
        with mock.patch.object(sentry.http.client, "HTTPSConnection") as C:
            C.return_value.getresponse.return_value = mock.Mock(
                status=429, read=mock.Mock(return_value=b"slow down"))
            with self.assertRaises(Exception):
                sentry.RealtimeSession().get("AABQI", "2026-08-20")

    def test_fetch_day_through_a_session_keeps_normal_bookkeeping(self):
        body = json.dumps({"viewModel": viewmodel([showtime()])}).encode()
        session = mock.Mock()
        session.get.return_value = (200, body)
        vm = sentry.fetch_day(self.st, "AABQI", "2026-08-20", session=session)
        self.assertEqual(len(sentry.odyssey_70mm(vm)), 1)
        self.assertTrue(self.st["lastFetch"]["ok"])
        self.assertEqual(self.st["budget"]["spent"], 1)
        session.get.assert_called_once_with("AABQI", "2026-08-20")
