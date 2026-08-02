"""Tests for the eBay Product Research CSV adapter.

Every fixture is synthetic: titles describe the pilot cards, but all prices,
dates, item numbers and URLs are invented placeholders on example.invalid.
Nothing here is a claimed real sale.

Run:  python -m unittest -v test_ebay_import
"""

import os
import tempfile
import unittest

import db
import ebay_product_research_import as adapter
import enrich
import manual_comps as mc
from test_manual_comps import CAND, NUMBERED

JORDAN = "1991 HOOPS #536 MICHAEL JORDAN PSA 8"


def write(tmp, text, name="export.csv"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


STANDARD = (
    "Title,Sold Price,Shipping Cost,Date Sold,Item Number,Condition,Item URL,Currency\n"
    f'"{JORDAN}",111.11,4.99,2026-06-15,900000000001,Graded,'
    "https://example.invalid/itm/900000000001,USD\n"
    f'"{JORDAN}",122.22,0.00,2026-06-20,900000000002,Graded,'
    "https://example.invalid/itm/900000000002,USD\n"
)


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_normal_header(self):
        idx, header, mapping, _ = adapter.detect(write(self.tmp, STANDARD))
        self.assertEqual(idx, 0)
        for f in ("raw_title", "sold_price", "shipping", "sale_date",
                  "source_item_id", "condition", "source_reference", "currency"):
            self.assertIn(f, mapping)

    def test_preamble_before_header_is_skipped(self):
        text = ("eBay Product Research export\n"
                "Date range:,last 90 days\n"
                "\n" + STANDARD)
        idx, _h, mapping, _ = adapter.detect(write(self.tmp, text))
        self.assertEqual(idx, 3)
        self.assertIn("raw_title", mapping)

    def test_renamed_columns_recognized(self):
        text = ("Listing title,Average sold price,Avg shipping cost,Sale date,Item ID\n"
                f'"{JORDAN}",111.11,4.99,2026-06-15,900000000003\n')
        _i, _h, mapping, _ = adapter.detect(write(self.tmp, text))
        self.assertIn("raw_title", mapping)
        self.assertIn("sold_price", mapping)
        self.assertIn("shipping", mapping)

    def test_missing_required_column_raises(self):
        text = "Seller,Date Sold\nsomeseller,2026-06-15\n"
        with self.assertRaises(adapter.AdapterError):
            adapter.detect(write(self.tmp, text))

    def test_no_header_at_all_raises(self):
        with self.assertRaises(adapter.AdapterError):
            adapter.detect(write(self.tmp, "just,some,junk\n1,2,3\n"))


class TestTranslate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def rows(self, text):
        return adapter.translate(write(self.tmp, text))[0]

    def test_basic_translation(self):
        rows = self.rows(STANDARD)
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual(r["raw_title"], JORDAN)
        self.assertEqual(r["sold_price"], "111.11")
        self.assertEqual(r["shipping"], "4.99")
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["source"], adapter.SOURCE)
        self.assertEqual(r["source_item_id"], "900000000001")
        self.assertEqual(r["actual_price_known"], "true")

    def test_missing_optional_columns_tolerated(self):
        text = ("Title,Sold Price\n"
                f'"{JORDAN}",111.11\n')
        r = self.rows(text)[0]
        self.assertEqual(r["shipping"], "")
        self.assertEqual(r["sale_date"], "")
        self.assertEqual(r["condition"], "")

    def test_currency_symbols_preserved_not_converted(self):
        text = ("Title,Sold Price\n"
                f'"{JORDAN}","£99.00"\n')
        r = self.rows(text)[0]
        self.assertEqual(r["currency"], "GBP")
        self.assertEqual(r["sold_price"], "99.0")

    def test_malformed_price_becomes_blank(self):
        text = ("Title,Sold Price\n"
                f'"{JORDAN}",n/a\n')
        self.assertEqual(self.rows(text)[0]["sold_price"], "")

    def test_thousands_separator_and_symbol(self):
        text = ("Title,Sold Price\n"
                f'"{JORDAN}","$1,234.56"\n')
        self.assertEqual(self.rows(text)[0]["sold_price"], "1234.56")

    def test_malformed_date_passed_through_for_matcher_to_null(self):
        text = ("Title,Sold Price,Date Sold\n"
                f'"{JORDAN}",111.11,not-a-date\n')
        self.assertEqual(self.rows(text)[0]["sale_date"], "not-a-date")
        self.assertIsNone(mc.parse_date("not-a-date"))

    def test_total_only_export_backs_out_item_price(self):
        text = ("Title,Total Price,Shipping\n"
                f'"{JORDAN}",116.10,4.99\n')
        r = self.rows(text)[0]
        self.assertAlmostEqual(float(r["sold_price"]), 111.11, places=2)

    def test_total_only_without_shipping_leaves_price_unknown(self):
        text = ("Title,Total Price\n"
                f'"{JORDAN}",116.10\n')
        self.assertEqual(self.rows(text)[0]["sold_price"], "")

    def test_best_offer_without_accepted_price_is_not_valued(self):
        text = ("Title,Sold Price,Best Offer Accepted\n"
                f'"{JORDAN}",200.00,true\n')
        r = self.rows(text)[0]
        self.assertEqual(r["actual_price_known"], "false")
        self.assertEqual(r["sold_price"], "")            # never valued
        self.assertEqual(r["displayed_original_price"], "200.0")

    def test_best_offer_with_distinct_accepted_price_is_valued(self):
        text = ("Title,Sold Price,Original Price,Best Offer Accepted\n"
                f'"{JORDAN}",150.00,200.00,true\n')
        r = self.rows(text)[0]
        self.assertEqual(r["actual_price_known"], "true")
        self.assertEqual(r["sold_price"], "150.0")

    def test_blank_and_summary_rows_skipped(self):
        text = STANDARD + "\n,,,,,,,\n"
        self.assertEqual(len(self.rows(text)), 2)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        enrich.load_surnames(db.connect())
        self._orig = mc.load_candidates
        mc.load_candidates = lambda: {CAND["item_id"]: CAND, "num": NUMBERED}

    def tearDown(self):
        mc.load_candidates = self._orig

    def load(self, text, **kw):
        rows, _m, _h = adapter.translate(write(self.tmp, text))
        return mc.import_rows(self.conn, rows, attribute_by_title=True, **kw)

    def test_attribution_by_title_and_acceptance(self):
        stats = self.load(STANDARD)
        self.assertEqual(stats["rows"], 2)
        self.assertEqual(stats["accepted"], 2)
        rows = self.conn.execute(
            "SELECT * FROM sold_comps WHERE accepted=1").fetchall()
        self.assertTrue(all(r["candidate_item_id"] == CAND["item_id"]
                            for r in rows))

    def test_unmatchable_row_kept_for_audit(self):
        text = ("Title,Sold Price,Item Number\n"
                '"2020 SOMETHING ELSE #99 NOBODY PSA 10",50.00,900000000009\n')
        stats = self.load(text)
        self.assertEqual(stats["unattributed"], 1)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertEqual(r["candidate_item_id"], "<unattributed>")
        self.assertEqual(r["accepted"], 0)
        self.assertIn("no pilot candidate", r["rejection_reason"])

    def test_duplicates_deduplicated_across_imports(self):
        self.load(STANDARD)
        stats = self.load(STANDARD)
        self.assertEqual(stats["duplicate"], 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0], 2)

    def test_missing_shipping_totals_to_item_price(self):
        text = ("Title,Sold Price,Item Number\n"
                f'"{JORDAN}",111.11,900000000010\n')
        self.load(text)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertIsNone(r["shipping"])
        self.assertAlmostEqual(r["total_price"], 111.11, places=2)

    def test_non_usd_not_converted(self):
        text = ("Title,Sold Price,Item Number\n"
                f'"{JORDAN}","£99.00",900000000011\n')
        self.load(text)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertEqual(r["currency"], "GBP")
        self.assertIsNone(r["fx_rate"])
        self.assertIsNone(r["converted_total"])

    def test_best_offer_unknown_counted_and_excluded(self):
        text = ("Title,Sold Price,Best Offer Accepted,Item Number\n"
                f'"{JORDAN}",200.00,true,900000000012\n')
        stats = self.load(text)
        self.assertEqual(stats["accepted"], 0)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertEqual(r["accepted"], 0)

    def test_currencies_tracked(self):
        self.load(STANDARD)
        self.assertIn("USD", mc.CURRENCIES)


class TestNoNetwork(unittest.TestCase):
    def test_adapter_has_no_network_calls(self):
        import inspect
        src = inspect.getsource(adapter)
        for banned in ("requests", "urllib", "http.client", "socket"):
            self.assertNotIn(banned, src, banned)


if __name__ == "__main__":
    unittest.main()
