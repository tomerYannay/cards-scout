"""Step 2 validation pass: how trustworthy is slab_key grouping?

Read-only. Answers three questions:
  1. Do the listings inside a slab_key group really describe the same card?
  2. How much grouping risk is there, quantified?
  3. Why did the non-PSA exclusion count land at 4,937?

The central risk is the parser DISCARDING a distinguishing token - if a
parallel word is dropped, two different parallels collapse into one group and
Step 3 would report a fake deal. So the core test compares the raw titles
inside each group and reports what differs.

Usage:  python validate_groups.py [--db cards.db] [--sample 25]
"""

import argparse
import collections
import json
import random
import re
import statistics

import db
import parse

ELIGIBLE = "c.identity_conf IN ('MEDIUM','HIGH') AND c.grade_type = 'NUMERIC'"

# Tokens that carry no identity meaning when diffing titles inside a group.
NOISE = {
    "PSA", "RC", "ROOKIE", "AUTO", "AUTOGRAPH", "SIGNED", "THE", "OF", "AND",
    "A", "&", "-", "CARD", "TRADING", "SP",
}


def load_groups(conn):
    """slab_key -> list of per-listing dicts, eligible listings only."""
    rows = conn.execute(f"""
        SELECT c.slab_key, c.item_id, l.title, l.price, l.shipping_cost,
               l.buying_option, l.raw, c.parallel, c.parallel_conf,
               c.identity_conf, c.card_number, c.year, c.set_name, c.athlete,
               c.grade_value, c.print_run, c.serial_num, c.is_rookie
        FROM cards c JOIN listings l USING (item_id)
        WHERE {ELIGIBLE} AND l.active = 1
    """).fetchall()
    groups = collections.defaultdict(list)
    for r in rows:
        d = dict(r)
        d["epid"] = (json.loads(r["raw"]) or {}).get("epid")
        groups[r["slab_key"]].append(d)
    return groups


def title_tokens(title):
    t = parse.normalize(title)
    t = re.sub(r"\bPSA\s*(10|[1-9](?:\.5)?)\b", " ", t)
    # Copy number varies legitimately within a group, in either notation:
    # "200/299" and "#/299" are the same card from the same print run.
    t = re.sub(r"#\s*/\s*\d{1,5}\b", " ", t)
    t = re.sub(r"\b\d{1,4}\s*/\s*\d{1,5}\b", " ", t)
    return {tok for tok in re.split(r"[^A-Z0-9'./-]+", t) if tok and tok not in NOISE}


def title_divergence(members):
    """Tokens that appear in some titles of a group but not others."""
    sets = [title_tokens(m["title"]) for m in members]
    common = set.intersection(*sets)
    union = set.union(*sets)
    return union - common


def spread(members):
    prices = [m["price"] for m in members if m["price"]]
    if len(prices) < 2 or min(prices) <= 0:
        return 1.0
    return max(prices) / min(prices)


def epid_state(members):
    epids = {m["epid"] for m in members if m["epid"]}
    if len(epids) > 1:
        return "conflict"
    if len(epids) == 1 and all(m["epid"] for m in members):
        return "agree"
    return "partial"


def describe(members, divergent):
    m = members[0]
    prices = sorted(p for p in (x["price"] for x in members) if p)
    verdict = "SAME CARD" if not divergent else "DIVERGENT TITLES"
    print(f"    n={len(members):3} {verdict:16} spread={spread(members):.1f}x "
          f"epid={epid_state(members)}  ${prices[0]:,.2f}-${prices[-1]:,.2f}")
    print(f"      {m['year']} {str(m['set_name'])[:28]:28} #{m['card_number']} "
          f"{str(m['athlete'])[:20]:20} PSA {m['grade_value']} "
          f"par={m['parallel']!r}/{m['parallel_conf']}")
    if divergent:
        print(f"      divergent tokens: {sorted(divergent)[:10]}")
        seen = set()
        for x in members:
            key = parse.normalize(x["title"])
            if key not in seen:
                seen.add(key)
                print(f"        ${x['price'] or 0:>9,.2f}  {x['title'][:66]}")
            if len(seen) >= 3:
                break


def bucket_report(groups, sample, rng):
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    buckets = {
        "HIGH parallel confidence": [
            k for k, v in multi.items() if v[0]["parallel_conf"] == parse.HIGH],
        "MEDIUM parallel conf (probable base)": [
            k for k, v in multi.items() if v[0]["parallel_conf"] == parse.MEDIUM],
        "No recognized parallel vocabulary": [
            k for k, v in multi.items() if v[0]["parallel"] is None],
        "Widest price spread": sorted(
            multi, key=lambda k: spread(multi[k]), reverse=True)[:sample],
        "Largest group size": sorted(
            multi, key=lambda k: len(multi[k]), reverse=True)[:sample],
    }
    findings = {}
    for name, keys in buckets.items():
        chosen = keys if len(keys) <= sample else rng.sample(keys, sample)
        print(f"\n{'=' * 70}\n  BUCKET: {name}  ({len(chosen)} of {len(keys)} groups)\n{'=' * 70}")
        divergent_count = 0
        for k in chosen:
            members = multi[k]
            div = title_divergence(members)
            if div:
                divergent_count += 1
            describe(members, div)
        findings[name] = (divergent_count, len(chosen))
        print(f"\n  -> {divergent_count}/{len(chosen)} groups have divergent titles")
    return findings


def risk_report(groups):
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\n{'=' * 70}\n  GROUPING RISK, QUANTIFIED\n{'=' * 70}")
    sizes = collections.Counter()
    for v in groups.values():
        n = len(v)
        sizes["1"] += n == 1
        sizes["2"] += n == 2
        sizes["3"] += n == 3
        sizes["4-5"] += 4 <= n <= 5
        sizes["6+"] += n >= 6
    print(f"  Eligible groups total          : {len(groups)}")
    for label in ("1", "2", "3", "4-5", "6+"):
        print(f"    size {label:4}                    : {sizes[label]}")

    par = collections.Counter()
    for v in multi.values():
        par[v[0]["parallel_conf"]] += 1
    print(f"\n  Multi-listing groups           : {len(multi)}")
    for lvl in (parse.HIGH, parse.MEDIUM, parse.LOW):
        print(f"    containing {lvl:6} parallel conf: {par.get(lvl, 0)}")

    divergent = [k for k, v in multi.items() if title_divergence(v)]
    print(f"\n  Groups with inconsistent titles : {len(divergent)} "
          f"({100*len(divergent)/max(len(multi),1):.1f}% of multi-listing groups)")

    ep = collections.Counter(epid_state(v) for v in multi.values())
    print(f"\n  ePID agrees                     : {ep['agree']}")
    print(f"  ePID conflicts                  : {ep['conflict']}")
    print(f"  ePID partial / absent           : {ep['partial']}")

    print("\n  Price spread within group:")
    sp = [spread(v) for v in multi.values()]
    for thresh in (2, 5, 10, 100):
        n = sum(1 for s in sp if s > thresh)
        print(f"    above {thresh:3}x                    : {n} "
              f"({100*n/max(len(sp),1):.1f}%)")
    return divergent, multi


def exclusion_report(conn):
    print(f"\n{'=' * 70}\n  EXCLUSION RECONCILIATION: why 4,937, not ~1,668?\n{'=' * 70}")
    titles = [r["title"] for r in conn.execute(
        "SELECT title FROM listings WHERE active = 1")]
    print(f"  Active listings now             : {len(titles)}")
    print("  Earlier estimate was taken over : 69,349 rows (partial crawl)")

    # Reproduce the earlier method: leftmost grader token wins, no digit needed.
    old = collections.Counter()
    for t in titles:
        u = parse.normalize(t)
        m = re.search(r"\b(PSA|BGS|SGC|CGC|CSG|BVG|TAG|HGA)\b", u)
        old[m.group(1) if m else "NONE"] += 1
    old_excl = sum(v for k, v in old.items() if k not in ("PSA",))
    print(f"  Earlier method re-run on {len(titles)}: {old_excl} non-PSA")
    print(f"    scaled 1,668 x {len(titles)/69349:.2f} = "
          f"{1668*len(titles)/69349:.0f} (expected)")

    now = collections.Counter()
    for t in titles:
        u = parse.normalize(t)
        rival = parse.rival_grader(u)
        has = "PSA" in u
        if rival and not has:
            now[rival] += 1
        elif rival and has:
            now[f"AMBIGUOUS PSA+{rival}"] += 1
        elif not has:
            now["NO GRADER"] += 1
    print(f"\n  Current method by grader:")
    for k, v in now.most_common():
        print(f"    {k:22} {v}")
    print(f"    {'TOTAL':22} {sum(now.values())}")

    print("\n  Falsely excluded PSA cards? (excluded titles containing 'PSA')")
    false_pos = [t for t in titles
                 if parse.rival_grader(parse.normalize(t))
                 and "PSA" not in parse.normalize(t)
                 and re.search(r"PSA", t.upper())]
    print(f"    titles excluded by a rival grader that still mention PSA: "
          f"{len(false_pos)}")
    nog = [t for t in titles if "PSA" not in parse.normalize(t)
           and not parse.rival_grader(parse.normalize(t))]
    print(f"\n  'NO GRADER' bucket ({len(nog)}) - samples:")
    for t in nog[:6]:
        print(f"      {t[:70]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    conn = db.connect(args.db)
    rng = random.Random(args.seed)
    groups = load_groups(conn)
    findings = bucket_report(groups, args.sample, rng)
    risk_report(groups)
    exclusion_report(conn)

    print(f"\n{'=' * 70}\n  BUCKET SUMMARY\n{'=' * 70}")
    for name, (bad, total) in findings.items():
        print(f"  {name:38} {bad:3}/{total:3} divergent")


if __name__ == "__main__":
    main()
