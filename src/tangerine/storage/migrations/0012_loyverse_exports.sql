-- Issue #102 (parent spec #100, map #62): the Loyverse cost-mirror paper trail.
-- Every confirmed export leaves one row here recording who pressed confirm,
-- when, how many rows the file carried, how many differed from Loyverse's
-- current cost, and the per-SKU drift payload the partner was shown and
-- approved. The drift badge (slice 3, issue #103) and any future export-
-- history surface read this newest-first.
--
-- This is a **dedicated table**, deliberately not a new ``kind`` on
-- ``audit_log``. The Q5 grilling resolution (issue #70) and spec #100
-- settled this: ``audit_log`` is for in-Books config edits and feeds the
-- 9am "N changes since last review" count that ``unreviewed_changes`` drives
-- (the #94/#95/#96 pattern). A Loyverse-bound export would pollute that
-- count — it is not a config edit, it is a mirror action whose paper trail
-- answers "what did we send Loyverse and when?" so staleness is detectable
-- from inside Books. Writes here therefore bypass ``_record_audit`` and
-- land directly on this table; ``unreviewed_changes`` is unaffected by
-- construction.
--
-- A zero-drift confirm still writes a row (``changed_count = 0``,
-- ``drift_payload = '[]'``) — PRD user story 9: the null-state proof that
-- "the mirror was confirmed current on <date>" is visible rather than
-- inferred from absence.
--
-- ``partner_id`` is TEXT (the confirming partner's assignee id), matching
-- ``audit_log.changed_by`` and ``cash_spend.created_by`` in shape but with
-- no FK — assignees live in the file-based ``config/assignees.yaml``
-- (ADR-0003), not this database. ``confirmed_at`` is TEXT (UTC ISO-8601)
-- stamped by the store's injectable clock (the same ``now=`` seam
-- ``cash_spend`` and every other audited write uses), so tests pin the
-- timestamp. ``drift_payload`` is TEXT holding a JSON array of
-- ``{sku, name, loyverse_cost, books_cost}`` for the changed rows — the
-- exact diff the prepare step rendered (issue #101), so "what did we
-- overwrite and when" is answerable later without reconstruction.

CREATE TABLE IF NOT EXISTS loyverse_exports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id     TEXT NOT NULL,                       -- confirming partner's assignee id
    confirmed_at   TEXT NOT NULL,                       -- UTC ISO-8601, from the store's injectable clock
    item_count     INTEGER NOT NULL,                    -- rows in the emitted file
    changed_count  INTEGER NOT NULL,                    -- rows where Books' number differed from Loyverse's (0 on a no-drift confirm)
    drift_payload  TEXT NOT NULL                        -- JSON array of {sku,name,loyverse_cost,books_cost} for the changed rows
);

CREATE INDEX IF NOT EXISTS idx_loyverse_exports_confirmed_at
    ON loyverse_exports(confirmed_at);
