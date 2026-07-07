-- Issue #34: unify the recipe yield. Replace the integer ``yield_units`` with a
-- decimal ``yield_qty`` (in the output SKU's own unit) plus a ``yield_estimated``
-- marker. This is a forward migration rather than an edit to 0002 so databases
-- that already applied 0002 (with ``yield_units``) upgrade in place — the store
-- now reads ``yield_qty`` / ``yield_estimated``, so a missing column would break
-- every recipe read at startup.
--
-- Decimals are stored as TEXT (mirrors the rest of the config tables) so Decimal
-- round-trips exactly. The old integer value carries over verbatim: a legacy
-- ``yield_units = 1`` becomes ``yield_qty = '1'``, still measured (estimated 0).
-- The estimated-yield backfill for preps (recipes consumed as ingredients) runs
-- in Python from ``seed_config`` — it needs Decimal-exact input sums and the
-- SKU units, neither of which SQLite arithmetic can reproduce faithfully.

-- recipes: yield_units (INTEGER NOT NULL DEFAULT 1) -> yield_qty + yield_estimated.
ALTER TABLE recipes ADD COLUMN yield_qty TEXT NOT NULL DEFAULT '1';        -- Decimal as TEXT, in the output SKU's own unit. Issue #34.
ALTER TABLE recipes ADD COLUMN yield_estimated INTEGER NOT NULL DEFAULT 0; -- 0/1 bool. Issue #34.
UPDATE recipes SET yield_qty = CAST(yield_units AS TEXT);
ALTER TABLE recipes DROP COLUMN yield_units;

-- skus: the nullable mirror (NULL means "inherit from the recipe row").
ALTER TABLE skus ADD COLUMN yield_qty TEXT;         -- Decimal as TEXT; NULL means inherit from the recipe row. Issue #34.
ALTER TABLE skus ADD COLUMN yield_estimated INTEGER; -- NULL means inherit from the recipe row. Issue #34.
UPDATE skus SET yield_qty = CAST(yield_units AS TEXT) WHERE yield_units IS NOT NULL;
ALTER TABLE skus DROP COLUMN yield_units;
