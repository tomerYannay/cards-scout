"""Manufacturer guard and query-invariant tests.

Two defects the five-candidate pilot exposed:

  A. A 1989-90 NBA Hoops #21 Michael Jordan PSA 6 was accepted as a comp for a
     1989 Fleer #21 Michael Jordan PSA 6 - same year, number, player and grade,
     different card.
  B. NORMAL dropped the maker, set and print run, turning "2011 Bowman Chrome
     #151 Gold Refractor /50" into a search for a different identity.

Run:  python -m unittest -v test_identity_guards
"""

import unittest

import db
import enrich
import manual_comps as mc
import parse
import product_research_parse as prp


def cand(**over):
    c = {"item_id": "v1|1|0", "title": "1989 FLEER #21 MICHAEL JORDAN PSA 6",
         "year": 1989, "manufacturer": "FLEER", "set": "BASE",
         "subject": "MICHAEL JORDAN", "card_number": "21", "parallel": None,
         "serial_num": None, "print_run": None, "psa_grade": "6",
         "grade_type": "NUMERIC", "qualifier": None, "is_auto": 0,
         "auto_grade": None, "asking_price": 423.0, "shipping": 0.0}
    c.update(over)
    return c


class TestBrandNormalization(unittest.TestCase):
    def test_known_aliases_resolve_to_one_maker(self):
        for alias in ("UD", "Upper Deck", "SPx"):
            self.assertEqual(parse.canonical_brand(alias), "UPPER DECK", alias)
        self.assertEqual(parse.canonical_brand("Fleer"), "FLEER")
        self.assertEqual(parse.canonical_brand("Bowman's"), "BOWMAN")

    def test_product_lines_resolve_to_their_maker(self):
        self.assertEqual(parse.canonical_brand("Ultra"), "FLEER")
        self.assertEqual(parse.canonical_brand("Metal"), "FLEER")
        self.assertEqual(parse.canonical_brand("Hoops"), "HOOPS")

    def test_unknown_name_is_none_not_a_guess(self):
        self.assertIsNone(parse.canonical_brand("Zqxwombat"))
        self.assertIsNone(parse.canonical_brand(""))
        self.assertIsNone(parse.canonical_brand(None))

    def test_brand_is_read_from_a_title_the_parser_cannot_structure(self):
        """The Hoops title has no year-first prefix, so parse_title misses it."""
        title = "PSA 6 Michael Jordan 1989-90 NBA Hoops #21 Chicago Bulls"
        self.assertIsNone(parse.parse_title(title)["fields"]["manufacturer"])
        self.assertEqual(parse.brands_in(title), {"HOOPS"})

    def test_brands_in_finds_every_maker_named(self):
        self.assertEqual(parse.brands_in("1989 Fleer Basketball #21"), {"FLEER"})
        self.assertIn("BOWMAN", parse.brands_in("2011 Bowman Chrome Prospects"))
        self.assertEqual(parse.brands_in("Michael Jordan #21 PSA 6"), set())


class TestManufacturerGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())

    def state(self, c, title):
        return prp.classify_comp(c, title)

    def test_the_pilot_false_accept_is_now_rejected(self):
        state, why = self.state(
            cand(), "PSA 6 Michael Jordan 1989-90 NBA Hoops #21 Chicago Bulls")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("manufacturer/brand conflict", why)

    def test_matching_manufacturer_stays_eligible(self):
        for title in ("1989-90 Fleer - Michael Jordan #21 PSA 6",
                      "1989 Fleer Basketball #21 Michael Jordan HOF PSA 6",
                      "Michael Jordan 1989 Fleer #21 Bulls PSA 6 EX-MT"):
            self.assertEqual(self.state(cand(), title)[0], prp.ACCEPTED, title)

    def test_alias_of_the_same_maker_stays_eligible(self):
        """UD is Upper Deck; a title using either form is the same maker."""
        c = cand(manufacturer="UPPER DECK", year=1991, card_number="69",
                 psa_grade="8", title="1991 UD #69 MICHAEL JORDAN PSA 8")
        for title in ("1991 UD #69 Michael Jordan PSA 8",
                      "1991 Upper Deck #69 Michael Jordan PSA 8"):
            self.assertEqual(self.state(c, title)[0], prp.ACCEPTED, title)

    def test_product_line_of_the_same_maker_stays_eligible(self):
        """Ultra is a Fleer product; it is not a conflicting maker."""
        self.assertEqual(
            self.state(cand(), "1989-90 Fleer Ultra Michael Jordan #21 PSA 6")[0],
            prp.ACCEPTED)

    def test_missing_manufacturer_on_the_comp_keeps_existing_behaviour(self):
        """No maker named at all is absent evidence, not a conflict."""
        state, why = self.state(cand(), "1989 Michael Jordan #21 PSA 6")
        self.assertEqual(state, prp.ACCEPTED)
        self.assertIsNone(why)

    def test_missing_manufacturer_on_the_candidate_never_rejects(self):
        c = cand(manufacturer=None)
        self.assertEqual(
            self.state(c, "1989-90 NBA Hoops #21 Michael Jordan PSA 6")[0],
            prp.ACCEPTED)

    def test_identical_number_year_player_grade_cannot_override_a_conflict(self):
        """Everything else matching is exactly the trap; the maker still wins."""
        state, why = self.state(
            cand(), "1989 Hoops #21 Michael Jordan PSA 6 Chicago Bulls")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("manufacturer/brand conflict", why)

    def test_a_different_maker_is_rejected_for_a_bowman_candidate(self):
        c = cand(manufacturer="BOWMAN", set="CHROME PROSPECTS", year=2011,
                 card_number="BCP99", subject="PAUL GOLDSCHMIDT", psa_grade="9",
                 title="2011 BOWMAN CHROME PROSPECTS #BCP99 PAUL GOLDSCHMIDT PSA 9")
        state, why = self.state(
            c, "2011 Topps Chrome #BCP99 Paul Goldschmidt PSA 9")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("manufacturer/brand conflict", why)

    def test_guard_is_separate_from_the_set_comparison(self):
        """A parallel conflict and a brand conflict report different reasons."""
        c = cand(parallel="REFRACTOR")
        _s, why = self.state(c, "1989 Fleer #21 Michael Jordan PSA 6")
        self.assertNotIn("manufacturer/brand", why or "")


class TestQueryInvariant(unittest.TestCase):
    """No tier may drop a material discriminator that is known."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())

    def levels(self, c):
        aborted = []
        got = prp.query_levels(
            c, on_abort=lambda l, q, m: aborted.append((l, m)))
        return dict(got), aborted

    def test_every_generated_tier_passes_the_invariant(self):
        for c in (cand(),
                  cand(is_auto=1, auto_grade="10"),
                  cand(qualifier="OC"),
                  cand(parallel="GOLD REFRACTOR"),
                  cand(parallel="GOLD REFRACTOR", print_run=50),
                  cand(card_number="BCP99")):
            got, _ = self.levels(c)
            self.assertTrue(got)
            for level, q in got.items():
                self.assertEqual(mc.query_violations(c, q), [],
                                 f"{level}: {q}")

    def test_rodriguez_keeps_bowman_chrome_gold_refractor_and_print_run(self):
        c = cand(year=2011, manufacturer="BOWMAN", set="CHROME",
                 subject="ALEX RODRIGUEZ", card_number="151",
                 parallel="GOLD REFRACTOR", print_run=50, psa_grade="9")
        got, _ = self.levels(c)
        self.assertIn("NORMAL", got)
        for level, q in got.items():
            up = q.upper()
            self.assertIn("BOWMAN", up, level)
            self.assertIn("CHROME", up, level)
            self.assertIn("GOLD REFRACTOR", up, level)
            self.assertIn("/50", up, level)

    def test_goldschmidt_keeps_bowman_chrome_prospects(self):
        c = cand(year=2011, manufacturer="BOWMAN", set="CHROME PROSPECTS",
                 subject="PAUL GOLDSCHMIDT", card_number="BCP99", psa_grade="9")
        got, _ = self.levels(c)
        self.assertIn("NORMAL", got)
        for level, q in got.items():
            self.assertIn("BOWMAN CHROME PROSPECTS", q.upper(), level)

    def test_normal_may_vary_surface_form_only(self):
        """#BCP99 and BCP99 are the same card; both are acceptable."""
        c = cand(card_number="BCP99")
        got, _ = self.levels(c)
        self.assertIn("#BCP99", got["STRICT"])
        self.assertIn("BCP99", got["NORMAL"])
        self.assertNotIn("#BCP99", got["NORMAL"])

    def test_auto_and_qualifier_survive_every_tier(self):
        c = cand(is_auto=1, auto_grade="10", qualifier="OC")
        got, _ = self.levels(c)
        for level, q in got.items():
            self.assertIn("auto 10", q, level)
            self.assertIn("OC", q, level)

    def test_a_tier_that_loses_a_discriminator_is_aborted_not_sent(self):
        c = cand(parallel="GOLD REFRACTOR", print_run=50)
        broken = "2011 ALEX RODRIGUEZ #151 PSA 9"        # no maker, no /50
        missing = mc.query_violations(c, broken)
        self.assertIn("manufacturer_or_set", missing)
        self.assertIn("parallel", missing)
        self.assertIn("print_run", missing)

    def test_absent_facts_are_not_required(self):
        """A candidate with no parallel cannot be faulted for omitting one."""
        self.assertNotIn("parallel", mc.required_discriminators(cand()))
        self.assertNotIn("print_run", mc.required_discriminators(cand()))
        self.assertNotIn("qualifier", mc.required_discriminators(cand()))

    def test_base_set_is_not_treated_as_a_missing_discriminator(self):
        c = cand()                                   # set == "BASE"
        got, _ = self.levels(c)
        for level, q in got.items():
            self.assertIn("FLEER", q.upper(), level)
            self.assertEqual(mc.query_violations(c, q), [])


if __name__ == "__main__":
    unittest.main()
