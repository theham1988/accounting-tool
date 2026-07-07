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

## SKU roles

Every SKU has a **role**, a fact about its relations rather than a stored
label:

- A **purchasable SKU** is bought, not made — it has no recipe. Its cost
  comes from purchases (the cost book). *Avoid*: "raw ingredient",
  "leaf SKU".
- A **produced SKU** is made in-house — it has a recipe. Sold dishes and
  preps are both produced.
- A **prep** is a produced SKU declared usable as a recipe ingredient
  (house sauces, dressings, concentrates, guacamole, homemade spam).
  Prep-ness is the one role fact that must be *declared*, not derived —
  nothing about a recipe's shape says whether its output may go into
  other recipes. A prep may also be sold directly; the two are not
  exclusive.

A produced SKU's cost is **always derived** from its recipe (recursively,
down to purchasable SKUs at receipt prices); it is never priced directly
in the cost book. Purchasable SKUs are priced *only* by the cost book.
One source of truth per role — if the venue starts buying a prep
pre-made instead of making it, deleting its recipe flips it to
purchasable.

## Serving recipe

A one-line recipe expressing how much of a purchasable SKU one sold unit
consumes: a bottled Chang sale = 330 ml of the `beer-chang` SKU, a
draught pint = 473 ml of the keg SKU. Serving recipes exist so
directly-sold purchasables (beer, wine, soft drinks) cost through the
same item → SKU → recipe path as dishes — there is no second costing
path. "Beers don't have recipes" is therefore false in this model; they
have *uninteresting* ones.

An **ingredient** is not a kind of SKU; it is a role a SKU plays inside
one recipe: an (SKU, quantity) line. Purchasable SKUs and preps may play
it; sold-only dishes may not.

## Yield

The quantity of its output SKU that one execution of a recipe produces,
expressed in **that SKU's own unit**: a pitcher recipe yields 2 (units —
two pours), an ahi sauce batch yields ~61 (g). One formula everywhere:
cost per unit of output = batch cost ÷ yield. The old integer
`yield_units` is the special case of a `unit`-denominated output; it is
not a separate concept.
_Avoid_: "yield units" and "batch yield" as distinct ideas.

For a prep whose batch has never been measured, the yield defaults to
the **sum of the recipe's input quantities** — a no-loss estimate,
explicitly rough for reduced/cooked preps (evaporation means true yield
is lower and true cost per gram higher). Measured yields replace
defaults as batches get weighed.

## Cost unit convention

Every ingredient SKU's per-unit cost is **THB per smallest weight/volume
unit**: per **ml** for liquids (beer, milk, syrups), per **g** for solids
(beans, sugar, flour), per **unit** for countables (eggs, napkins). The
recipe `quantity` for that ingredient uses the same unit. The convention is
still implicit *in practice* — Wave 1.5 Slice 1 (ADR-0003 decision 3) added
an explicit `unit` column to the `skus` table — populated by the migration
where it could confidently derive one from a `costs.yaml` comment, and
always set explicitly on SKUs created through the UI (the create form and
the recipe editor's inline sub-form both require it). Slice 3's cost editor
displays it (the pack quantity is entered in the SKU's unit, and the
derived price reads "THB/g" etc.), and Slice 4's recipe editor *uses* it:
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

**Wave 1.5 Slice 1 has landed** (ADR-0003 decision 1): `recipes.yaml` and
`costs.yaml` are now seed-only. They are read once, into SQLite, the first
time the app boots against an empty database — after that, editing them
has no effect on the running tool until a fresh database is seeded. The
code-review gate above therefore no longer catches "wrong quantity in a
hurry" for the running system, only for the seed data a new deployment
would start from. `config/assignees.yaml` is unaffected — it stays
file-based and is still read at every startup (ADR-0003 consequence).

**Wave 1.5 Slice 2 has landed**: two read-only visibility surfaces, `/skus`
(one row per SKU, classified active / prep-internal / dangling, with a
green/yellow/red health indicator) and `/items` (one row per Loyverse item,
unmapped-or-broken items sorted to the top, each mapped row showing its
SKU's chain health and derived margin). The daily review's `needs_attention`
list deep-links each flagged item straight to its `/items?item=<id>` row.
Both are still purely visibility — no editing.

**Wave 1.5 Slice 3 has landed**: the first in-browser *edits*. A cost
editor per SKU (`/skus/<sku_id>`) captures pack price + pack quantity +
a `vat_inclusive` checkbox and derives the net per-unit price live; a
bulk path (`/upload`) serves a CSV template pre-filled with every
Loyverse item and every known SKU, previews what an uploaded file would
change, and applies on confirm (per-row errors block the whole apply).

**Wave 1.5 Slice 4 has landed**: the recipe editor. The same
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

**Wave 1.5 Slice 5 has landed**: the audit-and-revert safety net that
replaces the code-review gate (ADR-0003 decision 2). Every config edit —
recipe, cost, mapping, SKU creation, whether typed or bulk-uploaded —
writes an `audit_log` row: who, when, a whole-row before/after snapshot,
and a `session_id` grouping everything saved in one browser login. The
`/audit` page renders the trail with a per-entry **Revert** (surgical:
only the fields that entry changed go back, so later edits to other
fields of the same row survive; a creation's revert deletes the row) and
a **Revert this session** panic undo; reverts are themselves logged with
an optional typed reason (ADR-0003: the log records intent), so even the
undo has a paper trail. The 9am review shows an "N changes since last
review" link per partner; the audit page highlights those entries and
its **Mark as reviewed** button (an explicit POST — merely loading the
page never moves the mark) is what counts as reviewing. The link is
upgraded to a banner when any unreviewed change is under 24 hours old.
The trade-off this accepts (wrong numbers ship instantly and are caught
the next morning by the diff, not before shipping by a reviewer) is
recorded in ADR-0003.

## VAT model

Every cost is entered as what the purchase actually showed: a pack price
that may or may not include VAT. The migration (and, once it ships, the
Slice 3 cost editor) records a per-entry `vat_inclusive` flag alongside the
price and stores the SKU's cost **net** of VAT — dividing by 1.07 only when
`vat_inclusive` is set. VAT-ness is a property of the *purchase*, not the
supplier or the SKU: the same SKU bought from a VAT-registered supplier
(Makro, ARO) on one occasion and a wet-market stall on another carries a
different flag each time.

The Wave 1.5 Slice 1 migration set `vat_inclusive=true` only for
`costs.yaml` entries whose trailing comment clearly names Makro or ARO;
every other entry defaults to `false` so the migration never makes a
number *worse* by guessing wrong. This is why every margin the tool
produces against a VAT-registered ingredient rose slightly (~7% of COGS) on
cutover — the old numbers were silently gross, not net, and this is the
fix, not a regression (ADR-0003 decision 4).

## COGS recognition

The cost the tool attributes to a sale. Two models live in the codebase:

- **Recipe-cost COGS** — the cost of a sale is its recipe's ingredient
  costs at the net price in effect on the sale's day, summed over the
  units sold. This is the model the daily 9am review uses and, from Wave
  2, the model every reporting surface (period, month, trend) uses. It is
  *theoretical*: it costs what *should* have been consumed, at the
  in-effect price, with no inventory measurement. It needs only sales
  plus the recipe and cost books.
- **Accrual COGS** — `beginning inventory + purchases − ending inventory`,
  measured from keg weighs (bar) and cafe stock counts (cafe), priced at
  the latest approved price. This is the model `monthly_pnl.py` was built
  around (issue 08). It is *actual*: it costs what *was* consumed,
  capturing waste and yield loss, and matches costs to the period of
  consumption.

Accrual COGS requires per-purchase transactions (the input the
receipt/OCR flow, issue 03, was to feed) and physical inventory counts.
Wave 2 drops OCR and the inventory capture flows, so accrual COGS is
**dormant** — `monthly_pnl.py`'s accrual path, `keg_inventory.py`, and
`cafe_stock.py` stay built and E2E-tested but drive no surface. The
recipe-cost model is the tool's live COGS. The trade-off (losing the
waste/yield-loss signal and period-matched costing) is recorded in
ADR-0004.

Unmapped items are handled as the daily view handles them — their revenue
is excluded from headline totals (recipe-cost COGS is unknown for them)
and surfaced in a needs-attention section. The accrual monthly view's
reason for *including* unmapped revenue (consumption-derived COGS catches
their cost regardless of the sale) no longer applies.

## As-of-date pricing

Recipe-cost COGS costs each sale at the **net price in effect on the
sale's date**, not the price in the cost book at the moment the report is
rendered. The price-as-of-a-date is reconstructed from the `audit_log`
(each cost edit snapshots the row's old/new `price_per_unit_net` and
`changed_at`); pre-cutover sales use the seed price. A day's margin is
therefore stable whether the day is viewed on its own morning or inside a
monthly view three weeks later — one truth, not two, the principle
ADR-0003 applied to VAT. The daily review, the period view, and the
monthly view share one as-of-date lookup, so they agree by construction.
This also corrects a latent Wave 1 behaviour where the daily review costed
at *current* price and so re-costed history after any price edit.

## Fixed costs

Entity-level costs (rent, utilities, shared staff, insurance) that are
**never allocated to a segment** — segments carry contribution margin
only; fixed costs sit above the segment line and reduce entity net
profit. From Wave 2 a fixed cost is **recurring** (defined once, auto-
applies each month) or **one-off** (entered for a specific period). A
calendar-month P&L shows full net profit. A sub-month arbitrary period
(e.g. the last 7 days) shows fixed costs **day-apportioned** —
`(days in range / days in month) × monthly amount` — on a clearly-labelled
"estimated fixed costs (apportioned)" line, with the resulting net profit
labelled as an estimate. Apportionment is a documented estimate
(utilities are not truly linear); the un-apportioned monthly number
remains the honest one.

**Wave 2 slice 3 has landed** (entry in Admin at `/admin/fixed-costs`),
pinning two semantics. **Ending** a recurring cost is dated the day the
partner ends it, and the end month still charges in full — a month
already owed is not un-charged; later months charge nothing. Ending is
distinct from **deleting**, which removes the row from every month (for
typos/duplicates; the audit log's revert restores it). In a partially-
covered month, one-off costs are day-apportioned by the same ratio as
recurring ones — the cost belongs to the month, not to a day in it, and
the estimate label covers both. Fixed-cost edits write to the same
`audit_log` (`table='fixed_costs'`) and revert the same way as every
other config edit.

## Reporting periods and modes

The reporting surface is one page rendered in four **modes** — **Day,
Period, Month, Trends** — sharing one report shape and switched by a
single top control (an HTMX swap). The daily review stays the home at
`/`, defaulting to yesterday (Wave 1 user story 19 preserved).
**Drill-down is zooming the same report**: a period row → the days in the
period (switches to Day mode for that date) → an item's performance over
the period. Each zoom step is a deep-linkable URL with a breadcrumb
(Review › Jul › 14 Jul › Cappuccino). A mapped item's row carries a
separate "edit recipe" link to the Admin surface (the recipe/cost/mapping
authoring from Wave 1.5). The Admin surface (`/skus`, `/items`,
`/upload`, `/audit`) is the second top-level destination. Trends render
as server-rendered SVG sparklines and clickable CSS bars — no client
JavaScript, so ADR-0002's stack is unchanged.

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
