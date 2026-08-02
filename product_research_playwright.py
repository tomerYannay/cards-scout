"""Deterministic Product Research collector for valuation candidates.

This is a data-acquisition layer only. It drives eBay Seller Hub Product
Research with Playwright, reads the sold-transaction table, and hands the rows
to the existing identity matcher. It never decides what matches, never values
anything, and never touches the main inventory crawler.

Safeguards, by design:
  - you log in manually, once; the session lives in a local browser profile
  - no password, cookie or token is ever read, printed or stored by this code
  - one candidate at a time, small randomized delay, no parallel workers
  - any sign-in or verification prompt stops the run with a clear message
  - nothing attempts to bypass CAPTCHA, MFA or rate limits

  python product_research_playwright.py --login
  python product_research_playwright.py --candidate-id "v1|...|0" --headed
  python product_research_playwright.py --limit 5 --headed
  python product_research_playwright.py --resume
  python product_research_playwright.py --report
"""

import argparse
import collections
import datetime as dt
import json
import os
import random
import re
import uuid
import sys
import time

import db
import decision as dec
import manual_comps as mc
import parse
import product_research_parse as prp

PROFILE_DIR = os.path.join("data", "playwright", "ebay-profile")
ARTIFACT_DIR = os.path.join("data", "playwright", "artifacts")
RAW_DIR = os.path.join("data", "playwright", "raw")
RESEARCH_URL = "https://www.ebay.com/sh/research"
DEFAULT_CDP = "http://localhost:9222"

# Statuses stored in pr_runs.
PENDING, RUNNING, COMPLETED = "pending", "running", "completed"
NO_RESULTS, INSUFFICIENT = "no_results", "insufficient_comps"
FAILED, AUTH_REQUIRED, REVIEW_REQUIRED = "failed", "auth_required", "review_required"

TERMINAL_OK = (COMPLETED, NO_RESULTS, INSUFFICIENT)
MIN_EXACT_COMPS = 3          # below this the evidence is called insufficient


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_dirs():
    for d in (PROFILE_DIR, ARTIFACT_DIR, RAW_DIR):
        os.makedirs(d, exist_ok=True)


# CDP exposes Chrome's own internals as targets - omnibox popups, devtools,
# extension pages. They are never the tab the user is looking at.
INTERNAL_PREFIXES = ("chrome://", "devtools://", "chrome-extension://",
                     "chrome-untrusted://", "edge://", "about:", "blob:")


def is_internal_url(url):
    u = (url or "").strip().lower()
    return not u or u.startswith(INTERNAL_PREFIXES)


def page_url(page):
    try:
        return page.url or ""
    except Exception:
        return ""


def rank_page(url):
    """0 = Product Research, 1 = any eBay page, 2 = any normal web page."""
    u = (url or "").lower()
    if "ebay.com/sh/research" in u:
        return 0
    if "ebay.com" in u:
        return 1
    if u.startswith(("http://", "https://")):
        return 2
    return 3


def usable_pages(pages):
    """Open, non-internal tabs only."""
    out = []
    for p in pages:
        try:
            if p.is_closed():
                continue
        except Exception:
            continue
        if is_internal_url(page_url(p)):
            continue
        out.append(p)
    return out


def pick_page(pages):
    """Best real tab to drive: Product Research > eBay > any normal page.

    Internal Chrome targets are discarded outright, so an omnibox popup can
    never win over a real page - nor be selected when it is the only target.
    """
    candidates = usable_pages(pages)
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: rank_page(page_url(p)))[0]


def open_browser(pw, args, allow_new_page=True):
    """Return (page, close_fn).

    Two modes:
      attach  - connect to a Chrome you launched and logged into yourself.
                Nothing is automated about the login; we only drive an existing
                tab. Your browser is left open when we disconnect.
      profile - Playwright's own persistent profile (--login flow).
    """
    if args.connect_existing:
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_url)
        except Exception as exc:
            sys.exit(
                f"error: could not attach to Chrome at {args.cdp_url}\n"
                f"       {exc}\n\n"
                "Start Chrome with remote debugging first - see\n"
                "PLAYWRIGHT_PRODUCT_RESEARCH.md > 'Attach to your own Chrome'.")
        contexts = list(browser.contexts) or [browser.new_context()]
        all_pages = [p for ctx in contexts for p in ctx.pages]
        print(f"  attached to Chrome at {args.cdp_url}")
        print(f"  targets seen ({len(all_pages)}):")
        for p in all_pages:
            url = page_url(p)
            mark = "skip (internal)" if is_internal_url(url) else \
                {0: "PRODUCT RESEARCH", 1: "ebay", 2: "web"}.get(
                    rank_page(url), "other")
            print(f"    [{mark:16}] {url[:90] or '(blank)'}")
        page = pick_page(all_pages)
        if page is None:
            if not allow_new_page:
                browser.close()
                sys.exit("error: no usable tab found - open eBay Seller Hub > "
                         "Research > Product Research in that Chrome window "
                         "and re-run (no tab was created)")
            page = contexts[0].new_page()
            print("    no usable tab; opened a new one")
        print(f"  selected tab: {page_url(page)[:90] or '(blank)'}")
        return page, lambda: browser.close()      # disconnects, does not quit Chrome

    if not os.path.isdir(PROFILE_DIR) or not os.listdir(PROFILE_DIR):
        sys.exit(f"error: no saved session in {PROFILE_DIR} - run --login first, "
                 "or use --connect-existing to attach to your own Chrome")
    ctx = pw.chromium.launch_persistent_context(
        PROFILE_DIR, headless=not args.headed,
        viewport={"width": 1500, "height": 950})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return page, lambda: ctx.close()


def load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("error: playwright is not installed - pip install playwright "
                 "&& playwright install chromium")
    return sync_playwright


# --------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------

def get_status(conn, cid):
    r = conn.execute("SELECT * FROM pr_runs WHERE candidate_id=?", (cid,)).fetchone()
    return dict(r) if r else None


def set_status(conn, cid, status, **fields):
    existing = get_status(conn, cid)
    data = {"candidate_id": cid, "status": status, "updated_at": now()}
    if existing:
        data = {**existing, **data}
    data.update({k: v for k, v in fields.items() if v is not None})
    cols = ["candidate_id", "status", "query_level", "query_used", "attempts",
            "rows_extracted", "rows_seen", "accepted", "rejected",
            "review_required", "date_range", "run_id", "batch_id",
            "last_error", "updated_at"]
    conn.execute(
        f"INSERT OR REPLACE INTO pr_runs ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [data.get(c) for c in cols])
    conn.commit()


# --------------------------------------------------------------------------
# Page interaction
# --------------------------------------------------------------------------

def save_artifacts(page, cid, tag):
    """Screenshot + HTML on failure. HTML is what we need to fix a selector."""
    ensure_dirs()
    stamp = re.sub(r"[^0-9A-Za-z]", "", cid)[-12:] or "cand"
    base = os.path.join(ARTIFACT_DIR, f"{stamp}_{tag}")
    try:
        page.screenshot(path=base + ".png", full_page=True)
    except Exception:
        pass
    try:
        with open(base + ".html", "w", encoding="utf-8") as fh:
            fh.write(safe_content(page, 5000, attempts=2))
    except Exception:
        pass
    return base


# eBay re-navigates Seller Hub after first paint, so page.content() can land
# mid-navigation. These are the Playwright messages that mean "try again".
NAV_ERROR_MARKERS = (
    "navigating and changing the content",
    "unable to retrieve content",
    "execution context was destroyed",
    "navigation",
)


def is_navigation_error(exc):
    msg = str(exc).lower()
    return any(m in msg for m in NAV_ERROR_MARKERS)


def wait_settled(page, timeout_ms):
    """Wait for the document, never for networkidle - eBay keeps background
    requests alive indefinitely, so networkidle would just time out."""
    for state in ("domcontentloaded", "load"):
        try:
            page.wait_for_load_state(state, timeout=timeout_ms)
        except Exception:
            pass          # a slow 'load' is not fatal; the DOM is what we read


def safe_content(page, timeout_ms=8000, attempts=5, base_delay=0.4):
    """page.content() with a bounded retry while the page is still navigating.

    Any error that is NOT a navigation race is re-raised immediately.
    """
    delay, last = base_delay, None
    for _ in range(attempts):
        try:
            wait_settled(page, timeout_ms)
            return page.content()
        except Exception as exc:
            if not is_navigation_error(exc):
                raise
            last = exc
            time.sleep(delay)
            delay *= 1.6
    raise last


def open_research(page, timeout_ms, navigate=True):
    """Read the Product Research page state.

    navigate=False reuses whatever tab is already open, so --check-connection
    never yanks you away from a page you had set up by hand.
    """
    already_there = "/sh/research" in (page.url or "")
    if navigate and not already_there:
        page.goto(RESEARCH_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    wait_settled(page, timeout_ms)
    page.wait_for_timeout(1500)
    return prp.detect_page_state(safe_content(page, timeout_ms))


def run_search(page, query, timeout_ms):
    """Type a query and submit. Prefers role/label selectors over CSS classes."""
    box = None
    for locator in (
        lambda: page.get_by_placeholder(
            "Enter keywords, MPN, UPC, EPID, EAN or ISBN", exact=False),
        lambda: page.get_by_role("searchbox"),
        lambda: page.get_by_role("combobox"),
        lambda: page.get_by_placeholder("Search", exact=False),
        lambda: page.get_by_label("Search", exact=False),
        lambda: page.locator("input[type='search']"),
        lambda: page.locator("input[name*='keyword' i]"),
    ):
        try:
            cand = locator().first
            if cand.count() and cand.is_visible():
                box = cand
                break
        except Exception:
            continue
    if box is None:
        raise RuntimeError("could not locate the Product Research search box")

    box.click()
    box.fill("")
    box.type(query, delay=35)
    box.press("Enter")
    # Deliberately not networkidle: Seller Hub polls in the background forever.
    wait_settled(page, timeout_ms)
    page.wait_for_timeout(2000)
    return safe_content(page, timeout_ms)


def read_date_range(html):
    """Whatever range the account has selected, recorded with every run."""
    text = re.sub(r"\s+", " ", html or "")
    for pattern in (r"Last\s+\d+\s+(?:days|months|year|years)",
                    r"\d{1,2}/\d{1,2}/\d{2,4}\s*[-–]\s*\d{1,2}/\d{1,2}/\d{2,4}"):
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(0)
    return "unknown"


# --------------------------------------------------------------------------
# Per-candidate collection
# --------------------------------------------------------------------------

def validate_flags(args):
    """Guard the destructive flag before anything is touched."""
    if not getattr(args, "reset_comps", False):
        return
    if not args.candidate_id:
        sys.exit("error: --reset-comps deletes data and must name exactly one "
                 "candidate.\n       Add --candidate-id \"<item id>\".")
    if args.limit:
        sys.exit("error: --reset-comps cannot be combined with --limit - it "
                 "clears a single candidate only.")


def reset_comps(conn, cid):
    """Delete this candidate's sold_comps rows. Returns how many were removed.

    Scoped to one candidate and wrapped in a transaction: either every row for
    that candidate goes, or none does. No other candidate is affected.
    """
    with conn:                       # BEGIN ... COMMIT / ROLLBACK
        cur = conn.execute("DELETE FROM sold_comps WHERE candidate_item_id = ?",
                           (cid,))
        return cur.rowcount


def maybe_reset(conn, args):
    """Perform the reset only when explicitly asked. --force never deletes."""
    validate_flags(args)
    if not getattr(args, "reset_comps", False):
        return None
    deleted = reset_comps(conn, args.candidate_id)
    print(f"  --reset-comps: deleted {deleted} sold_comps row(s) for "
          f"{args.candidate_id}")
    return deleted


def new_run_id():
    return uuid.uuid4().hex[:16]


def batch_label(requested):
    """The label every candidate in one invocation is stored under.

    `--batch-id` used to be read only by --report and --audit-accepted, while
    the collector always minted a fresh id, so the candidates of a labelled run
    could not be grouped afterwards by the label their operator gave them.
    """
    return (requested or "").strip() or new_run_id()


def reclassify_comps(conn, cid, cand, only_ids=None, run_id=None):
    """Re-decide every stored row for this candidate via the comp classifier.

    import_rows() stores and matches; this applies the comp-specific evidence
    rules (year hoisting, absent-vs-conflicting field) on top, so a row that the
    raw matcher rejected for a field the title merely omits is recorded as
    review_required. The matcher itself is untouched.
    """
    counts = collections.Counter()
    sql = "SELECT id, raw_title, source_item_id FROM sold_comps WHERE candidate_item_id=?"
    for r in conn.execute(sql, (cid,)).fetchall():
        # Only this run's rows count toward the invariant. Rows left by an
        # earlier run stay in the table but are not double-counted.
        if only_ids is not None and r["source_item_id"] not in only_ids:
            continue
        state, reason = prp.classify_comp(cand, r["raw_title"],
                                          source_item_id=r["source_item_id"])
        counts[state] += 1
        conn.execute(
            "UPDATE sold_comps SET accepted=?, match_confidence=?, "
            "rejection_reason=?, run_id=COALESCE(?, run_id) WHERE id=?",
            (1 if state == prp.ACCEPTED else 0,
             {"accepted": "EXACT", "review_required": "REVIEW_REQUIRED",
              "rejected": None}[state],
             reason, run_id, r["id"]))
    conn.commit()
    return counts


def save_unclassified(cid, rows, counts, extracted, classified_ids=(),
                      run_id=None, batch_id=None):
    """Invariant breach: dump everything needed to find the missing row."""
    ensure_dirs()
    path = os.path.join(ARTIFACT_DIR,
                        f"{re.sub(r'[^0-9A-Za-z]', '', cid)[-12:]}_unclassified.json")
    extracted_ids = [r.get("source_item_id") for r in rows]
    seen = collections.Counter(extracted_ids)
    collisions = {k: v for k, v in seen.items() if v > 1}
    classified = set(classified_ids)
    payload = {
        "candidate_id": cid, "run_id": run_id, "batch_id": batch_id,
        "extracted": extracted, "counts": dict(counts),
        "extracted_ids": extracted_ids,
        "unique_extracted_ids": sorted(set(extracted_ids)),
        "classified_ids": sorted(classified),
        "missing_ids": sorted(set(extracted_ids) - classified),
        "unexpected_classified_ids": sorted(classified - set(extracted_ids)),
        "collision_groups": {k: [r for r in rows if r.get("source_item_id") == k]
                             for k in collisions},
        "rows": rows,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return path


def mark_review_required(conn, cid):
    """Re-label rejections caused by ABSENT evidence rather than a conflict.

    The row stays rejected for valuation - it is never counted as a comp - but
    it is visibly distinguished from a genuine mismatch.
    """
    n = 0
    for r in conn.execute(
            "SELECT id, rejection_reason FROM sold_comps "
            "WHERE candidate_item_id=? AND accepted=0", (cid,)).fetchall():
        if r["rejection_reason"] and prp._MISSING_EVIDENCE.search(r["rejection_reason"]):
            conn.execute(
                "UPDATE sold_comps SET match_confidence='REVIEW_REQUIRED', "
                "rejection_reason=? WHERE id=?",
                (f"{r['rejection_reason']} (field absent from title, "
                 "not a conflict)", r["id"]))
            n += 1
    conn.commit()
    return n


def raw_artifact_path(cid, level, suffix=""):
    stem = re.sub(r"[^0-9A-Za-z]", "", cid)[-12:]
    return os.path.join(RAW_DIR, f"{stem}_{level}{suffix}.json")


def write_raw_artifact(cid, level, query, *, state, rows, records, header,
                       table_rows, date_range, run_id, batch_id,
                       completion_status="attempt_completed"):
    """Persist one completed attempt. Returns the path written.

    `rows` is what was actually parsed and stored. `table_rows` only ever holds
    anything on the legacy <table> layout, so writing it alone left the artifact
    empty on every div/grid page.

    A zero-row result never overwrites an existing artifact that has rows - it
    is written alongside, keyed by run id, so re-running a candidate whose sales
    have since aged out of the window cannot erase the earlier evidence.
    """
    path = raw_artifact_path(cid, level)
    if not rows and os.path.exists(path):
        try:
            if (json.load(open(path, encoding="utf-8")).get("rows") or []):
                path = raw_artifact_path(cid, level, f"_norows_{run_id}")
        except (ValueError, OSError):
            pass                       # unreadable historical file: leave it
    payload = {
        "candidate_id": cid, "run_id": run_id, "batch_id": batch_id,
        "query": query, "level": level, "page_state": state,
        "collected_at": now(), "date_range": date_range,
        "row_count": len(rows), "completion_status": completion_status,
        "source": "records" if records else ("table" if table_rows else "none"),
        "header": header, "rows": rows, "legacy_table_rows": table_rows,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return path


def finalize_raw_artifacts(paths, status):
    """Stamp the run's real outcome onto the artifacts it wrote."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (ValueError, OSError):
            continue
        payload["completion_status"] = status
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)


def collect_candidate(conn, page, cand, args, run_id=None, batch_id=None):
    cid = cand["item_id"]
    run_id = run_id or new_run_id()
    set_status(conn, cid, RUNNING, run_id=run_id, batch_id=batch_id,
               attempts=(get_status(conn, cid) or {}).get("attempts", 0) + 1)
    print(f"\n  candidate {cid}")
    # Print the FULL title: truncating it here has twice looked like a data bug
    # ("...ALESI 7" hid "7/50 PSA 8").
    print(f"    {cand['title']}")

    all_rows, used_level, used_query, date_range = [], None, None, "unknown"
    seen_keys, tiers_attempted, rows_seen = {}, [], 0
    written = []                       # raw artifacts this run has written
    for level, query in prp.query_levels(cand):
        tiers_attempted.append(level)
        print(f"    [{level}] {query}")
        try:
            html = run_search(page, query, args.timeout * 1000)
        except Exception as exc:
            base = save_artifacts(page, cid, "search_error")
            set_status(conn, cid, FAILED, run_id=run_id, last_error=str(exc)[:300],
                       query_level=level, query_used=query)
            print(f"    FAILED: {exc}\n    artifacts: {base}.png / .html")
            return FAILED

        state = prp.detect_page_state(html, query_submitted=True)
        if state == prp.LOGIN_REQUIRED:
            save_artifacts(page, cid, "login")
            set_status(conn, cid, AUTH_REQUIRED, run_id=run_id,
                       query_level=level, query_used=query)
            print("    SESSION EXPIRED - run --login and sign in again.")
            return AUTH_REQUIRED
        if state == prp.VERIFICATION_REQUIRED:
            save_artifacts(page, cid, "verification")
            set_status(conn, cid, AUTH_REQUIRED, run_id=run_id,
                       query_level=level, query_used=query)
            print("    eBay is asking for verification. Stopping.\n"
                  "    Complete it yourself in the browser, then re-run --resume.")
            return AUTH_REQUIRED

        date_range = read_date_range(html)
        if state in (prp.UNSUPPORTED_LAYOUT, prp.EXTRACTION_ERROR):
            base = save_artifacts(page, cid, "layout")
            set_status(conn, cid, FAILED, run_id=run_id,
                       query_level=level, query_used=query, date_range=date_range,
                       last_error=f"{state}: results visible but 0 rows parsed")
            print(f"    {state.upper()}: the page shows results but nothing "
                  f"parsed.\n    artifacts: {base}.png / .html")
            return FAILED

        records = prp.extract_result_rows(html)
        header, table_rows = prp.parse_html_table(html)
        if records:
            rows = prp.records_to_rows(records, cid, query, level)
        elif table_rows:
            rows = prp.rows_from_table(header, table_rows, cid, query, level)
        else:
            rows = []
        # Every completed attempt leaves a raw artifact, including one that
        # found nothing. A search that returned no sales is a real answer about
        # the market and has to be as auditable as one that returned fifty.
        written.append(write_raw_artifact(
            cid, level, query, state=state, rows=rows, records=records,
            header=header, table_rows=table_rows, date_range=date_range,
            run_id=run_id, batch_id=batch_id))
        if state == prp.RESULTS_OK and rows:
            # Merge across tiers, keeping the STRICTEST tier that found a sale.
            fresh = []
            for r in rows:                 # update as we go: a repeat WITHIN
                key = r["source_item_id"]  # one tier is a duplicate too
                if key in seen_keys:
                    continue
                seen_keys[key] = level
                fresh.append(r)
            all_rows.extend(fresh)
            tier_raw_count = len(rows)
            tier_new_unique_count = len(fresh)
            cumulative_unique_count = len(all_rows)
            rows_seen += tier_raw_count
            exact = sum(1 for r in all_rows
                        if prp.classify_comp(
                            cand, r["raw_title"],
                            source_item_id=r.get("source_item_id"))[0]
                        == prp.ACCEPTED)
            print(f"    extracted {tier_raw_count}, {tier_new_unique_count} new, "
                  f"cumulative unique {cumulative_unique_count}"
                  f"   exact comps so far: {exact}")
            used_level, used_query = level, query
            if exact >= MIN_EXACT_COMPS:
                break                      # enough evidence; stop widening
            print(f"    only {exact}/{MIN_EXACT_COMPS} exact comps - "
                  "continuing to the next tier")
        else:
            print(f"    no rows at this tier ({state})")
            used_level, used_query = level, query
        time.sleep(random.uniform(args.delay_min, args.delay_max))

    if not all_rows:
        set_status(conn, cid, NO_RESULTS, run_id=run_id, query_level=used_level,
                   query_used=used_query, rows_extracted=0, date_range=date_range)
        finalize_raw_artifacts(written, NO_RESULTS)
        print(f"    NO RESULTS at any allowed tier"
              f"{' - raw artifact: ' + written[-1] if written else ''}")
        return NO_RESULTS

    # Attribution is already fixed (these rows came from this candidate's
    # search). Acceptance is decided solely by the existing matcher.
    stats = mc.import_rows(conn, all_rows, attribute_by_title=False)
    run_ids = {r["source_item_id"] for r in all_rows}
    counts = reclassify_comps(conn, cid, cand, only_ids=run_ids,
                              run_id=run_id)
    cumulative_unique_count = len(all_rows)
    cumulative_accepted = accepted = counts[prp.ACCEPTED]
    cumulative_rejected = rejected = counts[prp.REJECTED]
    cumulative_review_required = review = counts[prp.REVIEW_REQUIRED]

    # Every unique transaction considered must end in exactly one class.
    classified = (cumulative_accepted + cumulative_rejected
                  + cumulative_review_required)
    if classified != cumulative_unique_count:
        traced = []
        for r in all_rows:
            state, reason = prp.classify_comp(
                cand, r["raw_title"], source_item_id=r.get("source_item_id"))
            traced.append({**r, "decision": state, "reason": reason})
        classified_ids = [r[0] for r in conn.execute(
            "SELECT source_item_id FROM sold_comps WHERE candidate_item_id=? "
            "AND run_id=?", (cid, run_id))]
        path = save_unclassified(cid, traced, counts, len(all_rows),
                                 classified_ids=classified_ids,
                                 run_id=run_id, batch_id=batch_id)
        set_status(conn, cid, FAILED, run_id=run_id, query_level=used_level,
                   query_used=used_query, rows_extracted=len(all_rows),
                   accepted=accepted, rejected=rejected, date_range=date_range,
                   rows_seen=rows_seen,
                   last_error=(f"extraction_error: {classified} classified != "
                               f"{cumulative_unique_count} unique"))
        print(f"    EXTRACTION_ERROR: {classified} classified != "
              f"{cumulative_unique_count} unique comps considered"
              f"\n    trace: {path}")
        return FAILED

    status = COMPLETED if accepted >= MIN_EXACT_COMPS else INSUFFICIENT
    finalize_raw_artifacts(written, status)
    set_status(conn, cid, status, run_id=run_id, query_level=used_level,
               query_used=used_query,
               rows_extracted=cumulative_unique_count, rows_seen=rows_seen,
               accepted=accepted, rejected=rejected, review_required=review,
               date_range=date_range)
    print(f"    tiers={'+'.join(tiers_attempted)}  rows seen across tiers="
          f"{rows_seen}  unique comps considered={cumulative_unique_count}")
    print(f"    accepted={accepted} rejected={rejected} "
          f"review_required={review}  ({accepted}+{rejected}+{review}="
          f"{classified}) -> {status}")
    return status


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def do_login(args):
    ensure_dirs()
    sync_playwright = load_playwright()
    print("Opening a visible browser. Sign in to eBay yourself, open\n"
          "Seller Hub > Research > Product Research, then close the window.\n"
          "Nothing about your credentials is read or stored by this script.\n")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, viewport={"width": 1500, "height": 950})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(RESEARCH_URL, wait_until="domcontentloaded")
        print("Waiting for you to finish (close the browser window when done)...")
        try:
            page.wait_for_event("close", timeout=args.login_timeout * 1000)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    print(f"\nSession stored in {PROFILE_DIR}/ - re-usable for future runs.")


def select_candidates(conn, args):
    cands = mc.load_candidates()
    if args.candidate_id:
        if args.candidate_id not in cands:
            sys.exit(f"error: {args.candidate_id} is not in {mc.CANDIDATES}")
        chosen = [cands[args.candidate_id]]
    else:
        order = [i for i in mc.PILOT if i in cands]
        order += [i for i in cands if i not in order]
        chosen = [cands[i] for i in order]

    if args.resume or args.retry_failed:
        keep = []
        for c in chosen:
            st = get_status(conn, c["item_id"])
            if st is None:
                keep.append(c)
            elif args.retry_failed and st["status"] in (FAILED, AUTH_REQUIRED):
                keep.append(c)
            elif not args.force and st["status"] in TERMINAL_OK:
                continue
            elif args.force:
                keep.append(c)
        chosen = keep
    elif not args.force:
        chosen = [c for c in chosen
                  if (get_status(conn, c["item_id"]) or {}).get("status")
                  not in TERMINAL_OK]
    return chosen[:args.limit] if args.limit else chosen


def do_collect(args):
    ensure_dirs()
    conn = db.connect(args.db)
    import enrich
    enrich.load_surnames(conn)
    validate_flags(args)
    todo = select_candidates(conn, args)
    if todo:
        maybe_reset(conn, args)
    if not todo:
        print("nothing to do (all selected candidates already completed; "
              "use --force or --retry-failed)")
        return
    mode = "attached Chrome" if args.connect_existing else (
        "headed" if args.headed else "headless")
    batch_id = batch_label(args.batch_id)
    print(f"collecting {len(todo)} candidate(s), one at a time, {mode}")
    print(f"batch {batch_id}")
    sync_playwright = load_playwright()
    with sync_playwright() as p:
        page, close = open_browser(p, args)
        state = open_research(page, args.timeout * 1000)
        if state in (prp.LOGIN_REQUIRED, prp.VERIFICATION_REQUIRED):
            save_artifacts(page, "session", state)
            close()
            sys.exit(f"error: {state} - sign in inside your browser first, "
                     "then re-run")

        for i, cand in enumerate(todo, 1):
            print(f"\n[{i}/{len(todo)}]", end="")
            status = collect_candidate(conn, page, cand, args,
                                       run_id=new_run_id(), batch_id=batch_id)
            if status == AUTH_REQUIRED:
                print("\nStopping the run so you can resolve it manually.")
                break
            if i < len(todo):
                time.sleep(random.uniform(args.delay_min, args.delay_max))
        close()
    try:
        report(conn, candidate_id=args.candidate_id, batch_id=batch_id)
    except Exception as exc:              # rendering only - collection stands
        print(f"\n  REPORT_ERROR: {type(exc).__name__}: {exc}")
        print("  The collection result above is unaffected and remains stored.")


def do_check_connection(args):
    """Attach, report what we can see, change nothing."""
    sync_playwright = load_playwright()
    with sync_playwright() as p:
        page, close = open_browser(p, args, allow_new_page=False)
        try:
            on_research = "/sh/research" in page_url(page)
            if not on_research:
                print("  note: attached tab is not on Product Research; "
                      "reading it as-is without navigating")
            state = open_research(page, args.timeout * 1000, navigate=False)
            print(f"  page state: {state}")
            if state == prp.LOGIN_REQUIRED:
                print("  -> not signed in. Sign in inside that Chrome window.")
            elif state == prp.VERIFICATION_REQUIRED:
                print("  -> eBay wants verification. Complete it manually.")
            elif state in (prp.RESULTS_OK, prp.RESEARCH_READY,
                           prp.EMPTY_RESULTS):
                print(f"  -> Product Research reachable. "
                      f"date range: {read_date_range(safe_content(page, 5000))}")
            else:
                base = save_artifacts(page, "check", "unknown")
                print(f"  -> page not recognized; saved {base}.html for review")
        finally:
            close()


def _run_rows(conn, cid, run_id):
    """Every stored comp for ONE run. Historical rows are never mixed in."""
    if run_id:
        sql = ("SELECT * FROM sold_comps WHERE candidate_item_id=? AND run_id=? "
               "ORDER BY id")
        return conn.execute(sql, (cid, run_id)).fetchall()
    # No run id recorded (a pre-run_id row): fall back to the candidate, but say so.
    return conn.execute(
        "SELECT * FROM sold_comps WHERE candidate_item_id=? AND run_id IS NULL "
        "ORDER BY id", (cid,)).fetchall()


def priced(row):
    """A comp usable for valuation: it has a real sold price.

    An accepted IDENTITY match and a priced VALUATION comp are different
    things. A missing price is never treated as zero.
    """
    return row["sold_price"] is not None


def valuation_confidence(priced_count):
    if priced_count >= 5:
        return "HIGH"
    if priced_count >= 3:
        return "MEDIUM"
    if priced_count >= 1:
        return "LOW"
    return "NONE"


def split_rows(rows):
    """One pass, three lists - always defined, whatever the outcome."""
    accepted_rows, rejected_rows, review_rows = [], [], []
    for r in rows:
        if r["accepted"]:
            accepted_rows.append(r)
        elif (r["match_confidence"] or "") == "REVIEW_REQUIRED":
            review_rows.append(r)
        else:
            rejected_rows.append(r)
    return accepted_rows, rejected_rows, review_rows


def reason_counts(rows):
    counts = collections.Counter(r["rejection_reason"] or "(no reason recorded)"
                                 for r in rows)
    return counts.most_common()


def candidate_decision(conn, cand, priced_rows):
    """BUY/WATCH/PASS from accepted priced comps only.

    review_required and rejected rows are never in `priced_rows`, so they can
    never influence the outcome.
    """
    listing = conn.execute(
        "SELECT active, price, shipping_cost FROM listings WHERE item_id=?",
        (cand["item_id"],)).fetchone()
    comps = [{"total_price": r["total_price"], "sale_date": r["sale_date"]}
             for r in priced_rows]
    # Comp totals include shipping, so the candidate side must too. The
    # listing row is authoritative; a candidate export may carry only a price.
    item_price = listing["price"] if listing else cand.get("asking_price")
    shipping = listing["shipping_cost"] if listing else cand.get("shipping")
    return dec.decide(item_price, comps, shipping=shipping,
                      listing_active=bool(listing["active"]) if listing else True)


def report_candidate(conn, run, cand):
    """Render one candidate for one run. Never raises on empty inputs."""
    import statistics
    rows = _run_rows(conn, run["candidate_id"], run["run_id"])
    accepted_rows, rejected_rows, review_rows = split_rows(rows)
    total_n = len(accepted_rows) + len(rejected_rows) + len(review_rows)
    ask = cand["asking_price"] or 0.0

    print(f"\n  {cand['title'][:70]}")
    print(f"    status         : {run['status']}   tier={run['query_level']}   "
          f"date range={run['date_range']}")
    print(f"    query          : {run['query_used']}")
    print(f"    run            : {run['run_id'] or '(pre-run_id rows)'}")
    print(f"    PSA asking     : ${ask:,.2f}")
    print(f"    rows seen across tiers  : {run['rows_seen'] or 0}")
    print(f"    unique comps considered : {run['rows_extracted'] or 0}")
    print(f"    accepted: {len(accepted_rows)}   rejected: {len(rejected_rows)}"
          f"   review_required: {len(review_rows)}"
          f"   ({len(accepted_rows)}+{len(rejected_rows)}+{len(review_rows)}"
          f"={total_n})")
    if run["rows_extracted"] and total_n != run["rows_extracted"]:
        print(f"    ACCOUNTING WARNING: {total_n} classified != "
              f"{run['rows_extracted']} unique comps considered")

    for reason, n in reason_counts(rejected_rows):
        print(f"      rejected {n:3}x  {reason}")
    for reason, n in reason_counts(review_rows):
        print(f"      review   {n:3}x  {reason}")

    priced_rows = [r for r in accepted_rows if priced(r)]
    unpriced = len(accepted_rows) - len(priced_rows)
    print(f"    accepted identity comps : {len(accepted_rows)}"
          f"   priced: {len(priced_rows)}   unpriced: {unpriced}")
    if unpriced:
        print(f"    {unpriced} accepted comp(s) carry no usable sold price and are "
              "excluded from valuation")

    if not accepted_rows:
        print("    INSUFFICIENT EVIDENCE - no accepted exact comps")
        print("    valuation       : unavailable (NONE)")
        print(dec.format_decision(candidate_decision(conn, cand, [])))
        return
    if not priced_rows:
        # Identity is proven, price evidence is not. No estimate may be shown.
        print("    INSUFFICIENT EVIDENCE - no accepted comp has a usable price")
        print("    valuation       : unavailable (NONE) - 0 priced comps")
        print(dec.format_decision(candidate_decision(conn, cand, priced_rows)))
        return

    items = [r["sold_price"] for r in priced_rows if r["sold_price"] is not None]
    totals = [r["total_price"] for r in priced_rows
              if r["total_price"] is not None]
    dates = sorted(r["sale_date"] for r in priced_rows if r["sale_date"])
    if items:
        print(f"    sold item price: median ${statistics.median(items):,.2f}  "
              f"min ${min(items):,.2f}  max ${max(items):,.2f}")
    if totals:
        med_total = statistics.median(totals)
        gap, pct = market_gap(ask, med_total)
        print(f"    market total median : ${med_total:,.2f}  "
              f"mean ${statistics.mean(totals):,.2f}  "
              f"min ${min(totals):,.2f}  max ${max(totals):,.2f}")
        print(f"    asking price        : ${ask:,.2f}")
        print(f"    market gap          : ${gap:,.2f}  ({pct:+.1f}%)"
              f"  {gap_label(gap)}")
        print("                          gross price gap before taxes, import "
              "costs, marketplace fees and resale costs")
    if dates:
        print(f"    most recent exact sale: {dates[-1]}")
    print(f"    valuation conf  : {valuation_confidence(len(priced_rows))} "
          f"({len(priced_rows)} priced comps of {len(accepted_rows)} accepted)")
    if len(priced_rows) < MIN_EXACT_COMPS:
        print("    INSUFFICIENT EVIDENCE - treat this estimate as unreliable")
    print(dec.format_decision(candidate_decision(conn, cand, priced_rows)))


def market_gap(asking_price, market_total_median):
    """Positive = asking is BELOW market (a discount); negative = a premium."""
    gap = (market_total_median or 0) - (asking_price or 0)
    pct = (100 * gap / market_total_median) if market_total_median else 0.0
    return gap, pct


def gap_label(gap):
    if gap > 0:
        return "discount to market"
    if gap < 0:
        return "premium over market"
    return "at market"


def candidate_provenance(conn, cid):
    """Where each candidate field came from: title, Tier B aspects, or both."""
    import json as _json
    row = conn.execute("SELECT * FROM listings WHERE item_id=?", (cid,)).fetchone()
    card = conn.execute("SELECT * FROM cards WHERE item_id=?", (cid,)).fetchone()
    tb = conn.execute("SELECT * FROM tierb WHERE item_id=?", (cid,)).fetchone()
    aspects = _json.loads(tb["aspects_json"] or "{}") if tb else {}
    api_title = _json.loads(row["raw"])["title"] if row else None
    return {
        "api_title": api_title,
        "api_title_length": len(api_title or ""),
        "title_truncation_risk": bool(card and card["truncation_risk"]),
        "item_url": row["url"] if row else None,
        "asking_price_source": "Browse API item_summary.price.value",
        "asking_price": row["price"] if row else None,
        "condition": row["condition"] if row else None,
        "tier_a": {
            "grade_value": card["grade_value"] if card else None,
            "grade_raw": card["grade_raw"] if card else None,
            "grade_conf": card["grade_conf"] if card else None,
            "print_run": card["print_run"] if card else None,
            "is_auto": card["is_auto"] if card else None,
            "auto_grade": card["auto_grade"] if card else None,
            "parallel": card["parallel"] if card else None,
        },
        "tier_b_aspects": aspects,
        "grade_provenance": (
            "tier_a title token '%s'" % (card["grade_raw"] if card else "?")
            + (" + tier_b aspect Grade=%s" % aspects.get("Grade")
               if aspects.get("Grade") else " (no tier_b grade aspect)")),
        "auto_provenance": (
            "explicit AUTO/AUTOGRAPH token in title"
            if (card and card["is_auto"]) else "not signed"),
        "print_run_provenance": (
            "'#/N' or 'n/N' serial syntax in title"
            if (card and card["print_run"]) else "no print run stated"),
    }


def query_token_provenance(cand):
    """Explain every token the STRICT query is built from."""
    out = []
    for level, query in prp.query_levels(cand):
        toks = []
        for tok in query.split():
            up = tok.upper()
            if up == str(cand["year"]):
                src = "candidate.year"
            elif cand["subject"] and up in cand["subject"].upper():
                src = "candidate.subject"
            elif cand["manufacturer"] and up in cand["manufacturer"].upper():
                src = "candidate.manufacturer"
            elif cand["set"] and up in cand["set"].upper():
                src = "candidate.set"
            elif tok.startswith("#"):
                src = "candidate.card_number"
            elif tok.startswith("/"):
                src = "candidate.print_run"
            elif up == "AUTO":
                src = "candidate.is_auto"
            elif up == "PSA" or up == str(cand["psa_grade"]):
                src = "candidate.psa_grade"
            elif cand["parallel"] and up in cand["parallel"].upper():
                src = "candidate.parallel"
            else:
                src = "unattributed"
            toks.append({"term": tok, "source": src})
        out.append({"tier": level, "query": query, "tokens": toks})
    return out


def audit_accepted(conn, candidate_id=None, out_path="accepted_audit.json",
                   batch_id=None):
    """Export every accepted comp with its parsed fields and decision trace.

    Precision check before any ranking is built on these numbers.
    """
    cands = mc.load_candidates()
    sql = ("SELECT * FROM sold_comps WHERE accepted=1")
    params = []
    if batch_id:
        sql += (" AND run_id IN (SELECT run_id FROM pr_runs WHERE batch_id=?)")
        params.append(batch_id)
    if candidate_id:
        sql += " AND candidate_item_id=?"
        params.append(candidate_id)
    rows = conn.execute(sql + " ORDER BY candidate_item_id, sold_price",
                        params).fetchall()

    grouped, export = collections.OrderedDict(), []
    for r in rows:
        cand = cands.get(r["candidate_item_id"])
        if cand is None:
            continue
        title = prp.clean_listing_title(r["raw_title"])
        repaired = prp.repair_print_run(prp.normalize_comp_title(title))
        f = parse.parse_title(repaired)["fields"]
        state, reason = prp.classify_comp(
            cand, r["raw_title"], source_item_id=r["source_item_id"])
        rec = {
            "candidate": cand["title"], "candidate_id": r["candidate_item_id"],
            "sold_price": r["sold_price"], "shipping": r["shipping"],
            "total_price": r["total_price"], "currency": r["currency"],
            "sale_date": r["sale_date"], "query_tier": r["query_tier"],
            "raw_title": r["raw_title"], "cleaned_title": title,
            "repaired_title": repaired,
            "parsed": {"year": f["year"], "set": f["set_name"],
                       "parallel": f["parallel"], "card_number": f["card_number"],
                       "grade": f["grade_value"], "qualifier": f["grade_qualifier"],
                       "is_auto": f["is_auto"], "auto_grade": f["auto_grade"],
                       "print_run": f["print_run"], "serial": f["serial_num"],
                       "athlete": f["athlete"]},
            "decision": state, "decision_reason": reason,
            "priced": r["sold_price"] is not None,
            "valuation_exclusion_reason": (
                None if r["sold_price"] is not None
                else "no usable sold price - excluded from valuation"),
            "candidate_provenance": candidate_provenance(conn, r["candidate_item_id"]),
            "query_token_provenance": query_token_provenance(cand),
        }
        grouped.setdefault(cand["title"], []).append(rec)
        export.append(rec)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(export, fh, indent=1)

    print("=" * 78)
    print("  ACCEPTED-COMP PRECISION AUDIT")
    print("=" * 78)
    total_priced = sum(1 for r in export if r["priced"])
    print(f"  accepted rows: {len(export)}   priced: {total_priced}   "
          f"unpriced: {len(export) - total_priced}")
    for title, recs in grouped.items():
        cand = next(c for c in cands.values() if c["title"] == title)
        prices = [r["sold_price"] for r in recs if r["sold_price"] is not None]
        n_priced = sum(1 for r in recs if r["priced"])
        print(f"\n  {title[:70]}")
        head = (f"    ask ${cand['asking_price']:,.2f}   accepted {len(recs)}"
                f"   priced {n_priced}   unpriced {len(recs) - n_priced}")
        if prices:
            head += f"   price range ${min(prices):,.2f}-${max(prices):,.2f}"
        else:
            head += "   NO PRICED COMPS - no market estimate possible"
        print(head)
        for r in sorted(recs, key=lambda x: (x["sold_price"] is None,
                                             x["sold_price"] or 0))[:4]:
            p = r["parsed"]
            price_txt = ("     n/a" if r["sold_price"] is None
                         else f"{r['sold_price']:>8,.2f}")
            ship_txt = ("  n/a" if r["shipping"] is None
                        else f"{r['shipping']:>5,.2f}")
            print(f"      ${price_txt} +${ship_txt}"
                  f"  [{r['query_tier']}] {r['cleaned_title'][:52]}")
            if not r["priced"]:
                print(f"          EXCLUDED FROM VALUATION: "
                      f"{r['valuation_exclusion_reason']}")
            print(f"          yr={p['year']} set={str(p['set'])[:18]!r} "
                  f"par={str(p['parallel'])[:14]!r} #={p['card_number']!r} "
                  f"PSA {p['grade']} run={p['print_run']} auto={p['is_auto']}")
        if len(recs) > 4:
            print(f"      ... and {len(recs) - 4} more (see {out_path})")
    print(f"\n  full export -> {out_path}")
    print("=" * 78)


def report(conn, candidate_id=None, run_id=None, batch_id=None):
    """Report THIS invocation only - one batch, one run per candidate.

    Without a batch id (a standalone --report) it falls back to the latest run
    per candidate, still never mixing runs together.
    """
    cands = mc.load_candidates()
    where, params = [], []
    if batch_id:
        where.append("batch_id = ?")
        params.append(batch_id)
    if candidate_id:
        where.append("candidate_id = ?")
        params.append(candidate_id)
    sql = "SELECT * FROM pr_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    runs = conn.execute(sql + " ORDER BY updated_at", params).fetchall()

    print("\n" + "=" * 78)
    print("  PRODUCT RESEARCH COLLECTION REPORT")
    print("=" * 78)
    if not runs:
        print("  nothing collected yet")
        print("=" * 78)
        return
    for run in runs:
        cand = cands.get(run["candidate_id"])
        if cand is None:
            continue
        run = dict(run)
        if run_id and run["candidate_id"] == candidate_id:
            run["run_id"] = run_id
        report_candidate(conn, run, cand)
    print("\n  Decisions above are GROSS opportunities before taxes, import "
          "costs,\n  marketplace fees and resale costs.")
    print("=" * 78)


def main():
    global RAW_DIR
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--login-timeout", type=int, default=900)
    ap.add_argument("--candidates", default=mc.CANDIDATES,
                    help=f"candidate file (default {mc.CANDIDATES})")
    ap.add_argument("--candidate-id")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--headed", dest="headed", action="store_true", default=True)
    ap.add_argument("--headless", dest="headed", action="store_false")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-collect a completed candidate (never deletes)")
    ap.add_argument("--reset-comps", dest="reset_comps", action="store_true",
                    help="DESTRUCTIVE: delete this candidate's stored sold "
                         "comps before collecting (requires --candidate-id)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--connect-existing", action="store_true",
                    help="attach to a Chrome you started with "
                         "--remote-debugging-port instead of Playwright's profile")
    ap.add_argument("--cdp-url", default=DEFAULT_CDP)
    ap.add_argument("--batch-id", dest="batch_id",
                    help="scope --report / --audit-accepted to one batch")
    ap.add_argument("--audit-accepted", action="store_true",
                    help="export accepted comps with parsed fields + trace")
    ap.add_argument("--check-connection", action="store_true",
                    help="verify the attach works and stop")
    ap.add_argument("--output", default=RAW_DIR)
    ap.add_argument("--delay-min", type=float, default=4.0)
    ap.add_argument("--delay-max", type=float, default=9.0)
    ap.add_argument("--timeout", type=int, default=45)
    args = ap.parse_args()
    validate_flags(args)

    if args.candidates != mc.CANDIDATES:
        mc.CANDIDATES = args.candidates
    RAW_DIR = args.output

    if args.login:
        do_login(args)
        return
    if args.report:
        report(db.connect(args.db), candidate_id=args.candidate_id,
               batch_id=args.batch_id)
        return
    if args.audit_accepted:
        audit_accepted(db.connect(args.db), candidate_id=args.candidate_id,
                       batch_id=args.batch_id)
        return
    if args.check_connection:
        do_check_connection(args)
        return
    do_collect(args)


if __name__ == "__main__":
    main()
