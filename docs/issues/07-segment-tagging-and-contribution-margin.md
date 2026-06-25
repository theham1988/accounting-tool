# 07 — Segment tagging (cafe vs bar) + segment contribution margin

## What to build

Tag every transaction, recipe, and item as `cafe` or `bar`. Tagging source is Loyverse category by default, with shift timestamp as fallback (8am–5pm = cafe, 5pm–10pm = bar).

Compute per segment, per period:
- Revenue
- Variable costs (COGS + direct labor if tracked)
- Contribution margin = revenue − variable costs

Fixed costs are explicitly NOT allocated to segments — they stay at entity level (handled in slice 08). Segment contribution margin is the only segment profitability number.

Flag a segment red when its contribution margin for the period is < 0. The "segment failing" threshold is CM ≥ 0; nothing else (no fixed-cost allocation, no percentage-of-revenue gate). This was the only threshold that didn't blow up the data model.

## Acceptance criteria

- [ ] Items, recipes, and transactions carry a `cafe` or `bar` segment tag
- [ ] Default tagging is Loyverse category, with shift-timestamp fallback
- [ ] Per-segment revenue, variable costs, and contribution margin are computed for any period
- [ ] Fixed costs are not allocated to segments
- [ ] A segment whose CM < 0 is flagged red
- [ ] End-to-end test feeds synthetic sales + costs split across segments; asserts per-segment CM and red flags

## Blocked by

- 04 — Recipe and per-item cost engine
