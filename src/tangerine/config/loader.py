"""YAML -> engine-object loaders with fail-loud validation.

Schema (``recipes.yaml``):

.. code-block:: yaml

    recipes:
      - sku_id: chang-draft-500
        name: Chang Draft 500ml
        segment: bar              # "cafe" or "bar"
        yield_units: 1            # optional, defaults to 1
        target_gross_margin_pct: "75"   # optional
        ingredients:
          - { sku_id: chang-keg, quantity: "500" }
    mappings:
      - { item_id: i-1, sku_id: chang-draft-500 }   # optional block

Schema (``costs.yaml``):

.. code-block:: yaml

    costs:
      chang-keg: { price: "0.07", updated_at: "2026-06-01" }
      beans-arabica: { price: "2", updated_at: "2026-06-01" }

Quantities, prices, and target margins are parsed as :class:`~decimal.Decimal`
(the engine's money type), so they must be quoted strings in the YAML to avoid
float-coercion drift.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from ..cost import CostBook
from ..recipes import RecipeCatalog
from ..types import Assignee, Recipe, RecipeIngredient, Segment, SkuMapping


class ConfigError(Exception):
    """Raised when a config file is malformed or fails validation.

    Carries a single human-readable sentence; the CLI surfaces it verbatim at
    startup so a bad deploy is caught before partners open the tool.
    """


# --- recipes + mappings ------------------------------------------------------


def load_recipes(path: str | Path) -> RecipeCatalog:
    """Load ``recipes.yaml`` into a :class:`RecipeCatalog`.

    Raises :class:`ConfigError` if the file is unreadable, the YAML is
    malformed, a recipe or ingredient is missing a required field, a segment
    is not ``cafe`` or ``bar``, or a mapping references a SKU with no recipe.
    """
    data = _load_yaml(path)
    recipes = _parse_recipes(data, path)
    mappings = _parse_mappings(data, path)
    _validate_mappings_target_real_recipes(mappings, recipes, path)
    return RecipeCatalog(recipes, mappings)


def _parse_recipes(
    data: dict[str, Any], path: str | Path
) -> list[Recipe]:
    raw_recipes = data.get("recipes")
    if raw_recipes is None:
        raise ConfigError(f"{path}: missing top-level 'recipes' list")
    if not isinstance(raw_recipes, list):
        raise ConfigError(f"{path}: 'recipes' must be a list")

    recipes: list[Recipe] = []
    for i, raw in enumerate(raw_recipes):
        recipes.append(_parse_recipe(raw, path, i))
    return recipes


def _parse_recipe(
    raw: Any, path: str | Path, index: int
) -> Recipe:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: recipe #{index} must be a mapping")
    sku_id = _required_str(raw, "sku_id", path, f"recipe #{index}")
    name = _required_str(raw, "name", path, f"recipe #{index}")
    segment = _parse_segment(
        _required_str(raw, "segment", path, f"recipe #{index}"), path, f"recipe #{index}"
    )
    ingredients = _parse_ingredients(raw.get("ingredients"), path, f"recipe #{index}")
    yield_units = _parse_int(raw.get("yield_units", 1), path, f"recipe #{index}.yield_units")
    target = _parse_optional_decimal(
        raw.get("target_gross_margin_pct"), path, f"recipe #{index}.target_gross_margin_pct"
    )
    return Recipe(
        sku_id=sku_id,
        name=name,
        segment=segment,
        ingredients=ingredients,
        yield_units=yield_units,
        target_gross_margin_pct=target,
    )


def _parse_ingredients(
    raw: Any, path: str | Path, ctx: str
) -> tuple[RecipeIngredient, ...]:
    if raw is None:
        raise ConfigError(f"{path}: {ctx}.ingredients is required")
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: {ctx}.ingredients must be a list")
    ingredients: list[RecipeIngredient] = []
    for j, ing in enumerate(raw):
        if not isinstance(ing, dict):
            raise ConfigError(f"{path}: {ctx}.ingredients[{j}] must be a mapping")
        sku_id = _required_str(ing, "sku_id", path, f"{ctx}.ingredients[{j}]")
        quantity = _parse_decimal(
            ing.get("quantity"), path, f"{ctx}.ingredients[{j}].quantity"
        )
        ingredients.append(RecipeIngredient(sku_id=sku_id, quantity=quantity))
    return tuple(ingredients)


def _parse_mappings(
    data: dict[str, Any], path: str | Path
) -> list[SkuMapping]:
    raw_mappings = data.get("mappings", [])
    if raw_mappings is None:
        return []
    if not isinstance(raw_mappings, list):
        raise ConfigError(f"{path}: 'mappings' must be a list")
    mappings: list[SkuMapping] = []
    for i, raw in enumerate(raw_mappings):
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: mapping #{i} must be a mapping")
        mappings.append(
            SkuMapping(
                item_id=_required_str(raw, "item_id", path, f"mapping #{i}"),
                sku_id=_required_str(raw, "sku_id", path, f"mapping #{i}"),
            )
        )
    return mappings


def _validate_mappings_target_real_recipes(
    mappings: list[SkuMapping], recipes: list[Recipe], path: str | Path
) -> None:
    """A mapping's ``sku_id`` must reference a defined recipe (your decision).

    A dangling mapping would silently point a sold item at nothing; the
    margin engine would treat the item as unmapped, which is the opposite of
    what a mapping is for. Surface it at startup instead.
    """
    known_skus = {r.sku_id for r in recipes}
    for m in mappings:
        if m.sku_id not in known_skus:
            raise ConfigError(
                f"{path}: mapping for item {m.item_id!r} references sku_id "
                f"{m.sku_id!r}, which has no recipe defined"
            )


# --- assignees (slice 4) -----------------------------------------------------


def load_assignees(path: str | Path) -> list[Assignee]:
    """Load ``assignees.yaml`` into a list of :class:`Assignee`.

    The auth role selector (slice 4) is populated from this list. Per PRD user
    story 31, onboarding the future manager is a config entry, not a code
    change — this loader is the seam.

    Raises :class:`ConfigError` if the file is unreadable, the YAML is
    malformed, the top-level ``assignees`` block is missing, or an entry is
    missing ``assignee_id`` / ``name``. Duplicate ``assignee_id`` values are
    rejected so the signed-cookie payload can carry one id unambiguously.

    Availability windows are NOT parsed here. Slice 12 owns those for admin
    checklists; the auth gate does not need them.
    """
    data = _load_yaml(path)
    raw = data.get("assignees")
    if raw is None:
        raise ConfigError(f"{path}: missing top-level 'assignees' list")
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: 'assignees' must be a list")
    if not raw:
        raise ConfigError(f"{path}: 'assignees' must contain at least one entry")

    assignees: list[Assignee] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: assignee #{i} must be a mapping")
        assignee_id = _required_str(
            entry, "assignee_id", path, f"assignee #{i}"
        )
        name = _required_str(entry, "name", path, f"assignee #{i}")
        if assignee_id in seen:
            raise ConfigError(
                f"{path}: duplicate assignee_id {assignee_id!r}"
            )
        seen.add(assignee_id)
        assignees.append(Assignee(assignee_id=assignee_id, name=name))
    return assignees


# --- costs -------------------------------------------------------------------


def load_costs(path: str | Path) -> CostBook:
    """Load ``costs.yaml`` into a :class:`CostBook`.

    Raises :class:`ConfigError` on unreadable file, malformed YAML, or a cost
    entry missing ``price`` / ``updated_at``.
    """
    data = _load_yaml(path)
    raw_costs = data.get("costs")
    if raw_costs is None:
        raise ConfigError(f"{path}: missing top-level 'costs' mapping")
    if not isinstance(raw_costs, dict):
        raise ConfigError(f"{path}: 'costs' must be a mapping of sku_id -> entry")

    prices: dict[str, tuple[Decimal, date]] = {}
    for sku_id, entry in raw_costs.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{path}: costs.{sku_id} must be a mapping with 'price' and 'updated_at'"
            )
        price = _parse_decimal(
            entry.get("price"), path, f"costs.{sku_id}.price"
        )
        updated_at = _parse_date(
            entry.get("updated_at"), path, f"costs.{sku_id}.updated_at"
        )
        prices[sku_id] = (price, updated_at)
    return CostBook(prices)


# --- shared parse helpers ----------------------------------------------------


def _load_yaml(path: str | Path) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file ({exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: malformed YAML ({exc})") from exc
    if data is None:
        raise ConfigError(f"{path}: empty file")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")
    return data


def _required_str(
    mapping: dict[str, Any], key: str, path: str | Path, ctx: str
) -> str:
    if key not in mapping:
        raise ConfigError(f"{path}: {ctx}.{key} is required")
    value = mapping[key]
    if not isinstance(value, str):
        raise ConfigError(f"{path}: {ctx}.{key} must be a string")
    return value


def _parse_segment(value: str, path: str | Path, ctx: str) -> Segment:
    try:
        return Segment(value)
    except ValueError:
        valid = ", ".join(repr(s.value) for s in Segment)
        raise ConfigError(
            f"{path}: {ctx}.segment must be one of [{valid}], got {value!r}"
        ) from None


def _parse_decimal(value: Any, path: str | Path, ctx: str) -> Decimal:
    if value is None:
        raise ConfigError(f"{path}: {ctx} is required")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{path}: {ctx} must be a decimal number ({exc})") from exc


def _parse_optional_decimal(
    value: Any, path: str | Path, ctx: str
) -> Decimal | None:
    if value is None:
        return None
    return _parse_decimal(value, path, ctx)


def _parse_int(value: Any, path: str | Path, ctx: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}: {ctx} must be an integer")
    return value


def _parse_date(value: Any, path: str | Path, ctx: str) -> date:
    if value is None:
        raise ConfigError(f"{path}: {ctx} is required")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(
                f"{path}: {ctx} must be ISO-8601 (YYYY-MM-DD) ({exc})"
            ) from exc
    raise ConfigError(f"{path}: {ctx} must be a date string (YYYY-MM-DD)")


__all__ = [
    "ConfigError",
    "load_assignees",
    "load_costs",
    "load_recipes",
]
