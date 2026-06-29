## Parent

- #14 (Wave 1 — The 9am Review Spine)

## What to build

The second vertical slice of Wave 1. Wrap the persisted data and engine (from Slice 1) in a FastAPI + Jinja2 + HTMX web layer that renders the daily 9am review as HTML in a browser.

End-to-end behavior:

- Opening `http://localhost:8000/` in a browser renders **yesterday's** daily 9am review as HTML.
- The review shows, in one fast-scan page:
  - yesterday's revenue, COGS, and gross margin
  - per-segment contribution margin (cafe vs bar) with a red flag where a segment's CM is below zero
  - top three and bottom three items by gross margin
  - top three and bottom three items by units sold
  - items whose actual margin is below their set target
  - items sold without a recipe mapping (dedicated "unmapped" section)
  - the 7-day rolling-average daily gross margin vs the 10,000 THB/day target, with a met/missing indicator
- No login yet (Slice 4 adds it). No sync button yet (Slice 3 adds it). Reads from the SQLite store + config Slice 1 built.
- CSS is **mobile-first responsive** — the same template renders on a phone and a desktop, because capture happens in-venue (phone) and review happens at home (laptop).
- Renders in well under a second.

Routes added:

- `GET /` — the daily 9am review, defaulting to yesterday
- `GET /review?day=YYYY-MM-DD` — the review for a specific day (day navigation comes in Slice 5; the route is here now)

The engine's `build_daily_review(...)` is called unchanged. The template iterates the resulting `DailyReview` object's fields and renders them.

A FastAPI app factory wires up routes, dependency-injects the `Source` adapter from Slice 1, and selects the Jinja2 template directory. Templates and static assets live under conventional locations.

## Acceptance criteria

- [ ] `GET /` returns HTML showing yesterday's review with all sections (headline numbers, segment CM with flags, four rankings, below-target, unmapped, goal progress).
- [ ] Numbers in the rendered HTML match what `build_daily_review(...)` returns for the same date against the same persisted data.
- [ ] `GET /review?day=YYYY-MM-DD` returns that day's review.
- [ ] The template renders correctly on a phone-width viewport (responsive CSS).
- [ ] Per-segment CM rows show a red flag where CM < 0.
- [ ] Items excluded from totals (unmapped / unknown-price) appear in the unmapped section, not in rankings.
- [ ] The goal progress section shows the rolling average, the target, a met/missing indicator, and days in window.
- [ ] Renders in under one second against a few weeks of seeded data.
- [ ] UI seam tests added (e.g. `tests/test_daily_review_ui_e2e.py`) using FastAPI's test client, parsing the rendered HTML and asserting on the visible numbers/flags (not on implementation details).
- [ ] Tests seed data via the SQLite store from Slice 1; the Loyverse HTTP boundary is stubbed using the existing `StubHttp` pattern.

## Blocked by

- #15 (Slice 1 — SQLite persistence, config loader, and Source adapter)
