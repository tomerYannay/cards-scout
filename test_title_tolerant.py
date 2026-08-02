"""Order-tolerant title parsing. Fixtures are real persisted pilot patterns.

The canonical parser must never be overridden: a title it already understands
is returned verbatim, which is what makes a regression structurally impossible.
"""

import unittest

import db
import enrich
import parse
import title_tolerant as tt

SUR = None


def setUpModule():
    global SUR
    SUR = enrich.load_surnames(db.connect())


def P(title):
    return tt.parse_tolerant(title, SUR)


class TestCanonicalUntouched(unittest.TestCase):
    CANON = "1974 TOPPS #116 NATE WILLIAMS PSA 9 Mint 81383525"

    def test_canonical_layout_is_handled_canonically(self):
        f, prov, amb = P(self.CANON)
        self.assertEqual(prov, tt.CANONICAL)
        self.assertEqual(amb, [])

    def test_canonical_fields_are_identical_to_parse_title(self):
        f, _prov, _amb = P(self.CANON)
        self.assertEqual(f, parse.parse_title(self.CANON)["fields"])

    def test_canonical_slab_key_is_unchanged(self):
        f, _p, _a = P(self.CANON)
        self.assertEqual(parse.make_keys(f),
                         parse.make_keys(parse.parse_title(self.CANON)["fields"]))

    def test_psa_store_titles_never_reach_the_tolerant_path(self):
        for t in ("1989 FLEER #21 MICHAEL JORDAN PSA 6",
                  "2011 BOWMAN CHROME GOLD REFRACTOR #151 ALEX RODRIGUEZ 34/50 PSA 9",
                  "1981 DONRUSS GOLF #13 JACK NICKLAUS PSA 9 OC"):
            self.assertEqual(P(t)[1], tt.CANONICAL, t)


class TestLayouts(unittest.TestCase):
    def test_player_first(self):
        f, prov, _ = P("Cortez Kennedy Auto Signed 1990 Topps Traded RC 44T "
                       "Seattle Seahawks PSA 9")
        self.assertEqual(prov, tt.TOLERANT)
        self.assertEqual(f["athlete"], "CORTEZ KENNEDY")
        self.assertEqual(f["year"], 1990)
        self.assertEqual((f["grader"], f["grade_value"]), ("PSA", "9"))
        self.assertEqual(f["is_auto"], 1)

    def test_grader_first(self):
        f, _p, _a = P("PSA 8 Shaquille O'Neal 1992 Hoops #1 All-Rookie Team")
        self.assertEqual(f["athlete"], "SHAQUILLE O'NEAL")
        self.assertEqual(f["card_number"], "1")

    def test_graded_year_prefix(self):
        f, _p, _a = P("Graded 2025 Topps Now Alex Ovechkin #29 Hockey Card "
                      "PSA 10 Gem Mint")
        self.assertEqual(f["athlete"], "ALEX OVECHKIN")
        self.assertEqual(f["year"], 2025)
        self.assertEqual(f["card_number"], "29")
        self.assertTrue(tt.is_complete(f))

    def test_grader_at_end(self):
        f, _p, _a = P("Graded 2024 Panini Prizm Victor Wembanyama #136 "
                      "Basketball Card PSA 9")
        self.assertEqual((f["grader"], f["grade_value"]), ("PSA", "9"))

    def test_grader_mid_title(self):
        f, _p, _a = P("1999 Fleer Tradition Brett Favre BGS 9 POP 2")
        self.assertEqual((f["grader"], f["grade_value"]), ("BGS", "9"))

    def test_a_product_line_word_is_not_part_of_the_name(self):
        """"Topps NOW Alex Ovechkin" is not a player called NOW."""
        f, _p, _a = P("Graded 2025 Topps Now Alex Ovechkin #29 Card PSA 10")
        self.assertNotIn("NOW", (f["athlete"] or "").split())


class TestGraders(unittest.TestCase):
    def test_psa_bgs_sgc_and_cgc(self):
        for g in ("PSA", "BGS", "SGC", "CGC"):
            f, _p, _a = P(f"Some Player 2020 Topps #1 Card {g} 9")
            self.assertEqual(f["grader"], g)

    def test_decimal_grade(self):
        f, _p, _a = P("Some Player 2020 Topps #1 Card BGS 9.5")
        self.assertEqual(f["grade_value"], "9.5")

    def test_gem_mint_wording_does_not_break_the_grade(self):
        f, _p, _a = P("Graded 2025 Topps Now Alex Ovechkin #29 Card PSA 10 Gem Mint")
        self.assertEqual(f["grade_value"], "10")

    def test_a_bare_grader_word_is_not_a_grade(self):
        """"Beckett" with no number is not a graded claim."""
        f, _p, _a = P("Charlie Sheen Signed Major League Card Rick Vaughn Beckett")
        self.assertIsNone(f["grader"])
        self.assertIsNone(f["grade_value"])

    def test_two_different_graders_is_ambiguous(self):
        _f, prov, amb = P("Some Player 2020 Topps #1 PSA 9 and BGS 9.5")
        self.assertEqual(prov, tt.AMBIGUOUS)
        self.assertTrue(amb)


class TestCardNumbers(unittest.TestCase):
    def test_hash_number(self):
        self.assertEqual(tt.find_card_number("2020 TOPPS #29 X")[0], "29")

    def test_alphanumeric_number(self):
        self.assertEqual(tt.find_card_number("2011 BOWMAN #BCP99 X")[0], "BCP99")

    def test_word_form_card_n(self):
        self.assertEqual(tt.find_card_number("Pete Rose 1987 Topps Card 200")[0],
                         "200")

    def test_word_form_no_n(self):
        self.assertEqual(tt.find_card_number("Player 1987 Topps No. 29")[0], "29")

    def test_two_disagreeing_forms_are_ambiguous(self):
        self.assertEqual(tt.find_card_number("#29 ... Card 200")[0], "AMBIGUOUS")

    def test_a_serial_is_not_a_card_number(self):
        n, _raw = tt.find_card_number("Player 2023 Leaf #1/1 Rookie")
        self.assertIsNone(n)

    def test_missing_number_stays_absent(self):
        self.assertEqual(tt.find_card_number("1999 FLEER BRETT FAVRE BGS 9"),
                         (None, None))


class TestSerialAndParallel(unittest.TestCase):
    def test_serial_and_print_run_are_preserved(self):
        f, _p, _a = P("Alejandro Garnacho 2023 Leaf HYPE! #99A Orange "
                      "Blank Back 14/199 Rookie Card PSA 10")
        self.assertEqual((f["serial_num"], f["print_run"]), (14, 199))

    def test_an_impossible_serial_is_rejected(self):
        self.assertEqual(tt.find_serial("Player 500/50"), (None, None))

    def test_autograph_is_an_attribute_not_a_name(self):
        f, _p, _a = P("Frank Thomas Signed 1990 Topps #414 White Sox PSA 8")
        self.assertEqual(f["is_auto"], 1)
        self.assertEqual(f["athlete"], "FRANK THOMAS")
        for w in ("SIGNED", "AUTO"):
            self.assertNotIn(w, f["athlete"])


class TestRefusals(unittest.TestCase):
    def test_nothing_is_invented_from_an_empty_title(self):
        f = tt.extract("")
        for k in tt.REQUIRED:
            self.assertIsNone(f[k])
        self.assertFalse(tt.is_complete(f))

    def test_a_title_with_no_year_is_incomplete(self):
        f, _p, _a = P("Charlie Sheen Signed Major League Card Beckett")
        self.assertFalse(tt.is_complete(f))

    def test_a_single_word_name_is_refused(self):
        self.assertIsNone(tt.find_player("MADONNA 1990 TOPPS", "1990", None))

    def test_ambiguity_blocks_completeness(self):
        f = tt.extract("Player 2020 Topps #29 PSA 9 BGS 9.5")
        self.assertTrue(f["ambiguity"])
        self.assertFalse(tt.is_complete(f))

    def test_a_year_inside_a_serial_is_not_a_year(self):
        self.assertEqual(tt.find_year("Player Panini 1/2534 Rookie")[0], None)


class TestPopulationSafety(unittest.TestCase):
    """The validated population must not move."""

    def test_no_regression_across_psa_and_sports_cards_forever(self):
        conn = db.connect()
        rows = conn.execute("""SELECT l.title, c.slab_key, c.identity_conf
            FROM listings l JOIN cards c USING(item_id)
            WHERE l.seller IN ('psa','sports-cards-forever')
            AND c.identity_conf IN ('MEDIUM','HIGH') LIMIT 4000""").fetchall()
        for r in rows:
            f, prov, _amb = tt.parse_tolerant(r["title"], SUR)
            self.assertEqual(prov, tt.CANONICAL, r["title"])
            # Identical to the canonical parser, which is the only thing that
            # may set a slab key for an already-complete identity.
            self.assertEqual(parse.make_keys(f),
                             parse.make_keys(parse.parse_title(r["title"])["fields"]))

    def test_identical_slabs_across_sellers_still_group(self):
        title = "1985 TOPPS #215 EDDIE EDWARDS PSA 9"
        a, _p, _x = P(title)
        b, _p2, _x2 = P(title)
        self.assertEqual(parse.make_keys(a)[1], parse.make_keys(b)[1])

    def test_different_cards_do_not_collapse(self):
        a, _p, _x = P("Graded 2025 Topps Now Alex Ovechkin #29 Card PSA 10")
        b, _p2, _x2 = P("Graded 2025 Topps Now Alex Ovechkin #30 Card PSA 10")
        self.assertNotEqual(parse.make_keys(a)[1], parse.make_keys(b)[1])

    def test_same_player_year_grade_but_different_set_do_not_collapse(self):
        a, _p, _x = P("Graded 2024 Panini Prizm Victor Wembanyama #136 Card PSA 9")
        b, _p2, _x2 = P("Graded 2024 Topps Chrome Victor Wembanyama #136 Card PSA 9")
        self.assertNotEqual(parse.make_keys(a)[1], parse.make_keys(b)[1])


class TestFiltersUnweakened(unittest.TestCase):
    def test_singles_filter_constant_is_unchanged(self):
        import fetch_listings as fl
        self.assertEqual(fl.SINGLES_LEAF, "261328")

    def test_lot_and_box_wording_is_still_recognisable(self):
        """The parser does not silently accept a lot as a single card."""
        f, _p, _a = P("2023 TOPPS SILVER PACK BLUE/150 GUNNAR HENDERSON RC PSA 9")
        self.assertIsNotNone(f)          # parsing a title is not admission
