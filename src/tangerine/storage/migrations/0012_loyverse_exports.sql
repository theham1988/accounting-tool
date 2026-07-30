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
-- timestamp.
--
-- ``changed_count`` vs ``drift_payload`` — the intentional asymmetry (issue
-- #105, decision option 1). The two fields answer genuinely different
-- questions and are deliberately NOT aligned:
--
--   * ``changed_count`` answers "how many ``Cost`` cells in this file moved
--     on import?" — the partner-facing, import-shape fact. A cell "moves"
--     when (a) Loyverse had a ``Cost`` and it disagreed with Books' number
--     (a DIFFERS row), or (b) Loyverse's ``Cost`` was blank and Books is
--     adding one (a blank-fill FILLED row). A rounded match does not count
--     as a move.
--   * ``drift_payload`` answers "where did Books overwrite a value Loyverse
--     actually held?" — the audit-trail fact. It carries the DIFFERS rows
--     only; each entry is ``{sku, name, loyverse_cost, books_cost}``. A
--     blank-fill is NOT an overwrite of a Loyverse value (there was nothing
--     there), so blank-fill FILLED rows count toward ``changed_count`` but
--     do NOT appear in the payload.
--
-- So ``len(drift_payload)`` can be strictly less than ``changed_count``.
-- The clearest case — a first-ever confirm where Books fills Loyverse's
-- blanks — records e.g. ``changed_count = 2, drift_payload = '[]'``, and
-- that is correct, not contradictory: two cells moved (Loyverse gained
-- costs it didn't have), but Books overwrote zero Loyverse-held values.
-- Aligning the payload to the count (option 2) would force
-- ``loyverse_cost: null`` entries into the payload, breaking its "both
-- values present" shape and its match with what the diff page rendered;
-- adding a third ``differs_count`` field (option 3) would add a count
-- column for a distinction these comments + the ``cost_mirror`` docstrings
-- already carry. Decision recorded in #105; this comment is the schema-
-- level leg of that decision.

CREATE TABLE IF NOT EXISTS loyverse_exports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id     TEXT NOT NULL,                       -- confirming partner's assignee id
    confirmed_at   TEXT NOT NULL,                       -- UTC ISO-8601, from the store's injectable clock
    item_count     INTEGER NOT NULL,                    -- rows in the emitted file
    changed_count  INTEGER NOT NULL,                    -- cells that moved (differs + blank-fills); see #105 — a blank Loyverse Cost that Books fills counts as a move, a rounded match does not
    drift_payload  TEXT NOT NULL                        -- DIFFERS rows only (where Loyverse had a value to overwrite); blank-fill FILLED rows count toward changed_count but not here. See #105.
);

CREATE INDEX IF NOT EXISTS idx_loyverse_exports_confirmed_at
    ON loyverse_exports(confirmed_at);
