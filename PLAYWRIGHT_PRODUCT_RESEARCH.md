# Product Research collector (Playwright)

Collects **sold** transactions for valuation candidates from eBay Seller Hub →
Research → Product Research, and hands them to the existing identity matcher.

This is **UI automation, not a stable API.** eBay can change the page at any
time and the selectors will need updating. The official sold-transaction API
(Marketplace Insights) returned `invalid_scope` for this application, which is
why this layer exists.

## Scope

It runs **only** on the ~27 candidates that already survived the inventory
pipeline. It never touches the main crawler and never walks the 148k listings.

```
PSA active inventory
  → peer/anomaly filtering        (existing)
  → valuation candidates          pilot_candidates.json
  → Product Research via Playwright   ← this module
  → existing identity matcher     manual_comps.match()
  → valuation report
```

## Install

Playwright and Chromium are already installed in `.venv`. If you need them again:

```
.venv/bin/pip install playwright
.venv/bin/playwright install chromium
```

## 1. Log in (once)

```
.venv/bin/python product_research_playwright.py --login
```

Opens a visible browser. **You** sign in, complete any 2FA, and open
Seller Hub → Research → Product Research. Close the window when done.

The session is stored in `data/playwright/ebay-profile/`. The script never
reads, prints or stores your password, cookies or tokens — it only lets Chromium
keep its own profile on disk. Treat that folder as sensitive and do not commit it.

## Attach to your own Chrome (recommended if eBay uses Google sign-in)

Google blocks sign-in from automated browsers, so the `--login` flow above fails
if your eBay account authenticates via Google. Instead, log in yourself in real
Chrome and let Playwright attach to that already-open session. Nothing about the
login is automated — we only drive a tab you already opened.

**1. Start Chrome with remote debugging** (a dedicated profile; Chrome refuses
remote debugging on the default one):

```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome-ebay-profile"
```

**2. In that window**, sign in to eBay normally (Google auth works — it is real
Chrome), then open Seller Hub → Research → Product Research and switch to
**Sold**.

**3. Verify the attach**, which changes nothing:

```
.venv/bin/python product_research_playwright.py --check-connection
```

Expected:

```
  attached to Chrome at http://localhost:9222
  using tab: https://www.ebay.com/sh/research...
  page state: results_ok
  -> Product Research reachable. date range: Last 90 days
```

**4. Run a candidate against that session:**

```
.venv/bin/python product_research_playwright.py \
  --connect-existing --candidate-id "v1|298544784209|0"

.venv/bin/python product_research_playwright.py --connect-existing --limit 5
```

Add `--cdp-url` if you used a different port. Your Chrome stays open when the
script disconnects — it is never closed or quit for you.

**Keeping your existing logins:** if you would rather not sign in again, quit
Chrome and copy your profile first, then launch against the copy:

```
cp -R "$HOME/Library/Application Support/Google/Chrome" "$HOME/chrome-ebay-profile"
```

**`page state: unknown_page` on the real Product Research tab** — fixed.
Detection now looks for markers specific to the Terapeak app: the
`sh_terapeak_research_default` tracking pageName, the `research-container`
class, the `search-input-panel__research-button` button, the search placeholder
`Enter keywords, MPN, UPC, EPID, EAN or ISBN`, and a `<title>` containing
"Product Research". Any one is sufficient; a generic eBay page satisfies none of
them. A loaded page with no results yet reports `research_ready`, which needs no
result rows or row count. Sign-in and verification detection still outrank it,
and now read **rendered text only** so a script blob mentioning "captcha" cannot
trigger a false auth state.

**"attached tab is not on Product Research" when it clearly is** — fixed. CDP
exposes Chrome internals (omnibox popups, devtools, extension pages) as targets,
and your real tab may live in a different browser context than `contexts[0]`.
Tabs are now gathered across **all** contexts, internal targets are discarded,
and selection is ranked: Product Research > any eBay page > any normal page.
`--check-connection` prints every target it saw and which one it chose.

`--check-connection` reads whichever tab it attached to **without navigating**,
so it will not pull you off a page you set up by hand, and it never opens a new
tab.

**"Unable to retrieve content because the page is navigating"** — fixed. Seller
Hub re-navigates after first paint, so every `page.content()` call goes through
a bounded retry that waits for `domcontentloaded` and retries only on a
navigation race. `networkidle` is deliberately never awaited: Seller Hub polls
in the background indefinitely and would simply time out.

**If this still will not work**, the CSV path needs no browser automation at
all: export the sold results from Product Research and run
`python manual_comps.py --import-ebay <file>.csv`. That path is fully built and
tested.

## 2. One candidate (start here)

```
.venv/bin/python product_research_playwright.py \
  --candidate-id "v1|298544784209|0" --headed
```

That is the Hasbulla `Red Prizm /199 PSA 9` card used for the smoke test.

## 3. Five-candidate pilot

```
.venv/bin/python product_research_playwright.py --limit 5 --headed
```

Runs the five pilot candidates, one at a time, with a randomized 4-9s delay.
Headed is the default; `--headless` is opt-in.

## 4. Resume

```
.venv/bin/python product_research_playwright.py --resume --headed
.venv/bin/python product_research_playwright.py --retry-failed --headed
.venv/bin/python product_research_playwright.py --report
```

Progress is checkpointed to the `pr_runs` table **after every candidate**, so a
crash, timeout, expired session or verification prompt loses at most one card.

Statuses: `pending`, `running`, `completed`, `no_results`, `insufficient_comps`,
`failed`, `auth_required`, `review_required`.
A `completed` candidate is never redone without `--force` or `--retry-failed`.

## Clean re-collection

`--force` re-collects but leaves earlier rows in `sold_comps`, so a candidate's
report can aggregate stale duplicates under different synthetic ids. To start
that one candidate from scratch:

```
.venv/bin/python product_research_playwright.py \
  --connect-existing \
  --candidate-id "v1|298544784209|0" \
  --reset-comps \
  --force
```

`--reset-comps` is the only flag that deletes anything. It requires
`--candidate-id`, refuses to run with `--limit`, deletes inside a transaction,
prints the exact row count removed, and touches no other candidate.

## Options

| Flag | Meaning |
|---|---|
| `--candidates FILE` | candidate file (default `pilot_candidates.json`) |
| `--candidate-id ID` | run exactly one |
| `--limit N` | cap how many candidates run |
| `--headed` / `--headless` | headed is the default |
| `--resume` | skip anything already finished |
| `--retry-failed` | re-run `failed` / `auth_required` only |
| `--force` | re-run even completed candidates (never deletes anything) |
| `--reset-comps` | **destructive**: delete this candidate's stored comps first; requires `--candidate-id`, refuses `--limit` |
| `--output DIR` | raw transaction JSON directory |
| `--delay-min` / `--delay-max` | seconds between searches (default 4-9) |
| `--timeout` | per-page timeout, seconds (default 45) |
| `--report` | print the report without collecting |
| `--connect-existing` | attach to your own Chrome over CDP instead of the saved profile |
| `--cdp-url` | debugging endpoint (default `http://localhost:9222`) |
| `--check-connection` | verify the attach and stop |

## Run scoping

Every collection gets a `run_id`, stamped on `pr_runs` and on the comps it
classified. The report renders **one run**, never the candidate's whole history,
so re-collecting cannot inflate the counts. Rows from before `run_id` existed are
shown as `(pre-run_id rows)` with an accounting warning rather than silently
merged.

A failure while *rendering* the report is caught and printed as `REPORT_ERROR`;
the collection status it just wrote is left untouched.

## Priced vs accepted comps

An **accepted identity comp** and a **priced valuation comp** are different
things. The report states all three counts:

```
accepted identity comps : 3   priced: 0   unpriced: 3
INSUFFICIENT EVIDENCE - no accepted comp has a usable price
valuation       : unavailable (NONE) - 0 priced comps
```

Valuation confidence counts **priced** comps only, so three accepted rows with
no usable price can never produce MEDIUM. A missing price is never read as zero,
and shipping only becomes `0.00` when the row explicitly says *Free shipping*.

Shipping is identified from what the row states (`+$32.00 shipping`) rather than
from column order - header-order guessing had been putting the sold price into
the shipping field.

## Market gap

The report states the gap in the direction a buyer cares about:

```
market gap = market_total_median - asking_price
```

Positive is a **discount to market** (asking below market); negative is a
**premium over market**. Still a gross figure, before taxes, import costs,
marketplace fees and resale costs - and still no BUY/WATCH/PASS.

## Classification accounting

Every extracted transaction ends in exactly one of **accepted**, **rejected**,
**review_required**, and the collector asserts

```
accepted + rejected + review_required == rows extracted
```

If that fails the run is marked `extraction_error` and every row is dumped with
its parsed fields and decision trace to
`data/playwright/artifacts/*_unclassified.json`. The report prints all three
counts and warns if they do not reconcile.

The dedup key identifies a **transaction**, not a listing: eBay item id (or
title) + price + shipping + date. One listing can sell more than once - item
117290548001 sold at $88.00 and again at $53.14 - and keying on the item id
alone silently merged those into one row. The same sale seen again in a broader
tier still collapses to a single comp.

When the invariant fails, the artifact records run_id, batch_id, every extracted
id, every classified id, the missing ids, unexpected ids, and full collision
groups.

## Search tiers

Built from the candidate's **effective identity**, de-duplicated so no word is
sent twice (`PRIZM UFC … RED PRIZM` → one `PRIZM`; an `AUTOGRAPH` parallel does
not also add `auto`).

`STRICT` — year, subject, brand, set, card number, parallel, print run, grade —
is the **only active tier**. `NORMAL` and `RELAXED` are retired.

They originally broadened the search by dropping brand, set and print run, which
searched a different card. Once they were made identity-safe they differed from
`STRICT` only in punctuation (`#151` vs `151`), and eBay's tokenizer strips
punctuation, so they re-asked a question eBay had already answered. The
nine-candidate pilot escalated three times and gained 0 unique raw rows and 0
unique accepted comps.

Both names remain valid **data**: historical raw artifacts and stored
`sold_comps` rows carry `query_tier="NORMAL"` and stay readable. Only
`query_levels()` decides what may be sent, from `manual_comps.ACTIVE_TIERS`.

There is no widening step. A candidate whose single query returns fewer than 3
accepted exact comps is recorded as `insufficient_comps`, and one returning
nothing as `no_results`. Both are completed research runs with an honest answer,
not failures to retry with a looser query.

Deduplication still applies within the run: the same sale seen twice is stored
once, keyed by item id, price, shipping and date.

## Output files

| Path | Contents |
|---|---|
| `data/playwright/raw/*.json` | raw extracted rows + query + date range |
| `data/playwright/artifacts/*.png` / `.html` | screenshot + page HTML on failure |
| `sold_comps` table | every transaction, accepted / rejected / review_required, with reasons |
| `pr_runs` table | per-candidate checkpoint |

## Matching: attribution ≠ acceptance

A row is **attributed** to a candidate because it came from that candidate's
search. Whether it is **accepted** is decided solely by the existing
`manual_comps.match()`, from the evidence in the title.

Worked example from the test fixture — 5 rows extracted, 2 accepted:

```
[ACCEPT] $83.60  ... Red Prizm #200 Hasbulla Magomedov 57/199 PSA 9
[ACCEPT] $60.00  ... Red Prizm #200 Hasbulla Magomedov 112/199 PSA 9
[REJECT] $50.99  ... Red Prizm #200 Hasbulla Magomedov PSA 9
         print run None != 199
[REJECT] $57.00  ... Red Prizm #200 Hasbulla Magomedov 41/199 PSA 10
         PSA grade 10 != 9
[REJECT] $66.75  ... Silver Prizm #200 Hasbulla Magomedov 12/99 PSA 9
         print run 99 != 199
```

Evidence classification: a field the title **contradicts** is a rejection; a
field the title simply **omits** is `review_required` - stored, visible, and
never valued. A genuine parallel conflict outranks an absent print run, so
`Red Ruby Wave` is rejected rather than held.

`57/199` and `112/199` are the same card — a different serial copy of one print
run. A title that omits `/199` is **not** accepted just because the search asked
for `/199`.

## Report

Per candidate: PSA asking price, raw rows extracted, accepted, rejected with
reasons, median/mean/min/max sold total, and confidence
(`HIGH` ≥5, `MEDIUM` 3-4, `LOW` 1-2, `NONE` 0). Under 3 accepted comps it is
labelled **INSUFFICIENT EVIDENCE**.

The price difference is labelled **gross price gap before taxes, import costs,
marketplace fees and resale costs** — it is *not* net profit. No fee model
exists yet, and no BUY/WATCH/PASS is assigned.

## Troubleshooting

**"no saved session"** — run `--login`.

**`auth_required`** — the session expired or eBay wants verification. The run
stops immediately. Complete it yourself in the browser, then `--resume`. Nothing
here attempts to bypass CAPTCHA, MFA or rate limits.

**"could not locate the Product Research search box"** — the page changed. Check
`data/playwright/artifacts/*_search_error.html`, then update the locator list in
`run_search()`. Locators are tried in order: role=searchbox, role=combobox,
placeholder, label, `input[type=search]`, `input[name*=keyword]`.

**`no_results` on everything** — confirm you are on the **sold** tab and that
your date range is wide.

**Rows extracted but all rejected** — usually correct. Read the reasons; the
search is broader than the identity.

## Known limitations

- **Result rows are not in a `<table>`.** `extract_result_rows()` therefore
  works on semantics rather than markup: the app's own column labels
  (`Avg sold price`, `Avg shipping`, `Total sold`, `Item sales`,
  `Date last sold`, `Bids`), `role=row` / `role=gridcell` when present, links,
  and the shape of the content (a money amount plus a date). The old table
  parser is kept as a fallback. **The DOM shape in the populated fixtures is
  inferred** - a real populated capture has not been taken - so the first live
  run is still the real test.
- UI automation: selectors are best-effort and will break when eBay redesigns.
- Sold history is limited to whatever window your account exposes. The selected
  range is recorded with each run rather than assumed to be 90 days.
- Product Research shows title, price, shipping, date, condition and sale type.
  It does **not** expose structured card aspects, so identity still comes from
  the title.
- Best Offer sales often show only the asking price. Those are stored with
  `actual_price_known=false` and excluded from valuation — never valued at the
  asking price.
- Currency is preserved; non-USD rows get `NULL` conversion rather than an
  assumed rate.
- Single browser, one candidate at a time, no parallel workers.

## Tests

```
.venv/bin/python -m unittest test_product_research
```

36 offline tests against sanitized fixtures in
`tests/fixtures/product_research/`. **No test touches eBay.** The live smoke
test is opt-in: run the single-candidate command yourself.

## Known issues (deferred)

- **VAPORWAVE / WAVE family normalization** may need a curated taxonomy rule.
  `WAVE` is a parallel token today, but families such as `BLUE/AQUA VAPORWAVE`
  are not modelled as a family the way `MONEY SHIMMER` now is.
- **A zero-result STRICT run** is indistinguishable from a page that rendered
  late or partially. Stronger page-state verification would tell them apart.
  (The old STRICT -> NORMAL version of this note is obsolete: NORMAL is retired.)

## BUY / WATCH / PASS (conservative MVP)

Computed in `decision.py` from **accepted, priced comps only** - review_required
and rejected rows are never passed in, so they cannot move a decision.

| | rule |
|---|---|
| BUY | discount >= 25%, >= 3 priced comps, newest comp <= 12 months old |
| WATCH | 10% <= discount < 25%, >= 3 priced comps |
| PASS | discount < 10%, at/above market, or insufficient evidence |

Benchmark is the **median market total** (sold price + shipping). Guards only
ever downgrade: dispersion `max/min > 2.5` (with `min > 0`) turns BUY into
WATCH, as does a newest comp older than 12 months or comps with no dates.

Every result is labelled **gross opportunity before taxes, import costs,
marketplace fees and resale costs**. No net profit is claimed.
