# ADR-0002: FastAPI + Jinja2 + HTMX for the frontend

Date: 2026-06-28

## Status

Accepted.

## Context

The accounting engine is pure Python with frozen dataclasses — a pure function
takes inputs and returns a result object (e.g. `build_daily_review(...) ->
DailyReview`). The frontend must render those result objects to partners on a
responsive web form factor (mobile-first for capture, desktop-class for review,
per `CONTEXT.md`).

The decision is between:

- **FastAPI + Jinja2 + HTMX** — server-rendered HTML, HTMX for interactivity,
  one language (Python) end-to-end.
- **React (or similar SPA) + FastAPI JSON API** — two languages, build pipeline,
  client-side state management, richer interactivity.
- **Streamlit / Gradio / Dash** — Python-native data-app frameworks; fastest to
  a first UI; framework ceiling on operational capture flows.
- **Next.js full-stack** — React frontend with API routes; would still need to
  call the Python engine over HTTP, adding a transport boundary for no gain.

The tool's primary surfaces are read-heavy tables of numbers (daily 9am review,
monthly P&L). Capture flows are small forms (keg weigh, cash close). The team
maintaining this is small.

## Decision

**Use FastAPI + Jinja2 + HTMX** for the frontend.

Each engine function becomes a FastAPI route that calls it and renders a Jinja2
template with the result. HTMX handles interactivity (form submission, partial
reloads, the "Sync now" button) via HTML fragments. No client-side state, no
build step, no npm dependencies, no JSON serialization layer.

## Consequences

- **One language end-to-end.** The team maintains Python only — the single
  biggest predictor of "can a small team keep this alive for years."
- **Zero impedance mismatch.** `build_daily_review(...)` returns a `DailyReview`;
  the template iterates its fields. No DTO layer, no client cache, no
  re-fetching.
- **No build pipeline.** No bundler, no transpiler, no `node_modules`. Edits to
  templates take effect on next page load.
- **Mobile-first is achievable.** Responsive HTML + CSS works on phones; HTMX
  form submissions are ordinary POSTs; PWA install is a manifest away.
- **Escape hatch preserved.** A specific surface that genuinely needs SPA-grade
  interactivity (e.g. a complex receipt-correction UI) can adopt React on one
  route without rewriting the rest. HTMX does not lock the codebase out.
- **Smaller talent pool.** HTMX is less familiar than React. If outside help is
  brought in, the available developer pool is narrower. Accepted as a
  consequence of the one-language benefit.

## Considered and rejected

- **React SPA** — two languages, build pipeline, unjustified for read-heavy
  table surfaces. The complexity earns its keep on highly interactive surfaces
  (charts, drag-and-drop, complex filters) that this tool does not have.
- **Streamlit / Gradio** — hits its framework ceiling fast on operational
  capture flows (the 5pm handoff block, mobile forms, multi-step entry). Great
  for internal dashboards; awkward for operational tools.
- **Next.js full-stack** — would require a Python-to-JS transport boundary for
  no benefit; the engine already lives in Python.

## References

- PRD "Open items": deployment target
- `CONTEXT.md` → Stack, Form Factor
- Wave 1 PRD (this ADR's first application)
