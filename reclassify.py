"""Re-classify every cached Tier B row from stored evidence. No API calls.

Used after a classification-policy change - it re-runs compare/classify over
the aspects already in `tierb`, records what moved, and writes an audit file.

Nothing here fetches, and nothing here touches `listings`, `sold_comps` or the
collector's research state.

Usage:  python reclassify.py [--out reclassification_audit.json]
"""

import argparse
import collections
import json

import db
import enrich
import parse
import peers

FIELDS = ("year", "manufacturer", "set_name", "athlete", "card_number",
          "parallel", "serial_num", "print_run", "grade_value", "grade_type",
          "grade_qualifier", "is_auto", "auto_grade", "sport")


def snapshot(conn):
    """Classification and canonical keys for every cached row, before the run."""
    return {r["item_id"]: dict(r) for r in conn.execute(
        """SELECT item_id, classification, effective_identity,
                  effective_card_key, effective_slab_key, verdict
           FROM tierb WHERE http_status = 200""")}


def provenance(b, eff):
    """Where each effective field came from: tier_b overrides, else tier_a."""
    out = {}
    for f in FIELDS:
        tb = b.get(f)
        out[f] = {"value": eff[f], "source": "tier_b" if
                  (tb is not None and str(tb) != "" and eff[f] == tb)
                  else "tier_a"}
    return out


def run(conn):
    """Returns (audit rows, before snapshot). Writes the new classifications."""
    enrich.load_surnames(conn)
    before = snapshot(conn)
    enrich.score_all(conn)                       # refresh verdicts from cache
    eff = peers.effective_for(conn, list(before))  # writes classification+keys

    audit = []
    for iid, e in eff.items():
        row, f = e["row"], e["eff"]
        b = {}
        aspects = json.loads(row["aspects_json"] or "{}")
        upper = {k.upper(): v for k, v in aspects.items()}
        for field, aliases in enrich.ASPECT_MAP.items():
            b[field] = next((upper[al] for al in aliases if al in upper), None)
        prev = before[iid]
        card_key = parse.make_keys(f)[0] if f else None
        audit.append({
            "item_id": iid,
            "title": row["title"],
            "classification_before": prev["classification"],
            "classification_after": e["cls"],
            "changed": prev["classification"] != e["cls"],
            "identity_before": prev["effective_identity"],
            "identity_after": enrich.identity_string(f) if f else None,
            "card_key_before": prev["effective_card_key"],
            "card_key_after": card_key,
            "slab_key_before": prev["effective_slab_key"],
            "slab_key_after": e["eff_slab"],
            "tier_a_slab_key": row["slab_key"],
            "enriched_fields": provenance(b, f) if f else None,
            "tier_b_aspects": aspects,
            "disagreements": e["findings"],
        })
    audit.sort(key=lambda a: (not a["changed"], a["item_id"]))
    return audit, before


def report(audit):
    moves = collections.Counter(
        (a["classification_before"], a["classification_after"]) for a in audit)
    after = collections.Counter(a["classification_after"] for a in audit)
    key_moved = [a for a in audit if a["slab_key_before"] != a["slab_key_after"]]

    print("=" * 78)
    print("  CACHED RECLASSIFICATION (no API calls)")
    print("=" * 78)
    print(f"  rows re-classified          : {len(audit)}")
    print(f"  classification changed      : {sum(a['changed'] for a in audit)}")
    print(f"  canonical slab key changed  : {len(key_moved)}")
    print("\n  transitions")
    for (b, a), n in sorted(moves.items(), key=lambda kv: -kv[1]):
        arrow = "->" if b != a else "=="
        print(f"    {str(b):32} {arrow} {a:32} {n}")
    print("\n  classification after")
    for k, n in after.most_common():
        print(f"    {k:36} {n}")
    if key_moved:
        print("\n  rows whose canonical key moved")
        for a in key_moved[:20]:
            print(f"    {a['item_id']}  {a['slab_key_before']} -> "
                  f"{a['slab_key_after']}")
            print(f"      {a['title'][:70]}")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--out", default="reclassification_audit.json")
    args = ap.parse_args()

    conn = db.connect(args.db)
    audit, _before = run(conn)
    report(audit)
    json.dump(audit, open(args.out, "w"), indent=1)
    print(f"  audit written -> {args.out}")


if __name__ == "__main__":
    main()
