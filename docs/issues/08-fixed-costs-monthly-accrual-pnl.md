# 08 — Fixed costs entry + monthly accrual P&L

## What to build

Introduce entity-level fixed cost entry: rent, utilities, shared staff salaries, insurance, etc. Fixed costs are recorded against the entity (the whole business), never against a segment.

Build the monthly reconciliation view using proper accrual-basis COGS:
- Beginning inventory value + purchases − ending inventory value = COGS
- Ending inventory comes from keg weighs (slice 05) and cafe stock counts (slice 06)
- Revenue is from Loyverse sales (slice 02), recognized by transaction timestamp
- Payables (cash-flow view) tracked separately by invoice date — both views available

Monthly P&L output:
- Revenue per segment
- COGS per segment (accrual basis)
- Contribution margin per segment
- Fixed costs at entity level
- Entity net profit = sum of segment CM − fixed costs
- Comparison to 10,000 THB/day target (× days in month)

## Acceptance criteria

- [ ] Fixed cost entries exist: amount, category, period
- [ ] Monthly view computes accrual COGS using beginning + purchases − ending inventory
- [ ] Segment contribution margin shown alongside entity net profit
- [ ] Entity net profit compared to 10K THB/day × days in month
- [ ] Cash-flow view (payables by invoice date) is available separately from accrual view
- [ ] End-to-end test feeds synthetic inventory + purchases + fixed costs; asserts monthly P&L numbers

## Blocked by

- 05 — Keg inventory via weekly weighing
- 06 — Cafe stock counts
- 07 — Segment tagging and contribution margin
