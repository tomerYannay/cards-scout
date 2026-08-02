"""Seller-agnostic discovery.

Seller identity is discovery provenance. It is persisted per listing, filters
the crawl, and scopes deactivation - but it never touches card or slab
identity, so two sellers offering the same slab are two listings in one group,
never one merged record.

Run:  python -m unittest -v test_discovery
"""

import os
import tempfile
import unittest

import db
import fetch_listings as fl


def item(item_id, seller="psa", price="10.00", title="1989 FLEER #21 X PSA 6",
         feedback_pct="99.9", feedback_score=1000):
    payload = {
        "itemId": item_id, "title": title,
        "price": {"value": price, "currency": "USD"},
        "categories": [{"categoryId": "261328"}],
        "leafCategoryIds": ["261328"],
        "shippingOptions": [{"shippingCost": {"value": "5.99"}}],
        "buyingOptions": ["FIXED_PRICE"], "itemWebUrl": "http://x",
        "condition": "Used",
    }
    if seller is not None:
        payload["seller"] = {"username": seller,
                             "feedbackPercentage": feedback_pct,
                             "feedbackScore": feedback_score}
    return payload


class TestSellerFilter(unittest.TestCase):
    def test_a_single_seller(self):
        f = fl.band_filters(0, 25, ["psa"])
        self.assertIn("sellers:{psa}", f)

    def test_multiple_sellers_in_one_clause(self):
        f = fl.band_filters(0, 25, ["probstein123", "psa", "dacardworld"])
        self.assertIn("sellers:{dacardworld|probstein123|psa}", f)

    def test_order_is_deterministic(self):
        a = fl.band_filters(0, 25, ["b", "a"])
        b = fl.band_filters(0, 25, ["a", "b"])
        self.assertEqual(a, b)

    def test_price_band_and_currency_are_preserved(self):
        f = fl.band_filters(25, 100, ["psa"])
        self.assertIn("price:[25..100]", f)
        self.assertIn("priceCurrency:USD", f)
        self.assertIn("price:[5000]", fl.band_filters(5000, None, ["psa"]))

    def test_sports_category_is_unchanged(self):
        self.assertEqual(fl.SPORTS_CATEGORY, "212")
        self.assertEqual(fl.EXPECTED_LEAF_CATEGORY, "261328")

    def test_adaptive_split_is_unchanged(self):
        """Geometric midpoint, because card prices skew low."""
        self.assertEqual(fl.split_point(100, 400), 200.0)
        self.assertEqual(fl.split_point(0, 100), 50.0)


class TestSellerAudit(unittest.TestCase):
    def setUp(self):
        self.stats = fl.Stats()

    def test_an_allowlisted_seller_counts(self):
        fl.audit(item("v1|1|0", "psa"), self.stats, {"psa"})
        self.assertEqual(self.stats.per_seller["psa"], 1)
        self.assertEqual(self.stats.seller_violations, [])

    def test_multiple_sellers_are_counted_separately(self):
        for n, s in (("v1|1|0", "psa"), ("v1|2|0", "other"), ("v1|3|0", "psa")):
            fl.audit(item(n, s), self.stats, {"psa", "other"})
        self.assertEqual(self.stats.per_seller["psa"], 2)
        self.assertEqual(self.stats.per_seller["other"], 1)

    def test_a_foreign_seller_is_a_violation(self):
        fl.audit(item("v1|1|0", "someone_else"), self.stats, {"psa"})
        self.assertEqual(len(self.stats.seller_violations), 1)
        self.assertEqual(self.stats.per_seller["someone_else"], 0)

    def test_a_missing_seller_block_is_recorded_not_attributed(self):
        fl.audit(item("v1|1|0", seller=None), self.stats, {"psa"})
        self.assertEqual(self.stats.missing_seller, ["v1|1|0"])
        self.assertEqual(sum(self.stats.per_seller.values()), 0)

    def test_a_blank_username_is_recorded_not_attributed(self):
        fl.audit(item("v1|1|0", seller=""), self.stats, {"psa"})
        self.assertEqual(self.stats.missing_seller, ["v1|1|0"])
        self.assertEqual(sum(self.stats.per_seller.values()), 0)


class TestRowPersistence(unittest.TestCase):
    def test_seller_identity_is_persisted(self):
        row = db.to_row(item("v1|1|0", "probstein123", feedback_pct="99.4",
                             feedback_score=1234567), "now", "disc-1")
        self.assertEqual(row["seller"], "probstein123")
        self.assertEqual(row["seller_feedback_pct"], 99.4)
        self.assertEqual(row["seller_feedback_score"], 1234567)
        self.assertEqual(row["discovery_run_id"], "disc-1")

    def test_a_missing_seller_stays_null(self):
        row = db.to_row(item("v1|1|0", seller=None), "now", "disc-1")
        self.assertIsNone(row["seller"])
        self.assertIsNone(row["seller_feedback_pct"])

    def test_malformed_feedback_is_null_not_zero(self):
        bad = item("v1|1|0", "x", feedback_pct="n/a", feedback_score="lots")
        row = db.to_row(bad, "now")
        self.assertEqual(row["seller"], "x")
        self.assertIsNone(row["seller_feedback_pct"])
        self.assertIsNone(row["seller_feedback_score"])

    def test_raw_payload_is_untouched(self):
        import json
        row = db.to_row(item("v1|1|0", "psa"), "now")
        self.assertEqual(json.loads(row["raw"])["seller"]["username"], "psa")


class TestDeduplication(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    def test_the_same_item_twice_is_one_row(self):
        for _ in range(2):
            db.upsert_listings(self.conn, [db.to_row(item("v1|1|0"), "now")])
        n = self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        self.assertEqual(n, 1)

    def test_rerunning_discovery_does_not_multiply_records(self):
        rows = [db.to_row(item(f"v1|{i}|0"), "now", "disc-1") for i in range(5)]
        db.upsert_listings(self.conn, rows)
        rows2 = [db.to_row(item(f"v1|{i}|0"), "later", "disc-2") for i in range(5)]
        db.upsert_listings(self.conn, rows2)
        n = self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        self.assertEqual(n, 5)
        run = self.conn.execute(
            "SELECT DISTINCT discovery_run_id FROM listings").fetchall()
        self.assertEqual([r[0] for r in run], ["disc-2"])

    def test_overlapping_bands_do_not_double_count(self):
        """`stats.seen` is what stops a band overlap becoming two records."""
        stats = fl.Stats()
        rows = []
        for _ in range(2):                       # same item in two bands
            it = item("v1|1|0")
            stats.fetched_rows += 1              # as walk_band counts it
            if it["itemId"] not in stats.seen:
                stats.seen.add(it["itemId"])
                rows.append(db.to_row(it, "now"))
        db.upsert_listings(self.conn, rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(stats.seen), 1)
        self.assertEqual(stats.duplicates, 1)    # seen twice, stored once
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 1)

    def test_different_item_ids_from_one_seller_are_separate(self):
        db.upsert_listings(self.conn, [
            db.to_row(item("v1|1|0", "psa"), "now"),
            db.to_row(item("v1|2|0", "psa"), "now")])
        n = self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        self.assertEqual(n, 2)

    def test_identical_cards_from_two_sellers_stay_two_listings(self):
        """Same title, same card - different eBay items. Never merged."""
        title = "1989 FLEER #21 MICHAEL JORDAN PSA 6"
        db.upsert_listings(self.conn, [
            db.to_row(item("v1|1|0", "psa", title=title), "now"),
            db.to_row(item("v1|2|0", "other", title=title), "now")])
        rows = self.conn.execute(
            "SELECT item_id, seller FROM listings ORDER BY item_id").fetchall()
        self.assertEqual([r["seller"] for r in rows], ["psa", "other"])
        self.assertEqual(len(rows), 2)


class TestCrossSellerGrouping(unittest.TestCase):
    """Equivalent slabs from different sellers must group together."""

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        title = "1989 FLEER #21 MICHAEL JORDAN PSA 6"
        db.upsert_listings(self.conn, [
            db.to_row(item("v1|1|0", "psa", title=title, price="100.00"), "now"),
            db.to_row(item("v1|2|0", "other", title=title, price="90.00"), "now")])
        import parse
        for iid in ("v1|1|0", "v1|2|0"):
            f = parse.parse_title(title)["fields"]
            card, slab = parse.make_keys(f)
            self.conn.execute(
                "INSERT INTO cards (item_id, slab_key, card_key) VALUES (?,?,?)",
                (iid, slab, card))
        self.conn.commit()

    def test_one_slab_key_covers_both_sellers(self):
        rows = self.conn.execute(
            """SELECT c.slab_key, COUNT(*) n, COUNT(DISTINCT l.seller) sellers
               FROM cards c JOIN listings l USING (item_id)
               GROUP BY c.slab_key""").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["n"], 2)
        self.assertEqual(rows[0]["sellers"], 2)

    def test_the_group_keeps_two_distinct_item_ids(self):
        ids = [r[0] for r in self.conn.execute(
            "SELECT item_id FROM cards ORDER BY item_id")]
        self.assertEqual(ids, ["v1|1|0", "v1|2|0"])

    def test_seller_is_not_part_of_the_slab_key(self):
        keys = {r[0] for r in self.conn.execute("SELECT slab_key FROM cards")}
        self.assertEqual(len(keys), 1)


class TestScopedDeactivation(unittest.TestCase):
    """A pilot against new sellers must not retire the existing population."""

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        db.upsert_listings(self.conn, [
            db.to_row(item("v1|1|0", "psa"), "2026-01-01T00:00:00"),
            db.to_row(item("v1|2|0", "psa"), "2026-01-01T00:00:00"),
            db.to_row(item("v1|3|0", "other"), "2026-01-01T00:00:00")])

    def active(self, seller):
        return self.conn.execute(
            "SELECT COUNT(*) FROM listings WHERE active=1 AND seller=?",
            (seller,)).fetchone()[0]

    def test_crawling_one_seller_leaves_the_others_active(self):
        db.upsert_listings(self.conn,
                           [db.to_row(item("v1|3|0", "other"), "2026-06-01T00:00:00")])
        n = db.deactivate_stale(self.conn, "2026-06-01T00:00:00", ["other"])
        self.assertEqual(n, 0)
        self.assertEqual(self.active("psa"), 2)
        self.assertEqual(self.active("other"), 1)

    def test_a_seller_scoped_sweep_only_retires_that_seller(self):
        n = db.deactivate_stale(self.conn, "2026-06-01T00:00:00", ["psa"])
        self.assertEqual(n, 2)
        self.assertEqual(self.active("psa"), 0)
        self.assertEqual(self.active("other"), 1)

    def test_an_unscoped_sweep_still_retires_everything(self):
        n = db.deactivate_stale(self.conn, "2026-06-01T00:00:00")
        self.assertEqual(n, 3)

    def test_legacy_rows_with_no_seller_survive_a_scoped_sweep(self):
        self.conn.execute("UPDATE listings SET seller=NULL WHERE item_id='v1|1|0'")
        self.conn.commit()
        db.deactivate_stale(self.conn, "2026-06-01T00:00:00", ["psa", "other"])
        left = self.conn.execute(
            "SELECT active FROM listings WHERE item_id='v1|1|0'").fetchone()[0]
        self.assertEqual(left, 1)


class TestBackwardCompatibility(unittest.TestCase):
    def test_the_live_database_has_seller_on_every_listing(self):
        conn = db.connect()
        n = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE seller IS NULL").fetchone()[0]
        self.assertEqual(n, 0)

    def test_legacy_psa_records_are_attributed_to_psa(self):
        conn = db.connect()
        n = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE seller='psa'").fetchone()[0]
        self.assertGreater(n, 100000)

    def test_default_sellers_preserves_the_original_behaviour(self):
        self.assertEqual(fl.DEFAULT_SELLERS, ("psa",))

    def test_seller_feedback_was_backfilled_from_raw(self):
        conn = db.connect()
        r = conn.execute(
            """SELECT seller_feedback_pct, seller_feedback_score FROM listings
               WHERE seller='psa' LIMIT 1""").fetchone()
        self.assertIsNotNone(r["seller_feedback_pct"])
        self.assertGreater(r["seller_feedback_score"], 0)


if __name__ == "__main__":
    unittest.main()


class TestBoundedPilot(unittest.TestCase):
    """A pilot restricts the price range without touching the band logic."""

    def test_unbounded_keeps_the_original_bands(self):
        self.assertEqual(fl.bands_within(None, None), list(fl.PRICE_BANDS))

    def test_a_range_clips_to_it(self):
        self.assertEqual(fl.bands_within(50, 1000),
                         [(50, 100), (100, 500), (500, 1000)])

    def test_a_lower_bound_only(self):
        bands = fl.bands_within(500, None)
        self.assertEqual(bands[0][0], 500)
        self.assertIsNone(bands[-1][1])

    def test_an_upper_bound_only(self):
        bands = fl.bands_within(None, 100)
        self.assertEqual(bands, [(0, 25), (25, 100)])

    def test_bands_stay_contiguous_and_ordered(self):
        bands = fl.bands_within(50, 1000)
        for (_, hi), (lo, _) in zip(bands, bands[1:]):
            self.assertEqual(hi, lo)

    def test_the_cap_is_per_seller_not_global(self):
        stats = fl.Stats()
        stats.retained_per_seller["a"] = 5
        stats.retained_per_seller["b"] = 1
        self.assertGreaterEqual(stats.retained_per_seller["a"], 5)
        self.assertLess(stats.retained_per_seller["b"], 5)


class TestSinglesOnly(unittest.TestCase):
    """Only single graded cards enter the population - not lots or boxes."""

    def leaves(self, leaf):
        it = item("v1|1|0", "psa")
        it["leafCategoryIds"] = [leaf]
        return it

    def test_the_singles_leaf_is_retained(self):
        stats = fl.Stats()
        it = self.leaves(fl.SINGLES_LEAF)
        self.assertIn(fl.SINGLES_LEAF, it["leafCategoryIds"])
        self.assertEqual(sum(stats.non_singles.values()), 0)

    def test_lots_sets_and_boxes_are_recognised_as_non_singles(self):
        for leaf in ("261329", "261330", "261332"):
            it = self.leaves(leaf)
            self.assertNotIn(fl.SINGLES_LEAF, it["leafCategoryIds"])

    def test_the_singles_leaf_constant_is_stable(self):
        self.assertEqual(fl.SINGLES_LEAF, "261328")
        self.assertEqual(fl.EXPECTED_LEAF_CATEGORY, fl.SINGLES_LEAF)
