"""Read-only preview of intra-store anomaly candidates. NOT production Step 3.

This ranks listings priced below the median ASK of other listings of the same
exact slab inside the PSA store. That is an internal-consistency signal, not a
market valuation - no external comparable sale is consulted anywhere here.

Guardrails (each one is enforced in `eligible_groups`):
  - identity confidence must be MEDIUM or HIGH for every listing in the group
  - MEDIUM parallel confidence is labelled `parallel_unresolved`, never `base`
  - PSA AUTHENTIC is excluded from the model
  - missing card number / grade / year / failed parses are excluded
  - groups with divergent titles are excluded (they may mix different slabs)
  - baseline is a MEDIAN, never a mean, and excludes the candidate itself
  - extreme asks are trimmed from the baseline but never deleted from the DB
  - shipping is added to every price before comparison
  - Best Offer is recorded as a flag only; no discount is assumed
  - >=3 listings for the main model; size-2 groups are weak-signal only

Usage:  python preview_anomalies.py [--db cards.db] [--top 50]
"""

import argparse
import collections
import json
import statistics

import db
import parse
import validate_groups as vg

# A listing whose ask exceeds this multiple of the raw group median is treated
# as a data-entry artifact and dropped FROM THE BASELINE ONLY. The store
# genuinely contains $230,069 asks sitting beside $29 copies of the same card.
OUTLIER_MULTIPLE = 5.0

# Placeholder asks. Measured in this store: 3,247 listings sit at an exact round
# value, and 25.1% of all asks >= $1,000 land exactly on a $1,000 boundary
# (chance would be ~0.1%). These are "not really for sale" prices, so they are
# dropped from the baseline - never from the database.
PLACEHOLDER_FLOOR = 1000.0
PLACEHOLDER_STEP = 1000.0

# Even after trimming, comparators this far apart do not describe one coherent
# market, so the group cannot supply a baseline at all. This is what stops
# "cheapest only because the other two asked $10,005.99" from ranking.
GROUP_SPREAD_LIMIT = 10.0

# Ignore trivial gaps so cheap cards do not dominate on percentage alone.
MIN_DOLLAR_GAP = 5.0

MIN_GROUP = 3          # main model
WEAK_GROUP = 2         # surfaced separately, never a strong score

# A baseline needs at least this many surviving comparators after trimming.
# Below it the median is not robust and the candidate is dropped.
MIN_COMPARATORS = 2


def total_price(m):
    """Acquisition cost = ask + shipping. Missing shipping is treated as 0."""
    price = m["price"] or 0.0
    ship = m["shipping_cost"] or 0.0
    return price + ship, ship


def is_placeholder(ask):
    """An ask sitting exactly on a $1,000 boundary at or above $1,000."""
    return ask >= PLACEHOLDER_FLOOR and abs(ask - round(ask / PLACEHOLDER_STEP)
                                            * PLACEHOLDER_STEP) < 0.005


def clean_baseline(comparators, group_totals):
    """Median of the comparators after trimming asks that are not real offers.

    `comparators` is a list of (total, ask) for every group member except the
    candidate. Two trims apply: placeholder round-number asks, and asks far
    above the median of the WHOLE group (not of the comparator subset - a group
    of three minus the candidate can leave one sane price and one artifact,
    whose median is meaningless).

    Returns (median, n_trimmed), or (None, n_trimmed) when the survivors are too
    few or too incoherent to be a baseline.
    """
    threshold = OUTLIER_MULTIPLE * statistics.median(group_totals)
    kept = [t for t, ask in comparators
            if t <= threshold and not is_placeholder(ask)]
    trimmed = len(comparators) - len(kept)
    if len(kept) < MIN_COMPARATORS:
        return None, trimmed
    if min(kept) > 0 and max(kept) / min(kept) > GROUP_SPREAD_LIMIT:
        return None, trimmed
    return statistics.median(kept), trimmed


def eligible_groups(conn):
    """Apply every guardrail, return {slab_key: members}."""
    groups = vg.load_groups(conn)   # already MEDIUM/HIGH identity + NUMERIC grade
    rejected = collections.Counter()
    out = {}
    for k, members in groups.items():
        if len(members) < WEAK_GROUP:
            rejected["group smaller than 2"] += 1
            continue
        m0 = members[0]
        if any(x["identity_conf"] not in ("MEDIUM", "HIGH") for x in members):
            rejected["LOW identity confidence in group"] += 1
            continue
        if not m0["card_number"] or not m0["year"] or not m0["grade_value"]:
            rejected["missing card number / year / grade"] += 1
            continue
        if any(x["parallel_conf"] == parse.LOW for x in members):
            rejected["LOW parallel confidence"] += 1
            continue
        if vg.title_divergence(members):
            # Titles disagree: may mix PSA qualifiers (PSA 4 MC) or differing
            # autograph grades (PSA 9 AUTO 10), neither of which is in slab_key.
            rejected["divergent titles within group"] += 1
            continue
        if any((x["price"] or 0) <= 0 for x in members):
            rejected["non-positive price"] += 1
            continue
        out[k] = members
    return out, rejected


def build_candidates(groups, exact_size=None):
    cands = []
    for slab, members in groups.items():
        if exact_size is not None and len(members) != exact_size:
            continue
        if exact_size is None and len(members) < MIN_GROUP:
            continue
        totals = {m["item_id"]: total_price(m)[0] for m in members}
        asks = {m["item_id"]: (m["price"] or 0.0) for m in members}
        all_prices = sorted(totals.values())
        for m in members:
            mine = totals[m["item_id"]]
            others = [(v, asks[k]) for k, v in totals.items()
                      if k != m["item_id"]]
            if not others:
                continue
            if exact_size == WEAK_GROUP:
                # One comparator only: nothing can be trimmed or corroborated.
                other_total, other_ask = others[0]
                if is_placeholder(other_ask):
                    continue
                baseline, trimmed = other_total, 0
            else:
                baseline, trimmed = clean_baseline(others, all_prices)
            if baseline is None or baseline <= 0 or mine >= baseline:
                continue
            gap = baseline - mine
            if gap < MIN_DOLLAR_GAP:
                continue
            cands.append({
                "item_id": m["item_id"], "title": m["title"],
                "price": m["price"], "shipping": total_price(m)[1], "total": mine,
                "slab_key": slab, "size": len(members), "baseline": baseline,
                "pct_below": 100.0 * gap / baseline, "gap": gap,
                "gmin": all_prices[0], "gmax": all_prices[-1],
                "trimmed": trimmed, "parallel": m["parallel"],
                "parallel_conf": m["parallel_conf"],
                "identity_conf": m["identity_conf"],
                "epid": vg.epid_state(members),
                "best_offer": "BEST_OFFER" in (m["buying_option"] or ""),
                "year": m["year"], "set_name": m["set_name"],
                "card_number": m["card_number"], "grade": m["grade_value"],
            })
    cands.sort(key=lambda c: c["pct_below"], reverse=True)
    return cands


def parallel_state(c):
    if c["parallel_conf"] == parse.HIGH:
        return f"parallel_confirmed ({c['parallel']})"
    return "parallel_unresolved"


def pass_reason(c):
    bits = [f"{c['size']} active listings of this exact slab",
            f"identity={c['identity_conf']}",
            f"ePID {c['epid']}",
            f"${c['gap']:,.2f} below trimmed median of the other {c['size']-1}"]
    if c["trimmed"]:
        bits.append(f"{c['trimmed']} absurd ask(s) trimmed from baseline")
    return "; ".join(bits)


def tierb_reason(c):
    bits = []
    if c["size"] == WEAK_GROUP:
        bits.append("single comparator - baseline is one other ask, not a median; "
                    "weak signal only")
    if c["parallel_conf"] != parse.HIGH:
        bits.append("parallel never confirmed from the title - needs the "
                    "getItem Parallel/Variety aspect")
    bits.append("baseline is the median ASK inside one store, not a sold price - "
                "PSA's whole group could be above market")
    if c["best_offer"]:
        bits.append("Best Offer enabled on this listing; comparators may also "
                    "transact below ask")
    if c["epid"] != "agree":
        bits.append(f"ePID {c['epid']} - product identity not corroborated")
    return "; ".join(bits)


def show(cands, limit, heading):
    print(f"\n{'=' * 78}\n  {heading}\n{'=' * 78}")
    if not cands:
        print("  (none)")
        return
    for i, c in enumerate(cands[:limit], 1):
        print(f"\n{i:3}. {c['title'][:72]}")
        print(f"     item {c['item_id']}   slab {c['slab_key']}")
        print(f"     ask ${c['price']:,.2f} + ship ${c['shipping']:,.2f} "
              f"= TOTAL ${c['total']:,.2f}"
              f"{'   [BEST OFFER]' if c['best_offer'] else ''}")
        print(f"     group n={c['size']}  median ${c['baseline']:,.2f}  "
              f"min ${c['gmin']:,.2f}  max ${c['gmax']:,.2f}  "
              f"-> {c['pct_below']:.1f}% below median")
        print(f"     {parallel_state(c)}  |  identity={c['identity_conf']}  |  "
              f"ePID={c['epid']}")
        print(f"     passed: {pass_reason(c)}")
        print(f"     Tier B still required: {tierb_reason(c)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    conn = db.connect(args.db)
    groups, rejected = eligible_groups(conn)

    print("=" * 78)
    print("  INTRA-STORE ANOMALY PREVIEW - read-only, no external comps")
    print("=" * 78)
    print("  Groups rejected by guardrail:")
    for reason, n in rejected.most_common():
        print(f"    {reason:36} {n}")
    sizes = collections.Counter(
        "2" if len(v) == 2 else "3+" for v in groups.values())
    print(f"\n  Groups surviving guardrails      : {len(groups)}"
          f"   (size 2: {sizes['2']}, size 3+: {sizes['3+']})")

    strong = build_candidates(groups)
    weak = build_candidates(groups, exact_size=WEAK_GROUP)
    print(f"  Candidates from groups of 3+     : {len(strong)}")
    print(f"  Weak-signal candidates (size 2)  : {len(weak)}")

    show(strong, args.top, f"TOP {args.top} ANOMALY CANDIDATES (group size >= 3)")
    show(weak, 10, "WEAK-SIGNAL CANDIDATES (size-2 groups; no strong score)")

    print(f"\n{'=' * 78}")
    print("  No deal score, no BUY/WATCH/PASS, no external comparable was used.")
    print("  Every candidate above requires Tier B verification before it means")
    print("  anything about market value.")
    print("=" * 78)


if __name__ == "__main__":
    main()
