"""Candidate parallel extraction, and the seller-year diagnostic.

The Gate B checkpoint left a candidate ("UD Canvas") unable to accept any of its
own comps because the tolerant parser extracted no parallel at all. Extraction
is delegated to parse._extract_parallel - the same routine the canonical parser
and comp matching use - so the vocabulary cannot drift into a second list. What
this module adds is the SPAN it runs over.

Run:  python -m unittest -v test_parallel_extraction
"""

import unittest

import card_vocab
import db
import enrich
import manual_comps as mc
import parse
import product_research_parse as prp
import title_tolerant as tt

SUR = None


def setUpModule():
    global SUR
    SUR = enrich.load_surnames(db.connect())


def par(title):
    return tt.parse_tolerant(title, SUR)[0].get("parallel")


class TestVocabularyIsShared(unittest.TestCase):
    def test_extraction_uses_the_authoritative_routine(self):
        """No second vocabulary: the same function the matcher relies on."""
        self.assertTrue(hasattr(parse, "_extract_parallel"))
        self.assertIn("CANVAS", parse.PARALLEL_PHRASES)
        self.assertIn("GOLD", parse.PARALLEL_COLORS)

    def test_no_private_parallel_list_exists(self):
        src = open("title_tolerant.py").read()
        self.assertIn("parse._extract_parallel", src)


class TestStatedParallels(unittest.TestCase):
    def test_named_parallel_canvas(self):
        self.assertEqual(
            par("Graded 2021 Upper Deck Jason Robertson #C147 UD Canvas "
                "Rookie Hockey Card PSA 10"), "CANVAS")

    def test_colour_parallel(self):
        self.assertEqual(
            par("2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA 22/199 PSA 9"),
            "RED")

    def test_multi_word_parallel_phrase(self):
        self.assertEqual(
            par("Graded 2022 Panini Prizm Player #5 Cracked Ice Rookie PSA 10"),
            "CRACKED ICE")

    def test_longest_match_wins_over_a_bare_colour(self):
        """CRACKED ICE, not ICE; GOLD REFRACTOR keeps both words."""
        self.assertEqual(
            par("Graded 2022 Panini Player #5 Cracked Ice Card PSA 10"),
            "CRACKED ICE")
        self.assertEqual(
            par("2011 BOWMAN CHROME GOLD REFRACTOR #151 ALEX RODRIGUEZ 34/50 PSA 9"),
            "GOLD REFRACTOR")

    def test_parallel_alongside_a_print_run(self):
        f, _p, _a = tt.parse_tolerant(
            "2011 BOWMAN CHROME GOLD REFRACTOR #151 ALEX RODRIGUEZ 34/50 PSA 9", SUR)
        self.assertEqual(f["parallel"], "GOLD REFRACTOR")
        self.assertEqual((f["serial_num"], f["print_run"]), (34, 50))

    def test_canonical_ordering_is_preserved(self):
        """Same normalization the comp matcher applies."""
        a = par("2011 BOWMAN CHROME GOLD REFRACTOR #151 PLAYER NAME PSA 9")
        b = parse._extract_parallel("GOLD REFRACTOR")[0]
        self.assertEqual(a, b)


class TestNoFalsePositives(unittest.TestCase):
    """Ordinary words in team, player and set names are not parallels."""

    def test_colour_bearing_team_names(self):
        for title in (
                "Frank Thomas 1990 Topps #414 Chicago White Sox PSA 8",
                "Player Name 1990 Topps #1 Boston Red Sox PSA 9",
                "Player Name 1990 Topps #1 Toronto Blue Jays PSA 9",
                "Player Name 1990 Topps #1 Green Bay Packers PSA 9"):
            self.assertIsNone(par(title), title)

    def test_a_base_card_stays_none(self):
        for title in (
                "PSA 10 Puka Nacua 2023 Donruss Football #357 Los Angeles Rams RC",
                "Graded 2020-21 Panini Prizm DP LAMELO BALL #3 Rookie RC PSA 10",
                "PSA 10 Cameron Brink 2023 Bowman University Now #44 Stanford PSA 10",
                "1974 TOPPS #116 NATE WILLIAMS PSA 9 Mint"):
            self.assertIsNone(par(title), title)

    def test_a_serial_alone_never_implies_a_parallel(self):
        self.assertIsNone(par("Player Name 2023 Topps #55 12/99 Rookie PSA 10"))

    def test_rarity_language_never_implies_a_parallel(self):
        self.assertIsNone(
            par("Player Name 2023 Leaf #5 Just 25 Made Rare Rookie PSA 10"))

    def test_a_grader_token_is_not_a_parallel(self):
        self.assertIsNone(par("Player Name 2020 Topps #1 SGC 9 Card"))


class TestAmbiguity(unittest.TestCase):
    def test_two_competing_colours_are_ambiguous(self):
        f, prov, amb = tt.parse_tolerant(
            "Graded 2022 Panini Player #5 Red Blue Mixed Card PSA 10", SUR)
        self.assertEqual(prov, tt.AMBIGUOUS)
        self.assertTrue(any("parallel" in a for a in amb))

    def test_an_ambiguous_parallel_blocks_completeness(self):
        f = tt.extract("Some Player 2022 Panini #5 Red Blue Card PSA 10", SUR)
        self.assertTrue(f["ambiguity"])
        self.assertFalse(tt.is_complete(f))

    def test_one_colour_with_a_phrase_is_not_ambiguous(self):
        f, prov, _a = tt.parse_tolerant(
            "2011 BOWMAN CHROME GOLD REFRACTOR #151 PLAYER NAME 34/50 PSA 9", SUR)
        self.assertNotEqual(prov, tt.AMBIGUOUS)


class TestIdentityFlow(unittest.TestCase):
    """The parallel must reach the identity, the key, the query and the match."""

    CANVAS = ("Graded 2021 Upper Deck Jason Robertson #C147 UD Canvas "
              "Rookie Hockey Card PSA 10")

    def cand(self, title):
        f, _p, _a = tt.parse_tolerant(title, SUR)
        card, slab = parse.make_keys(f)
        return f, card, slab

    def test_the_parallel_changes_the_slab_key(self):
        _f, _c, with_par = self.cand(self.CANVAS)
        _f2, _c2, without = self.cand(self.CANVAS.replace("UD Canvas ", ""))
        self.assertNotEqual(with_par, without)

    def test_query_generation_includes_the_parallel_once(self):
        f, _c, _s = self.cand(self.CANVAS)
        c = {"year": f["year"], "subject": f["athlete"],
             "manufacturer": f["manufacturer"], "set": "BASE",
             "card_number": f["card_number"], "parallel": f["parallel"],
             "print_run": f["print_run"], "is_auto": 0, "auto_grade": None,
             "psa_grade": f["grade_value"], "qualifier": None}
        q = prp.build_query(c, "STRICT")
        self.assertEqual(q.upper().count("CANVAS"), 1)
        self.assertEqual(mc.query_violations(c, q), [])

    def test_matching_accepts_the_same_stated_parallel(self):
        f, _c, _s = self.cand(self.CANVAS)
        c = {"item_id": "v1|1|0", "year": f["year"], "subject": f["athlete"],
             "manufacturer": f["manufacturer"], "set": "BASE",
             "card_number": f["card_number"], "parallel": f["parallel"],
             "serial_num": None, "print_run": None, "psa_grade": f["grade_value"],
             "grade_type": "NUMERIC", "qualifier": None, "is_auto": 0,
             "auto_grade": None}
        state, why = prp.classify_comp(
            c, "2021-22 Upper Deck Jason Robertson #C147 UD Canvas PSA 10")
        self.assertEqual(state, prp.ACCEPTED, why)

    def test_matching_still_rejects_a_different_parallel(self):
        f, _c, _s = self.cand(self.CANVAS)
        c = {"item_id": "v1|1|0", "year": f["year"], "subject": f["athlete"],
             "manufacturer": f["manufacturer"], "set": "BASE",
             "card_number": f["card_number"], "parallel": f["parallel"],
             "serial_num": None, "print_run": None, "psa_grade": f["grade_value"],
             "grade_type": "NUMERIC", "qualifier": None, "is_auto": 0,
             "auto_grade": None}
        state, why = prp.classify_comp(
            c, "2021-22 Upper Deck Jason Robertson #C147 GOLD REFRACTOR PSA 10")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("parallel", why)


class TestCheckpointRegressions(unittest.TestCase):
    """Real stored titles from the Gate B checkpoint, not invented examples."""

    def test_jason_robertson_canvas_is_now_extracted(self):
        self.assertEqual(
            par("Graded 2021 Upper Deck Jason Robertson #C147 UD Canvas "
                "Rookie Hockey Card PSA 10"), "CANVAS")

    def test_cameron_brink_remains_a_base_card(self):
        self.assertIsNone(
            par("PSA 10 Cameron Brink 2023 Bowman University Now #44 "
                "Stanford/Sparks Rookie Card"))

    def test_puka_nacua_remains_a_base_card(self):
        self.assertIsNone(
            par("PSA 10 Puka Nacua 2023 Donruss Football #357 Los Angeles Rams "
                "Rookie Card"))

    def test_lamelo_ball_remains_a_base_card(self):
        self.assertIsNone(
            par("Graded 2020-21 Panini Prizm DP LAMELO BALL #3 Rookie RC "
                "Basketball Card PSA 10"))

    def test_base_candidates_still_reject_their_parallels(self):
        """A base card must not be valued against its own parallels."""
        c = {"item_id": "v1|1|0", "year": 2023, "subject": "PUKA NACUA",
             "manufacturer": "DONRUSS", "set": "BASE", "card_number": "357",
             "parallel": None, "serial_num": None, "print_run": None,
             "psa_grade": "10", "grade_type": "NUMERIC", "qualifier": None,
             "is_auto": 0, "auto_grade": None}
        state, why = prp.classify_comp(
            c, "2023 Donruss Puka Nacua #357 PINK Rookie PSA 10")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("parallel", why)


class TestYearDisagreementDiagnostic(unittest.TestCase):
    """Diagnostic only. It never rewrites a year or accepts a comp."""

    def test_the_jackson_holliday_case_is_flagged(self):
        flag = tt.year_disagreement(2022, [2023] * 47, accepted_count=0)
        self.assertIsNotNone(flag)
        self.assertEqual(flag["flag"], tt.YEAR_DISAGREEMENT)
        self.assertEqual(flag["candidate_year"], 2022)
        self.assertEqual(flag["observed_year"], 2023)
        self.assertEqual(flag["supporting_comps"], 47)

    def test_it_does_not_rewrite_the_year(self):
        flag = tt.year_disagreement(2022, [2023] * 47)
        self.assertIn("not rewritten", flag["action"])
        self.assertEqual(flag["candidate_year"], 2022)

    def test_one_or_two_rows_prove_nothing(self):
        self.assertIsNone(tt.year_disagreement(2022, [2023, 2023]))

    def test_a_split_distribution_is_not_flagged(self):
        self.assertIsNone(
            tt.year_disagreement(2022, [2023] * 5 + [2021] * 5))

    def test_candidates_with_accepted_evidence_are_not_flagged(self):
        self.assertIsNone(
            tt.year_disagreement(2022, [2023] * 47, accepted_count=10))

    def test_agreement_is_not_a_disagreement(self):
        self.assertIsNone(tt.year_disagreement(2022, [2022] * 40))

    def test_the_distribution_is_reported(self):
        flag = tt.year_disagreement(2022, [2023] * 20 + [2023])
        self.assertEqual(flag["year_distribution"], {2023: 21})
