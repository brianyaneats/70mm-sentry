"""Unit tests for the Fandango adapter (adapters/fandango.py) and its
frozen-contract conformance (adapters/base.py). Run from ~/Developer/70mm-sentry:

    python3 -m unittest discover -s tests -v

All network I/O is mocked; nothing here touches Fandango, ntfy, or WhatsApp.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentry  # noqa: E402
from adapters import base  # noqa: E402
from adapters import fandango  # noqa: E402


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


TARGET = {"venue": "AABQI", "referer": "https://ref.example/ls",
          "movie_match": "odyssey", "movie_exclude": [],
          "format_match": "70MM"}


class TestAdapterDeclaration(unittest.TestCase):
    """The registry/politeness declaration the kernel enforces at its
    choke point — not something the adapter can opt out of."""

    def test_identity_and_risk_tier(self):
        a = fandango.FandangoAdapter()
        self.assertEqual(a.name, "fandango")
        self.assertEqual(a.risk_tier, "yellow")

    def test_politeness_matches_config_defaults(self):
        a = fandango.FandangoAdapter()
        self.assertEqual(a.min_spacing_seconds, 1.0)
        self.assertEqual(a.daily_request_budget, 25000)

    def test_does_not_import_sentry(self):
        # a genuine circular import would already have blown up at module
        # load, but assert the module object explicitly to catch a
        # regression where someone adds "import sentry" back in later
        self.assertNotIn("sentry", vars(fandango))


class TestURLAndHeaders(unittest.TestCase):
    def test_url_includes_venue_and_date(self):
        url = fandango.build_url(TARGET, "2026-08-20")
        self.assertIn("AABQI", url)
        self.assertIn("2026-08-20", url)

    def test_headers_include_the_venue_referer(self):
        headers = fandango.build_headers(TARGET)
        self.assertEqual(headers["Referer"], "https://ref.example/ls")
        self.assertIn("User-Agent", headers)

    def test_fetch_composes_url_and_headers_then_calls_http_get(self):
        seen = {}

        def http_get(url, headers):
            seen["url"] = url
            seen["headers"] = headers
            return b'{"viewModel": {"movies": []}}'

        a = fandango.FandangoAdapter()
        raw = a.fetch(TARGET, "2026-08-20", http_get)
        self.assertIn("AABQI", seen["url"])
        self.assertEqual(seen["headers"]["Referer"], TARGET["referer"])
        self.assertEqual(raw, b'{"viewModel": {"movies": []}}')


class TestParseStateMapping(unittest.TestCase):
    def setUp(self):
        self.a = fandango.FandangoAdapter()

    def parse(self, showtimes, title="The Odyssey (2026)"):
        raw = json.dumps({"viewModel": viewmodel(showtimes, title)}).encode()
        return self.a.parse(raw, TARGET, "2026-08-20")

    def test_available_soldout_restricted_map_to_contract_states(self):
        obs = self.parse([showtime(type_="available"),
                          showtime(type_="soldout"),
                          showtime(type_="restricted")])
        self.assertEqual(len(obs), 3)
        states = sorted(o["state"] for o in obs)
        self.assertEqual(states, sorted([base.STATE_AVAILABLE,
                                         base.STATE_SOLDOUT,
                                         base.STATE_RESTRICTED]))

    def test_pastshowtime_and_expired_are_dropped_not_gone(self):
        # preserves sentry.py's pre-extraction odyssey_70mm() behavior
        # exactly: past/expired showtimes never appeared in its result,
        # so they must not appear as observations either (not even as
        # STATE_GONE)
        obs = self.parse([showtime(type_="pastshowtime"),
                          showtime(expired=True)])
        self.assertEqual(obs, [])

    def test_non_70mm_and_non_matching_title_excluded(self):
        obs = self.parse([showtime(fmts=("Dolby Cinema",))])
        self.assertEqual(obs, [])
        obs = self.parse([showtime()], title="Some Other Film")
        self.assertEqual(obs, [])

    def test_unrecognized_type_maps_to_gone_not_silently_dropped(self):
        obs = self.parse([showtime(type_="some_future_fandango_type")])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["state"], base.STATE_GONE)

    def test_observations_validate_against_the_contract(self):
        obs = self.parse([showtime()])
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertIn(o["state"], base.STATES)
        self.assertEqual(o["date"], "2026-08-20")
        self.assertTrue(o["unit_id"])
        self.assertIn("formats", o["meta"])

    def test_expired_date_payload_is_a_benign_empty_list(self):
        raw = json.dumps({"date": None}).encode()
        self.assertEqual(self.a.parse(raw, TARGET, "2026-08-20"), [])

    def test_unrecognized_shape_raises_not_silent_empty_list(self):
        raw = json.dumps([1, 2, 3]).encode()  # a JSON array, not an object
        with self.assertRaises(fandango.FandangoFeedShapeError):
            self.a.parse(raw, TARGET, "2026-08-20")

    def test_viewmodel_missing_movies_key_raises(self):
        raw = json.dumps({"viewModel": {"notMovies": []}}).encode()
        with self.assertRaises(fandango.FandangoFeedShapeError):
            self.a.parse(raw, TARGET, "2026-08-20")


class TestHealthCheck(unittest.TestCase):
    def setUp(self):
        self.a = fandango.FandangoAdapter()

    def test_ok_when_movies_listed(self):
        raw = json.dumps({"viewModel": viewmodel([showtime()])}).encode()
        ok, reason = self.a.health_check(raw)
        self.assertTrue(ok)
        self.assertIn("1 movies", reason)

    def test_not_ok_when_zero_movies(self):
        raw = json.dumps({"viewModel": {"movies": []}}).encode()
        ok, reason = self.a.health_check(raw)
        self.assertFalse(ok)
        self.assertIn("0 movies", reason)

    def test_ok_for_the_benign_expired_date_payload(self):
        raw = json.dumps({"date": None}).encode()
        ok, reason = self.a.health_check(raw)
        self.assertTrue(ok)

    def test_not_ok_for_invalid_json(self):
        ok, reason = self.a.health_check(b"not json at all")
        self.assertFalse(ok)

    def test_not_ok_for_unrecognized_shape(self):
        ok, reason = self.a.health_check(b"[1, 2, 3]")
        self.assertFalse(ok)


class TestKernelPipeLogging(unittest.TestCase):
    """The kernel (sentry.py), not the adapter, owns catching and tagging
    a transport failure — this proves a BrokenPipeError raised by the
    transport on sentry.fetch_day's non-session path is caught, tagged
    [PIPE] with errno and exception class, and logged (never swallowed),
    per the contract's failure-logging requirement."""

    def setUp(self):
        self._cfg = sentry.CFG
        sentry.CFG = {
            "venues": {"AABQI": {"name": "Lincoln Square",
                                 "referer": "https://ref.example/ls"}},
            "movie_match": "odyssey", "movie_exclude": [],
            "format_match": "70MM", "daily_request_budget": 250,
            "min_request_spacing_seconds": 0, "seed_horizon": "2026-08-19",
        }
        fd, sentry.LOG_PATH = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        sentry._LAST_REQUEST_AT = 0.0
        self.st = sentry.default_state()

    def tearDown(self):
        os.unlink(sentry.LOG_PATH)
        sentry.CFG = self._cfg

    def test_broken_pipe_on_fetch_is_tagged_and_logged(self):
        err = BrokenPipeError(32, "Broken pipe")
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=err):
            result = sentry.fetch_day(self.st, "AABQI", "2026-08-20")
        self.assertIsNone(result)
        self.assertFalse(self.st["lastFetch"]["ok"])
        with open(sentry.LOG_PATH) as f:
            logged = f.read()
        self.assertIn("[PIPE]", logged)
        self.assertIn("BrokenPipeError", logged)
        self.assertIn("errno=32", logged)

    def test_connection_reset_on_fetch_is_tagged_pipe(self):
        with mock.patch.object(sentry.urllib.request, "urlopen",
                               side_effect=ConnectionResetError(
                                   104, "Connection reset by peer")):
            result = sentry.fetch_day(self.st, "AABQI", "2026-08-20")
        self.assertIsNone(result)
        with open(sentry.LOG_PATH) as f:
            logged = f.read()
        self.assertIn("[PIPE]", logged)
        self.assertIn("ConnectionResetError", logged)

    def test_realtime_session_dead_socket_logs_pipe_even_on_silent_retry(
            self):
        with mock.patch.object(sentry.http.client, "HTTPSConnection") as C:
            good = mock.Mock(status=200, read=mock.Mock(return_value=b"{}"))
            C.return_value.getresponse.side_effect = [
                BrokenPipeError(32, "Broken pipe"), good]
            session = sentry.RealtimeSession()
            status, body = session.get("AABQI", "2026-08-20")
        self.assertEqual((status, body), (200, b"{}"))
        with open(sentry.LOG_PATH) as f:
            logged = f.read()
        # the retry succeeded silently from the caller's point of view,
        # but the dead-socket event must still have been logged
        self.assertIn("[PIPE]", logged)
        self.assertIn("BrokenPipeError", logged)


if __name__ == "__main__":
    unittest.main()
