"""Bounded Gate C discovery: ending-soon auctions, price-banded, research-only.

Discovery only. This module never bids, buys, offers, watches, contacts a
seller or modifies a listing, and it never calls `deactivate_stale` - the
existing fixed-price population is not part of this run and must not be
retired by it.
"""

import argparse
import datetime as dt
import json

import db
import ebay_api
import fetch_listings as fl
import gate_c_plan as plan

PAGE_SIZE = 200


def discover(token, start, served, run_id, max_requests=plan.MAX_SEARCH_REQUESTS,
             max_items=plan.MAX_DISCOVERY_ITEMS, page_size=PAGE_SIZE):
    """Page the served bands. Returns (items, report)."""
    items, seen = [], set()
    report = {"queries": [], "excluded": [], "requests": 0,
              "non_usd_excluded": 0, "duplicate_in_run": 0,
              "non_singles_excluded": 0}
    queue = [(b["band"][0], b["band"][1], b["pages"]) for b in served]
    while queue:
        low, high, pages = queue.pop(0)
        if report["requests"] >= max_requests or len(items) >= max_items:
            report["excluded"].append({"band": [low, high],
                                       "reason": "request_or_item_cap_reached"})
            continue
        filters = fl.auction_filters(start, price_min=low, price_max=high)
        report["requests"] += 1
        body = ebay_api.search_page(token, filters, fl.SPORTS_CATEGORY, 0,
                                    sort=fl.ENDING_SOONEST, limit=page_size)
        # Any warning means a filter may not have been applied, and the whole
        # pilot rests on itemEndDate being applied. Fail the query, do not log
        # past it.
        fl.assert_query_accepted(body, filters)
        total = body.get("total", 0)
        page = body.get("itemSummaries") or []
        entry = {"band": [low, high], "total": total, "returned": len(page),
                 "filters": filters}
        if total > plan.PAGING_CEILING:
            pieces, unpageable = plan.split_band(low, high, total)
            entry["over_ceiling"] = True
            entry["split_into"] = len(pieces)
            for lo2, hi2 in pieces:
                queue.append((lo2, hi2, pages))
            for band in unpageable:
                report["excluded"].append({"band": list(band[:2]),
                                           "count": band[2],
                                           "reason": plan.BAND_INCOMPLETE})
            report["queries"].append(entry)
            continue
        # Lots, sets and sealed boxes all sit under category 212 alongside
        # singles. The fixed-price crawl queries 212 and screens on the leaf,
        # and a graded-single pilot must not silently admit a lot.
        singles = [it for it in page
                   if fl.EXPECTED_LEAF_CATEGORY in (it.get("leafCategoryIds") or [])]
        report["non_singles_excluded"] += len(page) - len(singles)
        usd, other = fl.usd_only(singles)
        report["non_usd_excluded"] += len(other)
        for it in usd:
            if it.get("itemId") in seen:
                report["duplicate_in_run"] += 1
                continue
            if len(items) >= max_items:
                break
            seen.add(it.get("itemId"))
            items.append(it)
        report["queries"].append(entry)
    return items, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", action="store_true",
                    help="print the plan and exit without any network call")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out", default="gateC_pilot_discovery.json")
    args = ap.parse_args(argv)

    T0 = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    # Displaced by the proven 12-hour lead: a window starting at T0 is rejected
    # outright (warning 12002), and an auction ending within minutes could not
    # be researched in time to be a useful candidate anyway.
    start, end = plan.displaced_window(T0)
    plan.assert_window_acceptable(start, T0)
    bands, pre_excluded = plan.split_band(plan.BAND_MIN, plan.BAND_MAX, 333_296)
    served = plan.plan_requests(bands)
    run_id = "gateC_" + fl.iso_utc(T0).replace(":", "").replace("-", "")

    print(f"T0 {fl.iso_utc(T0)}  window {fl.iso_utc(start)} .. {fl.iso_utc(end)}")
    print(f"bands {len(bands)}  served {len(served)}  run_id {run_id}")
    if not args.run:
        return 0

    token = ebay_api.get_token()
    items, report = discover(token, start, served, run_id)
    print(f"discovery: {len(items)} items in {report['requests']} requests")

    conn = db.connect()
    rows = [db.to_row(it, fl.iso_utc(T0), run_id=run_id) for it in items]
    written = db.upsert_listings(conn, rows)
    conn.commit()
    report.update({"T0": fl.iso_utc(T0), "start": fl.iso_utc(start),
                   "end": fl.iso_utc(end), "run_id": run_id,
                   "sub_bands": len(bands), "served": len(served),
                   "pre_excluded": pre_excluded, "items": len(items),
                   "rows_written": written})
    json.dump(report, open(args.out, "w"), indent=2)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
