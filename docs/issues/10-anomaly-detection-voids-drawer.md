# 10 — Anomaly detection on voids + drawer variance

## What to build

Rules-based anomaly detection over the cash and void history. There is no on-site manager; the tool must do the segregation-of-duties work that a manager would otherwise do.

Initial rules (keep simple, defer ML tuning to a later slice — out of scope per PRD):
- Voids: void rate per cashier above venue median for the period
- Voids: void clustering at peak hours (configurable peak window)
- Drawer: drawer-short rate per cashier above threshold
- Drawer: drawer short three shifts in a row by the same cashier

Flags are surfaced in the 9am daily review (slice 11).

## Acceptance criteria

- [ ] Void rate per cashier is computed and compared to venue median
- [ ] Void clustering at peak hours is detected
- [ ] Drawer-short rate per cashier is computed and compared to threshold
- [ ] Consecutive short shifts by same cashier are flagged
- [ ] Flags include enough context (cashier, period, the offending pattern) to act on
- [ ] End-to-end test feeds synthetic void + drawer history; asserts expected flags fire and clean history produces no flags

## Blocked by

- 02 — Loyverse API sync (voids source)
- 09 — Cash drawer reconciliation
