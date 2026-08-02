"""Re-score cached Tier B pilot data with the corrected comparison logic.

Makes ZERO eBay calls: every input comes from the `tierb` rows already stored.
Rewrites only the verdict/findings columns of `tierb` - `listings` and `cards`
are read-only here.

Usage:  python reevaluate.py [--db cards.db]
"""

import argparse
import collections
import json

import db
import enrich
import parse


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    args = ap.parse_args()
    conn = db.connect(args.db)

    # Freeze the verdict the fetch run produced, so re-running this script is
    # idempotent and the before/after comparison stays honest.
    conn.execute("UPDATE tierb SET original_verdict = verdict "
                 "WHERE original_verdict IS NULL")
    conn.commit()

    rows = conn.execute("""
        SELECT t.item_id, t.original_verdict AS old_verdict, t.aspects_json,
               t.http_status, c.*, l.title
        FROM tierb t
        JOIN cards c USING (item_id)
        JOIN listings l USING (item_id)
        WHERE t.http_status = 200
    """).fetchall()

    print("=" * 78)
    print("  TIER B PILOT - RE-EVALUATION (cached data, zero API calls)")
    print("=" * 78)

    results = []
    for r in rows:
        aspects = json.loads(r["aspects_json"])
        upper = {k.upper(): v for k, v in aspects.items()}
        b = {"aspects": aspects}
        for field, aliases in enrich.ASPECT_MAP.items():
            b[field] = next((upper[a] for a in aliases if a in upper), None)
        for field in enrich.NOT_PROVIDED:
            b[field] = None

        findings, resolved, verdict, reasons = enrich.compare(r, b)
        a_canon = enrich.canonical_set(r["set_name"], r["year"],
                                       r["manufacturer"], b.get("season"))
        b_canon = enrich.canonical_set(b.get("set_name"), r["year"],
                                       r["manufacturer"], b.get("season"))
        par = next(f for f in findings if f["field"] == "parallel")
        state = {enrich.BENIGN: "exact", enrich.CANONICAL: "canonical_match",
                 enrich.MATERIAL: "material_disagreement",
                 enrich.MISSING_B: "tier_b_missing",
                 enrich.UNRESOLVED: "tier_a_unresolved"}[par["severity"]]

        conn.execute("UPDATE tierb SET verdict=?, disagreements=?, resolved_json=? "
                     "WHERE item_id=?",
                     (verdict, json.dumps(findings), json.dumps(resolved),
                      r["item_id"]))
        results.append({"row": r, "b": b, "findings": findings, "verdict": verdict,
                        "old": r["old_verdict"], "a_canon": a_canon,
                        "b_canon": b_canon, "par_state": state})
    conn.commit()

    for i, x in enumerate(results, 1):
        r, b = x["row"], x["b"]
        mat = [f for f in x["findings"] if f["severity"] == enrich.MATERIAL]
        print(f"\n{i:2}. {r['title'][:70]}")
        print(f"    verdict  : {x['old']}  ->  {x['verdict']}")
        print(f"    set      : A={r['set_name']!r}  B={b.get('set_name')!r}")
        print(f"    set canon: A={x['a_canon']!r}  B={x['b_canon']!r}")
        print(f"    parallel : A={r['parallel']!r}  B={b.get('parallel')!r}"
              f"  -> {x['par_state']}")
        if mat:
            for f in mat:
                print(f"    MATERIAL : {f['field']}: A={f['tier_a']!r} "
                      f"B={f['tier_b']!r} {f.get('note') or ''}")
        else:
            print("    material : none")

    print("\n" + "=" * 78)
    print("  REVISED PILOT SUMMARY")
    print("=" * 78)
    v = collections.Counter(x["verdict"] for x in results)
    old = collections.Counter(x["old"] for x in results)
    print(f"  original verdicts : {dict(old)}")
    for k in ("verified", "verified_with_missing_fields",
              "held_for_parallel_resolution", "quarantined"):
        print(f"    {k:32} {v.get(k, 0)}")

    set_mat = sum(1 for x in results for f in x["findings"]
                  if f["field"] == "set_name" and f["severity"] == enrich.MATERIAL)
    par_mat = sum(1 for x in results if x["par_state"] == "material_disagreement")
    canon = sum(1 for x in results for f in x["findings"]
                if f["severity"] == enrich.CANONICAL)
    print(f"\n  true material SET disagreements     : {set_mat}")
    print(f"  true material PARALLEL disagreements: {par_mat}")
    print(f"  canonical matches (formatting only) : {canon}")
    print(f"  parallel outcomes: "
          f"{dict(collections.Counter(x['par_state'] for x in results))}")

    print("\n  Aspect coverage (of %d cached items):" % len(results))
    for field in enrich.ASPECT_MAP:
        n = sum(1 for x in results if x["b"].get(field))
        print(f"    {field:14} {n:3}/{len(results)}")
    print("\n  Fields unavailable from getItem:")
    for field in enrich.NOT_PROVIDED:
        print(f"    {field:14} {enrich.NOT_PROVIDED_MARK}")
    print("=" * 78)


if __name__ == "__main__":
    main()
