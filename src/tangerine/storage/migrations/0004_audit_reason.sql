-- Wave 1.5, Slice 5 follow-up: reshape audit_log for whole-row snapshots
-- and revert reasons.
--
-- Slice 5 records whole-row before/after snapshots in old_value/new_value
-- (a revert undoes a *save action*, and one save touches several columns
-- at once), which left 0002's per-field `field` column dead — every insert
-- wrote ''. Dropped rather than left as a column whose documented contract
-- ("the changed column") nothing honours.
--
-- `reason` is the optional why a partner types when reverting (ADR-0003:
-- the audit log is the record of intent — who changed what, when,
-- why-noted-as-revert-reason). NULL for ordinary edits and for reverts
-- where the partner gave none.

ALTER TABLE audit_log DROP COLUMN field;
ALTER TABLE audit_log ADD COLUMN reason TEXT;
