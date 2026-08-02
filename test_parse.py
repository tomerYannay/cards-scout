"""Tests for Step 2 parsing. Every title here is a real one from cards.db.

Run:  python -m unittest -v test_parse
"""

import unittest

import parse


def fields(title):
    return parse.parse_title(title)["fields"]


def conf(title):
    return parse.parse_title(title)["conf"]


def status(title):
    r = parse.parse_title(title)
    return parse.parse_status(r["conf"], r["issues"])


class TestStandardTitles(unittest.TestCase):
    def test_modern_parallel_with_serial(self):
        f = fields("2020 PANINI PRIZM GREEN SCOPE #398 JUSTIN JEFFERSON ROOKIE RC 36/75 PSA 10")
        self.assertEqual(f["year"], 2020)
        self.assertEqual(f["manufacturer"], "PANINI")
        self.assertEqual(f["set_name"], "PRIZM")
        self.assertEqual(f["parallel"], "GREEN SCOPE")
        self.assertEqual(f["card_number"], "398")
        self.assertEqual(f["athlete"], "JUSTIN JEFFERSON")
        self.assertEqual((f["serial_num"], f["print_run"]), (36, 75))
        self.assertEqual(f["is_rookie"], 1)
        self.assertEqual((f["grade_type"], f["grade_value"]), ("NUMERIC", "10"))

    def test_vintage_base_card(self):
        f = fields("1975 TOPPS #370 TOM SEAVER PSA AUTHENTIC")
        self.assertEqual(f["set_name"], "BASE")  # absent set span = flagship base
        self.assertIsNone(f["parallel"])
        self.assertEqual(f["athlete"], "TOM SEAVER")

    def test_parallel_only_set_span_becomes_base(self):
        f = fields("1988 TOPPS TIFFANY #580 MARK MCGWIRE PSA 9")
        self.assertEqual(f["set_name"], "BASE")
        self.assertEqual(f["parallel"], "TIFFANY")

    def test_autograph_and_alphanumeric_card_number(self):
        f = fields("2022 BOWMAN CHROME PRSPCT AUTOS #CPAJCO JACKSON CHOURIO 68/75 PSA 10 AUTO")
        self.assertEqual(f["card_number"], "CPAJCO")
        self.assertEqual(f["athlete"], "JACKSON CHOURIO")
        self.assertEqual(f["is_auto"], 1)
        self.assertEqual(f["print_run"], 75)

    def test_apostrophe_set_name(self):
        f = fields("2001 UD TIGER'S TALES #TT10 TIGER WOODS PSA 8")
        self.assertEqual(f["manufacturer"], "UPPER DECK")
        self.assertEqual(f["set_name"], "TIGER'S TALES")
        self.assertEqual(f["athlete"], "TIGER WOODS")


class TestGradeHandling(unittest.TestCase):
    def test_glued_grade_token_rc_psa(self):
        # Real title: the grade is concatenated onto the previous word.
        f = fields("2013 PANINI ABSOLUTE HOGG HEAVEN #65 DION JORDAN ROOKIE RCPSA 9")
        self.assertEqual(f["grade_value"], "9")
        self.assertEqual(f["athlete"], "DION JORDAN")

    def test_glued_grade_token_o_psa(self):
        f = fields("2025 PANINI INSTANT NFL #33 JAXSON DART ROOKIE RC OPSA 8")
        self.assertEqual(f["grade_value"], "8")

    def test_half_grade(self):
        f = fields("1993 TOPPS FINEST #199 DEREK JETER PSA 8.5")
        self.assertEqual(f["grade_value"], "8.5")

    def test_authentic_is_non_numeric_grade_type(self):
        f = fields("1962 POST CEREAL HAND CUT #126 BOBBY LAYNE PSA AUTHENTIC")
        self.assertEqual(f["grade_type"], "AUTHENTIC")
        self.assertEqual(f["grade_value"], "AUTHENTIC")

    def test_authentic_never_shares_slab_key_with_numeric(self):
        base = "1975 TOPPS #370 TOM SEAVER PSA "
        auth = parse.make_keys(fields(base + "AUTHENTIC"))
        num = parse.make_keys(fields(base + "10"))
        self.assertEqual(auth[0], num[0])       # same physical card
        self.assertNotEqual(auth[1], num[1])    # never comped together


class TestQualifiers(unittest.TestCase):
    """PSA qualifiers materially lower value; they must split slab identity."""

    def test_mc_miscut(self):
        f = fields("1980 TOPPS ALL-PRO #170 OTTIS ANDERSON PSA 4 MC")
        self.assertEqual(f["grade_value"], "4")
        self.assertEqual(f["grade_qualifier"], "MC")
        self.assertEqual(f["grade_raw"], "PSA 4 MC")

    def test_oc_off_centre(self):
        f = fields("1986 TOPPS #207 FERNANDO VALENZUELA PSA 8 OC")
        self.assertEqual((f["grade_value"], f["grade_qualifier"]), ("8", "OC"))

    def test_st_stain(self):
        f = fields("1969 TOPPS ALL-STAR #422 DON KESSINGER PSA 7 ST")
        self.assertEqual((f["grade_value"], f["grade_qualifier"]), ("7", "ST"))

    def test_mk_marked(self):
        f = fields("1968 TOPPS #60 KEN HOLTZMAN PSA 1 MK")
        self.assertEqual((f["grade_value"], f["grade_qualifier"]), ("1", "MK"))

    def test_pd_print_defect(self):
        f = fields("1987 TOPPS ALL-STAR #611 KIRBY PUCKETT PSA 9 PD")
        self.assertEqual((f["grade_value"], f["grade_qualifier"]), ("9", "PD"))

    def test_qualifier_splits_slab_key(self):
        plain = fields("1980 TOPPS ALL-PRO #170 OTTIS ANDERSON PSA 4")
        mc = fields("1980 TOPPS ALL-PRO #170 OTTIS ANDERSON PSA 4 MC")
        self.assertEqual(parse.make_keys(plain)[0], parse.make_keys(mc)[0])
        self.assertNotEqual(parse.make_keys(plain)[1], parse.make_keys(mc)[1])

    def test_no_qualifier_when_absent(self):
        self.assertIsNone(
            fields("1992 UD MVP #67 MICHAEL JORDAN PSA 4")["grade_qualifier"])


class TestAutographGrade(unittest.TestCase):
    """An autograph grade is not the card grade and never may be read as one."""

    def test_psa_9_auto_9(self):
        f = fields("1988 FLEER #43 DENNIS RODMAN PSA 9 AUTO 9")
        self.assertEqual(f["grade_value"], "9")
        self.assertEqual(f["auto_grade"], "9")

    def test_psa_9_auto_10(self):
        f = fields("1988 FLEER #43 DENNIS RODMAN PSA 9 AUTO 10")
        self.assertEqual(f["grade_value"], "9")   # NOT 10
        self.assertEqual(f["auto_grade"], "10")

    def test_auto_grade_splits_slab_key(self):
        a9 = fields("1988 FLEER #43 DENNIS RODMAN PSA 9 AUTO 9")
        a10 = fields("1988 FLEER #43 DENNIS RODMAN PSA 9 AUTO 10")
        self.assertEqual(parse.make_keys(a9)[0], parse.make_keys(a10)[0])
        self.assertNotEqual(parse.make_keys(a9)[1], parse.make_keys(a10)[1])

    def test_psa_authentic_auto_9(self):
        f = fields("2024 PANINI PRIZM BLACK SENSATIONAL SIGNATURES "
                   "JALEN DUREN PSA AUTHENTIC AUTO 9")
        self.assertEqual(f["grade_type"], "AUTHENTIC")
        self.assertEqual(f["grade_value"], "AUTHENTIC")
        self.assertEqual(f["auto_grade"], "9")

    def test_auto_authentic_suffix(self):
        f = fields("2021 ORANGE #IALD2 LEWIS DUARTE 30/50 PSA 8 AUTO AUTHENTIC")
        self.assertEqual(f["grade_value"], "8")
        self.assertEqual(f["auto_grade"], "AUTHENTIC")

    def test_bare_auto_is_not_a_grade(self):
        f = fields("2022 BOWMAN CHROME PRSPCT AUTOS #CPAJCO JACKSON CHOURIO "
                   "68/75 PSA 10 AUTO")
        self.assertEqual(f["grade_value"], "10")
        self.assertIsNone(f["auto_grade"])
        self.assertEqual(f["is_auto"], 1)

    def test_auto_colour_is_a_set_name_not_a_grade(self):
        f = fields("2023 TOPPS CHROME AUTO GOLD REFRACTOR #CA-JD JULIO "
                   "RODRIGUEZ 12/50 PSA 10")
        self.assertIsNone(f["auto_grade"])

    def test_psa_dna_auto_has_no_card_grade(self):
        f = fields("PETE ROSE AUTOGRAPHED SIGNED TRADING CARD AUTO GRADE "
                   "PSA DNA AUTO 10")
        self.assertIsNone(f["grade_value"])      # excluded from valuation
        self.assertIsNone(f["grade_type"])
        self.assertEqual(f["auto_grade"], "10")  # recorded, but not a card grade


class TestPrintRun(unittest.TestCase):
    """PSA writes '#/50' for a serialized card whose copy number is undisclosed."""

    def test_card_number_plus_hash_slash_run(self):
        f = fields("1999 FLEER FORCE #211 STEVE FRANCIS #/1600 PSA 8")
        self.assertEqual(f["card_number"], "211")
        self.assertEqual(f["print_run"], 1600)
        self.assertIsNone(f["serial_num"])          # never invented
        self.assertEqual(f["athlete"], "STEVE FRANCIS")

    def test_hash_slash_99(self):
        f = fields("2019 PANINI CHRONICLES PURPLE #181 KENDRICK NUNN #/99 PSA 8")
        self.assertEqual((f["print_run"], f["serial_num"]), (99, None))

    def test_hash_slash_with_spaces(self):
        f = fields("2019 PANINI CHRONICLES PURPLE #181 KENDRICK NUNN # / 50 PSA 8")
        self.assertEqual((f["print_run"], f["serial_num"]), (50, None))

    def test_hash_slash_one(self):
        f = fields("2024 BOWMAN CHROME UNIVERSITY #24 FLORY BIDUNGA #/1 PSA 9")
        self.assertEqual(f["print_run"], 1)

    def test_existing_serial_form_unchanged(self):
        f = fields("2020 PANINI PRIZM GREEN SCOPE #398 JUSTIN JEFFERSON 12/50 PSA 10")
        self.assertEqual((f["serial_num"], f["print_run"]), (12, 50))

    def test_one_of_one_unchanged(self):
        f = fields("2019 BOWMAN CHROME SUPERFRACTOR 1/1 #45 GRIFFIN CANNING PSA 9")
        self.assertEqual(f["print_run"], 1)

    def test_different_copies_share_keys(self):
        a = fields("2020 PANINI PRIZM SILVER #398 JUSTIN JEFFERSON 12/50 PSA 10")
        b = fields("2020 PANINI PRIZM SILVER #398 JUSTIN JEFFERSON 31/50 PSA 10")
        self.assertEqual(parse.make_keys(a), parse.make_keys(b))

    def test_numbered_does_not_group_with_base(self):
        base = fields("2018 TOPPS CHROME #25 RAFAEL DEVERS ROOKIE RC PSA 10")
        fifty = fields("2018 TOPPS CHROME #25 RAFAEL DEVERS ROOKIE RC #/50 PSA 10")
        self.assertIsNone(base["print_run"])
        self.assertEqual(fifty["print_run"], 50)
        self.assertNotEqual(parse.make_keys(base)[0], parse.make_keys(fifty)[0])
        self.assertNotEqual(parse.make_keys(base)[1], parse.make_keys(fifty)[1])

    def test_fifty_does_not_group_with_forty_nine(self):
        a = fields("2018 TOPPS CHROME #25 RAFAEL DEVERS ROOKIE RC #/50 PSA 10")
        b = fields("2018 TOPPS CHROME #25 RAFAEL DEVERS ROOKIE RC #/49 PSA 10")
        self.assertNotEqual(parse.make_keys(a)[0], parse.make_keys(b)[0])

    def test_fifty_does_not_group_with_ninety_nine(self):
        a = fields("2018 TOPPS CHROME #25 RAFAEL DEVERS ROOKIE RC #/50 PSA 10")
        b = fields("2018 TOPPS CHROME #25 RAFAEL DEVERS ROOKIE RC #/99 PSA 10")
        self.assertNotEqual(parse.make_keys(a)[0], parse.make_keys(b)[0])

    def test_slash_inside_card_number_is_not_a_print_run(self):
        # Real titles whose card NUMBER contains a slash.
        for title, expect_num in (
            ("1993 COLLECTOR'S EDGE RC F/X ROOKIES #F/X12 TROY AIKMAN PSA 8", "F/X12"),
            ("2018-19 PANINI FIFA 365 STICKERS #468A/B PHIL FODEN PSA 9", "468A/B"),
            ("1988 PANINI SUPERSPORT #69/30 CARL LEWIS PSA 6", "69/30"),
        ):
            f = fields(title)
            self.assertIsNone(f["print_run"], title)
            self.assertIsNone(f["serial_num"], title)
            self.assertEqual(f["card_number"], expect_num)

    def test_conflicting_print_runs_are_quarantined(self):
        f = parse.parse_title(
            "2020 PANINI PRIZM #398 JUSTIN JEFFERSON 12/50 #/25 PSA 10")
        self.assertIn("print_run", dict(f["issues"]))
        self.assertEqual(f["conf"]["card_number"], parse.LOW)


class TestQuarantine(unittest.TestCase):
    def test_truncated_title_missing_grade_fails(self):
        # Real title, cut off by eBay's 80-char cap so the grade never appears.
        title = ("2024 BOWMAN U BEST LET IT RAIN RELIC AUTO GOLD REFRACTOR "
                 "ASA NEWELL 48/50 PSA")
        f = fields(title)
        self.assertIsNone(f["grade_value"])
        self.assertEqual(f["truncation_risk"], 1)
        self.assertEqual(status(title), "failed")

    def test_psa_dna_autograph_is_not_a_card_grade(self):
        title = "PETE ROSE AUTOGRAPHED SIGNED TRADING CARD AUTO GRADE PSA DNA AUTO 10"
        r = parse.parse_title(title)
        self.assertIsNone(r["fields"]["grade_value"])
        self.assertIn("PSA/DNA", dict(r["issues"])["grade"])
        self.assertEqual(status(title), "failed")

    def test_authentic_with_separate_auto_grade(self):
        # "PSA AUTHENTIC AUTO 9" - the 9 grades the signature, not the card.
        f = fields("2024 PANINI PRIZM BLACK SENSATIONAL SIGNATURES "
                   "JALEN DUREN PSA AUTHENTIC AUTO 9")
        self.assertEqual(f["grade_type"], "AUTHENTIC")
        self.assertEqual(f["grade_value"], "AUTHENTIC")

    def test_missing_card_number_is_quarantined_not_guessed(self):
        title = ("2023 PANINI DONRUSS UFC SIGNATURE-HOLO BLUE LASER "
                 "RAUL ROSAS JR. 18/25 PSA 9")
        f = fields(title)
        self.assertIsNone(f["card_number"])
        self.assertIsNone(f["athlete"])  # no anchor, so no guess
        self.assertEqual(status(title), "failed")

    def test_identity_confidence_gate(self):
        good = conf("1998 UD OVATION #29 KOBE BRYANT PSA 8")
        self.assertGreaterEqual(
            parse.RANK[parse.identity_confidence(good)], parse.RANK[parse.MEDIUM]
        )
        bad = conf("2024 PANINI PRIZM SILVER PRIZM MICHAEL PENIX JR. ROOKIE R")
        self.assertLess(
            parse.RANK[parse.identity_confidence(bad)], parse.RANK[parse.MEDIUM]
        )


class TestGraderExclusion(unittest.TestCase):
    def test_rival_grader_excluded(self):
        r = parse.parse_title("2024 MARVEL FLAIR FLARIUM LVL 4 #34 DOCTOR DOOM CSG 9")
        self.assertEqual(r["excluded"], "CSG")

    def test_bgs_excluded(self):
        r = parse.parse_title("1986 TOPPS TRADED #20T JOSE CANSECO ROOKIE RC BGS 8.5")
        self.assertEqual(r["excluded"], "BGS")

    def test_psa_not_excluded(self):
        r = parse.parse_title("1992 UD MVP #67 MICHAEL JORDAN PSA 4")
        self.assertIsNone(r["excluded"])


class TestYearForms(unittest.TestCase):
    def test_four_digit_season_span(self):
        f = fields("2021-2022 SKYBOX METAL UNIVERSE #200 COLE CAUFIELD PSA 8")
        self.assertEqual(f["year_raw"], "2021-2022")
        self.assertEqual(f["year"], 2021)
        self.assertEqual(f["manufacturer"], "SKYBOX")  # not "-2022"

    def test_two_digit_season_span(self):
        f = fields("2024-25 PANINI DONRUSS FIFA NET MARVELS #20 JHON DURAN PSA 9")
        self.assertEqual(f["year"], 2024)

    def test_year_glued_to_brand(self):
        f = fields("1996TOPPS FINEST REFRACTOR #8 DELL CURRY PSA 6")
        self.assertEqual(f["year"], 1996)
        self.assertEqual(f["manufacturer"], "TOPPS")

    def test_pre_1900_year(self):
        f = fields("1880S TRADE CARD #H804 KELSEY FLINT PSA 2")
        self.assertEqual(f["year"], 1880)


class TestGraderFalsePositives(unittest.TestCase):
    def test_tag_inside_set_name_is_not_a_grader(self):
        # "TAG" appears in real Panini set names; excluding these would drop
        # legitimate PSA cards.
        for title in (
            "2023 PANINI SELECT TAG RC SWATCHES #RJSJJ JAIME JAQUEZ JR. PSA 9",
            "2013 PANINI ABSOLUTE MATERIAL-TAG #209 EJ MANUEL ROOKIE RC 84/99 PSA 9",
            "2024 TOPPS INCEPTION RC TAG RELICS #RJR-NT NIKOLA TOPIC PSA 9",
        ):
            self.assertIsNone(parse.parse_title(title)["excluded"], title)

    def test_grader_requires_adjacent_grade(self):
        self.assertEqual(parse.rival_grader("JOSE CANSECO BGS 8.5"), "BGS")
        self.assertIsNone(parse.rival_grader("PANINI SELECT TAG SWATCHES"))


class TestSport(unittest.TestCase):
    def test_explicit_league_token(self):
        self.assertEqual(fields("2025 PANINI INSTANT NFL #33 JAXSON DART ROOKIE RC PSA 8")["sport"],
                         "FOOTBALL")
        self.assertEqual(fields("2023 PANINI DONRUSS UFC #12 RAUL ROSAS PSA 9")["sport"], "MMA")
        self.assertEqual(fields("2025 PANINI DONRUSS WNBA NET MARVELS #20 ANGEL REESE PSA 10")["sport"],
                         "BASKETBALL")

    def test_unknown_when_no_explicit_evidence(self):
        # Jordan is obviously basketball, but the title says so nowhere.
        f = fields("1992 UD MVP #67 MICHAEL JORDAN PSA 4")
        self.assertIsNone(f["sport"])
        self.assertEqual(conf("1992 UD MVP #67 MICHAEL JORDAN PSA 4")["sport"],
                         parse.MISSING)


class TestKeys(unittest.TestCase):
    def test_manufacturer_alias_unifies_keys(self):
        a = parse.make_keys(fields("1998 UD OVATION #29 KOBE BRYANT PSA 8"))
        b = parse.make_keys(fields("1998 UPPER DECK OVATION #29 KOBE BRYANT PSA 8"))
        self.assertEqual(a, b)

    def test_grade_separates_slab_key_only(self):
        a = parse.make_keys(fields("1998 UD OVATION #29 KOBE BRYANT PSA 8"))
        b = parse.make_keys(fields("1998 UD OVATION #29 KOBE BRYANT PSA 6"))
        self.assertEqual(a[0], b[0])
        self.assertNotEqual(a[1], b[1])

    def test_serial_excluded_but_print_run_included(self):
        one = fields("2020 PANINI PRIZM GREEN SCOPE #398 JUSTIN JEFFERSON 36/75 PSA 10")
        two = fields("2020 PANINI PRIZM GREEN SCOPE #398 JUSTIN JEFFERSON 7/75 PSA 10")
        self.assertEqual(parse.make_keys(one), parse.make_keys(two))
        rarer = fields("2020 PANINI PRIZM GREEN SCOPE #398 JUSTIN JEFFERSON 7/10 PSA 10")
        self.assertNotEqual(parse.make_keys(one)[0], parse.make_keys(rarer)[0])

    def test_parallel_separates_keys(self):
        base = parse.make_keys(fields("2020 PANINI PRIZM #398 JUSTIN JEFFERSON PSA 10"))
        green = parse.make_keys(fields("2020 PANINI PRIZM GREEN SCOPE #398 JUSTIN JEFFERSON PSA 10"))
        self.assertNotEqual(base[0], green[0])

    def test_auto_separates_keys(self):
        plain = parse.make_keys(fields("2022 BOWMAN CHROME #BCP1 JACKSON CHOURIO PSA 10"))
        auto = parse.make_keys(fields("2022 BOWMAN CHROME #BCP1 JACKSON CHOURIO PSA 10 AUTO"))
        self.assertNotEqual(plain[0], auto[0])


class TestNormalization(unittest.TestCase):
    def test_accent_and_quote_normalization(self):
        a = fields("2021 TOPPS CHROME #150 JOSÉ RAMÍREZ PSA 10")["athlete"]
        b = fields("2021 TOPPS CHROME #150 JOSE RAMIREZ PSA 10")["athlete"]
        self.assertEqual(a, b)

    def test_whitespace_collapse(self):
        f = fields("1992   UD   MVP   #67   MICHAEL JORDAN   PSA 4")
        self.assertEqual(f["set_name"], "MVP")
        self.assertEqual(f["athlete"], "MICHAEL JORDAN")


if __name__ == "__main__":
    unittest.main()


class TestTierBCanonicalization(unittest.TestCase):
    """Step 3A: Tier B formatting must not read as an identity conflict."""

    def setUp(self):
        import enrich
        self.e = enrich

    def test_year_and_manufacturer_prefix_stripped(self):
        for raw, year, mfr, expect in (
            ("2018 PANINI PRIZM", 2018, "PANINI", "PRIZM"),
            ("2011 BOWMAN CHROME", 2011, "BOWMAN", "CHROME"),
            ("2023 PANINI DONRUSS OPTIC", 2023, "PANINI", "DONRUSS OPTIC"),
            ("2024 TOPPS NOW", 2024, "TOPPS", "NOW"),
            ("2018 BOWMAN'S BEST", 2018, "BOWMAN", "BEST"),
        ):
            self.assertEqual(self.e.canonical_set(raw, year, mfr), expect, raw)

    def test_set_name_containing_maker_word_survives(self):
        # DONRUSS is a Panini product; only a LEADING maker token is stripped.
        self.assertEqual(
            self.e.canonical_set("2023 PANINI DONRUSS", 2023, "PANINI"), "DONRUSS")

    def test_empty_set_span_is_base(self):
        self.assertEqual(self.e.canonical_set("1989 BOWMAN", 1989, "BOWMAN"), "BASE")

    def test_parallel_order_and_punctuation_insensitive(self):
        a = self.e.canonical_parallel("GREEN LASER HOLO", "DONRUSS", "PANINI")
        b = self.e.canonical_parallel("HOLO GREEN LASER", "DONRUSS", "PANINI")
        self.assertEqual(a, b)
        c = self.e.canonical_parallel("CHROME BLUE SPARKLE REFRACTOR", "HERITAGE", "TOPPS")
        d = self.e.canonical_parallel("CHROME-BLUE SPARKLE REFRACTOR", "HERITAGE", "TOPPS")
        self.assertEqual(c, d)

    def test_product_word_dropped_from_parallel(self):
        # "SILVER PRIZM" in the Prizm set is the SILVER parallel.
        self.assertEqual(
            self.e.canonical_parallel("SILVER PRIZM", "PRIZM", "PANINI"),
            self.e.canonical_parallel("SILVER", "PRIZM", "PANINI"))

    def test_different_parallels_still_differ(self):
        self.assertNotEqual(
            self.e.canonical_parallel("SILVER", "PRIZM", "PANINI"),
            self.e.canonical_parallel("GOLD", "PRIZM", "PANINI"))
        self.assertNotEqual(
            self.e.canonical_parallel("BLUE REFRACTOR", "CHROME", "TOPPS"),
            self.e.canonical_parallel("BLUE CRACKED ICE REFRACTOR", "CHROME", "TOPPS"))

    def test_boundary_shift_matches_on_union(self):
        # eBay calls ALL-STAR a Parallel/Variety; the title reads it as the set.
        a = self.e.identity_tokens("ALL STAR", None, "HOOPS")
        b = self.e.identity_tokens("BASE", "ALL-STAR", "HOOPS")
        self.assertEqual(a, b)

    def test_boundary_shift_does_not_hide_a_real_difference(self):
        a = self.e.identity_tokens("FOILBOARD", "RAINBOW", "TOPPS")
        b = self.e.identity_tokens("BASE", "RAINBOW FOILBOARD", "TOPPS")
        self.assertEqual(a, b)
        c = self.e.identity_tokens("BASE", "GOLD FOILBOARD", "TOPPS")
        self.assertNotEqual(a, c)

    def test_token_synonyms(self):
        self.assertEqual(
            self.e.canonical_set("2024 TOPPS CHROME ROOKIE AUTOS VARIATIONS", 2024, "TOPPS"),
            self.e.canonical_set("2024 TOPPS CHROME ROOKIE AUTOGRAPHS VARIATIONS", 2024, "TOPPS"))


class TestCanonicalDoesNotCollapseMeaning(unittest.TestCase):
    """The union rule may absorb field-boundary differences; it must NEVER
    discard a meaningful unmatched token."""

    def setUp(self):
        import enrich
        self.e = enrich

    def _par(self, text, set_canon="PRIZM", mfr="PANINI"):
        return self.e.canonical_parallel(text, set_canon, mfr)

    def test_distinct_parallels_never_collapse(self):
        cases = [
            ("SILVER", "SILVER WAVE"),
            ("SILVER PRIZM", "SILVER WAVE PRIZM"),
            ("RED", "RED ICE"),
            ("RED ICE", "RED WAVE"),
            ("BLUE", "BLUE SPARKLE"),
            ("REFRACTOR", "X-FRACTOR"),
            ("REFRACTOR", "SUPERFRACTOR"),
            ("HOLO", "HOLO LASER"),
            ("GOLD", "GOLD VINYL"),
            ("GREEN SCOPE", "GREEN"),
        ]
        for a, b in cases:
            self.assertNotEqual(self._par(a), self._par(b), f"{a!r} vs {b!r}")

    def test_base_never_equals_an_explicit_parallel(self):
        base = self.e.identity_tokens("BASE", None, "PANINI")
        for p in ("SILVER", "RED ICE", "REFRACTOR", "GOLD WAVE"):
            self.assertNotEqual(
                base, self.e.identity_tokens("BASE", p, "PANINI"), p)

    def test_extra_token_always_breaks_the_union(self):
        a = self.e.identity_tokens("PRIZM", "SILVER", "PANINI")
        for extra in ("SILVER WAVE", "SILVER ICE", "SILVER CRACKED ICE"):
            self.assertNotEqual(
                a, self.e.identity_tokens("PRIZM", extra, "PANINI"), extra)

    def test_boundary_shift_only_absorbs_when_union_is_identical(self):
        # Same tokens either side of the set/parallel boundary -> equal.
        self.assertEqual(
            self.e.identity_tokens("FOILBOARD", "RAINBOW", "TOPPS"),
            self.e.identity_tokens("BASE", "RAINBOW FOILBOARD", "TOPPS"))
        # One extra token anywhere -> not equal.
        self.assertNotEqual(
            self.e.identity_tokens("FOILBOARD", "RAINBOW", "TOPPS"),
            self.e.identity_tokens("BASE", "RAINBOW FOILBOARD GOLD", "TOPPS"))

    def test_material_verdict_on_unmatched_parallel_token(self):
        a = {"set_name": "PRIZM", "parallel": "SILVER", "year": 2020,
             "manufacturer": "PANINI", "card_number": "1", "grade_value": "10",
             "grade_qualifier": None, "auto_grade": None, "print_run": None,
             "serial_num": None, "sport": "BASKETBALL",
             "parallel_conf": parse.HIGH}
        b = {"grader": "Professional Sports Authenticator (PSA)", "grade": "10",
             "sport": "Basketball", "card_number": "1", "season": "2020",
             "set_name": "2020 PANINI PRIZM", "parallel": "SILVER WAVE"}
        for f in self.e.NOT_PROVIDED:
            b.setdefault(f, None)
        findings, _, verdict, _ = self.e.compare(a, b)
        self.assertEqual(verdict, "quarantined")
        self.assertIn("parallel",
                      [f["field"] for f in findings
                       if f["severity"] == self.e.MATERIAL])


class TestBoundaryVsIdentityChange(unittest.TestCase):
    """Field-boundary differences may reconcile; new identity tokens may not."""

    def setUp(self):
        import enrich
        self.e = enrich

    def test_sapphire_edition_boundary_split(self):
        # Tier A: set "CHROME FORMULA 1 EDITION" + parallel "GOLD SAPPHIRE"
        # Tier B: set "CHROME FORMULA 1 SAPPHIRE EDITION" + parallel "GOLD"
        a = self.e.identity_tokens("CHROME FORMULA 1 EDITION", "GOLD SAPPHIRE", "TOPPS")
        b = self.e.identity_tokens("CHROME FORMULA 1 SAPPHIRE EDITION", "GOLD", "TOPPS")
        self.assertEqual(a, b)

    def test_wnba_logo_matches_only_the_same_identity(self):
        a = self.e.identity_tokens("PRIZM WNBA LOGO", None, "PANINI")
        b = self.e.identity_tokens("PRIZM WNBA", "WNBA LOGO PRIZM", "PANINI")
        self.assertEqual(a, b)
        # A different WNBA parallel must NOT match.
        c = self.e.identity_tokens("PRIZM WNBA", "WNBA LOGO GOLD PRIZM", "PANINI")
        self.assertNotEqual(a, c)
        d = self.e.identity_tokens("PRIZM WNBA", "SILVER PRIZM", "PANINI")
        self.assertNotEqual(a, d)

    def test_player_names_are_not_a_parallel(self):
        import db
        self.e.load_surnames(db.connect())
        self.assertTrue(self.e.is_name_only_parallel(
            "JORDAN/WILKINS/MALONE", "SCORING LEADERS", "SCORING LEADERS"))
        # A real parallel is never mistaken for names.
        for real in ("WNBA LOGO PRIZM", "SPECKLE REFRACTOR", "GOLD", "RED ICE"):
            self.assertFalse(self.e.is_name_only_parallel(
                real, "CAMERON BRINK", "CAMERON BRINK"), real)

    def test_added_identity_token_always_changes_identity(self):
        base = self.e.identity_tokens("PRIZM", "SILVER", "PANINI")
        for extra in ("SILVER SPECKLE", "SILVER VAPORWAVE", "SILVER VARIATION",
                      "SILVER ICE", "SILVER WAVE", "SILVER REFRACTOR"):
            self.assertNotEqual(base,
                                self.e.identity_tokens("PRIZM", extra, "PANINI"),
                                extra)

    def test_print_run_changes_identity(self):
        import parse as p
        f = {"sport": "BASKETBALL", "year": 2020, "manufacturer": "PANINI",
             "set_name": "PRIZM", "insert_name": None, "parallel": "SILVER",
             "card_number": "1", "athlete": "X", "is_auto": 0, "is_relic": 0,
             "print_run": None, "grade_type": "NUMERIC", "grade_value": "10",
             "grade_qualifier": None, "auto_grade": None}
        g = dict(f, print_run=50)
        self.assertNotEqual(p.make_keys(f)[0], p.make_keys(g)[0])

    def test_season_span_and_sport_aliases(self):
        self.assertEqual(self.e.canonical_set("2023-24 TOPPS MERLIN", 2023, "TOPPS"),
                         "MERLIN")
        self.assertEqual(self.e.canonical_sport("Mixed Martial Arts (MMA)"), "MMA")
        self.assertEqual(self.e.canonical_sport("Auto Racing"), "RACING")

    def test_uefa_optional_only_when_rest_matches(self):
        ok, _ = self.e.sets_reconcile("CHROME CHAMPIONS LEAGUE",
                                      "CHROME UEFA CHAMPIONS LEAGUE")
        self.assertTrue(ok)
        bad, _ = self.e.sets_reconcile("CHROME CHAMPIONS LEAGUE",
                                       "CHROME UEFA EUROPA LEAGUE")
        self.assertFalse(bad)
