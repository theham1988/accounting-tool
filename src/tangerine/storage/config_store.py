"""SQLite-backed config store + one-time YAML seeder (Wave 1.5, Step 1).

Implements ADR-0003 decision 1: recipes, costs, and mappings move out of the
shipped YAML files into SQLite. The YAML files become seed-only — read once
on first run if the config tables are empty, never read at runtime after.

The store is the read-side half of the engine's :class:`~tangerine.ingestion.Source`
protocol: ``recipes()`` / ``cost_book()`` / ``mappings()``. ``StoreSource`` delegates
to it once wired up; the engine itself is unchanged.

The seeder reuses the existing YAML loaders (:func:`~tangerine.config.loader.load_recipes`,
:func:`~tangerine.config.loader.load_costs`) so a malformed file still fails
loudly at startup with the same ``ConfigError``. The loaders' parsing behaviour
is unchanged — only their *role* narrows from "called every startup" to "called
once by the migrator".
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

from ..config.loader import load_costs, load_recipes
from ..cost import CostBook
from ..recipes import RecipeCatalog
from ..types import Recipe, RecipeIngredient, Segment, SkuMapping
from .schema import apply_migrations

_MIGRATION_ACTOR = "migration"


class SqliteConfigStore:
    """Read-side view over the config tables.

    The store holds an open connection for its lifetime. ``":memory:"`` for
    tests; a filesystem path for the running tool. Like
    :class:`~tangerine.storage.sqlite_store.SqliteLoyverseStore`, the connection
    is serialised by a per-store lock because SQLite connections are not safe
    for concurrent use from multiple threads even with ``check_same_thread=False``
    — and the web app serves sync routes from a threadpool alongside the nightly
    sync cron.
    """

    def __init__(
        self, conn: sqlite3.Connection, lock: threading.Lock | None = None
    ) -> None:
        """``lock`` lets a caller share one serialisation lock across every
        store wrapping the *same* connection — see
        :class:`~tangerine.storage.sqlite_store.SqliteLoyverseStore` for why
        two independent locks over one connection would be unsafe. Defaults
        to a private lock for standalone use (tests, the migration seam's
        own tests).
        """
        self._conn = conn
        self._lock = lock if lock is not None else threading.Lock()

    def recipes(self) -> list[Recipe]:
        """All stored recipes, in sku_id order, each with its ingredient rows.

        Ingredients come back ordered by ``position`` so the same recipe
        round-trips deterministically (and so a recipe that legitimately uses
        the same SKU twice keeps its stages in order).
        """
        with self._lock:
            header_rows = self._conn.execute(
                "SELECT sku_id, name, segment, yield_units, target_gross_margin_pct"
                " FROM recipes ORDER BY sku_id"
            ).fetchall()
            if not header_rows:
                return []
            ingredient_rows = self._conn.execute(
                "SELECT sku_id, ingredient_sku_id, quantity, position"
                " FROM recipe_ingredients ORDER BY sku_id, position"
            ).fetchall()
        ingredients_by_recipe: dict[str, list[RecipeIngredient]] = {}
        for recipe_sku, ing_sku, quantity, _position in ingredient_rows:
            ingredients_by_recipe.setdefault(recipe_sku, []).append(
                RecipeIngredient(sku_id=ing_sku, quantity=_parse_decimal(quantity))
            )
        return [
            Recipe(
                sku_id=sku_id,
                name=name,
                segment=Segment(segment),
                ingredients=tuple(ingredients_by_recipe.get(sku_id, [])),
                yield_units=yield_units,
                target_gross_margin_pct=(
                    _parse_decimal(target) if target is not None else None
                ),
            )
            for sku_id, name, segment, yield_units, target in header_rows
        ]

    def cost_book(self) -> CostBook:
        """All stored costs as a :class:`CostBook`.

        The table holds net per-unit prices (``price_per_unit_net``); the
        book is built directly from those. Slice 3's editor will start
        capturing pack price + quantity; this read side is already net.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT sku_id, price_per_unit_net, updated_at FROM costs"
            ).fetchall()
        prices: dict[str, tuple[Decimal, date]] = {}
        for sku_id, price_net, updated_at in rows:
            prices[sku_id] = (Decimal(price_net), date.fromisoformat(updated_at))
        return CostBook(prices)

    def mappings(self) -> list[SkuMapping]:
        """All stored Loyverse-item -> SKU mappings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, sku_id FROM mappings ORDER BY item_id"
            ).fetchall()
        return [SkuMapping(item_id=item_id, sku_id=sku_id) for item_id, sku_id in rows]


def seed_config(
    conn: sqlite3.Connection,
    *,
    recipes_path: str | Path,
    costs_path: str | Path | None = None,
) -> None:
    """Seed the config tables from YAML on first run.

    Idempotent: a no-op when the ``skus`` table already has rows, so calling
    this on every startup is safe (and what ``create_app`` does). The seeder
    applies migrations first so a freshly-created database is immediately
    seedable.

    Costs are seeded only when ``costs_path`` is provided; tests that exercise
    recipes alone can omit it. ``create_app`` always provides both.

    The seeding writes commit as one transaction (``with conn:``) so they are
    durable and release the write lock immediately — without this, sqlite3's
    default deferred-transaction behaviour leaves the insert transaction open
    until some *other* call happens to commit, holding a write lock that
    blocks any other connection to the same file (surfaces as
    ``sqlite3.OperationalError: database is locked``).
    """
    apply_migrations(conn)
    if _skus_table_has_rows(conn):
        return
    catalog = load_recipes(recipes_path)
    now = _utc_now_iso()
    with conn:
        _seed_recipes(conn, catalog, now)
        _seed_mappings(conn, catalog, now)
        if costs_path is not None:
            _seed_costs(conn, costs_path, now)


def _seed_recipes(conn: sqlite3.Connection, catalog: RecipeCatalog, now: str) -> None:
    """Write every recipe (header + ingredient rows) and its producing SKU.

    The SKU row carries name + segment; the unit column is left NULL here
    (ADR-0003 decision 3 — best-effort derivation happens in ``_seed_costs``
    where the pack-size comment lives; ambiguous cases stay NULL for the
    Step 3 editor to confirm). yield_units and target margin live on both
    the SKU (for the editor's eventual form) and the recipe (for the engine);
    the SKU's copy is left NULL where the recipe carries the default of 1.
    """
    for recipe in catalog.all():
        target_str = _decimal_or_none_to_str(recipe.target_gross_margin_pct)
        conn.execute(
            "INSERT OR IGNORE INTO skus"
            " (sku_id, name, segment, unit, yield_units,"
            "  target_gross_margin_pct, created_at, created_by)"
            " VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                recipe.sku_id,
                recipe.name,
                recipe.segment.value,
                recipe.yield_units if recipe.yield_units != 1 else None,
                target_str,
                now,
                _MIGRATION_ACTOR,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO recipes"
            " (sku_id, name, segment, yield_units, target_gross_margin_pct)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                recipe.sku_id,
                recipe.name,
                recipe.segment.value,
                recipe.yield_units,
                target_str,
            ),
        )
        for position, ing in enumerate(recipe.ingredients):
            conn.execute(
                "INSERT INTO recipe_ingredients"
                " (sku_id, ingredient_sku_id, quantity, position)"
                " VALUES (?, ?, ?, ?)",
                (recipe.sku_id, ing.sku_id, str(ing.quantity), position),
            )


def _seed_mappings(conn: sqlite3.Connection, catalog: RecipeCatalog, now: str) -> None:
    """Write every Loyverse-item -> SKU mapping the loaded catalog carries.

    The loader already validated that every mapping's ``sku_id`` references a
    real recipe (``_validate_mappings_target_real_recipes``), so this is a
    straight write — no FK violation is possible for a file that passed
    ``load_recipes``.
    """
    conn.executemany(
        "INSERT OR REPLACE INTO mappings (item_id, sku_id, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?)",
        [(m.item_id, m.sku_id, now, _MIGRATION_ACTOR) for m in catalog.mappings()],
    )


def _seed_costs(
    conn: sqlite3.Connection, costs_path: str | Path, now: str
) -> None:
    """Write every SKU's net per-unit cost.

    Reads the raw YAML text *and* the parsed structure. The parsed dict gives
    the per-SKU ``price`` / ``updated_at`` (and drives ``load_costs``'s
    validation, which runs first so a malformed file still raises
    ``ConfigError``). The raw text is walked line-by-line to extract the
    trailing per-line comments — ``yaml.safe_load`` drops comments, but the
    file holds the supplier / pack-size provenance there that slice 3 needs
    for VAT detection (ADR-0003 decision 4).
    """
    # Run the validator first; on a bad file it raises ConfigError before we
    # touch the DB. We don't use the returned CostBook for iteration (it has
    # no list-all accessor) but constructing it validates the file end-to-end.
    load_costs(costs_path)

    text = Path(costs_path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return  # load_costs would have raised; defensive only.
    raw_costs = data.get("costs", {})
    if not isinstance(raw_costs, dict):
        return

    comments = _cost_comments_by_sku(text)
    rows: list[tuple[str, int, str, str, str]] = []
    for sku_id, entry in raw_costs.items():
        if not (isinstance(entry, dict) and "price" in entry and "updated_at" in entry):
            continue
        vat_inclusive = _looks_vat_inclusive(comments.get(sku_id))
        net = _net_per_unit(_parse_decimal(entry["price"]), vat_inclusive)
        rows.append(
            (sku_id, 1 if vat_inclusive else 0, str(net), entry["updated_at"], _MIGRATION_ACTOR)
        )
    conn.executemany(
        "INSERT OR REPLACE INTO costs"
        " (sku_id, pack_price, pack_quantity, vat_inclusive,"
        "  price_per_unit_net, updated_at, updated_by)"
        " VALUES (?, NULL, NULL, ?, ?, ?, ?)",
        rows,
    )


def _net_per_unit(gross: Decimal, vat_inclusive: bool) -> Decimal:
    """Gross-input / net-stored: divide by 1.07 when the purchase was VAT-inclusive.

    Per ADR-0003 decision 4: today's ``costs.yaml`` stores gross (VAT-inclusive)
    prices with a comment saying "divide by 1.07 for net" that the engine never
    executes — so every margin the shipped tool has produced is understated by
    ~7% of COGS on VAT-inclusive inputs. The migrator performs that division
    once, on seed, so the engine sees net from then on.

    Quantised to 6 decimal places (ROUND_HALF_UP) — matching the existing
    per-unit price precision in ``costs.yaml`` — so the "except Makro rows"
    delta is deterministic and the comparison in the verification test is exact.
    """
    if not vat_inclusive:
        return gross
    return (gross / Decimal("1.07")).quantize(Decimal("0.000001"))


def _cost_comments_by_sku(text: str) -> dict[str, str]:
    """Map each ``costs:`` entry's sku_id to the trailing comment on its line.

    ``costs.yaml`` records supplier / pack-size provenance in ``# ...``
    comments after each entry (e.g. ``almond-ground: {...}  # ARO Almond 500 g``).
    Those comments are the only place the VAT-ness and the unit live, so the
    seeder parses them directly. Only the ``costs:`` block is walked; lines
    outside it are ignored.
    """
    comments: dict[str, str] = {}
    in_costs_block = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if not in_costs_block:
            if stripped.rstrip() == "costs:":
                in_costs_block = True
            continue
        # A line at the original indent (no leading whitespace) and not blank
        # ends the costs: block.
        if line and not line[0].isspace() and not stripped.startswith("#"):
            in_costs_block = False
            continue
        if stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        sku_part, _, rest = stripped.partition(":")
        sku_id = sku_part.strip()
        if "#" in rest:
            comments[sku_id] = rest.split("#", 1)[1].strip()
    return comments


def _looks_vat_inclusive(comment: str | None) -> bool:
    """True when a cost line's comment clearly names a VAT-registered supplier.

    Per ADR-0003 decision 4: VAT-ness is a property of the purchase, not the
    SKU. The migrator sets ``vat_inclusive`` only for costs whose comment
    clearly names Makro or ARO (case-insensitive). Everything else defaults
    to ``False`` so the migration never makes a number *worse* by guessing
    wrong — wet-market and ambiguous rows surface in the Step 3 editor for
    partner confirmation.
    """
    if not comment:
        return False
    return any(marker in comment.lower() for marker in ("makro", "aro"))


def _skus_table_has_rows(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM skus").fetchone()
    return bool(row and row[0] > 0)


def _parse_decimal(value: str) -> Decimal:
    return Decimal(value)


def _decimal_or_none_to_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["SqliteConfigStore", "seed_config"]
