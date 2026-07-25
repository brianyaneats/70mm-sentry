"""Unit tests for 70mm Sentry. Run from ~/Developer/70mm-sentry:

    python3 -m unittest discover -s tests -v

All network I/O is mocked; nothing here touches Fandango, ntfy, or WhatsApp.
"""

import copy
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
import zoneinfo
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentry  # noqa: E402

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
        sentry.CFG = copy.deepcopy(TEST_CFG)
        fd, sentry.LOG_PATH = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        self.st = sentry.default_state()

    def tearDown(self):
        os.unlink(sentry.LOG_PATH)
        sentry.CFG = self._cfg
        sentry.LOG_PATH = self._log


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
        self.assertEqual(found, {"d1": {"total": 2, "open": 1}})

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
        with mock.patch.object(sentry.urllib.request, "urlopen") as u:
            self.assertIsNone(sentry.fetch_day(self.st, "AABQI", "2026-08-20"))
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
            self.assertTrue(sentry.push("DROP", "b", priority="urgent"))
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
        updates = {"result": [
            {"message": {"chat": {"id": 111, "first_name": "Old"}}},
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
        self.assertIn("RESTOCK", p.call_args.args[0])
        self.assertIn("2026-08-15 6:00p → https://tickets.example/jump?y=2",
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
        self.assertIn("TARGET LIVE", p.call_args.args[0])
        self.assertIn("2026-08-21", p.call_args.args[1])
        self.assertEqual(self.st["targetAlerted"], ["2026-08-21"])
        # already alerted → no further fetches
        f2, p2 = self.run_target([showtime(time_="7:00p")])
        f2.assert_not_called()

    def test_alert_includes_amc_cross_check_link(self):
        sentry.CFG["venues"]["AABQI"]["amc_link"] = \
            "https://amc.example/showtimes?date={date}"
        f, p = self.run_target([showtime(time_="7:00p")])
        self.assertIn("AMC side: https://amc.example/showtimes"
                      "?date=2026-08-21", p.call_args.args[1])

    def test_open_slot_alert_carries_showtime_deep_link(self):
        f, p = self.run_target(
            [showtime(time_="7:00p", url="https://tickets.example/jump?x=1")])
        self.assertIn("7:00p → https://tickets.example/jump?x=1",
                      p.call_args.args[1])

    def test_missing_deep_link_falls_back_to_venue_page(self):
        f, p = self.run_target([showtime(time_="7:00p")])
        self.assertIn("7:00p → https://link.example/ls", p.call_args.args[1])

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


if __name__ == "__main__":
    unittest.main()
