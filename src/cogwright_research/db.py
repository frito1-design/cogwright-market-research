"""SQLite storage.

Schema follows CW-RES-001 §10. Every write is an upsert keyed on the natural key so a
re-run updates in place — §12 requires idempotency, and a crawl that takes hours will be
resumed at least once.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    domain           TEXT PRIMARY KEY,
    root_url         TEXT,
    first_seen       TEXT,
    source_locators  TEXT,            -- JSON array
    excluded         INTEGER DEFAULT 0,
    exclusion_reason TEXT
);

CREATE TABLE IF NOT EXISTS crawl_results (
    domain                        TEXT PRIMARY KEY REFERENCES domains(domain),
    crawled_at                    TEXT,
    http_status                   INTEGER,
    platform                      TEXT,
    pos_platform                  TEXT DEFAULT 'unknown',
    has_cart                      INTEGER,
    robots_blocked                INTEGER DEFAULT 0,
    catalog_size                  INTEGER,
    variant_count                 INTEGER,
    catalog_method                TEXT,
    truncated                     INTEGER DEFAULT 0,
    days_since_last_product_update INTEGER,
    dormant                       INTEGER DEFAULT 0,
    final_url                     TEXT,
    tls_valid                     INTEGER,
    last_modified                 TEXT
);

CREATE TABLE IF NOT EXISTS products_raw (
    domain        TEXT REFERENCES domains(domain),
    product_id    TEXT,
    title         TEXT,
    vendor        TEXT,
    product_type  TEXT,
    tags          TEXT,
    variant_count INTEGER,
    image_count   INTEGER,
    min_price     REAL,
    published_at  TEXT,
    updated_at    TEXT,
    no_sku_variants INTEGER DEFAULT 0,
    default_title_only INTEGER DEFAULT 0,
    zero_price_variants INTEGER DEFAULT 0,
    PRIMARY KEY (domain, product_id)
);

CREATE TABLE IF NOT EXISTS vendor_matches (
    domain           TEXT REFERENCES domains(domain),
    vendor_raw       TEXT,
    vendor_canonical TEXT,
    match_type       TEXT,            -- exact | alias | fuzzy | none
    confidence       REAL,
    product_count    INTEGER,
    PRIMARY KEY (domain, vendor_raw)
);

CREATE TABLE IF NOT EXISTS quality_signals (
    domain                   TEXT PRIMARY KEY REFERENCES domains(domain),
    pct_no_image             REAL,
    pct_no_product_type      REAL,
    pct_untagged             REAL,
    pct_default_variant_only REAL,
    pct_thin_title           REAL,
    duplicate_title_count    INTEGER,
    pct_zero_price           REAL,
    pct_no_sku               REAL,
    defect_density           REAL
);

CREATE TABLE IF NOT EXISTS scores (
    domain       TEXT PRIMARY KEY REFERENCES domains(domain),
    tier         TEXT,
    fit_score    REAL,
    size_pts     REAL,
    platform_pts REAL,
    vendor_pts   REAL,
    defect_pts   REAL,
    modifiers    TEXT,               -- JSON
    scored_at    TEXT
);

-- Audit trail. Not in §10, but §12 asks for a crawl log with zero robots violations.
CREATE TABLE IF NOT EXISTS request_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    domain  TEXT,
    url     TEXT,
    status  INTEGER,
    outcome TEXT,
    note    TEXT,
    logged_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_products_domain ON products_raw(domain);
CREATE INDEX IF NOT EXISTS idx_vendor_domain   ON vendor_matches(domain);
CREATE INDEX IF NOT EXISTS idx_log_outcome     ON request_log(outcome);
"""


@contextmanager
def connect(path: Path | str) -> Iterator[sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _upsert(conn: sqlite3.Connection, table: str, keys: list[str], row: dict[str, Any]) -> None:
    cols = list(row)
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    conflict = ", ".join(keys)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
        if updates
        else f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    )
    conn.execute(sql, [row[c] for c in cols])


def upsert_domain(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    row = dict(row)
    if isinstance(row.get("source_locators"), (list, tuple, set)):
        row["source_locators"] = json.dumps(sorted(row["source_locators"]))
    _upsert(conn, "domains", ["domain"], row)


def merge_domain_locators(conn: sqlite3.Connection, domain: str, locators: Iterable[str]) -> None:
    """Union new locator sources into the existing JSON array without losing history."""
    cur = conn.execute("SELECT source_locators FROM domains WHERE domain=?", (domain,))
    existing = cur.fetchone()
    merged = set(locators)
    if existing and existing[0]:
        with contextlib.suppress(ValueError):
            merged |= set(json.loads(existing[0]))
    conn.execute(
        "UPDATE domains SET source_locators=? WHERE domain=?",
        (json.dumps(sorted(merged)), domain),
    )


def upsert_crawl_result(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _upsert(conn, "crawl_results", ["domain"], row)


def replace_products(conn: sqlite3.Connection, domain: str, rows: list[dict[str, Any]]) -> None:
    """Products are replaced wholesale — a re-crawl is the new truth for that domain."""
    conn.execute("DELETE FROM products_raw WHERE domain=?", (domain,))
    if not rows:
        return
    cols = list(rows[0])
    conn.executemany(
        f"INSERT OR REPLACE INTO products_raw ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [[r[c] for c in cols] for r in rows],
    )


def replace_vendor_matches(conn: sqlite3.Connection, domain: str, rows: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM vendor_matches WHERE domain=?", (domain,))
    for row in rows:
        _upsert(conn, "vendor_matches", ["domain", "vendor_raw"], row)


def upsert_quality(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _upsert(conn, "quality_signals", ["domain"], row)


def upsert_score(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    row = dict(row)
    if isinstance(row.get("modifiers"), (dict, list)):
        row["modifiers"] = json.dumps(row["modifiers"])
    _upsert(conn, "scores", ["domain"], row)


def log_requests(conn: sqlite3.Connection, records: Iterable[Any]) -> None:
    conn.executemany(
        "INSERT INTO request_log (domain, url, status, outcome, note) VALUES (?,?,?,?,?)",
        [(r.domain, r.url, r.status, r.outcome, r.note) for r in records],
    )
