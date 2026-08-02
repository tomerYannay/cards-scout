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
import enrich
import final_report as fr
import manual_comps as mc
import product_research_parse as prp


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


# The Sapphire Gold's final state now lives in test_gate0.py
# (TestSapphireGoldFinalState). It has no valid comps at all, so it no
# longer appears in the valued ranking this module asserts over.


class TestSelfCompIdentity(unittest.TestCase):
    """A comp that IS the candidate's own listing is circular evidence."""

    def test_extracts_the_item_number_from_a_candidate_id(self):
        self.assertEqual(prp.ebay_item_number("v1|306942391283|0"), "306942391283")

    def test_extracts_the_item_number_from_a_bare_id(self):
        self.assertEqual(prp.ebay_item_number("306942391283"), "306942391283")

    def test_extracts_the_item_number_from_a_suffixed_id(self):
        self.assertEqual(prp.ebay_item_number("306942391283-23d2a0628b"),
                         "306942391283")

    def test_synthesized_and_malformed_ids_yield_nothing(self):
        for bad in ("pr-4faa8fd4b2", "", None, "abc", "v1||0", "12345"):
            self.assertIsNone(prp.ebay_item_number(bad), bad)

    def test_exact_item_match_is_a_self_comp(self):
        self.assertTrue(prp.is_self_comp("v1|306942391283|0", "306942391283"))
        self.assertTrue(prp.is_self_comp("v1|306942391283|0",
                                         "306942391283-23d2a0628b"))

    def test_different_items_are_not_self_comps(self):
        self.assertFalse(prp.is_self_comp("v1|306942391283|0", "306912070834"))
        self.assertFalse(prp.is_self_comp("v1|306942391283|0",
                                          "306912070834-77818f5b8e"))

    def test_a_shared_prefix_is_not_a_match(self):
        """3069423912 must never match 306942391283."""
        self.assertFalse(prp.is_self_comp("v1|3069423912|0", "306942391283"))
        self.assertFalse(prp.is_self_comp("v1|306942391283|0", "3069423912"))
        self.assertFalse(prp.is_self_comp("v1|306942391283|0", "3069423912834"))

    def test_missing_ids_never_exclude(self):
        """Identity must be provable; absence is not evidence."""
        self.assertFalse(prp.is_self_comp("v1|306942391283|0", None))
        self.assertFalse(prp.is_self_comp("v1|306942391283|0", "pr-abc123"))
        self.assertFalse(prp.is_self_comp(None, "306942391283"))
        self.assertFalse(prp.is_self_comp("", ""))

    def test_variation_suffix_on_the_candidate_is_ignored(self):
        for variation in ("0", "1", "12345"):
            self.assertTrue(prp.is_self_comp(f"v1|306942391283|{variation}",
                                             "306942391283"), variation)


class TestSelfCompClassification(unittest.TestCase):
    """Exclusion happens in the shared classifier, not in reporting."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates().get("v1|306942391283|0")

    def setUp(self):
        if self.cand is None:
            self.skipTest("Sapphire Gold not in the pool")
        self.title = ("2020 TOPPS CHROME FORMULA 1 SAPPHIRE EDITION GOLD #52 "
                      "GIULIANO ALESI 7/50 PSA 8")

    def test_own_listing_is_no_longer_silently_discarded(self):
        """Policy changed: an exact-item row is uncertain, not auto-rejected.

        Without UI text the gate cannot tell a prior completed sale from a
        duplicate of the live listing, so it is held for review rather than
        thrown away - discarding it once removed the cheapest comp and turned a
        WATCH into a BUY.
        """
        state, why = prp.classify_comp(self.cand, self.title,
                                       source_item_id="306942391283-23d2a0628b")
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("cancelled_or_relisted_sale", why)
        self.assertIn("306942391283", why)

    def test_own_listing_with_an_active_layout_is_a_duplicate(self):
        """With UI text, Gate 0 resolves it outright."""
        active = ("… GOLD #52 GIULIANO ALESI 7/50 PSA 8 Edit Sell Similar "
                  "$150.00 +$32.00 shipping - 0 - May 16, 2026")
        state, why = prp.classify_comp(self.cand, self.title,
                                       source_item_id="306942391283-23d2a0628b",
                                       raw_text=active)
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("same_listing_duplicate", why)

    def test_the_same_card_from_another_listing_is_still_accepted(self):
        """Same serial, different item id: a real second sale of the same card."""
        state, _why = prp.classify_comp(self.cand, self.title,
                                        source_item_id="306912070834-77818f5b8e")
        self.assertEqual(state, prp.ACCEPTED)

    def test_without_a_source_id_behaviour_is_unchanged(self):
        self.assertEqual(prp.classify_comp(self.cand, self.title)[0],
                         prp.ACCEPTED)
        self.assertEqual(
            prp.classify_comp(self.cand, self.title, source_item_id=None)[0],
            prp.ACCEPTED)

    def test_a_synthesized_id_does_not_exclude(self):
        self.assertEqual(
            prp.classify_comp(self.cand, self.title,
                              source_item_id="pr-4faa8fd4b2")[0], prp.ACCEPTED)

    def test_no_stored_self_comp_remains_accepted(self):
        conn = db.connect()
        bad = [r for r in conn.execute(
            "SELECT candidate_item_id, source_item_id FROM sold_comps "
            "WHERE accepted = 1")
            if prp.is_self_comp(r["candidate_item_id"], r["source_item_id"])]
        self.assertEqual(bad, [])


class TestShippingDiagnostic(unittest.TestCase):
    """Diagnostic only - it must never move a decision."""

    def diag(self, cand_ship, cand_price, comps):
        return fr.shipping_diagnostic(cand_ship, cand_price, comps)

    def test_exposes_every_required_field(self):
        d = self.diag(5.99, 150.0, [{"sold_price": 195.0, "shipping": 32.0}])
        for key in ("candidate_shipping", "median_comp_shipping",
                    "median_comp_item_price", "median_comp_total",
                    "total_cost_gap", "item_price_only_gap",
                    "shipping_contribution", "shipping_dominated", "reason"):
            self.assertIn(key, d)

    def test_shipping_contribution_is_the_postage_difference(self):
        d = self.diag(5.99, 150.0, [{"sold_price": 195.0, "shipping": 32.0}])
        self.assertAlmostEqual(d["shipping_contribution"], 32.0 - 5.99, places=6)

    def test_gap_decomposes_exactly(self):
        d = self.diag(5.99, 150.0, [{"sold_price": 195.0, "shipping": 32.0}])
        self.assertAlmostEqual(
            d["total_cost_gap"],
            d["item_price_only_gap"] + d["shipping_contribution"], places=6)

    def test_equal_shipping_contributes_nothing(self):
        d = self.diag(5.0, 100.0, [{"sold_price": 200.0, "shipping": 5.0}])
        self.assertEqual(d["shipping_contribution"], 0.0)
        self.assertFalse(d["shipping_dominated"])

    def test_dominated_when_most_of_the_gap_is_postage(self):
        # gap 30: item 10, shipping 20 -> 67%
        d = self.diag(0.0, 100.0, [{"sold_price": 110.0, "shipping": 20.0}])
        self.assertTrue(d["shipping_dominated"])
        self.assertAlmostEqual(d["shipping_share_of_gap"], 20 / 30)

    def test_boundary_exactly_at_the_threshold_is_dominated(self):
        # gap 20: item 10, shipping 10 -> exactly 50%
        d = self.diag(0.0, 100.0, [{"sold_price": 110.0, "shipping": 10.0}])
        self.assertAlmostEqual(d["shipping_share_of_gap"], fr.SHIPPING_DOMINANCE)
        self.assertTrue(d["shipping_dominated"])

    def test_boundary_just_below_the_threshold_is_not(self):
        # gap 20.02: item 10.02, shipping 10 -> 49.95%
        d = self.diag(0.0, 100.0, [{"sold_price": 110.02, "shipping": 10.0}])
        self.assertLess(d["shipping_share_of_gap"], fr.SHIPPING_DOMINANCE)
        self.assertFalse(d["shipping_dominated"])

    def test_no_discount_is_not_flagged(self):
        d = self.diag(5.0, 300.0, [{"sold_price": 100.0, "shipping": 5.0}])
        self.assertFalse(d["shipping_dominated"])
        self.assertEqual(d["reason"], "no discount to attribute")

    def test_no_comps_is_safe(self):
        d = self.diag(5.0, 100.0, [])
        self.assertFalse(d["shipping_dominated"])
        self.assertIsNone(d["median_comp_total"])

    def test_missing_shipping_on_a_comp_counts_as_zero(self):
        d = self.diag(0.0, 100.0, [{"sold_price": 150.0, "shipping": None}])
        self.assertEqual(d["median_comp_shipping"], 0.0)

    def test_it_does_not_appear_in_the_decision(self):
        """The flag is reporting-only; decide() has no notion of it."""
        r = dec.decide(150.0, [{"total_price": 227.0, "sale_date": "2026-05-01"}] * 3,
                       shipping=5.99)
        self.assertNotIn("shipping_dominated", r)
