"""Conservative BUY / WATCH / PASS from accepted, priced comps only.

Pure functions, no I/O, so every threshold is directly testable.

Rules, in the order they are applied:

  1. Eligibility
     - fewer than MIN_COMPS accepted priced comps -> PASS / INSUFFICIENT_EVIDENCE
     - missing or non-positive asking price       -> PASS / NO_ASKING_PRICE
     - listing no longer active                   -> PASS / STALE_ASKING_PRICE

  2. Benchmark
     - market total = sold price + shipping, per comp
     - benchmark    = MEDIAN of those totals
     - candidate cost = item price + shipping, so both sides of the comparison
       are on the same basis. Comparing a shipping-exclusive ask against
       shipping-inclusive comps understated the ask on every candidate.
     - taxes, import duty and currency conversion are NOT part of this

  3. Classification, on discount = (median - asking) / median
     - BUY   : discount >= 25%, >= 3 priced comps, newest comp <= 12 months old
     - WATCH : 10% <= discount < 25%, >= 3 priced comps
     - PASS  : discount < 10%, at or above market, or insufficient evidence

  4. Safety guards - these only ever downgrade
     - dispersed prices (max/min > 2.5 with min > 0) -> BUY becomes WATCH
     - newest comp older than 12 months, or undated  -> BUY becomes WATCH
     - shipping unknown, so the candidate cost is a floor -> BUY becomes WATCH

review_required and rejected rows are never passed in, so they cannot move a
decision in either direction.

The result is a GROSS opportunity before taxes, import costs, marketplace fees
and resale costs. No net profit is claimed.
"""

import datetime as dt
import statistics

BUY, WATCH, PASS = "BUY", "WATCH", "PASS"

MIN_COMPS = 3
BUY_DISCOUNT = 0.25          # >= 25% below median
WATCH_DISCOUNT = 0.10        # >= 10% below median
MAX_COMP_AGE_DAYS = 365      # "no older than 12 months"
DISPERSION_LIMIT = 2.5       # max/min above this is too dispersed to trust

# Reason codes
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
NO_ASKING_PRICE = "NO_ASKING_PRICE"
STALE_ASKING_PRICE = "STALE_ASKING_PRICE"
BUY_DEEP_DISCOUNT = "BUY_DISCOUNT_AT_OR_ABOVE_25"
WATCH_MODERATE_DISCOUNT = "WATCH_DISCOUNT_10_TO_25"
PASS_SHALLOW_DISCOUNT = "PASS_DISCOUNT_BELOW_10"
PASS_AT_OR_ABOVE_MARKET = "PASS_AT_OR_ABOVE_MARKET"

# Downgrade codes
DOWNGRADED_HIGH_DISPERSION = "DOWNGRADED_HIGH_DISPERSION"
DOWNGRADED_STALE_COMPS = "DOWNGRADED_STALE_COMPS"
DOWNGRADED_INCOMPLETE_COST = "DOWNGRADED_INCOMPLETE_COST"

# Sentinel: shipping was never observed. NOT the same as free shipping.
UNKNOWN_SHIPPING = None


def candidate_cost(item_price, shipping):
    """(total, complete) on the same basis as a comp total.

    Free shipping is 0.0 and gives a complete total. Unknown shipping is None:
    the item price alone is then a FLOOR on what the card costs, so the gap it
    produces is optimistic and must never support a BUY.
    """
    if item_price is None:
        return None, False
    if shipping is None:
        return item_price, False
    return item_price + shipping, True


def confidence(n):
    if n >= 5:
        return "HIGH"
    if n >= 3:
        return "MEDIUM"
    if n >= 1:
        return "LOW"
    return "NONE"


def _parse_date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def dispersion(totals):
    """max/min, or None when it cannot be computed."""
    if len(totals) < 2 or min(totals) <= 0:
        return None
    return max(totals) / min(totals)


def decide(item_price, comps, today=None, listing_active=True, shipping=0.0):
    """Classify one candidate.

    `comps` is a list of dicts with `total_price` and optional `sale_date`.
    Callers must pass ONLY accepted, priced comps.

    `shipping` defaults to 0.0 (nothing charged). Pass None when shipping was
    never observed - that is recorded as an incomplete cost, never as free.
    """
    today = today or dt.date.today()
    cost, complete = candidate_cost(item_price, shipping)
    result = {
        "decision": PASS, "reason": INSUFFICIENT_EVIDENCE,
        "item_price": item_price, "shipping": shipping,
        "candidate_total_cost": cost, "cost_complete": complete,
        # Retained name: every consumer reads this as "the number compared
        # against the market", which is now the shipping-inclusive total.
        "asking_price": cost,
        "median_market_total": None,
        "gross_gap": None, "gross_pct": None, "comp_count": 0,
        "min_total": None, "max_total": None, "dispersion": None,
        "confidence": "NONE", "most_recent_sale": None,
        "downgrade_reason": None,
        "basis": "gross opportunity before taxes, import costs, marketplace "
                 "fees and resale costs",
    }

    totals = [c["total_price"] for c in comps
              if c.get("total_price") is not None]
    dates = sorted(d for d in (_parse_date(c.get("sale_date")) for c in comps)
                   if d is not None)
    result["comp_count"] = len(totals)
    result["confidence"] = confidence(len(totals))
    if dates:
        result["most_recent_sale"] = dates[-1].isoformat()

    if len(totals) < MIN_COMPS:
        return result                        # PASS / INSUFFICIENT_EVIDENCE

    median = statistics.median(totals)
    result.update({"median_market_total": median, "min_total": min(totals),
                   "max_total": max(totals), "dispersion": dispersion(totals)})

    if cost is None or cost <= 0:
        result["reason"] = NO_ASKING_PRICE
        return result
    if not listing_active:
        result["reason"] = STALE_ASKING_PRICE
        return result

    gap = median - cost
    pct = gap / median if median else 0.0
    result["gross_gap"] = gap
    result["gross_pct"] = pct * 100

    if pct >= BUY_DISCOUNT:
        result["decision"], result["reason"] = BUY, BUY_DEEP_DISCOUNT
        # Guards may only downgrade, never upgrade.
        newest = dates[-1] if dates else None
        stale = newest is None or (today - newest).days > MAX_COMP_AGE_DAYS
        spread = result["dispersion"]
        if spread is not None and spread > DISPERSION_LIMIT:
            result["decision"] = WATCH
            result["downgrade_reason"] = DOWNGRADED_HIGH_DISPERSION
        elif stale:
            result["decision"] = WATCH
            result["downgrade_reason"] = DOWNGRADED_STALE_COMPS
        elif not complete:
            # The gap was computed from a floor on the true cost.
            result["decision"] = WATCH
            result["downgrade_reason"] = DOWNGRADED_INCOMPLETE_COST
    elif pct >= WATCH_DISCOUNT:
        result["decision"], result["reason"] = WATCH, WATCH_MODERATE_DISCOUNT
    elif pct > 0:
        result["decision"], result["reason"] = PASS, PASS_SHALLOW_DISCOUNT
    else:
        result["decision"], result["reason"] = PASS, PASS_AT_OR_ABOVE_MARKET
    return result


def format_decision(d):
    """One readable block per candidate."""
    lines = [f"    DECISION       : {d['decision']}   reason={d['reason']}"]
    if d["downgrade_reason"]:
        lines.append(f"    downgraded     : {d['downgrade_reason']}")
    money = lambda v: "n/a" if v is None else f"${v:,.2f}"
    ship = ("unknown" if d.get("shipping") is None
            else f"${d['shipping']:,.2f}"
            if d.get("shipping") else "free")
    lines.append(f"    item price     : {money(d.get('item_price'))}"
                 f"   shipping {ship}")
    lines.append(f"    TOTAL COST     : {money(d.get('candidate_total_cost'))}"
                 + ("" if d.get("cost_complete", True)
                    else "   INCOMPLETE - shipping unknown, this is a floor"))
    if d["median_market_total"] is not None:
        lines.append(f"    median market  : ${d['median_market_total']:,.2f}"
                     f"   min ${d['min_total']:,.2f}"
                     f"   max ${d['max_total']:,.2f}")
    if d["gross_gap"] is not None:
        word = "discount to market" if d["gross_gap"] > 0 else (
            "premium over market" if d["gross_gap"] < 0 else "at market")
        lines.append(f"    gross gap      : ${d['gross_gap']:,.2f} "
                     f"({d['gross_pct']:+.1f}%)  {word}")
    lines.append(f"    priced comps   : {d['comp_count']}"
                 f"   confidence {d['confidence']}"
                 + (f"   dispersion {d['dispersion']:.2f}x"
                    if d["dispersion"] else ""))
    lines.append(f"    newest comp    : {d['most_recent_sale'] or 'unknown'}")
    lines.append(f"    basis          : {d['basis']}")
    return "\n".join(lines)
