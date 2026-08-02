"""Tests for the economic report generator.

The first, hand-rolled version of this report contradicted itself: it showed a
candidate 24% BELOW market, reported one BUY at every hypothetical discount, and
still carried a "no buying opportunities" headline. Every check here targets one
of the defects that made that possible.

Run:  python -m unittest -v test_final_report
"""

import json
import unittest

import db
import decision as dec
import final_report as fr


class TestMarkupFormula(unittest.TestCase):
    """markup = (cost / median - 1) * 100. Negative means below market."""

    def test_below_market_is_negative(self):
        self.assertAlmostEqual(fr.markup_pct(155.99, 204.50), -23.72, places=2)

    def test_at_market_is_zero(self):
        self.assertEqual(fr.markup_pct(100.0, 100.0), 0.0)

    def test_above_market_is_positive(self):
        self.assertAlmostEqual(fr.markup_pct(200.0, 100.0), 100.0)

    def test_markup_is_the_exact_negative_of_the_engine_discount(self):
        """Both divide by the median, so markup == -gross_pct exactly.

        Worth pinning: a markup measured against COST instead would not be, and
        mixing the two conventions is how a "-24% markup" and a "24% discount"
        could be mistaken for different findings about different cards.
        """
        cost, median = 155.99, 204.50
        discount = (median - cost) / median * 100        # what decide() reports
        markup = fr.markup_pct(cost, median)             # what the report shows
        self.assertAlmostEqual(discount, 23.72, places=2)
        self.assertAlmostEqual(markup, -23.72, places=2)
        self.assertAlmostEqual(markup, -discount, places=9)
        against_cost = (median - cost) / cost * 100      # the other convention
        self.assertNotAlmostEqual(against_cost, discount, places=2)

    def test_no_median_gives_no_markup(self):
        self.assertIsNone(fr.markup_pct(100.0, None))
        self.assertIsNone(fr.markup_pct(100.0, 0))


class TestBands(unittest.TestCase):
    """Bands must be mutually exclusive and cover every markup."""

    def test_negative_markup_lands_below_market(self):
        for m in (-100.0, -23.72, -0.01, 0.0):
            self.assertEqual(fr.band_of(m), "at or below market", m)

    def test_each_band_is_exclusive(self):
        cases = {0.01: "above market 0-5%", 5.0: "above market 0-5%",
                 5.01: "above market 5-10%", 10.0: "above market 5-10%",
                 10.01: "above market 10-15%", 15.0: "above market 10-15%",
                 15.01: "above market 15-25%", 25.0: "above market 15-25%",
                 25.01: "above market 25-50%", 50.0: "above market 25-50%",
                 50.01: "above market over 50%", 13539.0: "above market over 50%"}
        for m, want in cases.items():
            self.assertEqual(fr.band_of(m), want, m)

    def test_every_label_is_reachable(self):
        seen = {fr.band_of(m) for m in
                (-1, 1, 7, 12, 20, 30, 100)}
        self.assertEqual(seen, set(fr.band_labels()) - {"at or below market"} | {"at or below market"})

    def test_bands_are_ordered(self):
        labels = fr.band_labels()
        self.assertEqual(labels[0], "at or below market")
        self.assertEqual(labels[-1], "above market over 50%")
        self.assertEqual(len(labels), len(set(labels)))


class TestLiveReport(unittest.TestCase):
    """The generated report must be internally consistent."""

    @classmethod
    def setUpClass(cls):
        cls.report = fr.build(db.connect())

    def test_no_consistency_problems(self):
        self.assertEqual(fr.check(self.report), [])

    def test_bands_total_the_valued_population(self):
        self.assertEqual(sum(self.report["markup_bands"].values()),
                         len(self.report["ranked_by_markup"]))

    def test_populations_total_the_candidate_count(self):
        h = self.report["headline"]
        self.assertEqual(h["valued"] + h["benchmark_only"] + h["no_benchmark"],
                         h["candidates"])

    def test_decisions_total_the_candidate_count(self):
        h = self.report["headline"]
        self.assertEqual(h["BUY"] + h["WATCH"] + h["PASS"], h["candidates"])

    def test_ranking_is_monotonic(self):
        m = [r["markup_percent"] for r in self.report["ranked_by_markup"]]
        self.assertEqual(m, sorted(m))

    def test_every_valued_candidate_has_a_gap(self):
        for r in self.report["ranked_by_markup"]:
            self.assertIsNotNone(r["gap_absolute"], r["candidate_id"])

    def test_benchmark_only_candidates_have_no_gap(self):
        """1-2 comps: a median exists but the engine refuses to value it."""
        for r in self.report["benchmark_only"]:
            self.assertIsNone(r["gap_absolute"], r["candidate_id"])
            self.assertIsNotNone(r["markup_percent"], r["candidate_id"])
            self.assertEqual(r["reason"], dec.INSUFFICIENT_EVIDENCE)
            self.assertLess(r["accepted_comps"], dec.MIN_COMPS)

    def test_no_benchmark_candidates_have_no_median(self):
        for r in self.report["no_benchmark"]:
            self.assertIsNone(r["median_comp_total"], r["candidate_id"])
            self.assertIsNone(r["markup_percent"], r["candidate_id"])

    def test_report_decision_matches_a_fresh_recomputation(self):
        """The report never carries a stale decision."""
        conn = db.connect()
        for r in (self.report["ranked_by_markup"] + self.report["benchmark_only"]
                  + self.report["no_benchmark"]):
            cid = r["candidate_id"]
            acc = conn.execute("""SELECT total_price, sale_date FROM sold_comps
                WHERE candidate_item_id=? AND accepted=1""", (cid,)).fetchall()
            L = conn.execute("SELECT active, price, shipping_cost FROM listings "
                             "WHERE item_id=?", (cid,)).fetchone()
            fresh = dec.decide(
                L["price"], [{"total_price": x["total_price"], "sale_date": x["sale_date"]}
                             for x in acc],
                shipping=L["shipping_cost"], listing_active=bool(L["active"]))
            self.assertEqual(r["decision"], fresh["decision"], cid)
            self.assertEqual(r["reason"], fresh["reason"], cid)


class TestHypotheticalDiscount(unittest.TestCase):
    """The modelled table must include today's real counts."""

    @classmethod
    def setUpClass(cls):
        cls.report = fr.build(db.connect())

    def test_actual_row_is_present(self):
        """Without it, "1 BUY at 5% off" reads as a contradiction."""
        self.assertIn("actual", self.report["hypothetical_discount"])
        self.assertEqual(
            self.report["hypothetical_discount"]["actual"]["discount_pct"], 0)

    def test_actual_row_matches_the_headline(self):
        h = self.report["headline"]
        actual = self.report["hypothetical_discount"]["actual"]
        self.assertEqual(actual["BUY"], h["BUY"])
        self.assertEqual(actual["WATCH"], h["WATCH"])

    def test_each_row_totals_the_valued_population(self):
        n = len(self.report["ranked_by_markup"])
        for label, row in self.report["hypothetical_discount"].items():
            self.assertEqual(row["BUY"] + row["WATCH"] + row["PASS"], n, label)

    def test_a_deeper_discount_never_reduces_buys(self):
        rows = sorted(self.report["hypothetical_discount"].values(),
                      key=lambda r: r["discount_pct"])
        buys = [r["BUY"] for r in rows]
        self.assertEqual(buys, sorted(buys))

    def test_thresholds_are_the_real_ones(self):
        self.assertEqual(dec.BUY_DISCOUNT, 0.25)
        self.assertEqual(dec.WATCH_DISCOUNT, 0.10)


class TestSapphireGoldReconciliation(unittest.TestCase):
    """The candidate that exposed the contradiction, pinned to its evidence."""

    CID = "v1|306942391283|0"

    @classmethod
    def setUpClass(cls):
        cls.report = fr.build(db.connect())
        cls.row = next((r for r in cls.report["ranked_by_markup"]
                        if r["candidate_id"] == cls.CID), None)

    def setUp(self):
        if self.row is None:
            self.skipTest("Sapphire Gold not in the pool")

    def test_it_is_the_only_candidate_below_market(self):
        below = [r for r in self.report["ranked_by_markup"]
                 if r["markup_percent"] <= 0]
        self.assertEqual([r["candidate_id"] for r in below], [self.CID])

    def test_it_is_a_watch_not_a_pass(self):
        self.assertEqual(self.row["decision"], dec.WATCH)
        self.assertEqual(self.row["reason"], dec.WATCH_MODERATE_DISCOUNT)

    def test_its_discount_sits_below_the_buy_threshold(self):
        self.assertGreaterEqual(self.row["gap_percent"], dec.WATCH_DISCOUNT * 100)
        self.assertLess(self.row["gap_percent"], dec.BUY_DISCOUNT * 100)

    def test_it_ranks_first(self):
        self.assertEqual(self.report["ranked_by_markup"][0]["candidate_id"],
                         self.CID)

    def test_shipping_is_what_kept_it_out_of_buy(self):
        """Price-only it clears 25%; shipping-inclusive it does not."""
        conn = db.connect()
        acc = conn.execute("""SELECT total_price, sale_date FROM sold_comps
            WHERE candidate_item_id=? AND accepted=1""", (self.CID,)).fetchall()
        comps = [{"total_price": r["total_price"], "sale_date": r["sale_date"]}
                 for r in acc]
        L = conn.execute("SELECT price, shipping_cost FROM listings WHERE item_id=?",
                         (self.CID,)).fetchone()
        self.assertEqual(dec.decide(L["price"], comps, shipping=0.0)["decision"],
                         dec.BUY)
        self.assertEqual(
            dec.decide(L["price"], comps, shipping=L["shipping_cost"])["decision"],
            dec.WATCH)


if __name__ == "__main__":
    unittest.main()
