"""Candidate selection picks the cheapest fully eligible exact peer.

Selection used to take each group's Tier B representative, which is an identity
decision applied to a price question. Across the 117 researched candidates in
an active peer group, that representative was the cheapest eligible member zero
times, with a median cost error of $196.00 and a maximum of $69,173.01.

Run:  python -m unittest -v test_cheapest_peer
"""

import json
import unittest

import db
import peers

SINGLES = ["261328"]


def payload(item_id, price, shipping="5.99", shipping_type="FIXED",
            currency="USD", options=("FIXED_PRICE",), leaf=None):
    item = {"itemId": item_id, "title": "1996 Topps #138 Kobe Bryant PSA 9",
            "buyingOptions": list(options),
            "leafCategoryIds": SINGLES if leaf is None else leaf}
    if price is not None:
        item["price"] = {"value": price, "currency": currency}
    if "AUCTION" in options:
        item["currentBidPrice"] = {"value": price, "currency": currency}
    if shipping is not None:
        item["shippingOptions"] = [{"shippingCostType": shipping_type,
                                    "shippingCost": {"value": shipping,
                                                     "currency": currency}}]
    else:
        item["shippingOptions"] = [{"shippingCostType": shipping_type}]
    return item


def member(item_id, price, active=1, parse_status="ok", **kw):
    """A group_members-shaped row."""
    item = payload(item_id, price, **kw)
    return {"item_id": item_id, "slab_key": "SLAB", "parse_status": parse_status,
            "title": item["title"], "price": float(price) if price else None,
            "shipping_cost": 5.99, "active": active,
            "raw": json.dumps(item), "print_run": None, "auto_grade": None,
            "grade_qualifier": None}


def eff_for(*item_ids, slab="SLAB", cls="verified"):
    return {i: {"eff_slab": slab, "cls": cls} for i in item_ids}


class TestCheapestEligibleSelection(unittest.TestCase):

    def test_the_cheapest_eligible_member_is_selected(self):
        ms = [member("b", "180.00"), member("a", "30.00"), member("c", "95.00")]
        eff = eff_for("a", "b", "c")
        iid, cost, _ = peers.cheapest_eligible(ms, eff, "SLAB")
        self.assertEqual(iid, "a")
        self.assertAlmostEqual(cost, 35.99)

    def test_selection_uses_acquisition_total_not_price_alone(self):
        """A lower sticker price with dearer shipping is not the cheapest."""
        cheap_sticker = member("a", "50.00", shipping="40.00")   # 90.00
        cheap_total = member("b", "60.00", shipping="5.00")      # 65.00
        iid, cost, _ = peers.cheapest_eligible([cheap_sticker, cheap_total],
                                               eff_for("a", "b"), "SLAB")
        self.assertEqual(iid, "b")
        self.assertAlmostEqual(cost, 65.00)

    def test_equal_totals_break_on_item_id_ascending(self):
        ms = [member("zzz", "30.00"), member("aaa", "30.00"),
              member("mmm", "30.00")]
        iid, _, _ = peers.cheapest_eligible(ms, eff_for("zzz", "aaa", "mmm"),
                                            "SLAB")
        self.assertEqual(iid, "aaa")

    def test_selection_is_deterministic_across_input_order(self):
        ms = [member("b", "30.00"), member("a", "30.00")]
        first = peers.cheapest_eligible(ms, eff_for("a", "b"), "SLAB")[0]
        second = peers.cheapest_eligible(list(reversed(ms)),
                                         eff_for("a", "b"), "SLAB")[0]
        self.assertEqual(first, second)


class TestEligibilityGates(unittest.TestCase):

    def reject_reason(self, bad, good_price="180.00"):
        ms = [bad, member("keep", good_price)]
        eff = eff_for(bad["item_id"], "keep")
        iid, _, rejected = peers.cheapest_eligible(ms, eff, "SLAB")
        return iid, dict(rejected)

    def test_a_cheaper_member_with_unknown_shipping_is_ineligible(self):
        bad = member("cheap", "30.00", shipping=None,
                     shipping_type="CALCULATED")
        iid, why = self.reject_reason(bad)
        self.assertEqual(iid, "keep")
        self.assertEqual(why["cheap"], "shipping_CALCULATED_UNKNOWN")

    def test_calculated_zero_shipping_is_still_ineligible(self):
        bad = member("cheap", "30.00", shipping="0.00",
                     shipping_type="CALCULATED")
        iid, why = self.reject_reason(bad)
        self.assertEqual(iid, "keep")
        self.assertEqual(why["cheap"], "shipping_CALCULATED_UNKNOWN")

    def test_an_inactive_member_is_ineligible(self):
        bad = member("cheap", "30.00", active=0)
        iid, why = self.reject_reason(bad)
        self.assertEqual(iid, "keep")
        self.assertEqual(why["cheap"], "not_active")

    def test_a_non_usd_member_is_ineligible(self):
        bad = member("cheap", "30.00", currency="CAD")
        iid, why = self.reject_reason(bad)
        self.assertEqual(iid, "keep")
        self.assertEqual(why["cheap"], "usd_not_proven")

    def test_an_auction_only_member_is_ineligible(self):
        bad = member("cheap", "30.00", options=("AUCTION",))
        iid, why = self.reject_reason(bad)
        self.assertEqual(iid, "keep")
        self.assertEqual(why["cheap"], "no_genuine_fixed_price")

    def test_an_auction_hybrid_is_ineligible(self):
        bad = member("cheap", "30.00", options=("FIXED_PRICE", "AUCTION"))
        iid, why = self.reject_reason(bad)
        self.assertEqual(iid, "keep")
        self.assertEqual(why["cheap"], "no_genuine_fixed_price")

    def test_a_non_single_card_member_is_ineligible(self):
        bad = member("cheap", "30.00", leaf=["261329"])   # Lots
        iid, why = self.reject_reason(bad)
        self.assertEqual(iid, "keep")
        self.assertEqual(why["cheap"], "not_single_card")

    def test_an_identity_ambiguous_member_is_ineligible(self):
        ms = [member("cheap", "30.00"), member("keep", "180.00")]
        eff = eff_for("cheap", "keep")
        eff["cheap"]["cls"] = "held_for_parallel_resolution"
        iid, _, rejected = peers.cheapest_eligible(ms, eff, "SLAB")
        self.assertEqual(iid, "keep")
        self.assertEqual(dict(rejected)["cheap"],
                         "identity_held_for_parallel_resolution")

    def test_an_unenriched_member_is_ineligible(self):
        ms = [member("cheap", "30.00"), member("keep", "180.00")]
        eff = eff_for("cheap", "keep")
        eff["cheap"]["cls"] = "not_enriched"
        iid, _, _ = peers.cheapest_eligible(ms, eff, "SLAB")
        self.assertEqual(iid, "keep")

    def test_a_member_with_a_different_slab_key_is_never_substituted(self):
        ms = [member("cheap", "30.00"), member("keep", "180.00")]
        eff = eff_for("cheap", "keep")
        eff["cheap"]["eff_slab"] = "OTHER_SLAB"
        iid, cost, rejected = peers.cheapest_eligible(ms, eff, "SLAB")
        self.assertEqual(iid, "keep")
        self.assertAlmostEqual(cost, 185.99)
        self.assertEqual(dict(rejected)["cheap"], "different_slab_identity")

    def test_a_group_with_no_eligible_member_selects_nothing(self):
        ms = [member("a", "30.00", active=0), member("b", "40.00", active=0)]
        iid, cost, rejected = peers.cheapest_eligible(ms, eff_for("a", "b"),
                                                      "SLAB")
        self.assertIsNone(iid)
        self.assertIsNone(cost)
        self.assertEqual(len(rejected), 2)

    def test_incomplete_cost_never_falls_back_to_price(self):
        """No shipping representation at all must not become a bare price."""
        bad = member("cheap", "30.00", shipping=None, shipping_type="")
        iid, why = self.reject_reason(bad)
        self.assertEqual(iid, "keep")
        self.assertEqual(why["cheap"], "shipping_NOT_RETURNED")


class TestProvenanceCannotOverridePrice(unittest.TestCase):

    def test_the_tier_b_representative_does_not_win_on_provenance(self):
        """The dearer member is the group's provenance representative."""
        ms = [member("provenance", "180.00"), member("cheaper", "30.00")]
        eff = eff_for("provenance", "cheaper")
        iid, cost, _ = peers.cheapest_eligible(ms, eff, "SLAB")
        self.assertNotEqual(iid, "provenance")
        self.assertEqual(iid, "cheaper")
        self.assertAlmostEqual(cost, 35.99)

    def test_provenance_still_wins_when_the_cheaper_peer_is_ineligible(self):
        ms = [member("provenance", "180.00"),
              member("cheaper", "30.00", shipping=None,
                     shipping_type="CALCULATED")]
        iid, _, _ = peers.cheapest_eligible(ms, eff_for("provenance", "cheaper"),
                                            "SLAB")
        self.assertEqual(iid, "provenance")


class TestOneCandidatePerSlabIdentity(unittest.TestCase):

    def test_two_original_groups_on_one_identity_yield_one_candidate(self):
        """Deduplication happens on the effective key, not the parsed one."""
        seen, chosen = set(), []
        for original_slab in ("SLAB_A", "SLAB_B"):
            ms = [member("a", "30.00"), member("b", "40.00")]
            eff = eff_for("a", "b", slab="EFFECTIVE")
            group_slab = eff["a"]["eff_slab"]
            if group_slab in seen:
                continue
            iid, _, _ = peers.cheapest_eligible(ms, eff, group_slab)
            seen.add(group_slab)
            chosen.append(iid)
        self.assertEqual(chosen, ["a"])


class TestDispersionIsOnlyAPrior(unittest.TestCase):

    def test_dispersion_is_computed_from_asking_prices(self):
        ms = [member("a", "30.00"), member("b", "180.00")]
        self.assertAlmostEqual(peers.dispersion_of(ms), 185.99 / 35.99, places=2)

    def test_dispersion_is_none_without_prices(self):
        self.assertIsNone(peers.dispersion_of([]))

    def test_dispersion_never_reaches_the_valuation(self):
        """It is carried as data; no decision code reads it."""
        import decision
        import inspect
        self.assertNotIn("dispersion_of", inspect.getsource(decision))


class TestEvidenceReuseByEffectiveSlabKey(unittest.TestCase):

    def setUp(self):
        import os
        import tempfile
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        for iid, key in (("x", "SLAB"), ("y", "OTHER")):
            self.conn.execute(
                "INSERT INTO tierb (item_id, effective_slab_key, fetched_at)"
                " VALUES (?,?,?)", (iid, key, "2026-08-04"))
        for iid, accepted in (("x", 1), ("x", 0), ("y", 1)):
            self.conn.execute(
                "INSERT INTO sold_comps (candidate_item_id, source, raw_title,"
                " imported_at, accepted, total_price) VALUES (?,?,?,?,?,?)",
                (iid, "product_research", "t", "2026-08-04", accepted, 60.0))
        self.conn.commit()

    def test_reuse_matches_on_exact_effective_slab_key(self):
        rows = peers.comps_for_slab(self.conn, "SLAB")
        self.assertEqual(len(rows), 1)

    def test_reuse_never_crosses_a_different_identity(self):
        self.assertEqual(len(peers.comps_for_slab(self.conn, "NOPE")), 0)

    def test_a_null_identity_reuses_nothing(self):
        self.assertEqual(peers.comps_for_slab(self.conn, None), [])

    def test_only_accepted_comps_are_reused(self):
        self.assertTrue(all(r["accepted"] == 1
                            for r in peers.comps_for_slab(self.conn, "SLAB")))

    def test_sold_comps_has_no_slab_key_column(self):
        """A derived key copied here would go stale on the next re-keying."""
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(sold_comps)")}
        self.assertNotIn("slab_key", cols)
        self.assertNotIn("effective_slab_key", cols)


class TestStabilityOutsideAffectedGroups(unittest.TestCase):

    def test_a_single_member_group_is_unchanged(self):
        ms = [member("only", "99.00")]
        iid, cost, rejected = peers.cheapest_eligible(ms, eff_for("only"),
                                                      "SLAB")
        self.assertEqual(iid, "only")
        self.assertAlmostEqual(cost, 104.99)
        self.assertEqual(rejected, [])

    def test_a_group_already_cheapest_first_is_unchanged(self):
        ms = [member("provenance", "30.00"), member("other", "180.00")]
        iid, _, _ = peers.cheapest_eligible(ms, eff_for("provenance", "other"),
                                            "SLAB")
        self.assertEqual(iid, "provenance")

    def test_total_helper_is_untouched(self):
        self.assertAlmostEqual(peers.total(member("a", "30.00")), 35.99)


if __name__ == "__main__":
    unittest.main()
