"""Gate 0 - a row must structurally represent a completed sale to be evidence.

Four of the PSA store's own ACTIVE listings were collected as if sold, priced at
their own asking price and dated to their own creation, and one became the
project's only BUY. Nothing downstream recovers from that: a valuation built on
asking prices is circular. These tests pin the gate that stops it.

Run:  python -m unittest -v test_gate0
"""

import unittest

import db
import decision as dec
import enrich
import manual_comps as mc
import product_research_parse as prp

# Layouts observed in the persisted corpus, trimmed to the tail that matters.
SOLD_FIXED = ("… PSA 9 Edit Sell Similar Exclude listing $75.00 Fixed price "
              "$8.60 0% Free shipping 1 $75.00 - Jul 13, 2026")
SOLD_AUCTION = ("… PSA 9 Edit Exclude listing $45.00 Auction $5.99 0% "
                "Free shipping 1 $45.00 11 Sep 6, 2025")
SOLD_BEST_OFFER = ("… PSA 9 Edit Exclude listing $54.48 Best Offer $5.52 0% "
                   "Free shipping 1 $54.48 - Jan 23, 2026")
ACTIVE_PANEL = ("… GOLD #52 GIULIANO ALESI 7/50 PSA 8 Edit Sell Similar "
                "$150.00 +$32.00 shipping - 0 - May 16, 2026")
TRUNCATED = ("… Michael Jordan Scoring Leaders PSA 10 HOF Edit Sell Similar "
             "Exclude listing - Page 1 Results per page 10 20 50")


class TestGateAcceptsGenuineSales(unittest.TestCase):
    def test_fixed_price_sold_row(self):
        v, s = prp.looks_like_sold_row(SOLD_FIXED)
        self.assertEqual(v, prp.SALE)
        self.assertTrue(s["has_exclude_marker"] and s["sale_format_in_text"])

    def test_auction_sold_row(self):
        self.assertEqual(prp.looks_like_sold_row(SOLD_AUCTION)[0], prp.SALE)

    def test_best_offer_sold_row(self):
        self.assertEqual(prp.looks_like_sold_row(SOLD_BEST_OFFER)[0], prp.SALE)

    def test_case_is_ignored(self):
        self.assertEqual(
            prp.looks_like_sold_row(SOLD_FIXED.upper())[0], prp.SALE)

    def test_a_recorded_sale_format_field_is_enough(self):
        """Future rows carry the format directly; no UI text needed."""
        v, s = prp.looks_like_sold_row("", {"sale_format": "Fixed price"})
        self.assertEqual(v, prp.SALE)
        self.assertEqual(s["sale_format_field"], "Fixed price")

    def test_manually_imported_rows_without_ui_text_still_pass(self):
        self.assertEqual(prp.looks_like_sold_row("")[0], prp.SALE)
        self.assertEqual(prp.looks_like_sold_row(None)[0], prp.SALE)


class TestGateRejectsActiveListings(unittest.TestCase):
    def test_active_listing_panel_is_not_a_sale(self):
        v, s = prp.looks_like_sold_row(ACTIVE_PANEL)
        self.assertEqual(v, prp.NOT_A_SALE)
        self.assertFalse(s["has_exclude_marker"])
        self.assertFalse(s["sale_format_in_text"])
        self.assertIn("Exclude listing", s["reason"])
        self.assertIn("sale format", s["reason"])

    def test_edit_and_sell_similar_are_not_evidence(self):
        """Both appear on genuine sold rows too; neither may decide anything."""
        self.assertIn("Edit", SOLD_FIXED)
        self.assertIn("Sell Similar", SOLD_FIXED)
        self.assertEqual(prp.looks_like_sold_row(SOLD_FIXED)[0], prp.SALE)
        self.assertIn("Edit", ACTIVE_PANEL)
        self.assertIn("Sell Similar", ACTIVE_PANEL)
        self.assertEqual(prp.looks_like_sold_row(ACTIVE_PANEL)[0], prp.NOT_A_SALE)

    def test_missing_exclude_marker_alone_fails(self):
        text = SOLD_FIXED.replace("Exclude listing ", "")
        v, s = prp.looks_like_sold_row(text)
        self.assertEqual(v, prp.NOT_A_SALE)
        self.assertIn("Exclude listing", s["reason"])

    def test_missing_sale_format_alone_fails(self):
        text = SOLD_FIXED.replace("Fixed price ", "")
        v, s = prp.looks_like_sold_row(text)
        self.assertEqual(v, prp.NOT_A_SALE)
        self.assertIn("sale format", s["reason"])

    def test_quantity_zero_disqualifies_outright(self):
        v, s = prp.looks_like_sold_row(SOLD_FIXED, {"quantity_sold": 0})
        self.assertEqual(v, prp.NOT_A_SALE)
        self.assertIn("quantity_sold is zero", s["reason"])

    def test_positive_quantity_does_not_rescue_a_bad_layout(self):
        self.assertEqual(
            prp.looks_like_sold_row(ACTIVE_PANEL, {"quantity_sold": 1})[0],
            prp.NOT_A_SALE)

    def test_null_quantity_is_absent_evidence_not_a_sale(self):
        """NULL on every pre-migration row; it must decide nothing."""
        for null in (None, "", "None"):
            self.assertEqual(
                prp.looks_like_sold_row(ACTIVE_PANEL, {"quantity_sold": null})[0],
                prp.NOT_A_SALE, repr(null))


class TestTruncatedRows(unittest.TestCase):
    def test_pagination_truncation_is_uncertain_not_rejected(self):
        v, s = prp.looks_like_sold_row(TRUNCATED)
        self.assertEqual(v, prp.UNCERTAIN)
        self.assertTrue(s["truncated_by_pagination"])
        self.assertIn("truncated", s["reason"])

    def test_a_persisted_field_resolves_a_truncated_row(self):
        v, _s = prp.looks_like_sold_row(TRUNCATED, {"sale_format": "Auction"})
        self.assertEqual(v, prp.SALE)

    def test_quantity_zero_beats_truncation(self):
        self.assertEqual(
            prp.looks_like_sold_row(TRUNCATED, {"quantity_sold": 0})[0],
            prp.NOT_A_SALE)


class TestEvidenceCategories(unittest.TestCase):
    CID = "v1|306942391283|0"

    def test_active_listing_of_the_same_item_is_never_a_prior_sale(self):
        cat = prp.evidence_category(self.CID, "306942391283-abc", prp.NOT_A_SALE)
        self.assertEqual(cat, prp.SAME_LISTING_DUPLICATE)
        self.assertNotEqual(cat, prp.SAME_ITEM_PRIOR_SALE)

    def test_active_listing_of_another_item_is_not_a_sale(self):
        self.assertEqual(
            prp.evidence_category(self.CID, "306912070834-x", prp.NOT_A_SALE),
            prp.NOT_A_SALE)

    def test_same_item_with_a_sold_layout_is_uncertain(self):
        """The candidate is active, so the card sold AND is listed again."""
        self.assertEqual(
            prp.evidence_category(self.CID, "306942391283-abc", prp.SALE),
            prp.CANCELLED_OR_RELISTED)

    def test_a_different_item_with_a_sold_layout_is_ordinary(self):
        self.assertEqual(
            prp.evidence_category(self.CID, "117197928888-x", prp.SALE),
            prp.ORDINARY_COMP)

    def test_an_unknown_source_id_is_ordinary(self):
        self.assertEqual(
            prp.evidence_category(self.CID, "pr-4faa8fd4b2", prp.SALE),
            prp.ORDINARY_COMP)


class TestClassifyCompGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates().get("v1|306942391283|0")

    def setUp(self):
        if self.cand is None:
            self.skipTest("Sapphire Gold not in the pool")
        self.title = ("2020 TOPPS CHROME FORMULA 1 SAPPHIRE EDITION GOLD #52 "
                      "GIULIANO ALESI 14/50 PSA 8")

    def test_an_active_listing_row_is_rejected_before_matching(self):
        state, why = prp.classify_comp(self.cand, self.title,
                                       source_item_id="306912070834-x",
                                       raw_text=ACTIVE_PANEL)
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("not_a_sale", why)

    def test_a_genuine_sold_row_still_accepts(self):
        state, _why = prp.classify_comp(self.cand, self.title,
                                        source_item_id="306912070834-x",
                                        raw_text=SOLD_FIXED)
        self.assertEqual(state, prp.ACCEPTED)

    def test_same_item_with_a_sold_layout_is_review_required(self):
        state, why = prp.classify_comp(self.cand, self.title,
                                       source_item_id="306942391283-x",
                                       raw_text=SOLD_FIXED)
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("cancelled_or_relisted_sale", why)

    def test_without_ui_text_the_gate_does_not_fire(self):
        self.assertEqual(
            prp.classify_comp(self.cand, self.title,
                              source_item_id="306912070834-x")[0], prp.ACCEPTED)


class TestPersistedCorpus(unittest.TestCase):
    """The gate, applied to every stored row."""

    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect()

    def rows(self):
        return self.conn.execute(
            """SELECT id, raw_text, sale_format, sale_type, quantity_sold,
                      accepted, total_price, candidate_item_id
               FROM sold_comps""").fetchall()

    def test_the_ten_known_false_rows_are_not_a_sale(self):
        known = {526, 527, 528, 529, 530, 531, 533, 534, 535, 536}
        for r in self.rows():
            v, _ = prp.looks_like_sold_row(
                r["raw_text"], {"sale_format": r["sale_format"],
                                "sale_type": r["sale_type"],
                                "quantity_sold": r["quantity_sold"]})
            if r["id"] in known:
                self.assertEqual(v, prp.NOT_A_SALE, r["id"])

    def test_no_not_a_sale_row_is_accepted(self):
        for r in self.rows():
            v, _ = prp.looks_like_sold_row(
                r["raw_text"], {"sale_format": r["sale_format"],
                                "sale_type": r["sale_type"],
                                "quantity_sold": r["quantity_sold"]})
            if v == prp.NOT_A_SALE:
                self.assertEqual(r["accepted"], 0, r["id"])

    def test_not_a_sale_never_reaches_a_median(self):
        """No accepted comp anywhere fails the gate."""
        bad = [r["id"] for r in self.rows() if r["accepted"] == 1
               and prp.looks_like_sold_row(
                   r["raw_text"], {"sale_format": r["sale_format"],
                                   "sale_type": r["sale_type"],
                                   "quantity_sold": r["quantity_sold"]})[0]
               != prp.SALE]
        self.assertEqual(bad, [])

    def test_the_vast_majority_are_genuine(self):
        n = sum(1 for r in self.rows()
                if prp.looks_like_sold_row(
                    r["raw_text"], {"sale_format": r["sale_format"],
                                    "sale_type": r["sale_type"],
                                    "quantity_sold": r["quantity_sold"]})[0]
                == prp.SALE)
        self.assertGreater(n, 3000)


class TestReviewRequiredDecision(unittest.TestCase):
    """The fourth state, and what may not produce it."""

    # Median 227 -> BUY at a cost of 155.99. Adding a 4th low comp moves the
    # median to (180+227)/2 = 203.5, which is only a WATCH.
    COMPS = [{"total_price": 180.0, "sale_date": "2026-05-01"},
             {"total_price": 227.0, "sale_date": "2026-05-01"},
             {"total_price": 227.0, "sale_date": "2026-05-01"}]

    def test_it_is_a_declared_state(self):
        self.assertIn(dec.REVIEW_REQUIRED, dec.DECISION_STATES)
        self.assertEqual(len(dec.DECISION_STATES), 4)

    def test_uncertain_evidence_that_changes_the_answer_triggers_it(self):
        # 3 comps at 227 -> BUY. Adding a 4th at 100 drags the median down.
        r = dec.decide(155.99, self.COMPS, shipping=0.0,
                       uncertain_comps=[{"total_price": 100.0,
                                         "sale_date": "2026-05-16"}])
        self.assertEqual(r["decision"], dec.REVIEW_REQUIRED)
        self.assertEqual(r["reason"], dec.UNCERTAIN_EVIDENCE_DECIDES)
        self.assertEqual(r["decision_without_uncertain"], dec.BUY)
        self.assertNotEqual(r["decision_with_uncertain"], dec.BUY)

    def test_uncertain_evidence_that_changes_nothing_does_not(self):
        r = dec.decide(155.99, self.COMPS, shipping=0.0,
                       uncertain_comps=[{"total_price": 230.0,
                                         "sale_date": "2026-05-16"}])
        self.assertEqual(r["decision"], dec.BUY)
        self.assertEqual(r["decision_with_uncertain"], dec.BUY)

    def test_uncertain_comps_never_enter_the_benchmark(self):
        base = dec.decide(155.99, self.COMPS, shipping=0.0)
        withu = dec.decide(155.99, self.COMPS, shipping=0.0,
                           uncertain_comps=[{"total_price": 1000.0,
                                             "sale_date": "2026-05-16"}])
        self.assertEqual(base["median_market_total"],
                         withu["median_market_total"])
        self.assertEqual(base["comp_count"], withu["comp_count"])

    def test_ordinary_insufficient_evidence_stays_pass(self):
        """Thin evidence is not uncertain evidence."""
        r = dec.decide(155.99, [{"total_price": 227.0, "sale_date": "2026-05-01"}],
                       shipping=0.0)
        self.assertEqual(r["decision"], dec.PASS)
        self.assertEqual(r["reason"], dec.INSUFFICIENT_EVIDENCE)

    def test_zero_comps_stays_pass(self):
        r = dec.decide(155.99, [], shipping=0.0)
        self.assertEqual(r["decision"], dec.PASS)
        self.assertEqual(r["reason"], dec.INSUFFICIENT_EVIDENCE)

    def test_existing_three_state_records_still_serialize(self):
        """Backward compatibility: no uncertain comps, no new keys required."""
        r = dec.decide(155.99, self.COMPS, shipping=0.0)
        self.assertIn(r["decision"], (dec.BUY, dec.WATCH, dec.PASS))
        self.assertNotIn("uncertain_comp_count", r)

    def test_all_four_states_format(self):
        for r in (dec.decide(40.0, self.COMPS, shipping=0.0),
                  dec.decide(200.0, self.COMPS, shipping=0.0),
                  dec.decide(1000.0, self.COMPS, shipping=0.0),
                  dec.decide(155.99, self.COMPS, shipping=0.0,
                             uncertain_comps=[{"total_price": 100.0,
                                               "sale_date": "2026-05-16"}])):
            text = dec.format_decision(r)
            self.assertIn(f"DECISION       : {r['decision']}", text)


class TestSapphireGoldFinalState(unittest.TestCase):
    """The candidate the whole correction came from."""

    CID = "v1|306942391283|0"

    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect()

    def test_it_has_no_accepted_comps(self):
        n = self.conn.execute(
            "SELECT COUNT(*) FROM sold_comps WHERE candidate_item_id=? "
            "AND accepted=1", (self.CID,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_all_four_of_its_rows_are_gate_rejected(self):
        rows = self.conn.execute(
            "SELECT rejection_reason FROM sold_comps WHERE candidate_item_id=?",
            (self.CID,)).fetchall()
        self.assertEqual(len(rows), 4)
        for r in rows:
            self.assertTrue("not_a_sale" in r[0] or "duplicate" in r[0], r[0])

    def test_it_is_neither_buy_nor_watch(self):
        acc = self.conn.execute(
            """SELECT total_price, sale_date FROM sold_comps
               WHERE candidate_item_id=? AND accepted=1""", (self.CID,)).fetchall()
        L = self.conn.execute(
            "SELECT active, price, shipping_cost FROM listings WHERE item_id=?",
            (self.CID,)).fetchone()
        d = dec.decide(L["price"],
                       [{"total_price": r["total_price"], "sale_date": r["sale_date"]}
                        for r in acc],
                       shipping=L["shipping_cost"],
                       listing_active=bool(L["active"]))
        self.assertNotIn(d["decision"], (dec.BUY, dec.WATCH))
        self.assertEqual(d["decision"], dec.PASS)
        self.assertEqual(d["reason"], dec.INSUFFICIENT_EVIDENCE)

    def test_no_misparsed_active_listing_contributes_to_any_median(self):
        """Structure is the gate; price/date coincidence is only a diagnostic.

        An item can be active AND have a genuine earlier sale - a seller with
        two copies, or a relist. Two such rows exist and are correctly kept.
        What must never survive is a row that is active AND priced at its own
        ask AND dated to its own creation: that is a listing, not a sale.
        """
        import json
        crawl = {}
        for r in self.conn.execute("SELECT item_id, price, active, raw FROM listings"):
            raw = json.loads(r["raw"]) if r["raw"] else {}
            crawl[r["item_id"].split("|")[1]] = (
                bool(r["active"]), r["price"], (raw.get("itemCreationDate") or "")[:10])
        bad = []
        for r in self.conn.execute(
                """SELECT id, source_item_id, sold_price, sale_date
                   FROM sold_comps WHERE accepted=1"""):
            n = prp.ebay_item_number(r["source_item_id"])
            if not n or n not in crawl:
                continue
            active, ask, created = crawl[n]
            price_eq = (ask is not None and r["sold_price"] is not None
                        and abs(r["sold_price"] - ask) < 0.005)
            date_eq = bool(created) and str(r["sale_date"])[:10] == created
            if active and price_eq and date_eq:
                bad.append(r["id"])
        self.assertEqual(bad, [])

    def test_genuine_sales_of_still_listed_items_are_kept(self):
        """A seller relisting or holding a second copy is not a fake comp."""
        kept = [r["id"] for r in self.conn.execute(
            "SELECT id FROM sold_comps WHERE accepted=1 AND id IN (1968, 2349)")]
        self.assertEqual(sorted(kept), [1968, 2349])


if __name__ == "__main__":
    unittest.main()
