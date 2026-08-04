"""Gate C readiness: mapping, shipping, currency, query and capacity.

Every fixture here is sanitized and offline. The shipping and price fixtures
reproduce exactly what the search/getItem comparison returned for item
v1|336710243332|0, where search said FIXED/0.00 and getItem said
CALCULATED/0.00 for the same listing, and getItem mirrored the 3.99 current bid
into `price` on an auction-only item.

Run:  python -m unittest -v test_gate_c_readiness
"""

import datetime as dt
import unittest

import db
import fetch_listings as fl
import gate_c_plan as plan

FETCHED = "2026-08-02T17:28:46.000Z"
USD = {"currency": "USD"}


def money(value, currency="USD"):
    return {"value": value, "currency": currency}


def item(**kw):
    base = {"itemId": "v1|1|0", "title": "PSA 10 card",
            "shippingOptions": [{"shippingCostType": "FIXED",
                                 "shippingCost": money("0.00")}]}
    base.update(kw)
    return base


class TestAuctionOnlyMapping(unittest.TestCase):
    """The getItem body mirrors currentBidPrice into `price`."""

    def setUp(self):
        self.row = db.to_row(item(
            itemId="v1|336710243332|0",
            buyingOptions=["AUCTION", "BEST_OFFER"],
            currentBidPrice=money("3.99"),
            price=money("3.99"),            # the mirror, not an asking price
            bidCount=0,
            minimumPriceToBid=money("3.99"),
            itemEndDate="2026-08-03T05:28:47.000Z"), FETCHED)

    def test_current_bid_maps_only_to_current_bid_price(self):
        self.assertEqual(self.row["current_bid_price"], 3.99)
        self.assertEqual(self.row["sale_format"], db.AUCTION_ONLY)

    def test_mirrored_price_populates_neither_price_column(self):
        self.assertIsNone(self.row["fixed_asking_price"])
        self.assertIsNone(self.row["price"])

    def test_acquisition_total_is_null_for_an_active_auction(self):
        self.assertIsNone(self.row["acquisition_total"])
        self.assertFalse(self.row["acquisition_total_complete"])

    def test_minimum_price_to_bid_is_never_read(self):
        """Not a reserve, not a final price, not an asking price.

        The live item had minimumPriceToBid == currentBidPrice, which would let
        a column legitimately derived from the bid pass this test by
        coincidence, so the fixture gives it a distinct value.
        """
        row = db.to_row(item(buyingOptions=["AUCTION"],
                             currentBidPrice=money("3.99"),
                             minimumPriceToBid=money("4.49")), FETCHED)
        for column, value in row.items():
            if column == "raw":
                continue
            self.assertNotEqual(value, 4.49,
                                f"{column} took minimumPriceToBid's value")
        self.assertEqual(row["reserve_state"], db.RESERVE_UNKNOWN)

    def test_bid_count_zero_stays_zero(self):
        self.assertEqual(self.row["bid_count"], 0)
        self.assertIsNotNone(self.row["bid_count"])

    def test_missing_bid_count_stays_null(self):
        row = db.to_row(item(buyingOptions=["AUCTION"],
                             currentBidPrice=money("3.99")), FETCHED)
        self.assertIsNone(row["bid_count"])


class TestHybridAndFixedPriceUnchanged(unittest.TestCase):

    def test_hybrid_keeps_bid_and_asking_price_distinct(self):
        row = db.to_row(item(buyingOptions=["FIXED_PRICE", "AUCTION"],
                             currentBidPrice=money("75.00"),
                             price=money("150.00")), FETCHED)
        self.assertEqual(row["sale_format"], db.AUCTION_WITH_FIXED)
        self.assertEqual(row["current_bid_price"], 75.00)
        self.assertEqual(row["fixed_asking_price"], 150.00)
        self.assertIsNone(row["acquisition_total"])

    def test_fixed_price_only_behaviour_is_unchanged(self):
        row = db.to_row(item(buyingOptions=["FIXED_PRICE"],
                             price=money("120.00")), FETCHED)
        self.assertEqual(row["sale_format"], db.FIXED_PRICE_ONLY)
        self.assertEqual(row["fixed_asking_price"], 120.00)
        self.assertEqual(row["price"], 120.00)
        self.assertIsNone(row["current_bid_price"])
        self.assertEqual(row["acquisition_total"], 120.00)
        self.assertTrue(row["acquisition_total_complete"])

    def test_fixed_price_with_paid_shipping_totals_correctly(self):
        row = db.to_row(item(buyingOptions=["FIXED_PRICE"],
                             price=money("120.00"),
                             shippingOptions=[{"shippingCostType": "FIXED",
                                               "shippingCost": money("5.50")}]),
                        FETCHED)
        self.assertEqual(row["acquisition_total"], 125.50)


class TestShippingRepresentations(unittest.TestCase):
    """One conservative rule per representation, stated in the docstring."""

    def state(self, option):
        return db.shipping_state_of(option)

    def test_calculated_with_zero(self):
        self.assertEqual(
            self.state({"shippingCostType": "CALCULATED",
                        "shippingCost": money("0.00")}),
            (db.SHIPPING_CALCULATED_UNKNOWN, None))

    def test_calculated_with_positive_amount(self):
        self.assertEqual(
            self.state({"shippingCostType": "CALCULATED",
                        "shippingCost": money("7.20")}),
            (db.SHIPPING_CALCULATED_UNKNOWN, None))

    def test_fixed_with_zero_is_genuinely_free(self):
        self.assertEqual(
            self.state({"shippingCostType": "FIXED",
                        "shippingCost": money("0.00")}),
            (db.SHIPPING_KNOWN, 0.0))

    def test_fixed_with_positive_amount(self):
        self.assertEqual(
            self.state({"shippingCostType": "FIXED",
                        "shippingCost": money("4.25")}),
            (db.SHIPPING_KNOWN, 4.25))

    def test_shipping_absent_entirely(self):
        self.assertEqual(self.state(None),
                         (db.SHIPPING_NOT_RETURNED, None))
        self.assertEqual(self.state({}),
                         (db.SHIPPING_NOT_RETURNED, None))

    def test_type_absent_is_never_known_even_with_a_value(self):
        """An unproven representation cannot license a KNOWN cost."""
        self.assertEqual(self.state({"shippingCost": money("3.00")}),
                         (db.SHIPPING_NOT_RETURNED, None))

    def test_calculated_row_carries_no_cost_and_no_totals(self):
        row = db.to_row(item(buyingOptions=["AUCTION"],
                             currentBidPrice=money("20.00"),
                             shippingOptions=[{"shippingCostType": "CALCULATED",
                                               "shippingCost": money("0.00")}]),
                        FETCHED)
        self.assertEqual(row["shipping_state"], db.SHIPPING_CALCULATED_UNKNOWN)
        self.assertIsNone(row["shipping_cost"])
        self.assertIsNone(row["acquisition_total"])
        self.assertFalse(row["acquisition_total_complete"])
        self.assertIsNone(row["provisional_bid_total"])

    def test_calculated_fixed_price_row_gets_no_acquisition_total(self):
        row = db.to_row(item(buyingOptions=["FIXED_PRICE"],
                             price=money("120.00"),
                             shippingOptions=[{"shippingCostType": "CALCULATED",
                                               "shippingCost": money("0.00")}]),
                        FETCHED)
        self.assertIsNone(row["acquisition_total"])
        self.assertFalse(row["acquisition_total_complete"])


class TestCurrencyHandling(unittest.TestCase):

    def test_usd_item_is_usd(self):
        self.assertTrue(db.is_usd(item(price=money("10.00"))))

    def test_non_usd_item_is_not_usd(self):
        self.assertFalse(db.is_usd(item(price=money("10.00", "CAD"))))

    def test_mixed_currency_item_is_not_usd(self):
        self.assertFalse(db.is_usd(item(
            price=money("10.00"),
            shippingOptions=[{"shippingCostType": "FIXED",
                              "shippingCost": money("3.00", "GBP")}])))

    def test_currency_absent_is_not_assumed_usd(self):
        self.assertFalse(db.is_usd({"itemId": "v1|1|0"}))

    def test_non_usd_cannot_enter_usd_valuation_logic(self):
        row = db.to_row(item(buyingOptions=["FIXED_PRICE"],
                             price=money("120.00", "CAD")), FETCHED)
        self.assertIsNone(row["acquisition_total"])
        self.assertFalse(row["acquisition_total_complete"])

    def test_non_usd_auction_gets_no_provisional_total(self):
        row = db.to_row(item(buyingOptions=["AUCTION"],
                             currentBidPrice=money("50.00", "CAD")), FETCHED)
        self.assertIsNone(row["provisional_bid_total"])

    def test_usd_only_splits_without_converting(self):
        keep, drop = fl.usd_only([item(itemId="a", price=money("1.00")),
                                  item(itemId="b", price=money("1.00", "EUR"))])
        self.assertEqual([i["itemId"] for i in keep], ["a"])
        self.assertEqual([i["itemId"] for i in drop], ["b"])


class TestGateCQuery(unittest.TestCase):

    START = dt.datetime(2026, 8, 3, 5, 28, 46, tzinfo=dt.timezone.utc)

    def test_price_currency_cannot_be_emitted_without_price(self):
        out = fl.auction_filters(self.START)
        self.assertFalse([f for f in out if f.startswith("priceCurrency")])

    def test_valid_price_and_currency_pair_serializes(self):
        out = fl.auction_filters(self.START, price_min=50, price_max=1000)
        self.assertIn("price:[50..1000]", out)
        self.assertIn("priceCurrency:USD", out)
        self.assertLess(out.index("price:[50..1000]"),
                        out.index("priceCurrency:USD"))

    def test_window_is_always_present_and_absolute(self):
        out = fl.auction_filters(self.START)
        self.assertIn("itemEndDate:[2026-08-03T05:28:46.000Z"
                      "..2026-08-04T05:28:46.000Z]", out)

    def test_naive_start_is_rejected(self):
        with self.assertRaises(ValueError):
            fl.auction_filters(dt.datetime(2026, 8, 3, 5, 28, 46))

    def test_warnings_fail_the_pilot(self):
        body = {"itemSummaries": [], "total": 333296, "warnings": [
            {"errorId": 12002,
             "message": "The priceCurrency filter value is invalid."}]}
        with self.assertRaises(fl.QueryRejected) as caught:
            fl.assert_query_accepted(body, ["priceCurrency:USD"])
        self.assertIn("12002", str(caught.exception))

    def test_errors_fail_the_pilot(self):
        with self.assertRaises(fl.QueryRejected):
            fl.assert_query_accepted({"errors": [{"errorId": 12001,
                                                  "message": "bad filter"}]})

    def test_a_clean_response_passes(self):
        fl.assert_query_accepted({"itemSummaries": [], "total": 10})


class TestCapacityPlan(unittest.TestCase):

    START = dt.datetime(2026, 8, 3, 5, 28, 46, tzinfo=dt.timezone.utc)

    def test_window_is_frozen_and_24_hours(self):
        start, end = plan.window(self.START)
        self.assertEqual((end - start).total_seconds(), 86400)

    def test_naive_window_start_is_rejected(self):
        with self.assertRaises(ValueError):
            plan.window(dt.datetime(2026, 8, 3, 5, 28, 46))

    def test_a_band_under_the_ceiling_is_not_split(self):
        self.assertEqual(plan.split_band(50, 1000, 9_999),
                         ([(50, 1000)], []))

    def test_an_oversized_band_splits_until_it_fits(self):
        bands, excluded = plan.split_band(50, 1000, 333_296)
        self.assertGreater(len(bands), 1)
        self.assertEqual(excluded, [])
        self.assertEqual(bands[0][0], 50)
        self.assertEqual(bands[-1][1], 1000)
        for (a, b), (c, _) in zip(bands, bands[1:]):
            self.assertLess(a, b)
            self.assertEqual(b, c)          # contiguous, no gaps

    def test_an_unsplittable_band_is_excluded_not_truncated(self):
        bands, excluded = plan.split_band(50, 50.5, 50_000)
        self.assertEqual(bands, [])
        self.assertEqual(excluded, [(50, 50.5, 50_000)])

    def test_an_exclusion_is_never_silently_dropped(self):
        bands, excluded = plan.split_band(50, 51, 10_000_000)
        self.assertEqual(bands, [])
        self.assertTrue(excluded, "an unpageable band vanished without record")

    def test_request_plan_respects_both_caps(self):
        bands, _ = plan.split_band(50, 1000, 333_296)
        reqs = plan.plan_requests(bands)
        self.assertLessEqual(sum(r["pages"] for r in reqs),
                             plan.MAX_SEARCH_REQUESTS)
        self.assertLessEqual(sum(r["max_items"] for r in reqs),
                             plan.MAX_DISCOVERY_ITEMS)

    def test_empty_band_list_plans_nothing(self):
        self.assertEqual(plan.plan_requests([]), [])


class TestCandidateSelection(unittest.TestCase):

    def row(self, item_id, end, fmt=db.AUCTION_ONLY, bid=10.0):
        return {"item_id": item_id, "end_time": end, "sale_format": fmt,
                "current_bid_price": bid}

    def test_selection_is_ordered_by_end_time_then_id(self):
        rows = [self.row("b", "2026-08-03T06:00:00.000Z"),
                self.row("a", "2026-08-03T06:00:00.000Z"),
                self.row("c", "2026-08-03T05:00:00.000Z")]
        sel, _ = plan.select_candidates(rows)
        self.assertEqual([r["item_id"] for r in sel], ["c", "a", "b"])

    def test_selection_is_deterministic_across_input_order(self):
        rows = [self.row(x, f"2026-08-03T0{i}:00:00.000Z")
                for i, x in enumerate("abcde")]
        first, _ = plan.select_candidates(rows)
        second, _ = plan.select_candidates(list(reversed(rows)))
        self.assertEqual([r["item_id"] for r in first],
                         [r["item_id"] for r in second])

    def test_already_discovered_ids_are_deduplicated(self):
        rows = [self.row("a", "2026-08-03T05:00:00.000Z"),
                self.row("b", "2026-08-03T06:00:00.000Z")]
        sel, rej = plan.select_candidates(rows, known_item_ids={"a"})
        self.assertEqual([r["item_id"] for r in sel], ["b"])
        self.assertIn(("a", "already_discovered"), rej)

    def test_researched_identities_are_deduplicated(self):
        rows = [self.row("a", "2026-08-03T05:00:00.000Z"),
                self.row("b", "2026-08-03T06:00:00.000Z")]
        sel, rej = plan.select_candidates(
            rows, researched_keys={"slab-1"},
            key_of=lambda r: "slab-1" if r["item_id"] == "a" else "slab-2")
        self.assertEqual([r["item_id"] for r in sel], ["b"])
        self.assertIn(("a", "identity_already_researched"), rej)

    def test_duplicate_identities_within_one_run_are_collapsed(self):
        rows = [self.row("a", "2026-08-03T05:00:00.000Z"),
                self.row("b", "2026-08-03T06:00:00.000Z")]
        sel, _ = plan.select_candidates(rows, key_of=lambda r: "same")
        self.assertEqual([r["item_id"] for r in sel], ["a"])

    def test_non_auctions_and_incomplete_rows_are_rejected(self):
        rows = [self.row("a", "2026-08-03T05:00:00.000Z",
                         fmt=db.FIXED_PRICE_ONLY),
                self.row("b", None),
                self.row("c", "2026-08-03T05:00:00.000Z", bid=None)]
        sel, rej = plan.select_candidates(rows)
        self.assertEqual(sel, [])
        self.assertEqual({r[1] for r in rej},
                         {"not_an_auction", "no_end_time", "no_current_bid"})

    def test_candidate_cap_is_enforced(self):
        rows = [self.row(f"i{i:03}", f"2026-08-03T05:00:{i:02}.000Z")
                for i in range(40)]
        sel, rej = plan.select_candidates(rows, limit=5)
        self.assertEqual(len(sel), 5)
        self.assertTrue(all(r[1] == "over_candidate_cap" for r in rej))

    def test_coverage_claim_disclaims_representativeness(self):
        claim = plan.coverage_claim([(50, 1000)], [], 60)
        self.assertIn("not a representative sample", claim)
        self.assertIn("no market-wide conclusion", claim)

    def test_coverage_claim_reports_excluded_bands(self):
        claim = plan.coverage_claim([(50, 1000)], [(50, 52)], 60)
        self.assertIn("excluded as unpageable", claim)

    def test_coverage_claim_describes_bands_served_not_the_nominal_range(self):
        bands, _ = plan.split_band(plan.BAND_MIN, plan.BAND_MAX, 333_296)
        served = [r["band"] for r in plan.plan_requests(bands)]
        claim = plan.coverage_claim(served, [], 60, total_bands=len(bands))
        self.assertIn(f"of {len(bands)} sub-bands", claim)
        self.assertIn("never queried", claim)

    def test_coverage_claim_with_nothing_served_claims_nothing(self):
        self.assertIn("establishes nothing", plan.coverage_claim([], [], 0))


class TestBandSpread(unittest.TestCase):
    """The caps afford far fewer pages than there are sub-bands."""

    def test_served_bands_span_the_whole_range(self):
        bands, _ = plan.split_band(plan.BAND_MIN, plan.BAND_MAX, 333_296)
        served = [r["band"] for r in plan.plan_requests(bands)]
        self.assertLess(len(served), len(bands))
        self.assertEqual(served[0][0], plan.BAND_MIN)
        top = max(b[1] for b in served)
        self.assertGreater(top, plan.BAND_MAX * 0.5,
                           "the spread never reached the upper half of the band")

    def test_the_plan_is_not_an_ascending_prefix(self):
        bands, _ = plan.split_band(plan.BAND_MIN, plan.BAND_MAX, 333_296)
        served = [r["band"] for r in plan.plan_requests(bands)]
        self.assertNotEqual(served, bands[:len(served)],
                            "the pilot would spend itself on the cheapest cards")

    def test_spread_is_deterministic(self):
        bands = [(i, i + 1) for i in range(64)]
        self.assertEqual(plan.spread_bands(bands, 10),
                         plan.spread_bands(bands, 10))

    def test_spread_returns_everything_when_it_fits(self):
        bands = [(1, 2), (2, 3)]
        self.assertEqual(plan.spread_bands(bands, 10), bands)

    def test_spread_of_zero_is_empty(self):
        self.assertEqual(plan.spread_bands([(1, 2)], 0), [])


class TestReservePolicy(unittest.TestCase):
    """Reserve is UNKNOWN and must stay that way until proven otherwise."""

    def test_reserve_is_unknown_on_every_auction_row(self):
        for options in (["AUCTION"], ["AUCTION", "BEST_OFFER"],
                        ["FIXED_PRICE", "AUCTION"]):
            row = db.to_row(item(buyingOptions=options,
                                 currentBidPrice=money("3.99"),
                                 minimumPriceToBid=money("3.99")), FETCHED)
            self.assertEqual(row["reserve_state"], db.RESERVE_UNKNOWN)

    def test_unknown_reserve_never_becomes_met_or_absent(self):
        self.assertNotIn(db.RESERVE_UNKNOWN,
                         (db.RESERVE_KNOWN_MET, db.RESERVE_KNOWN_NOT_MET))

    def test_an_auction_never_carries_an_actionable_total(self):
        """No acquisition_total means no executable price for BUY/WATCH."""
        row = db.to_row(item(buyingOptions=["AUCTION"],
                             currentBidPrice=money("3.99")), FETCHED)
        self.assertIsNone(row["acquisition_total"])
        self.assertFalse(row["acquisition_total_complete"])


if __name__ == "__main__":
    unittest.main()


class TestWindowAcceptability(unittest.TestCase):
    """eBay rejected a T0..T0+24h window with warning 12002.

    The Gate C pilot's first query was refused outright, which also explains
    the earlier unexplained near-unfiltered total of 2,430,520: that probe used
    the same start-at-now window, so its filter was dropped and it measured the
    unfiltered market.
    """

    NOW = dt.datetime(2026, 8, 4, 16, 39, 55, tzinfo=dt.timezone.utc)

    def test_a_window_starting_now_is_refused_locally(self):
        with self.assertRaises(plan.WindowRejected):
            plan.window(self.NOW, now=self.NOW)

    def test_a_window_starting_in_the_past_is_refused(self):
        with self.assertRaises(plan.WindowRejected):
            plan.window(self.NOW - dt.timedelta(hours=1), now=self.NOW)

    def test_an_unproven_short_lead_is_refused(self):
        with self.assertRaises(plan.WindowRejected):
            plan.window(self.NOW + dt.timedelta(hours=1), now=self.NOW)

    def test_the_proven_twelve_hour_lead_is_accepted(self):
        start, end = plan.window(self.NOW + plan.PROVEN_WINDOW_LEAD, now=self.NOW)
        self.assertEqual((end - start).total_seconds(), 86400)

    def test_displaced_window_matches_the_verified_probe_shape(self):
        start, end = plan.displaced_window(self.NOW)
        self.assertEqual(start, self.NOW + dt.timedelta(hours=12))
        self.assertEqual(end, self.NOW + dt.timedelta(hours=36))
        plan.assert_window_acceptable(start, self.NOW)

    def test_validation_is_skipped_when_no_now_is_supplied(self):
        """Offline planning against a fixed historical T0 stays possible."""
        start, end = plan.window(self.NOW)
        self.assertEqual((end - start).total_seconds(), 86400)
