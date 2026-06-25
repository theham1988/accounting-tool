# 11 — Daily 9am review view

## What to build

The single daily review surface. At 9am, a partner opens this view and sees everything that needs attention from yesterday:

- Yesterday's revenue, COGS, gross margin
- Contribution margin per segment (cafe / bar) with red flags where CM < 0
- Top and bottom items by margin and sell volume
- Items whose actual margin is below their set target
- Anomaly flags from slice 10 (voids, drawer variance, clustering)
- Items sold without recipe mapping
- Progress toward 10,000 THB/day target (running 7-day average vs target)

The view is designed for fast scanning — the partner's job is to spot anything that needs action, not to study every line. Layout should make flags and red items visually dominant.

## Acceptance criteria

- [ ] View shows yesterday's revenue, COGS, gross margin
- [ ] Segment contribution margins displayed with red flags for CM < 0
- [ ] Top and bottom items by margin and volume visible
- [ ] Below-target-margin items flagged
- [ ] Anomaly flags from slice 10 appear
- [ ] Unmapped items flagged
- [ ] 7-day rolling average vs 10K THB/day target shown
- [ ] End-to-end test feeds a synthetic yesterday; asserts all expected sections render with the right numbers/flags

## Blocked by

- 07 — Segment tagging and contribution margin
- 10 — Anomaly detection
