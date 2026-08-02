"""Parsing layer for eBay Product Research pages.

Deliberately free of any Playwright import so the whole extraction path is
testable against sanitized HTML fixtures without a browser.

This module only reads pages and shapes rows. It never decides whether a
transaction matches a candidate - that stays with manual_comps.match().
"""

import datetime as dt
import hashlib
import re

from bs4 import BeautifulSoup

import card_vocab
import ebay_product_research_import as adapter
import enrich
import manual_comps as mc
import parse

# Page states the driver must react to before trying to read a table.
RESULTS_OK = "results_ok"
RESEARCH_READY = "research_ready"      # loaded, no query submitted yet
UNSUPPORTED_LAYOUT = "unsupported_layout"   # results on screen, 0 rows parsed
EXTRACTION_ERROR = "extraction_error"       # parser raised
EMPTY_RESULTS = "empty_results"
LOGIN_REQUIRED = "login_required"
VERIFICATION_REQUIRED = "verification_required"
UNKNOWN_PAGE = "unknown_page"

LOGIN_MARKERS = [
    "sign in to your account", "signin.ebay", "sign in or register",
    "please sign in", "user id or email",
]
VERIFY_MARKERS = [
    "verify your identity", "confirm it's you", "confirm it is you",
    "security challenge", "captcha", "unusual activity", "two-step",
    "verification code", "help us keep your account secure",
]
# Positive markers for Seller Hub Product Research (Terapeak). Each is specific
# to that app - none of them appear on a generic eBay page.
PRODUCT_RESEARCH_MARKERS = [
    "sh_terapeak_research_default",              # tracking pageName
    "research-container",                        # app container class
    "search-input-panel__research-button",       # the "Research" button
    "enter keywords, mpn, upc, epid, ean or isbn",   # search box placeholder
]
PRODUCT_RESEARCH_TITLE = "product research"      # <title>, e.g. "... - eBay Seller Hub"

EMPTY_MARKERS = [
    "no results found", "we couldn't find", "we could not find",
    "0 results", "no items matched", "try a different search",
    "no sold items",
]

# Column labels taken verbatim from the app's own i18n block
# (soldResults.columnHeader in the live page). These are stable app strings,
# not generated CSS class names.
SOLD_COLUMNS = {
    "listing": ["listing"],
    "avg_sold_price": ["avg sold price", "avg. sold price", "average sold price"],
    "avg_shipping": ["avg shipping", "avg. shipping", "average shipping"],
    "total_sold": ["total sold"],
    "item_sales": ["item sales"],
    "date_last_sold": ["date last sold", "last sold"],
    "bids": ["bids"],
}
# Any of these rendered as visible text means the sold-results UI is on screen.
RESULT_UI_MARKERS = ["avg sold price", "date last sold", "total sold",
                     "item sales"]

MONEY_RE = re.compile(r"[$£€¥]\s?\d[\d,]*(?:\.\d{1,2})?")
# The row states which amount is shipping ("+$32.00 shipping"), which is far
# more reliable than guessing from column order.
SHIPPING_CTX_RE = re.compile(
    r"\+?\s*([$£€¥]\s?\d[\d,]*(?:\.\d{1,2})?)\s*(?:shipping|postage|s/h)",
    re.I)
FREE_SHIPPING_RE = re.compile(r"free\s+shipping", re.I)
DATE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b", re.I)
ITEM_ID_RE = re.compile(r"/itm/(?:[^/]*/)?(\d{9,15})")


def _norm_label(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower().rstrip(":")


def column_order(soup):
    """Header labels in document order, from role=columnheader / th / any
    element whose entire text is exactly a known column label."""
    found = []
    for el in soup.find_all(True):
        if el.find(True):                      # leaf-ish elements only
            continue
        label = _norm_label(el.get_text(" ", strip=True))
        if not label:
            continue
        for key, aliases in SOLD_COLUMNS.items():
            if label in aliases and key not in [k for k, _ in found]:
                found.append((key, el))
                break
    return [k for k, _ in found]


def _looks_like_row(el):
    """A result row: contains a money amount and a date, and no descendant
    that already satisfies both (so we take the innermost row block)."""
    text = el.get_text(" ", strip=True)
    if not (MONEY_RE.search(text) and DATE_RE.search(text)):
        return False
    for child in el.find_all(True):
        ctext = child.get_text(" ", strip=True)
        if MONEY_RE.search(ctext) and DATE_RE.search(ctext):
            return False
    return True


def extract_result_rows(html):
    """Layout-agnostic extraction of populated sold results.

    Uses semantics rather than eBay's generated class names: role attributes,
    the app's own column labels, links, and the shape of the content itself
    (a money amount plus a date). Works for a <table>, an ARIA grid, or a plain
    div list.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    order = column_order(soup)

    rows = [el for el in soup.find_all(["tr", "li", "div", "article", "section"])
            if _looks_like_row(el)]
    # role=row wins when present.
    aria_rows = [el for el in soup.find_all(attrs={"role": "row"})
                 if _looks_like_row(el)]
    if aria_rows:
        rows = aria_rows

    out = []
    for el in rows:
        rec = _row_record(el, order)
        if rec:
            out.append(rec)
    return out


def _cells_of(el):
    for attr in ({"role": "gridcell"}, {"role": "cell"}):
        cells = el.find_all(attrs=attr)
        if cells:
            return [c.get_text(" ", strip=True) for c in cells]
    tds = el.find_all(["td", "th"])
    if tds:
        return [c.get_text(" ", strip=True) for c in tds]
    kids = [c for c in el.find_all(recursive=False)
            if c.get_text(" ", strip=True)]
    return [c.get_text(" ", strip=True) for c in kids]


def _row_record(el, order):
    """One result row -> canonical fields. Prices are assigned by column order
    when known, otherwise by position (sold price first, shipping second)."""
    text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
    link = el.find("a", href=True)
    href = link["href"] if link else ""

    title = clean_listing_title(link.get_text(" ", strip=True)) if link else ""
    if len(title) < 12:                        # fall back to the longest cell
        cells = [clean_listing_title(c) for c in _cells_of(el)]
        longest = max(cells, key=len) if cells else ""
        if len(longest) > len(title):
            title = longest
    if len(title) < 8:
        return None

    monies = MONEY_RE.findall(text)
    ship_ctx, ship_span = None, None
    m_ship = SHIPPING_CTX_RE.search(text)
    if m_ship:
        ship_ctx, ship_span = m_ship.group(1), m_ship.span(1)
    free_shipping = bool(FREE_SHIPPING_RE.search(text)) and ship_ctx is None
    dates = DATE_RE.findall(text)
    ints = re.findall(r"(?<![\d.$])\b\d{1,4}\b(?![\d.])", text)

    rec = {"listing": title, "avg_sold_price": None, "avg_shipping": None,
           "total_sold": None, "item_sales": None, "date_last_sold": None,
           "bids": None, "source_item_id": "", "source_url": href,
           "raw_text": text}

    # Prefer what the row itself says over the column order. The live grid
    # renders "$8,987.80 +$32.00 shipping", and header detection had been
    # putting the SOLD PRICE into the shipping field.
    if ship_ctx is not None:
        rec["avg_shipping"] = ship_ctx
        before = text[:ship_span[0]]
        priced = MONEY_RE.findall(before)
        if priced:
            rec["avg_sold_price"] = priced[0]
        else:
            after = [m for m in MONEY_RE.findall(text[ship_span[1]:])]
            if after:
                rec["avg_sold_price"] = after[0]
    elif free_shipping:
        # Only an explicit "Free shipping" may become a zero.
        rec["avg_shipping"] = "0.00"
        if monies:
            rec["avg_sold_price"] = monies[0]
    else:
        money_cols = [c for c in order if c in ("avg_sold_price", "avg_shipping",
                                                "item_sales")]
        if money_cols and len(monies) >= len(money_cols):
            for key, value in zip(money_cols, monies):
                rec[key] = value
        elif monies:
            rec["avg_sold_price"] = monies[0]
            if len(monies) > 1:
                rec["avg_shipping"] = monies[1]
    if dates:
        rec["date_last_sold"] = dates[0]
    if ints:
        rec["total_sold"] = ints[0]
    m = ITEM_ID_RE.search(href)
    if m:
        rec["source_item_id"] = m.group(1)
    return rec


def records_to_rows(records, candidate_id, query, level, collected_at=None):
    """Canonical records -> internal SoldComp row shape (same as the CSV path)."""
    collected_at = collected_at or dt.datetime.now(dt.timezone.utc).isoformat()
    out = []
    for rec in records:
        price, cur_p = adapter.money(rec.get("avg_sold_price"))
        ship, cur_s = adapter.money(rec.get("avg_shipping"))
        currency = (cur_p or cur_s or "USD")
        sale_type = _sale_type(rec.get("raw_text"))
        best_offer = sale_type == "BEST_OFFER"
        orig = None
        known = True
        if best_offer:
            known = False                       # only an average is displayed
            orig, price = price, None
        # The dedup key identifies a TRANSACTION, not a listing. One eBay
        # listing can sell more than once (item 117290548001 sold at $88.00 and
        # again at $53.14), and keying on the item id alone silently merged
        # those into a single row. Price, shipping and date make each sale
        # distinct, while the same sale seen again in a broader tier still
        # collapses to one row.
        seed = "|".join([
            (rec.get("source_item_id") or
             (rec.get("listing") or "").strip().lower()),
            str(rec.get("avg_sold_price") or ""),
            str(rec.get("avg_shipping") or ""),
            str(rec.get("date_last_sold") or "")])
        digest = hashlib.sha1(seed.encode()).hexdigest()[:10]
        ebay_id = rec.get("source_item_id") or ""
        item_id = f"{ebay_id}-{digest}" if ebay_id else f"pr-{digest}"

        out.append({
            "candidate_item_id": candidate_id,
            "query_tier": level,
            "source": adapter.SOURCE,
            "source_item_id": item_id,
            "raw_title": rec.get("listing") or "",
            "sold_price": "" if price is None else f"{price}",
            "shipping": "" if ship is None else f"{ship}",
            "currency": currency,
            "sale_date": rec.get("date_last_sold") or "",
            "condition": "",
            "source_reference": rec.get("source_url") or "",
            "notes": f"Product Research query [{level}]: {query}",
            "best_offer_indicator": "true" if best_offer else "false",
            "displayed_original_price": "" if orig is None else f"{orig}",
            "actual_price_known": "true" if known else "false",
            "sale_type": sale_type or "",
            "collected_at": collected_at,
            "raw_text": rec.get("raw_text") or "",
        })
    return out


# Sale-type wording seen on the results table.
SALE_TYPES = {
    "auction": "AUCTION", "buy it now": "FIXED_PRICE",
    "fixed price": "FIXED_PRICE", "best offer": "BEST_OFFER",
    "accepted offer": "BEST_OFFER",
}


def visible_text(html):
    """Rendered text only. Scripts and styles are excluded so an i18n blob or a
    tracking string can never be mistaken for something the user can see."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).lower()


def page_title(html):
    soup = BeautifulSoup(html or "", "html.parser")
    return (soup.title.string or "").strip().lower() if soup.title else ""


def is_product_research(html):
    """True only for the Seller Hub Product Research app itself.

    Deliberately narrow: a generic eBay page, or another Seller Hub tab, must
    not satisfy this.
    """
    if not html:
        return False
    low = html.lower()
    if PRODUCT_RESEARCH_TITLE in page_title(html):
        return True
    return any(m in low for m in PRODUCT_RESEARCH_MARKERS)


def has_result_ui(html):
    """Are the sold-results columns actually rendered (not just in a script)?"""
    text = visible_text(html)
    return any(m in text for m in RESULT_UI_MARKERS)


def detect_page_state(html, query_submitted=False):
    """Classify a page before attempting extraction.

    `query_submitted` matters: once a search has been sent, a page that clearly
    shows results but yields no parsed rows is an extraction problem, and must
    never be reported as "no results".
    """
    if not html:
        return UNKNOWN_PAGE
    text = visible_text(html)
    # Auth states outrank everything: a challenge page may also render the app
    # shell, and must never be read as usable results.
    if any(m in text for m in VERIFY_MARKERS):
        return VERIFICATION_REQUIRED
    if any(m in text for m in LOGIN_MARKERS):
        return LOGIN_REQUIRED

    try:
        records = extract_result_rows(html)
    except Exception:
        return EXTRACTION_ERROR
    header, table_rows = parse_html_table(html)
    if records or (header and table_rows):
        return RESULTS_OK

    if any(m in text for m in EMPTY_MARKERS):
        return EMPTY_RESULTS
    if has_result_ui(html):
        # Result columns are on screen but nothing parsed. This is our problem,
        # not an empty result set.
        return UNSUPPORTED_LAYOUT
    if is_product_research(html):
        return UNSUPPORTED_LAYOUT if query_submitted and has_result_ui(html) \
            else RESEARCH_READY
    if header and not table_rows:
        return EMPTY_RESULTS
    return UNKNOWN_PAGE


def _looks_like_results_header(cells):
    """Reuse the CSV adapter's column vocabulary - the table uses the same words."""
    mapping = adapter.build_mapping(cells)
    return "raw_title" in mapping and any(
        f in mapping for f in ("sold_price", "total_price"))


def parse_html_table(html):
    """Return (header_cells, data_rows) for the results table, else (None, [])."""
    if not html:
        return None, []
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True)
                     for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if not rows:
            continue
        for i, cells in enumerate(rows):
            if _looks_like_results_header(cells):
                body = [r for r in rows[i + 1:] if any(c.strip() for c in r)]
                return cells, body
    return None, []


# A word whose NEXT token is its value, not a repetition: "PSA 10", "AUTO 10".
VALUE_LABELS = {"PSA", "AUTO"}
_VALUE_RE = re.compile(r"10|[1-9](?:\.5)?|AUTHENTIC|AUTH")


def dedupe_query(text):
    """Drop repeated / redundant search words, preserving order.

    "PRIZM UFC ... RED PRIZM" would otherwise send PRIZM twice, and an
    "AUTOGRAPH" parallel already implies the separate "auto" keyword.

    A value belonging to a label is never a repetition. "auto 10 PSA 10" is a
    card graded 10 whose signature graded 10; blind deduplication turned it into
    "auto 10 PSA" - a query with no grade at all, which the identity invariant
    then rightly refused to send, leaving the candidate unresearchable.
    """
    out, seen = [], set()
    tokens = [t for t in str(text or "").split() if t]
    has_autograph = any(t.upper().startswith("AUTOGRAPH") for t in tokens)
    prev = None
    for tok in tokens:
        key = tok.upper()
        if has_autograph and key == "AUTO":
            continue
        if key in seen and not (prev in VALUE_LABELS
                                and _VALUE_RE.fullmatch(key)):
            continue
        seen.add(key)
        out.append(tok)
        prev = key
    return " ".join(out)


def build_query(candidate, level):
    """Search text for one candidate at one tier, via the existing generator."""
    return dedupe_query(mc.query_terms(candidate, level))


# Defined in manual_comps, which owns query generation. Re-exported here so the
# collector and reports have one name for "tiers that may be sent".
ACTIVE_TIERS = mc.ACTIVE_TIERS


def query_levels(candidate, on_abort=None):
    """The queries that may actually be sent for one candidate.

    Every tier is checked against the canonical identity BEFORE it can be sent.
    A tier that would drop a material discriminator is aborted, not issued -
    searching a different identity cannot produce a comp for this one, and a
    result set gathered that way is worse than no result set.
    """
    levels, seen = [], set()
    for level in ACTIVE_TIERS:
        if level == "RELAXED" and not mc.relaxed_allowed(candidate):
            continue
        q = build_query(candidate, level)
        missing = mc.query_violations(candidate, q)
        if missing:
            if on_abort:
                on_abort(level, q, missing)
            continue
        if q in seen:
            continue
        seen.add(q)
        levels.append((level, q))
    return levels


# Accessibility / UI text eBay renders inside the listing cell.
A11Y_NOISE = [
    ", preview full size image", "preview full size image",
    "opens in a new window or tab", "opens in a new window",
    "close preview", "exclude listing",
]


def clean_listing_title(text):
    """Strip UI/accessibility text and collapse a title repeated in one cell.

    The listing cell contains an image-preview control whose accessible label
    (", preview full size image") is rendered as text, and the title itself
    often appears twice in the same cell.
    """
    out = re.sub(r"\s+", " ", (text or "")).strip()
    low = out.lower()
    for noise in A11Y_NOISE:
        idx = low.find(noise)
        while idx != -1:
            out = (out[:idx] + " " + out[idx + len(noise):]).strip()
            low = out.lower()
            idx = low.find(noise)
    out = re.sub(r"\s+", " ", out).strip(" ,;|-")

    # "TITLE TITLE" -> "TITLE" (exact repetition, or a prefix repeat)
    half = len(out) // 2
    if half > 15 and out[:half].strip().lower() == out[half:].strip().lower():
        out = out[:half].strip()
    else:
        m = re.match(r"^(.{20,}?)\s+\1\b", out, re.S)
        if m:
            out = m.group(1).strip()
    return out


def _sale_type(row_text):
    low = (row_text or "").lower()
    for phrase, value in SALE_TYPES.items():
        if phrase in low:
            return value
    return None


def rows_from_table(header, rows, candidate_id, query, level, collected_at=None):
    """Map extracted table rows onto the internal SoldComp row shape.

    Prices, shipping, currency and Best Offer handling all reuse the CSV
    adapter, so the manual and browser paths cannot drift apart.
    """
    mapping = adapter.build_mapping(header)
    collected_at = collected_at or dt.datetime.now(dt.timezone.utc).isoformat()
    out = []
    for cells in rows:
        title = adapter.cell(cells, mapping, "raw_title")
        if not title:
            continue
        raw_text = " | ".join(c for c in cells if c)

        price, cur_p = adapter.money(adapter.cell(cells, mapping, "sold_price"))
        total, cur_t = adapter.money(adapter.cell(cells, mapping, "total_price"))
        ship, cur_s = adapter.money(adapter.cell(cells, mapping, "shipping"))
        orig, cur_o = adapter.money(
            adapter.cell(cells, mapping, "displayed_original_price"))

        if price is None and total is not None:
            price = total - ship if ship is not None else None

        currency = (adapter.cell(cells, mapping, "currency") or cur_p or cur_t
                    or cur_s or cur_o or "USD")
        currency = str(currency).strip().upper()[:3] or "USD"

        sale_type = _sale_type(raw_text)
        best_offer = (sale_type == "BEST_OFFER"
                      or adapter.truthy(adapter.cell(cells, mapping, "best_offer")))
        # Never treat a displayed asking price as an accepted Best Offer price.
        if best_offer:
            known = (orig is not None and price is not None
                     and abs(orig - price) > 1e-9)
        else:
            known = True
        if best_offer and not known:
            orig = orig if orig is not None else price
            price = None

        out.append({
            "candidate_item_id": candidate_id,
            "query_tier": level,
            "source": adapter.SOURCE,
            "source_item_id": adapter.cell(cells, mapping, "source_item_id") or "",
            "raw_title": title,
            "sold_price": "" if price is None else f"{price}",
            "shipping": "" if ship is None else f"{ship}",
            "currency": currency,
            "sale_date": adapter.cell(cells, mapping, "sale_date") or "",
            "condition": adapter.cell(cells, mapping, "condition") or "",
            "source_reference": adapter.cell(cells, mapping, "source_reference") or "",
            "notes": f"Product Research query [{level}]: {query}",
            "best_offer_indicator": "true" if best_offer else "false",
            "displayed_original_price": "" if orig is None else f"{orig}",
            "actual_price_known": "true" if known else "false",
            "sale_type": sale_type or "",
            "collected_at": collected_at,
            "raw_text": raw_text,
        })
    return out


# --------------------------------------------------------------------------
# Evidence classification (does NOT modify the matcher)
# --------------------------------------------------------------------------

ACCEPTED = "accepted"
REJECTED = "rejected"
REVIEW_REQUIRED = "review_required"

# A field the comp title simply does not state, versus one that conflicts.
_MISSING_EVIDENCE = re.compile(
    r"\b(print run|card number|PSA grade|qualifier|autograph grade|year) "
    r"None !=|\bsubject None !=|comp never states", re.I)


YEAR_ANYWHERE = re.compile(r"\b((?:19|20)\d{2})\b")


def normalize_comp_title(title):
    """Comp-side only: hoist a year that is not at the front.

    Sold titles are written every which way ("Hasbulla Magomedov ~ 2023 Panini
    ..."). The inventory parser expects a leading year, so this repairs the
    comp title before matching. It never touches PSA's own listings.
    """
    text = (title or "").strip()
    if not text or re.match(r"^\s*(?:18|19|20)\d{2}", text):
        return text
    m = YEAR_ANYWHERE.search(text)
    if not m:
        return text
    # Start the title AT the year and push the leading words to the end, so the
    # brand still follows the year ("2023 Panini Prizm ...") and the parser can
    # read it. The dropped prefix is appended, not deleted - it usually holds
    # the player name, which is still needed as subject evidence.
    prefix = text[:m.start()].strip(" ~-|,")
    rest = text[m.start():].strip()
    return f"{rest} {prefix}".strip()


CARDNO_THEN_SERIAL = re.compile(r"#\s*\d{1,5}\b.*?#(\d{1,4}\s*/\s*\d{1,5})",
                                re.S)
BARE_PRINT_RUN = re.compile(r"(?<![\d/#])\s/\s*(\d{1,5})\b")


def repair_print_run(text, candidate_print_run=None):
    """Make two real-world print-run spellings readable by the parser.

    "... RC Rookie /199 PSA 9"   -> "#/199"  (run stated, copy number omitted)
    "#200 ... #37/99"            -> "37/99"  (the second # marks the serial,
                                              not a second card number)
    Comp titles only; PSA's own listings are never touched.
    """
    out = text or ""
    # A zero-padded "#0NN/M" is ambiguous on its own, but when M equals the
    # candidate's print run it is a serial stamp. Dropping the '#' lets the
    # parser read it as one without weakening the general rule.
    if candidate_print_run:
        pad = re.search(rf"#\s*(0\d{{1,4}})\s*/\s*({int(candidate_print_run)})\b",
                        out)
        if pad and int(pad.group(1)) <= int(pad.group(2)):
            out = out[:pad.start()] + f"{pad.group(1)}/{pad.group(2)}" + out[pad.end():]
    m = CARDNO_THEN_SERIAL.search(out)
    if m:
        out = out[:m.start(1) - 1] + m.group(1) + out[m.end(1):]
    out = BARE_PRINT_RUN.sub(lambda mm: f" #/{mm.group(1)}", out)
    return out


def parallel_conflicts(candidate, raw_title):
    """Do the set+parallel identity tokens actually disagree?

    Used so a genuine parallel conflict outranks a merely-absent print run.
    """
    f = parse.parse_title(repair_print_run(
        normalize_comp_title(clean_listing_title(raw_title)),
        candidate.get("print_run")))["fields"]
    cand = enrich.identity_tokens(
        enrich.canonical_set(candidate["set"], candidate["year"],
                             candidate["manufacturer"]),
        candidate["parallel"], candidate["manufacturer"])
    comp = enrich.identity_tokens(
        enrich.canonical_set(f["set_name"], f["year"], f["manufacturer"]),
        f["parallel"], f["manufacturer"])
    # Only the CANDIDATE's subject is trustworthy noise. The comp's parsed
    # athlete is not: on "#200 Red Ruby Wave Hasbulla Magomedov" the parser puts
    # the parallel into the name slot, and treating that as noise would hide a
    # genuine Ruby Wave conflict.
    subject = set(enrich.name_tokens(candidate.get("subject")))
    cand = {t for t in cand if t not in subject}
    # Synonyms on BOTH sides: an AUTOGRAPHS candidate against an "AUTO" comp is
    # the same concept. Roles decide what may reject - team, city, league,
    # sport, accolade and generic words never do.
    title_tokens = {enrich._syn(t) for t in
                    card_vocab.tokens_of(parse.normalize(raw_title))} - subject
    kind, toks = card_vocab.parallel_conflict(cand, title_tokens,
                                              synonym=enrich._syn)
    # Only a genuine EXTRA parallel is a conflict here. A parallel the comp
    # simply never states is absent evidence, and is left to the caller to
    # record as review_required.
    if kind == card_vocab.EXTRA_PARALLEL:
        return f"parallel/set differs - comp is {toks}, candidate is not"
    return None


def classify_comp(candidate, raw_title):
    """accepted / rejected / review_required for one sold title.

    The existing matcher decides accept-or-not; this only separates "the title
    contradicts the candidate" from "the title never states the field". A
    missing /199 is not evidence of a different print run, but it is not proof
    of the same one either - so it is held for review, never valued.
    """
    title = repair_print_run(normalize_comp_title(clean_listing_title(raw_title)),
                             candidate.get("print_run"))
    ok, reason, _norm = mc.match(candidate, title)
    if ok:
        return ACCEPTED, None
    if reason and _MISSING_EVIDENCE.search(reason):
        # A real parallel conflict outranks a field the title simply omits.
        conflict = parallel_conflicts(candidate, title)
        if conflict:
            return REJECTED, conflict
        return REVIEW_REQUIRED, f"{reason} (field absent from title, not a conflict)"
    return REJECTED, reason
