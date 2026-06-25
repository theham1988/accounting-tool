# 05 — Keg inventory via weekly weighing → beer yield → accrual COGS

## What to build

Introduce keg inventory by weekly weighing. Per brand:
- Tare weight of the empty keg (entered once, edit rarely — draught rotation is low)
- Density approximation per brand (default to water density; documented ~0.5–1.5% volume tolerance)
- Gross weight at each weekly weigh

From gross weight, compute beer volume: `(gross − tare) ÷ density`.

Yield computation:
- Theoretical pours per 20L keg at glass size (e.g. 40 × 500ml)
- Actual yield from weekly weigh: aggregate beer volume consumed vs. Loyverse rung-up pours
- Variance (loss %) surfaced but not attributed to individual kegs

Beer volume consumed feeds accrual COGS for the period: beer cost = consumed volume × cost per ml (from latest approved keg purchase price).

Note: this slice produces the periodic-inventory number that makes accrual COGS work (see slice 08). Without this slice, monthly COGS would swing violently with delivery timing.

## Acceptance criteria

- [ ] Per-brand keg records exist: tare weight, density approximation
- [ ] Weekly weigh entry captures gross weight per keg (or per keg batch) per brand
- [ ] Beer volume is computed: `(gross − tare) ÷ density`
- [ ] Actual yield vs. theoretical is computed and loss % surfaced
- [ ] Consumed beer volume feeds COGS for the period at the latest approved keg cost
- [ ] End-to-end test feeds synthetic weigh-ins + sales; asserts beer volume consumed and COGS contribution
- [ ] Density approximation error (0.5–1.5%) is documented in the code/output, not silently absorbed

## Blocked by

- 04 — Recipe and per-item cost engine
