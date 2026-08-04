"""Step 1: fetch active sports card listings from one or more eBay sellers.

Sports-only comes from the category filter, not keywords: 212 is eBay's
"Sports Trading Cards" tree. Pokemon and the other TCGs sit under Collectible
Card Games (183050), so they are excluded structurally.

The Browse API stops paging at 10,000 results per query, so the store is walked
in price bands that subdivide until each fits under the cap.

Seller identity is discovery provenance only. It is persisted per listing and
never takes part in card or slab identity, so the same slab offered by two
sellers is two listings inside one group - never one merged record.

Usage:
  python fetch_listings.py                      # the psa store, as before
  python fetch_listings.py --sellers a b c      # a bounded multi-seller pilot
  python fetch_listings.py --sellers a --max-per-seller 500
"""

import argparse
import collections
import datetime as dt
import math
import sys
import time

import db
import ebay_api

DEFAULT_SELLERS = ("psa",)
SPORTS_CATEGORY = "212"
# 261328 is "Trading Card Singles". Its siblings under 212 are Lots (261329),
# Sets (261330) and Sealed Boxes (261332) - none of which is a single graded
# card. The PSA store listed only singles, so this was previously an audit
# check; across sellers it has to be an actual filter.
SINGLES_LEAF = "261328"
EXPECTED_LEAF_CATEGORY = SINGLES_LEAF

PRICE_BANDS = [(0, 25), (25, 100), (100, 500), (500, 5000), (5000, None)]

# Prices cluster on round values ($9.99, $19.99), so a band can stop being
# splittable before it fits. Below this width we give up and report it.
MIN_BAND_WIDTH = 0.02


class Stats:
    def __init__(self):
        self.api_total = 0          # sum of API-reported totals across leaf bands
        self.fetched_rows = 0       # item summaries received, including repeats
        self.leaf_bands = 0
        self.max_leaf_total = 0
        self.truncated = []
        self.seller_violations = []
        self.missing_seller = []
        self.category_violations = []
        self.per_seller = collections.Counter()
        self.retained_per_seller = collections.Counter()
        self.capped = set()
        self.non_singles = collections.Counter()
        self.seen = set()

    @property
    def duplicates(self):
        return self.fetched_rows - len(self.seen)


def band_filters(low, high, sellers):
    """Browse filter for one price band, restricted to the allowlist.

    eBay accepts several usernames in one `sellers:{a|b|c}` clause, so the band
    walk stays a single traversal rather than one per seller.
    """
    price = f"[{low}..{high}]" if high is not None else f"[{low}]"
    names = "|".join(sorted(sellers))
    return [f"sellers:{{{names}}}", f"price:{price}", "priceCurrency:USD"]


# --------------------------------------------------------------------------
# Ending-window construction for auction discovery
# --------------------------------------------------------------------------
# `sort=endingSoonest` alone cannot define a 24-hour cohort: the probe's first
# page was already ending within 60 seconds, and 2.4 million auctions match the
# base filter, so shallow pagination would only ever see the next few seconds.
# An absolute bounded range is required.
#
# VERIFIED LIVE 2026-08-02: a window displaced to preflight+12h..+36h returned
# ten auctions all ending at +12.00h exactly, the window's lower bound, where an
# unfiltered endingSoonest query returns items ending in seconds. The response
# carried no warning naming itemEndDate. The filter is enforced.
AUCTION_FILTER = "buyingOptions:{AUCTION}"
ENDING_SOONEST = "endingSoonest"


def iso_utc(moment):
    """Browse-API timestamp: ISO-8601 UTC, milliseconds, trailing Z.

    A naive datetime is rejected rather than assumed local: silently reading
    17:00 as 14:00Z would shift the whole ending window by the machine's offset
    and quietly research the wrong auctions.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("ending-window timestamps must be timezone-aware UTC")
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ending_window_filter(start, hours=24):
    """`itemEndDate:[start..start+hours]` as the API documents ranges."""
    end = start + dt.timedelta(hours=hours)
    return f"itemEndDate:[{iso_utc(start)}..{iso_utc(end)}]"


def auction_filters(start, hours=24, price_min=None, price_max=None):
    """Every filter clause for one bounded ending-soon auction query.

    priceCurrency is emitted only alongside a price clause. Sent on its own it
    is rejected outright - observed warnings 12002 and 12012 - and rejected
    silently, so a query that looked currency-restricted was not. Without a
    price band the currency is enforced in the application layer instead, by
    `db.is_usd`, which refuses to value a non-USD listing at all.
    """
    out = [AUCTION_FILTER, ending_window_filter(start, hours)]
    if price_min is not None or price_max is not None:
        lo = 0 if price_min is None else price_min
        out.append(f"price:[{lo}..{price_max}]" if price_max is not None
                   else f"price:[{lo}]")
        out.append("priceCurrency:USD")
    return out


class QueryRejected(RuntimeError):
    """eBay accepted the request but rejected part of the query."""


def assert_query_accepted(response, filters=()):
    """Raise unless the response carries no warnings and no errors.

    A rejected filter still returns HTTP 200 with a full result set, which is
    indistinguishable from success unless the warnings are read. Gate C's
    correctness rests entirely on itemEndDate being applied, so any warning
    fails the pilot rather than being logged and passed over.
    """
    errors = response.get("errors") or []
    if errors:
        raise QueryRejected(f"eBay returned errors: {_describe(errors)}")
    warnings = response.get("warnings") or []
    if warnings:
        raise QueryRejected(
            f"eBay warned about this query, so the filters cannot be assumed "
            f"applied: {_describe(warnings)} (filters sent: {list(filters)})")


def _describe(entries):
    return "; ".join(
        f"{e.get('errorId')} {e.get('message')}" for e in entries)


def usd_only(items):
    """Split discovery results into USD and non-USD. Never converts."""
    keep = [i for i in items if db.is_usd(i)]
    return keep, [i for i in items if not db.is_usd(i)]


def label_for(low, high):
    return f"${low:g}-${high:g}" if high is not None else f"${low:g}+"


def split_point(low, high):
    """Geometric midpoint - card prices skew low, so this splits more evenly."""
    mid = math.sqrt(low * high) if low > 0 else high / 2
    return round(mid, 2)


def audit(item, stats, sellers):
    """Verify each item really is an allowlisted seller's sports card.

    A missing or blank username is recorded as a violation and left NULL in the
    database rather than attributed to whichever seller we were crawling.
    """
    username = (item.get("seller") or {}).get("username")
    if not username:
        stats.missing_seller.append(item.get("itemId"))
    elif username not in sellers:
        stats.seller_violations.append((item.get("itemId"), username))
    else:
        stats.per_seller[username] += 1
    leaves = item.get("leafCategoryIds") or []
    if EXPECTED_LEAF_CATEGORY not in leaves:
        stats.category_violations.append((item.get("itemId"), leaves))


def walk_band(conn, token, low, high, fetched_at, stats, sellers,
              run_id=None, max_per_seller=None):
    """Fetch one price band, subdividing it if it exceeds the result cap."""
    label = label_for(low, high)
    filters = band_filters(low, high, sellers)
    first = ebay_api.search_page(token, filters, SPORTS_CATEGORY, 0)
    total = first.get("total", 0)
    if not total:
        return

    if total > ebay_api.MAX_OFFSET:
        if high is not None and (high - low) > MIN_BAND_WIDTH:
            mid = split_point(low, high)
            if low < mid < high:
                walk_band(conn, token, low, mid, fetched_at, stats, sellers,
                          run_id, max_per_seller)
                walk_band(conn, token, mid, high, fetched_at, stats, sellers,
                          run_id, max_per_seller)
                return
        stats.truncated.append((label, total))

    stats.leaf_bands += 1
    stats.api_total += total
    stats.max_leaf_total = max(stats.max_leaf_total, total)

    new = 0
    for items, _total, _trunc in ebay_api.search_all(
        token, filters, SPORTS_CATEGORY
    ):
        stats.fetched_rows += len(items)
        rows = []
        for item in items:
            audit(item, stats, sellers)
            item_id = item.get("itemId")
            leaves = item.get("leafCategoryIds") or []
            if SINGLES_LEAF not in leaves:
                # A lot, a set or a sealed box - not a single graded card.
                stats.non_singles[leaves[0] if leaves else "unknown"] += 1
                continue
            # An item seen again in an overlapping band, or in a re-run, is the
            # same listing: upserted by primary key, never a second record.
            if item_id in stats.seen:
                continue
            username = (item.get("seller") or {}).get("username")
            if (max_per_seller is not None and username
                    and stats.retained_per_seller[username] >= max_per_seller):
                stats.capped.add(username)
                continue
            stats.seen.add(item_id)
            if username:
                stats.retained_per_seller[username] += 1
            rows.append(db.to_row(item, fetched_at, run_id))
        db.upsert_listings(conn, rows)
        new += len(rows)
    flag = "  [CAPPED]" if total > ebay_api.MAX_OFFSET else ""
    print(f"  {label:>18}  {new:>6} new / {total:>6} reported{flag}")


def bands_within(price_min, price_max):
    """The default bands clipped to a bounded pilot range."""
    if price_min is None and price_max is None:
        return list(PRICE_BANDS)
    lo = 0 if price_min is None else price_min
    out = []
    for low, high in PRICE_BANDS:
        top = high
        if price_max is not None and (top is None or top > price_max):
            top = price_max
        start = max(low, lo)
        if top is not None and start >= top:
            continue
        if price_max is not None and start >= price_max:
            continue
        out.append((start, top))
    return out


def fetch_all(conn, token, fetched_at, sellers, run_id=None,
              max_per_seller=None, price_min=None, price_max=None):
    stats = Stats()
    for low, high in bands_within(price_min, price_max):
        walk_band(conn, token, low, high, fetched_at, stats, sellers, run_id,
                  max_per_seller)
    return stats


def report(conn, stats, elapsed, deactivated):
    print("\n" + "=" * 62)
    print("STEP 1 RUN REPORT")
    print("=" * 62)
    print(f"  API-reported results (sum of leaf bands) : {stats.api_total}")
    print(f"  Rows fetched (incl. repeats)             : {stats.fetched_rows}")
    print(f"  Unique item IDs                          : {len(stats.seen)}")
    print(f"  Duplicates discarded                     : {stats.duplicates}")
    print(f"  Leaf price bands                         : {stats.leaf_bands}")
    print(f"  Largest leaf band                        : {stats.max_leaf_total}"
          f" (cap {ebay_api.MAX_OFFSET})")
    print(f"  API requests                             : {ebay_api.request_count}")
    print(f"  Elapsed                                  : {elapsed:.1f}s")

    active = conn.execute("SELECT COUNT(*) FROM listings WHERE active = 1").fetchone()[0]
    total_rows = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"  Rows in cards.db                         : {total_rows} "
          f"({active} active, {total_rows - active} inactive)")
    print(f"  Deactivated this run (no longer listed)  : {deactivated}")

    if stats.per_seller:
        print("\n  Per seller (observed / retained):")
        for name in sorted(stats.per_seller):
            cap = "  [CAP REACHED]" if name in stats.capped else ""
            print(f"    {name:28} {stats.per_seller[name]:>6} / "
                  f"{stats.retained_per_seller[name]:>6}{cap}")

    print("\n  Buying options:")
    for r in conn.execute(
        """SELECT buying_option, COUNT(*) n FROM listings
           WHERE active = 1 GROUP BY 1 ORDER BY n DESC"""
    ):
        print(f"    {r['buying_option']:26} {r['n']}")

    print("\n  Verification:")
    ok = "PASS"
    if stats.seller_violations:
        ok = "FAIL"
        print(f"    seller allowlist       : FAIL - {len(stats.seller_violations)} "
              f"foreign, e.g. {stats.seller_violations[:3]}")
    else:
        print(f"    seller allowlist       : PASS - all {len(stats.seen)} items")
    if stats.missing_seller:
        ok = "FAIL"
        print(f"    seller identity        : FAIL - {len(stats.missing_seller)} "
              f"item(s) with no username; stored as NULL, never attributed")
    else:
        print(f"    seller identity        : PASS - every item named a seller")
    if stats.non_singles:
        print(f"    singles-only filter    : {sum(stats.non_singles.values())} "
              f"non-single listing(s) skipped {dict(stats.non_singles)}")
    if stats.category_violations:
        kept = len(stats.category_violations) - sum(stats.non_singles.values())
        if kept > 0:
            ok = "FAIL"
            print(f"    leaf category {SINGLES_LEAF}   : FAIL - {kept} retained "
                  f"outside the singles leaf")
        else:
            print(f"    leaf category {SINGLES_LEAF}   : PASS - every retained "
                  f"item is a single")
    else:
        print(f"    leaf category {SINGLES_LEAF}   : PASS - all items")
    if stats.max_leaf_total > ebay_api.MAX_OFFSET:
        ok = "FAIL"

    if stats.truncated:
        missed = sum(t - ebay_api.MAX_OFFSET for _, t in stats.truncated)
        print(f"\n  COVERAGE GAP - {len(stats.truncated)} band(s) could not be split "
              f"below the cap;\n  roughly {missed} listings unreachable:")
        for label, total in stats.truncated:
            print(f"    {label}: {total} reported, {ebay_api.MAX_OFFSET} reachable")
    else:
        print("\n  COVERAGE: complete - every leaf band is under the 10,000 cap.")

    print(f"\n  RESULT: {ok}")
    print("=" * 62)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--no-deactivate", action="store_true",
                        help="skip marking unseen listings inactive")
    parser.add_argument("--sellers", nargs="+", default=list(DEFAULT_SELLERS),
                        help="eBay usernames to crawl (default: the psa store)")
    parser.add_argument("--max-per-seller", type=int,
                        help="retain at most N new listings per seller")
    parser.add_argument("--price-min", type=float,
                        help="lower bound for a bounded pilot")
    parser.add_argument("--price-max", type=float,
                        help="upper bound for a bounded pilot")
    args = parser.parse_args()
    sellers = sorted({s.strip() for s in args.sellers if s and s.strip()})
    if not sellers:
        sys.exit("error: --sellers needs at least one username")

    try:
        token = ebay_api.get_token()
    except ebay_api.EbayError as exc:
        sys.exit(f"error: {exc}")

    conn = db.connect(args.db)
    run_start = dt.datetime.now(dt.timezone.utc).isoformat()
    started = time.monotonic()
    run_id = f"disc-{run_start[:19].replace(':', '').replace('-', '')}"
    print(f"Fetching sports cards from {len(sellers)} seller(s): "
          f"{', '.join(sellers)}  (category {SPORTS_CATEGORY})")
    if args.max_per_seller:
        print(f"Bounded pilot: at most {args.max_per_seller} new listings per seller")
    print(f"Discovery run id: {run_id}\n")

    try:
        stats = fetch_all(conn, token, run_start, sellers, run_id,
                          args.max_per_seller, args.price_min, args.price_max)
    except (ebay_api.EbayError, KeyboardInterrupt) as exc:
        conn.commit()
        sys.exit(
            f"\nrun aborted ({type(exc).__name__}: {exc}).\n"
            "Rows fetched so far are kept; nothing was deactivated."
        )

    # Only prune after a clean crawl - a partial run would wrongly deactivate
    # listings that are still live.
    deactivated = 0
    if not args.no_deactivate:
        # Scoped to the sellers this run crawled: a pilot against new
        # sellers must never retire the existing population.
        deactivated = db.deactivate_stale(conn, run_start, sellers)

    report(conn, stats, time.monotonic() - started, deactivated)


if __name__ == "__main__":
    main()
