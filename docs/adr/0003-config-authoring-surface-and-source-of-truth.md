# ADR-0003: Config moves into SQLite; the partner edits it live in the UI

Date: 2026-07-01

## Status

Accepted.

## Context

Wave 1 loads recipes, SKU mappings, and current SKU prices from three YAML
files in the repo (`config/recipes.yaml`, `config/costs.yaml`,
`config/assignees.yaml`). `CONTEXT.md` defines a control called **Recipe
review**: any change to these files is a PR against `main`, `main` is
branch-protected, and the *other* partner must approve before merge. The
control's stated purpose is to catch "wrong quantity in a hurry" before it
ships, because a wrong quantity silently corrupts every margin number.

In practice this workflow has failed for the operator. The partner is not a
coder; YAML mechanics, git, and PRs are not a fluent surface; and the bigger
pain is not the approval latency but **invisibility** — the partner cannot see
which of the 232 Loyverse items are mapped, which SKUs are fully priced, or
which recipes are complete, without scanning a 1,400-line YAML by eye. The
9am review surfaces *yesterday's* unmapped items reactively, but gives no view
of the whole menu's mapping health. Configuration has become the thing the
partner cannot see and cannot easily change.

Wave 1 PRD explicitly listed "Admin UI for editing recipes, SKU mappings, or
costs in-browser" as **out of scope**, deferring it to a future wave. This ADR
records the decision to build that future wave now, ahead of the rest of Wave
2, because the configuration pain is blocking the partner from trusting the
margin numbers the tool already produces.

Four sub-decisions were reached during design. Each is hard to reverse,
surprising without context, and the result of a real trade-off.

## Decision

**1. SQLite becomes the source of truth for recipes, mappings, and costs.**
The three YAML files become seed-only — loaded into SQLite on first run if the
relevant tables are empty, never read from at runtime after, never written to.
Every edit goes through the DB. The YAML files remain in the repo as a
known-good recovery artifact (alongside the nightly SQLite snapshot).

Rejected: writing back to YAML from the running app (file-locking, atomic-write
correctness, drift between the committed file and live state).
Rejected: YAML as source of truth with the UI producing downloadable patches
(relocates the YAML pain, doesn't remove it).

**2. The code-review gate on config changes is removed.** Partners edit live
in the UI; changes take effect on the next page load. No second-partner
approval. This overrides the **Recipe review** control documented in
`CONTEXT.md`.

The gate is replaced by an audit-and-revert safety net: every edit records
who/when/old-value/new-value in an `audit_log` table; every edit can be
reverted per-change or per-session; the daily 9am review shows a "N changes
since last review" link (banner only if changes exist in the last 24h).

The trade-off accepted: silent-corruption failures (wrong quantity, wrong
price) now ship instantly and are caught the *next morning* by the diff, not
*before* shipping by a reviewer. This is judged acceptable for two reasons:
the gate was not actually being used as designed (the partner could not
fluently make the change in the first place), and the diff view is a stronger
detection mechanism than code review for the failure modes that survived
review (wrong pack size that looks plausible to both partners).

**3. SKUs gain an explicit `unit` field (`g` / `ml` / `unit`).** Today the
unit convention is implicit and lives only in `CONTEXT.md`'s glossary — a
recipe's `quantity: "200"` means 200 g for beans, 200 ml for milk, 200 units
for eggs, depending on which SKU the human editing the file remembered it to
be. In a UI editor with no gate, this becomes a silent-corruption machine.
The migration derives the unit for each existing SKU from the pack-size
comments in `costs.yaml`; ambiguous cases are flagged for partner confirmation
in the UI.

The stored unit vocabulary is strict (`g` / `ml` / `unit`). The recipe editor
accepts shorthand (Thai spoon measures: `1 tbsp` → 15, `1 tsp` → 5, `1 pinch`
→ 2, etc.) and converts to the SKU's unit before saving — so partners type
what's natural, the data underneath stays canonical.

**4. Costs are stored net; the partner enters gross.** Today `costs.yaml`
stores THB per smallest unit, documented as gross (VAT-inclusive), with a
comment saying "divide by 1.07 for net" that the engine never executes. Every
margin the shipped tool has produced is therefore understated by ~7% of COGS
on every VAT-inclusive cost. The cost editor captures what the partner
actually sees on a receipt — pack price + pack quantity + a per-entry
`vat_inclusive` flag — and the engine divides by 1.07 only when the flag is
set, storing net. VAT-ness is a property of the purchase, not the supplier or
the SKU, because the same SKU can be bought from a VAT-registered supplier
(Makro, ARO) and a non-registered one (wet market) on different occasions.

The migration sets `vat_inclusive=true` only for costs whose `costs.yaml`
comment clearly names Makro/ARO with a pack size; everywhere else it defaults
to `false` so the migration never makes a number *worse* by guessing wrong.
Ambiguous rows surface in the UI with a `[check]` marker for partner
confirmation. On cutover, every historical margin number rises slightly
(average ~7% of COGS) — this is the latent bug being fixed, not a regression.

## Consequences

- **Config is no longer version-controlled at the row level.** The
  `config/*.yaml` files stop being the live state. The audit log becomes the
  record of *intent* (who changed what, when, why-noted-as-revert-reason);
  the SQLite snapshot is the record of *state*. Git history of the YAML files
  captures only seed-time decisions.
- **Recovery posture shifts.** The nightly SQLite snapshot is now the
  authoritative backup for config, not just for sales. If the DB dies and the
  snapshot is a week old, a week of config edits is lost. Mitigated by the
  audit log (replaying the lost week's edits is mechanical) and by keeping the
  YAML files as the known-good starting point.
- **The `Recipe review` and `Cost unit convention` entries in `CONTEXT.md`
  become stale on cutover** and must be rewritten. A new `VAT model` entry is
  added. See the companion Wave 1.5 PRD for the migration checklist.
- **`assignees.yaml` remains file-based.** Auth identity is low-volume,
  onboarding-via-config is a deliberate feature, and the manager-onboarding
  story (PRD user story 31) depends on it. Config authoring does not extend
  to assignees.
- **Wave 3's receipt-approval flow produces exactly the cost shape defined
  here** (pack size + pack price + VAT flag). Building the cost editor this
  way now means Wave 3 fills the same fields from a parsed receipt instead of
  from partner typing — no schema rework.
- **The four sub-decisions are independently reversible in principle but
  coupled in practice.** Removing the gate without the audit log would be
  unsafe; the audit log without the gate has no purpose. The unit field
  without the editor gives no benefit; the editor without the unit field is a
  silent-corruption machine. They ship together or not at all.

## Considered and rejected

- **Preserve the gate, move it into the UI as two-key approval.** Rejected by
  the partner: "we are too small to have gates." The operating reality is two
  equal partners on alternating shifts; same-hour approval is rarely
  available.
- **Tiered gate (recipes/mappings gated, costs ungated).** Rejected for the
  same reason; the partner does not want to maintain a mental model of which
  edits need approval.
- **Trust + audit log with no revert.** Rejected as the audit log's value
  depends on the partner being able to act on it; revert is the action.
- **YAML stays source of truth; UI writes patches.** Rejected as it
  relocates the YAML pain without removing it.
- **Strict per-unit cost entry (no pack capture).** Rejected as it relocates
  the per-unit arithmetic (`380 ÷ 2000 = 0.19`) to the browser, failing the
  whole purpose of removing the partner from manual calculation.
- **VAT-ness on the supplier or SKU.** Rejected as wrong granularity — the
  same SKU from different suppliers, or the same supplier on different items,
  can differ; only the cost entry knows the truth.
- **Keep historical reviews on the old (gross-derived) numbers, apply net
  only to new data.** Rejected as it leaves two truths in the data and makes
  month-over-month comparisons incoherent. The one-time jump is the bug being
  fixed.

## References

- `CONTEXT.md` → Recipe review (to be rewritten on cutover)
- `CONTEXT.md` → Cost unit convention (to be rewritten on cutover)
- `docs/PRD-WAVE-1.md` → Out of Scope: "Admin UI for editing recipes, SKU
  mappings, or costs in-browser" (this ADR supersedes that deferral)
- `docs/PRD-WAVE-1.md` → Migration path for the interim cost book (this ADR
  accelerates that migration from Wave 3 to Wave 1.5)
- Companion: `docs/PRD-WAVE-1.5.md`
