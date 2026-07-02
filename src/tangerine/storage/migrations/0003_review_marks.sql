-- Wave 1.5, Slice 5: when each partner last reviewed the config changes.
--
-- The 9am review's "N changes since last review" link counts audit_log
-- entries newer than this mark; the "Mark as reviewed" button on the audit
-- page moves the mark. Per-partner (not global) because each partner
-- sanity-checks the diff for themselves — Noi marking the changes reviewed
-- must not silence Daniel's nag.

CREATE TABLE IF NOT EXISTS review_marks (
    assignee_id     TEXT NOT NULL PRIMARY KEY,           -- the partner (auth session's assignee_id)
    reviewed_at     TEXT NOT NULL                        -- UTC ISO-8601: when they last marked the log reviewed
);
