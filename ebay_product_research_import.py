"""Adapter for the CSV eBay Product Research exports. Zero network calls.

This module ONLY translates formats. It does not decide what a row means, does
not match identities, and does not value anything - it maps eBay's columns onto
the internal SoldComp row shape and hands them back untouched otherwise.

Product Research exports vary by locale, tab and eBay release, so columns are
recognized by normalized name rather than exact spelling, and a preamble above
the real header row is tolerated.
"""

import csv
import re

SOURCE = "EBAY_PRODUCT_RESEARCH"

# Internal field -> accepted eBay column spellings (normalized: lowercase,
# alphanumerics only). First match in the file wins.
COLUMN_ALIASES = {
    "raw_title": ["title", "listingtitle", "itemtitle", "producttitle",
                  "name", "productname"],
    "sold_price": ["soldprice", "price", "saleprice", "itemprice",
                   "lastsoldprice", "avgsoldprice", "averagesoldprice",
                   "avgsoldpriceitem"],
    "shipping": ["shipping", "shippingcost", "shippingprice", "postage",
                 "avgshippingcost", "averageshippingcost"],
    "sale_date": ["datesold", "saledate", "solddate", "dateofsale",
                  "lastsolddate", "date", "enddate"],
    "currency": ["currency", "currencycode"],
    "source_item_id": ["itemnumber", "itemid", "legacyitemid", "listingid",
                       "ebayitemnumber", "itemno"],
    "condition": ["condition", "itemcondition"],
    "source_reference": ["itemurl", "url", "listingurl", "link", "viewitemurl",
                         "itemweburl"],
    "seller": ["seller", "sellername", "sellerid", "sellerusername"],
    "total_price": ["totalprice", "total", "soldpricewithshipping"],
    "best_offer": ["bestoffer", "bestofferaccepted", "offeraccepted",
                   "bestofferenabled"],
    "displayed_original_price": ["originalprice", "listprice", "askingprice",
                                 "startprice", "buyitnowprice", "listedprice"],
    "quantity": ["quantity", "quantitysold", "qty", "totalsold"],
}

# Without these there is nothing to match or value.
REQUIRED = ("raw_title", "sold_price")

CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY",
                    "C$": "CAD", "A$": "AUD", "₪": "ILS"}


class AdapterError(ValueError):
    pass


def normalize_header(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").strip().lower())


def build_mapping(header):
    """Map internal field -> column index, by normalized header name."""
    norm = [normalize_header(h) for h in header]
    mapping = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in norm:
                mapping[field] = norm.index(alias)
                break
    return mapping


def looks_like_header(row):
    """A Product Research export can carry a title/filter preamble first."""
    mapping = build_mapping(row)
    return "raw_title" in mapping and any(
        f in mapping for f in ("sold_price", "total_price"))


def detect(path, max_preamble=25):
    """Find the header row and its mapping. Raises AdapterError if absent."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rows = list(csv.reader(fh))
    for i, row in enumerate(rows[:max_preamble]):
        if row and looks_like_header(row):
            mapping = build_mapping(row)
            missing = [f for f in REQUIRED
                       if f not in mapping and not (f == "sold_price"
                                                    and "total_price" in mapping)]
            if missing:
                raise AdapterError(
                    f"header found at line {i+1} but required column(s) missing: "
                    f"{missing}; saw {row}")
            return i, row, mapping, rows
    raise AdapterError(
        "no eBay Product Research header row found - expected a row containing "
        "a title column and a price column")


def money(value):
    """Parse a displayed price. Returns (amount, currency_or_None)."""
    if value is None:
        return None, None
    text = str(value).strip()
    if not text or text.lower() in ("-", "n/a", "na", "none", "--"):
        return None, None
    currency = None
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            currency = code
            break
    m = re.search(r"-?\d[\d,]*\.?\d*", text.replace(" ", ""))
    if not m:
        return None, currency
    try:
        return float(m.group(0).replace(",", "")), currency
    except ValueError:
        return None, currency


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "y", "accepted")


def cell(row, mapping, field):
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return None
    v = row[idx]
    return v.strip() if isinstance(v, str) else v


def translate(path, default_tier="EBAY_EXPORT", default_currency="USD"):
    """Read an export and return internal-shape rows. No network, no matching."""
    header_idx, header, mapping, rows = detect(path)
    out = []
    for row in rows[header_idx + 1:]:
        if not any((c or "").strip() for c in row):
            continue

        title = cell(row, mapping, "raw_title")
        if not title:
            continue                      # trailing summary/blank line

        price, cur_from_price = money(cell(row, mapping, "sold_price"))
        total, cur_from_total = money(cell(row, mapping, "total_price"))
        ship, cur_from_ship = money(cell(row, mapping, "shipping"))
        orig, cur_from_orig = money(cell(row, mapping, "displayed_original_price"))

        if price is None and total is not None:
            # Only a shipping-inclusive figure was exported; back it out when we
            # can, otherwise leave the item price unknown rather than guess.
            price = total - ship if ship is not None else None

        currency = (cell(row, mapping, "currency") or cur_from_price
                    or cur_from_total or cur_from_ship or cur_from_orig
                    or default_currency)
        currency = str(currency).strip().upper()[:3] or default_currency

        best_offer = truthy(cell(row, mapping, "best_offer"))
        # A Best Offer sale usually shows the ASKING price. Only when the export
        # carries a separate original price that differs can the sold figure be
        # taken as the accepted one.
        if best_offer:
            known = orig is not None and price is not None and abs(orig - price) > 1e-9
        else:
            known = True
        if best_offer and not known:
            orig = orig if orig is not None else price
            price = None                  # never let an asking price be valued

        out.append({
            "candidate_item_id": "",      # attribution happens in manual_comps
            "query_tier": default_tier,
            "source": SOURCE,
            "source_item_id": cell(row, mapping, "source_item_id") or "",
            "raw_title": title,
            "sold_price": "" if price is None else f"{price}",
            "shipping": "" if ship is None else f"{ship}",
            "currency": currency,
            "sale_date": cell(row, mapping, "sale_date") or "",
            "condition": cell(row, mapping, "condition") or "",
            "source_reference": (cell(row, mapping, "source_reference")
                                 or cell(row, mapping, "seller") or ""),
            "notes": f"imported from Product Research export ({path})",
            "best_offer_indicator": "true" if best_offer else "false",
            "displayed_original_price": "" if orig is None else f"{orig}",
            "actual_price_known": "true" if known else "false",
        })
    return out, mapping, header
