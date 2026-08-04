"""Deterministic capacity plan for the bounded Gate C auction pilot.

The displaced-window probe reported 333,296 auctions ending in a single 24-hour
slice of category 212 - thirty-three times eBay's 10,000-result paging ceiling.
Five pages of that query would be five pages of whatever eBay happened to sort
first, so this module never issues the unbounded query at all. It splits a
price-banded range into sub-ranges small enough to page exhaustively, and it
refuses any band it cannot page exhaustively rather than truncating it.

Nothing here claims the pilot represents the 24-hour auction market. It is a
bounded sample of one documented price band, and `coverage_claim()` is the only
sentence about coverage this project is entitled to make.
"""

import datetime as dt

import db
import fetch_listings as fl

# --- window ---------------------------------------------------------------
# The next 24 hours from a frozen run start. Gate C is about auctions that are
# about to end, so the window opens at T0 rather than being displaced; the
# displaced form stays available and is what proved the filter is enforced.
WINDOW_HOURS = 24

# --- price band -----------------------------------------------------------
# Motivated by the 153 researched candidates, not chosen to satisfy the API:
# every $50-$1000 bucket resolved at least one accepted comp for 78-100% of its
# candidates, and 135 of 153 researched candidates fall inside it. Below $50 a
# single candidate exists and shipping dominates the gap; above $1000 comp
# volume thins while per-unit risk rises, which a research-only pilot should
# not carry.
BAND_MIN = 50.0
BAND_MAX = 1000.0

# --- hard bounds ----------------------------------------------------------
MAX_SEARCH_REQUESTS = 40        # search pages across the whole pilot
MAX_DISCOVERY_ITEMS = 2_000     # rows persisted from discovery
MAX_FROZEN_CANDIDATES = 60      # candidates admitted to Product Research
PAGING_CEILING = 10_000         # eBay's offset cap, per distinct query
MIN_BAND_WIDTH = 1.0            # narrower than this, splitting has failed

BAND_INCOMPLETE = "INCOMPLETE_BAND_EXCLUDED"


def window(start, hours=WINDOW_HOURS):
    """The frozen (start, end) pair. Rejects naive datetimes via iso_utc."""
    fl.iso_utc(start)
    return start, start + dt.timedelta(hours=hours)


def split_band(low, high, count, ceiling=PAGING_CEILING,
               min_width=MIN_BAND_WIDTH):
    """Halve a band until each piece is expected to fit under the ceiling.

    Returns `(bands, excluded)`. `count` is the total eBay reported for the
    whole band; the split is geometric, which matches how card prices are
    distributed far better than an arithmetic one, so the pieces carry more
    similar populations.

    A piece that is still over the ceiling once it has been narrowed to
    `min_width` cannot be paged exhaustively, so it is returned in `excluded`
    and never queried. Dropping it silently would leave a hole in the price
    range that every downstream count would then describe as covered.
    """
    if count <= ceiling:
        return [(low, high)], []
    if high - low <= min_width:
        return [], [(low, high, count)]
    mid = round((low * high) ** 0.5, 2)
    if mid <= low or mid >= high:
        mid = round((low + high) / 2, 2)
    halves = max(1, count // 2)
    lo_bands, lo_out = split_band(low, mid, halves, ceiling, min_width)
    hi_bands, hi_out = split_band(mid, high, halves, ceiling, min_width)
    return lo_bands + hi_bands, lo_out + hi_out


def spread_bands(bands, count):
    """Pick `count` sub-bands spanning the whole range, evenly and by index.

    The caps almost always afford fewer pages than there are sub-bands - 64
    sub-bands against 10 affordable pages, at the observed density. Serving
    them in ascending order would spend the entire pilot on the cheapest few
    dollars of the range and then describe the result as covering $50-$1000.
    Striding by index keeps the sample spanning the band, and being purely
    positional it never favours a price the pilot is supposed to be testing.
    """
    if count >= len(bands):
        return list(bands)
    if count <= 0:
        return []
    step = len(bands) / count
    return [bands[min(len(bands) - 1, int(i * step))] for i in range(count)]


def plan_requests(bands, page_size=200, max_requests=MAX_SEARCH_REQUESTS,
                  max_items=MAX_DISCOVERY_ITEMS):
    """Which bands to query and how many pages each may take.

    Returns one entry per band actually served, in ascending price order. The
    bands served are a deterministic spread across the range, never a prefix.
    """
    if not bands:
        return []
    affordable = min(max_requests, max_items // page_size)
    served = spread_bands(bands, affordable)
    if not served:
        return []
    per_band = max(1, min(max_requests // len(served),
                          (max_items // page_size) // len(served)))
    out, spent, items = [], 0, 0
    for low, high in served:
        pages = min(per_band, max_requests - spent,
                    max(0, (max_items - items) // page_size))
        if pages <= 0:
            break
        out.append({"band": (low, high), "pages": pages,
                    "max_items": min(pages * page_size, max_items - items)})
        spent += pages
        items += pages * page_size
    return out


def eligible(row):
    """Whether a discovered auction may become a Gate C candidate at all."""
    if row.get("sale_format") not in (db.AUCTION_ONLY, db.AUCTION_WITH_FIXED):
        return False, "not_an_auction"
    if not row.get("end_time"):
        return False, "no_end_time"
    if row.get("current_bid_price") is None:
        return False, "no_current_bid"
    return True, None


def select_candidates(rows, known_item_ids=(), researched_keys=(),
                      key_of=None, limit=MAX_FROZEN_CANDIDATES):
    """Deterministic, deduplicated candidate selection.

    Ordered by end time then item id - never by price or by any measure of how
    attractive a listing looks, which would bias the sample toward the outcome
    the pilot is meant to test. Returns (selected, rejected).
    """
    known = set(known_item_ids)
    seen_keys = set(researched_keys)
    selected, rejected = [], []
    for row in sorted(rows, key=lambda r: (r.get("end_time") or "",
                                           r.get("item_id") or "")):
        ok, why = eligible(row)
        if not ok:
            rejected.append((row.get("item_id"), why))
            continue
        if row.get("item_id") in known:
            rejected.append((row.get("item_id"), "already_discovered"))
            continue
        key = key_of(row) if key_of else None
        if key and key in seen_keys:
            rejected.append((row.get("item_id"), "identity_already_researched"))
            continue
        if len(selected) >= limit:
            rejected.append((row.get("item_id"), "over_candidate_cap"))
            continue
        known.add(row.get("item_id"))
        if key:
            seen_keys.add(key)
        selected.append(row)
    return selected, rejected


def coverage_claim(served, excluded, selected_count, total_bands=None):
    """The only coverage sentence this pilot is entitled to make.

    `served` is the bands actually queried, not the nominal range: describing
    the pilot by a band it only partly sampled is the failure this sentence
    exists to prevent.
    """
    if not served:
        return ("No bands were queried, so this pilot establishes nothing "
                "about the auction market.")
    lo = min(b[0] for b in served)
    hi = max(b[1] for b in served)
    sampled = (f" It sampled {len(served)} of {total_bands} sub-bands, so parts "
               f"of the range were never queried."
               if total_bands and total_bands > len(served) else "")
    note = (f" {len(excluded)} band(s) were excluded as unpageable and are "
            f"absent from these results." if excluded else "")
    return (f"{selected_count} auction candidates sampled from "
            f"${lo:,.2f}-${hi:,.2f} across a single {WINDOW_HOURS}-hour ending "
            f"window, bounded by a {MAX_FROZEN_CANDIDATES}-candidate cap."
            f"{sampled}{note} This is not a representative sample of the "
            f"24-hour auction market and no market-wide conclusion may be "
            f"drawn from it.")
