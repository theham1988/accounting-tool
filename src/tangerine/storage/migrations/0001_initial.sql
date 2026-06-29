-- Wave 1 initial schema. Mirrors the frozen dataclasses in types.py:
--   sales           -> one row per Sale, keyed by (receipt_number, line_id)
--                      for idempotent sync (slice 3 replays the same receipts).
--   menu_snapshots  -> one row per recorded snapshot, carrying the `at` timestamp.
--   menu_changes    -> the diff history between consecutive snapshots
--                      (added / price_change / renamed / discontinued).
--   schema_migrations -> forward-only migration bookkeeping.

CREATE TABLE IF NOT EXISTS schema_migrations (
    id          INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    receipt_number  TEXT NOT NULL,
    line_id         TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    sell_price      TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    segment         TEXT,
    PRIMARY KEY (receipt_number, line_id)
);

CREATE TABLE IF NOT EXISTS menu_snapshots (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS menu_items (
    snapshot_id  INTEGER NOT NULL,
    item_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    sell_price   TEXT NOT NULL,
    segment      TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES menu_snapshots(id)
);

CREATE TABLE IF NOT EXISTS menu_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL,
    change_kind  TEXT NOT NULL,
    at           TEXT NOT NULL,
    from_value   TEXT,
    to_value     TEXT
);
