-- Issue #96 (parent #82: "model cash-basis supplier spend"): the cash-spend
-- data-model spine. Each row is one bucket's slice of a vendor bill on a
-- date (decision A); a multi-bucket bill is N sibling rows sharing date +
-- supplier, differing bucket + amount. The invoice total is the derived
-- fact SUM(amount) WHERE date=X AND supplier_id=Y — never stored on a
-- parent row.
--
-- FK targets are #94's suppliers and #95's spend_buckets (both must land
-- first). The REFERENCES clauses match the convention established in
-- 0002 (recipes/mappings): declarative, with the route-level in-use
-- guards (supplier_in_use / spend_bucket_in_use, already shipped in
-- #94 / #95 and querying this table) as the runtime referential-
-- integrity check. SQLite's PRAGMA foreign_keys stays at its default
-- (off) — turning it on app-wide is out of scope for this slice; see
-- ADR-0010.
--
-- amount is Decimal as TEXT, like every money column in this schema
-- (costs.price_per_unit_net, fixed_costs.amount) — str(Decimal) round-
-- trips without float drift. vat_inclusive is INTEGER 0/1, matching
-- costs.vat_inclusive; it defaults to FALSE per ADR-0003 decision 4
-- ("default false so the migration never makes a number worse by
-- guessing wrong"). The column ships now even though this slice's UI
-- surfaces it as an unchecked-by-default checkbox — shipping the column
-- on day one avoids the irreversible trap of backfilling "was this
-- gross or net?" for rows recorded between now and whenever a fuller
-- VAT surface lands.

CREATE TABLE IF NOT EXISTS cash_spend (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT NOT NULL,                      -- ISO-8601 date the purchase was paid/incurred
    supplier_id    TEXT NOT NULL REFERENCES suppliers(supplier_id),
    description    TEXT NOT NULL,                      -- the human phrase (the audit trail)
    bucket_id      TEXT NOT NULL REFERENCES spend_buckets(bucket_id),
    amount         TEXT NOT NULL,                      -- Decimal as TEXT; THB, gross-as-paid
    vat_inclusive  INTEGER NOT NULL DEFAULT 0,         -- 0/1; divide by 1.07 at aggregation when set
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cash_spend_date ON cash_spend(date);
CREATE INDEX IF NOT EXISTS idx_cash_spend_supplier ON cash_spend(supplier_id);
CREATE INDEX IF NOT EXISTS idx_cash_spend_bucket ON cash_spend(bucket_id);
