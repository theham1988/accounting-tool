"""Add the two TAPS taco-trio sets the derived-recipes import left out.

    python scripts/add_taps_sets.py --dry-run   # plan only, no writes
    python scripts/add_taps_sets.py             # back up, then apply

The workbook import (``scripts/import_derived_recipes.py``) could not carry
these two sets: their drink ingredients had no SKUs yet and no cost basis.
This script closes that gap through the same audited store — one
:meth:`SqliteConfigStore.batch`, one ``session_id`` — so it is one
"Revert this session" click on ``/audit`` if wrong.

What it writes:

  - **4 purchasable drink SKUs** (RTD margarita can + three 330 ml S&B
    craft beers), created bare — no cost row. The sets therefore resolve
    UNPRICED (flagged on ``/skus``) until receipt prices are entered in
    the cost editor; that is the honest state, not a bug. Names follow the
    Loyverse menu items from the July reconcile doc so mapping via
    ``/items`` is a visual match after the next sync.
  - **2 set recipes** (``taps-taco-trio-rtd``, ``taps-taco-trio-craft-beer``),
    segment ``cafe`` like their sibling sets, built on ``taco-complete-est``
    (the workbook's ~30 THB/taco placeholder) rather than the real taco
    SKUs, which are not flagged as preps — same convention as
    ``sushi-taco-trio-set`` and ``mexi-sushi-taco-set``.

Safety: refuses to run if any SKU involved already exists; prints the full
plan (and, after applying, the re-resolved cost per set) either way.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

# Allow ``python scripts/add_taps_sets.py`` from a source checkout without
# installing the package — mirrors scripts/import_derived_recipes.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tangerine.types import Segment  # noqa: E402

DEFAULT_DB_PATH = "./tangerine.db"

ACTOR = "taps-sets-import"

#: Purchasable resold drinks: (sku_id, Loyverse menu name). Bare — the
#: wholesale cost is a receipt away and guessing it would silently skew
#: the sets' margins.
DRINK_SKUS: tuple[tuple[str, str], ...] = (
    ("rtd-margarita", "RTD Cocktail - Margarita"),
    ("sb-modern-lager-330", "330 ml - S&B Modern Lager"),
    ("sb-saijai-bright-ipa-330", "330 ml - S&B Saijai Bright IPA"),
    ("sb-west-coast-ipa-330", "330 ml - S&B West Coast Anda IPA"),
)


@dataclass(frozen=True)
class SetRecipe:
    """A produced set SKU + its recipe."""

    sku_id: str
    name: str
    ingredients: tuple[tuple[str, Decimal], ...]


SET_RECIPES: tuple[SetRecipe, ...] = (
    SetRecipe(
        sku_id="taps-taco-trio-rtd",
        name="Taps Taco Trio - RTD Cocktail",
        ingredients=(
            ("taco-complete-est", Decimal("3")),
            ("rtd-margarita", Decimal("1")),
        ),
    ),
    SetRecipe(
        sku_id="taps-taco-trio-craft-beer",
        name="Taps Taco Trio - Craft Beer",
        ingredients=(
            ("taco-complete-est", Decimal("3")),
            ("sb-modern-lager-330", Decimal("1")),
            ("sb-saijai-bright-ipa-330", Decimal("1")),
            ("sb-west-coast-ipa-330", Decimal("1")),
        ),
    ),
)

#: Set ingredient that must already exist (imported with the workbook).
PREREQ_SKUS = ("taco-complete-est",)


class ImportError_(Exception):
    """A validation failure — nothing has been written."""


def validate(store: Any) -> None:
    existing = {sku.sku_id for sku in store.skus()}
    wanted = [s for s, _ in DRINK_SKUS] + [r.sku_id for r in SET_RECIPES]
    clashes = sorted(set(wanted) & existing)
    if clashes:
        raise ImportError_(f"sku_id already exists in DB: {clashes}")
    missing = sorted(set(PREREQ_SKUS) - existing)
    if missing:
        raise ImportError_(
            f"prerequisite SKU missing (run import_derived_recipes first): {missing}"
        )


def apply_import(store: Any, *, session_id: str, today: date) -> None:
    with store.batch():
        for sku_id, name in DRINK_SKUS:
            store.create_sku(
                sku_id,
                name=name,
                unit="unit",
                created_by=ACTOR,
                session_id=session_id,
            )
        for recipe in SET_RECIPES:
            store.create_sku(
                recipe.sku_id,
                name=recipe.name,
                unit="unit",
                created_by=ACTOR,
                session_id=session_id,
                segment=Segment.CAFE,
            )
            store.save_recipe(
                recipe.sku_id,
                ingredients=list(recipe.ingredients),
                yield_qty=Decimal("1"),
                yield_estimated=False,
                prep=False,
                updated_by=ACTOR,
                session_id=session_id,
            )


def verify_costs(store: Any) -> list[str]:
    """Re-resolve each set through the live margin engine."""
    from tangerine.margin import CostResolver
    from tangerine.recipes import RecipeCatalog

    catalog = RecipeCatalog(list(store.recipes()), list(store.mappings()))
    resolver = CostResolver(catalog, store.cost_book())
    lines: list[str] = []
    for recipe in SET_RECIPES:
        unit_cost = resolver.unit_cost(recipe.sku_id)
        if unit_cost is None:
            lines.append(
                f"  {recipe.sku_id:28s} UNPRICED (enter drink costs in the editor)"
            )
        else:
            lines.append(f"  {recipe.sku_id:28s} {unit_cost:>9.4f} THB/unit")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("TANGERINE_DB_PATH", DEFAULT_DB_PATH),
        help="SQLite database path (default: $TANGERINE_DB_PATH or ./tangerine.db)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and print the plan only"
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        from tangerine.storage.config_store import SqliteConfigStore

        store = SqliteConfigStore(conn)
        validate(store)

        print(f"plan: {len(DRINK_SKUS)} bare drink SKUs, {len(SET_RECIPES)} set recipes")
        for sku_id, name in DRINK_SKUS:
            print(f"  [drink] {sku_id} — {name} (unit, no cost yet)")
        for recipe in SET_RECIPES:
            preview = "; ".join(f"{s}:{q}" for s, q in recipe.ingredients)
            print(f"  [set]   {recipe.sku_id} (yield 1): {preview}")

        if args.dry_run:
            print("dry run — nothing written.")
            return 0

        backup = f"{args.db}.pre-taps-sets-{date.today():%Y%m%d}.bak"
        shutil.copyfile(args.db, backup)
        print(f"backup written: {backup}")

        session_id = f"taps-sets-import-{date.today():%Y%m%d}"
        apply_import(store, session_id=session_id, today=date.today())
        print(f"applied (session {session_id})")
        print("re-resolved costs through the live engine:")
        for line in verify_costs(store):
            print(line)
        return 0
    except ImportError_ as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.exit(main())
