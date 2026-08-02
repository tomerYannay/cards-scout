# cards-scout

Finds underpriced PSA-graded sports cards in the PSA eBay store, verifies each
card's identity against authoritative eBay item aspects, matches it to real sold
transactions, and issues a conservative **BUY / WATCH / PASS**.

The guiding constraint throughout: a valuation is only allowed to rest on comps
that are provably the *same physical card* — same set, number, parallel, serial
and grade. When identity cannot be settled, the pipeline declines to value.


## Install

```bash
python3 -m venv .venv
.venv/bin/pip install requests beautifulsoup4 playwright
.venv/bin/playwright install chromium     # only for the sold-comps collector
```

Requires Python 3.9+. Pinned versions used in development are in
`requirements.txt`.


## Credentials

The crawler needs eBay **production** application keys from
developer.ebay.com → My Account → Application Keys. They are read from the
environment and are never written into the repo:

```bash
export EBAY_APP_ID=...
export EBAY_CERT_ID=...
```

Keeping them in `~/.cards-scout.env` (outside the repository) and sourcing it is
the convention used here. `.gitignore` blocks `.env` files, tokens, cookies and
the browser profile so credentials cannot be committed by accident.

Only the Browse API is used. Marketplace Insights — eBay's only official sold
transaction API — returns `invalid_scope` for this application; it is a Limited
Release requiring business approval. `check_sold_source.py` re-probes that
entitlement. Sold data therefore comes from Product Research instead
(see below).


## Recreating `cards.db`

**`cards.db` is not in the repository** — it is a ~360 MB generated dataset,
rebuilt entirely from the eBay API and the scripts here. Nothing in it is
authored by hand. `db.py` creates every table with `CREATE TABLE IF NOT EXISTS`,
so each step below is safe to re-run, and steps that have already fetched an
item never refetch it without `--refresh`.

```bash
# Step 1 — crawl active PSA sports listings (needs API keys).
#          Walks the store in price bands that subdivide under eBay's
#          10,000-result paging cap. Writes `listings`.
.venv/bin/python fetch_listings.py

# Step 2 — normalize titles into structured cards. Read-only on `listings`;
#          writes `cards` and `parse_issues`, then prints a validation report.
.venv/bin/python parse_listings.py

# Step 2 validation (read-only, optional)
.venv/bin/python validate_groups.py
.venv/bin/python preview_anomalies.py

# Step 3A — Tier B identity verification for the shortlist only, via
#           GET /buy/browse/v1/item/{item_id}. 1 request per item against a
#           5,000/day quota; remaining quota is checked first.
.venv/bin/python enrich.py
.venv/bin/python peers.py --plan     # inspect the plan first
.venv/bin/python peers.py --run      # refuses to start without quota headroom

# Re-scoring passes. These make ZERO API calls — they re-run the logic over
# aspects already cached in `tierb`.
.venv/bin/python adjudicate.py
.venv/bin/python reclassify.py
.venv/bin/python reevaluate.py

# Freeze the valuation-ready candidates.
.venv/bin/python export_pilot.py     # -> pilot_candidates.json
```

Step 1 is the only expensive stage; everything after it is cache-driven and
re-runnable at no API cost. Exact rerun cost depends on store size at the time —
the run this repository's audit artifacts came from covered ~148k listings.


## Sold comps and valuation

Two interchangeable paths produce sold transactions, both matched by the same
identity rules in `manual_comps.match()`:

```bash
# Automated collector — drives eBay Seller Hub Product Research with Playwright.
# You log in manually once; the session lives in a local browser profile that is
# git-ignored. No password, cookie or token is read, printed or stored.
.venv/bin/python product_research_playwright.py

# Manual / CSV path — zero network calls.
.venv/bin/python manual_comps.py --research          # emit the searches to run
.venv/bin/python manual_comps.py --import-ebay FILE  # import a PR export CSV
.venv/bin/python manual_comps.py --report            # valuation summary
```

`decision.py` turns accepted, priced comps into BUY / WATCH / PASS. It is pure
functions with no I/O, so every threshold is directly testable — it benchmarks
on the *median* of sold price + shipping and refuses to decide on thin evidence.

Details: **`PLAYWRIGHT_PRODUCT_RESEARCH.md`** (collector, selectors, failure
modes) and **`MANUAL_COMPS_GUIDE.md`** (CSV workflow).


## Tests

```bash
.venv/bin/python -m unittest discover -s . -p 'test_*.py'
```

427 tests, no network access and no database required — the extraction path is
tested against sanitized HTML fixtures in `tests/fixtures/product_research/`.
Many cases are drawn from real titles and real live failures, which is why the
parser suite is as large as it is.


## Layout

| Path | Purpose |
| --- | --- |
| `ebay_api.py` | Browse API client (token, search, getItem, rate limit) |
| `db.py` | Schema and connection helpers |
| `parse.py`, `card_vocab.py` | Title parsing, identity keys, parallel vocabulary |
| `enrich.py`, `peers.py` | Tier B aspect verification |
| `adjudicate.py`, `reclassify.py`, `reevaluate.py` | Cache-only re-scoring |
| `manual_comps.py`, `ebay_product_research_import.py` | Sold-comp import and matching |
| `product_research_playwright.py`, `product_research_parse.py` | Collector and its browser-free parser |
| `decision.py` | BUY / WATCH / PASS |
| `data/playwright/raw`, `derived` | Collected transactions and reclassified output |
| `*_audit.json`, `pilot_candidates.json` | Audit artifacts from the reference run |
