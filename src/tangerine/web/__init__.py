"""FastAPI + Jinja2 + HTMX web layer (Wave 1, Slice 2).

Wraps the Wave-1-Slice-1 persistence + config stack in a thin web layer that
renders the daily 9am review as HTML. The engine's ``build_daily_review(...)`` is
called unchanged; the routes and templates are a presentation adapter over its
result objects (ADR-0002).

Scope of this slice:
  - ``GET /``               — yesterday's review (default landing surface).
  - ``GET /review?day=...`` — the review for a specific day (the day-navigation
                              control itself arrives in Slice 5).

No login (Slice 4), no sync button (Slice 3). The app factory takes the DB and
config paths as explicit kwargs with env-var defaults, mirroring the CLI's
``main()`` so tests can drive it in-process without env mutation.
"""
