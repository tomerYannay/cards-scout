"""Step 2: normalize active PSA listings into the cards table.

Reads `listings` (never modifies it), writes `cards` and `parse_issues`, then
prints a validation report.

Usage:  python parse_listings.py [--db cards.db] [--samples 4]
"""

import argparse
import collections
import datetime as dt

import db
import parse

FIELDS = [
    ("sport", "sport_conf"), ("year", "year_conf"),
    ("manufacturer", "manufacturer_conf"), ("set_name", "set_conf"),
    ("insert_name", "insert_conf"), ("parallel", "parallel_conf"),
    ("athlete", "athlete_conf"), ("card_number", "card_number_conf"),
    ("grade_value", "grade_conf"),
]


def classify(title):
    """Decide whether a listing belongs to the PSA-only population."""
    text = parse.normalize(title)
    rival = parse.rival_grader(text)
    has_psa = "PSA" in text
    if rival and not has_psa:
        return "excluded", rival
    if rival and has_psa:
        # Both graders named: identity of the slab is ambiguous, so it is
        # excluded rather than risk a wrong comparison later.
        return "excluded", f"AMBIGUOUS PSA+{rival}"
    if not has_psa:
        return "excluded", "NO GRADER"
    return "psa", None


def build(conn, parsed_at, run_id=None):
    """Parse active listings into `cards`.

    `run_id` scopes the parse to one discovery run. The parser has changed
    since the existing rows were written - qualifiers, tolerant titles - so an
    unscoped re-parse would silently rewrite identities the researched cohort
    and the 93-candidate baseline already depend on.
    """
    if run_id:
        rows = conn.execute(
            "SELECT item_id, title FROM listings "
            "WHERE active = 1 AND discovery_run_id = ?", (run_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT item_id, title FROM listings WHERE active = 1"
        ).fetchall()

    card_rows, issue_rows = [], []
    counts = collections.Counter()
    exclusions = collections.Counter()

    for r in rows:
        item_id, title = r["item_id"], r["title"]
        bucket, reason = classify(title)
        if bucket == "excluded":
            exclusions[reason] += 1
            counts["excluded"] += 1
            # Listing stays in `listings`; the exclusion is recorded so it can
            # be audited from the database, not just this report.
            issue_rows.append((item_id, "excluded", reason, title, parsed_at))
            continue

        counts["psa_only"] += 1
        result = parse.parse_title(title)
        f, conf, issues = result["fields"], result["conf"], result["issues"]
        status = parse.parse_status(conf, issues)
        ident = parse.identity_confidence(conf)
        card_key, slab_key = parse.make_keys(f)
        counts[status] += 1

        card_rows.append({
            "item_id": item_id, "sport": f["sport"], "sport_conf": conf["sport"],
            "year": f["year"], "year_raw": f["year_raw"], "year_conf": conf["year"],
            "manufacturer": f["manufacturer"],
            "manufacturer_conf": conf["manufacturer"],
            "set_name": f["set_name"], "set_conf": conf["set_name"],
            "insert_name": f["insert_name"], "insert_conf": conf["insert_name"],
            "parallel": f["parallel"], "parallel_conf": conf["parallel"],
            "athlete": f["athlete"], "athlete_conf": conf["athlete"],
            "card_number": f["card_number"],
            "card_number_conf": conf["card_number"],
            "is_rookie": f["is_rookie"], "is_auto": f["is_auto"],
            "is_relic": f["is_relic"], "serial_num": f["serial_num"],
            "print_run": f["print_run"], "grade_type": f["grade_type"],
            "grade_value": f["grade_value"],
            "grade_qualifier": f["grade_qualifier"],
            "auto_grade": f["auto_grade"], "grade_raw": f["grade_raw"],
            "grade_conf": conf["grade"],
            "cert_number": None,  # Tier B only - not fetched in Step 2
            "card_key": card_key, "slab_key": slab_key, "identity_conf": ident,
            "parse_status": status, "truncation_risk": f["truncation_risk"],
            "parsed_at": parsed_at,
        })
        for field, why in issues:
            issue_rows.append((item_id, field, why, title, parsed_at))

    cols = list(card_rows[0].keys()) if card_rows else []
    if card_rows:
        conn.executemany(
            f"INSERT OR REPLACE INTO cards ({','.join(cols)}) "
            f"VALUES ({','.join(':' + c for c in cols)})",
            card_rows,
        )
    conn.executemany(
        "INSERT INTO parse_issues (item_id, field, reason, title, created_at) "
        "VALUES (?,?,?,?,?)",
        issue_rows,
    )
    conn.commit()
    return len(rows), counts, exclusions


def show_samples(conn, status, limit, label):
    rows = conn.execute(
        """SELECT c.*, l.title FROM cards c JOIN listings l USING (item_id)
           WHERE c.parse_status = ? LIMIT ?""",
        (status, limit),
    ).fetchall()
    print(f"\n  {label}")
    if not rows:
        print("    (none)")
    for r in rows:
        print(f"    {r['title'][:76]}")
        print(f"      year={r['year']} mfr={r['manufacturer']} set={r['set_name']!r}")
        print(f"      parallel={r['parallel']!r} #={r['card_number']!r} "
              f"athlete={r['athlete']!r}")
        print(f"      grade={r['grade_type']}:{r['grade_value']} sport={r['sport']} "
              f"identity={r['identity_conf']} slab={r['slab_key']}")


def report(conn, examined, counts, exclusions, samples):
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    psa = counts["psa_only"]

    print("\n" + "=" * 64)
    print("STEP 2 VALIDATION REPORT")
    print("=" * 64)
    print(f"  Active listings examined       : {examined}")
    print(f"  PSA-only listings parsed       : {psa}")
    print(f"  Non-PSA exclusions             : {counts['excluded']}")
    for reason, n in exclusions.most_common():
        print(f"      {reason:24} {n}")

    print("\n  Parse status:")
    for status in ("ok", "partial", "failed"):
        n = counts[status]
        print(f"      {status:8} {n:7} ({100*n/max(psa,1):5.1f}%)")

    print("\n  Field coverage (non-null) and confidence distribution:")
    for field, conf_col in FIELDS:
        filled = q(f"SELECT COUNT(*) FROM cards WHERE {field} IS NOT NULL")
        dist = collections.Counter(
            r[0] for r in conn.execute(f"SELECT {conf_col} FROM cards")
        )
        shown = " ".join(
            f"{lvl[:4]}={dist.get(lvl,0)}"
            for lvl in (parse.HIGH, parse.MEDIUM, parse.LOW, parse.MISSING)
            if dist.get(lvl)
        )
        print(f"      {field:14} {filled:7} ({100*filled/max(psa,1):5.1f}%)  {shown}")

    print("\n  Identity confidence (gate for comps eligibility):")
    for lvl in (parse.HIGH, parse.MEDIUM, parse.LOW, parse.MISSING):
        n = q("SELECT COUNT(*) FROM cards WHERE identity_conf = ?", lvl)
        if n:
            print(f"      {lvl:8} {n:7} ({100*n/max(psa,1):5.1f}%)")

    print(f"\n  Truncation risk (title >= {parse.TITLE_LIMIT} chars): "
          f"{q('SELECT COUNT(*) FROM cards WHERE truncation_risk = 1')}")
    auth = q("SELECT COUNT(*) FROM cards WHERE grade_type = 'AUTHENTIC'")
    print(f"  PSA AUTHENTIC (non-numeric grade): {auth}"
          "   [held out of anomaly ranking]")
    print(f"  Listings with a PSA qualifier    : "
          f"{q('SELECT COUNT(*) FROM cards WHERE grade_qualifier IS NOT NULL')}")
    for r in conn.execute("""SELECT grade_qualifier g, COUNT(*) n FROM cards
                             WHERE grade_qualifier IS NOT NULL
                             GROUP BY 1 ORDER BY n DESC"""):
        print(f"      {r['g']:6} {r['n']}")
    print(f"  Listings with an autograph grade : "
          f"{q('SELECT COUNT(*) FROM cards WHERE auto_grade IS NOT NULL')}")
    for r in conn.execute("""SELECT auto_grade g, COUNT(*) n FROM cards
                             WHERE auto_grade IS NOT NULL
                             GROUP BY 1 ORDER BY n DESC LIMIT 6"""):
        print(f"      AUTO {r['g']:6} {r['n']}")

    print("\n  Top parse failure reasons:")
    for r in conn.execute(
        """SELECT field, reason, COUNT(*) n FROM parse_issues
           WHERE field <> 'excluded'
           GROUP BY field, reason ORDER BY n DESC LIMIT 10"""
    ):
        print(f"      {r['n']:7}  {r['field']:12} {r['reason'][:58]}")

    groups = q("SELECT COUNT(*) FROM (SELECT slab_key FROM cards GROUP BY slab_key)")
    multi = q("""SELECT COUNT(*) FROM (SELECT slab_key FROM cards
                 GROUP BY slab_key HAVING COUNT(*) > 1)""")
    in_multi = q("""SELECT COALESCE(SUM(n),0) FROM (SELECT COUNT(*) n FROM cards
                    GROUP BY slab_key HAVING COUNT(*) > 1)""")
    print(f"\n  slab_key groups (all parses)   : {groups}")
    print(f"  Groups with >1 active listing  : {multi} "
          f"(covering {in_multi} listings)")

    # What Step 3 can actually act on: identity established, numeric grade only
    # (PSA AUTHENTIC is held out by decision until exact-match data exists).
    ELIGIBLE = ("identity_conf IN ('MEDIUM','HIGH') AND grade_type = 'NUMERIC'")
    eligible = q(f"SELECT COUNT(*) FROM cards WHERE {ELIGIBLE}")
    e_groups = q(f"""SELECT COUNT(*) FROM (SELECT slab_key FROM cards
                     WHERE {ELIGIBLE} GROUP BY slab_key)""")
    e_multi = q(f"""SELECT COUNT(*) FROM (SELECT slab_key FROM cards
                    WHERE {ELIGIBLE} GROUP BY slab_key HAVING COUNT(*) > 1)""")
    e_in_multi = q(f"""SELECT COALESCE(SUM(n),0) FROM (SELECT COUNT(*) n FROM cards
                       WHERE {ELIGIBLE} GROUP BY slab_key HAVING COUNT(*) > 1)""")
    print(f"\n  Comps-eligible listings        : {eligible} "
          f"({100*eligible/max(psa,1):.1f}% of PSA-only)")
    print(f"  Eligible slab_key groups       : {e_groups}")
    print(f"  Eligible groups with >1 listing: {e_multi} "
          f"(covering {e_in_multi} listings)")
    print("  ^ this is the substrate for the intra-store anomaly pre-filter")

    show_samples(conn, "ok", samples, "Representative SUCCESSFUL parses:")
    show_samples(conn, "partial", samples, "Representative PARTIAL parses:")
    show_samples(conn, "failed", samples, "Representative FAILED parses:")

    print("\n  Representative AMBIGUOUS parses (quarantined, not guessed):")
    rows = conn.execute(
        """SELECT l.title, i.field, i.reason FROM parse_issues i
           JOIN listings l USING (item_id)
           WHERE i.field IN ('parallel','card_number','athlete') LIMIT ?""",
        (samples,),
    ).fetchall()
    for r in rows:
        print(f"    {r['title'][:74]}")
        print(f"      -> {r['field']}: {r['reason'][:66]}")
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--samples", type=int, default=3)
    args = ap.parse_args()

    conn = db.connect(args.db)
    db.reset_parse_tables(conn)
    parsed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    print("Parsing active PSA listings...")
    examined, counts, exclusions = build(conn, parsed_at)
    report(conn, examined, counts, exclusions, args.samples)


if __name__ == "__main__":
    main()
