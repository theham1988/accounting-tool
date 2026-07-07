# ADR-0005: Derived costing — recurse into preps, no leaf-price-wins

Date: 2026-07-08

## Status

Accepted.

Reverses two Wave 1 decisions recorded in `config/recipes.yaml` (the seed
commentary) and in the spreadsheet's prototype resolver.

## Context

Wave 1 (slice 04) introduced recipes but deliberately did not cost produced
SKUs from their own recipes. Two rules followed from that:

1. **No recursion into prep recipes.** A dish's recipe could name a prep as
   an ingredient, but the engine did not walk into the prep's own recipe to
   derive its cost. The prep output was treated as a leaf to be priced
   directly in the cost book. The seed commentary (issue: `config/recipes.yaml`
   lines 39–45) records this verbatim: *"the engine costs sold items from
   their ingredient SKUs (it does not recurse into prep recipes), so each
   prep output below is also a costable ingredient SKU in its own right once
   a partner prices it."*

2. **Direct price wins over recipe.** The offline cost spreadsheet
   (`scripts/build_cost_spreadsheet.py`) carried its own prototype resolver
   to do the recursion Wave 1 had skipped, with one extra branch: *"leaf
   price wins: if a SKU has a direct approved price, use it (a partner can
   price the sauce-as-bought even when a recipe exists)."* The live engine
   did not honour that branch (it had no recursion to apply it in), so the
   spreadsheet and the engine silently disagreed whenever a produced SKU
   carried a direct price.

Issue #36's evidence (CONTEXT.md "Costing review 2026-07"): of the 13
sub-recipes in the seed, **0 are priced directly** in the cost book. Every
prep output in production is unpriced as a leaf and uncosted as a dish
ingredient — dishes that use a sauce or a base carry that line at zero,
understating COGS. The two Wave 1 rules were deferrals, not decisions, and
they have run their course.

## Decision

The engine resolves a produced SKU's cost **recursively from its recipe**,
down to purchasables, and the spreadsheet uses the same resolver.

**1. Recurse into prep recipes.** A SKU's per-unit cost is resolved by
`tangerine.margin.CostResolver`:

- a **purchasable** SKU (no recipe) takes its price from the cost book;
- a **produced** SKU (has a recipe) is costed as
  `Σ(ingredient qty × unit_cost) / yield_qty`, recursing through preps down
  to purchasables.

Resolution is memoised per costing pass (a sauce used by eight dishes is
walked once) and cycle-safe (a SKU on its own resolution stack resolves as
unpriceable — defense in depth behind the save-time cycle rejection from
issue #35). Each level divides by its own yield in its own unit, so the
unified yield model from issue #34 carries through to a 25 g line of a 61 g
sauce batch costing exactly 25/61 of the batch's input cost.

**2. No leaf-price-wins.** A produced SKU's cost is *always* derived from
its recipe, never typed directly. The cost book is consulted only for
purchasables. The seed migration removes any pre-existing direct cost rows
on produced SKUs; the cost editor rejects new ones. A stale direct price
reaching the resolver is silently ignored rather than honoured — the recipe
is the one source of truth. This collapses the engine/spreadsheet
divergence: there is no branch for the spreadsheet to disagree with.

**3. Unknown-price propagates recursively.** A prep whose recipe contains
an unpriced purchasable makes every dish using it flag `unknown_price`;
revenue still surfaces; totals stay clean. The honesty rule that applied to
a dish's direct ingredients in Wave 1 now applies at every depth — a prep
can no longer hide an unpriced leaf inside its own recipe and silently
zero-cost the dish.

**4. As-of-date pricing composes by construction.** The resolver runs
against the as-of-date cost book (ADR-0004 decision 2). Editing a leaf
price reprices a prep-containing dish for later days only; earlier days
keep the old derived cost — the prep's per-gram cost is recomputed against
each day's cost book, never stored.

**5. The offline spreadsheet calls the engine's resolver directly.** The
prototype `_Resolver` in `scripts/build_cost_spreadsheet.py` is removed.
The spreadsheet now imports `tangerine.margin.CostResolver`, so the offline
tool and the running tool share one resolver and can never disagree about
a dish's cost.

## Consequences

- A dish containing a prep is costed honestly once its prep's recipe is
  fully priced — no need to also price the prep output as a leaf.
- Removing the leaf-price-wins branch is a breaking change for any partner
  who relied on pricing a produced SKU as bought. The team judged this
  acceptable given the evidence (0 of 13 preps are priced that way today)
  and the simplicity win (one source of truth per SKU).
- `recipe_input_cost`, `recipe_cost_per_unit`, and `has_unknown_price` keep
  their Wave 1 (non-recursive) shapes as the slice-04 primitives for
  callers that hold one recipe plus a cost book but no catalog. The
  recursive behaviour lives in `CostResolver`, used by
  `compute_item_margins` and `unit_cost`. Future callers wanting recursion
  build a `CostResolver`; callers wanting the slice-04 primitive use the
  bare helpers.
- Cycle detection at save time (issue #35's `find_recipe_cycle`) is the
  primary defense against infinite recursion. The runtime `seen`-stack
  guard is the fallback for cycles that slip past save-time (a bad
  migration, a hand-edited YAML seed, a future import path).
