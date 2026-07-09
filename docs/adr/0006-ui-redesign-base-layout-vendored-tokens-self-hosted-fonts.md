# ADR-0006: Base-layout templates, vendored design tokens, self-hosted fonts

Date: 2026-07-09

## Status

Accepted.

## Context

Wave 3 dresses the tool in the venue's own brand identity (the "Tangerine /
Tangerine Taps" design system: cream paper ground, hard ink keylines, Space
Mono body + Hobo BT display, square corners, no shadows). The handoff ships
high-fidelity references plus a token bundle (colours / typography / spacing
CSS, an `@font-face` sheet, and the display font files).

Two facts about the existing frontend shape the decision:

1. **Every template is a standalone HTML document.** Each repeats its own
   `<!doctype>`, `<head>`, HTMX `<script>`, and stylesheet link — there is no
   Jinja inheritance anywhere in the repo. The redesign introduces genuinely
   shared chrome (a sticky app header, a sticky 4-cell bottom nav, per-screen
   tab rows) that would otherwise be copy-pasted across 8+ templates and drift.
2. **The stack already pulls HTMX from a CDN** (ADR-0002), so an external
   runtime dependency is not unprecedented — but the body typeface is a
   different risk class than a JS enhancement.

The design system's `fonts.css` pulls Space Mono (the **body** font for the
whole UI) and Kavoon (a fallback only) from Google Fonts, and `@font-face`s the
bundled commercial display faces (Hobo BT, TAN Nimbus).

## Decision

Three coupled decisions for the redesign's foundation:

1. **Introduce a base layout.** Add `_base.html` (`{% extends %}` + `{% block %}`)
   and small chrome partials (`_app_header.html`, `_bottom_nav.html`, a tab
   row); every screen template — including the ones not being visually
   redesigned — extends it. This is the repo's first template inheritance.

2. **Vendor the design tokens verbatim.** Copy the design system's token CSS
   files (colours, typography, spacing) into `static/` unchanged, cruft and
   all, as the source of truth, and write one component stylesheet on top that
   consumes the `--vars`. This new stylesheet **replaces** `review.css`
   outright — no dual stylesheet. Out-of-scope screens are ported onto the base
   layout with generic on-brand element styling (plain but coherent).

3. **Self-host every font; no font CDN.** Bundle Space Mono (OFL) locally, serve
   Hobo BT + TAN Nimbus from `static/`, and drop the Kavoon Google-Fonts
   fallback entirely. The rendered tool has no runtime font dependency on
   Google.

The redesign stays in the presentation + view layer (templates, CSS, static
assets, route query params, small presenter helpers). The domain engines are
frozen; a genuinely new domain value would be a separate decision.

## Consequences

- **Hard to unwind once landed.** After 8+ templates extend `_base.html`,
  reverting to standalone documents is a large change — hence recording it.
- **Chrome lives in one place.** The header/bottom-nav/tab-row are edited once;
  screens cannot drift out of sync.
- **Tokens re-sync cleanly.** Because the token files are vendored verbatim, a
  future design-system update is a file copy, not a manual reconciliation.
- **No third-party font risk.** Google Fonts being slow or blocked on café wifi
  can no longer fail to load the body typeface at 9am — consistent with the
  venue's rebuild-from-scratch recovery posture. The cost is that font updates
  are a manual re-bundle rather than an automatic CDN refresh.
- **HTMX stays on its CDN.** This ADR does not change ADR-0002's HTMX-from-CDN
  choice; the self-hosting rule is scoped to fonts (the body typeface is a
  render-blocking dependency in a way an interaction enhancement is not).

## Considered and rejected

- **Chrome partials `{% include %}`d into standalone documents** (no base
  layout) — keeps the no-inheritance status quo but repeats `<head>`/scripts in
  every file; the drift this avoids is the whole point.
- **Hand-written stylesheet inlining only the tokens used** — smaller, but loses
  the verbatim-vendor re-sync path and re-opens "which token value was that?".
- **Keeping Space Mono / Kavoon on the Google Fonts CDN** (matching the HTMX
  pattern) — rejected because the body font is render-blocking for the entire
  tool, unlike an interaction library.

## References

- ADR-0002 (frontend stack; HTMX-from-CDN, unchanged)
- ADR-0004 decision 4 (deep-linkable URLs — why new controls are query params)
- `CONTEXT.md` → Brand display names
- Wave 3 UI/UX design handoff ("Tangerine Books" mobile redesign)
