"""Step 1: fetch every active sports card listing from the PSA eBay store.

Sports-only comes from the category filter, not keywords: 212 is eBay's
"Sports Trading Cards" tree. Pokemon and the other TCGs sit under Collectible
Card Games (183050), so they are excluded structurally.

The Browse API stops paging at 10,000 results per query, so the store is walked
in price bands that subdivide until each fits under the cap.

Usage:  EBAY_APP_ID=... EBAY_CERT_ID=... python fetch_listings.py
"""

import argparse
import datetime as dt
import math
import sys
import time

import db
import ebay_api

SELLER = "psa"
SPORTS_CATEGORY = "212"
EXPECTED_LEAF_CATEGORY = "261328"

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
        self.category_violations = []
        self.seen = set()

    @property
    def duplicates(self):
        return self.fetched_rows - len(self.seen)


def band_filters(low, high):
    price = f"[{low}..{high}]" if high is not None else f"[{low}]"
    return [f"sellers:{{{SELLER}}}", f"price:{price}", "priceCurrency:USD"]


def label_for(low, high):
    return f"${low:g}-${high:g}" if high is not None else f"${low:g}+"


def split_point(low, high):
    """Geometric midpoint - card prices skew low, so this splits more evenly."""
    mid = math.sqrt(low * high) if low > 0 else high / 2
    return round(mid, 2)


def audit(item, stats):
    """Verify each item really is a PSA-store sports card."""
    username = (item.get("seller") or {}).get("username")
    if username != SELLER:
        stats.seller_violations.append((item.get("itemId"), username))
    leaves = item.get("leafCategoryIds") or []
    if EXPECTED_LEAF_CATEGORY not in leaves:
        stats.category_violations.append((item.get("itemId"), leaves))


def walk_band(conn, token, low, high, fetched_at, stats):
    """Fetch one price band, subdividing it if it exceeds the result cap."""
    label = label_for(low, high)
    first = ebay_api.search_page(token, band_filters(low, high), SPORTS_CATEGORY, 0)
    total = first.get("total", 0)
    if not total:
        return

    if total > ebay_api.MAX_OFFSET:
        if high is not None and (high - low) > MIN_BAND_WIDTH:
            mid = split_point(low, high)
            if low < mid < high:
                walk_band(conn, token, low, mid, fetched_at, stats)
                walk_band(conn, token, mid, high, fetched_at, stats)
                return
        stats.truncated.append((label, total))

    stats.leaf_bands += 1
    stats.api_total += total
    stats.max_leaf_total = max(stats.max_leaf_total, total)

    new = 0
    for items, _total, _trunc in ebay_api.search_all(
        token, band_filters(low, high), SPORTS_CATEGORY
    ):
        stats.fetched_rows += len(items)
        rows = []
        for item in items:
            audit(item, stats)
            if item.get("itemId") not in stats.seen:
                stats.seen.add(item.get("itemId"))
                rows.append(db.to_row(item, fetched_at))
        db.upsert_listings(conn, rows)
        new += len(rows)
    flag = "  [CAPPED]" if total > ebay_api.MAX_OFFSET else ""
    print(f"  {label:>18}  {new:>6} new / {total:>6} reported{flag}")


def fetch_all(conn, token, fetched_at):
    stats = Stats()
    for low, high in PRICE_BANDS:
        walk_band(conn, token, low, high, fetched_at, stats)
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
        print(f"    seller == '{SELLER}'   : FAIL - {len(stats.seller_violations)} "
              f"foreign, e.g. {stats.seller_violations[:3]}")
    else:
        print(f"    seller == '{SELLER}'   : PASS - all {len(stats.seen)} items")
    if stats.category_violations:
        ok = "FAIL"
        print(f"    leaf category {EXPECTED_LEAF_CATEGORY} : FAIL - "
              f"{len(stats.category_violations)} outside, "
              f"e.g. {stats.category_violations[:3]}")
    else:
        print(f"    leaf category {EXPECTED_LEAF_CATEGORY} : PASS - all items")
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
    args = parser.parse_args()

    try:
        token = ebay_api.get_token()
    except ebay_api.EbayError as exc:
        sys.exit(f"error: {exc}")

    conn = db.connect(args.db)
    run_start = dt.datetime.now(dt.timezone.utc).isoformat()
    started = time.monotonic()
    print(f"Fetching sports cards from eBay store '{SELLER}' "
          f"(category {SPORTS_CATEGORY})\n")

    try:
        stats = fetch_all(conn, token, run_start)
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
        deactivated = db.deactivate_stale(conn, run_start)

    report(conn, stats, time.monotonic() - started, deactivated)


if __name__ == "__main__":
    main()
