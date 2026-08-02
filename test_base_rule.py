"""Tests for the deterministic confirmed-base rule.

The rule is affirmative evidence of a base card, NOT "a missing Parallel/Variety
field means base". Every condition is tested in both directions.

Run:  python -m unittest -v test_base_rule
"""

import unittest

import card_vocab
import db
import enrich


def aspects(**over):
    a = {"Player/Athlete": "MICHAEL JORDAN", "Card Name": "MICHAEL JORDAN",
         "Set": "1988 FLEER", "Card Number": "17", "Grade": "9",
         "Professional Grader": "Professional Sports Authenticator (PSA)",
         "Sport": "Basketball", "Season": "1988", "Type": "Sports Trading Card",
         "Vintage": "No", "Graded": "Yes"}
    a.update(over)
    return a


def tier_b(asp=None, **over):
    asp = aspects() if asp is None else asp
    upper = {k.upper(): v for k, v in asp.items()}
    b = {"aspects": asp}
    for field, aliases in enrich.ASPECT_MAP.items():
        b[field] = next((upper[x] for x in aliases if x in upper), None)
    for field in enrich.NOT_PROVIDED:
        b[field] = None
    b.update(over)
    return b


def tier_a(**over):
    a = {"set_name": "BASE", "parallel": None, "card_number": "17",
         "year": 1988, "manufacturer": "FLEER", "athlete": "MICHAEL JORDAN",
         "grade_value": "9", "grade_qualifier": None, "auto_grade": None,
         "print_run": None, "serial_num": None, "is_auto": 0, "is_relic": 0,
         "insert_name": None, "sport": None, "grade_type": "NUMERIC",
         "parallel_conf": "MEDIUM", "identity_conf": "MEDIUM",
         "slab_key": "k", "title": "1988 FLEER #17 MICHAEL JORDAN PSA 9"}
    a.update(over)
    return a


class TestConfirmedBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())

    def test_complete_core_and_both_silent_is_base(self):
        ok, why = enrich.base_compatible(tier_a(), tier_b(),
                                         "1988 FLEER #17 MICHAEL JORDAN PSA 9")
        self.assertTrue(ok, why)
        self.assertIn("structured base-compatible", why)

    def test_psa_qualifier_does_not_block_base(self):
        for qual in ("MC", "OC", "ST", "MK", "PD"):
            title = f"1988 FLEER #17 MICHAEL JORDAN PSA 9 {qual}"
            ok, why = enrich.base_compatible(
                tier_a(grade_qualifier=qual, title=title), tier_b(), title)
            self.assertTrue(ok, f"{qual}: {why}")

    def test_manufacturer_alias_in_title_is_accounted_for(self):
        title = "1991 UD #69 MICHAEL JORDAN PSA 8"
        ok, why = enrich.base_compatible(
            tier_a(manufacturer="UPPER DECK", card_number="69", year=1991,
                   title=title),
            tier_b(aspects(**{"Set": "1991 UPPER DECK", "Card Number": "69",
                              "Grade": "8", "Season": "1991"})), title)
        self.assertTrue(ok, why)


class TestCoreIdentityRequired(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())

    def test_each_missing_core_field_holds(self):
        for field in ("Player/Athlete", "Set", "Card Number", "Grade",
                      "Professional Grader"):
            asp = aspects()
            del asp[field]
            ok, why = enrich.base_compatible(
                tier_a(), tier_b(asp), "1988 FLEER #17 MICHAEL JORDAN PSA 9")
            self.assertFalse(ok, field)
            self.assertIn("core identity incomplete", why)

    def test_missing_title_holds(self):
        ok, why = enrich.base_compatible(tier_a(), tier_b(), None)
        self.assertFalse(ok)
        self.assertIn("title unavailable", why)


class TestParallelEvidenceBlocksBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())

    def test_title_states_parallel_aspects_silent(self):
        title = "1988 FLEER #17 MICHAEL JORDAN SILVER PSA 9"
        ok, why = enrich.base_compatible(tier_a(title=title), tier_b(), title)
        self.assertFalse(ok)
        self.assertIn("title states parallel", why)

    def test_aspects_state_parallel_title_silent(self):
        b = tier_b()
        b["parallel"] = "SILVER PRIZM"
        ok, why = enrich.base_compatible(tier_a(), b,
                                         "1988 FLEER #17 MICHAEL JORDAN PSA 9")
        self.assertFalse(ok)
        self.assertIn("Tier B states Parallel/Variety", why)

    def test_parallel_term_hidden_in_another_aspect(self):
        b = tier_b(aspects(**{"Set": "1988 FLEER REFRACTOR"}))
        b["parallel"] = None
        ok, why = enrich.base_compatible(tier_a(), b,
                                         "1988 FLEER #17 MICHAEL JORDAN PSA 9")
        self.assertFalse(ok)
        self.assertIn("parallel term", why)

    def test_parallel_term_inside_the_set_identity(self):
        ok, why = enrich.base_compatible(
            tier_a(set_name="CHROME GOLD"), tier_b(),
            "1988 FLEER #17 MICHAEL JORDAN PSA 9")
        self.assertFalse(ok)
        self.assertIn("parallel term", why)

    def test_unknown_token_keeps_it_held(self):
        title = "1988 FLEER #17 MICHAEL JORDAN PSA 9 ZQXWOMBAT"
        ok, why = enrich.base_compatible(tier_a(title=title), tier_b(), title)
        self.assertFalse(ok)
        self.assertIn("unexplained token", why)

    def test_boolean_aspects_are_not_parallel_evidence(self):
        """Vintage="No" must not read as a parallel via "NO HUDDLE"."""
        self.assertNotIn("NO", card_vocab.TRUE_PARALLEL)
        ok, why = enrich.base_compatible(tier_a(), tier_b(),
                                         "1988 FLEER #17 MICHAEL JORDAN PSA 9")
        self.assertTrue(ok, why)


class TestSerialEvidenceBlocksBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())

    def test_print_run_is_not_silently_base(self):
        ok, why = enrich.base_compatible(tier_a(print_run=500), tier_b(),
                                         "1988 FLEER #17 MICHAEL JORDAN PSA 9")
        self.assertFalse(ok)
        self.assertIn("serial/print-run", why)

    def test_serial_number_is_not_silently_base(self):
        ok, why = enrich.base_compatible(tier_a(serial_num=42), tier_b(),
                                         "1988 FLEER #17 MICHAEL JORDAN PSA 9")
        self.assertFalse(ok)
        self.assertIn("serial/print-run", why)


class TestNoRegressionForKnownParallels(unittest.TestCase):
    """Real parallel candidates must never become base."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())

    def test_known_parallel_candidates_are_not_base(self):
        import manual_comps as mc
        cands = [c for c in mc.load_candidates().values() if c["parallel"]]
        if not cands:
            self.skipTest("no parallel candidates in the pool")
        for c in cands[:8]:
            a = tier_a(set_name=c["set"], parallel=c["parallel"],
                       card_number=c["card_number"], year=c["year"],
                       manufacturer=c["manufacturer"], athlete=c["subject"],
                       grade_value=c["psa_grade"], print_run=c["print_run"],
                       title=c["title"])
            ok, _why = enrich.base_compatible(a, tier_b(), c["title"])
            self.assertFalse(ok, c["title"])


class TestPeerConflictUnchanged(unittest.TestCase):
    """Group-level conflict handling is untouched by the base rule."""

    def test_material_conflict_still_wins(self):
        import peers
        eff = {"a": {"cls": "quarantined_material_conflict", "eff_slab": "x"},
               "b": {"cls": "verified", "eff_slab": "x"}}
        self.assertEqual(peers.classify_group(["a", "b"], eff, "a"),
                         "material_conflict")

    def test_a_held_member_still_blocks_the_group(self):
        import peers
        eff = {"a": {"cls": "verified", "eff_slab": "x"},
               "b": {"cls": "held_for_parallel_resolution", "eff_slab": "x"}}
        self.assertEqual(peers.classify_group(["a", "b"], eff, "a"),
                         "unresolved_peer_identity")

    def test_all_verified_same_slab_is_confirmed(self):
        import peers
        eff = {"a": {"cls": "verified", "eff_slab": "x"},
               "b": {"cls": "verified", "eff_slab": "x"},
               "c": {"cls": "verified", "eff_slab": "x"}}
        self.assertEqual(peers.classify_group(["a", "b", "c"], eff, "a"),
                         "confirmed_same_identity")


if __name__ == "__main__":
    unittest.main()
