"""Tests for adapters/recreation_gov.py — fully mocked, no live network.

Covers: URL composition, the full raw-state vocabulary mapping, the
unknown-shape-must-raise rule, health_check verdicts both ways, unit_id
stability across polls, and a "needs nothing beyond base.py" gate that
drives the adapter purely through the SourceAdapter interface with a tiny
fake kernel poll loop — including a fake http_get that raises
BrokenPipeError, proving the adapter does not catch transport errors.
"""

import unittest
from urllib.parse import unquote

from adapters.base import (
    STATE_AVAILABLE,
    STATE_RESTRICTED,
    STATE_SOLDOUT,
    SourceAdapter,
)
from adapters.recreation_gov import RecreationGovAdapter


def _payload(entries):
    """entries: {site_id: (site_name, campsite_type, {date_key: raw_state})}"""
    campsites = {}
    for site_id, (name, ctype, availabilities) in entries.items():
        campsites[site_id] = {
            "site": name,
            "campsite_type": ctype,
            "availabilities": availabilities,
        }
    return {"campsites": campsites}


class FetchUrlCompositionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecreationGovAdapter()

    def test_composes_month_start_and_url_encoded_timestamp(self):
        captured = {}

        def fake_http_get(url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return _payload({})

        self.adapter.fetch({"campground_id": "232447"}, "2026-08-15", fake_http_get)

        url = captured["url"]
        self.assertIn(
            "recreation.gov/api/camps/availability/campground/232447/month", url
        )
        self.assertIn("start_date=", url)

        query_value = url.split("start_date=", 1)[1]
        self.assertEqual(unquote(query_value), "2026-08-01T00:00:00.000Z")
        # And it really is URL-encoded in the raw URL (colons escaped).
        self.assertNotIn(":", query_value)

    def test_sends_json_accept_headers(self):
        captured = {}

        def fake_http_get(url, headers):
            captured["headers"] = headers
            return _payload({})

        self.adapter.fetch({"campground_id": "1"}, "2026-01-01", fake_http_get)
        headers = captured["headers"]
        self.assertEqual(headers.get("Accept"), "application/json")

    def test_fetch_does_not_catch_transport_errors(self):
        def raising_http_get(url, headers):
            raise BrokenPipeError("simulated dead kept-alive socket")

        with self.assertRaises(BrokenPipeError):
            self.adapter.fetch({"campground_id": "1"}, "2026-01-01", raising_http_get)


class StateMappingTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecreationGovAdapter()

    def _parse_single(self, raw_state, date="2026-08-01"):
        payload = _payload(
            {"5391": ("A12", "STANDARD NONELECTRIC", {date + "T00:00:00Z": raw_state})}
        )
        obs = self.adapter.parse(payload, {"campground_id": "232447"}, date)
        self.assertEqual(len(obs), 1)
        return obs[0]

    def test_available_maps_to_available(self):
        o = self._parse_single("Available")
        self.assertEqual(o["state"], STATE_AVAILABLE)

    def test_reserved_maps_to_soldout(self):
        o = self._parse_single("Reserved")
        self.assertEqual(o["state"], STATE_SOLDOUT)

    def test_not_available_maps_to_restricted(self):
        o = self._parse_single("Not Available")
        self.assertEqual(o["state"], STATE_RESTRICTED)
        self.assertEqual(o["meta"]["raw_state"], "Not Available")

    def test_not_reservable_maps_to_restricted(self):
        o = self._parse_single("Not Reservable")
        self.assertEqual(o["state"], STATE_RESTRICTED)
        self.assertEqual(o["meta"]["raw_state"], "Not Reservable")

    def test_closed_maps_to_restricted(self):
        o = self._parse_single("Closed")
        self.assertEqual(o["state"], STATE_RESTRICTED)
        self.assertEqual(o["meta"]["raw_state"], "Closed")

    def test_nyr_maps_to_restricted(self):
        o = self._parse_single("NYR")
        self.assertEqual(o["state"], STATE_RESTRICTED)
        self.assertEqual(o["meta"]["raw_state"], "NYR")

    def test_unrecognized_raw_value_maps_to_restricted_not_crash(self):
        o = self._parse_single("Something New Recreation.gov Invented")
        self.assertEqual(o["state"], STATE_RESTRICTED)
        self.assertEqual(o["meta"]["raw_state"], "Something New Recreation.gov Invented")

    def test_label_built_from_site_name_and_type(self):
        o = self._parse_single("Available")
        self.assertIn("A12", o["label"])
        self.assertIn("STANDARD NONELECTRIC", o["label"])

    def test_missing_date_key_yields_no_observation_for_that_site(self):
        date = "2026-08-01"
        payload = _payload(
            {
                "5391": ("A12", "STANDARD", {"2026-08-02T00:00:00Z": "Available"}),
                "5392": ("A13", "STANDARD", {date + "T00:00:00Z": "Available"}),
            }
        )
        obs = self.adapter.parse(payload, {"campground_id": "232447"}, date)
        self.assertEqual(len(obs), 1)
        self.assertIn("5392", obs[0]["unit_id"])


class UnknownShapeRaisesTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecreationGovAdapter()

    def test_missing_campsites_key_raises(self):
        with self.assertRaises(ValueError):
            self.adapter.parse({"unexpected": "shape"}, {"campground_id": "1"}, "2026-08-01")

    def test_campsites_not_a_dict_raises(self):
        with self.assertRaises(ValueError):
            self.adapter.parse({"campsites": []}, {"campground_id": "1"}, "2026-08-01")

    def test_non_dict_top_level_raises(self):
        with self.assertRaises(ValueError):
            self.adapter.parse(["not", "a", "dict"], {"campground_id": "1"}, "2026-08-01")

    def test_site_entry_not_a_dict_raises(self):
        payload = {"campsites": {"5391": "not-a-dict"}}
        with self.assertRaises(ValueError):
            self.adapter.parse(payload, {"campground_id": "1"}, "2026-08-01")

    def test_site_missing_availabilities_raises(self):
        payload = {"campsites": {"5391": {"site": "A12", "campsite_type": "STANDARD"}}}
        with self.assertRaises(ValueError):
            self.adapter.parse(payload, {"campground_id": "1"}, "2026-08-01")


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecreationGovAdapter()

    def test_healthy_payload_is_ok(self):
        payload = _payload(
            {"5391": ("A12", "STANDARD", {"2026-08-01T00:00:00Z": "Available"})}
        )
        ok, reason = self.adapter.health_check(payload)
        self.assertTrue(ok)

    def test_non_dict_payload_is_unhealthy(self):
        ok, reason = self.adapter.health_check(["nope"])
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_missing_campsites_key_is_unhealthy(self):
        ok, reason = self.adapter.health_check({"foo": "bar"})
        self.assertFalse(ok)
        self.assertIn("campsites", reason)

    def test_campsites_wrong_type_is_unhealthy(self):
        ok, reason = self.adapter.health_check({"campsites": "nope"})
        self.assertFalse(ok)

    def test_empty_campsites_is_unhealthy(self):
        ok, reason = self.adapter.health_check({"campsites": {}})
        self.assertFalse(ok)

    def test_site_missing_availabilities_is_unhealthy(self):
        ok, reason = self.adapter.health_check(
            {"campsites": {"5391": {"site": "A12"}}}
        )
        self.assertFalse(ok)

    def test_site_with_empty_availabilities_is_unhealthy(self):
        ok, reason = self.adapter.health_check(
            {"campsites": {"5391": {"site": "A12", "availabilities": {}}}}
        )
        self.assertFalse(ok)


class UnitIdStabilityTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecreationGovAdapter()

    def test_unit_id_stable_across_polls_with_state_change(self):
        date = "2026-08-01"
        target = {"campground_id": "232447"}

        payload_1 = _payload(
            {"5391": ("A12", "STANDARD", {date + "T00:00:00Z": "Available"})}
        )
        payload_2 = _payload(
            {"5391": ("A12", "STANDARD", {date + "T00:00:00Z": "Reserved"})}
        )

        obs_1 = self.adapter.parse(payload_1, target, date)[0]
        obs_2 = self.adapter.parse(payload_2, target, date)[0]

        self.assertEqual(obs_1["unit_id"], obs_2["unit_id"])
        self.assertEqual(obs_1["unit_id"], "232447:5391:2026-08-01")
        self.assertNotEqual(obs_1["state"], obs_2["state"])

    def test_unit_id_varies_by_date(self):
        target = {"campground_id": "232447"}
        payload = _payload(
            {
                "5391": (
                    "A12",
                    "STANDARD",
                    {
                        "2026-08-01T00:00:00Z": "Available",
                        "2026-08-02T00:00:00Z": "Available",
                    },
                )
            }
        )
        obs_day1 = self.adapter.parse(payload, target, "2026-08-01")[0]
        obs_day2 = self.adapter.parse(payload, target, "2026-08-02")[0]
        self.assertNotEqual(obs_day1["unit_id"], obs_day2["unit_id"])


class FakeKernelPollHarness:
    """Simulates just enough of a kernel poll cycle to drive an adapter
    purely through the SourceAdapter interface — no adapter-specific
    hooks, no special-casing. If recreation_gov.py needed anything beyond
    what adapters/base.py declares, this harness would not compile or
    would need adapter-specific branches; it has neither.
    """

    def __init__(self, adapter: SourceAdapter, http_get):
        assert isinstance(adapter, SourceAdapter)
        self.adapter = adapter
        self.http_get = http_get

    def poll_once(self, target, date):
        raw = self.adapter.fetch(target, date, self.http_get)
        ok, reason = self.adapter.health_check(raw)
        if not ok:
            raise RuntimeError("[BLIND] %s: %s" % (self.adapter.name, reason))
        return self.adapter.parse(raw, target, date)


class FandangoShapedSpecialCaseGateTests(unittest.TestCase):
    """The adapter must need NOTHING beyond base.py: driving it through a
    generic, adapter-agnostic harness must work, and transport failures
    from a fake http_get must propagate uncaught."""

    def test_full_poll_cycle_through_generic_harness(self):
        date = "2026-08-01"
        target = {"campground_id": "232447"}

        def fake_http_get(url, headers):
            self.assertIn("232447", url)
            return _payload(
                {"5391": ("A12", "STANDARD", {date + "T00:00:00Z": "Available"})}
            )

        harness = FakeKernelPollHarness(RecreationGovAdapter(), fake_http_get)
        observations = harness.poll_once(target, date)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["state"], STATE_AVAILABLE)
        self.assertEqual(observations[0]["unit_id"], "232447:5391:2026-08-01")

    def test_broken_pipe_from_transport_propagates_uncaught(self):
        def dead_socket_http_get(url, headers):
            raise BrokenPipeError("[PIPE] simulated dead kept-alive socket, errno=32")

        harness = FakeKernelPollHarness(RecreationGovAdapter(), dead_socket_http_get)

        with self.assertRaises(BrokenPipeError):
            harness.poll_once({"campground_id": "232447"}, "2026-08-01")

    def test_connection_reset_from_transport_propagates_uncaught(self):
        def reset_http_get(url, headers):
            raise ConnectionResetError("simulated connection reset by peer")

        harness = FakeKernelPollHarness(RecreationGovAdapter(), reset_http_get)

        with self.assertRaises(ConnectionResetError):
            harness.poll_once({"campground_id": "232447"}, "2026-08-01")

    def test_adapter_declares_only_base_py_politeness_attributes(self):
        adapter = RecreationGovAdapter()
        self.assertEqual(adapter.name, "recreation_gov")
        self.assertEqual(adapter.risk_tier, "green")
        self.assertEqual(adapter.min_spacing_seconds, 2.0)
        self.assertEqual(adapter.daily_request_budget, 5000)
        hints = adapter.cadence_hints()
        self.assertIsInstance(hints, dict)


if __name__ == "__main__":
    unittest.main()
