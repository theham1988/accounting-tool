-- Wave 2 slice 3 (ADR-0004 decision 3): entity-level fixed costs. A row is
-- recurring (applies every month from `period` until `ended_at`'s month,
-- inclusive) or one-off (applies only in `period`). Amounts are Decimal as
-- TEXT, like every money column. Fixed costs are never allocated to a
-- segment — there is deliberately no segment column.

CREATE TABLE IF NOT EXISTS fixed_costs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,
    category        TEXT NOT NULL,               -- rent | utilities | staff_salaries | insurance | other
    amount          TEXT NOT NULL,               -- Decimal as TEXT; the monthly amount in THB
    kind            TEXT NOT NULL,               -- 'recurring' | 'oneoff'
    period          TEXT NOT NULL,               -- 'YYYY-MM': the month a one-off applies to, or a recurring row's first month
    day_of_month    INTEGER NOT NULL DEFAULT 1,  -- informational (when in the month the cost lands)
    ended_at        TEXT,                        -- date a recurring row stopped applying; NULL = still applies
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL
);
