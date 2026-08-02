"""Offline tests for the Product Research collector.

No browser is launched and no request reaches eBay. Every fixture under
tests/fixtures/product_research/ is sanitized HTML written by hand.

The live smoke test is opt-in and is NOT part of this suite - run
product_research_playwright.py manually for that.

Run:  python -m unittest -v test_product_research
"""

import json
import os
import tempfile
import unittest

import card_vocab
import db
import enrich
import manual_comps as mc
import parse
import product_research_parse as prp
import product_research_playwright as pw

FIX = os.path.join("tests", "fixtures", "product_research")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


HASBULLA_ID = "v1|298544784209|0"
JORDAN_ID = "v1|117330553310|0"


def pick(cands, *must_contain):
    """The one pooled candidate whose title contains every given fragment.

    `next(c for c in ... if "GRIFFEY" in c["title"])` silently re-binds to a
    different card whenever the pool grows, turning a real assertion into a
    test of some unrelated listing. This refuses to guess: no match skips, and
    an ambiguous match fails loudly.
    """
    hits = [c for c in cands.values()
            if all(f.upper() in c["title"].upper() for f in must_contain)]
    if len(hits) > 1:
        raise AssertionError(
            f"{must_contain} matches {len(hits)} candidates: "
            + "; ".join(h["title"][:60] for h in hits))
    return hits[0] if hits else None


class TestPageState(unittest.TestCase):
    def test_results_detected(self):
        self.assertEqual(prp.detect_page_state(fixture("results_hasbulla.html")),
                         prp.RESULTS_OK)

    def test_empty_results_detected(self):
        self.assertEqual(prp.detect_page_state(fixture("empty_results.html")),
                         prp.EMPTY_RESULTS)

    def test_login_page_detected(self):
        self.assertEqual(prp.detect_page_state(fixture("login_required.html")),
                         prp.LOGIN_REQUIRED)

    def test_verification_page_detected(self):
        self.assertEqual(prp.detect_page_state(fixture("verification.html")),
                         prp.VERIFICATION_REQUIRED)

    def test_blank_page_is_unknown(self):
        self.assertEqual(prp.detect_page_state(""), prp.UNKNOWN_PAGE)


class TestTableParsing(unittest.TestCase):
    def test_header_and_rows_extracted(self):
        header, rows = prp.parse_html_table(fixture("results_hasbulla.html"))
        self.assertIn("Title", header)
        self.assertEqual(len(rows), 5)

    def test_date_range_recorded(self):
        self.assertEqual(pw.read_date_range(fixture("results_hasbulla.html")),
                         "Last 90 days")
        self.assertIn("1/1/2026",
                      pw.read_date_range(fixture("best_offer_and_edges.html")))

    def test_rows_mapped_to_internal_shape(self):
        header, rows = prp.parse_html_table(fixture("results_hasbulla.html"))
        out = prp.rows_from_table(header, rows, HASBULLA_ID, "q", "STRICT")
        self.assertEqual(len(out), 5)
        r = out[0]
        self.assertEqual(r["candidate_item_id"], HASBULLA_ID)
        self.assertEqual(r["query_tier"], "STRICT")
        self.assertEqual(r["source"], "EBAY_PRODUCT_RESEARCH")
        self.assertEqual(r["sold_price"], "78.61")
        self.assertEqual(r["shipping"], "4.99")
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["sale_type"], "FIXED_PRICE")
        self.assertTrue(r["raw_text"])
        self.assertTrue(r["collected_at"])

    def test_extraction_matches_the_manual_reference_totals(self):
        """The five reference totals must survive DOM extraction intact."""
        header, rows = prp.parse_html_table(fixture("results_hasbulla.html"))
        out = prp.rows_from_table(header, rows, HASBULLA_ID, "q", "STRICT")
        totals = [round(float(r["sold_price"]) + float(r["shipping"]), 2)
                  for r in out]
        self.assertEqual(sorted(totals),
                         sorted([83.60, 60.00, 50.99, 57.00, 66.75]))

    def test_auction_sale_type(self):
        header, rows = prp.parse_html_table(fixture("results_hasbulla.html"))
        out = prp.rows_from_table(header, rows, HASBULLA_ID, "q", "STRICT")
        self.assertEqual(out[1]["sale_type"], "AUCTION")


class TestEdgeRows(unittest.TestCase):
    def rows(self):
        header, rows = prp.parse_html_table(fixture("best_offer_and_edges.html"))
        return prp.rows_from_table(header, rows, JORDAN_ID, "q", "STRICT")

    def test_best_offer_without_accepted_price_is_not_valued(self):
        r = self.rows()[0]
        self.assertEqual(r["sale_type"], "BEST_OFFER")
        self.assertEqual(r["actual_price_known"], "false")
        self.assertEqual(r["sold_price"], "")
        self.assertEqual(r["displayed_original_price"], "200.0")

    def test_best_offer_with_accepted_price_is_valued(self):
        r = self.rows()[1]
        self.assertEqual(r["actual_price_known"], "true")
        self.assertEqual(r["sold_price"], "150.0")

    def test_foreign_currency_preserved(self):
        r = self.rows()[2]
        self.assertEqual(r["currency"], "GBP")
        self.assertEqual(r["sold_price"], "99.0")

    def test_malformed_date_becomes_null_downstream(self):
        self.assertEqual(self.rows()[2]["sale_date"], "not-a-date")
        self.assertIsNone(mc.parse_date("not-a-date"))

    def test_missing_shipping_left_empty(self):
        self.assertEqual(self.rows()[3]["shipping"], "")


class TestQueryGeneration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cands = mc.load_candidates()

    def test_no_duplicate_or_redundant_words(self):
        c = self.cands[HASBULLA_ID]
        q = prp.build_query(c, "STRICT")
        tokens = [t.upper() for t in q.split()]
        self.assertEqual(len(tokens), len(set(tokens)), q)

    def test_autograph_keyword_not_duplicated(self):
        """A parallel already saying AUTOGRAPH must not also add "auto"."""
        cands = [c for c in self.cands.values()
                 if c["is_auto"] and "AUTOGRAPH" in (c["parallel"] or "")]
        if not cands:
            self.skipTest("no signed candidate whose parallel says AUTOGRAPH")
        for c in cands:                      # holds for every one, not just the first
            q = prp.build_query(c, "STRICT").upper()
            self.assertIn("AUTOGRAPH", q, c["title"])
            self.assertNotIn(" AUTO ", f" {q} ", c["title"])

    def test_autograph_marker_kept_when_parallel_lacks_it(self):
        """When nothing else marks it signed, the "auto" keyword must stay."""
        cands = [c for c in self.cands.values()
                 if c["is_auto"] and "AUTOGRAPH" not in (c["parallel"] or "")]
        if not cands:
            self.skipTest("no signed candidate whose parallel omits AUTOGRAPH")
        for c in cands:
            self.assertIn(" AUTO ", f" {prp.build_query(c, 'STRICT').upper()} ",
                          c["title"])

    def test_strict_keeps_every_material_field(self):
        q = prp.build_query(self.cands[HASBULLA_ID], "STRICT")
        for token in ("2023", "HASBULLA", "#200", "RED", "/199", "PSA", "9"):
            self.assertIn(token, q)

    def test_relaxed_only_offered_when_rules_allow(self):
        levels = dict(prp.query_levels(self.cands[HASBULLA_ID]))
        self.assertNotIn("RELAXED", levels)          # print run must not relax
        base = dict(prp.query_levels(self.cands[JORDAN_ID]))
        self.assertIn("STRICT", base)

    def test_identical_tiers_collapse(self):
        for c in self.cands.values():
            qs = [q for _lvl, q in prp.query_levels(c)]
            self.assertEqual(len(qs), len(set(qs)))


class TestMatcherIndependence(unittest.TestCase):
    """Appearing in a candidate's search must never imply acceptance."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cands = mc.load_candidates()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))

    def import_fixture(self):
        header, rows = prp.parse_html_table(fixture("results_hasbulla.html"))
        out = prp.rows_from_table(header, rows, HASBULLA_ID, "q", "STRICT")
        return out, mc.import_rows(self.conn, out, attribute_by_title=False)

    def test_matcher_decides_row_by_row(self):
        out, stats = self.import_fixture()
        self.assertEqual(stats["rows"], 5)
        self.assertEqual(stats["accepted"] + stats["rejected"], 5)
        # Not every row that appeared under the search is a comp.
        self.assertLess(stats["accepted"], 5)

    def test_same_print_run_different_serial_accepted(self):
        self.import_fixture()
        acc = self.conn.execute(
            "SELECT raw_title FROM sold_comps WHERE accepted=1").fetchall()
        titles = " ".join(r["raw_title"] for r in acc)
        self.assertIn("57/199", titles)
        self.assertIn("112/199", titles)

    def test_grade_mismatch_rejected(self):
        self.import_fixture()
        r = self.conn.execute(
            "SELECT rejection_reason FROM sold_comps "
            "WHERE raw_title LIKE '%41/199%'").fetchone()
        self.assertIn("PSA grade", r["rejection_reason"])

    def test_different_parallel_and_print_run_rejected(self):
        self.import_fixture()
        r = self.conn.execute(
            "SELECT rejection_reason FROM sold_comps "
            "WHERE raw_title LIKE '%12/99%'").fetchone()
        self.assertIsNotNone(r["rejection_reason"])

    def test_title_missing_print_run_is_not_auto_accepted(self):
        """The search included /199; the title does not. Evidence must decide."""
        self.import_fixture()
        r = self.conn.execute(
            "SELECT accepted, rejection_reason FROM sold_comps "
            "WHERE raw_title LIKE '%Magomedov PSA 9%' "
            "AND raw_title NOT LIKE '%/199%'").fetchone()
        self.assertEqual(r["accepted"], 0)
        self.assertIn("print run", r["rejection_reason"])

    def test_duplicate_rows_deduplicated(self):
        self.import_fixture()
        _out, stats = self.import_fixture()
        self.assertEqual(stats["duplicate"], 5)


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))

    def test_status_roundtrip_and_update(self):
        pw.set_status(self.conn, "c1", pw.RUNNING, attempts=1)
        self.assertEqual(pw.get_status(self.conn, "c1")["status"], pw.RUNNING)
        pw.set_status(self.conn, "c1", pw.COMPLETED, accepted=4, rejected=1,
                      date_range="Last 90 days")
        st = pw.get_status(self.conn, "c1")
        self.assertEqual(st["status"], pw.COMPLETED)
        self.assertEqual(st["accepted"], 4)
        self.assertEqual(st["attempts"], 1)          # earlier field preserved
        self.assertEqual(st["date_range"], "Last 90 days")

    def test_unknown_candidate_has_no_status(self):
        self.assertIsNone(pw.get_status(self.conn, "nope"))

    def _args(self, **over):
        class A:
            candidate_id = None
            limit = None
            resume = False
            retry_failed = False
            force = False
        a = A()
        for k, v in over.items():
            setattr(a, k, v)
        return a

    def test_completed_candidates_are_skipped(self):
        first = pw.select_candidates(self.conn, self._args(limit=2))
        self.assertEqual(len(first), 2)
        pw.set_status(self.conn, first[0]["item_id"], pw.COMPLETED)
        again = pw.select_candidates(self.conn, self._args(limit=2))
        self.assertNotIn(first[0]["item_id"], [c["item_id"] for c in again])

    def test_force_reprocesses_completed(self):
        one = pw.select_candidates(self.conn, self._args(limit=1))[0]
        pw.set_status(self.conn, one["item_id"], pw.COMPLETED)
        forced = pw.select_candidates(self.conn, self._args(limit=1, force=True))
        self.assertEqual(forced[0]["item_id"], one["item_id"])

    def test_retry_failed_picks_up_failures_only(self):
        cands = pw.select_candidates(self.conn, self._args(limit=3))
        pw.set_status(self.conn, cands[0]["item_id"], pw.FAILED)
        pw.set_status(self.conn, cands[1]["item_id"], pw.COMPLETED)
        retry = pw.select_candidates(self.conn, self._args(retry_failed=True))
        ids = [c["item_id"] for c in retry]
        self.assertIn(cands[0]["item_id"], ids)
        self.assertNotIn(cands[1]["item_id"], ids)

    def test_candidate_id_selects_one(self):
        got = pw.select_candidates(self.conn, self._args(candidate_id=JORDAN_ID))
        self.assertEqual([c["item_id"] for c in got], [JORDAN_ID])

    def test_pilot_five_are_first(self):
        five = pw.select_candidates(self.conn, self._args(limit=5))
        self.assertEqual(len(five), 5)
        self.assertTrue(set(c["item_id"] for c in five) <= set(mc.PILOT))


class TestNoLiveCalls(unittest.TestCase):
    def test_parser_never_imports_playwright(self):
        import inspect
        self.assertNotIn("playwright", inspect.getsource(prp))

    def test_driver_does_not_store_credentials(self):
        import inspect
        src = inspect.getsource(pw).lower()
        for banned in ("password", "cookie", "token", "storage_state"):
            self.assertNotIn(f'"{banned}"', src,
                             f'{banned!r} appears as a literal in the driver')


if __name__ == "__main__":
    unittest.main()


class TestAttachMode(unittest.TestCase):
    """Tab selection for --connect-existing. No browser is launched here."""

    # The exact targets the CDP endpoint exposed in the field.
    REAL_TARGETS = [
        "chrome://omnibox-popup.top-chrome/",
        "chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html",
        "https://www.ebay.com/sh/research?marketplace=EBAY-US&tabName=SOLD",
    ]

    class FakePage:
        def __init__(self, url, closed=False):
            self.url = url
            self._closed = closed

        def is_closed(self):
            return self._closed

    def pages(self, urls):
        return [self.FakePage(u) for u in urls]

    def test_real_cdp_targets_select_product_research(self):
        picked = pw.pick_page(self.pages(self.REAL_TARGETS))
        self.assertEqual(picked.url, self.REAL_TARGETS[2])

    def test_real_cdp_targets_in_any_order(self):
        import itertools
        for order in itertools.permutations(self.REAL_TARGETS):
            picked = pw.pick_page(self.pages(list(order)))
            self.assertEqual(picked.url, self.REAL_TARGETS[2], order)

    def test_internal_targets_are_ignored(self):
        for url in ("chrome://omnibox-popup.top-chrome/", "devtools://devtools/x",
                    "chrome-extension://abc/page.html", "about:blank", ""):
            self.assertTrue(pw.is_internal_url(url), url)
        for url in ("https://www.ebay.com/sh/research", "http://example.com"):
            self.assertFalse(pw.is_internal_url(url), url)

    def test_omnibox_never_selected_when_a_normal_page_exists(self):
        picked = pw.pick_page(self.pages([
            "chrome://omnibox-popup.top-chrome/", "https://news.example.com"]))
        self.assertEqual(picked.url, "https://news.example.com")

    def test_only_internal_targets_yields_none(self):
        self.assertIsNone(pw.pick_page(self.pages(self.REAL_TARGETS[:2])))

    def test_priority_order(self):
        self.assertEqual(pw.rank_page("https://www.ebay.com/sh/research?x=1"), 0)
        self.assertEqual(pw.rank_page("https://www.ebay.com/mys/home"), 1)
        self.assertEqual(pw.rank_page("https://news.example.com"), 2)
        self.assertLess(pw.rank_page("https://www.ebay.com/sh/research"),
                        pw.rank_page("https://www.ebay.com/itm/1"))

    def test_prefers_product_research_tab(self):
        pages = [self.FakePage("https://www.ebay.com/itm/123"),
                 self.FakePage("https://www.ebay.com/sh/research?tab=sold"),
                 self.FakePage("https://news.example.com")]
        self.assertIn("/sh/research", pw.pick_page(pages).url)

    def test_falls_back_to_any_ebay_tab(self):
        pages = [self.FakePage("https://news.example.com"),
                 self.FakePage("https://www.ebay.com/mys/home")]
        self.assertIn("ebay.com", pw.pick_page(pages).url)

    def test_falls_back_to_first_open_tab(self):
        pages = [self.FakePage("https://news.example.com")]
        self.assertEqual(pw.pick_page(pages).url, "https://news.example.com")

    def test_closed_tabs_ignored(self):
        pages = [self.FakePage("https://www.ebay.com/sh/research", closed=True),
                 self.FakePage("https://www.ebay.com/mys/home")]
        self.assertEqual(pw.pick_page(pages).url, "https://www.ebay.com/mys/home")

    def test_no_pages_returns_none(self):
        self.assertIsNone(pw.pick_page([]))


class TestNavigationRace(unittest.TestCase):
    """eBay re-navigates Seller Hub after first paint; content() can lose the race."""

    NAV_MSG = ("Page.content: Unable to retrieve content because the page is "
               "navigating and changing the content.")

    class FakePage:
        """Raises a navigation error `fail_times` times, then returns html."""

        def __init__(self, html="<html><body>ok</body></html>", fail_times=0,
                     other_error=None, url="https://www.ebay.com/sh/research"):
            self.html = html
            self.fail_times = fail_times
            self.other_error = other_error
            self.url = url
            self.calls = 0
            self.goto_calls = 0
            self.load_states = []

        def wait_for_load_state(self, state, timeout=None):
            self.load_states.append(state)

        def wait_for_timeout(self, ms):
            pass

        def goto(self, url, **kw):
            self.goto_calls += 1
            self.url = url

        def content(self):
            self.calls += 1
            if self.other_error:
                raise RuntimeError(self.other_error)
            if self.calls <= self.fail_times:
                raise RuntimeError(TestNavigationRace.NAV_MSG)
            return self.html

    def test_navigation_error_recognized(self):
        self.assertTrue(pw.is_navigation_error(RuntimeError(self.NAV_MSG)))
        self.assertTrue(pw.is_navigation_error(
            RuntimeError("Execution context was destroyed")))
        self.assertFalse(pw.is_navigation_error(RuntimeError("Target closed")))

    def test_transient_error_then_success(self):
        page = self.FakePage(fail_times=2)
        html = pw.safe_content(page, timeout_ms=10, base_delay=0.001)
        self.assertIn("ok", html)
        self.assertEqual(page.calls, 3)          # two failures, then success

    def test_single_transient_error_recovers(self):
        page = self.FakePage(fail_times=1)
        self.assertIn("ok", pw.safe_content(page, timeout_ms=10, base_delay=0.001))

    def test_non_navigation_error_is_reraised_immediately(self):
        page = self.FakePage(other_error="Target page, context or browser closed")
        with self.assertRaises(RuntimeError):
            pw.safe_content(page, timeout_ms=10, base_delay=0.001)
        self.assertEqual(page.calls, 1)          # no pointless retries

    def test_persistent_navigation_error_gives_up(self):
        page = self.FakePage(fail_times=99)
        with self.assertRaises(RuntimeError):
            pw.safe_content(page, timeout_ms=10, attempts=3, base_delay=0.001)
        self.assertEqual(page.calls, 3)

    def test_never_waits_for_networkidle(self):
        page = self.FakePage()
        pw.safe_content(page, timeout_ms=10, base_delay=0.001)
        self.assertNotIn("networkidle", page.load_states)
        self.assertIn("domcontentloaded", page.load_states)

    def test_open_research_survives_a_race(self):
        page = self.FakePage(html=fixture("results_hasbulla.html"), fail_times=1)
        self.assertEqual(pw.open_research(page, 10), prp.RESULTS_OK)

    def test_open_research_does_not_navigate_when_already_on_research(self):
        page = self.FakePage(html=fixture("results_hasbulla.html"))
        pw.open_research(page, 10, navigate=False)
        self.assertEqual(page.goto_calls, 0)

    def test_open_research_reuses_tab_already_on_research_even_when_navigating(self):
        page = self.FakePage(html=fixture("results_hasbulla.html"),
                             url="https://www.ebay.com/sh/research?tab=SOLD")
        pw.open_research(page, 10, navigate=True)
        self.assertEqual(page.goto_calls, 0)

    def test_open_research_navigates_only_from_an_unrelated_tab(self):
        page = self.FakePage(html=fixture("empty_results.html"),
                             url="https://news.example.com")
        pw.open_research(page, 10, navigate=True)
        self.assertEqual(page.goto_calls, 1)

    def test_check_connection_reads_existing_tab_without_navigating(self):
        page = self.FakePage(html=fixture("results_hasbulla.html"),
                             url="https://news.example.com")
        state = pw.open_research(page, 10, navigate=False)
        self.assertEqual(page.goto_calls, 0)     # never yanks your tab away
        self.assertEqual(state, prp.RESULTS_OK)


class TestProductResearchDetection(unittest.TestCase):
    """Detection of the live Seller Hub Product Research (Terapeak) page.

    Fixture is a sanitized reduction of the real capture that returned
    unknown_page (data/playwright/artifacts/check_unknown.html).
    """

    LIVE = "live_initial_sold_page.html"

    # A generic eBay page must NOT satisfy Product Research detection.
    GENERIC_EBAY = """<html><head><title>eBay</title></head><body>
      <nav aria-label="Seller Hub"><span id="Research">Research</span></nav>
      <h1>My eBay Summary</h1><p>Watchlist</p></body></html>"""

    def test_live_initial_sold_page_is_recognized(self):
        html = fixture(self.LIVE)
        self.assertTrue(prp.is_product_research(html))
        self.assertEqual(prp.detect_page_state(html), prp.RESEARCH_READY)

    def test_no_row_count_required(self):
        """The page has no results table at all and must still be recognized."""
        html = fixture(self.LIVE)
        header, rows = prp.parse_html_table(html)
        self.assertIsNone(header)
        self.assertEqual(rows, [])
        self.assertEqual(prp.detect_page_state(html), prp.RESEARCH_READY)

    def test_each_marker_alone_is_sufficient(self):
        for marker in prp.PRODUCT_RESEARCH_MARKERS:
            page = f"<html><body><div class='x'>{marker}</div></body></html>"
            self.assertTrue(prp.is_product_research(page), marker)

    def test_title_alone_is_sufficient(self):
        page = "<html><head><title>Product Research - eBay Seller Hub</title>" \
               "</head><body></body></html>"
        self.assertTrue(prp.is_product_research(page))

    def test_generic_ebay_page_is_not_product_research(self):
        self.assertFalse(prp.is_product_research(self.GENERIC_EBAY))
        self.assertNotEqual(prp.detect_page_state(self.GENERIC_EBAY),
                            prp.RESEARCH_READY)

    def test_populated_table_still_returns_results_ok(self):
        self.assertEqual(prp.detect_page_state(fixture("results_hasbulla.html")),
                         prp.RESULTS_OK)

    def test_login_outranks_research_markers(self):
        html = fixture(self.LIVE).replace(
            "<body>", "<body><h1>Sign in to your account</h1>")
        self.assertEqual(prp.detect_page_state(html), prp.LOGIN_REQUIRED)

    def test_verification_outranks_research_markers(self):
        html = fixture(self.LIVE).replace(
            "<body>", "<body><h1>Let's verify your identity</h1>")
        self.assertEqual(prp.detect_page_state(html), prp.VERIFICATION_REQUIRED)

    def test_empty_results_outranks_ready(self):
        html = fixture(self.LIVE).replace(
            "<!-- no query has been run yet -->", "No results found")
        self.assertEqual(prp.detect_page_state(html), prp.EMPTY_RESULTS)

    def test_script_text_cannot_trigger_auth_states(self):
        """An i18n or tracking blob mentioning 'captcha' must not fool us."""
        html = ("<html><head><title>Product Research - eBay Seller Hub</title>"
                "<script>var msgs={a:'captcha',b:'please sign in'}</script>"
                "</head><body><div class='research-container'></div></body></html>")
        self.assertEqual(prp.detect_page_state(html), prp.RESEARCH_READY)

    def test_fixture_carries_no_personal_content(self):
        body = prp.visible_text(fixture(self.LIVE))
        for leak in ("hi tomer", "psa vault", "watchlist", "purchase history"):
            self.assertNotIn(leak, body, leak)


class TestPopulatedExtraction(unittest.TestCase):
    """Div/grid extraction from the live sold-results layout.

    Titles, prices, shipping and dates are the real values read off the live
    page; the surrounding DOM shape is inferred (see fixture header).
    """

    GRID = "live_populated_sold_grid.html"
    DIVS = "live_populated_sold_divs.html"

    def test_grid_layout_extracts_every_row(self):
        recs = prp.extract_result_rows(fixture(self.GRID))
        self.assertEqual(len(recs), 8)

    def test_plain_divs_extract_every_row(self):
        """No role attributes and no meaningful class names."""
        recs = prp.extract_result_rows(fixture(self.DIVS))
        self.assertEqual(len(recs), 8)

    def test_fields_are_captured(self):
        rec = prp.extract_result_rows(fixture(self.GRID))[1]
        self.assertIn("Red Prizm 57/199", rec["listing"])
        self.assertEqual(rec["avg_sold_price"], "$75.00")
        self.assertEqual(rec["avg_shipping"], "$8.60")
        self.assertEqual(rec["date_last_sold"], "Jul 13, 2026")
        self.assertEqual(rec["source_item_id"], "800000000102")

    def test_column_labels_come_from_the_app(self):
        order = prp.column_order(
            prp.BeautifulSoup(fixture(self.GRID), "html.parser"))
        for key in ("listing", "avg_sold_price", "avg_shipping", "total_sold",
                    "item_sales", "date_last_sold"):
            self.assertIn(key, order)

    def test_records_map_to_internal_rows(self):
        recs = prp.extract_result_rows(fixture(self.GRID))
        rows = prp.records_to_rows(recs, "cand", "q", "NORMAL")
        self.assertEqual(len(rows), 8)
        r = rows[1]
        self.assertEqual(r["sold_price"], "75.0")
        self.assertEqual(r["shipping"], "8.6")
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["source"], "EBAY_PRODUCT_RESEARCH")
        self.assertTrue(r["collected_at"])

    def test_table_parser_still_works_as_fallback(self):
        recs = prp.extract_result_rows(fixture("results_hasbulla.html"))
        self.assertEqual(len(recs), 5)
        header, rows = prp.parse_html_table(fixture("results_hasbulla.html"))
        self.assertEqual(len(rows), 5)


class TestPopulatedStateMachine(unittest.TestCase):
    """Requirement: populated-but-unparsed is never reported as no_results."""

    def test_populated_page_is_results_ok(self):
        self.assertEqual(
            prp.detect_page_state(fixture("live_populated_sold_grid.html"),
                                  query_submitted=True), prp.RESULTS_OK)

    def test_initial_page_is_research_ready(self):
        self.assertEqual(
            prp.detect_page_state(fixture("live_initial_sold_page.html")),
            prp.RESEARCH_READY)

    def test_result_ui_but_no_parsable_rows_is_unsupported_layout(self):
        html = ("<html><head><title>Product Research - eBay Seller Hub</title>"
                "</head><body><div class='research-container'>"
                "<span>Avg sold price</span><span>Avg shipping</span>"
                "<span>Date last sold</span>"
                "<div class='mystery-widget'>rows rendered by canvas</div>"
                "</div></body></html>")
        self.assertEqual(prp.detect_page_state(html, query_submitted=True),
                         prp.UNSUPPORTED_LAYOUT)
        self.assertNotEqual(prp.detect_page_state(html, query_submitted=True),
                            prp.EMPTY_RESULTS)

    def test_true_empty_results_still_reported(self):
        html = fixture("live_initial_sold_page.html").replace(
            "<!-- no query has been run yet -->", "No results found")
        self.assertEqual(prp.detect_page_state(html, query_submitted=True),
                         prp.EMPTY_RESULTS)

    def test_has_result_ui_ignores_script_only_labels(self):
        html = ("<html><body><script>var i18n={a:'Avg sold price'}</script>"
                "</body></html>")
        self.assertFalse(prp.has_result_ui(html))


class TestExactComparisonFiltering(unittest.TestCase):
    """The eight real sold rows, classified by evidence."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates()[HASBULLA_ID]

    def classify(self, title):
        return prp.classify_comp(self.cand, title)[0]

    def test_same_print_run_different_serial_accepted(self):
        for title in (
            "2023 Panini Prizm UFC - Hasbulla Magomedov #200 Red Prizm 57/199 PSA 9",
            "2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 43/199 PSA 9",
            "2023 Panini Red Prizm UFC #200 Hasbulla Magomedov RC Rookie 7/199 PSA 9",
        ):
            self.assertEqual(self.classify(title), prp.ACCEPTED, title)

    def test_different_print_run_rejected(self):
        for title in (
            "Hasbulla Magomedov ~ 2023 Panini Prizm Undercard Red Rookie #200 #/99 ~ PSA 9",
            "2023 Prizm UFC Hasbulla Magomedov Undercard Red #200 PSA 9 69/99",
        ):
            self.assertEqual(self.classify(title), prp.REJECTED, title)

    def test_red_ruby_wave_rejected_as_different_parallel(self):
        title = ("2023 Panini Prizm UFC Rookie RC #200 Red Ruby Wave "
                 "Hasbulla Magomedov PSA 9")
        state, why = prp.classify_comp(self.cand, title)
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("RUBY", why)

    def test_unnumbered_red_is_review_required_not_accepted(self):
        title = ("2023 Panini Prizm UFC Hasbulla Magomedov #200 PSA 9 "
                 "Red Trading Card Rookie RC")
        state, why = prp.classify_comp(self.cand, title)
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("not a conflict", why)

    def test_grade_mismatch_still_rejected(self):
        self.assertEqual(self.classify(
            "2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 43/199 PSA 10"),
            prp.REJECTED)

    def test_year_hoisted_for_titles_not_starting_with_it(self):
        self.assertTrue(prp.normalize_comp_title(
            "Hasbulla Magomedov ~ 2023 Panini Prizm").startswith("2023"))
        self.assertEqual(prp.normalize_comp_title("2023 Panini Prizm"),
                         "2023 Panini Prizm")

    def test_only_accepted_rows_would_be_valued(self):
        rows = [
            ("2023 Panini Prizm UFC - Hasbulla Magomedov #200 Red Prizm 57/199 PSA 9", 75.00),
            ("2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 43/199 PSA 9", 45.00),
            ("2023 Panini Red Prizm UFC #200 Hasbulla Magomedov RC Rookie 7/199 PSA 9", 52.00),
            ("2023 Panini Prizm UFC Rookie RC #200 Red Ruby Wave Hasbulla Magomedov PSA 9", 60.00),
        ]
        accepted = [p for t, p in rows if self.classify(t) == prp.ACCEPTED]
        self.assertEqual(sorted(accepted), [45.00, 52.00, 75.00])
        import statistics
        self.assertEqual(statistics.median(accepted), 52.00)


class TestClassificationAccounting(unittest.TestCase):
    """Every extracted transaction must end in exactly one classification."""

    LIVE_ROWS = [
        ("2023 Panini Prizm UFC - Hasbulla Magomedov #200 Red Prizm 57/199 PSA 9", "$75.00", "$8.60", "Jul 13, 2026"),
        ("Hasbulla Magomedov ~ 2023 Panini Prizm Undercard Red Rookie #200 #/99 ~ PSA 9", "$99.99", "$4.95", "Nov 4, 2023"),
        ("2023 Panini Prizm UFC Hasbulla Magomedov #200 PSA 9 Red Trading Card Rookie RC", "$54.48", "$5.52", "Jan 23, 2026"),
        ("2023 Panini Prizm UFC Rookie RC #200 Red Ruby Wave Hasbulla Magomedov PSA 9", "$60.00", "$5.85", "Jan 20, 2025"),
        ("2023 Prizm UFC Hasbulla Magomedov Undercard Red #200 PSA 9 69/99", "$70.00", "$4.11", "Aug 21, 2024"),
        ("2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 43/199 PSA 9", "$45.00", "$5.99", "Sep 6, 2025"),
        ("2023 Panini Red Prizm UFC #200 Hasbulla Magomedov RC Rookie 7/199 PSA 9", "$52.00", "$5.00", "Aug 28, 2023"),
    ]

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates()[HASBULLA_ID]

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    def records(self, rows):
        """No /itm/ links, exactly as the live page renders them."""
        return [{"listing": t, "avg_sold_price": p, "avg_shipping": s,
                 "date_last_sold": d, "source_item_id": "", "source_url": "",
                 "raw_text": f"{t} {p} {s} {d}"} for t, p, s, d in rows]

    def test_rows_without_item_ids_do_not_collapse(self):
        """The bug that lost 3 of 5 live rows: an empty dedup key."""
        rows = prp.records_to_rows(self.records(self.LIVE_ROWS),
                                   HASBULLA_ID, "q", "STRICT")
        keys = [r["source_item_id"] for r in rows]
        self.assertEqual(len(set(keys)), len(rows))
        self.assertTrue(all(k.startswith("pr-") for k in keys))
        mc.import_rows(self.conn, rows, attribute_by_title=False)
        stored = self.conn.execute(
            "SELECT COUNT(*) FROM sold_comps").fetchone()[0]
        self.assertEqual(stored, len(self.LIVE_ROWS))

    def test_same_listing_across_tiers_is_one_row(self):
        recs = self.records(self.LIVE_ROWS[:2])
        strict = prp.records_to_rows(recs, HASBULLA_ID, "q", "STRICT")
        normal = prp.records_to_rows(recs, HASBULLA_ID, "q", "NORMAL")
        self.assertEqual([r["source_item_id"] for r in strict],
                         [r["source_item_id"] for r in normal])

    def test_invariant_accepted_plus_rejected_plus_review_equals_extracted(self):
        rows = prp.records_to_rows(self.records(self.LIVE_ROWS),
                                   HASBULLA_ID, "q", "STRICT")
        mc.import_rows(self.conn, rows, attribute_by_title=False)
        counts = pw.reclassify_comps(self.conn, HASBULLA_ID, self.cand)
        total = (counts[prp.ACCEPTED] + counts[prp.REJECTED]
                 + counts[prp.REVIEW_REQUIRED])
        self.assertEqual(total, len(rows))

    def test_classification_is_total_over_odd_titles(self):
        """classify_comp must never return anything outside the three states."""
        for title in ("", "   ", "junk", "LOT OF 5 CARDS", "PSA 9",
                      "2023 Panini Prizm UFC #200 PSA 9",
                      "Hasbulla ~~~ ###", "REPRINT 2023 #200 PSA 9"):
            state, _why = prp.classify_comp(self.cand, title)
            self.assertIn(state, (prp.ACCEPTED, prp.REJECTED,
                                  prp.REVIEW_REQUIRED), repr(title))

    def test_expected_live_outcome(self):
        rows = prp.records_to_rows(self.records(self.LIVE_ROWS),
                                   HASBULLA_ID, "q", "STRICT")
        mc.import_rows(self.conn, rows, attribute_by_title=False)
        pw.reclassify_comps(self.conn, HASBULLA_ID, self.cand)
        accepted = sorted(r["sold_price"] for r in self.conn.execute(
            "SELECT sold_price FROM sold_comps WHERE accepted=1"))
        self.assertEqual(accepted, [45.0, 52.0, 75.0])
        import statistics
        self.assertEqual(statistics.median(accepted), 52.0)


class TestMissingYearEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates()[HASBULLA_ID]

    def test_absent_year_is_review_required(self):
        state, why = prp.classify_comp(
            self.cand,
            "Hasbulla Magomedov Panini Prizm UFC Red Prizm #200 57/199 PSA 9")
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("not a conflict", why)

    def test_conflicting_year_is_rejected(self):
        state, why = prp.classify_comp(
            self.cand,
            "2019 Panini Prizm UFC Red Prizm #200 Hasbulla Magomedov 57/199 PSA 9")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("2019", why)

    def test_year_stated_late_in_the_title_is_still_matched(self):
        state, _why = prp.classify_comp(
            self.cand,
            "Hasbulla Magomedov ~ 2023 Panini Prizm UFC Red Prizm #200 57/199 PSA 9")
        self.assertEqual(state, prp.ACCEPTED)


class TestLiveRunRegressions(unittest.TestCase):
    """Regressions from the live rerun: title noise, print-run spellings,
    invariant scope, and the review_required/rejected distinction."""

    CANDIDATE_TITLE = ("2023 PANINI PRIZM UFC RED PRIZM #200 "
                       "HASBULLA MAGOMEDOV 22/199 PSA 9")

    # Exactly as extracted from the live page, a11y prefix and all.
    LIVE = [
        ("2023 Panini Prizm UFC - Hasbulla Magomedov #200 Red Prizm 57/199 PSA 9", 75.00, prp.ACCEPTED),
        (", preview full size image 2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 43/199 PSA 9 2023 PANINI PRIZM UFC RED PRIZM #200 HASBULLA MAGOMEDOV 43/199 PSA 9", 45.00, prp.ACCEPTED),
        (", preview full size image 2023 Panini Red Prizm UFC #200 Hasbulla Magomedov RC Rookie /199 PSA 9 2023 Panini Red Prizm UFC #200 Hasbulla Magomedov RC Rookie /199 PSA 9", 52.00, prp.ACCEPTED),
        (", preview full size image 2023 Panini Prizm UFC Hasbulla Magomedov #200 PSA 9 RED Trading Card Rookie RC", 54.48, prp.REVIEW_REQUIRED),
        # "#112/199" is serial 112 of 199, NOT card number 112. The title never
        # states the card number, so this is absent evidence, not a conflict.
        (", preview full size image 2023 Prizm UFC Hasbulla Magomedov Red Prizm Rookie RC #112/199 PSA 9 2023 Prizm UFC Hasbulla Magomedov Red Prizm Rookie RC #112/199 PSA 9", 62.00, prp.REVIEW_REQUIRED),
        (", preview full size image Hasbulla Magomedov ~ 2023 Panini Prizm Under Card Red Rookie #200 #/99 ~ PSA 9", 99.99, prp.REJECTED),
        (", preview full size image 2023 Prizm UFC Hasbulla Magomedov Undercard Red #200 PSA 9 69/99", 70.00, prp.REJECTED),
        (", preview full size image 2023 Panini Prizm UFC Rookie RC #200 Red Ruby Wave Hasbulla Magomedov PSA 9", 60.00, prp.REJECTED),
        (", preview full size image 2023 Prizm UFC #200 Hasbulla Magomedov Under Card Red Prizm Rookie #37/99 PSA 9 2023 Prizm UFC #200 Hasbulla Magomedov Under Card Red Prizm Rookie #37/99 PSA 9", 100.00, prp.REJECTED),
    ]

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates()[HASBULLA_ID]

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    # --- issue 3: the missing $52 comp ---------------------------------
    def test_the_missing_52_dollar_comp_is_accepted(self):
        title = ("2023 Panini Red Prizm UFC #200 Hasbulla Magomedov "
                 "RC Rookie 7/199 PSA 9")
        self.assertEqual(prp.classify_comp(self.cand, title)[0], prp.ACCEPTED)

    def test_print_run_without_a_copy_number_is_read(self):
        """The live title lost the serial: "RC Rookie /199 PSA 9"."""
        title = ("2023 Panini Red Prizm UFC #200 Hasbulla Magomedov "
                 "RC Rookie /199 PSA 9")
        self.assertEqual(prp.classify_comp(self.cand, title)[0], prp.ACCEPTED)

    def test_accessibility_text_stripped_from_title(self):
        raw = ", preview full size image 2023 Panini Prizm UFC #200 PSA 9"
        self.assertNotIn("preview", prp.clean_listing_title(raw).lower())

    def test_doubled_title_collapsed(self):
        one = "2023 Panini Red Prizm UFC #200 Hasbulla Magomedov 7/199 PSA 9"
        self.assertEqual(prp.clean_listing_title(f"{one} {one}"), one)

    def test_hash_serial_after_card_number_is_a_print_run(self):
        """"#200 ... #37/99" - the second # marks the serial, not a card no."""
        title = ("2023 Prizm UFC #200 Hasbulla Magomedov Under Card Red "
                 "Prizm Rookie #37/99 PSA 9")
        state, why = prp.classify_comp(self.cand, title)
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("99", why)

    def test_hash_serial_stamp_is_not_a_card_number(self):
        """"#112/199" is copy 112 of 199. The card number is simply absent, so
        this is review_required rather than a card-number conflict."""
        title = ("2023 Prizm UFC Hasbulla Magomedov Red Prizm Rookie RC "
                 "#112/199 PSA 9")
        state, why = prp.classify_comp(self.cand, title)
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("card number", why)
        f = parse.parse_title(title)["fields"]
        self.assertEqual((f["serial_num"], f["print_run"]), (112, 199))

    def test_genuinely_different_card_number_still_rejected(self):
        state, why = prp.classify_comp(
            self.cand, "2023 Panini Prizm UFC Red Prizm #999 Hasbulla "
                       "Magomedov 57/199 PSA 9")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("card number", why)

    # --- issue 2: absent evidence never yields a rejection --------------
    def test_absent_print_run_is_never_rejected(self):
        title = (", preview full size image 2023 Panini Prizm UFC Hasbulla "
                 "Magomedov #200 PSA 9 RED Trading Card Rookie RC")
        state, why = prp.classify_comp(self.cand, title)
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("not a conflict", why)

    def test_review_reason_string_never_stored_as_a_rejection(self):
        """The exact inconsistency seen live: a "None != 199" reason counted
        under rejected."""
        rows = [{"candidate_item_id": HASBULLA_ID, "query_tier": "STRICT",
                 "source": "EBAY_PRODUCT_RESEARCH", "source_item_id": "x1",
                 "raw_title": ("2023 Panini Prizm UFC Hasbulla Magomedov #200 "
                               "PSA 9 RED Trading Card Rookie RC"),
                 "sold_price": "54.48", "shipping": "5.52", "currency": "USD",
                 "sale_date": "2026-01-23", "condition": "", "source_reference": ""}]
        mc.import_rows(self.conn, rows, attribute_by_title=False)
        pw.reclassify_comps(self.conn, HASBULLA_ID, self.cand, only_ids={"x1"})
        r = self.conn.execute("SELECT * FROM sold_comps").fetchone()
        self.assertEqual(r["match_confidence"], "REVIEW_REQUIRED")
        n_rejected = self.conn.execute(
            "SELECT COUNT(*) FROM sold_comps WHERE accepted=0 AND "
            "COALESCE(match_confidence,'') != 'REVIEW_REQUIRED'").fetchone()[0]
        self.assertEqual(n_rejected, 0)

    # --- issue 1: invariant scope ---------------------------------------
    def test_invariant_ignores_rows_from_earlier_runs(self):
        rows = [{"candidate_item_id": HASBULLA_ID, "query_tier": "STRICT",
                 "source": "EBAY_PRODUCT_RESEARCH", "source_item_id": f"id{i}",
                 "raw_title": t, "sold_price": str(p), "shipping": "5.00",
                 "currency": "USD", "sale_date": "2026-01-01", "condition": "",
                 "source_reference": ""}
                for i, (t, p, _e) in enumerate(self.LIVE)]
        stale = dict(rows[0], source_item_id="STALE")
        mc.import_rows(self.conn, [stale], attribute_by_title=False)
        mc.import_rows(self.conn, rows, attribute_by_title=False)

        run_ids = {r["source_item_id"] for r in rows}
        counts = pw.reclassify_comps(self.conn, HASBULLA_ID, self.cand,
                                     only_ids=run_ids)
        total = (counts[prp.ACCEPTED] + counts[prp.REJECTED]
                 + counts[prp.REVIEW_REQUIRED])
        self.assertEqual(total, len(rows))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0],
            len(rows) + 1)          # stale row kept, just not counted

    def test_every_live_row_classifies_as_expected(self):
        for title, price, expected in self.LIVE:
            state, why = prp.classify_comp(self.cand, title)
            self.assertEqual(state, expected, f"${price}: {why}")

    def test_accepted_median_is_52(self):
        accepted = [p for t, p, _e in self.LIVE
                    if prp.classify_comp(self.cand, t)[0] == prp.ACCEPTED]
        import statistics
        self.assertEqual(sorted(accepted), [45.00, 52.00, 75.00])
        self.assertEqual(statistics.median(accepted), 52.00)


class TestResetComps(unittest.TestCase):
    """--reset-comps is destructive, so its scope and guards are pinned down."""

    A = "v1|298544784209|0"          # Hasbulla
    B = "v1|117330553310|0"          # 1991 Hoops Jordan

    class Args:
        def __init__(self, **kw):
            self.candidate_id = None
            self.limit = None
            self.reset_comps = False
            self.force = False
            self.resume = False
            self.retry_failed = False
            for k, v in kw.items():
                setattr(self, k, v)

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        for cid, n in ((self.A, 3), (self.B, 2)):
            rows = [{"candidate_item_id": cid, "query_tier": "STRICT",
                     "source": "EBAY_PRODUCT_RESEARCH",
                     "source_item_id": f"{cid}-{i}",
                     "raw_title": "1991 HOOPS #536 MICHAEL JORDAN PSA 8",
                     "sold_price": "10.00", "shipping": "1.00",
                     "currency": "USD", "sale_date": "2026-01-01",
                     "condition": "", "source_reference": ""}
                    for i in range(n)]
            mc.import_rows(self.conn, rows, attribute_by_title=False)

    def count(self, cid):
        return self.conn.execute(
            "SELECT COUNT(*) FROM sold_comps WHERE candidate_item_id=?",
            (cid,)).fetchone()[0]

    def test_clears_only_the_selected_candidate(self):
        deleted = pw.reset_comps(self.conn, self.A)
        self.assertEqual(deleted, 3)
        self.assertEqual(self.count(self.A), 0)

    def test_other_candidates_untouched(self):
        pw.reset_comps(self.conn, self.A)
        self.assertEqual(self.count(self.B), 2)

    def test_without_the_flag_nothing_is_deleted(self):
        result = pw.maybe_reset(self.conn, self.Args(candidate_id=self.A))
        self.assertIsNone(result)
        self.assertEqual(self.count(self.A), 3)
        self.assertEqual(self.count(self.B), 2)

    def test_force_alone_deletes_nothing(self):
        pw.maybe_reset(self.conn, self.Args(candidate_id=self.A, force=True))
        self.assertEqual(self.count(self.A), 3)

    def test_flag_with_candidate_id_deletes(self):
        deleted = pw.maybe_reset(
            self.conn, self.Args(candidate_id=self.A, reset_comps=True))
        self.assertEqual(deleted, 3)
        self.assertEqual(self.count(self.A), 0)
        self.assertEqual(self.count(self.B), 2)

    def test_requires_candidate_id(self):
        with self.assertRaises(SystemExit) as ctx:
            pw.validate_flags(self.Args(reset_comps=True))
        self.assertIn("--candidate-id", str(ctx.exception))

    def test_rejected_with_limit(self):
        with self.assertRaises(SystemExit) as ctx:
            pw.validate_flags(self.Args(reset_comps=True,
                                        candidate_id=self.A, limit=5))
        self.assertIn("--limit", str(ctx.exception))

    def test_validate_is_a_noop_without_the_flag(self):
        pw.validate_flags(self.Args(limit=5))            # must not raise

    def test_deleting_an_empty_candidate_reports_zero(self):
        self.assertEqual(pw.reset_comps(self.conn, "no-such-candidate"), 0)
        self.assertEqual(self.count(self.A), 3)

    def test_deletion_is_atomic(self):
        """A failure inside the transaction must leave every row in place."""
        import sqlite3

        class FailingConn:
            """Delegates the real transaction, but blows up on the DELETE."""
            def __init__(self, real):
                self._real = real

            def __enter__(self):
                return self._real.__enter__()

            def __exit__(self, *exc):
                return self._real.__exit__(*exc)

            def execute(self, sql, *a, **kw):
                if sql.strip().upper().startswith("DELETE"):
                    raise sqlite3.OperationalError("simulated failure")
                return self._real.execute(sql, *a, **kw)

        with self.assertRaises(sqlite3.OperationalError):
            pw.reset_comps(FailingConn(self.conn), self.A)
        self.assertEqual(self.count(self.A), 3)
        self.assertEqual(self.count(self.B), 2)


class TestReportScoping(unittest.TestCase):
    """report() must show ONE run, never the candidate's whole history."""

    CID = "v1|298544784209|0"
    STALE_RUN, CURRENT_RUN = "staleaaaaaaaaaa1", "currentbbbbbbbb2"

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates()[cls.CID]

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        self._seed()

    def _insert(self, run_id, source_id, title, price, accepted, confidence,
                reason):
        self.conn.execute(
            """INSERT INTO sold_comps (candidate_item_id, query_tier, source,
               source_item_id, raw_title, sold_price, shipping, total_price,
               currency, accepted, match_confidence, rejection_reason, run_id,
               imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.CID, "STRICT", "EBAY_PRODUCT_RESEARCH", source_id, title,
             price, 5.0, price + 5.0, "USD", accepted, confidence, reason,
             run_id, "2026-07-31T00:00:00Z"))

    def _seed(self):
        # 10 stale rows from older runs, with distinctive prices and reasons.
        for i in range(10):
            self._insert(self.STALE_RUN, f"stale-{i}",
                         f"STALE HISTORICAL ROW {i} PSA 9", 999.0 + i,
                         1 if i < 4 else 0,
                         "EXACT" if i < 4 else ("REVIEW_REQUIRED" if i < 9 else None),
                         None if i < 4 else "STALE REASON MARKER")
        # 5 current-run rows: 3 accepted, 1 rejected, 1 review.
        for i, (price, acc, conf, reason) in enumerate([
                (75.0, 1, "EXACT", None),
                (45.0, 1, "EXACT", None),
                (52.0, 1, "EXACT", None),
                (60.0, 0, None, "parallel/set differs - comp is ['RUBY','WAVE']"),
                (54.48, 0, "REVIEW_REQUIRED",
                 "print run None != 199 (field absent from title, not a conflict)")]):
            self._insert(self.CURRENT_RUN, f"cur-{i}",
                         f"2023 PANINI PRIZM UFC RED PRIZM #200 CURRENT {i} PSA 9",
                         price, acc, conf, reason)
        self.conn.execute(
            """INSERT INTO pr_runs (candidate_id, status, query_level, query_used,
               rows_extracted, rows_seen, accepted, rejected, review_required,
               date_range, run_id, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.CID, "completed", "STRICT", "q", 5, 5, 3, 1, 1,
             "Last 90 days", self.CURRENT_RUN, "2026-07-31T01:00:00Z"))
        self.conn.commit()

    def render(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pw.report(self.conn, candidate_id=self.CID)
        return buf.getvalue()

    def test_report_does_not_raise(self):
        self.assertIn("PRODUCT RESEARCH COLLECTION REPORT", self.render())

    def test_counts_are_current_run_only(self):
        out = self.render()
        self.assertIn("accepted: 3   rejected: 1   review_required: 1", out)
        self.assertIn("(3+1+1=5)", out)

    def test_unique_comps_considered_reconciles(self):
        out = self.render()
        self.assertIn("unique comps considered : 5", out)
        self.assertIn("rows seen across tiers  : 5", out)
        self.assertNotIn("ACCOUNTING WARNING", out)

    def test_no_stale_reason_or_price_appears(self):
        out = self.render()
        self.assertNotIn("STALE REASON MARKER", out)
        self.assertNotIn("STALE HISTORICAL", out)
        for stale_price in ("999.0", "1,003.00", "1004"):
            self.assertNotIn(stale_price, out)

    def test_stale_rows_remain_stored(self):
        self.render()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM sold_comps").fetchone()[0], 15)

    def test_reason_breakdown_is_split_correctly(self):
        out = self.render()
        self.assertIn("rejected   1x  parallel/set differs", out)
        self.assertIn("review     1x  print run None != 199", out)
        # the review reason must never be printed under "rejected"
        for line in out.splitlines():
            if "rejected" in line and "print run None" in line:
                self.fail(f"review reason printed as rejected: {line}")

    def test_median_uses_current_run_only(self):
        out = self.render()
        self.assertIn("median $52.00", out)      # 45 / 52 / 75
        self.assertNotIn("$999", out)

    def test_review_rows_defined_when_none_accepted(self):
        """The NameError path: a run with only review/rejected rows."""
        self.conn.execute("UPDATE sold_comps SET accepted=0, "
                          "match_confidence='REVIEW_REQUIRED' WHERE run_id=?",
                          (self.CURRENT_RUN,))
        self.conn.commit()
        out = self.render()
        self.assertIn("INSUFFICIENT EVIDENCE", out)
        self.assertIn("valuation       : unavailable (NONE)", out)

    def test_report_handles_a_run_with_no_rows(self):
        self.conn.execute("DELETE FROM sold_comps WHERE run_id=?",
                          (self.CURRENT_RUN,))
        self.conn.commit()
        out = self.render()
        self.assertIn("accepted: 0   rejected: 0   review_required: 0", out)

    def test_split_rows_always_returns_three_lists(self):
        a, r, v = pw.split_rows([])
        self.assertEqual((a, r, v), ([], [], []))


class TestBatchScoping(unittest.TestCase):
    """report() must show only the candidates this invocation processed."""

    OLD_BATCH, NEW_BATCH = "oldbatch00000001", "newbatch00000002"

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cands = list(mc.load_candidates().values())

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        # an older completed candidate from a previous invocation
        self._run(self.cands[0]["item_id"], "oldrun0000000001", self.OLD_BATCH,
                  "OLDER HISTORICAL TITLE PSA 9", 987.65, "OLD STALE REASON")
        # five candidates collected by this invocation
        self.new_ids = [c["item_id"] for c in self.cands[1:6]]
        for i, cid in enumerate(self.new_ids):
            self._run(cid, f"newrun{i:010d}", self.NEW_BATCH,
                      f"2024 CURRENT BATCH ROW {i} PSA 9", 10.0 + i, None)

    def _run(self, cid, run_id, batch_id, title, price, reason):
        self.conn.execute(
            """INSERT INTO sold_comps (candidate_item_id, source, source_item_id,
               raw_title, sold_price, shipping, total_price, currency, accepted,
               match_confidence, rejection_reason, run_id, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, "EBAY_PRODUCT_RESEARCH", f"{run_id}-1", title, price, 1.0,
             price + 1.0, "USD", 1 if reason is None else 0,
             "EXACT" if reason is None else None, reason, run_id, "2026-07-31"))
        self.conn.execute(
            """INSERT INTO pr_runs (candidate_id, status, query_level, query_used,
               rows_extracted, rows_seen, accepted, rejected, review_required,
               date_range, run_id, batch_id, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, "completed", "STRICT", "q", 1, 1, 1 if reason is None else 0,
             0 if reason is None else 1, 0, "Last 90 days", run_id, batch_id,
             "2026-07-31T02:00:00Z"))
        self.conn.commit()

    def render(self, **kw):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pw.report(self.conn, **kw)
        return buf.getvalue()

    def test_batch_report_contains_exactly_the_five_new_candidates(self):
        out = self.render(batch_id=self.NEW_BATCH)
        cands = mc.load_candidates()
        for cid in self.new_ids:
            self.assertIn(cands[cid]["title"][:40], out)
        self.assertEqual(out.count("PSA asking"), 5)

    def test_older_candidate_absent_from_batch_report(self):
        out = self.render(batch_id=self.NEW_BATCH)
        cands = mc.load_candidates()
        self.assertNotIn(cands[self.cands[0]["item_id"]]["title"][:40], out)

    def test_no_older_title_price_or_reason_leaks(self):
        out = self.render(batch_id=self.NEW_BATCH)
        self.assertNotIn("OLDER HISTORICAL", out)
        self.assertNotIn("OLD STALE REASON", out)
        self.assertNotIn("987", out)

    def test_without_batch_id_all_runs_are_shown(self):
        out = self.render()
        self.assertEqual(out.count("PSA asking"), 6)


class TestVocabularyRoles(unittest.TestCase):
    """Team, city, league, sport and accolade words must not reject a comp."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cands = mc.load_candidates()
        cls.griffey = pick(cands, "GRIFFEY", "STAR ROOKIE", "#1 ")
        cls.jordan = pick(cands, "#536")
        cls.johnson = pick(cands, "RANDY JOHNSON")
        cls.hasbulla = cands[HASBULLA_ID]

    def setUp(self):
        for name in ("griffey", "jordan", "johnson"):
            if getattr(self, name) is None:
                self.skipTest(f"{name} candidate not in the pool")

    def accepted(self, cand, title):
        state, why = prp.classify_comp(cand, title)
        self.assertEqual(state, prp.ACCEPTED, f"{title}\n  -> {why}")

    def test_griffey_with_team_and_city_words(self):
        for title in (
            "1989 Upper Deck #1 Ken Griffey Jr Seattle Mariners Rookie RC PSA 7",
            "1989 Upper Deck Star Rookie #1 Ken Griffey Jr Baseball Mariners PSA 7",
            "1989 Upper Deck #1 Ken Griffey Jr PSA 7",
        ):
            self.accepted(self.griffey, title)

    def test_jordan_with_league_and_sport_words(self):
        for title in (
            "1991 Hoops #536 Michael Jordan NBA Basketball Chicago Bulls PSA 8",
            "1991 NBA Hoops #536 Michael Jordan PSA 8",
            "1991 Hoops Basketball #536 Michael Jordan PSA 8",
        ):
            self.accepted(self.jordan, title)

    def test_randy_johnson_with_accolade_words(self):
        for title in (
            "1989 Upper Deck #25 Randy Johnson Mariners HOF Rookie PSA 8",
            "1989 Upper Deck Star Rookie #25 Randy Johnson HOF PSA 8",
        ):
            self.accepted(self.johnson, title)

    def test_true_parallel_conflict_still_rejects(self):
        state, why = prp.classify_comp(
            self.hasbulla,
            "2023 Panini Prizm UFC Rookie RC #200 Red Ruby Wave "
            "Hasbulla Magomedov PSA 9")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("RUBY", why)

    def test_product_words_are_not_parallels(self):
        for word in ("PRIZM", "CHROME", "OPTIC", "HOOPS", "CONTENDERS"):
            self.assertNotIn(word, card_vocab.TRUE_PARALLEL, word)

    def test_true_parallels_kept(self):
        for word in ("SILVER", "RED", "GOLD", "REFRACTOR", "RUBY", "WAVE", "ICE"):
            self.assertIn(word, card_vocab.TRUE_PARALLEL, word)

    def test_star_is_not_treated_as_an_accolade(self):
        """"Star Rookie" is a real subset - STAR must not be ignorable."""
        self.assertFalse(card_vocab.is_ignorable("STAR"))
        self.assertNotIn("MVP", card_vocab.IGNORABLE)

    def test_roles_are_assigned_as_documented(self):
        self.assertEqual(card_vocab.role("SILVER"), "parallel")
        self.assertEqual(card_vocab.role("MARINERS"), "team_city")
        self.assertEqual(card_vocab.role("SEATTLE"), "team_city")
        self.assertEqual(card_vocab.role("NBA"), "league_sport")
        self.assertEqual(card_vocab.role("BASKETBALL"), "league_sport")
        self.assertEqual(card_vocab.role("HOF"), "accolade")
        self.assertEqual(card_vocab.role("TRADING"), "generic")
        self.assertEqual(card_vocab.role("PRIZM"), "set_product")


class TestAutographEquivalence(unittest.TestCase):
    """AUTO / AUTOGRAPH / AUTOGRAPHS / AUTOGRAPHED are one concept."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.milton = next(c for c in mc.load_candidates().values()
                          if "MILTON" in c["title"])

    def test_all_spellings_match(self):
        for word in ("Autograph", "AUTO", "Autographs", "Autographed"):
            title = f"2024 Panini Contenders #106 Joe Milton III {word} PSA 10"
            state, why = prp.classify_comp(self.milton, title)
            self.assertEqual(state, prp.ACCEPTED, f"{word}: {why}")

    def test_unsigned_base_card_does_not_match(self):
        state, why = prp.classify_comp(
            self.milton, "2024 Panini Contenders #106 Joe Milton III PSA 10")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("autograph", why.lower())

    def test_conflicting_colored_autograph_parallel_rejected(self):
        for colour in ("Blue", "Gold", "Red"):
            state, why = prp.classify_comp(
                self.milton,
                f"2024 Panini Contenders #106 Joe Milton III {colour} Autograph PSA 10")
            self.assertEqual(state, prp.REJECTED, colour)
            self.assertIn(colour.upper(), why)

    def test_player_name_alone_is_not_enough(self):
        """Right player, wrong card number - must not be accepted."""
        state, _why = prp.classify_comp(
            self.milton,
            "2024 Panini Contenders #999 Joe Milton III Autograph PSA 10")
        self.assertNotEqual(state, prp.ACCEPTED)

    def test_wrong_grade_still_rejected(self):
        state, why = prp.classify_comp(
            self.milton,
            "2024 Panini Contenders #106 Joe Milton III Autograph PSA 9")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("grade", why.lower())


class TestSubjectEvidence(unittest.TestCase):
    """A title that never names the player cannot prove the card."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.johnson = next(c for c in mc.load_candidates().values()
                           if "RANDY JOHNSON" in c["title"])

    def test_unnamed_title_is_review_required(self):
        state, why = prp.classify_comp(
            self.johnson, "1989 UPPER DECK STAR ROOKIE RC #25 PSA 8 NM-MT")
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("not a conflict", why)

    def test_named_title_is_accepted(self):
        self.assertEqual(prp.classify_comp(
            self.johnson,
            "1989 Upper Deck Randy Johnson Star Rookie #25 PSA 8")[0],
            prp.ACCEPTED)

    def test_surname_only_is_enough_evidence(self):
        self.assertEqual(prp.classify_comp(
            self.johnson, "1989 Upper Deck Johnson #25 Star Rookie PSA 8")[0],
            prp.ACCEPTED)


class TestTransactionDedup(unittest.TestCase):
    """One listing can sell more than once - each sale is its own comp."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.cand = mc.load_candidates()[HASBULLA_ID]

    def rec(self, price, ship, date, item_id="117290548001",
            title="Michael Jordan /Wilkins/Malone 1993 NBA Hoops Scoring Leaders #283 PSA 10"):
        return {"listing": title, "avg_sold_price": price, "avg_shipping": ship,
                "date_last_sold": date, "source_item_id": item_id,
                "source_url": f"https://www.ebay.com/itm/{item_id}",
                "raw_text": f"{title} {price} {ship} {date}"}

    def test_two_sales_of_one_listing_are_two_comps(self):
        """The live collision: item 117290548001 sold at $88.00 and $53.14."""
        rows = prp.records_to_rows(
            [self.rec("$88.00", "$5.99", "Jul 12, 2026"),
             self.rec("$53.14", "$3.25", "10/20/23")],
            "cand", "q", "STRICT")
        self.assertEqual(len({r["source_item_id"] for r in rows}), 2)

    def test_same_sale_across_tiers_is_one_comp(self):
        a = prp.records_to_rows([self.rec("$88.00", "$5.99", "Jul 12, 2026")],
                                "cand", "q", "STRICT")
        b = prp.records_to_rows([self.rec("$88.00", "$5.99", "Jul 12, 2026")],
                                "cand", "q", "NORMAL")
        self.assertEqual(a[0]["source_item_id"], b[0]["source_item_id"])

    def test_key_keeps_the_ebay_item_id_visible(self):
        row = prp.records_to_rows([self.rec("$88.00", "$5.99", "Jul 12, 2026")],
                                  "cand", "q", "STRICT")[0]
        self.assertTrue(row["source_item_id"].startswith("117290548001-"))

    def test_fiftyone_unique_rows_all_classify(self):
        """The exact invariant that failed live, at the same scale."""
        conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        recs = [self.rec(f"${40 + i}.00", "$5.00", f"Jan {1 + i % 28}, 2026",
                         item_id="117290548001")
                for i in range(51)]
        rows = prp.records_to_rows(recs, HASBULLA_ID, "q", "STRICT")
        self.assertEqual(len({r["source_item_id"] for r in rows}), 51)
        mc.import_rows(conn, rows, attribute_by_title=False)
        run_ids = {r["source_item_id"] for r in rows}
        counts = pw.reclassify_comps(conn, HASBULLA_ID, self.cand,
                                     only_ids=run_ids, run_id="r1")
        total = (counts[prp.ACCEPTED] + counts[prp.REJECTED]
                 + counts[prp.REVIEW_REQUIRED])
        self.assertEqual(total, 51)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0], 51)


class TestAskingPriceIntegrity(unittest.TestCase):
    """Asking price comes from ONE authoritative field, never from title text."""

    def test_inventory_price_is_the_api_field(self):
        import json as _json
        conn = db.connect()
        row = conn.execute(
            "SELECT * FROM listings WHERE item_id='v1|306925759220|0'").fetchone()
        if row is None:
            self.skipTest("live inventory row not present")
        raw = _json.loads(row["raw"])
        self.assertEqual(str(raw["price"]["value"]), "69223.00")
        self.assertEqual(row["price"], 69223.0)      # not a parsing artifact

    def test_price_beside_a_card_number_is_not_concatenated(self):
        import ebay_product_research_import as adapter
        amount, _cur = adapter.money("$69.00")
        self.assertEqual(amount, 69.00)
        header = ["Title", "Sold price", "Shipping", "Date sold"]
        rows = [["1990 Hoops #223 Sam Vincent PSA 8", "$69.00", "$5.99",
                 "Jan 5, 2026"]]
        out = prp.rows_from_table(header, rows, "c", "q", "STRICT")
        self.assertEqual(out[0]["sold_price"], "69.0")

    def test_exact_cents_preserved(self):
        import ebay_product_research_import as adapter
        self.assertEqual(adapter.money("$555.55")[0], 555.55)

    def test_year_quantity_and_shipping_do_not_contaminate_price(self):
        header = ["Title", "Sold price", "Shipping", "Total sold", "Date sold"]
        rows = [["2018 Panini Prizm #279 DeAndre Ayton PSA 10", "$5.99",
                 "$5.00", "12", "Jan 5, 2026"]]
        out = prp.rows_from_table(header, rows, "c", "q", "STRICT")
        self.assertEqual(out[0]["sold_price"], "5.99")
        self.assertEqual(out[0]["shipping"], "5.0")

    def test_malformed_price_yields_no_number(self):
        import ebay_product_research_import as adapter
        for bad in ("n/a", "--", "", "see description"):
            self.assertIsNone(adapter.money(bad)[0], bad)
        header = ["Title", "Sold price", "Date sold"]
        out = prp.rows_from_table(
            header, [["1990 Hoops #223 Sam Vincent PSA 8", "n/a", "Jan 5, 2026"]],
            "c", "q", "STRICT")
        self.assertEqual(out[0]["sold_price"], "")   # missing, never invented


class TestMarketGap(unittest.TestCase):
    def test_discount_to_market(self):
        gap, pct = pw.market_gap(40, 50)
        self.assertEqual(gap, 10)
        self.assertAlmostEqual(pct, 20.0)
        self.assertEqual(pw.gap_label(gap), "discount to market")

    def test_premium_over_market(self):
        gap, pct = pw.market_gap(60, 50)
        self.assertEqual(gap, -10)
        self.assertAlmostEqual(pct, -20.0)
        self.assertEqual(pw.gap_label(gap), "premium over market")

    def test_at_market(self):
        gap, pct = pw.market_gap(50, 50)
        self.assertEqual((gap, pct), (0, 0.0))
        self.assertEqual(pw.gap_label(gap), "at market")

    def test_zero_market_does_not_divide_by_zero(self):
        self.assertEqual(pw.market_gap(50, 0), (-50, 0.0))


class TestYearNotPrintRun(unittest.TestCase):
    """A four-digit year is only a print run in explicit serial context."""

    def pr(self, title):
        return parse.parse_title(title)["fields"]["print_run"]

    def test_plain_year_is_not_a_print_run(self):
        self.assertIsNone(self.pr("1991 Hoops #30 Michael Jordan PSA 8"))
        self.assertIsNone(self.pr("2023 Panini Prizm #200 Player PSA 9"))

    def test_explicit_serial_still_parses(self):
        self.assertEqual(self.pr("1991 Hoops #30 Michael Jordan 23/199 PSA 8"), 199)

    def test_explicit_slash_year_may_be_a_print_run(self):
        self.assertEqual(self.pr("2023 Topps Chrome #5 Player 23/1991 PSA 9"), 1991)
        self.assertEqual(self.pr("2023 Topps Chrome #5 Player #/1991 PSA 9"), 1991)

    def test_grade_over_year_is_not_a_serial(self):
        """The live case: "#30 PSA 8 / 1991 Fleer #29 PSA 8"."""
        title = ("1991 Hoops Michael Jordan #30 PSA 8 / "
                 "1991 Fleer Michael Jordan #29 PSA 8")
        self.assertIsNone(self.pr(title))

    def test_two_graded_cards_in_one_title_is_a_lot(self):
        enrich.load_surnames(db.connect())
        cand = mc.load_candidates().get("v1|297673833355|0")
        if cand is None:
            self.skipTest("Jordan #30 candidate not present")
        state, why = prp.classify_comp(
            cand, "1991 Hoops Michael Jordan #30 PSA 8 / "
                  "1991 Fleer Michael Jordan #29 PSA 8")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("lot", why)


class TestFailureArtifact(unittest.TestCase):
    def test_artifact_identifies_the_missing_row(self):
        import json as _json
        rows = [{"source_item_id": f"id{i}", "raw_title": f"row {i}",
                 "sold_price": 1.0} for i in range(5)]
        rows.append(dict(rows[0]))                     # a collision
        classified = ["id0", "id1", "id2", "id3"]      # id4 never classified
        path = pw.save_unclassified("v1|x|0", rows, {"accepted": 4}, len(rows),
                                    classified_ids=classified,
                                    run_id="run1", batch_id="batch1")
        data = _json.load(open(path))
        self.assertEqual(data["run_id"], "run1")
        self.assertEqual(data["batch_id"], "batch1")
        self.assertIn("id4", data["missing_ids"])
        self.assertIn("id0", data["collision_groups"])
        self.assertEqual(len(data["collision_groups"]["id0"]), 2)
        self.assertEqual(data["unexpected_classified_ids"], [])
        os.remove(path)


class TestPricedVsAcceptedComps(unittest.TestCase):
    """An accepted identity comp is not automatically a priced valuation comp."""

    CID = "v1|306564595993|0"          # Siakam
    RUN = "pricedrun0000001"

    def setUp(self):
        self.conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    def _row(self, sid, price, ship=5.0):
        total = None if price is None else price + (ship or 0)
        self.conn.execute(
            """INSERT INTO sold_comps (candidate_item_id, source, source_item_id,
               raw_title, sold_price, shipping, total_price, currency, accepted,
               match_confidence, run_id, imported_at)
               VALUES (?,?,?,?,?,?,?,?,1,'EXACT',?,?)""",
            (self.CID, "EBAY_PRODUCT_RESEARCH", sid, "title", price, ship,
             total, "USD", self.RUN, "2026-07-31"))
        self.conn.commit()

    def rows(self):
        return self.conn.execute("SELECT * FROM sold_comps").fetchall()

    def test_three_accepted_three_priced_is_medium(self):
        for i, p in enumerate((10.0, 20.0, 30.0)):
            self._row(f"p{i}", p)
        rows = self.rows()
        self.assertEqual(sum(1 for r in rows if pw.priced(r)), 3)
        self.assertEqual(pw.valuation_confidence(3), "MEDIUM")

    def test_three_accepted_one_priced_is_low(self):
        self._row("a", 10.0)
        self._row("b", None)
        self._row("c", None)
        rows = self.rows()
        n = sum(1 for r in rows if pw.priced(r))
        self.assertEqual(n, 1)
        self.assertEqual(pw.valuation_confidence(n), "LOW")

    def test_three_accepted_zero_priced_is_none(self):
        for i in range(3):
            self._row(f"u{i}", None)
        rows = self.rows()
        n = sum(1 for r in rows if pw.priced(r))
        self.assertEqual(n, 0)
        self.assertEqual(pw.valuation_confidence(n), "NONE")

    def test_missing_price_is_never_zero(self):
        self._row("x", None)
        r = self.rows()[0]
        self.assertIsNone(r["sold_price"])
        self.assertIsNone(r["total_price"])
        self.assertFalse(pw.priced(r))

    def test_report_shows_no_market_estimate_without_priced_comps(self):
        import contextlib, io
        for i in range(3):
            self._row(f"u{i}", None)
        self.conn.execute(
            """INSERT INTO pr_runs (candidate_id, status, query_level, query_used,
               rows_extracted, rows_seen, accepted, rejected, review_required,
               date_range, run_id, batch_id, updated_at)
               VALUES (?,'completed','STRICT','q',3,3,3,0,0,'Last 90 days',?,?,?)""",
            (self.CID, self.RUN, "b1", "2026-07-31T03:00:00Z"))
        self.conn.commit()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pw.report(self.conn, batch_id="b1")
        out = buf.getvalue()
        self.assertIn("no accepted comp has a usable price", out)
        self.assertIn("valuation       : unavailable (NONE)", out)
        self.assertNotIn("market gap", out)
        self.assertNotIn("market total median", out)


class TestShippingContext(unittest.TestCase):
    """The row states which amount is shipping; column order must not guess."""

    LIVE = (", preview full size image 2016 PANINI PRIZM RC SIGNATURES HYPER "
            "PRIZM #23 PASCAL SIAKAM #/10 PSA 10 AUTO Edit Sell Similar "
            "$8,987.80 +$32.00 shipping - 2 - Mar 7, 2026")

    def rec(self, text, title="2016 PANINI PRIZM #23 PASCAL SIAKAM PSA 10"):
        html = (f"<html><body><table><tr><th>Listing</th><th>Avg shipping</th>"
                f"<th>Date last sold</th></tr>"
                f"<tr><td>{title}</td><td>{text}</td><td>Mar 7, 2026</td></tr>"
                f"</table></body></html>")
        return prp.extract_result_rows(html)

    def test_sold_price_is_not_put_in_shipping(self):
        """The live bug: $8,987.80 was stored as shipping, price was NULL."""
        import re as _re
        m = prp.SHIPPING_CTX_RE.search(self.LIVE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "$32.00")
        before = prp.MONEY_RE.findall(self.LIVE[:m.start(1)])
        self.assertEqual(before[0], "$8,987.80")

    def test_free_shipping_may_become_zero(self):
        text = "2020 Topps #80 Luca Ghiotto PSA 8 $195.00 Free shipping Jan 5, 2026"
        self.assertTrue(prp.FREE_SHIPPING_RE.search(text))

    def test_absent_shipping_stays_absent(self):
        """No shipping stated and no "free shipping" - must not become 0."""
        header = ["Title", "Sold price", "Date sold"]
        rows = [["2020 Topps #80 Luca Ghiotto PSA 8", "$195.00", "Jan 5, 2026"]]
        out = prp.rows_from_table(header, rows, "c", "q", "STRICT")
        self.assertEqual(out[0]["shipping"], "")
        self.assertEqual(out[0]["sold_price"], "195.0")


class TestGradeProvenance(unittest.TestCase):
    """A grade must come from an explicit grade token, never from "#/10"."""

    def grade(self, title):
        f = parse.parse_title(title)["fields"]
        return f["grade_value"], f["print_run"]

    def test_title_ending_psa_with_no_number_has_no_grade(self):
        g, run = self.grade(
            "2016 PANINI PRIZM RC SIGNATURES HYPER PRIZM #23 PASCAL SIAKAM #/10 PSA")
        self.assertIsNone(g)
        self.assertEqual(run, 10)

    def test_explicit_grade_after_print_run(self):
        g, run = self.grade(
            "2016 PANINI PRIZM RC SIGNATURES HYPER PRIZM #23 PASCAL SIAKAM "
            "#/10 PSA 10 AUTO")
        self.assertEqual(g, "10")
        self.assertEqual(run, 10)

    def test_slash_ten_alone_never_supplies_a_grade(self):
        g, run = self.grade("2016 Panini Prizm #23 Pascal Siakam #/10")
        self.assertIsNone(g)
        self.assertEqual(run, 10)

    def test_autograph_provenance_is_explicit(self):
        signed = parse.parse_title(
            "2016 PANINI PRIZM RC SIGNATURES HYPER PRIZM #23 PASCAL SIAKAM "
            "#/10 PSA 10 AUTO")["fields"]
        self.assertEqual(signed["is_auto"], 1)
        # "SIGNATURES" in a set name alone must not assert a signed card.
        setonly = parse.parse_title(
            "2016 PANINI PRIZM ROOKIE SIGNATURES #23 PASCAL SIAKAM PSA 10")["fields"]
        self.assertEqual(setonly["is_auto"], 0)


class TestSerialStampVsCardNumber(unittest.TestCase):
    """"#429/500" is copy 429 of 500, not card number 429/500."""

    def fields(self, title):
        return parse.parse_title(title)["fields"]

    def test_card_number_then_bare_serial(self):
        f = self.fields("2022 Panini Prizm Monopoly #63 Chet Holmgren 136/500 PSA 10")
        self.assertEqual(f["card_number"], "63")
        self.assertEqual(f["serial_num"], 136)
        self.assertEqual(f["print_run"], 500)

    def test_drake_maye_card_number_then_serial(self):
        f = self.fields("2023 Bowman University Chrome #200 Drake Maye 396/499 PSA 10")
        self.assertEqual(f["card_number"], "200")
        self.assertEqual(f["serial_num"], 396)
        self.assertEqual(f["print_run"], 499)

    def test_plain_card_number_has_no_serial(self):
        f = self.fields("2021 Topps Heritage #245 Shohei Ohtani PSA 10")
        self.assertEqual(f["card_number"], "245")
        self.assertIsNone(f["serial_num"])
        self.assertIsNone(f["print_run"])

    def test_alphanumeric_card_number(self):
        self.assertEqual(
            self.fields("2021 Panini Prizm #V334 Justin Fields PSA 10")["card_number"],
            "V334")

    def test_serial_never_overwrites_an_identified_card_number(self):
        f = self.fields("2022 Panini Prizm Monopoly #63 Chet Holmgren 425/500 PSA 10")
        self.assertEqual(f["card_number"], "63")
        self.assertNotEqual(f["card_number"], "425/500")

    def test_hash_serial_stamp_leaves_card_number_unknown(self):
        """The live rows: "#429/500" with no card number stated anywhere."""
        for title, serial, run in (
            ("2022-23 Monopoly Prizm Chet Holmgren Gold Money Shimmer Prizm "
             "RC #429/500 PSA 10", 429, 500),
            ("2022-23 Prizm Monopoly Chet Holmgren RC Gold Money Shimmer "
             "#136/500 PSA 10", 136, 500),
            ("2023 Bowman Chrome U Drake Maye 1st Bowman Autograph Refractor "
             "#396/499 PSA 10", 396, 499),
        ):
            f = self.fields(title)
            self.assertIsNone(f["card_number"], title)
            self.assertEqual(f["serial_num"], serial)
            self.assertEqual(f["print_run"], run)

    def test_slash_card_numbers_are_left_alone(self):
        """Genuine slash-bearing card numbers must not become serials."""
        for title, expect in (
            ("1988 PANINI SUPERSPORT #69/30 CARL LEWIS PSA 6", "69/30"),
            ("2018 POKEMON HOLO #073/150 HYDREIGON PSA 10", "073/150"),
            ("1993 COLLECTOR'S EDGE #F/X12 TROY AIKMAN PSA 8", "F/X12"),
        ):
            f = self.fields(title)
            self.assertEqual(f["card_number"], expect, title)
            self.assertIsNone(f["print_run"], title)

    def test_unknown_card_number_is_review_not_reject(self):
        enrich.load_surnames(db.connect())
        chet = next((c for c in mc.load_candidates().values()
                     if "CHET HOLMGREN" in c["title"]), None)
        if chet is None:
            self.skipTest("Chet candidate not present")
        state, why = prp.classify_comp(
            chet, "2022-23 Prizm Monopoly Chet Holmgren RC Gold Money Shimmer "
                  "#136/500 PSA 10")
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("card number", why)

    def test_same_print_run_different_serial_still_matches(self):
        enrich.load_surnames(db.connect())
        drake = pick(mc.load_candidates(), "DRAKE MAYE", "#200")
        if drake is None:
            self.skipTest("Drake candidate not present")
        for serial in (56, 343, 396):
            state, why = prp.classify_comp(
                drake, f"2023 BOWMAN UNIVERSITY CHROME AUTO-REFRACTOR #200 "
                       f"DRAKE MAYE {serial}/499 PSA 10")
            self.assertEqual(state, prp.ACCEPTED, f"{serial}: {why}")


class TestMonopolyParallelFamilies(unittest.TestCase):
    """Gold Money Shimmer is not Gold Shimmer and not Gold Money Blast."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.chet = next((c for c in mc.load_candidates().values()
                         if "CHET HOLMGREN" in c["title"]), None)

    def setUp(self):
        if self.chet is None:
            self.skipTest("Chet candidate not present")

    def test_exact_family_accepted(self):
        self.assertEqual(prp.classify_comp(
            self.chet, "2022-23 Prizm Monopoly Chet Holmgren RC #63 Gold Money "
                       "Shimmer /500 PSA 10")[0], prp.ACCEPTED)

    def test_missing_money_is_review_required(self):
        state, why = prp.classify_comp(
            self.chet, "2022-23 Prizm Monopoly Chet Holmgren #63 RC Gold "
                       "Shimmer /500 PSA 10")
        self.assertEqual(state, prp.REVIEW_REQUIRED)
        self.assertIn("MONEY", why)

    def test_money_blast_is_rejected(self):
        state, why = prp.classify_comp(
            self.chet, "2022-23 Prizm Monopoly Chet Holmgren #63 Gold Money "
                       "Blast /500 PSA 10")
        self.assertEqual(state, prp.REJECTED)
        self.assertIn("BLAST", why)


class TestLeadingZeroSerialContext(unittest.TestCase):
    """A leading zero is evidence, not a veto, on serial parsing."""

    def fields(self, title):
        return parse.parse_title(title)["fields"]

    def test_zero_padded_bare_serial_with_card_number(self):
        f = self.fields("2022 Panini Prizm Monopoly #63 Chet Holmgren 073/150 PSA 10")
        self.assertEqual(f["card_number"], "63")
        self.assertEqual(f["serial_num"], 73)
        self.assertEqual(f["print_run"], 150)

    def test_zero_padded_serial_001(self):
        f = self.fields("2023 Bowman University Chrome #200 Drake Maye 001/499 PSA 10")
        self.assertEqual(f["card_number"], "200")
        self.assertEqual(f["serial_num"], 1)
        self.assertEqual(f["print_run"], 499)

    def test_zero_padded_stamp_with_another_card_number_is_a_serial(self):
        f = self.fields("2022 Panini Prizm Monopoly #63 Chet Holmgren #073/150 PSA 10")
        self.assertEqual(f["card_number"], "63")
        self.assertEqual((f["serial_num"], f["print_run"]), (73, 150))

    def test_zero_padded_stamp_alone_stays_a_card_number(self):
        """No other card-number evidence - must not be forced into a serial."""
        f = self.fields("2018 Pokemon Holo #073/150 Hydreigon PSA 10")
        self.assertEqual(f["card_number"], "073/150")
        self.assertIsNone(f["serial_num"])
        self.assertIsNone(f["print_run"])

    def test_numerator_above_denominator_stays_a_card_number(self):
        f = self.fields("1988 Panini Supersport #69/30 Carl Lewis PSA 10")
        self.assertEqual(f["card_number"], "69/30")
        self.assertIsNone(f["print_run"])

    def test_alphanumeric_slash_card_number(self):
        self.assertEqual(
            self.fields("1993 Collector's Edge #F/X12 Troy Aikman PSA 8")["card_number"],
            "F/X12")

    def test_candidate_print_run_disambiguates_a_padded_stamp(self):
        """Zero-padded, no other card number, but /150 matches the candidate."""
        raw = "2018 Some Set Holo #073/150 Player Name PSA 10"
        self.assertIsNone(parse.parse_title(raw)["fields"]["print_run"])
        repaired = prp.repair_print_run(raw, candidate_print_run=150)
        f = parse.parse_title(repaired)["fields"]
        self.assertEqual((f["serial_num"], f["print_run"]), (73, 150))

    def test_candidate_print_run_mismatch_leaves_it_alone(self):
        raw = "2018 Some Set Holo #073/150 Player Name PSA 10"
        repaired = prp.repair_print_run(raw, candidate_print_run=499)
        self.assertEqual(parse.parse_title(repaired)["fields"]["card_number"],
                         "073/150")


class TestOhtaniParallelRule(unittest.TestCase):
    """Omitted CHROME is tolerated; an omitted PARALLEL term is not."""

    @classmethod
    def setUpClass(cls):
        enrich.load_surnames(db.connect())
        cls.oh = pick(mc.load_candidates(), "OHTANI", "HERITAGE", "SPARKLE")

    def setUp(self):
        if self.oh is None:
            self.skipTest("Ohtani candidate not present")

    def state(self, title):
        return prp.classify_comp(self.oh, title)[0]

    def test_full_parallel_accepted(self):
        self.assertEqual(self.state(
            "2021 Topps Heritage Chrome #245 Shohei Ohtani Blue Sparkle "
            "Refractor PSA 10"), prp.ACCEPTED)

    def test_omitted_chrome_is_tolerated(self):
        """CHROME is a product word; REFRACTOR already implies a Chrome product."""
        self.assertEqual(self.state(
            "2021 Topps Heritage #245 Shohei Ohtani Blue Sparkle Refractor "
            "PSA 10"), prp.ACCEPTED)

    def test_missing_sparkle_is_not_accepted(self):
        self.assertEqual(self.state(
            "2021 Topps Heritage Chrome Blue Refractor #245 Shohei Ohtani "
            "PSA 10"), prp.REVIEW_REQUIRED)

    def test_missing_blue_is_not_accepted(self):
        self.assertEqual(self.state(
            "2021 Topps Heritage Chrome Refractor #245 Shohei Ohtani PSA 10"),
            prp.REVIEW_REQUIRED)

    def test_missing_refractor_is_not_accepted(self):
        self.assertEqual(self.state(
            "2021 Topps Heritage #245 Shohei Ohtani Blue Sparkle PSA 10"),
            prp.REVIEW_REQUIRED)

    def test_ordinary_chrome_is_not_accepted(self):
        self.assertEqual(self.state(
            "2021 Topps Heritage Chrome #245 Shohei Ohtani PSA 10"),
            prp.REVIEW_REQUIRED)

    def test_raw_and_wrong_grade_rejected(self):
        self.assertEqual(self.state(
            "2021 Topps Heritage Chrome #245 Shohei Ohtani Blue Sparkle Refractor"),
            prp.REJECTED)
        self.assertEqual(self.state(
            "2021 Topps Heritage Chrome #245 Shohei Ohtani Blue Sparkle "
            "Refractor PSA 9"), prp.REJECTED)

    def test_sparkle_is_a_true_parallel_token(self):
        self.assertIn("SPARKLE", card_vocab.TRUE_PARALLEL)
        self.assertNotIn("CHROME", card_vocab.TRUE_PARALLEL)


class TestRawArtifactIsNotHollow(unittest.TestCase):
    """The raw artifact must record the rows actually collected.

    The writer used to persist `table_rows` from the legacy <table> parser.
    The live page is div-based, so that list is always empty and every
    artifact was written with "rows": [] while the DB held the real data.
    """

    def test_div_layout_yields_rows_the_table_parser_cannot_see(self):
        for name in ("live_populated_sold_divs.html",
                     "live_populated_sold_grid.html"):
            html = fixture(name)
            records = prp.extract_result_rows(html)
            _header, table_rows = prp.parse_html_table(html)
            self.assertTrue(records, f"{name}: div extractor found nothing")
            self.assertFalse(table_rows,
                             f"{name} is table-based; cannot prove the bug")
            rows = prp.records_to_rows(records, "v1|1|0", "q", "STRICT")
            self.assertTrue(rows, name)
            # What the writer now persists is `rows`, never the empty list.
            self.assertGreater(len(rows), len(table_rows), name)

    def test_written_artifact_carries_the_rows(self):
        """Shape check on the payload the collector writes."""
        html = fixture("live_populated_sold_divs.html")
        records = prp.extract_result_rows(html)
        rows = prp.records_to_rows(records, "v1|1|0", "q", "STRICT")
        payload = {"candidate_id": "v1|1|0", "query": "q", "level": "STRICT",
                   "source": "records", "rows": rows, "legacy_table_rows": []}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            with open(p, "w") as fh:
                json.dump(payload, fh)
            back = json.load(open(p))
        self.assertEqual(len(back["rows"]), len(rows))
        self.assertTrue(back["rows"], "artifact must not be hollow")
