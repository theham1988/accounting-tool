-- Issue #66: heal historical sales rows after the UTC→Asia/Bangkok parser fix.
--
-- The parser used to take Loyverse ``created_at`` (UTC) directly: the sale's
-- ``timestamp`` was the UTC date and the shift-stamped ``segment`` was decided
-- by the UTC hour. Thailand is UTC+7, so the bar shift (local 17:00–22:00)
-- stamped *cafe* and night-time sales bucketed to the wrong local day.
--
-- The fix converts to Asia/Bangkok before bucketing and shift-stamping, so
-- *new* sales are correct from the next sync. But the sales table's
-- ``INSERT OR IGNORE`` dedup means a re-sync does NOT heal rows that are
-- already there with the wrong date/segment — the row is silently kept as-is.
-- Pre-existing rows also have no original ``created_at`` to recompute from:
-- slice 02 stored only the derived date, never the source timestamp.
--
-- This migration does two things:
--
--   1. Adds a ``created_at`` column so future syncs persist the source UTC
--      timestamp. From #66 forward the table is self-healing — a future fix
--      can re-derive date/segment from ``created_at`` without re-fetching.
--
--   2. Drops every pre-existing sales row. They are unrecoverable from the
--      DB alone (no source ``created_at``); the next sync detects an empty
--      sales table, treats it as a first run, and backfills the last 30 days
--      (``run_sync``'s ``BACKFILL_DAYS``), this time with correct local-time
--      date/segment and ``created_at`` populated. The venue went live in
--      early July 2026, so the backfill window covers all production data.

ALTER TABLE sales ADD COLUMN created_at TEXT;

DELETE FROM sales;
