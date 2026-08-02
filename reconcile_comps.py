"""Re-decide stored sold-comp rows against the CURRENT matcher. No live calls.

Rows are classified when they are collected. Every later matcher fix - the
SPARKLE vocabulary, serial-stamp parsing, print-run repair, the manufacturer
guard - applies only to rows collected afterwards, so an old candidate keeps
whatever verdict the rules of its day produced. This re-runs the current rules
over rows already in the database and records exactly what moved.

Raw collected fields (raw_title, prices, dates, source ids) are never touched;
only the classification columns are rewritten.

Usage:
  python reconcile_comps.py --plan                 # report only, write nothing
  python reconcile_comps.py                        # reconcile and persist
  python reconcile_comps.py --only <item_id> ...   # limit to some candidates
"""

import argparse
import collections
import datetime as dt
import json
import statistics
import sys

import db
import decision as dec
import enrich
import manual_comps as mc
import product_research_parse as prp

AUDIT_PATH = "comp_reconciliation_audit.json"

# Columns that describe the collected transaction. A reconciliation that
# changed any of these would be rewriting history, not re-deciding it.
IMMUTABLE = ("candidate_item_id", "source_item_id", "raw_title", "sold_price",
             "shipping", "total_price", "currency", "sale_date", "condition",
             "sale_type", "collected_at", "imported_at", "query_tier")


def rule_for(reason):
    """Which rule produced this verdict - the audit's 'rule responsible'."""
    if not reason:
        return "accepted"
    r = reason.lower()
    for needle, rule in (
            # Order matters: "raw/ungraded - no PSA grade in title" contains
            # "psa grade" but is an ungraded rejection, not a grade mismatch.
            ("self comp", "self_comp"),
            ("raw/ungraded", "ungraded"),
            ("manufacturer/brand", "manufacturer_guard"),
            ("parallel/set", "parallel_set_identity"),
            ("print run", "print_run"),
            ("card number", "card_number"),
            ("psa grade", "grade"),
            ("grade type", "grade_type"),
            ("qualifier", "qualifier"),
            ("autograph grade", "autograph_grade"),
            ("autograph status", "autograph_status"),
            ("year", "year"),
            ("subject", "subject"),
            ("lot", "lot"),
            ("graded by", "rival_grader"),
            ("field absent from title", "missing_evidence_review")):
        if needle in r:
            return rule
    return "other"


def snapshot(conn, cid, cand):
    """Counts, market benchmark and decision for one candidate as stored."""
    rows = conn.execute(
        """SELECT accepted, match_confidence, total_price, sale_date
           FROM sold_comps WHERE candidate_item_id = ?""", (cid,)).fetchall()
    acc = [r for r in rows if r["accepted"] == 1]
    review = [r for r in rows
              if r["accepted"] != 1 and r["match_confidence"] == "REVIEW_REQUIRED"]
    priced = [r["total_price"] for r in acc if r["total_price"] is not None]
    listing = conn.execute(
        "SELECT active, price, shipping_cost FROM listings WHERE item_id = ?",
        (cid,)).fetchone()
    d = dec.decide(
        listing["price"] if listing else cand.get("asking_price"),
        [{"total_price": r["total_price"], "sale_date": r["sale_date"]} for r in acc],
        shipping=listing["shipping_cost"] if listing else None,
        listing_active=bool(listing["active"]) if listing else True)
    return {"rows": len(rows), "accepted": len(acc), "review": len(review),
            "rejected": len(rows) - len(acc) - len(review),
            "median": statistics.median(priced) if priced else None,
            "confidence": d["confidence"], "decision": d["decision"],
            "reason": d["reason"], "downgrade": d["downgrade_reason"],
            "item_price": d["item_price"], "shipping": d["shipping"],
            "total_cost": d["candidate_total_cost"],
            "gross_gap": d["gross_gap"]}


def reconcile(conn, cands, cids, write=True):
    """Re-decide every stored row for `cids`. Returns (audit rows, before, after)."""
    enrich.load_surnames(conn)
    before = {c: snapshot(conn, c, cands[c]) for c in cids}
    audit, moved = [], 0
    for cid in cids:
        cand = cands[cid]
        for r in conn.execute(
                """SELECT id, raw_title, accepted, match_confidence,
                          rejection_reason, source_item_id FROM sold_comps
                   WHERE candidate_item_id = ?""", (cid,)).fetchall():
            state, reason = prp.classify_comp(
                cand, r["raw_title"], source_item_id=r["source_item_id"])
            new_acc = 1 if state == prp.ACCEPTED else 0
            new_conf = {"accepted": "EXACT", "review_required": "REVIEW_REQUIRED",
                        "rejected": None}[state]
            old_state = ("accepted" if r["accepted"] == 1 else
                         "review_required"
                         if r["match_confidence"] == "REVIEW_REQUIRED"
                         else "rejected")
            changed = old_state != state
            moved += changed
            if changed:
                audit.append({
                    "row_id": r["id"], "candidate_item_id": cid,
                    "candidate_title": cand["title"],
                    "comp_source_item_id": r["source_item_id"],
                    "comp_title": r["raw_title"],
                    "old_state": old_state, "new_state": state,
                    "old_reason": r["rejection_reason"], "new_reason": reason,
                    "old_rule": rule_for(r["rejection_reason"]),
                    "rule_responsible": rule_for(reason),
                })
            # Always applied so the "after" snapshot is real; --plan rolls the
            # whole thing back once it has been measured.
            conn.execute(
                """UPDATE sold_comps SET accepted = ?, match_confidence = ?,
                   rejection_reason = ? WHERE id = ?""",
                (new_acc, new_conf, reason, r["id"]))
    after = {c: snapshot(conn, c, cands[c]) for c in cids}
    conn.commit() if write else conn.rollback()
    return audit, before, after, moved


def verify_immutable(conn, baseline):
    """Every collected field must be byte-identical to the pre-run baseline."""
    now = {r["id"]: tuple(r[c] for c in IMMUTABLE)
           for r in conn.execute("SELECT * FROM sold_comps")}
    return {"added": sorted(set(now) - set(baseline)),
            "removed": sorted(set(baseline) - set(now)),
            "mutated": sorted(i for i in baseline
                              if i in now and baseline[i] != now[i])}


def baseline_of(conn):
    return {r["id"]: tuple(r[c] for c in IMMUTABLE)
            for r in conn.execute("SELECT * FROM sold_comps")}


def report(before, after, cids, cands, moved):
    print("=" * 100)
    print("  STORED-COMP RECONCILIATION (current matcher, no live calls)")
    print("=" * 100)
    print(f"  candidates            : {len(cids)}")
    print(f"  row classifications changed : {moved}")
    print()
    hdr = (f"  {'candidate':38} {'acc':>9} {'rej':>9} {'rev':>9} "
           f"{'median':>17} {'decision':>16}")
    print(hdr)
    print("  " + "-" * 96)
    changed_decision = []
    for cid in cids:
        b, a = before[cid], after[cid]
        m = lambda v: f"${v:,.2f}" if v is not None else "n/a"
        star = ""
        if b["decision"] != a["decision"]:
            changed_decision.append(cid)
            star = "  <== DECISION CHANGED"
        if (b["accepted"], b["rejected"], b["review"], b["median"],
                b["decision"]) == (a["accepted"], a["rejected"], a["review"],
                                   a["median"], a["decision"]):
            continue
        print(f"  {cands[cid]['title'][:38]:38} "
              f"{b['accepted']:>4}->{a['accepted']:<4} "
              f"{b['rejected']:>4}->{a['rejected']:<4} "
              f"{b['review']:>4}->{a['review']:<4} "
              f"{m(b['median']):>7}->{m(a['median']):<8} "
              f"{b['decision']:>6}->{a['decision']:<6}{star}")
    print()
    print(f"  candidates whose DECISION changed: {len(changed_decision)}")
    for cid in changed_decision:
        b, a = before[cid], after[cid]
        print(f"    {cands[cid]['title'][:60]}")
        print(f"      {b['decision']} ({b['reason']}) -> {a['decision']} ({a['reason']})")
    conf = collections.Counter(
        (before[c]["confidence"], after[c]["confidence"]) for c in cids)
    print("\n  confidence transitions:")
    for (b, a), n in sorted(conf.items(), key=lambda kv: -kv[1]):
        print(f"    {b:>6} -> {a:<6} {n}")
    print("=" * 100)
    return changed_decision


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--out", default=AUDIT_PATH)
    ap.add_argument("--plan", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--only", nargs="*", default=None,
                    help="limit to these candidate item ids")
    args = ap.parse_args()

    conn = db.connect(args.db)
    cands = mc.load_candidates()
    stored = [r[0] for r in conn.execute(
        "SELECT DISTINCT candidate_item_id FROM sold_comps ORDER BY 1")]
    cids = [c for c in stored if c in cands]
    if args.only:
        cids = [c for c in cids if c in set(args.only)]
    missing = [c for c in stored if c not in cands]
    if missing:
        print(f"  note: {len(missing)} candidate(s) have stored rows but are no "
              f"longer in the pool; left untouched")
    if not cids:
        sys.exit("error: no candidates to reconcile")

    base = baseline_of(conn)
    total_before = conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0]
    audit, before, after, moved = reconcile(conn, cands, cids,
                                            write=not args.plan)
    changed = report(before, after, cids, cands, moved)

    total_after = conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0]
    integrity = verify_immutable(conn, base)
    print(f"\n  row count {total_before} -> {total_after}"
          f"   added {len(integrity['added'])}"
          f"   removed {len(integrity['removed'])}"
          f"   collected fields mutated {len(integrity['mutated'])}")
    ok = (total_before == total_after and not integrity["added"]
          and not integrity["removed"] and not integrity["mutated"])
    print(f"  integrity: {'OK' if ok else 'VIOLATED'}")
    if not ok:
        sys.exit("ABORT: reconciliation altered collected data")

    if args.plan:
        print("\n  --plan: nothing written")
        return
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": cids, "rows_changed": moved,
        "row_count_before": total_before, "row_count_after": total_after,
        "decisions_changed": changed,
        "per_candidate": {c: {"before": before[c], "after": after[c]}
                          for c in cids},
        "changes": audit,
    }
    json.dump(payload, open(args.out, "w"), indent=1, default=str)
    print(f"  audit written -> {args.out}")


if __name__ == "__main__":
    main()
