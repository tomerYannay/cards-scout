"""Step 3A peer enrichment: settle every group whose candidate identity is fixed.

A candidate whose identity Tier B has settled - re-keyed or confirmed in place -
still cannot be grouped until its peers carry a Tier B identity too. This
enriches only those peers and rebuilds each affected group on effective
identities.

Scope is hard-bounded: only listings inside the affected original slab groups,
never more than PEER_CAP new requests, and never a refetch of an existing
HTTP 200 without --refresh. `--run` additionally refuses to start unless the
quota covers the worst case (every item retrying to exhaustion) plus a reserve.

Usage:
  python peers.py --plan     # print the plan, write it to disk, fetch nothing
  python peers.py --run      # execute exactly the planned request set
  python peers.py --report   # rebuild groups from cache only, zero calls
"""

import argparse
import collections
import datetime as dt
import json
import os
import sys

import db
import ebay_api
import enrich
import parse

# Hard backstop on requests per run. Raised from 120 once the scope widened
# from the 31 re-keyed groups to every group with a settled candidate: the 69
# groups confirmed by the base-card rule need 172 peers. Still a cap, not a
# budget - `guard()` aborts rather than truncating.
PEER_CAP = 200
# Calls that must still be available after the worst-case run, so a failed
# enrichment never leaves the account with nothing left for a retry.
QUOTA_RESERVE = 200
PLAN_PATH = "peer_plan.json"
PEER_AUDIT_PATH = "peer_enrichment_audit.json"
REKEY = "identity_rekey_required"


def affected_groups(conn):
    """Original Tier A slab groups whose candidate carries a settled identity.

    Started as the 31 re-keyed groups. A candidate that Tier B confirms in place
    (`verified`) is equally settled and its group equally needs rebuilding, so
    both are in scope. Candidates still `held_for_parallel_resolution` are not -
    their own identity is unresolved, so there is nothing to rebuild against.

    This is the scope of the rebuild only. How a group is then classified is
    `classify_group`, which is unchanged.
    """
    return {r["slab_key"]: r["item_id"] for r in conn.execute(
        f"""SELECT c.slab_key, c.item_id FROM tierb t JOIN cards c USING(item_id)
            WHERE t.classification IN ('{REKEY}', 'verified')""")}


def group_members(conn, slabs):
    q = ",".join("?" * len(slabs))
    return conn.execute(f"""
        SELECT c.slab_key, c.item_id, c.print_run, c.auto_grade,
               c.grade_qualifier, l.title, l.price, l.shipping_cost
        FROM cards c JOIN listings l USING (item_id)
        WHERE l.active = 1 AND c.slab_key IN ({q})""", list(slabs)).fetchall()


def build_plan(conn):
    groups = affected_groups(conn)
    members = group_members(conn, list(groups))
    cached = {r[0] for r in conn.execute(
        "SELECT item_id FROM tierb WHERE http_status = 200")}
    todo = sorted({m["item_id"] for m in members} - cached)

    by_group = collections.Counter(m["slab_key"] for m in members)
    sizes = collections.Counter()
    for n in by_group.values():
        sizes["2" if n == 2 else "3" if n == 3 else "4" if n == 4 else "5+"] += 1
    return {
        "slabs": sorted(groups),
        "candidates": sorted(groups.values()),
        "members": sorted({m["item_id"] for m in members}),
        "todo": todo,
        "expected": len(todo),
        "stats": {
            "groups": len(groups),
            "active_members": len(members),
            "cached": len({m["item_id"] for m in members} & cached),
            "sizes": dict(sizes),
            "numbered": sum(1 for m in members if m["print_run"] is not None),
            "autograph": sum(1 for m in members if m["auto_grade"] is not None),
            "qualifier": sum(1 for m in members if m["grade_qualifier"] is not None),
        },
    }


def print_plan(plan, quota, quota_at):
    s = plan["stats"]
    print("=" * 78)
    print("  PEER ENRICHMENT PLAN - groups with a settled candidate only")
    print("=" * 78)
    print(f"  Affected original groups     : {s['groups']}")
    print(f"  Active listings in them      : {s['active_members']}")
    print(f"  Already cached (HTTP 200)    : {s['cached']}  [frozen]")
    print(f"  Unique NEW peer requests     : {plan['expected']}  (cap {PEER_CAP})")
    print(f"  Worst case incl. retries     : {worst_case_calls(plan['expected'])}"
          f"  (+{QUOTA_RESERVE} reserve = "
          f"{worst_case_calls(plan['expected']) + QUOTA_RESERVE} required)")
    print(f"  Group sizes (2/3/4/5+)       : "
          f"{s['sizes'].get('2',0)} / {s['sizes'].get('3',0)} / "
          f"{s['sizes'].get('4',0)} / {s['sizes'].get('5+',0)}")
    print(f"  Numbered listings            : {s['numbered']}")
    print(f"  Autograph listings           : {s['autograph']}")
    print(f"  Qualifier listings           : {s['qualifier']}")
    print(f"  Quota read at                : {quota_at}")
    print(f"  Browse quota remaining       : {quota if quota is not None else 'UNREADABLE'}")
    print(f"  EXACT expected request count : {plan['expected']}")


def worst_case_calls(expected):
    """Upper bound on HTTP calls: get_item retries a failure up to RETRIES times."""
    return expected * ebay_api.get_item.__defaults__[-1]


def guard(conn, plan, quota, executing=False):
    """Every abort condition, checked before a single request goes out."""
    slabs = set(plan["slabs"])
    q = ",".join("?" * len(plan["todo"]))
    if plan["todo"]:
        outside = conn.execute(
            f"""SELECT COUNT(*) FROM cards WHERE item_id IN ({q})
                AND slab_key NOT IN ({','.join('?' * len(slabs))})""",
            plan["todo"] + sorted(slabs)).fetchone()[0]
        if outside:
            sys.exit(f"ABORT: {outside} planned item(s) fall outside the "
                     f"{len(slabs)} affected groups")
    if plan["expected"] > PEER_CAP:
        sys.exit(f"ABORT: {plan['expected']} requests exceeds the cap {PEER_CAP}")
    if quota is not None and quota < plan["expected"]:
        sys.exit(f"ABORT: quota {quota} < {plan['expected']} expected requests")
    if not executing:
        return
    # Executing for real: an unknown quota is not permission to spend one.
    if quota is None:
        sys.exit("ABORT: quota is UNREADABLE; refusing to spend calls blind")
    worst = worst_case_calls(plan["expected"])
    if quota < worst + QUOTA_RESERVE:
        sys.exit(f"ABORT: quota {quota} does not cover the worst case "
                 f"{worst} (every item retrying to exhaustion) plus the "
                 f"{QUOTA_RESERVE}-call reserve")


def run_fetch(conn, plan, token, refresh):
    cached = {r[0] for r in conn.execute(
        "SELECT item_id FROM tierb WHERE http_status = 200")}
    todo = plan["todo"] if not refresh else plan["members"]
    if not refresh and (set(todo) & cached):
        sys.exit("ABORT: plan would refetch cached rows without --refresh")
    if len(todo) != plan["expected"]:
        sys.exit(f"ABORT: request count changed since --plan "
                 f"({len(todo)} vs {plan['expected']})")

    rows = {r["item_id"]: r for r in conn.execute(
        "SELECT * FROM cards WHERE item_id IN (%s)" % ",".join("?" * len(todo)),
        todo)}
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    stats = collections.Counter()
    outcomes = {}
    for n, iid in enumerate(todo, 1):
        before_calls = ebay_api.request_count
        status, raw = ebay_api.get_item(token, iid)
        # get_item counts one request per attempt, so the delta is the true
        # HTTP cost of this item including any retry.
        calls = ebay_api.request_count - before_calls
        stats["attempted"] += 1
        stats["http_calls"] += calls
        stats["retries"] += calls - 1
        stats["ok" if status == 200 else "failed"] += 1
        stats["404"] += status == 404
        outcomes[iid] = {"http_status": status, "http_calls": calls,
                         "retries": calls - 1,
                         "error": (raw or {}).get("error")
                         if status != 200 else None}
        # Committed per item by enrich.store, so an interruption loses nothing
        # and the next --plan simply excludes what already succeeded.
        enrich.store(conn, iid, status, raw, rows[iid], fetched_at)
        if n % 10 == 0 or status != 200:
            print(f"    [{n}/{len(todo)}] {iid} -> HTTP {status}"
                  + (f" ({calls} attempts)" if calls > 1 else ""))
    return stats, outcomes


def effective_for(conn, item_ids):
    """Effective identity for every cached member. No API calls.

    Writes classifications, so it must never run against an unloaded surname
    table: without it `is_name_only_parallel` cannot fire and real base cards
    get written back as material conflicts.
    """
    enrich.load_surnames(conn)
    q = ",".join("?" * len(item_ids))
    rows = conn.execute(f"""
        SELECT t.aspects_json, t.http_status, c.*, l.title, l.price,
               l.shipping_cost
        FROM cards c JOIN listings l USING (item_id)
        LEFT JOIN tierb t USING (item_id)
        WHERE c.item_id IN ({q})""", list(item_ids)).fetchall()
    out = {}
    for r in rows:
        if r["http_status"] != 200:
            out[r["item_id"]] = {"row": r, "eff_slab": None, "cls": "not_enriched",
                                 "eff": None, "findings": []}
            continue
        b = {"aspects": json.loads(r["aspects_json"] or "{}")}
        upper = {k.upper(): v for k, v in b["aspects"].items()}
        for f, aliases in enrich.ASPECT_MAP.items():
            b[f] = next((upper[a] for a in aliases if a in upper), None)
        for f in enrich.NOT_PROVIDED:
            b[f] = None
        findings, resolved, _v, _r = enrich.compare(r, b)
        eff = enrich.effective_fields(r, b)
        eff_card, eff_slab = parse.make_keys(eff)
        cls = enrich.classify(r, b, findings, eff, eff_slab)
        conn.execute("""UPDATE tierb SET classification=?, effective_identity=?,
                        effective_card_key=?, effective_slab_key=?,
                        disagreements=?, resolved_json=? WHERE item_id=?""",
                     (cls, enrich.identity_string(eff), eff_card, eff_slab,
                      json.dumps(findings), json.dumps(resolved), r["item_id"]))
        out[r["item_id"]] = {"row": r, "eff_slab": eff_slab, "cls": cls,
                             "eff": eff, "findings": findings}
    conn.commit()
    return out


def tier_a_identity(r):
    """The identity a listing had from its title alone, before any Tier B."""
    return enrich.identity_string({
        "year": r["year"], "manufacturer": r["manufacturer"],
        "set_name": enrich.canonical_set(r["set_name"], r["year"],
                                         r["manufacturer"]),
        "parallel": r["parallel"], "card_number": r["card_number"],
        "grade_type": r["grade_type"], "grade_value": r["grade_value"],
        "grade_qualifier": r["grade_qualifier"], "auto_grade": r["auto_grade"],
        "print_run": r["print_run"], "sport": r["sport"]})


def peer_snapshot(conn, item_ids):
    """Pre-fetch state for the peers about to be enriched. Tier A only."""
    q = ",".join("?" * len(item_ids))
    return {r["item_id"]: {
        "title": r["title"], "group": r["slab_key"],
        "identity": tier_a_identity(r),
        "card_key": r["card_key"], "slab_key": r["slab_key"]}
        for r in conn.execute(
            f"""SELECT c.*, l.title FROM cards c JOIN listings l USING (item_id)
                WHERE c.item_id IN ({q})""", list(item_ids))}


def peer_audit(conn, before, outcomes, eff, group_of):
    """One record per attempted peer: what it was, what it became, what it cost."""
    rows = []
    for iid, was in before.items():
        got = outcomes.get(iid, {})
        e = eff.get(iid)
        fields, prov = {}, {}
        if e and e["eff"]:
            aspects = json.loads(e["row"]["aspects_json"] or "{}")
            upper = {k.upper(): v for k, v in aspects.items()}
            for f, aliases in enrich.ASPECT_MAP.items():
                tb = next((upper[a] for a in aliases if a in upper), None)
                fields[f] = tb
                prov[f] = "tier_b" if tb not in (None, "") else "tier_a"
            for f in enrich.NOT_PROVIDED:
                prov[f] = f"tier_a ({enrich.NOT_PROVIDED_MARK})"
        rows.append({
            "item_id": iid,
            "title": was["title"],
            "group_before": was["group"],
            "group_after": (e["eff_slab"] if e and e["eff_slab"]
                            else was["group"]),
            "identity_before": was["identity"],
            "identity_after": (enrich.identity_string(e["eff"])
                               if e and e["eff"] else None),
            "card_key_before": was["card_key"],
            "card_key_after": (parse.make_keys(e["eff"])[0]
                               if e and e["eff"] else None),
            "slab_key_before": was["slab_key"],
            "slab_key_after": e["eff_slab"] if e else None,
            "rekeyed": bool(e and e["eff_slab"]
                            and e["eff_slab"] != was["slab_key"]),
            "tier_b_fields": fields,
            "provenance": prov,
            "tier_b_aspects": (json.loads(e["row"]["aspects_json"] or "{}")
                               if e and e["eff"] else {}),
            "classification": e["cls"] if e else "not_enriched",
            "disagreements": e["findings"] if e else [],
            "http_status": got.get("http_status"),
            "http_calls": got.get("http_calls"),
            "retries": got.get("retries"),
            "error": got.get("error"),
            "candidate_group_after": group_of.get(iid),
        })
    rows.sort(key=lambda r: (r["http_status"] == 200, not r["rekeyed"],
                             r["item_id"]))
    return rows


def total(row):
    return (row["price"] or 0) + (row["shipping_cost"] or 0)


def classify_group(members, eff, candidate_id):
    """One of the six approved group outcomes, from identity only - not price."""
    mine = [eff[i] for i in members]
    if any(e["cls"] == "quarantined_material_conflict" for e in mine):
        return "material_conflict"
    if any(e["cls"] in ("not_enriched", "held_for_parallel_resolution")
           for e in mine):
        return "unresolved_peer_identity"
    cand_slab = eff[candidate_id]["eff_slab"]
    same = [i for i in members if eff[i]["eff_slab"] == cand_slab]
    if len({eff[i]["eff_slab"] for i in members}) == 1:
        return "confirmed_same_identity"
    if len(same) >= 3:
        return "split_but_candidate_group_viable"
    if len(same) == 2:
        return "split_weak_signal"
    return "candidate_isolated"


def report(conn, audit_titles=()):
    groups = affected_groups(conn)
    members = group_members(conn, list(groups))
    by_group = collections.defaultdict(list)
    for m in members:
        by_group[m["slab_key"]].append(m)
    eff = effective_for(conn, [m["item_id"] for m in members])

    print("\n" + "=" * 78)
    print("  AFFECTED GROUP RECONSTRUCTION (effective identities)")
    print("=" * 78)

    outcomes, sub_sizes, disc_changes = collections.Counter(), collections.Counter(), []
    detail = []
    for slab, ms in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
        cand = groups[slab]
        ids = [m["item_id"] for m in ms]
        outcome = classify_group(ids, eff, cand)
        outcomes[outcome] += 1
        cand_slab = eff[cand]["eff_slab"]
        with_cand = [m for m in ms if eff[m["item_id"]]["eff_slab"] == cand_slab]
        n = len(with_cand)
        sub_sizes["1" if n == 1 else "2" if n == 2 else "3+"] += 1

        old_prices = sorted(total(m) for m in ms)
        new_prices = sorted(total(m) for m in with_cand)
        cand_total = total(next(m for m in ms if m["item_id"] == cand))
        still_lowest = bool(new_prices) and abs(cand_total - new_prices[0]) < 1e-9
        old_med = old_prices[len(old_prices) // 2]
        new_med = new_prices[len(new_prices) // 2] if new_prices else None
        if new_med and old_med and len(new_prices) >= 2:
            od = 100 * (old_med - cand_total) / old_med
            nd = 100 * (new_med - cand_total) / new_med
            if abs(od - nd) >= 10:
                disc_changes.append((cand, od, nd))
        detail.append((slab, cand, ms, outcome, cand_slab, with_cand,
                       still_lowest, n))

    for slab, cand, ms, outcome, cand_slab, with_cand, lowest, n in detail:
        title = next(m["title"] for m in ms if m["item_id"] == cand)
        show = any(t.lower() in title.lower() for t in audit_titles) if audit_titles else True
        if not show:
            continue
        subs = collections.Counter(eff[m["item_id"]]["eff_slab"] or "not_enriched"
                                   for m in ms)
        print(f"\n  original slab {slab}   size {len(ms)}   -> {outcome}")
        print(f"    candidate: {title[:66]}")
        print(f"    effective subgroups: "
              f"{ {k[:10]: v for k, v in subs.items()} }")
        for m in ms:
            e = eff[m["item_id"]]
            mark = "CAND" if m["item_id"] == cand else "peer"
            tog = "same" if e["eff_slab"] == cand_slab else "SPLIT"
            print(f"      [{mark}] ${total(m):>9,.2f}  {(e['eff_slab'] or 'not_enriched')[:10]}"
                  f"  {tog:5}  {m['title'][:52]}")
        print(f"    candidate subgroup size: {n} "
              f"({'1 listing' if n == 1 else '2 listings' if n == 2 else '3+ listings'})")
        print(f"    candidate still lowest-priced in its subgroup: {lowest}")

    print("\n" + "=" * 78)
    print("  GROUP OUTCOMES")
    print("=" * 78)
    for k in ("confirmed_same_identity", "split_but_candidate_group_viable",
              "split_weak_signal", "candidate_isolated",
              "unresolved_peer_identity", "material_conflict"):
        print(f"    {k:36} {outcomes.get(k, 0)}")

    print("\n  TRIAGE ELIGIBILITY (effective identities, asking-price only)")
    confirmed3 = sum(1 for d in detail
                     if d[3] in ("confirmed_same_identity",
                                 "split_but_candidate_group_viable") and d[7] >= 3)
    confirmed2 = sum(1 for d in detail if d[7] == 2 and
                     d[3] in ("confirmed_same_identity", "split_weak_signal"))
    isolated = sum(1 for d in detail if d[7] == 1)
    unresolved = outcomes.get("unresolved_peer_identity", 0)
    print(f"    confirmed groups with 3+ listings   : {confirmed3}")
    print(f"    confirmed groups with exactly 2     : {confirmed2}")
    print(f"    isolated candidates                 : {isolated}")
    print(f"    unresolved groups                   : {unresolved}")
    print(f"    candidates REMOVED from triage      : {isolated + unresolved}")
    print(f"    candidates still eligible           : {confirmed3 + confirmed2}")
    print(f"    discounts changing >=10pp after regroup: {len(disc_changes)}")
    for cand, od, nd in disc_changes[:8]:
        print(f"      {cand}: {od:.1f}% -> {nd:.1f}% below group median")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--audit", nargs="*", default=[])
    args = ap.parse_args()

    conn = db.connect(args.db)
    enrich.load_surnames(conn)

    if args.report:
        report(conn, args.audit)
        return

    plan = build_plan(conn)
    quota, quota_at = None, dt.datetime.now(dt.timezone.utc).isoformat()
    token = None
    try:
        token = ebay_api.get_token()
        limits, _err = ebay_api.get_rate_limit(token)
        if limits:
            quota = ebay_api.browse_quota(limits)
    except ebay_api.EbayError as exc:
        if not args.plan:
            sys.exit(f"error: {exc}")
        print(f"  (credentials unavailable: {exc})")

    print_plan(plan, quota, quota_at)
    guard(conn, plan, quota, executing=not args.plan)

    if args.plan:
        json.dump(plan, open(PLAN_PATH, "w"), indent=1)
        print(f"\n  plan written to {PLAN_PATH}; nothing fetched.")
        return

    if not os.path.exists(PLAN_PATH):
        sys.exit("ABORT: run --plan first so the request count can be verified")
    saved = json.load(open(PLAN_PATH))
    if saved["todo"] != plan["todo"]:
        sys.exit(f"ABORT: request set changed since --plan "
                 f"({len(saved['todo'])} planned vs {len(plan['todo'])} now)")

    todo = plan["members"] if args.refresh else plan["todo"]
    before = peer_snapshot(conn, todo)

    print(f"\n  fetching {plan['expected']} peers...")
    stats, outcomes = run_fetch(conn, plan, token, args.refresh)
    print(f"\n  attempted={stats['attempted']} ok={stats['ok']} "
          f"failed={stats['failed']} 404={stats['404']} "
          f"http_calls={stats['http_calls']} retries={stats['retries']}")

    # Groups are only re-classified after every successful fetch is persisted.
    groups = affected_groups(conn)
    members = group_members(conn, list(groups))
    eff = effective_for(conn, [m["item_id"] for m in members])
    group_of = {}
    for slab, cand in groups.items():
        ids = [m["item_id"] for m in members if m["slab_key"] == slab]
        outcome = classify_group(ids, eff, cand)
        for i in ids:
            group_of[i] = {"original_slab": slab, "candidate": cand,
                           "outcome": outcome}

    audit = peer_audit(conn, before, outcomes, eff, group_of)
    json.dump({"stats": dict(stats), "peers": audit},
              open(PEER_AUDIT_PATH, "w"), indent=1)
    print(f"  peer audit written -> {PEER_AUDIT_PATH}")
    report(conn, args.audit)


if __name__ == "__main__":
    main()
