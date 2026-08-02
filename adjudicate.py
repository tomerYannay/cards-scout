"""Stage 1 adjudication: re-score all cached Tier B rows. ZERO Browse calls.

Builds an effective identity per listing (Tier A + Tier B authoritative
overrides), stores effective keys separately from the raw Tier A keys, and
reports what each re-key does to the original anomaly group.

Usage:  python adjudicate.py [--db cards.db] [--quarantined-only]
"""

import argparse
import collections
import json

import db
import enrich
import parse


def load(conn):
    return conn.execute("""
        SELECT t.item_id, t.original_verdict, t.aspects_json, t.verdict AS prev,
               c.*, l.title
        FROM tierb t JOIN cards c USING (item_id) JOIN listings l USING (item_id)
        WHERE t.http_status = 200
    """).fetchall()


def tier_b_dict(aspects_json):
    aspects = json.loads(aspects_json or "{}")
    upper = {k.upper(): v for k, v in aspects.items()}
    b = {"aspects": aspects}
    for field, aliases in enrich.ASPECT_MAP.items():
        b[field] = next((upper[a] for a in aliases if a in upper), None)
    for field in enrich.NOT_PROVIDED:
        b[field] = None
    return b


def group_sizes(conn):
    return {r["slab_key"]: r["n"] for r in conn.execute(
        """SELECT slab_key, COUNT(*) n FROM cards
           WHERE identity_conf IN ('MEDIUM','HIGH') AND grade_type='NUMERIC'
           GROUP BY slab_key""")}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--quarantined-only", action="store_true")
    ap.add_argument("--focus-file", help="file of item_ids to detail")
    args = ap.parse_args()

    conn = db.connect(args.db)
    enrich.load_surnames(conn)
    sizes = group_sizes(conn)
    rows = load(conn)

    results = []
    for r in rows:
        b = tier_b_dict(r["aspects_json"])
        findings, resolved, _verdict, reasons = enrich.compare(r, b)
        eff = enrich.effective_fields(r, b)
        eff_card, eff_slab = parse.make_keys(eff)
        cls = enrich.classify(r, b, findings, eff, eff_slab)
        rekey = enrich.substantive_rekey(r, eff)

        a_ident = enrich.identity_string({
            "year": r["year"], "manufacturer": r["manufacturer"],
            "set_name": enrich.canonical_set(r["set_name"], r["year"],
                                             r["manufacturer"]),
            "parallel": r["parallel"], "card_number": r["card_number"],
            "grade_type": r["grade_type"], "grade_value": r["grade_value"],
            "grade_qualifier": r["grade_qualifier"], "auto_grade": r["auto_grade"],
            "print_run": r["print_run"], "sport": r["sport"]})
        b_ident = (f"{b.get('season')}|{b.get('set_name')}|{b.get('parallel')}|"
                   f"#{b.get('card_number')}|{b.get('grade')}|"
                   f"{enrich.canonical_sport(b.get('sport'))}")
        e_ident = enrich.identity_string(eff)

        conn.execute("""UPDATE tierb SET verdict=?, classification=?,
                        disagreements=?, resolved_json=?, tier_a_identity=?,
                        tier_b_identity=?, effective_identity=?,
                        effective_card_key=?, effective_slab_key=?
                        WHERE item_id=?""",
                     (cls, cls, json.dumps(findings), json.dumps(resolved),
                      a_ident, b_ident, e_ident, eff_card, eff_slab,
                      r["item_id"]))
        results.append({"r": r, "b": b, "findings": findings, "cls": cls,
                        "rekey": rekey,
                        "eff": eff, "eff_slab": eff_slab, "a_ident": a_ident,
                        "b_ident": b_ident, "e_ident": e_ident,
                        "reasons": reasons})
    conn.commit()

    # How many members of each original group were re-keyed away?
    leaving = collections.Counter(
        x["r"]["slab_key"] for x in results if x["rekey"])
    enriched_per_group = collections.Counter(x["r"]["slab_key"] for x in results)

    def group_fate(slab):
        original = sizes.get(slab, 0)
        gone = leaving.get(slab, 0)
        remaining = original - gone
        unenriched = original - enriched_per_group.get(slab, 0)
        if unenriched > 0 and gone > 0:
            return (f"unresolved - {remaining} left but {unenriched} peer(s) "
                    f"un-enriched", remaining)
        if remaining <= 1:
            return f"invalidated (drops to {remaining})", remaining
        if gone > 0:
            return f"reduced {original} -> {remaining}", remaining
        return f"still viable ({original})", remaining

    print("=" * 78)
    print("  STAGE 1 ADJUDICATION - cached only, zero Browse item calls")
    print("=" * 78)

    focus = results
    if args.focus_file:
        want = {l.strip() for l in open(args.focus_file) if l.strip()}
        focus = [x for x in results if x["r"]["item_id"] in want]
    elif args.quarantined_only:
        focus = [x for x in results if x["r"]["prev"] == "quarantined"]

    for i, x in enumerate(focus, 1):
        r = x["r"]
        fate, remaining = group_fate(r["slab_key"])
        same = not x["rekey"]
        print(f"\n{i:3}. {r['title'][:70]}")
        print(f"     item {r['item_id']}")
        print(f"     Tier A identity : {x['a_ident']}")
        print(f"     Tier B raw      : {x['b_ident']}")
        print(f"     effective       : {x['e_ident']}")
        print(f"     slab {r['slab_key']} -> {x['eff_slab']}"
              f"   {'SAME CARD IDENTITY' if same else 'RE-KEYED'}")
        print(f"     original group size: {sizes.get(r['slab_key'], 0)}"
              f"   fate: {fate}")
        print(f"     classification  : {x['cls']}")
        mat = [f for f in x["findings"] if f["severity"] == enrich.MATERIAL]
        for f in mat:
            print(f"     MATERIAL {f['field']}: A={f['tier_a']!r} "
                  f"B={f['tier_b']!r} {f.get('note') or ''}")

    print("\n" + "=" * 78)
    print("  REVISED STAGE 1 TOTALS")
    print("=" * 78)
    c = collections.Counter(x["cls"] for x in results)
    for k in ("verified", "verified_with_missing_fields",
              "held_for_parallel_resolution", "identity_rekey_required",
              "quarantined_material_conflict"):
        print(f"    {k:34} {c.get(k, 0)}")

    was_q = focus if (args.focus_file or args.quarantined_only) else []
    resolved_fp = sum(1 for x in was_q
                      if x["cls"] not in ("quarantined_material_conflict",))
    rekeys = [x for x in results if x["rekey"]]
    touched = {x["r"]["slab_key"] for x in rekeys}
    invalidated = below2 = below3 = 0
    for slab in touched:
        _f, remaining = group_fate(slab)
        invalidated += remaining <= 1
        below2 += remaining < 2
        below3 += remaining < 3
    eligible = sum(1 for x in results
                   if x["cls"] in ("verified", "verified_with_missing_fields"))

    print(f"\n    false-positive quarantines resolved by normalization: "
          f"{resolved_fp} of {len(was_q)}")
    print(f"    original groups touched by a re-key   : {len(touched)}")
    print(f"    groups invalidated (<=1 remaining)    : {invalidated}")
    print(f"    groups reduced below size 2           : {below2}")
    print(f"    groups reduced below size 3           : {below3}")
    print(f"    candidates still eligible for triage  : {eligible}")
    print("=" * 78)


if __name__ == "__main__":
    main()
