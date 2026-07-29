"""Unit tests for the availability replay report. No network, no real files."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import report  # noqa: E402
import sentry  # noqa: E402


def ev(mins_ago, time_="6:00p", frm=None, to="soldout", date="2026-08-15"):
    at = (dt.datetime.now() - dt.timedelta(minutes=mins_ago))
    return {"at": at.isoformat(timespec="seconds"), "venue": "AABQI",
            "date": date, "time": time_, "from": frm, "to": to}


class TestLoadEvents(unittest.TestCase):
    def test_skips_torn_lines_and_sorts(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        with open(path, "w") as f:
            f.write(json.dumps(ev(10)) + "\n")
            f.write("{not json\n")
            f.write("\n")
            f.write(json.dumps(ev(90)) + "\n")
        try:
            rows = report.load_events(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(rows), 2)
        self.assertLess(rows[0]["at"], rows[1]["at"])

    def test_missing_file_is_empty(self):
        self.assertEqual(report.load_events("/nonexistent/history.jsonl"), [])


class TestAnalyze(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(report.analyze([]))

    def test_counts_reopenings_takings_and_survival(self):
        rows = [ev(180), ev(120, frm="soldout", to="available"),
                ev(90, frm="available", to="soldout")]
        a = report.analyze(rows)
        t = a["totals"]
        self.assertEqual(t["opened"], 1)
        self.assertEqual(t["taken"], 1)
        self.assertEqual(t["survivalSamples"], 1)
        self.assertAlmostEqual(t["medianSurvivalMin"], 30, delta=1)

    def test_first_sighting_is_not_counted_as_churn(self):
        a = report.analyze([ev(60, to="available")])
        self.assertEqual(a["totals"]["opened"], 0)

    def test_grid_carries_state_forward_to_end(self):
        a = report.analyze([ev(120), ev(60, frm="soldout", to="available")])
        self.assertEqual(a["grid"][0][-1], "a")
        self.assertEqual(a["grid"][0][0], "s")

    def test_slots_and_dates_deduped(self):
        rows = [ev(60, time_="6:00p"), ev(50, time_="9:00p"),
                ev(40, time_="6:00p", date="2026-08-16")]
        a = report.analyze(rows)
        self.assertEqual(a["totals"]["slots"], 3)
        self.assertEqual(a["totals"]["dates"], 2)


class TestRender(unittest.TestCase):
    def _render(self, rows):
        return report.render(report.analyze(rows), sentry.CFG)

    def test_self_contained_and_ascii_safe(self):
        html = self._render([ev(120), ev(60, frm="soldout", to="available")])
        markup = html.split("<script>")[0]
        self.assertFalse(any(ord(c) > 127 for c in markup))
        for bad in ("http://", "https://", "<img", "@import"):
            self.assertNotIn(bad, html)

    def test_empty_state_is_honest_not_broken(self):
        html = report.render(None, sentry.CFG)
        self.assertIn("Collecting", html)
        self.assertNotIn("Traceback", html)

    def test_theme_aware_both_directions(self):
        html = self._render([ev(60)])
        self.assertIn("prefers-color-scheme:dark", html)
        self.assertIn(':root[data-theme=dark]', html)
        self.assertIn(':root[data-theme=light]', html)

    def test_fade_is_opt_in_so_no_js_still_shows_content(self):
        html = self._render([ev(60)])
        self.assertIn(".js .fade{opacity:0", html)
        self.assertIn("classList.add('js')", html)

    def test_scrubber_matches_bucket_count(self):
        html = self._render([ev(120), ev(60, frm="soldout", to="available")])
        self.assertIn(f'max="{report.BUCKETS - 1}"', html)
        self.assertEqual(html.count('class="bar"'), report.BUCKETS)


if __name__ == "__main__":
    unittest.main()


class TestFindingBlock(unittest.TestCase):
    """The 'this feed cannot see sellouts' conclusion must be evidence-driven:
    it appears only while the data supports it and retracts itself if a real
    sellout ever shows up."""

    def _html(self, rows):
        return report.render(report.analyze(rows), sentry.CFG)

    def _many(self, to="available"):
        return [ev(100 - i, time_=f"{i}:00p", to=to) for i in range(1, 7)]

    def test_shown_when_nothing_ever_sold_out(self):
        html = self._html(self._many())
        self.assertIn("What this feed can and cannot see", html)
        self.assertIn("new dates going on", html)

    def test_retracts_itself_once_a_sellout_appears(self):
        rows = self._many()
        rows.append(ev(5, time_="1:00p", frm="available", to="soldout"))
        self.assertNotIn("What this feed can and cannot see", self._html(rows))

    def test_not_claimed_on_a_thin_sample(self):
        html = self._html([ev(60), ev(30)])
        self.assertNotIn("What this feed can and cannot see", html)


class TestStatHonesty(unittest.TestCase):
    """Stats must claim only what was measured: a flip to on-sale is not
    proof of a refund, and one observation is not a median."""

    def test_single_sample_is_not_called_a_median(self):
        rows = [ev(180), ev(120, frm="soldout", to="available"),
                ev(90, frm="available", to="soldout")]
        html = report.render(report.analyze(rows), sentry.CFG)
        self.assertIn("too few to average", html)
        self.assertNotIn("median time back on sale", html)
        self.assertNotIn("1 observations", html)  # plural agreement

    def test_median_claimed_once_samples_support_it(self):
        rows = [ev(300)]
        for i in range(3):
            rows.append(ev(200 - i * 40, time_=f"{i + 1}:00p",
                           frm="soldout", to="available"))
            rows.append(ev(190 - i * 40, time_=f"{i + 1}:00p",
                           frm="available", to="soldout"))
        html = report.render(report.analyze(rows), sentry.CFG)
        self.assertIn("median time back on sale", html)

    def test_does_not_assert_refunds_were_seen(self):
        html = report.render(report.analyze([ev(60), ev(30)]), sentry.CFG)
        self.assertIn("flips to on sale", html)
        self.assertNotIn("refunds seen", html)


class TestRestrictedNuance(unittest.TestCase):
    """'Refunds are invisible' is only claimable while nothing moves. Once a
    restricted flip is observed the page must soften to what was measured."""

    def _base(self):
        # available, not the helper's soldout default — a sellout would
        # (correctly) suppress the block outright
        return [ev(300 - i * 10, time_=f"{i}:00p", to="available")
                for i in range(1, 7)]

    def test_blunt_claim_only_when_nothing_moves(self):
        html = report.render(report.analyze(self._base()), sentry.CFG)
        self.assertIn("refunds cannot be", html)
        self.assertNotIn("unverified", html)

    def test_restricted_flip_softens_the_claim(self):
        rows = self._base()
        rows.append(ev(20, time_="1:00p", frm="available", to="restricted"))
        html = report.render(report.analyze(rows), sentry.CFG)
        self.assertNotIn("refunds cannot be", html)
        self.assertIn("unverified", html)
        self.assertIn("restricted", html)

    def test_plural_agreement_on_single_flip(self):
        rows = self._base()
        rows.append(ev(20, time_="1:00p", frm="available", to="restricted"))
        html = report.render(report.analyze(rows), sentry.CFG)
        self.assertIn("flipping 1 time on", html)


class TestSamplingCaveat(unittest.TestCase):
    def test_footer_states_the_resolution_limit(self):
        html = report.render(report.analyze([ev(60), ev(30)]), sentry.CFG)
        self.assertIn("Sampled every", html)
        self.assertIn("bounded by that interval", html)

    def test_caveat_tracks_the_configured_interval(self):
        cfg = dict(sentry.CFG, study_every_seconds=300)
        html = report.render(report.analyze([ev(60), ev(30)]), cfg)
        self.assertIn("Sampled every 5 min", html)


def evw(mins_ago, within, frm="soldout", to="available", time_="6:00p"):
    r = ev(mins_ago, time_=time_, frm=frm, to=to)
    r["within"] = within
    return r


class TestPrecisionAwareStats(unittest.TestCase):
    """Only well-pinned events may vote on 'when refunds land'."""

    def test_coarse_events_excluded_from_the_hour_chart(self):
        a = report.analyze([ev(300, to="soldout"),
                            evw(100, within=86400)])   # daily reconcile
        self.assertEqual(sum(a["byHour"]), 0)
        self.assertEqual(a["totals"]["coarseOpened"], 1)
        self.assertEqual(a["totals"]["preciseOpened"], 0)

    def test_precise_events_do_vote(self):
        a = report.analyze([ev(300, to="soldout"), evw(100, within=180)])
        self.assertEqual(sum(a["byHour"]), 1)
        self.assertEqual(a["totals"]["preciseOpened"], 1)

    def test_legacy_rows_without_within_are_treated_as_coarse(self):
        a = report.analyze([ev(300, to="soldout"),
                            ev(100, frm="soldout", to="available")])
        self.assertEqual(sum(a["byHour"]), 0)
        self.assertEqual(a["totals"]["coarseOpened"], 1)

    def test_page_explains_why_events_were_excluded(self):
        html = report.render(report.analyze(
            [ev(300, to="soldout"), evw(100, within=86400)]), sentry.CFG)
        self.assertIn("when the watcher looked", html)
        self.assertIn("once a day", html)

    def test_page_reports_the_precise_sample_size(self):
        html = report.render(report.analyze(
            [ev(300, to="soldout"), evw(100, within=120)]), sentry.CFG)
        self.assertIn("pinned to within", html)
