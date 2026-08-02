"""Ranked economic report over every researched candidate. No live calls.

Built from persisted rows only: `listings` for the ask, `sold_comps` for the
market, `pr_runs` for research state. It reports; it never decides. Every
decision here comes from `decision.decide`, unchanged.

Three populations, kept apart because mixing them is what made the first
hand-rolled version of this report self-contradictory:

  valued        >= MIN_COMPS priced comps. The engine issues a real decision and
                a real gap. Only these may be called BUY, WATCH or PASS on merit.
  benchmark_only 1-2 priced comps. A median exists, so a markup can be shown,
                but the engine refuses to value it: the decision is PASS for
                INSUFFICIENT_EVIDENCE and the gap is undefined. A markup here is
                an observation, not a verdict.
  no_benchmark  no priced comps at all. Nothing to compare against.

Usage:  python final_report.py [--out final_economic_report.json]
"""

import argparse
import collections
import json
import statistics
import datetime as dt

import db
import decision as dec
import enrich
import manual_comps as mc

# markup_pct = (candidate_total_cost / median_comp_total - 1) * 100
#   negative -> priced BELOW market
#   zero     -> at market
#   positive -> priced ABOVE market
MARKUP_FORMULA = "(candidate_total_cost / median_comp_total - 1) * 100"

# Upper edges, in order. Bands are half-open (prev, edge] so they cannot overlap.
BAND_EDGES = (0, 5, 10, 15, 25, 50)
DISCOUNTS = (0, 5, 10, 15, 20, 25)


def cohort_of(c):
    if c["parallel"] or c["print_run"]:
        return "parallel/serial"
    if c["is_auto"] or c["auto_grade"]:
        return "auto"
    if c["qualifier"]:
        return "qualifier"
    return "base"


def markup_pct(cost, median):
    if not median:
        return None
    return (cost / median - 1) * 100


def band_of(markup):
    """Exclusive band label for one markup. None only when markup is None."""
    if markup is None:
        return None
    if markup <= 0:
        return "at or below market"
    prev = 0
    for edge in BAND_EDGES[1:]:
        if markup <= edge:
            return f"above market {prev}-{edge}%"
        prev = edge
    return f"above market over {BAND_EDGES[-1]}%"


def band_labels():
    out = ["at or below market"]
    prev = 0
    for edge in BAND_EDGES[1:]:
        out.append(f"above market {prev}-{edge}%")
        prev = edge
    out.append(f"above market over {BAND_EDGES[-1]}%")
    return out


def rows_for(conn, pool, cands):
    """One record per candidate, straight from persisted data."""
    out = []
    for cid, c in pool.items():
        acc = conn.execute(
            """SELECT total_price, sale_date FROM sold_comps
               WHERE candidate_item_id = ? AND accepted = 1""", (cid,)).fetchall()
        listing = conn.execute(
            "SELECT active, price, shipping_cost FROM listings WHERE item_id = ?",
            (cid,)).fetchone()
        run = conn.execute(
            "SELECT status FROM pr_runs WHERE candidate_id = ?", (cid,)).fetchone()
        cost, complete = dec.candidate_cost(listing["price"],
                                            listing["shipping_cost"])
        priced = [r["total_price"] for r in acc if r["total_price"] is not None]
        median = statistics.median(priced) if priced else None
        d = dec.decide(listing["price"],
                       [{"total_price": r["total_price"], "sale_date": r["sale_date"]}
                        for r in acc],
                       shipping=listing["shipping_cost"],
                       listing_active=bool(listing["active"]))
        mk = markup_pct(cost, median)
        population = ("valued" if len(priced) >= dec.MIN_COMPS
                      else "benchmark_only" if priced else "no_benchmark")
        out.append({
            "candidate_id": cid, "title": c["title"],
            "canonical_identity": c["effective_identity"],
            "effective_slab_key": c["effective_slab_key"],
            "cohort": cohort_of(c), "population": population,
            "item_price": listing["price"], "shipping": listing["shipping_cost"],
            "total_candidate_cost": cost, "cost_complete": complete,
            "accepted_comps": len(priced), "median_comp_total": median,
            "confidence": d["confidence"],
            # Defined only where the engine issues one. A gap on 1-2 comps
            # would read as a verdict the engine explicitly declines to give.
            "gap_absolute": d["gross_gap"],
            "gap_percent": d["gross_pct"],
            "markup_percent": mk,
            "cost_to_market_ratio": (cost / median) if median else None,
            "markup_band": band_of(mk),
            "decision": d["decision"], "reason": d["reason"],
            "downgrade_reason": d["downgrade_reason"],
            "run_status": run["status"] if run else None,
        })
    return out


def hypothetical(rows, discounts=DISCOUNTS):
    """What the decision would be at a seller discount. Modelled only.

    Includes the 0% row deliberately: without today's real counts beside them,
    the discounted counts read as a contradiction of the headline.
    """
    out = {}
    valued = [r for r in rows if r["population"] == "valued"]
    for disc in discounts:
        counts = collections.Counter()
        for r in valued:
            cost = r["total_candidate_cost"] * (1 - disc / 100)
            pct = (r["median_comp_total"] - cost) / r["median_comp_total"]
            if pct >= dec.BUY_DISCOUNT:
                counts[dec.BUY] += 1
            elif pct >= dec.WATCH_DISCOUNT:
                counts[dec.WATCH] += 1
            else:
                counts[dec.PASS] += 1
        out["actual" if disc == 0 else f"{disc}%"] = {
            "discount_pct": disc, "BUY": counts[dec.BUY],
            "WATCH": counts[dec.WATCH], "PASS": counts[dec.PASS]}
    return out


def build(conn):
    enrich.load_surnames(conn)
    pool = {c["item_id"]: c for c in json.load(open(mc.CANDIDATES))}
    rows = rows_for(conn, pool, mc.load_candidates())
    valued = sorted([r for r in rows if r["population"] == "valued"],
                    key=lambda r: r["markup_percent"])
    bench = sorted([r for r in rows if r["population"] == "benchmark_only"],
                   key=lambda r: r["markup_percent"])
    none_ = [r for r in rows if r["population"] == "no_benchmark"]

    bands = collections.Counter(r["markup_band"] for r in valued)
    decisions = collections.Counter(r["decision"] for r in rows)
    m = [r["markup_percent"] for r in valued]
    by_cohort = {}
    for coh in sorted({r["cohort"] for r in valued}):
        g = [r["markup_percent"] for r in valued if r["cohort"] == coh]
        by_cohort[coh] = {"valued": len(g), "median_markup_pct": statistics.median(g),
                          "min_markup_pct": min(g), "max_markup_pct": max(g)}
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "markup_formula": MARKUP_FORMULA,
        "headline": {
            "candidates": len(rows),
            "BUY": decisions[dec.BUY], "WATCH": decisions[dec.WATCH],
            "PASS": decisions[dec.PASS],
            "valued": len(valued), "benchmark_only": len(bench),
            "no_benchmark": len(none_),
            "without_sufficient_benchmark": len(bench) + len(none_),
            "above_market_over_50pct": bands[f"above market over {BAND_EDGES[-1]}%"],
            "at_or_below_market": bands["at or below market"],
        },
        "markup_bands": {k: bands.get(k, 0) for k in band_labels()},
        "markup_quartiles_pct": ([sorted(m)[len(m) // 4], statistics.median(m),
                                  sorted(m)[3 * len(m) // 4]] if m else []),
        "by_cohort": by_cohort,
        "hypothetical_discount": hypothetical(rows),
        "ranked_by_markup": valued,
        "benchmark_only": bench,
        "no_benchmark": none_,
    }


def check(report):
    """Internal-consistency assertions. Returns a list of problems."""
    h, bad = report["headline"], []
    valued = report["ranked_by_markup"]
    if sum(report["markup_bands"].values()) != len(valued):
        bad.append("markup bands do not total the valued population")
    if h["BUY"] + h["WATCH"] + h["PASS"] != h["candidates"]:
        bad.append("decision counts do not total the candidate population")
    if h["valued"] + h["benchmark_only"] + h["no_benchmark"] != h["candidates"]:
        bad.append("populations do not total the candidate population")
    for i in range(len(valued) - 1):
        if valued[i]["markup_percent"] > valued[i + 1]["markup_percent"]:
            bad.append("ranking is not monotonic")
            break
    for r in valued:
        if r["gap_absolute"] is None:
            bad.append(f"valued candidate without a gap: {r['candidate_id']}")
    for r in report["benchmark_only"]:
        if r["gap_absolute"] is not None:
            bad.append(f"benchmark_only candidate with a gap: {r['candidate_id']}")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--out", default="final_economic_report.json")
    args = ap.parse_args()

    report = build(db.connect(args.db))
    problems = check(report)
    h = report["headline"]
    print("=" * 78)
    print("  FINAL ECONOMIC REPORT")
    print("=" * 78)
    print(f"  candidates {h['candidates']}   BUY {h['BUY']}   WATCH {h['WATCH']}"
          f"   PASS {h['PASS']}")
    print(f"  valued {h['valued']}   benchmark only {h['benchmark_only']}"
          f"   no benchmark {h['no_benchmark']}")
    print(f"  at or below market {h['at_or_below_market']}"
          f"   more than 50% above {h['above_market_over_50pct']}")
    print(f"\n  markup = {MARKUP_FORMULA}")
    for k, v in report["markup_bands"].items():
        print(f"    {k:28} {v:>3}")
    print("\n  hypothetical seller discount (modelled; thresholds unchanged)")
    for k, v in report["hypothetical_discount"].items():
        print(f"    {k:>7}  BUY {v['BUY']:>2}  WATCH {v['WATCH']:>2}  PASS {v['PASS']:>3}")
    print(f"\n  consistency: {'OK' if not problems else 'PROBLEMS'}")
    for p in problems:
        print(f"    !! {p}")
    json.dump(report, open(args.out, "w"), indent=1, default=str)
    print(f"  written -> {args.out}")


if __name__ == "__main__":
    main()
