-- Issue #95: the spend-bucket controlled vocabulary for cash-spend rows
-- (parent #82 "model cash-basis supplier spend"). Each cash-spend row that
-- slice #96 will land FKs to exactly one bucket; the bucket is the
-- aggregation key that produces the HTML's per-bucket cost breakdown
-- (taps / kitchen / coffee / bakery / staff / rent).
--
-- Controlled vocabulary, not free-form and not enum (issue #82 decision D):
-- free-form lets "coffee" / "Coffee" / "cof" drift and breaks the cost-side
-- / revenue-side mirror; enum makes adding a bucket a code change + migration,
-- and the vocabulary genuinely evolves. A partner-managed table is the right
-- shape. Cash-spend-only: deliberately not shared with fixed_costs.category
-- (that overlap on staff/rent is HTML-column coincidence, not a taxonomy).
--
-- Retire vs. delete mirrors fixed_costs' ending-vs-deleting (ADR-0004
-- decision 3): retiring a bucket soft-flags it (it stays in the table so
-- historical aggregation stays honest); hard-deleting an empty bucket
-- removes it. The route-level guard for "rows reference this bucket" ships
-- now; the FK constraint itself lands with slice #96.

CREATE TABLE IF NOT EXISTS spend_buckets (
    bucket_id   TEXT NOT NULL PRIMARY KEY,        -- stable slug; cash-spend rows FK to this
    name        TEXT NOT NULL,                    -- display label shown in the picker and on the page
    retired_at  TEXT,                             -- ISO-8601 timestamp a partner soft-retired the bucket; NULL = live
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL                     -- 'migration' for the six seeded rows
);
