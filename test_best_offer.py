"""Best Offer safety guard.

A Best Offer sells for an amount eBay does not always publish. What IS shown may
be the original asking price or a struck-through figure. Substituting either
would inflate the market and make a candidate look cheap against a sale that
never happened at that price.

import_rows already refused such rows, but reclassification re-ran the matcher,
which knew nothing about reliability, and silently re-accepted them - so the
guard now lives in the shared classifier every path goes through.

Run:  python -m unittest -v test_best_offer
"""

import unittest

import db
import decision as dec
import enrich
import manual_comps as mc
import product_research_parse as prp

TITLE = "2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 22/199 PSA 9"
BEST_OFFER = ("… PSA 9 Edit Exclude listing $75.00 Best Offer $8.60 0% "
              "Free shipping 1 $75.00 - Jul 13, 2026")
FIXED = ("… PSA 9 Edit Exclude listing $75.00 Fixed price $8.60 0% "
         "Free shipping 1 $75.00 - Jul 13, 2026")
AUCTION = ("… PSA 9 Edit Exclude listing $45.00 Auction $5.99 0% "
           "Free shipping 1 $45.00 11 Sep 6, 2025")


class TestReliability(unittest.TestCase):
    """The pure predicate, independent of matching."""

    def R(self, **f):
        return prp.offer_price_is_reliable(f)

    def test_best_offer_with_a_known_accepted_price_is_reliable(self):
        self.assertTrue(self.R(best_offer=1, actual_price_known=1,
                               sold_price=75.0, total_price=83.60))

    def test_best_offer_with_price_explicitly_unknown_is_not(self):
        self.assertFalse(self.R(best_offer=1, actual_price_known=0))

    def test_best_offer_with_only_an_original_asking_price_is_not(self):
        self.assertFalse(self.R(best_offer=1, displayed_original_price=75.0,
                                sold_price=None))

    def test_best_offer_with_no_price_at_all_is_not(self):
        self.assertFalse(self.R(best_offer=1, sold_price=None, total_price=None))

    def test_sale_type_spelling_is_recognised(self):
        for spelling in ("BEST_OFFER", "Best Offer", "best_offer"):
            self.assertFalse(self.R(sale_type=spelling, actual_price_known=0),
                             spelling)

    def test_falsey_flags_are_all_understood(self):
        for falsey in (0, "0", "false", "False", "no", ""):
            self.assertFalse(self.R(best_offer=1, actual_price_known=falsey),
                             repr(falsey))

    # --- the regressions that must NOT change -----------------------------
    def test_fixed_price_is_always_reliable(self):
        self.assertTrue(self.R(sale_type="FIXED_PRICE", sold_price=75.0,
                               total_price=83.60))

    def test_auction_is_always_reliable(self):
        self.assertTrue(self.R(sale_type="AUCTION", sold_price=45.0,
                               total_price=50.99))

    def test_absent_fields_mean_reliable(self):
        """Every one of the 3,128 stored rows predates these fields."""
        self.assertTrue(self.R())
        self.assertTrue(prp.offer_price_is_reliable(None))

    def test_a_non_offer_row_with_no_price_is_not_this_guard_s_problem(self):
        """Gate 0 and the NULL-total rule handle that; this guard stays out."""
        self.assertTrue(self.R(sale_type="AUCTION", sold_price=None))


class TestClassification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates().get("v1|298544784209|0")

    def setUp(self):
        if self.cand is None:
            self.skipTest("Hasbulla candidate not in the pool")

    def C(self, raw_text, **fields):
        return prp.classify_comp(self.cand, TITLE, source_item_id="pr-x",
                                 raw_text=raw_text, persisted_fields=fields)

    def test_unreliable_offer_is_review_required_with_a_deterministic_reason(self):
        state, why = self.C(BEST_OFFER, best_offer=1, actual_price_known=0)
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertEqual(why, prp.UNRELIABLE_OFFER_REASON)

    def test_the_reason_is_identical_every_time(self):
        a = self.C(BEST_OFFER, best_offer=1, actual_price_known=0)[1]
        b = self.C(BEST_OFFER, best_offer=1, sold_price=None, total_price=None)[1]
        self.assertEqual(a, b)

    def test_only_the_original_price_available_is_not_accepted(self):
        state, _why = self.C(BEST_OFFER, best_offer=1,
                             displayed_original_price=75.0, sold_price=None)
        self.assertEqual(state, prp.REVIEW_REQUIRED)

    def test_a_reliable_offer_is_accepted_normally(self):
        state, why = self.C(BEST_OFFER, best_offer=1, actual_price_known=1,
                            sold_price=75.0, total_price=83.60)
        self.assertEqual(state, prp.ACCEPTED)
        self.assertIsNone(why)

    def test_fixed_price_regression(self):
        state, _why = self.C(FIXED, sale_type="FIXED_PRICE", sold_price=75.0,
                             total_price=83.60)
        self.assertEqual(state, prp.ACCEPTED)

    def test_auction_regression(self):
        state, _why = self.C(AUCTION, sale_type="AUCTION", sold_price=45.0,
                             total_price=50.99)
        self.assertEqual(state, prp.ACCEPTED)

    def test_rows_with_no_persisted_fields_behave_exactly_as_before(self):
        self.assertEqual(self.C(FIXED)[0], prp.ACCEPTED)
        self.assertEqual(prp.classify_comp(self.cand, TITLE,
                                           source_item_id="pr-x")[0],
                         prp.ACCEPTED)

    def test_the_guard_runs_before_gate_0(self):
        """An unreliable price is held even if the layout also looks wrong."""
        state, why = self.C("no markers here at all", best_offer=1,
                            actual_price_known=0)
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertEqual(why, prp.UNRELIABLE_OFFER_REASON)


class TestNoUnreliablePriceReachesAMedian(unittest.TestCase):
    """The end of the chain: what decide() will and will not average."""

    RECENT = "2026-06-01"

    def test_a_null_total_never_enters_the_median(self):
        comps = [{"total_price": 100.0, "sale_date": self.RECENT},
                 {"total_price": 100.0, "sale_date": self.RECENT},
                 {"total_price": 100.0, "sale_date": self.RECENT},
                 {"total_price": None, "sale_date": self.RECENT}]
        r = dec.decide(40.0, comps, shipping=0.0)
        self.assertEqual(r["comp_count"], 3)
        self.assertEqual(r["median_market_total"], 100.0)

    def test_a_null_total_does_not_inflate_confidence(self):
        priced = [{"total_price": 100.0, "sale_date": self.RECENT}] * 3
        nulls = [{"total_price": None, "sale_date": self.RECENT}] * 5
        a = dec.decide(40.0, priced, shipping=0.0)
        b = dec.decide(40.0, priced + nulls, shipping=0.0)
        self.assertEqual(a["confidence"], b["confidence"])
        self.assertEqual(a["comp_count"], b["comp_count"])

    def test_all_null_totals_are_insufficient_evidence(self):
        comps = [{"total_price": None, "sale_date": self.RECENT}] * 6
        r = dec.decide(40.0, comps, shipping=0.0)
        self.assertEqual(r["decision"], dec.PASS)
        self.assertEqual(r["reason"], dec.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(r["median_market_total"])

    def test_an_original_asking_price_could_never_have_been_used(self):
        """The guard holds the row; there is no path that swaps the number in."""
        state, _why = prp.classify_comp(
            mc.load_candidates().get("v1|298544784209|0") or {"item_id": "x"},
            TITLE, source_item_id="pr-x", raw_text=BEST_OFFER,
            persisted_fields={"best_offer": 1, "displayed_original_price": 999.0,
                              "sold_price": None})
        self.assertEqual(state, prp.REVIEW_REQUIRED)


class TestStoredEvidenceUnchanged(unittest.TestCase):
    def test_no_stored_row_is_a_best_offer(self):
        """The guard is new machinery; it must not disturb existing evidence."""
        conn = db.connect()
        n = conn.execute("SELECT COUNT(*) FROM sold_comps WHERE best_offer=1").fetchone()[0]
        self.assertEqual(n, 0)

    def test_every_accepted_stored_row_still_has_a_total(self):
        conn = db.connect()
        n = conn.execute("SELECT COUNT(*) FROM sold_comps "
                         "WHERE accepted=1 AND total_price IS NULL").fetchone()[0]
        self.assertEqual(n, 0)

    def test_the_93_run_baseline_is_preserved(self):
        """The corpus grows as new checkpoints run; the baseline must not shrink.

        Asserting a fixed row count would fail on every legitimate collection,
        so this pins what actually matters: the original 93 researched
        candidates are all still present with their evidence.
        """
        import json
        conn = db.connect()
        baseline = {c["item_id"] for c in json.load(open("pilot_candidates.json"))}
        self.assertEqual(len(baseline), 93)
        researched = {r[0] for r in conn.execute(
            "SELECT DISTINCT candidate_id FROM pr_runs")}
        self.assertTrue(baseline <= researched)
        self.assertGreaterEqual(
            conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0], 3128)
        rows = conn.execute(
            "SELECT COUNT(*) FROM sold_comps WHERE candidate_item_id IN "
            "(%s)" % ",".join("?" * len(baseline)), sorted(baseline)).fetchone()[0]
        self.assertEqual(rows, 3128)
