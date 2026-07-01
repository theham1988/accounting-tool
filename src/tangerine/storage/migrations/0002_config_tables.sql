-- Wave 1.5 config tables (ADR-0003). Recipes, costs, and SKU mappings move
-- out of YAML into SQLite; YAML becomes seed-only. Mirrors the frozen
-- dataclasses in types.py (Recipe, RecipeIngredient, SkuMapping) with the
-- two new fields ADR-0003 introduces: skus.unit (decision 3) and the
-- gross-input / net-stored cost shape with per-entry vat_inclusive
-- (decision 4). Decimals are stored as TEXT so Decimal round-trips exactly
-- (mirrors sell_price TEXT in 0001_initial.sql).

CREATE TABLE IF NOT EXISTS skus (
    sku_id                      TEXT NOT NULL PRIMARY KEY,
    name                        TEXT NOT NULL,
    segment                     TEXT NOT NULL,           -- 'cafe' | 'bar'
    unit                        TEXT,                    -- 'g' | 'ml' | 'unit'; NULL until confirmed (ADR-0003 decision 3)
    yield_units                 INTEGER,                 -- NULL means inherit from the recipe row
    target_gross_margin_pct     TEXT,                    -- Decimal as TEXT; NULL when unset
    created_at                  TEXT NOT NULL,
    created_by                  TEXT NOT NULL            -- 'migration' for seeded rows
);

CREATE TABLE IF NOT EXISTS recipes (
    sku_id                      TEXT NOT NULL PRIMARY KEY REFERENCES skus(sku_id),
    name                        TEXT NOT NULL,
    segment                     TEXT NOT NULL,           -- denormalised from skus for query convenience
    yield_units                 INTEGER NOT NULL DEFAULT 1,
    target_gross_margin_pct     TEXT                     -- Decimal as TEXT; NULL when unset
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    sku_id              TEXT NOT NULL REFERENCES recipes(sku_id),
    ingredient_sku_id   TEXT NOT NULL,                   -- the input SKU (not FK-constrained: a recipe may reference an unpriced SKU)
    quantity            TEXT NOT NULL,                   -- Decimal as TEXT, in the ingredient's canonical unit
    position            INTEGER NOT NULL,                -- stable row ordering within the recipe
    PRIMARY KEY (sku_id, ingredient_sku_id, position)    -- same ingredient may appear twice (e.g. water in two stages)
);

CREATE TABLE IF NOT EXISTS costs (
    sku_id                  TEXT NOT NULL PRIMARY KEY,
    pack_price              TEXT,                        -- Decimal as TEXT; NULL for migrated per-unit-only rows (Step 3 captures this)
    pack_quantity           TEXT,                        -- Decimal as TEXT; NULL until Step 3
    vat_inclusive           INTEGER NOT NULL DEFAULT 0,  -- 0/1 bool; ADR-0003 decision 4
    price_per_unit_net      TEXT NOT NULL,               -- Decimal as TEXT; derived on write (gross / 1.07 when vat_inclusive)
    updated_at              TEXT NOT NULL,
    updated_by              TEXT NOT NULL                -- 'migration' for seeded rows
);

CREATE TABLE IF NOT EXISTS mappings (
    item_id         TEXT NOT NULL PRIMARY KEY,           -- one mapping per Loyverse item
    sku_id          TEXT NOT NULL REFERENCES skus(sku_id),
    updated_at      TEXT NOT NULL,
    updated_by      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name      TEXT NOT NULL,                       -- 'recipes' | 'costs' | 'mappings' | 'skus'
    pk              TEXT NOT NULL,                       -- the changed row's primary key
    field           TEXT NOT NULL,                       -- the changed column (empty for whole-row insert/delete)
    old_value       TEXT,
    new_value       TEXT,
    changed_by      TEXT NOT NULL,                       -- the assignee_id from the auth session
    changed_at      TEXT NOT NULL,
    session_id      TEXT                                 -- groups edits made in one browser session for session-revert
);
