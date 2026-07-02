# Domain Context — Tangerine Phuket Accounting Tool

A glossary of the terms the project uses, sharpened as decisions resolve.
This file is *only* a glossary — implementation decisions live in
`docs/adr/`, the operational runbook lives in `DEPLOY.md`.

## Venue

**Tangerine Phuket** — the dual-concept venue this tool serves. Cafe
8am–5pm, bar 5pm–10pm. Two equal partners (Daniel, Noi) alternate
day/night shifts.

## Partners

The two co-owners. Each is an **Assignee** for the engine's task
assignment (slice 12) and the `cashier_id`/`assignee_id` audit trail.
There is no on-site manager; the tool does the
segregation-of-duties work a manager would do.

## Wave 1 sync surface

The set of Loyverse data the Wave 1 nightly sync pulls: **SALE/REFUND
receipts** and **menu state** (item → category, current prices). Loyverse
**voids**, shift closes, and per-cashier sales counts are *not* part of
the Wave 1 sync — they arrive in Wave 3. A Wave 1 daily review therefore
has an empty anomaly section by design; this is correct, not a bug.

## Regular item

A **regular item** is a Loyverse menu item sold more than once a week.
Wave 1's mapping-coverage bar is "every regular item is mapped to a
recipe"; one-off and seasonal items are allowed to surface in the daily
review's `unmapped` section (that is their correct home — revenue the
tool cannot cost is surfaced, not silently dropped). The bar is reviewed
at every menu change.

## Cost unit convention

Every ingredient SKU's per-unit cost is **THB per smallest weight/volume
unit**: per **ml** for liquids (beer, milk, syrups), per **g** for solids
(beans, sugar, flour), per **unit** for countables (eggs, napkins). The
recipe `quantity` for that ingredient uses the same unit. The convention is
still implicit *in practice* — Wave 1.5 Step 1 (ADR-0003 decision 3) added
an explicit `unit` column to the `skus` table — populated by the migration
where it could confidently derive one from a `costs.yaml` comment, and
always set explicitly on SKUs created through the UI (the create form and
the recipe editor's inline sub-form both require it). Step 3's cost editor
displays it (the pack quantity is entered in the SKU's unit, and the
derived price reads "THB/g" etc.), and Step 4's recipe editor *uses* it:
quantity shorthand (`1 tbsp` → 15, `1 tsp` → 5, `1 pinch` → 2, `1 knob` →
10, `1 pepper grind` → 0.2) converts into the ingredient's canonical unit
before saving — `1 tbsp` of milk stores 15 (ml), `1 tbsp` of flour stores
15 (g) — and shorthand against a SKU whose unit is unconfirmed is rejected
rather than guessed. Migrated SKUs whose unit is still NULL remain the
residual convention-only cases: reasoning about their `quantity` values
still means knowing per-g/per-ml/per-unit by convention.

`config/costs.yaml` and `config/recipes.yaml` are no longer read at
runtime — see **Recipe review** below — they seed the `costs` / `recipes` /
`recipe_ingredients` / `mappings` tables once, the first time the app boots
against an empty database.

## Recipe review

The control that "recipes go through code review" (PRD user story 22) was,
through Wave 1: any change to `config/recipes.yaml`, `config/costs.yaml`,
or `config/assignees.yaml` is a PR against `main`; `main` is
branch-protected; the **other** partner must approve before merge.
Self-merge is not permitted on config changes.

**Wave 1.5 Step 1 has landed** (ADR-0003 decision 1): `recipes.yaml` and
`costs.yaml` are now seed-only. They are read once, into SQLite, the first
time the app boots against an empty database — after that, editing them
has no effect on the running tool until a fresh database is seeded. The
code-review gate above therefore no longer catches "wrong quantity in a
hurry" for the running system, only for the seed data a new deployment
would start from. `config/assignees.yaml` is unaffected — it stays
file-based and is still read at every startup (ADR-0003 consequence).

**Wave 1.5 Step 2 has landed**: two read-only visibility surfaces, `/skus`
(one row per SKU, classified active / prep-internal / dangling, with a
green/yellow/red health indicator) and `/items` (one row per Loyverse item,
unmapped-or-broken items sorted to the top, each mapped row showing its
SKU's chain health and derived margin). The daily review's `needs_attention`
list deep-links each flagged item straight to its `/items?item=<id>` row.
Both are still purely visibility — no editing.

**Wave 1.5 Step 3 has landed**: the first in-browser *edits*. A cost
editor per SKU (`/skus/<sku_id>`) captures pack price + pack quantity +
a `vat_inclusive` checkbox and derives the net per-unit price live; a
bulk path (`/upload`) serves a CSV template pre-filled with every
Loyverse item and every known SKU, previews what an uploaded file would
change, and applies on confirm (per-row errors block the whole apply).

**Wave 1.5 Step 4 has landed**: the recipe editor. The same
`/skus/<sku_id>` page edits the SKU's recipe as `(ingredient, quantity)`
rows with add/remove/reorder, a live per-row and total cost preview, and
a per-recipe target gross margin input. The ingredient picker offers only
existing SKUs (no orphan `sku_id` references possible) plus an inline
"Create new SKU…" sub-form (sku_id, name, unit, price) that creates the
ingredient and auto-selects it. New SKUs are also creatable from the SKU
view's "New SKU" button and from an unmapped item's "create new SKU…"
option in item coverage (which maps the item in the same stroke).
Recipes, costs, and mappings are therefore all editable without YAML or
git.

The audit log with per-change and per-session revert is **not yet built**
(Wave 1.5 Step 5). Until it ships, edits land with at most
`updated_at` / `updated_by` provenance on the row itself — there is no
paper trail of old values and no revert. This entry will be rewritten
again once the audit-and-revert safety net lands.

## VAT model

Every cost is entered as what the purchase actually showed: a pack price
that may or may not include VAT. The migration (and, once it ships, the
Step 3 cost editor) records a per-entry `vat_inclusive` flag alongside the
price and stores the SKU's cost **net** of VAT — dividing by 1.07 only when
`vat_inclusive` is set. VAT-ness is a property of the *purchase*, not the
supplier or the SKU: the same SKU bought from a VAT-registered supplier
(Makro, ARO) on one occasion and a wet-market stall on another carries a
different flag each time.

The Wave 1.5 Step 1 migration set `vat_inclusive=true` only for
`costs.yaml` entries whose trailing comment clearly names Makro or ARO;
every other entry defaults to `false` so the migration never makes a
number *worse* by guessing wrong. This is why every margin the tool
produces against a VAT-registered ingredient rose slightly (~7% of COGS) on
cutover — the old numbers were silently gross, not net, and this is the
fix, not a regression (ADR-0003 decision 4).

## Recovery posture

The operational story when the server or its data dies. For Wave 1:

- **Data** — nightly SQLite snapshot (`deploy/tangerine-snapshot.sh`),
  rotated to the newest 14, restorable by file copy.
- **Box** — both: a weekly DigitalOcean droplet snapshot for fast
  restore, *and* the `DEPLOY.md` runbook as the tested-from-scratch
  rebuild path. The runbook is the source of truth; the droplet snapshot
  is the speed optimisation.
- **Secrets** — the auth passphrase, cookie-signing secret, and Loyverse
  access token live in a shared password manager both partners can reach
  (alongside the runbook). The only on-server copy is
  `/etc/tangerine/env`; without the off-box copy, a rebuilt droplet
  cannot be logged into.
