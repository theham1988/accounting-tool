# 06 — Cafe stock counts → accrual COGS contribution

## What to build

Introduce weekly (or per-item cadence) cafe stock counts for perishables: milk, beans, pastries, etc. Each item type has its own count cadence based on shelf life (e.g. milk daily, beans weekly, pastries daily).

Consumed quantity for the period = beginning stock + purchases − ending stock. This feeds accrual COGS for cafe items, mirroring the keg inventory approach in slice 05.

Cafe stock count entry is a quick partner ritual — keep the UI/input path minimal since this is part of the 10–15 hrs/week partner labor.

## Acceptance criteria

- [ ] Per-item count cadence is configurable (daily, weekly)
- [ ] Stock count entry captures item, quantity, timestamp
- [ ] Consumed quantity per period = beginning + purchases − ending
- [ ] Consumed quantity feeds accrual COGS at latest approved purchase price
- [ ] End-to-end test feeds synthetic stock counts + purchases; asserts consumed quantity and COGS contribution

## Blocked by

- 04 — Recipe and per-item cost engine
