-- Issue #139: relax skus.segment from NOT NULL to nullable.
--
-- The column was created NOT NULL by an early deployment's schema; the
-- forward-only migrations never relaxed it. The code has always assumed it
-- nullable — ingredient-only SKUs (and sold-as-is purchasables) legitimately
-- carry no segment, because an ingredient may feed both cafe and bar. On a
-- fresh database the migrations create the column nullable, so the whole
-- test suite passes; on the legacy production shape the sold-as-is quick-
-- create crashed with ``NOT NULL constraint failed: skus.segment`` because
-- the purchasable it creates is segment-NULL by design.
--
-- SQLite cannot ALTER a column's nullability in place, so we rebuild the
-- table: copy every row into a nullable-segment twin, drop the original,
-- rename. ``PRAGMA foreign_keys=OFF`` is the documented SQLite idiom for a
-- table rebuild — ``recipes.sku_id`` and ``mappings.sku_id`` both reference
-- ``skus(sku_id)``, so dropping the parent would otherwise violate the FK
-- even though we re-create it with identical contents. The PRAGMA is
-- connection-scoped and re-enabled at the end, so no other statement in
-- this script (or later migration) is affected.
--
-- The rebuild preserves every column the later migrations added (yield_qty,
-- yield_estimated) and the column order applications querying the table
-- rely on. No data changes — every existing segment value is copied as-is.

PRAGMA foreign_keys = OFF;

CREATE TABLE skus_new (
    sku_id                      TEXT NOT NULL PRIMARY KEY,
    name                        TEXT NOT NULL,
    segment                     TEXT,                    -- 'cafe' | 'bar'; NULL for ingredient-only SKUs with no segment of their own (they may feed both segments)
    unit                        TEXT,                    -- 'g' | 'ml' | 'unit'; NULL until confirmed (ADR-0003 decision 3)
    target_gross_margin_pct     TEXT,                    -- Decimal as TEXT; NULL when unset
    created_at                  TEXT NOT NULL,
    created_by                  TEXT NOT NULL,           -- 'migration' for seeded rows
    yield_qty                   TEXT,                    -- Decimal as TEXT; NULL means inherit from the recipe row (0006)
    yield_estimated             INTEGER                  -- NULL means inherit from the recipe row (0006)
);

INSERT INTO skus_new (
    sku_id, name, segment, unit, target_gross_margin_pct,
    created_at, created_by, yield_qty, yield_estimated
)
SELECT
    sku_id, name, segment, unit, target_gross_margin_pct,
    created_at, created_by, yield_qty, yield_estimated
FROM skus;

DROP TABLE skus;
ALTER TABLE skus_new RENAME TO skus;

PRAGMA foreign_keys = ON;
