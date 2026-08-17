"""Add serving recipes for mapped-but-unrecipe'd sold items.

    python scripts/add_serving_recipes.py --dry-run   # plan only, no writes
    python scripts/add_serving_recipes.py             # back up, then apply

The July 17 bulk import (``/upload``) created mappings and costs for the
Taps resold-drink items but could not create recipes — the bulk CSV path
authors mappings + costs only. In this model a directly-sold purchasable
costs through a **serving recipe** (CONTEXT.md "Serving recipe": a
one-line recipe, one sale = one bottle/can/pint), so those items resolve
``unmapped``-equivalent in the margin engine (no recipe -> flagged,
excluded from COGS) and their revenue sits in the flagged bucket —
173,775 THB of the 524,280 THB sold since 2026-06-01 at the time of
writing.

This script closes that gap through the same audited store the UI uses —
never raw SQL — running the **last three strokes** of the canonical
sold-as-is five-write setup per item (the purchasable SKU and its cost
already exist):

  1. the produced sold SKU (``<sku_id>:served``, unit ``unit``,
     segment inherited from the Loyverse menu item),
  2. the serving recipe (one ingredient line — the purchasable at
     quantity 1 — yield 1, measured, not a prep),
  3. the mapping repointed from the purchasable to the sold SKU.

Everything lands in one :meth:`SqliteConfigStore.batch` under one
``session_id``, so it is one "Revert this session" click on ``/audit`` if
wrong. Because mappings/recipes read at current state and first-ever
prices reach back, **every past day's view heals on its next render** —
no backfill job.

Consumables only (decision 2026-08-17): merch and fees (corkage, sticker
set, mug) stay unmapped and flagged; their COGS is not a recipe question.
The 6 SKUs still missing receipt prices keep their recipes UNPRICED
(flagged on ``/skus``) until the cost editor prices them — then they heal
too, automatically.

Safety: refuses to run if any ``<sku>:served`` id already exists; prints
the full plan and the re-resolved cost per item after applying.
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
from pathlib import Path
from typing import Any

# Allow ``python scripts/add_serving_recipes.py`` from a source checkout
# without installing the package — mirrors scripts/import_derived_recipes.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tangerine.types import Segment  # noqa: E402

DEFAULT_DB_PATH = "./tangerine.db"

ACTOR = "serving-recipes-import"

#: Cutoff for "does this item still matter" — the venue only trusts views
#: from this month on (decision 2026-08-17). Items whose mapped SKU has no
#: recipe and sold nothing since the cutoff are skipped: no revenue to
#: heal, no recipe worth the audit noise.
SALES_CUTOFF = "2026-06-01"

#: Purchasable SKUs to skip even though sold — merch and fees, not
#: consumables (decision 2026-08-17). Their COGS is not a recipe question;
#: they stay mapped to the bare purchasable and flag in the daily review.
NON_CONSUMABLE_SKUS: frozenset[str] = frozenset(
    {
        "corkage-charge",
        "sticker-set",
        "tangerine-mug",
        "extra",
    }
)

_SERVED_SUFFIX = ":served"


@dataclass(frozen=True)
class ServingPlan:
    """One item's planned serving-recipe setup."""

    item_id: str
    sku_id: str          # the existing purchasable
    item_name: str
    segment: Segment | None


class ImportError_(Exception):
    """A validation failure — nothing has been written."""


def plan_items(conn: sqlite3.Connection) -> list[ServingPlan]:
    """Mapped items with no recipe that sold since the cutoff.

    Joins mappings x (no recipe) x (sales since cutoff) x current menu
    (for the item's segment), skipping the non-consumable SKUs.
    """
    rows = conn.execute(
        "SELECT DISTINCT m.item_id, m.sku_id, mi.name, mi.segment "
        "FROM mappings m "
        "LEFT JOIN recipes r ON r.sku_id = m.sku_id "
        "JOIN sales s ON s.item_id = m.item_id AND s.timestamp >= ? "
        "LEFT JOIN menu_items mi ON mi.item_id = m.item_id AND mi.snapshot_id = "
        "  (SELECT id FROM menu_snapshots ORDER BY id DESC LIMIT 1) "
        "WHERE r.sku_id IS NULL "
        "AND m.sku_id NOT IN (%s) "
        "ORDER BY m.item_id" % ",".join("?" for _ in NON_CONSUMABLE_SKUS),
        (SALES_CUTOFF, *sorted(NON_CONSUMABLE_SKUS)),
    ).fetchall()
    return [
        ServingPlan(
            item_id=item_id,
            sku_id=sku_id,
            item_name=name or item_id,
            segment=Segment(segment) if segment else None,
        )
        for item_id, sku_id, name, segment in rows
    ]


def validate(store: Any, plan: list[ServingPlan]) -> None:
    """Fail loudly on clashes before anything is written."""
    existing = {sku.sku_id for sku in store.skus()}
    clashes = sorted(
        {f"{p.sku_id}{_SERVED_SUFFIX}" for p in plan} & existing
    )
    if clashes:
        raise ImportError_(f":served sku_id already exists in DB: {clashes}")
    missing = sorted({p.sku_id for p in plan} - existing)
    if missing:
        raise ImportError_(f"mapped purchasable SKU missing from skus: {missing}")


def apply_import(store: Any, plan: list[ServingPlan], *, session_id: str) -> None:
    """The three audited writes per item, one atomic batch."""
    with store.batch():
        for p in plan:
            sold_sku_id = f"{p.sku_id}{_SERVED_SUFFIX}"
            store.create_sku(
                sold_sku_id,
                name=f"{p.item_name} (serving)",
                unit="unit",
                created_by=ACTOR,
                session_id=session_id,
                segment=p.segment,
            )
            store.save_recipe(
                sold_sku_id,
                ingredients=[(p.sku_id, Decimal("1"))],
                yield_qty=Decimal("1"),
                yield_estimated=False,
                prep=False,
                updated_by=ACTOR,
                session_id=session_id,
            )
            store.save_mapping(
                p.item_id, sold_sku_id, updated_by=ACTOR, session_id=session_id
            )


def verify_costs(store: Any, plan: list[ServingPlan]) -> list[str]:
    """Re-resolve each served SKU through the live margin engine."""
    from tangerine.margin import CostResolver
    from tangerine.recipes import RecipeCatalog

    catalog = RecipeCatalog(list(store.recipes()), list(store.mappings()))
    resolver = CostResolver(catalog, store.cost_book())
    lines: list[str] = []
    unpriced = 0
    for p in plan:
        unit_cost = resolver.unit_cost(f"{p.sku_id}{_SERVED_SUFFIX}")
        if unit_cost is None:
            unpriced += 1
            lines.append(
                f"  {p.item_id:8s} -> {p.sku_id}{_SERVED_SUFFIX:32s} "
                "UNPRICED (enter receipt cost in the editor)"
            )
        else:
            lines.append(
                f"  {p.item_id:8s} -> {p.sku_id}{_SERVED_SUFFIX:32s} "
                f"{unit_cost:>9.4f} THB/unit"
            )
    if unpriced:
        lines.append(f"  ({unpriced} items unpriced — price them in the cost editor)")
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
        plan = plan_items(conn)
        print(
            f"planned serving-recipe setups: {len(plan)} items "
            f"(sold since {SALES_CUTOFF}, consumables only)"
        )
        for p in plan:
            seg = p.segment.value if p.segment else "-"
            print(f"  {p.item_id:8s} -> {p.sku_id}{_SERVED_SUFFIX} [{seg}] {p.item_name}")

        validate(store, plan)
        if args.dry_run:
            print("dry run — nothing written.")
            return 0

        backup = f"{args.db}.pre-serving-recipes-{date.today():%Y%m%d}.bak"
        shutil.copyfile(args.db, backup)
        print(f"backup written: {backup}")

        session_id = f"serving-recipes-import-{date.today():%Y%m%d}"
        apply_import(store, plan, session_id=session_id)
        print(f"applied (session {session_id})")
        print("re-resolved costs through the live engine:")
        for line in verify_costs(store, plan):
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
