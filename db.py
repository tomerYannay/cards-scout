"""SQLite storage for scraped PSA store listings."""

import json
import sqlite3

DB_PATH = "cards.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    price         REAL,
    currency      TEXT,
    shipping_cost REAL,
    buying_option TEXT,
    bid_count     INTEGER,
    end_time      TEXT,
    category_id   TEXT,
    condition     TEXT,
    url           TEXT,
    fetched_at    TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    raw           TEXT NOT NULL
);
"""

# Step 2 output. `listings` stays the untouched raw record; everything parsed
# lives here and can be rebuilt from scratch at any time.
CARDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    item_id          TEXT PRIMARY KEY,
    sport            TEXT,
    sport_conf       TEXT,
    year             INTEGER,
    year_raw         TEXT,
    year_conf        TEXT,
    manufacturer     TEXT,
    manufacturer_conf TEXT,
    set_name         TEXT,
    set_conf         TEXT,
    insert_name      TEXT,
    insert_conf      TEXT,
    parallel         TEXT,
    parallel_conf    TEXT,
    athlete          TEXT,
    athlete_conf     TEXT,
    card_number      TEXT,
    card_number_conf TEXT,
    is_rookie        INTEGER,
    is_auto          INTEGER,
    is_relic         INTEGER,
    serial_num       INTEGER,
    print_run        INTEGER,
    grade_type       TEXT,
    grade_value      TEXT,
    grade_qualifier  TEXT,
    auto_grade       TEXT,
    grade_raw        TEXT,
    grade_conf       TEXT,
    cert_number      TEXT,
    card_key         TEXT,
    slab_key         TEXT,
    identity_conf    TEXT,
    parse_status     TEXT,
    truncation_risk  INTEGER,
    parsed_at        TEXT
);

-- Tier B: authoritative eBay item aspects for the shortlist only. Kept in its
-- own table so Tier A parsed values in `cards` are never overwritten - the two
-- are compared, and disagreement is itself a signal.
CREATE TABLE IF NOT EXISTS tierb (
    item_id        TEXT PRIMARY KEY,
    fetched_at     TEXT NOT NULL,
    http_status    INTEGER,
    grader         TEXT,
    grade          TEXT,
    cert_number    TEXT,
    sport          TEXT,
    year           TEXT,
    brand          TEXT,
    set_name       TEXT,
    card_number    TEXT,
    parallel       TEXT,
    features       TEXT,
    autographed    TEXT,
    auto_grade     TEXT,
    serial_num     TEXT,
    print_run      TEXT,
    condition      TEXT,
    shipping_cost  REAL,
    buying_options TEXT,
    epid           TEXT,
    aspects_json   TEXT,
    resolved_json  TEXT,
    disagreements  TEXT,
    verdict        TEXT,
    original_verdict TEXT,
    tier_a_identity  TEXT,
    tier_b_identity  TEXT,
    effective_identity TEXT,
    effective_card_key TEXT,
    effective_slab_key TEXT,
    classification   TEXT
);

-- Manually imported eBay Product Research sold rows. Every raw row is kept,
-- accepted or not, so a rejection can always be audited.
CREATE TABLE IF NOT EXISTS sold_comps (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_item_id  TEXT NOT NULL,
    query_tier         TEXT,
    source             TEXT NOT NULL,
    source_item_id     TEXT,
    raw_title          TEXT NOT NULL,
    sold_price         REAL,
    shipping           REAL,
    total_price        REAL,
    currency           TEXT,
    fx_rate            REAL,
    fx_date            TEXT,
    converted_price    REAL,
    converted_shipping REAL,
    converted_total    REAL,
    sale_date          TEXT,
    condition          TEXT,
    source_reference   TEXT,
    best_offer         INTEGER DEFAULT 0,
    actual_price_known INTEGER DEFAULT 1,
    displayed_original_price REAL,
    accepted           INTEGER,
    rejection_reason   TEXT,
    match_confidence   TEXT,
    norm_year          INTEGER,
    norm_subject       TEXT,
    norm_card_number   TEXT,
    norm_parallel      TEXT,
    norm_grade         TEXT,
    norm_qualifier     TEXT,
    norm_auto          INTEGER,
    norm_print_run     INTEGER,
    raw_row            TEXT,
    run_id             TEXT,
    sale_type          TEXT,
    collected_at       TEXT,
    raw_text           TEXT,
    imported_at        TEXT NOT NULL
);

-- Per-candidate checkpoint for the Playwright collector, so a run survives a
-- crash, a timeout, an expired session or an eBay verification prompt.
CREATE TABLE IF NOT EXISTS pr_runs (
    candidate_id   TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    query_level    TEXT,
    query_used     TEXT,
    attempts       INTEGER DEFAULT 0,
    rows_extracted INTEGER DEFAULT 0,
    rows_seen      INTEGER DEFAULT 0,
    review_required INTEGER DEFAULT 0,
    accepted       INTEGER DEFAULT 0,
    rejected       INTEGER DEFAULT 0,
    date_range     TEXT,
    run_id         TEXT,
    batch_id       TEXT,
    last_error     TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS parse_issues (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL,
    field      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(active);
CREATE INDEX IF NOT EXISTS idx_cards_slab_key ON cards(slab_key);
CREATE INDEX IF NOT EXISTS idx_cards_card_key ON cards(card_key);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(parse_status);
CREATE INDEX IF NOT EXISTS idx_issues_field ON parse_issues(field);
CREATE INDEX IF NOT EXISTS idx_tierb_cert ON tierb(cert_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comps_dedup
    ON sold_comps(candidate_item_id, source, source_item_id);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migrate before indexing: a database created by an earlier version has no
    # `active` column, and indexing a missing column fails.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    if "active" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    pr = {r["name"] for r in conn.execute("PRAGMA table_info(pr_runs)")}
    if pr:
        for col in ("rows_seen", "review_required", "run_id", "batch_id"):
            if col not in pr:
                kind = ("TEXT" if col in ("run_id", "batch_id")
                        else "INTEGER DEFAULT 0")
                conn.execute(f"ALTER TABLE pr_runs ADD COLUMN {col} {kind}")
    sc = {r["name"] for r in conn.execute("PRAGMA table_info(sold_comps)")}
    if sc:
        for col in ("sale_type", "collected_at", "raw_text", "run_id"):
            if col not in sc:
                conn.execute(f"ALTER TABLE sold_comps ADD COLUMN {col} TEXT")
    tb = {r["name"] for r in conn.execute("PRAGMA table_info(tierb)")}
    if tb:
        for col in ("original_verdict", "tier_a_identity", "tier_b_identity",
                    "effective_identity", "effective_card_key",
                    "effective_slab_key", "classification"):
            if col not in tb:
                conn.execute(f"ALTER TABLE tierb ADD COLUMN {col} TEXT")
    conn.executescript(CARDS_SCHEMA)
    conn.executescript(INDEXES)
    conn.commit()
    return conn


def reset_parse_tables(conn):
    """Step 2 is fully re-derivable, so each run rebuilds it from listings.

    Dropped rather than emptied so a schema change takes effect without a
    migration - `listings` is the durable record, `cards` is derived.
    """
    conn.execute("DROP TABLE IF EXISTS cards")
    conn.execute("DROP TABLE IF EXISTS parse_issues")
    conn.executescript(CARDS_SCHEMA)
    conn.executescript(INDEXES)
    conn.commit()


def deactivate_stale(conn, run_start):
    """Mark listings not seen in this run as inactive.

    Only ever called after a run finishes cleanly - a partial crawl would
    otherwise deactivate listings that are still live.
    """
    cur = conn.execute(
        "UPDATE listings SET active = 0 WHERE fetched_at < ? AND active = 1",
        (run_start,),
    )
    conn.commit()
    return cur.rowcount


def upsert_listings(conn, rows):
    """Insert or replace listings. Returns the number of rows written."""
    conn.executemany(
        """INSERT OR REPLACE INTO listings
           (item_id, title, price, currency, shipping_cost, buying_option,
            bid_count, end_time, category_id, condition, url, fetched_at,
            active, raw)
           VALUES (:item_id, :title, :price, :currency, :shipping_cost,
                   :buying_option, :bid_count, :end_time, :category_id,
                   :condition, :url, :fetched_at, 1, :raw)""",
        rows,
    )
    conn.commit()
    return len(rows)


def to_row(item, fetched_at):
    """Flatten one Browse API item_summary into a listings row."""
    price = item.get("price") or {}
    shipping = (item.get("shippingOptions") or [{}])[0].get("shippingCost") or {}
    options = item.get("buyingOptions") or []
    return {
        "item_id": item.get("itemId"),
        "title": item.get("title", ""),
        "price": _num(price.get("value")),
        "currency": price.get("currency"),
        "shipping_cost": _num(shipping.get("value")),
        "buying_option": ",".join(options),
        "bid_count": item.get("bidCount"),
        "end_time": item.get("itemEndDate"),
        "category_id": (item.get("categories") or [{}])[0].get("categoryId"),
        "condition": item.get("condition"),
        "url": item.get("itemWebUrl"),
        "fetched_at": fetched_at,
        "raw": json.dumps(item, separators=(",", ":")),
    }


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
