"""Manual sold-comps workflow. Makes ZERO network calls, ever.

eBay's Marketplace Insights API (the only official automated sold-transaction
source) returned invalid_scope for this application, so sold data is retrieved
by hand from eBay Product Research. This module generates the exact searches to
run, imports what comes back, and applies the already-approved identity rules.

  python manual_comps.py --research      # write sold_comps_research.csv
  python manual_comps.py --template      # write a blank import template
  python manual_comps.py --import FILE   # load and match a filled-in CSV
  python manual_comps.py --report        # valuation summary from the DB
"""

import argparse
import collections
import csv
import datetime as dt
import json
import os
import re
import statistics
import sys

import card_vocab
import db
import ebay_product_research_import
import enrich
import parse

RESEARCH_CSV = "sold_comps_research.csv"
TEMPLATE_CSV = "sold_comps_import_template.csv"
CANDIDATES = "pilot_candidates.json"
SOURCE = "EBAY_PRODUCT_RESEARCH"

REQUIRED_COLS = ["candidate_item_id", "query_tier", "source", "source_item_id",
                 "raw_title", "sold_price", "shipping", "currency", "sale_date",
                 "condition", "source_reference"]
OPTIONAL_COLS = ["notes", "best_offer_indicator", "displayed_original_price",
                 "actual_price_known"]

# Currencies observed during the current import, for the summary line.
CURRENCIES = set()

# The five pilot candidates, chosen to span the identity shapes that matter.
PILOT = {
    "v1|298544784209|0": "modern_numbered",
    "v1|298544831895|0": "modern_parallel",
    "v1|307073949024|0": "vintage",
    "v1|297711604305|0": "autograph",
    "v1|117330553310|0": "base",
}

# Titles that can never be the same product as a graded single card.
DISQUALIFY = [
    (r"\b(LOT|LOTS)\b|\bBUNDLE\b|\bSET OF\b|\(\s*\d+\s*(CARDS?)?\s*\)|\bX\s*\d+\b",
     "lot or multi-card listing"),
    (r"\bREPRINT|\bRP\b|\bREPRO\b|\bFACSIMILE\b", "reprint"),
    (r"\bDIGITAL\b|\bNFT\b|\bTOPPS BUNT\b", "digital card"),
    (r"\bPACK\b|\bBOX\b|\bCASE\b|\bSEALED\b|\bBLASTER\b|\bHOBBY BOX\b", "pack/box/case"),
]


def load_candidates():
    if not os.path.exists(CANDIDATES):
        sys.exit(f"error: {CANDIDATES} missing - run export_pilot.py first")
    with open(CANDIDATES) as fh:
        return {c["item_id"]: c for c in json.load(fh)}


# --------------------------------------------------------------------------
# Query generation
# --------------------------------------------------------------------------

def query_terms(c, tier):
    """Search terms from the effective identity. Prices are never included.

    Tiers vary the SURFACE FORM only - punctuation and spelling. No tier drops
    a material discriminator: a query that omits the maker, the parallel or the
    print run is searching a different card, and whatever it returns cannot be
    a comp for this one. Broader recall is the acceptance layer's problem, not
    the query's.

    Previously NORMAL dropped manufacturer, set and print run, which turned
    "2011 Bowman Chrome #151 Gold Refractor /50" into "2011 #151 Gold
    Refractor" - a different search entirely.
    """
    alias = tier != "STRICT"
    bits = [str(c["year"]), c["subject"] or ""]
    if c["manufacturer"]:
        bits.append(c["manufacturer"])
    if c["set"] and c["set"] != "BASE":
        bits.append(c["set"])
    if c["card_number"]:
        # "#BCP99" and "BCP99" are the same card; sellers write both.
        bits.append(str(c["card_number"]) if alias else f"#{c['card_number']}")
    if c["parallel"]:
        bits.append(c["parallel"])
    if c["print_run"]:
        bits.append(f"/{c['print_run']}")
    if c["is_auto"]:
        bits.append("auto")
    if c["auto_grade"]:
        bits.append(f"auto {c['auto_grade']}")
    bits.append(f"PSA {c['psa_grade']}")
    if c["qualifier"]:
        bits.append(c["qualifier"])
    return re.sub(r"\s+", " ", " ".join(b for b in bits if b)).strip()


def required_discriminators(c):
    """Material identity facts a query must keep -> acceptable surface forms.

    Only facts KNOWN for this candidate are required; an absent parallel or
    print run cannot be dropped because it was never there.
    """
    req = {}
    if c["year"]:
        req["year"] = [str(c["year"])]
    if c.get("subject"):
        # A query may abbreviate a first name; the surname must survive.
        req["subject"] = [(c["subject"] or "").split()[-1]]
    if c["card_number"]:
        req["card_number"] = [f"#{c['card_number']}", str(c["card_number"])]
    if c["psa_grade"]:
        req["grade"] = [f"PSA {c['psa_grade']}"]
    if c["manufacturer"] or (c["set"] and c["set"] != "BASE"):
        forms = []
        if c["manufacturer"]:
            forms.append(c["manufacturer"])
            forms.extend(sorted(enrich._mfr_tokens(c["manufacturer"])))
        if c["set"] and c["set"] != "BASE":
            forms.append(c["set"])
        req["manufacturer_or_set"] = forms
    if c["parallel"]:
        req["parallel"] = [c["parallel"]]
    if c["print_run"]:
        req["print_run"] = [f"/{c['print_run']}", str(c["print_run"])]
    if c["is_auto"]:
        req["auto_status"] = ["auto"]
    if c["auto_grade"]:
        req["auto_grade"] = [f"auto {c['auto_grade']}"]
    if c["qualifier"]:
        req["qualifier"] = [c["qualifier"]]
    return req


def query_violations(c, query):
    """Material discriminators the query fails to state. Empty means safe."""
    text = parse.normalize(query or "")
    missing = []
    for field, forms in required_discriminators(c).items():
        if not any(parse.normalize(str(f)) in text for f in forms if f):
            missing.append(field)
    return missing


def relaxed_allowed(c):
    """RELAXED may only drop the parallel, and only when there isn't one."""
    if c["print_run"] or c["is_auto"] or c["auto_grade"] or c["qualifier"]:
        return False
    return not c["parallel"] or c["parallel"] == "BASE"


def must_match(c):
    fields = ["subject", "year", "card_number", "psa_grade", "parallel",
              "autograph_status"]
    if c["print_run"]:
        fields.append("print_run")
    if c["qualifier"]:
        fields.append("qualifier")
    if c["auto_grade"]:
        fields.append("autograph_grade")
    return fields


def never_relax(c):
    fields = ["card_number", "psa_grade"]
    if c["print_run"]:
        fields.append("print_run")
    if c["parallel"] and c["parallel"] != "BASE":
        fields.append("parallel")
    if c["is_auto"] or c["auto_grade"]:
        fields.append("autograph_status")
    if c["qualifier"]:
        fields.append("qualifier")
    return fields


def write_research(cands):
    rows = []
    for iid, kind in PILOT.items():
        c = cands.get(iid)
        if c is None:
            sys.exit(f"error: pilot candidate {iid} not in {CANDIDATES}")
        tiers = ["STRICT", "NORMAL"] + (["RELAXED"] if relaxed_allowed(c) else [])
        seen_q = set()
        for tier in tiers:
            q = query_terms(c, tier)
            if q in seen_q:      # RELAXED == NORMAL when there is no parallel
                continue
            seen_q.add(q)
            note = {"STRICT": "narrowest search; expect few but exact results",
                    "NORMAL": "same material identity, alternate surface form "
                              "(card number without '#')",
                    "RELAXED": "allowed only because this card has no parallel, "
                               "no serial, no autograph and no qualifier",
                    }[tier]
            rows.append({
                "candidate_item_id": iid,
                "candidate_title": c["title"],
                "candidate_asking_price": c["asking_price"],
                "effective_slab_key": c["effective_slab_key"],
                "candidate_type": kind,
                "query_tier": tier,
                "search_query": q,
                "year": c["year"], "subject": c["subject"],
                "manufacturer": c["manufacturer"], "set_name": c["set"],
                "card_number": c["card_number"], "parallel": c["parallel"] or "",
                "serial_number": c["serial_num"] or "",
                "print_run": c["print_run"] or "",
                "psa_grade": c["psa_grade"], "qualifier": c["qualifier"] or "",
                "autograph_status": "yes" if c["is_auto"] else "no",
                "autograph_grade": c["auto_grade"] or "",
                "fields_that_must_match": "|".join(must_match(c)),
                "fields_that_must_not_be_relaxed": "|".join(never_relax(c)),
                "notes": note,
            })
    with open(RESEARCH_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return rows


def write_template():
    with open(TEMPLATE_CSV, "w", newline="") as fh:
        csv.writer(fh).writerow(REQUIRED_COLS + OPTIONAL_COLS)
    return REQUIRED_COLS + OPTIONAL_COLS


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def num(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(re.sub(r"[^\d.\-]", "", str(value)))
    except ValueError:
        return None


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def parse_date(value):
    v = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y",
                "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def match(candidate, raw_title):
    """Apply the approved material identity rules. Returns (ok, reason, norm)."""
    text = parse.normalize(raw_title or "")
    for pattern, why in DISQUALIFY:
        if re.search(pattern, text):
            return False, why, {}

    rival = parse.rival_grader(text)
    if rival:
        return False, f"graded by {rival}, not PSA", {}
    # Two graded cards in one title is a lot, whatever the punctuation.
    if len(re.findall(r"PSA\s*(?:10|[1-9](?:\.5)?)\b", text)) > 1:
        return False, "lot - title describes more than one graded card", {}

    p = parse.parse_title(raw_title or "")
    f = p["fields"]
    norm = {"year": f["year"], "subject": f["athlete"],
            "card_number": f["card_number"], "parallel": f["parallel"],
            "grade": f["grade_value"], "qualifier": f["grade_qualifier"],
            "auto": f["is_auto"], "print_run": f["print_run"]}

    if f["grade_type"] is None:
        return False, "raw/ungraded - no PSA grade in title", norm
    if f["grade_type"] != candidate["grade_type"]:
        return False, f"grade type {f['grade_type']} != {candidate['grade_type']}", norm
    if enrich.norm(f["grade_value"]) != enrich.norm(candidate["psa_grade"]):
        return False, f"PSA grade {f['grade_value']} != {candidate['psa_grade']}", norm
    if enrich.norm(f["grade_qualifier"]) != enrich.norm(candidate["qualifier"]):
        return False, (f"qualifier {f['grade_qualifier']} != "
                       f"{candidate['qualifier']}"), norm
    if enrich.norm(f["auto_grade"]) != enrich.norm(candidate["auto_grade"]):
        return False, (f"autograph grade {f['auto_grade']} != "
                       f"{candidate['auto_grade']}"), norm
    if bool(f["is_auto"]) != bool(candidate["is_auto"]):
        return False, ("autograph status differs (comp="
                       f"{bool(f['is_auto'])}, candidate={bool(candidate['is_auto'])})"), norm
    if f["year"] != candidate["year"]:
        return False, f"year {f['year']} != {candidate['year']}", norm
    if enrich.norm(f["card_number"]) != enrich.norm(candidate["card_number"]):
        return False, f"card number {f['card_number']} != {candidate['card_number']}", norm
    if (f["print_run"] or None) != (candidate["print_run"] or None):
        return False, f"print run {f['print_run']} != {candidate['print_run']}", norm

    subject_a = enrich.norm(f["athlete"])
    subject_b = enrich.norm(candidate["subject"])
    # Titles do not agree on word order: "UFC - Hasbulla Magomedov #200 Red
    # Prizm" puts the player BEFORE the card number, so the parsed athlete slot
    # holds the parallel. The name is still stated, so accept it as evidence
    # wherever it appears in the title.
    subject_in_title = bool(subject_b) and subject_b in enrich.norm(text)
    surname = (candidate["subject"] or "").split()[-1].upper() \
        if candidate.get("subject") else ""
    surname_in_title = bool(surname) and len(surname) > 2 and surname in text
    if subject_a and subject_b and subject_a != subject_b \
            and not (subject_a in subject_b or subject_b in subject_a) \
            and not subject_in_title and not surname_in_title:
        return False, f"subject {f['athlete']} != {candidate['subject']}", norm
    # A title that never names anyone cannot prove it is this player's card,
    # even when the card number happens to line up.
    if subject_b and not subject_a and not subject_in_title \
            and not surname_in_title:
        return False, f"subject None != {candidate['subject']}", norm

    # --- manufacturer / brand -----------------------------------------------
    # Kept deliberately separate from the set/product-line comparison below: a
    # Fleer card and a Hoops card can share year, number, player and grade and
    # still be different cards. Only a positive conflict rejects - when either
    # side names no maker at all, this stays silent and the existing
    # missing-evidence behaviour applies.
    cand_brand = parse.canonical_brand(candidate.get("manufacturer"))
    comp_brands = parse.brands_in(text)
    if cand_brand and comp_brands and cand_brand not in comp_brands:
        return False, (f"manufacturer/brand conflict - comp is "
                       f"{sorted(comp_brands)}, candidate is {cand_brand}"), norm

    # Strip the player name and pure descriptors before comparing identity.
    # A title that reads "... Hasbulla Magomedov #200 Red Prizm" leaks the name
    # into the set span; a name is never a parallel, and neither is "RC".
    # Only the CANDIDATE's subject is reliable noise. The comp's parsed athlete
    # is not: when the player precedes the card number the parser puts the
    # PARALLEL in the name slot, and stripping that would erase real identity.
    _noise = set(enrich.name_tokens(candidate.get("subject")))

    def _clean(tokens):
        return tuple(t for t in tokens if t not in _noise)

    cand_ident = enrich.identity_tokens(
        enrich.canonical_set(candidate["set"], candidate["year"],
                             candidate["manufacturer"]),
        candidate["parallel"], candidate["manufacturer"])
    comp_ident = enrich.identity_tokens(
        enrich.canonical_set(f["set_name"], f["year"], f["manufacturer"]),
        f["parallel"], f["manufacturer"])
    # Compare by SEMANTIC ROLE, not raw token equality. Team, city, league,
    # sport, accolade and generic words are context a seller may add freely;
    # only a true parallel conflict identifies a different card.
    cand_ident, comp_ident = _clean(cand_ident), _clean(comp_ident)
    title_tokens = (card_vocab.tokens_of(text) | set(comp_ident)) - _noise
    kind, toks = card_vocab.parallel_conflict(
        cand_ident, title_tokens, synonym=enrich._syn)
    if kind == card_vocab.EXTRA_PARALLEL:
        return False, f"parallel/set differs - comp is {toks}, candidate is not", norm
    if kind == card_vocab.MISSING_PARALLEL:
        return False, f"parallel/set differs - comp never states {toks}", norm

    return True, None, norm


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def import_csv(conn, path):
    if not os.path.exists(path):
        sys.exit(f"error: {path} not found")
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLS if c not in (reader.fieldnames or [])]
        if missing:
            sys.exit(f"error: import file is missing required columns: {missing}")
        rows = list(reader)
    return import_rows(conn, rows)


def attribute(cands, raw_title):
    """Which pilot candidate is this row a comp for?

    Used only by the eBay adapter path, where the export carries no candidate
    column. A row is attributed only when exactly one candidate accepts it -
    ambiguity is recorded, never guessed.
    """
    hits = [cid for cid, c in cands.items() if match(c, raw_title)[0]]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, f"row matches {len(hits)} candidates ambiguously: {hits}"

    # No candidate accepts it. Fall back to "same card, ignoring grade and
    # variety" so the row still lands on the right candidate and receives a
    # precise rejection reason instead of a vague unattributed note.
    f = parse.parse_title(raw_title or "")["fields"]
    near = []
    for cid, c in cands.items():
        if f["year"] != c["year"]:
            continue
        if enrich.norm(f["card_number"]) != enrich.norm(c["card_number"]):
            continue
        a, b = enrich.norm(f["athlete"]), enrich.norm(c["subject"])
        if a and b and not (a in b or b in a):
            continue
        near.append(cid)
    if len(near) == 1:
        return near[0], None
    return None, "row matches no pilot candidate"


def _record_unattributed(conn, row, reason):
    """Keep an unattributable row for audit; it can never be valued."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        conn.execute(
            """INSERT INTO sold_comps (candidate_item_id, source, source_item_id,
               raw_title, currency, accepted, rejection_reason, raw_row,
               imported_at) VALUES (?,?,?,?,?,0,?,?,?)""",
            ("<unattributed>", row.get("source") or SOURCE,
             row.get("source_item_id"), row.get("raw_title"),
             (row.get("currency") or "").upper(), reason, json.dumps(row), now))
    except Exception as exc:
        if "UNIQUE" not in str(exc):
            raise


def import_rows(conn, rows, attribute_by_title=False):
    cands = load_candidates()
    CURRENCIES.clear()
    stats = collections.Counter()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for row in rows:
        stats["rows"] += 1
        cid = (row.get("candidate_item_id") or "").strip()
        attribution_error = None
        if not cid and attribute_by_title:
            cid, attribution_error = attribute(cands, row.get("raw_title"))
        cand = cands.get(cid) if cid else None
        if cand is None:
            stats["unattributed"] += 1
            stats["currencies"] = stats.get("currencies")
            _record_unattributed(conn, row, attribution_error
                                 or "unknown candidate_item_id")
            continue

        price, ship = num(row.get("sold_price")), num(row.get("shipping"))
        total = None if price is None else price + (ship or 0.0)
        best_offer = truthy(row.get("best_offer_indicator"))
        known_raw = row.get("actual_price_known")
        known = True if known_raw in (None, "") else truthy(known_raw)

        ok, reason, norm = match(cand, row.get("raw_title"))
        counted_bo = False
        if ok and best_offer and not known:
            ok, reason = False, ("Best Offer accepted price unknown - only the "
                                 "displayed asking price is available")
            stats["best_offer_unknown"] += 1
            counted_bo = True

        currency = (row.get("currency") or "USD").strip().upper()
        fx_rate = 1.0 if currency == "USD" else None
        stats["accepted" if ok else "rejected"] += 1
        CURRENCIES.add(currency)
        try:
            conn.execute("""INSERT INTO sold_comps
                (candidate_item_id, query_tier, source, source_item_id, raw_title,
                 sold_price, shipping, total_price, currency, fx_rate, fx_date,
                 converted_price, converted_shipping, converted_total, sale_date,
                 condition, source_reference, best_offer, actual_price_known,
                 displayed_original_price, accepted, rejection_reason,
                 match_confidence, norm_year, norm_subject, norm_card_number,
                 norm_parallel, norm_grade, norm_qualifier, norm_auto,
                 norm_print_run, raw_row, sale_type, collected_at, raw_text,
                 imported_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, row.get("query_tier"), row.get("source") or SOURCE,
                 row.get("source_item_id"), row.get("raw_title"), price, ship,
                 total, currency, fx_rate, now if fx_rate else None,
                 price if fx_rate == 1.0 else None,
                 ship if fx_rate == 1.0 else None,
                 total if fx_rate == 1.0 else None,
                 parse_date(row.get("sale_date")), row.get("condition"),
                 row.get("source_reference"), int(best_offer), int(known),
                 num(row.get("displayed_original_price")), int(ok), reason,
                 "EXACT" if ok else None, norm.get("year"), norm.get("subject"),
                 norm.get("card_number"), norm.get("parallel"), norm.get("grade"),
                 norm.get("qualifier"),
                 int(bool(norm.get("auto"))) if norm else None,
                 norm.get("print_run"), json.dumps(row), row.get("sale_type"),
                 row.get("collected_at"), row.get("raw_text"), now))
        except Exception as exc:            # UNIQUE -> already imported
            if "UNIQUE" in str(exc):
                stats["duplicate"] += 1
                stats["accepted" if ok else "rejected"] -= 1
                if counted_bo:
                    stats["best_offer_unknown"] -= 1
            else:
                raise
    conn.commit()
    return stats


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def confidence(n):
    return "HIGH" if n >= 5 else "MEDIUM" if n >= 3 else "LOW" if n >= 1 else "NONE"


def report(conn):
    cands = load_candidates()
    print("=" * 78)
    print("  MANUAL SOLD-COMPS PILOT REPORT")
    print("=" * 78)
    total_rows = conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0]
    if not total_rows:
        print("\n  No sold comps imported yet.")
        print("  Run the searches in sold_comps_research.csv via eBay Product")
        print("  Research, fill in sold_comps_import_template.csv, then:")
        print("      python manual_comps.py --import sold_comps_import.csv")
        print("=" * 78)
        return

    for cid, kind in PILOT.items():
        c = cands.get(cid)
        rows = conn.execute("SELECT * FROM sold_comps WHERE candidate_item_id=?",
                            (cid,)).fetchall()
        acc = [r for r in rows if r["accepted"]]
        rej = [r for r in rows if not r["accepted"]]
        print(f"\n  {c['title'][:70]}")
        print(f"    identity   : {c['effective_identity']}")
        print(f"    PSA ask    : ${c['asking_price']:,.2f} "
              f"(+ ${c['shipping'] or 0:,.2f} shipping)")
        print(f"    raw rows   : {len(rows)}   accepted: {len(acc)}   "
              f"rejected: {len(rej)}")
        if rej:
            for reason, n in collections.Counter(
                    r["rejection_reason"] for r in rej).most_common():
                print(f"      rejected {n:3}x  {reason}")
        bo = [r for r in rej if r["best_offer"] and not r["actual_price_known"]]
        print(f"    excluded unknown Best Offer prices: {len(bo)}")
        if not acc:
            print(f"    confidence : NONE (no valid comps)")
            continue
        prices = [r["sold_price"] for r in acc if r["sold_price"] is not None]
        totals = [r["total_price"] for r in acc if r["total_price"] is not None]
        dates = sorted(r["sale_date"] for r in acc if r["sale_date"])
        print(f"    sale dates : {dates[0]} .. {dates[-1]}" if dates
              else "    sale dates : none recorded")
        print(f"    item price : median ${statistics.median(prices):,.2f}  "
              f"min ${min(prices):,.2f}  max ${max(prices):,.2f}")
        if totals:
            print(f"    total      : median ${statistics.median(totals):,.2f}  "
                  f"mean ${statistics.mean(totals):,.2f}  "
                  f"min ${min(totals):,.2f}  max ${max(totals):,.2f}")
        print(f"    most recent exact sale: {dates[-1] if dates else 'unknown'}")
        print(f"    confidence : {confidence(len(acc))} ({len(acc)} exact comps)")
    print("\n  No BUY/WATCH/PASS is assigned at this stage.")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--research", action="store_true")
    ap.add_argument("--template", action="store_true")
    ap.add_argument("--import", dest="import_path")
    ap.add_argument("--import-ebay", dest="import_ebay",
                    help="raw CSV exported from eBay Product Research")
    ap.add_argument("--candidate", help="attribute every row to this "
                    "candidate item_id instead of matching by title")
    ap.add_argument("--tier", default="EBAY_EXPORT")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    conn = db.connect(args.db)
    enrich.load_surnames(conn)

    if args.research:
        rows = write_research(load_candidates())
        print(f"wrote {RESEARCH_CSV}: {len(rows)} query rows for "
              f"{len(PILOT)} candidates")
        for r in rows:
            print(f"  [{r['query_tier']:7}] {r['search_query']}")
    if args.template:
        cols = write_template()
        print(f"wrote {TEMPLATE_CSV} with {len(cols)} columns (header only)")
    if args.import_path:
        stats = import_csv(conn, args.import_path)
        print_import_summary(stats)
    if args.import_ebay:
        try:
            rows, mapping, header = ebay_product_research_import.translate(
                args.import_ebay, default_tier=args.tier)
        except ebay_product_research_import.AdapterError as exc:
            sys.exit(f"error: {exc}")
        if args.candidate:
            for r in rows:
                r["candidate_item_id"] = args.candidate
        print(f"eBay Product Research export: {len(rows)} data rows")
        print(f"  columns recognized: {sorted(mapping)}")
        unmapped = [h for h in header if ebay_product_research_import
                    .normalize_header(h) not in
                    {a for al in ebay_product_research_import.COLUMN_ALIASES.values()
                     for a in al}]
        if unmapped:
            print(f"  columns ignored   : {unmapped}")
        stats = import_rows(conn, rows, attribute_by_title=not args.candidate)
        print_import_summary(stats)
    if args.report:
        report(conn)
    if not any((args.research, args.template, args.import_path,
                args.import_ebay, args.report)):
        ap.print_help()


def print_import_summary(stats):
    print("  " + "-" * 52)
    print(f"  Rows read                      : {stats['rows']}")
    print(f"  Rows accepted                  : {stats['accepted']}")
    print(f"  Rows rejected                  : {stats['rejected']}")
    print(f"  Duplicate rows skipped         : {stats['duplicate']}")
    print(f"  Unknown Best Offer price       : {stats['best_offer_unknown']}")
    print(f"  Unattributed rows (kept, unusable): {stats['unattributed']}")
    print(f"  Currencies seen                : "
          f"{', '.join(sorted(CURRENCIES)) or 'none'}")
    print("  " + "-" * 52)


if __name__ == "__main__":
    main()
