"""Auction representation, mapped from ten real sanitized probe items.

The Gate C probe established by observation that an auction states its price in
currentBidPrice and never in `price`; that CALCULATED shipping returns no value
at discovery; that bidCount is always present and legitimately 0; and that no
reserve field is returned at all. These tests pin that behaviour.

Run:  python -m unittest -v test_auction_mapping
"""

import datetime as dt
import json
import os
import tempfile
import unittest

import db
import ebay_api
import fetch_listings as fl

FIXTURES = json.load(open("tests/fixtures/auction_probe_items.json"))


def fixture(item_id_tail):
    return next(f for f in FIXTURES if f["itemId"].endswith(item_id_tail))


def fixed_price_item(price="25.00", shipping="4.99"):
    return {"itemId": "v1|900000000001|0", "title": "1989 FLEER #21 X PSA 6",
            "categories": [{"categoryId": "261328"}], "leafCategoryIds": ["261328"],
            "buyingOptions": ["FIXED_PRICE"],
            "price": {"value": price, "currency": "USD"},
            "shippingOptions": [{"shippingCost": {"value": shipping,
                                                  "currency": "USD"},
                                 "shippingCostType": "FIXED"}],
            "seller": {"username": "psa", "feedbackPercentage": "99.9",
                       "feedbackScore": 1}}


class TestFormatNormalization(unittest.TestCase):
    def test_auction_only(self):
        self.assertEqual(db.normalize_sale_format(["AUCTION"]), db.AUCTION_ONLY)

    def test_auction_with_best_offer_is_still_auction_only(self):
        """BEST_OFFER is an offer channel, not a format."""
        self.assertEqual(db.normalize_sale_format(["AUCTION", "BEST_OFFER"]),
                         db.AUCTION_ONLY)

    def test_hybrid_auction_plus_fixed(self):
        self.assertEqual(db.normalize_sale_format(["FIXED_PRICE", "AUCTION"]),
                         db.AUCTION_WITH_FIXED)

    def test_fixed_price_only(self):
        self.assertEqual(db.normalize_sale_format(["FIXED_PRICE"]),
                         db.FIXED_PRICE_ONLY)
        self.assertEqual(db.normalize_sale_format(["FIXED_PRICE", "BEST_OFFER"]),
                         db.FIXED_PRICE_ONLY)

    def test_empty_or_unrecognised_is_unknown_not_fixed(self):
        for opts in ([], None, ["CLASSIFIED_AD"], ["AUCT_OFFER"]):
            self.assertEqual(db.normalize_sale_format(opts), db.FORMAT_UNKNOWN,
                             repr(opts))

    def test_order_and_case_do_not_matter(self):
        self.assertEqual(db.normalize_sale_format(["auction", "fixed_price"]),
                         db.AUCTION_WITH_FIXED)

    def test_every_probe_fixture_normalizes(self):
        got = {db.normalize_sale_format(f["buyingOptions"]) for f in FIXTURES}
        self.assertEqual(got, {db.AUCTION_ONLY, db.AUCTION_WITH_FIXED})


class TestShippingState(unittest.TestCase):
    def test_known_shipping(self):
        state, value = db.shipping_state_of(
            {"shippingCost": {"value": "5.99"}, "shippingCostType": "FIXED"})
        self.assertEqual((state, value), (db.SHIPPING_KNOWN, 5.99))

    def test_calculated_with_no_value(self):
        state, value = db.shipping_state_of({"shippingCostType": "CALCULATED"})
        self.assertEqual(state, db.SHIPPING_CALCULATED_UNKNOWN)
        self.assertIsNone(value)

    def test_no_shipping_representation_at_all(self):
        for opt in (None, {}):
            state, value = db.shipping_state_of(opt)
            self.assertEqual(state, db.SHIPPING_NOT_RETURNED)
            self.assertIsNone(value)

    def test_unknown_shipping_is_never_zero(self):
        for opt in ({"shippingCostType": "CALCULATED"}, None, {}):
            _state, value = db.shipping_state_of(opt)
            self.assertIsNone(value)
            self.assertNotEqual(value, 0.0)

    def test_free_shipping_is_zero_and_known(self):
        state, value = db.shipping_state_of(
            {"shippingCost": {"value": "0.00"}, "shippingCostType": "FIXED"})
        self.assertEqual((state, value), (db.SHIPPING_KNOWN, 0.0))


class TestPriceSemantics(unittest.TestCase):
    def test_auction_with_current_bid_and_no_price(self):
        r = db.to_row(fixture("407099853711|0"), "now")
        self.assertEqual(r["current_bid_price"], 100.0)
        self.assertEqual(r["current_bid_currency"], "USD")
        self.assertIsNone(r["fixed_asking_price"])
        self.assertIsNone(r["price"])

    def test_hybrid_preserves_both_prices(self):
        r = db.to_row(fixture("377371511454|0"), "now")
        self.assertEqual(r["current_bid_price"], 20.0)
        self.assertEqual(r["fixed_asking_price"], 30.0)
        self.assertEqual(r["price"], 30.0)
        self.assertEqual(r["sale_format"], db.AUCTION_WITH_FIXED)

    def test_current_bid_never_populates_the_fixed_asking_price(self):
        for f in FIXTURES:
            r = db.to_row(f, "now")
            if r["current_bid_price"] is not None and r["fixed_asking_price"] is not None:
                self.assertNotEqual(r["current_bid_price"], r["fixed_asking_price"], f["itemId"])
            if db.normalize_sale_format(f["buyingOptions"]) == db.AUCTION_ONLY:
                self.assertIsNone(r["fixed_asking_price"], f["itemId"])
                self.assertIsNone(r["price"], f["itemId"])

    def test_current_bid_never_becomes_an_acquisition_total(self):
        for f in FIXTURES:
            r = db.to_row(f, "now")
            self.assertIsNone(r["acquisition_total"], f["itemId"])
            self.assertEqual(r["acquisition_total_complete"], 0, f["itemId"])

    def test_provisional_bid_total_is_separate_and_labelled(self):
        r = db.to_row(fixture("278210961070|0"), "now")     # the one KNOWN shipping
        self.assertEqual(r["shipping_state"], db.SHIPPING_KNOWN)
        self.assertAlmostEqual(r["provisional_bid_total"], 13.99 + 5.99)
        self.assertIsNone(r["acquisition_total"])

    def test_no_provisional_total_without_known_shipping(self):
        r = db.to_row(fixture("407099853711|0"), "now")
        self.assertIsNone(r["provisional_bid_total"])


class TestBidCount(unittest.TestCase):
    def test_zero_bids_stay_zero(self):
        for f in FIXTURES:
            self.assertEqual(db.to_row(f, "now")["bid_count"], 0, f["itemId"])

    def test_missing_bid_count_stays_null(self):
        item = dict(fixture("407099853711|0"))
        item.pop("bidCount")
        self.assertIsNone(db.to_row(item, "now")["bid_count"])

    def test_zero_and_missing_are_distinguishable(self):
        zero = db.to_row(fixture("407099853711|0"), "now")["bid_count"]
        item = dict(fixture("407099853711|0")); item.pop("bidCount")
        missing = db.to_row(item, "now")["bid_count"]
        self.assertEqual(zero, 0)
        self.assertIsNone(missing)
        self.assertIsNot(zero, missing)


class TestReserve(unittest.TestCase):
    def test_every_probe_item_is_unknown(self):
        for f in FIXTURES:
            self.assertEqual(db.to_row(f, "now")["reserve_state"],
                             db.RESERVE_UNKNOWN, f["itemId"])

    def test_unknown_is_not_no_reserve(self):
        self.assertNotEqual(db.RESERVE_UNKNOWN, db.RESERVE_KNOWN_MET)
        self.assertEqual({db.RESERVE_KNOWN_MET, db.RESERVE_KNOWN_NOT_MET,
                          db.RESERVE_UNKNOWN},
                         {"KNOWN_MET", "KNOWN_NOT_MET", "UNKNOWN"})

    def test_no_probe_item_exposes_a_reserve_field(self):
        for f in FIXTURES:
            self.assertEqual([k for k in f if "reserve" in k.lower()], [])


class TestFixedPriceUnchanged(unittest.TestCase):
    """The 150,639 existing rows must keep their meaning exactly."""

    def test_fixed_price_maps_as_before(self):
        r = db.to_row(fixed_price_item(), "now")
        self.assertEqual(r["price"], 25.0)
        self.assertEqual(r["fixed_asking_price"], 25.0)
        self.assertIsNone(r["current_bid_price"])
        self.assertEqual(r["sale_format"], db.FIXED_PRICE_ONLY)

    def test_fixed_price_gets_a_complete_acquisition_total(self):
        r = db.to_row(fixed_price_item(), "now")
        self.assertAlmostEqual(r["acquisition_total"], 29.99)
        self.assertEqual(r["acquisition_total_complete"], 1)

    def test_fixed_price_with_unknown_shipping_is_incomplete(self):
        item = fixed_price_item()
        item["shippingOptions"] = [{"shippingCostType": "CALCULATED"}]
        r = db.to_row(item, "now")
        self.assertIsNone(r["acquisition_total"])
        self.assertEqual(r["acquisition_total_complete"], 0)

    def test_the_live_database_still_reads(self):
        conn = db.connect()
        n = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        self.assertEqual(n, 150639)
        legacy = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE price IS NOT NULL").fetchone()[0]
        self.assertEqual(legacy, 150639)


class TestRoundTrip(unittest.TestCase):
    """Isolated temp database only - the ten probe items never touch production."""

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        db.upsert_listings(self.conn, [db.to_row(f, "probe", "probe-run")
                                       for f in FIXTURES])

    def q(self, sql):
        return self.conn.execute(sql).fetchone()[0]

    def test_all_ten_persist(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM listings"), 10)

    def test_current_bid_and_end_time_survive(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM listings WHERE current_bid_price IS NOT NULL"), 10)
        self.assertEqual(self.q("SELECT COUNT(*) FROM listings WHERE end_time IS NOT NULL"), 10)

    def test_zero_bid_count_survives_as_zero(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM listings WHERE bid_count = 0"), 10)
        self.assertEqual(self.q("SELECT COUNT(*) FROM listings WHERE bid_count IS NULL"), 0)

    def test_raw_buying_options_are_retained(self):
        rows = self.conn.execute("SELECT raw_buying_options FROM listings").fetchall()
        self.assertTrue(all(r[0] for r in rows))
        self.assertIn("FIXED_PRICE,AUCTION",
                      {r[0] for r in rows})

    def test_unknown_states_survive_round_trip(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM listings "
                                "WHERE shipping_state = 'CALCULATED_UNKNOWN'"), 9)
        self.assertEqual(self.q("SELECT COUNT(*) FROM listings "
                                "WHERE reserve_state = 'UNKNOWN'"), 10)

    def test_hybrid_prices_are_not_collapsed(self):
        r = self.conn.execute("""SELECT current_bid_price, fixed_asking_price
            FROM listings WHERE sale_format = ?""", (db.AUCTION_WITH_FIXED,)).fetchone()
        self.assertEqual((r[0], r[1]), (20.0, 30.0))

    def test_absolute_end_time_round_trips(self):
        r = self.conn.execute("SELECT end_time FROM listings ORDER BY end_time").fetchone()[0]
        self.assertTrue(r.endswith("Z"))
        parsed = dt.datetime.fromisoformat(r.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed.tzinfo)

    def test_ending_order_is_monotonic(self):
        ends = [r[0] for r in self.conn.execute(
            "SELECT end_time FROM listings ORDER BY end_time")]
        self.assertEqual(ends, sorted(ends))


class TestEndingWindow(unittest.TestCase):
    START = dt.datetime(2026, 8, 2, 17, 0, 0, tzinfo=dt.timezone.utc)

    def test_serialization_is_iso8601_utc_with_milliseconds(self):
        self.assertEqual(fl.iso_utc(self.START), "2026-08-02T17:00:00.000Z")

    def test_a_naive_datetime_is_rejected(self):
        """Reading 17:00 as local would shift the whole window silently."""
        with self.assertRaises(ValueError):
            fl.iso_utc(dt.datetime(2026, 8, 2, 17, 0, 0))

    def test_a_non_utc_zone_is_converted(self):
        tz = dt.timezone(dt.timedelta(hours=3))
        self.assertEqual(fl.iso_utc(dt.datetime(2026, 8, 2, 20, 0, 0, tzinfo=tz)),
                         "2026-08-02T17:00:00.000Z")

    def test_the_24_hour_filter(self):
        self.assertEqual(
            fl.ending_window_filter(self.START),
            "itemEndDate:[2026-08-02T17:00:00.000Z..2026-08-03T17:00:00.000Z]")

    def test_the_window_length_is_configurable(self):
        self.assertIn("2026-08-02T23:00:00.000Z",
                      fl.ending_window_filter(self.START, hours=6))

    def test_full_filter_set(self):
        f = fl.auction_filters(self.START, 24, 50, 1000)
        self.assertIn("buyingOptions:{AUCTION}", f)
        self.assertIn("priceCurrency:USD", f)
        self.assertIn("price:[50..1000]", f)
        self.assertTrue(any(x.startswith("itemEndDate:[") for x in f))

    def test_it_does_not_rely_on_the_sort_alone(self):
        f = fl.auction_filters(self.START)
        self.assertTrue(any("itemEndDate" in x for x in f))


class TestPaginationCeiling(unittest.TestCase):
    def test_page_size_and_ceiling_are_the_documented_client_values(self):
        self.assertEqual(ebay_api.PAGE_SIZE, 200)
        self.assertEqual(ebay_api.MAX_OFFSET, 10_000)

    def test_max_reachable_items_per_query(self):
        self.assertEqual(ebay_api.MAX_OFFSET // ebay_api.PAGE_SIZE, 50)

    def test_deterministic_pilot_cap_selection(self):
        """A cap takes the first N in a deterministic order, never a sample."""
        items = [{"itemId": f"v1|{i:012d}|0"} for i in range(500)]
        cap = 40
        a = sorted(items, key=lambda x: x["itemId"])[:cap]
        b = sorted(list(reversed(items)), key=lambda x: x["itemId"])[:cap]
        self.assertEqual(a, b)
        self.assertEqual(len(a), cap)


class TestSearchResponseIsFullyAvailable(unittest.TestCase):
    """A silently-ignored filter is only visible in the response `warnings`.

    The Gate C verification request discarded everything except itemSummaries
    and total, so the one field that could have proved whether itemEndDate was
    applied was thrown away in-process. The client does return the whole body -
    the loss was in the probe script - so any future probe must retain it.
    """

    def test_search_page_returns_the_whole_response_body(self):
        import inspect
        src = inspect.getsource(ebay_api.search_page)
        self.assertIn("return resp.json()", src)
        self.assertNotIn("itemSummaries", src)

    def test_a_warnings_key_would_be_reachable(self):
        body = {"total": 5, "itemSummaries": [], "warnings": [
            {"errorId": 12001, "message": "The filter is not supported"}]}
        self.assertEqual(len(body.get("warnings") or []), 1)
        self.assertIn("not supported", body["warnings"][0]["message"])
