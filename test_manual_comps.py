"""Tests for the manual sold-comps import and matcher.

Run:  python -m unittest -v test_manual_comps
"""

import csv
import os
import tempfile
import unittest

import db
import enrich
import manual_comps as mc

# The 1991 Hoops Jordan pilot candidate, as exported.
CAND = {
    "item_id": "v1|117330553310|0", "title": "1991 HOOPS #536 MICHAEL JORDAN PSA 8",
    "asking_price": 423.0, "shipping": 5.99, "year": 1991,
    "manufacturer": "HOOPS", "set": "BASE", "subject": "MICHAEL JORDAN",
    "card_number": "536", "parallel": None, "serial_num": None,
    "print_run": None, "psa_grade": "8", "grade_type": "NUMERIC",
    "qualifier": None, "is_auto": 0, "auto_grade": None,
    "effective_identity": "x", "effective_slab_key": "x",
    "effective_card_key": "x", "group_members": [],
}

NUMBERED = dict(CAND, item_id="num", card_number="200", print_run=199,
                parallel="RED PRIZM", year=2023, manufacturer="PANINI",
                set="PRIZM UFC", subject="HASBULLA MAGOMEDOV", psa_grade="9")

AUTOED = dict(CAND, item_id="auto", is_auto=1, parallel="AUTOGRAPH",
              year=2024, manufacturer="PANINI", set="CONTENDERS",
              subject="JOE MILTON III", card_number="106", psa_grade="10")

QUALIFIED = dict(CAND, item_id="qual", qualifier="OC")


class TestMatcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())

    def test_exact_identity_accepted(self):
        ok, why, _ = mc.match(CAND, "1991 HOOPS #536 MICHAEL JORDAN PSA 8")
        self.assertTrue(ok, why)

    def test_grade_mismatch_rejected(self):
        ok, why, _ = mc.match(CAND, "1991 HOOPS #536 MICHAEL JORDAN PSA 9")
        self.assertFalse(ok)
        self.assertIn("PSA grade", why)

    def test_parallel_mismatch_rejected(self):
        ok, why, _ = mc.match(
            NUMBERED,
            "2023 PANINI PRIZM UFC GOLD PRIZM #200 HASBULLA MAGOMEDOV 5/199 PSA 9")
        self.assertFalse(ok)

    def test_print_run_mismatch_rejected(self):
        ok, why, _ = mc.match(
            NUMBERED,
            "2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 5/99 PSA 9")
        self.assertFalse(ok)
        self.assertIn("print run", why)

    def test_different_serial_same_print_run_accepted(self):
        ok, why, _ = mc.match(
            NUMBERED,
            "2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 5/199 PSA 9")
        self.assertTrue(ok, why)

    def test_autograph_mismatch_rejected(self):
        ok, why, _ = mc.match(
            AUTOED, "2024 PANINI CONTENDERS #106 JOE MILTON III PSA 10")
        self.assertFalse(ok)
        self.assertIn("autograph", why)

    def test_qualifier_mismatch_rejected(self):
        ok, why, _ = mc.match(QUALIFIED, "1991 HOOPS #536 MICHAEL JORDAN PSA 8")
        self.assertFalse(ok)
        self.assertIn("qualifier", why)

    def test_raw_card_rejected(self):
        ok, why, _ = mc.match(CAND, "1991 HOOPS #536 MICHAEL JORDAN")
        self.assertFalse(ok)
        self.assertIn("raw", why)

    def test_other_grader_rejected(self):
        ok, why, _ = mc.match(CAND, "1991 HOOPS #536 MICHAEL JORDAN BGS 8")
        self.assertFalse(ok)
        self.assertIn("BGS", why)

    def test_lot_rejected(self):
        for title in ("1991 HOOPS #536 MICHAEL JORDAN PSA 8 LOT OF 3",
                      "1991 HOOPS #536 MICHAEL JORDAN PSA 8 x2"):
            ok, why, _ = mc.match(CAND, title)
            self.assertFalse(ok, title)
            self.assertIn("lot", why)

    def test_reprint_and_digital_and_box_rejected(self):
        for title, frag in (
            ("1991 HOOPS #536 MICHAEL JORDAN PSA 8 REPRINT", "reprint"),
            ("1991 HOOPS #536 MICHAEL JORDAN PSA 8 DIGITAL", "digital"),
            ("1991 HOOPS SEALED HOBBY BOX PSA 8", "pack/box"),
        ):
            ok, why, _ = mc.match(CAND, title)
            self.assertFalse(ok, title)
            self.assertIn(frag, why)

    def test_wrong_card_number_rejected(self):
        ok, why, _ = mc.match(CAND, "1991 HOOPS #30 MICHAEL JORDAN PSA 8")
        self.assertFalse(ok)
        self.assertIn("card number", why)


def write_csv(path, rows, cols=None):
    cols = cols or (mc.REQUIRED_COLS + mc.OPTIONAL_COLS)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def base_row(**over):
    row = {c: "" for c in mc.REQUIRED_COLS + mc.OPTIONAL_COLS}
    row.update({
        "candidate_item_id": CAND["item_id"], "query_tier": "STRICT",
        "source": mc.SOURCE, "source_item_id": "1001",
        "raw_title": "1991 HOOPS #536 MICHAEL JORDAN PSA 8",
        "sold_price": "111.11", "shipping": "4.99", "currency": "USD",
        "sale_date": "2026-06-15", "condition": "Graded",
        "source_reference": "https://example.invalid/itm/1001",
    })
    row.update(over)
    return row


class TestImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dbp = os.path.join(self.tmp, "t.db")
        self.conn = db.connect(self.dbp)
        enrich.load_surnames(db.connect())
        self._orig = mc.load_candidates
        mc.load_candidates = lambda: {CAND["item_id"]: CAND, "num": NUMBERED}

    def tearDown(self):
        mc.load_candidates = self._orig

    def path(self, name="in.csv"):
        return os.path.join(self.tmp, name)

    def test_missing_required_column_is_fatal(self):
        p = self.path()
        write_csv(p, [{"candidate_item_id": CAND["item_id"]}],
                  cols=["candidate_item_id"])
        with self.assertRaises(SystemExit):
            mc.import_csv(self.conn, p)

    def test_accepts_exact_and_stores_raw(self):
        p = self.path()
        write_csv(p, [base_row()])
        stats = mc.import_csv(self.conn, p)
        self.assertEqual(stats["accepted"], 1)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertEqual(r["accepted"], 1)
        self.assertEqual(r["currency"], "USD")
        self.assertAlmostEqual(r["total_price"], 116.10, places=2)
        self.assertIsNotNone(r["raw_row"])

    def test_rejection_reason_retained(self):
        p = self.path()
        write_csv(p, [base_row(source_item_id="2002",
                               raw_title="1991 HOOPS #536 MICHAEL JORDAN PSA 9")])
        mc.import_csv(self.conn, p)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertEqual(r["accepted"], 0)
        self.assertIn("PSA grade", r["rejection_reason"])

    def test_duplicate_import_deduplicated(self):
        p = self.path()
        write_csv(p, [base_row(), base_row()])
        stats = mc.import_csv(self.conn, p)
        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0], 1)

    def test_missing_shipping_handled(self):
        p = self.path()
        write_csv(p, [base_row(shipping="")])
        mc.import_csv(self.conn, p)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertIsNone(r["shipping"])
        self.assertAlmostEqual(r["total_price"], 111.11, places=2)

    def test_unknown_best_offer_excluded_but_stored(self):
        p = self.path()
        write_csv(p, [base_row(best_offer_indicator="true",
                               actual_price_known="false",
                               displayed_original_price="200.00")])
        stats = mc.import_csv(self.conn, p)
        self.assertEqual(stats["best_offer_unknown"], 1)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertEqual(r["accepted"], 0)
        self.assertIn("Best Offer", r["rejection_reason"])
        self.assertAlmostEqual(r["displayed_original_price"], 200.0, places=2)

    def test_known_best_offer_accepted(self):
        p = self.path()
        write_csv(p, [base_row(best_offer_indicator="true",
                               actual_price_known="true")])
        stats = mc.import_csv(self.conn, p)
        self.assertEqual(stats["accepted"], 1)

    def test_currency_preserved(self):
        p = self.path()
        write_csv(p, [base_row(currency="GBP")])
        mc.import_csv(self.conn, p)
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertEqual(r["currency"], "GBP")
        self.assertIsNone(r["fx_rate"])          # not silently assumed to be 1:1
        self.assertIsNone(r["converted_total"])

    def test_date_formats(self):
        for given, want in (("2026-06-15", "2026-06-15"),
                            ("06/15/2026", "2026-06-15"),
                            ("Jun 15, 2026", "2026-06-15"),
                            ("garbage", None)):
            self.assertEqual(mc.parse_date(given), want)


class TestQueryGeneration(unittest.TestCase):
    def test_strict_contains_every_material_field(self):
        q = mc.query_terms(NUMBERED, "STRICT")
        for token in ("2023", "HASBULLA", "#200", "RED PRIZM", "/199", "PSA 9"):
            self.assertIn(token, q)

    def test_no_price_ever_appears_in_a_query(self):
        for tier in ("STRICT", "NORMAL", "RELAXED"):
            for cand in (CAND, NUMBERED, AUTOED):
                q = mc.query_terms(cand, tier)
                self.assertNotIn(str(int(cand["asking_price"])), q)

    def test_relaxed_blocked_for_material_identity(self):
        self.assertFalse(mc.relaxed_allowed(NUMBERED))   # print run
        self.assertFalse(mc.relaxed_allowed(AUTOED))     # autograph
        self.assertFalse(mc.relaxed_allowed(QUALIFIED))  # qualifier
        self.assertTrue(mc.relaxed_allowed(CAND))        # plain base card

    def test_autograph_tokens_present(self):
        self.assertIn("auto", mc.query_terms(AUTOED, "NORMAL"))


class TestNoNetwork(unittest.TestCase):
    def test_module_makes_no_network_calls(self):
        import inspect
        src = inspect.getsource(mc)
        for banned in ("requests.", "urllib", "http.client", "ebay_api",
                       "socket."):
            self.assertNotIn(banned, src, f"{banned} must not appear")


if __name__ == "__main__":
    unittest.main()
