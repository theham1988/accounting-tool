# Wave 1.5 — The Config Authoring Surface

> Wave 1 built the spine: persistence, identity, sync, the daily 9am review.
> Wave 1.5 builds the surface that lets the partner actually *use* the engine
> — seeing which of the 232 Loyverse items are mapped, which SKUs are priced,
> and fixing gaps without touching YAML or git.
>
> This wave exists because the partner is not a coder, the YAML-plus-PR
> workflow has failed in practice, and the engine's margin numbers cannot be
> trusted until its inputs are correct. Visibility first; editing built on
> top.
>
> Cross-references: `docs/PRD-WAVE-1.md` (the spine this builds on),
> `docs/adr/0003-config-authoring-surface-and-source-of-truth.md` (the four
> hard-to-reverse decisions this wave implements), `CONTEXT.md`.

## Problem Statement

Wave 1's 9am review surfaces real margin numbers — but only for items the
partner has mapped, costed, and recipe'd correctly. Today the partner cannot
see which items those are without scanning a 1,400-line `recipes.yaml` and a
212-line `costs.yaml` by eye. Every morning the review's `needs_attention`
strip throws a few unmapped items at the partner reactively, and each one
becomes a full YAML-edit-and-PR cycle to fix.

The result: the partner has stopped trusting the margin numbers, because the
inputs are visibly incomplete and invisibly wrong in places, and the path to
fix either is a workflow the partner cannot fluently perform. The tool is
producing numbers the partner doesn't believe, for reasons the partner cannot
see.

The smallest version that fixes this is a **visualization of the whole menu's
mapping health** (so the partner can see what's broken) plus an **in-browser
editor** (so the partner can fix it without YAML or git), plus a **bulk upload
path** for the initial mapping load (so the partner isn't typing 400 rows one
at a time). Recipes are the highest-stakes input and the most complex shape,
so they get a dedicated editor with inline ingredient creation rather than a
spreadsheet.

Wave 1.5 is done when every regular Loyverse item is mapped to a fully-priced
recipe, the partner has done that work entirely in the UI without opening a
terminal, and the 9am review's `needs_attention` strip has been empty for
three consecutive mornings.

## Solution

A config-authoring surface added to the existing FastAPI + Jinja2 + HTMX web
app. Four user-facing surfaces plus a data migration:

- **SKU view** (the partner's workspace, the landing). One row per SKU. Shows
  mapping health, recipe completeness, ingredient pricing, derived per-unit
  cost. "New SKU" button. Distinguishes active / prep-internal / dangling
  SKUs.
- **Item coverage view** (the gap report). One row per Loyverse item, sorted
  so unmapped and unpriced bubble to the top. Click → assign an existing SKU
  or create a new SKU inline.
- **Recipe editor.** A recipe is a list of `(sku_id, quantity)` rows plus
  yield and optional target margin. Ingredient picker offers existing SKUs
  and an inline "create new ingredient" sub-form. Accepts shorthand (`1 tbsp`
  → 15) and converts to the SKU's unit before saving. Live cost preview per
  row and for the whole recipe.
- **Cost editor.** Pack price + pack quantity + `vat_inclusive` checkbox →
  derived per-unit price (net) shown live. Replaces the mental arithmetic
  today's `costs.yaml` comments document.
- **Spreadsheet upload** for mappings and costs (flat data, ~400 rows). The
  tool generates a template pre-filled with current state; the partner fills
  in the blanks offline; upload reconciles.
- **Audit log + revert.** Every edit records who/when/old/new. Per-change
  revert and per-session revert.
- **Daily review diff link.** "N changes since last review" behind a link on
  the 9am review; banner only if changes exist in the last 24h.

Underneath: a one-time migration moves `recipes.yaml`, `costs.yaml`, and the
`mappings` block into new SQLite tables; the YAML files become seed-only. The
`assignees.yaml` file stays file-based (auth identity, low-volume,
onboarding-via-config is a feature). The four sub-decisions that shape this
wave are recorded in ADR-0003.

## User Stories

### Visibility (the partner's biggest pain)

1. As a partner, I want to open the SKU view and see every SKU I've defined
   with its mapping status (how many sold items point at it), recipe
   completeness (does it have ingredients), and ingredient pricing (are all
   ingredients costed) — so I can see the whole catalog's health at a glance.
2. As a partner, I want unmapped, unpriced, and incomplete SKUs visually
   distinguished (red/yellow/green) so the broken ones bubble up without me
   scanning.
3. As a partner, I want to see which SKUs are "dangling" — neither sold nor
   used as an ingredient in any recipe — so I can spot mappings that point at
   the wrong SKU or clean up retired items.
4. As a partner, I want the item coverage view: every Loyverse item the sync
   has ever seen, with its mapping status, sorted so unmapped items are at
   the top — so I can do a bulk audit ("map everything") in one sitting.
5. As a partner, I want each item coverage row to link directly to either
   "assign to existing SKU" or "create new SKU…" — so the fix path is one
   click from the diagnosis.
6. As a partner, I want the daily review's `needs_attention` strip to deep
   link each unmapped item into the item coverage view filtered to that
   item — so a morning "fix this one" is a single click from the review.

### Editing recipes

7. As a partner, I want to open a SKU and edit its recipe as a list of
   `(ingredient, quantity)` rows — so I don't have to write YAML.
8. As a partner, I want the ingredient picker to offer only existing SKUs by
   default, so I can't accidentally type a `sku_id` that points at nothing.
9. As a partner, when I reach for an ingredient that doesn't exist, I want a
   "create new SKU…" option right there in the picker that opens a tiny form
   (sku_id, name, unit, price) inline — so I can finish the recipe without
   context-switching to another page.
10. As a partner, I want to enter quantities in the units I actually think in
    (`1 tbsp`, `2 knobs`, `1 pinch`) and have the editor convert to the
    canonical stored unit (ml/g/unit) based on the ingredient's `unit` field
    — so I don't have to remember that 1 tbsp of milk is 15 ml.
11. As a partner, I want a live cost preview on each recipe row
    (`0.65/g × 18g = 11.70 THB`) and a total recipe cost below — so I can see
    a typo before I save, not after.
12. As a partner, I want to set a target gross margin per recipe so
    below-target items are flagged in the daily review (already supported by
    the engine; this wave surfaces the input).

### Editing costs

13. As a partner, I want to enter a cost as (pack price, pack quantity, unit,
    VAT-inclusive checkbox) and have the editor show the derived per-unit net
    cost live — so I never do `380 ÷ 2000 ÷ 1.07` in my head.
14. As a partner, I want the cost editor to default the `vat_inclusive`
    checkbox to checked (Makro is my dominant supplier) but let me uncheck it
    for wet-market and no-VAT purchases — so the engine uses the right net
    cost per purchase.
15. As a partner, I want to see when a cost was last updated and by whom — so
    a suspicious margin at 9am can be traced to "did the butter price change
    yesterday?"
16. As a partner, I want every SKU's cost history visible (a small list of
    past prices with dates) so I can see whether a supplier has been creeping
    prices up.

### Bulk upload

17. As a partner, I want to download a spreadsheet template (CSV or XLSX)
    pre-filled with every Loyverse item and every known SKU — so I can fill
    in mappings and costs offline in Excel, where I'm comfortable.
18. As a partner, I want to upload the filled spreadsheet and see a preview
    of what will change before it lands — so a typo in row 147 doesn't
    silently corrupt the cost book.
19. As a partner, I want upload errors (unknown SKU reference, malformed
    number, missing column) reported per-row with the row number — so I can
    fix and re-upload without guessing what broke.

### Safety net (replaces the removed code-review gate)

20. As a partner, I want every config edit — recipe, cost, mapping,
    ingredient creation — recorded in an audit log with who/when/old-value/
    new-value — so there's always a paper trail.
21. As a partner, I want a "revert" button on each audit-log entry that
    undoes exactly that one change — so a surgical fix is one click when I
    know which change was wrong.
22. As a partner, I want a "revert this session" button that undoes a batch
    of edits — so a panic undo is available when I know I broke something
    but not what.
23. As a partner, I want the 9am review to show "N changes since last review"
    behind a link when any config edit has happened since yesterday's review
    — so I can sanity-check my own (and the other partner's) work before
    trusting today's numbers.
24. As a partner, I want that link to surface as a banner (not just a link)
    when changes happened in the last 24 hours — so I can't miss recent
    edits on quiet days when I might otherwise skip the diff.

## Implementation Decisions

### Modules to be built

- **SQLite tables for config** (`skus`, `recipes`, `recipe_ingredients`,
  `costs`, `audit_log`). Schema mirrors the frozen dataclasses with the new
  `unit` and `vat_inclusive` fields added per ADR-0003.
- **A config store** alongside the existing `SqliteLoyverseStore` — reads and
  writes recipes/mappings/costs from SQLite instead of from the in-memory
  objects the YAML loader produces today.
- **A one-time migrator** that runs on app startup if the config tables are
  empty: reads `recipes.yaml` / `costs.yaml`, derives the `unit` field from
  `costs.yaml` pack-size comments where unambiguous, sets `vat_inclusive`
  from the comment (true for Makro/ARO with pack size, false otherwise),
  flags ambiguous rows with a `[check]` marker surfaced in the UI.
- **FastAPI routes** for the four surfaces plus upload and audit:
  - `GET /skus` — the SKU view (workspace, landing for the authoring surface)
  - `GET /skus/{sku_id}` — recipe editor + cost editor for one SKU
  - `POST /skus/{sku_id}/recipe` — save recipe edits (HTMX)
  - `POST /skus/{sku_id}/cost` — save cost edits (HTMX)
  - `POST /skus` — create new SKU
  - `GET /items` — the item coverage view (gap report)
  - `POST /items/{item_id}/mapping` — assign item to SKU
  - `GET /upload` and `POST /upload` — spreadsheet upload for mappings/costs
  - `GET /audit` — the audit log
  - `POST /audit/{entry_id}/revert` — revert a single change
- **Jinja2 templates** rendering each surface, mobile-first, consistent with
  the review page's existing CSS.
- **HTMX wiring** for inline-create (the ingredient picker's "create new SKU"
  sub-form), live cost preview on recipe/cost edit, and per-row revert.

### Interfaces to be modified or extended

- `RecipeCatalog` — gains a SQLite-backed constructor (or the source adapter
  does). The engine itself (`build_daily_review`, `compute_item_margins`,
  etc.) is unchanged; it consumes the same `RecipeCatalog` and `CostBook`
  shapes it does today.
- `CostBook` — unchanged shape, but populated from SQLite instead of from the
  YAML loader at runtime.
- The `Source` adapter (`StoreSource` in `loyverse/source.py`) — its
  `recipes()`, `mappings()`, and `cost_book()` methods read from SQLite
  instead of from the in-memory catalog/cost objects captured at app
  construction.
- The config loader (`config/loader.py`) — its `load_recipes` / `load_costs`
  become the *seeder*, called only by the migrator on first run. They are no
  longer called at every app startup.

### Architectural decisions (cross-referenced)

- **Config moves into SQLite** — see ADR-0003 decision 1.
- **The code-review gate is removed** — see ADR-0003 decision 2.
- **Explicit `unit` field per SKU** — see ADR-0003 decision 3.
- **Gross-input / net-stored with per-entry `vat_inclusive` flag** — see
  ADR-0003 decision 4.
- **FastAPI + Jinja2 + HTMX** — see ADR-0002 (unchanged).

### Schema changes (SQLite, new)

- `skus` — `sku_id` (PK), `name`, `segment`, `unit` (`g`/`ml`/`unit`),
  `yield_units` (nullable, for prep recipes), `target_gross_margin_pct`
  (nullable), `created_at`, `created_by`.
- `recipes` — `sku_id` (PK, the SKU this recipe produces; 1:1 with `skus`),
  `name`, `segment` (denormalised from `skus` for query convenience),
  `yield_units`, `target_gross_margin_pct`. (Splitting the recipe header from
  its ingredient rows lets the editor save the header and rows
  independently.)
- `recipe_ingredients` — `sku_id` (the recipe), `ingredient_sku_id` (the
  input SKU), `quantity` (Decimal, in the ingredient's canonical unit),
  `position` (int, for stable row ordering). Composite PK
  `(sku_id, ingredient_sku_id, position)` so the same ingredient can appear
  twice (rare but possible — e.g. water added in two stages).
- `costs` — `sku_id` (PK), `pack_price` (Decimal, gross), `pack_quantity`
  (Decimal), `vat_inclusive` (bool), `price_per_unit_net` (Decimal, derived
  on save), `updated_at`, `updated_by`. One row per SKU (latest wins); cost
  history lives in `audit_log`.
- `audit_log` — `entry_id` (PK), `table`, `pk`, `field`, `old_value`,
  `new_value`, `changed_by`, `changed_at`, `session_id` (groups edits made
  in one browser session for session-revert).
- `mappings` — `item_id` (PK), `sku_id` (FK), `updated_at`, `updated_by`.
  Today this is a YAML block; promoting it to a table makes the item coverage
  view a single join.

### Sequencing within Wave 1.5

Each step independently useful; each can ship on its own:

1. **Migration + tables** — `skus`, `recipes`, `recipe_ingredients`,
   `costs`, `mappings`, `audit_log`. YAML seeder. Engine reads from SQLite.
   No UI yet. *Verifiable: 9am review shows identical numbers post-migration
   (except the VAT fix on clearly-Makro rows).*
2. **SKU view + item coverage view (read-only).** Partners can see the
   mapping health for the first time. Editing still goes through YAML +
   re-seed for now. *Verifiable: partner opens the SKU view and can answer
   "how many items are unmapped" without scanning YAML.*
3. **Cost editor + spreadsheet upload for mappings/costs.** The flat-data
   editing loop closes. *Verifiable: partner enters a new supplier price via
   the UI and sees tomorrow's margin update without touching YAML.*
4. **Recipe editor with inline ingredient creation.** The nested-data editing
   loop closes. *Verifiable: partner builds a new recipe end-to-end in the
   UI, including creating a new ingredient SKU inline.*
5. **Audit log + revert + daily review diff link.** The safety net lands.
   *Verifiable: partner makes a change, sees it in the diff the next morning,
   reverts it from the audit log.*

## Testing Decisions

### What makes a good test (inherited)

- Test external behaviour — the HTML the tool produces and the resulting
  margin numbers — not implementation details (how SQLite stores rows, how
  Jinja renders).
- Mock only genuine external boundaries: the SQLite connection (`:memory:`
  for tests). The Loyverse HTTP boundary is unchanged from Wave 1.
- Each test reads as a worked example: "given a partner enters pack price
  380 THB for a 2 kg block of butter with VAT inclusive, the cost book shows
  0.178 THB/g net and the latte recipe's margin rises to X."

### Seams

**One new seam, extending the existing UI seam.**

- `tests/test_config_authoring_ui_e2e.py` — tests the new FastAPI routes
  through FastAPI's test client, seeded with the migrated SQLite tables
  (run the migrator against a `:memory:` DB loaded from synthetic YAML).
  Assertions cover:
  - The SKU view shows mapping health (green/yellow/red rows).
  - The item coverage view shows unmapped items at the top.
  - The recipe editor saves a new recipe and the next review reflects it.
  - The cost editor's pack-price entry produces the right net per-unit cost.
  - Spreadsheet upload reconciles mappings and costs.
  - Audit log records every edit; revert undoes a single edit and a session.
  - The daily review diff link surfaces changes made since the last review.

The engine's existing twelve E2E seams are untouched. Wave 1's persistence
seam and UI seam are extended (the migrator runs in the existing test setup;
the SKU/recipe/cost routes join the daily-review routes under the same auth
gate).

### Modules NOT covered by these seams

- The engine itself — covered by existing seams; this wave doesn't touch it.
- Loyverse HTTP client — unchanged from Wave 1.
- The YAML loader — covered by existing unit tests; its role narrows to
  seeding, but its parsing behaviour is unchanged.

## Out of Scope

The following are deliberately deferred or explicitly not being built:

- **Editing `assignees.yaml` in the UI.** Auth identity is low-volume,
  onboarding-via-config is a deliberate feature (PRD user story 31), and the
  manager-onboarding story depends on it. The authoring surface covers
  recipes, mappings, and costs only.
- **Two-key approval.** Explicitly rejected by the partner ("we are too small
  to have gates"). The audit-and-revert safety net replaces it. See ADR-0003.
- **Supplier modelling.** VAT-ness lives on the cost entry, not on a supplier
  entity. Suppliers become first-class in Wave 3 (receipt approvals); this
  wave does not pre-build them.
- **Recipe versioning beyond the audit log.** Old recipe/cost values are
  recoverable via `audit_log` revert; we do not build a separate
  "recipe history" surface. If a partner needs to see "what was this recipe
  a year ago," the audit log is the source.
- **Multi-currency, advanced cost formulas, supplier-price comparison.**
  Out of scope per the original PRD; this wave does not introduce them.
- **Re-introducing a code-review gate.** The decision is recorded in
  ADR-0003 as accepted. If the audit-and-revert safety net proves
  insufficient in practice (silent corruptions slip past the morning diff),
  the path back is a new ADR superseding 0003 — not a config flag.

## Further Notes

### Relationship to Wave 2 and Wave 3

Wave 1.5 sits *between* Wave 1 and the rest of Wave 2 (monthly P&L, keg
weigh capture, segment CM over arbitrary periods). It is not part of Wave 2
because the config pain is blocking the partner from trusting *any* number
the engine produces — including the Wave 2 numbers we'd otherwise build
next. Fix the inputs first.

Wave 3 (receipt approvals, cash drawer, anomaly detection) planned to migrate
`CostBook` from config-file to `CostBook.from_book(approval_book)`. Wave 1.5
accelerates that migration: the cost table already exists in SQLite, already
captures pack price + quantity + VAT flag, and Wave 3's receipt flow simply
fills those same fields from a parsed receipt instead of from partner typing.
The migration is additive, not breaking.

### The VAT fix is a one-time margin jump

On cutover, every historical margin number rises slightly — on average ~7%
of COGS — because costs previously stored as gross are now correctly stored
as net. This is the latent bug being fixed (see ADR-0003 decision 4), not a
regression. The partner has been told to expect the jump; the old spreadsheet
that the tool replaces had the same bug and will agree with the new (pre-fix)
numbers, not the new (post-fix) numbers. The post-fix numbers are correct.

### `CONTEXT.md` updates required on cutover

Three entries change when this wave ships:

- **Recipe review** — rewritten to describe the audit-and-revert safety net
  that replaces the code-review gate.
- **Cost unit convention** — rewritten to reflect the explicit `unit` field
  on every SKU (the convention is no longer implicit).
- **VAT model** — new entry, documenting gross-input/net-stored with the
  per-entry `vat_inclusive` flag.

These updates are part of the wave's done-definition, not a follow-on.

### Done-definition

Wave 1.5 is done when:

- Every regular Loyverse item (sold more than once a week, per `CONTEXT.md`)
  is mapped to a fully-priced recipe.
- The partner has done that work entirely in the UI without opening a
  terminal or editing YAML.
- The 9am review's `needs_attention` strip has been empty for three
  consecutive mornings.
- The audit log + revert + diff link have been used at least once each by
  each partner in a real morning-review context.
- `CONTEXT.md`'s three entries are updated and ADR-0003 is referenced from
  the rewritten **Recipe review** entry.

Code-complete is necessary but not sufficient — there is a one-to-two-week
dogfooding period after the editor is usable during which the partner
completes the initial mapping load, same shape as Wave 1's dogfooding.
