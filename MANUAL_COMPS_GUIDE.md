# Manual Sold-Comps Workflow

The official automated source for eBay sold transactions (Marketplace Insights
API) returned `invalid_scope` for this application — it is a Limited Release
requiring eBay business approval, which we do not have. Sold data is therefore
collected by hand from **eBay Product Research**, and the application does the
matching, validation and valuation.

The app makes **zero network calls** in this workflow.


## Faster path: import the eBay export directly

You no longer need to copy transactions by hand. eBay Product Research has an
export button — download the CSV and import it as-is:

```
python manual_comps.py --import-ebay exported_product_research.csv
```

No reformatting. The adapter recognizes columns by normalized name, so
`Title` / `Listing title` / `Item title` and `Sold price` / `Avg sold price`
all work, and a title/filter preamble above the header row is skipped.

Rows are attributed to a candidate by parsing the listing title. To force every
row onto one candidate instead:

```
python manual_comps.py --import-ebay export.csv --candidate "v1|117330553310|0"
```

Two things the adapter will not do:

- **Best Offer.** If the export shows only the asking price on a Best Offer
  sale, the price is moved to `displayed_original_price`, the row is marked
  `actual_price_known=false`, and it is stored but never valued. Only when the
  export carries a separate original price that differs from the sold price is
  the sold figure treated as the accepted one.
- **Currency.** The original currency is preserved. Non-USD rows get
  `fx_rate` and `converted_total` of NULL rather than an assumed 1:1 rate.

The hand-filled workflow below still works unchanged via `--import`.

## Workflow

1. Open **Seller Hub → Research → Product Research** on eBay.
2. Make sure you are looking at **sold / completed transactions**, not active
   listings. Active asking prices are not comps and must never be imported.
3. Open `sold_comps_research.csv`. Each row is one search to run, with the exact
   `search_query` text to paste in.
4. Run every row. Every row is a `STRICT` search — the only active tier. If it
   returns very little, that is the honest answer: record it and move on. Do
   not hand-broaden the query, because a broader search returns a different
   card's sales.
5. Set the **widest reasonable date range** the tool offers, so sparse cards
   still produce a usable sample.
6. Copy or export the **individual transaction rows** — not the summary
   averages the tool shows at the top.
7. Preserve exactly what is displayed: sold price, shipping, sale date, title,
   condition.
8. **Do not decide yourself whether a result matches.** Import every plausible
   result. The matcher applies the approved identity rules and records a reason
   for anything it rejects, which is how mistakes stay auditable.

## Filling in the import file

Copy `sold_comps_import_template.csv` to `sold_comps_import.csv` and fill it in.

### Required columns

| Column | Meaning |
|---|---|
| `candidate_item_id` | from `sold_comps_research.csv` — which candidate this comp is for |
| `query_tier` | which search produced it. `STRICT` for anything new; `NORMAL`/`RELAXED` appear only in rows collected before those tiers were retired |
| `source` | always `EBAY_PRODUCT_RESEARCH` |
| `source_item_id` | eBay item number of the sold listing (used for de-duplication) |
| `raw_title` | the listing title, copied verbatim — this is what the matcher reads |
| `sold_price` | the item price only, without shipping |
| `shipping` | shipping charged; leave blank if not shown |
| `currency` | e.g. `USD` |
| `sale_date` | date of sale; `YYYY-MM-DD` preferred, common US formats accepted |
| `condition` | as displayed |
| `source_reference` | listing URL, or a note describing where it came from |

### Optional columns

| Column | Meaning |
|---|---|
| `notes` | anything you want recorded |
| `best_offer_indicator` | `true` if the listing sold via Best Offer |
| `displayed_original_price` | the crossed-out asking price, if that is all you can see |
| `actual_price_known` | `false` when you could not see the real accepted price |

### Best Offer — the important one

eBay often shows the **original asking price** on a Best Offer sale, not what
the buyer actually paid. Never enter the asking price as the sold price.

- Real accepted price visible → put it in `sold_price`, set
  `actual_price_known=true`.
- Only the crossed-out asking price visible → put it in
  `displayed_original_price`, set `best_offer_indicator=true` and
  `actual_price_known=false`. The row is stored for audit but **excluded from
  valuation**.

### Example row

The values below are **invented for illustration** — they are not a real sale
and must not be imported.

```csv
candidate_item_id,query_tier,source,source_item_id,raw_title,sold_price,shipping,currency,sale_date,condition,source_reference,notes,best_offer_indicator,displayed_original_price,actual_price_known
v1|117330553310|0,STRICT,EBAY_PRODUCT_RESEARCH,000000000000,1991 HOOPS #536 MICHAEL JORDAN PSA 8,111.11,4.99,USD,2026-06-15,Graded,https://example.invalid/itm/000000000000,illustrative row - not a real sale,false,,true
```

## Then run

```
python manual_comps.py --import sold_comps_import.csv
python manual_comps.py --report
```

The report shows accepted comps, rejections with reasons, medians, means, the
most recent sale and a confidence rating. No BUY / WATCH / PASS is produced at
this stage.
