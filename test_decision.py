"""Threshold tests for the conservative BUY / WATCH / PASS engine.

Run:  python -m unittest -v test_decision
"""

import datetime as dt
import unittest

import decision as dec

TODAY = dt.date(2026, 7, 31)
RECENT = "2026-06-01"          # ~2 months old
OLD = "2024-01-01"             # >12 months old


def comps(*totals, date=RECENT):
    return [{"total_price": t, "sale_date": date} for t in totals]


def run(asking, *totals, date=RECENT, active=True):
    return dec.decide(asking, comps(*totals, date=date), today=TODAY,
                      listing_active=active)


class TestBuyThreshold(unittest.TestCase):
    def test_deep_discount_is_buy(self):
        r = run(40.0, 100, 100, 100)
        self.assertEqual(r["decision"], dec.BUY)
        self.assertEqual(r["reason"], dec.BUY_DEEP_DISCOUNT)
        self.assertIsNone(r["downgrade_reason"])

    def test_exactly_25_percent_is_buy(self):
        """The boundary is inclusive."""
        r = run(75.0, 100, 100, 100)
        self.assertEqual(r["decision"], dec.BUY)
        self.assertAlmostEqual(r["gross_pct"], 25.0)

    def test_just_under_25_percent_is_watch(self):
        r = run(75.5, 100, 100, 100)
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertEqual(r["reason"], dec.WATCH_MODERATE_DISCOUNT)

    def test_buy_needs_three_comps(self):
        r = run(40.0, 100, 100)
        self.assertEqual(r["decision"], dec.PASS)
        self.assertEqual(r["reason"], dec.INSUFFICIENT_EVIDENCE)


class TestWatchThreshold(unittest.TestCase):
    def test_exactly_10_percent_is_watch(self):
        r = run(90.0, 100, 100, 100)
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertAlmostEqual(r["gross_pct"], 10.0)

    def test_just_under_10_percent_is_pass(self):
        r = run(90.5, 100, 100, 100)
        self.assertEqual(r["decision"], dec.PASS)
        self.assertEqual(r["reason"], dec.PASS_SHALLOW_DISCOUNT)

    def test_mid_range_is_watch(self):
        self.assertEqual(run(85.0, 100, 100, 100)["decision"], dec.WATCH)


class TestPass(unittest.TestCase):
    def test_at_market_is_pass(self):
        r = run(100.0, 100, 100, 100)
        self.assertEqual(r["decision"], dec.PASS)
        self.assertEqual(r["reason"], dec.PASS_AT_OR_ABOVE_MARKET)
        self.assertEqual(r["gross_gap"], 0)

    def test_premium_is_pass(self):
        r = run(150.0, 100, 100, 100)
        self.assertEqual(r["decision"], dec.PASS)
        self.assertEqual(r["reason"], dec.PASS_AT_OR_ABOVE_MARKET)
        self.assertLess(r["gross_pct"], 0)

    def test_large_premium_reports_negative_percentage(self):
        r = run(3200.0, 100, 110, 120)
        self.assertEqual(r["decision"], dec.PASS)
        self.assertLess(r["gross_pct"], -1000)


class TestInsufficientEvidence(unittest.TestCase):
    def test_zero_comps(self):
        r = dec.decide(40.0, [], today=TODAY)
        self.assertEqual((r["decision"], r["reason"]),
                         (dec.PASS, dec.INSUFFICIENT_EVIDENCE))
        self.assertIsNone(r["median_market_total"])
        self.assertEqual(r["confidence"], "NONE")

    def test_one_and_two_comps(self):
        for n in (1, 2):
            r = dec.decide(40.0, comps(*([100] * n)), today=TODAY)
            self.assertEqual(r["reason"], dec.INSUFFICIENT_EVIDENCE, n)
            self.assertEqual(r["decision"], dec.PASS)

    def test_unpriced_comps_do_not_count(self):
        rows = [{"total_price": None, "sale_date": RECENT} for _ in range(5)]
        r = dec.decide(40.0, rows, today=TODAY)
        self.assertEqual(r["comp_count"], 0)
        self.assertEqual(r["reason"], dec.INSUFFICIENT_EVIDENCE)

    def test_no_market_estimate_is_published_without_evidence(self):
        r = dec.decide(40.0, comps(100, 100), today=TODAY)
        self.assertIsNone(r["median_market_total"])
        self.assertIsNone(r["gross_gap"])


class TestAskingPriceGuards(unittest.TestCase):
    def test_missing_asking_price_is_pass(self):
        r = dec.decide(None, comps(100, 100, 100), today=TODAY)
        self.assertEqual((r["decision"], r["reason"]),
                         (dec.PASS, dec.NO_ASKING_PRICE))

    def test_zero_or_negative_asking_price_is_pass(self):
        for bad in (0, -5.0):
            r = dec.decide(bad, comps(100, 100, 100), today=TODAY)
            self.assertEqual(r["reason"], dec.NO_ASKING_PRICE, bad)

    def test_inactive_listing_is_pass(self):
        r = run(40.0, 100, 100, 100, active=False)
        self.assertEqual((r["decision"], r["reason"]),
                         (dec.PASS, dec.STALE_ASKING_PRICE))


class TestStaleComps(unittest.TestCase):
    def test_buy_with_old_comps_is_downgraded(self):
        r = run(40.0, 100, 100, 100, date=OLD)
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertEqual(r["downgrade_reason"], dec.DOWNGRADED_STALE_COMPS)

    def test_boundary_just_inside_twelve_months(self):
        just_in = (TODAY - dt.timedelta(days=364)).isoformat()
        self.assertEqual(run(40.0, 100, 100, 100, date=just_in)["decision"],
                         dec.BUY)

    def test_boundary_just_outside_twelve_months(self):
        just_out = (TODAY - dt.timedelta(days=366)).isoformat()
        r = run(40.0, 100, 100, 100, date=just_out)
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertEqual(r["downgrade_reason"], dec.DOWNGRADED_STALE_COMPS)

    def test_undated_comps_cannot_support_buy(self):
        rows = [{"total_price": 100, "sale_date": None} for _ in range(3)]
        r = dec.decide(40.0, rows, today=TODAY)
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertEqual(r["downgrade_reason"], dec.DOWNGRADED_STALE_COMPS)

    def test_stale_comps_do_not_downgrade_a_watch(self):
        """Recency only gates BUY; WATCH is unaffected."""
        r = run(85.0, 100, 100, 100, date=OLD)
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertIsNone(r["downgrade_reason"])


class TestDispersionGuard(unittest.TestCase):
    def test_dispersed_prices_downgrade_buy(self):
        r = run(40.0, 50, 100, 150)          # 150/50 = 3.0
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertEqual(r["downgrade_reason"], dec.DOWNGRADED_HIGH_DISPERSION)

    def test_dispersion_just_inside_limit_keeps_buy(self):
        r = run(40.0, 100, 100, 240)          # 2.4x
        self.assertEqual(r["decision"], dec.BUY)
        self.assertIsNone(r["downgrade_reason"])

    def test_dispersion_just_outside_limit_downgrades(self):
        r = run(40.0, 100, 100, 260)          # 2.6x
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertEqual(r["downgrade_reason"], dec.DOWNGRADED_HIGH_DISPERSION)

    def test_dispersion_is_reported(self):
        self.assertAlmostEqual(run(40.0, 50, 100, 150)["dispersion"], 3.0)

    def test_dispersion_never_upgrades_a_watch(self):
        r = run(85.0, 50, 100, 150)
        self.assertEqual(r["decision"], dec.WATCH)
        self.assertIsNone(r["downgrade_reason"])

    def test_dispersion_never_upgrades_a_pass(self):
        r = run(150.0, 50, 100, 150)
        self.assertEqual(r["decision"], dec.PASS)

    def test_zero_minimum_disables_dispersion_ratio(self):
        self.assertIsNone(dec.dispersion([0, 100, 200]))


class TestReporting(unittest.TestCase):
    def test_all_required_output_fields_present(self):
        r = run(40.0, 100, 100, 100)
        for key in ("decision", "reason", "asking_price", "median_market_total",
                    "gross_gap", "gross_pct", "comp_count", "confidence",
                    "most_recent_sale", "downgrade_reason", "min_total",
                    "max_total", "basis"):
            self.assertIn(key, r)

    def test_result_is_labelled_gross(self):
        self.assertIn("gross opportunity", run(40.0, 100, 100, 100)["basis"])
        self.assertIn("before taxes", run(40.0, 100, 100, 100)["basis"])

    def test_median_is_the_benchmark_not_the_mean(self):
        # mean would be 300; median is 100.
        r = run(40.0, 100, 100, 700)
        self.assertEqual(r["median_market_total"], 100)

    def test_confidence_scales_with_priced_comps(self):
        self.assertEqual(dec.confidence(0), "NONE")
        self.assertEqual(dec.confidence(1), "LOW")
        self.assertEqual(dec.confidence(3), "MEDIUM")
        self.assertEqual(dec.confidence(5), "HIGH")

    def test_format_decision_renders(self):
        text = dec.format_decision(run(40.0, 100, 100, 100))
        self.assertIn("DECISION       : BUY", text)
        self.assertIn("gross gap", text)


if __name__ == "__main__":
    unittest.main()
