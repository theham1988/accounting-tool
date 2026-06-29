## Parent

- #14 (Wave 1 — The 9am Review Spine)

## What to build

The fifth vertical slice of Wave 1. Add the small UX interactions that make the daily-review surface actually pleasant to use for two weeks of dogfooding, and turn silent failures into readable error states.

End-to-end behavior:

- **Day navigation**: a date input on the review page HTMX-GETs `/review?day=YYYY-MM-DD` and swaps the review body. Future and out-of-range dates surface a readable "no data for that day" state rather than an empty page.
- **Sync UX polish**: the "Sync now" button (from Slice 3) shows "Syncing..." while in flight (HTMX indicator), and on completion the result fragment shows rows ingested, menu changes, and any errors. A subsequent refresh shows the new data.
- **Error states for failure modes** that would otherwise be silent 500s:
  - stale data (sync hasn't run for >24h) — a banner at the top of the review: "Last sync was N days ago — Sync now?"
  - empty store (no sales yet) — a friendly empty state with a prominent "Sync now" button
  - missing recipes/SKU mappings for sold items — already surfaced as `unmapped_items`, but the section's wording should make clear it's actionable
- **Last-sync indicator**: somewhere on the review page, the timestamp of the most recent successful sync is shown, so a partner can tell at a glance whether they're looking at fresh data.

No new routes — this slice is HTMX interactions on existing routes plus error-state rendering. The `/review?day=` route exists from Slice 2; this slice wires the day input to it.

## Acceptance criteria

- [ ] Selecting a different date in the date input HTMX-swaps the review body to that day's review.
- [ ] A date with no data shows a readable empty state, not a broken page.
- [ ] The "Sync now" button shows "Syncing..." while in flight.
- [ ] On sync completion, the result fragment describes the sync, and a follow-up refresh shows the new data.
- [ ] A stale-data banner appears at the top of the review when the last sync was more than 24 hours ago, with a "Sync now" affordance.
- [ ] The empty-store state shows a friendly message and a prominent "Sync now" button.
- [ ] The last successful sync's timestamp is visible somewhere on the review page.
- [ ] UI seam tests extended to cover: day navigation swaps the body; sync indicator appears during in-flight; stale-data banner appears when last sync is old; empty store renders the empty state.
- [ ] Tests assert on visible HTML content, not implementation details.

## Blocked by

- #16 (Slice 2 — FastAPI app + Jinja2 daily review)
- #17 (Slice 3 — Loyverse sync wiring + /sync route + cron entrypoint)
