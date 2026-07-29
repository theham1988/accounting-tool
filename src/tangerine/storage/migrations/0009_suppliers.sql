-- Issue #94: suppliers reference surface (parent #82, "model cash-basis
-- supplier spend"). A controlled vendor list the cash-spend entry surface
-- (slice #96) will FK into. Vendors have no lifecycle (no recurring / one-off
-- / ended-at — those are fixed-cost concepts); this is plain CRUD on a
-- controlled list, because free-form lets "Makro" / "Makro Phuket" drift
-- and break per-vendor aggregation (decision 2a of #82).
--
-- The dormant `Supplier(supplier_id, name)` dataclass in types.py sketches
-- the shape; this table is its persistence (decision E of #82: reuse the
-- type in place). The FK from `cash_spend` lands with #96; for now the
-- table ships empty and the route-level guard in the store refuses a
-- delete that would break referential integrity once that FK is real.

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id   TEXT NOT NULL PRIMARY KEY,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL
);
