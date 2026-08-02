"""Minimal eBay Browse API client (application token, item search)."""

import base64
import os
import time

import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{item_id}"
RATE_LIMIT_URL = "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay caps offset-based paging at 10,000 results per distinct query.
MAX_OFFSET = 10_000
PAGE_SIZE = 200


class EbayError(RuntimeError):
    pass


# Count of HTTP requests made to the search endpoint, for run reporting.
request_count = 0


def get_token():
    app_id = os.environ.get("EBAY_APP_ID")
    cert_id = os.environ.get("EBAY_CERT_ID")
    if not app_id or not cert_id:
        raise EbayError(
            "Set EBAY_APP_ID and EBAY_CERT_ID (production keys from "
            "developer.ebay.com > My Account > Application Keys)."
        )
    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=30,
    )
    if resp.status_code != 200:
        raise EbayError(f"Token request failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["access_token"]


def search_page(token, filters, category_ids, offset, sort=None, q=None,
                limit=None):
    """One page of item summaries.

    `q` adds a keyword term. `limit` caps the page size, which a bounded
    feasibility probe needs - the crawler always wants the full PAGE_SIZE.
    """
    global request_count
    request_count += 1
    params = {
        "category_ids": category_ids,
        "filter": ",".join(filters),
        "limit": limit or PAGE_SIZE,
        "offset": offset,
    }
    if sort:
        params["sort"] = sort
    if q:
        params["q"] = q
    resp = requests.get(
        SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params=params,
        timeout=45,
    )
    if resp.status_code == 429:
        time.sleep(5)
        return search_page(token, filters, category_ids, offset, sort)
    if resp.status_code != 200:
        raise EbayError(f"Search failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def get_rate_limit(token):
    """Remaining Browse API calls for today, or None if unavailable."""
    try:
        resp = requests.get(
            RATE_LIMIT_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"api_name": "browse", "api_context": "buy"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}: {resp.text[:160]}"
        out = []
        for group in resp.json().get("rateLimits", []):
            for res in group.get("resources", []):
                for r in res.get("rates", []):
                    out.append({
                        "resource": res.get("name"),
                        "limit": r.get("limit"),
                        "remaining": r.get("remaining"),
                        "reset": r.get("reset"),
                    })
        return out, None
    except requests.RequestException as exc:
        return None, str(exc)


# The resource that getItem actually spends. eBay names it "buy.browse";
# "buy.browse.item.bulk" is a different pool and must not be read instead.
BROWSE_RESOURCE = "buy.browse"


def browse_quota(limits):
    """Remaining calls on the resource getItem spends, or None if absent.

    Never guesses. An unmatched resource name returns None so the caller can
    abort, rather than silently reporting some other pool's headroom.
    """
    for l in limits or []:
        if (l.get("resource") or "").lower() == BROWSE_RESOURCE:
            return l.get("remaining")
    return None


def get_item(token, item_id, timeout=30, retries=3):
    """Fetch one item with its aspects. Returns (status_code, json_or_none).

    Retries on 429 and 5xx with linear backoff; never retries a 404, which is a
    real answer (the listing ended).
    """
    global request_count
    delay = 2
    for attempt in range(retries):
        request_count += 1
        try:
            resp = requests.get(
                ITEM_URL.format(item_id=requests.utils.quote(item_id, safe="")),
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt == retries - 1:
                return 0, {"error": str(exc)}
            time.sleep(delay)
            delay += 2
            continue
        if resp.status_code == 200:
            return 200, resp.json()
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(delay * 3 if resp.status_code == 429 else delay)
            delay += 2
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"error": resp.text[:300]}
    return 0, {"error": "retries exhausted"}


def search_all(token, filters, category_ids, sort=None):
    """Page through one query. Yields (items, total, truncated)."""
    offset = 0
    while offset < MAX_OFFSET:
        data = search_page(token, filters, category_ids, offset, sort)
        items = data.get("itemSummaries") or []
        total = data.get("total", 0)
        if not items:
            return
        truncated = total > MAX_OFFSET
        yield items, total, truncated
        offset += PAGE_SIZE
        if offset >= total:
            return
