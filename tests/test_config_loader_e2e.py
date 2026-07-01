"""End-to-end config loader seam (Wave 1, Slice 1).

The loader turns two YAML files — ``recipes.yaml`` (recipes + SKU mappings) and
``costs.yaml`` (current SKU prices) — into the same ``RecipeCatalog`` /
``CostBook`` shapes the engine already accepts. Per the PRD testing rules the
genuine boundary here is the filesystem; tests use real temp files.

Validation fails loudly at startup on malformed YAML, unknown SKU references,
or missing required fields. The tool does not start in a half-working state.

These tests read as worked examples: a YAML file goes in; the equivalent
in-process object comes out.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tangerine.config.loader import ConfigError, load_assignees, load_costs, load_recipes
from tangerine.recipes import RecipeCatalog
from tangerine.types import Assignee, Segment, SkuMapping

D = Decimal


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# --- AC: recipes + mappings round-trip through YAML --------------------------


def test_load_recipes_produces_equivalent_catalog(tmp_path: Path) -> None:
    """A ``recipes.yaml`` with recipes + mappings yields the same catalog an
    inline construction would.

    Worked example. One bar recipe (Chang, 500ml of keg) and one cafe recipe
    (latte, beans + milk) plus a mapping from a Loyverse item id to the Chang
    SKU. The loaded catalog resolves the mapped item to the Chang recipe and
    falls back to item-id-as-SKU for the latte (the seeded-fixture convention).
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
    target_gross_margin_pct: "75"
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }

mappings:
  - { item_id: i-1, sku_id: chang-draft-500 }
""",
    )

    catalog = load_recipes(recipes_yaml)

    assert isinstance(catalog, RecipeCatalog)
    chang = catalog.recipe_for_sku("chang-draft-500")
    assert chang is not None
    assert chang.name == "Chang Draft 500ml"
    assert chang.segment is Segment.BAR
    assert chang.target_gross_margin_pct == D("75")
    assert [ing.sku_id for ing in chang.ingredients] == ["chang-keg"]
    assert chang.ingredients[0].quantity == D("500")
    # The mapping resolves the Loyverse item id to the recipe.
    assert catalog.for_item("i-1") is chang
    # The fallback path: an unmapped item whose id coincides with a SKU.
    assert catalog.for_item("espresso-latte") is catalog.recipe_for_sku(
        "espresso-latte"
    )


# --- AC: costs round-trip through YAML ---------------------------------------


def test_load_costs_produces_equivalent_cost_book(tmp_path: Path) -> None:
    """A ``costs.yaml`` mapping SKU -> {price, updated_at} yields the same cost
    book an inline ``CostBook({...})`` construction would.

    Worked example. Three SKU prices: chang-keg @ 0.07, beans-arabica @ 2,
    milk-fresh @ 0.025, all set 2026-06-01. The loaded book returns the same
    price entries.
    """
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
  beans-arabica: { price: "2", updated_at: "2026-06-01" }
  milk-fresh: { price: "0.025", updated_at: "2026-06-01" }
""",
    )

    book = load_costs(costs_yaml)

    assert book.price("chang-keg") is not None
    assert book.price("chang-keg").price == D("0.07")  # type: ignore[union-attr]
    assert book.price("chang-keg").updated_at == date(2026, 6, 1)  # type: ignore[union-attr]
    assert book.price("beans-arabica").price == D("2")  # type: ignore[union-attr]
    assert book.price("milk-fresh").price == D("0.025")  # type: ignore[union-attr]
    assert book.price("unknown-sku") is None


# --- AC: validation fails loudly at startup ----------------------------------


def test_malformed_yaml_raises_config_error(tmp_path: Path) -> None:
    """A YAML file with a syntax error raises ``ConfigError``, not a raw
    parser exception.

    The partner-facing path (startup) must surface a readable message; the
    raw ``yaml.YAMLError`` would leak an implementation detail.
    """
    bad = tmp_path / "recipes.yaml"
    _write(
        bad,
        """
recipes:
  - sku_id: chang
    name: Chang
   segment: bar  # inconsistent indent -> YAML error
""",
    )

    with pytest.raises(ConfigError, match="malformed YAML"):
        load_recipes(bad)


def test_missing_recipes_file_raises_config_error(tmp_path: Path) -> None:
    """A path to a non-existent file raises ``ConfigError`` (not ``OSError``)."""
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(ConfigError, match="cannot read file"):
        load_recipes(missing)


def test_dangling_sku_mapping_raises_config_error(tmp_path: Path) -> None:
    """A mapping whose ``sku_id`` has no recipe raises ``ConfigError``.

    Per the agreed scope: a mapping is a claim that a Loyverse item resolves to
    a known recipe. A dangling mapping would silently behave like an unmapped
    item (the engine falls back to item-id-as-SKU), hiding the typo. Surface it
    at startup.

    Note: this does NOT reject recipes whose ingredient SKUs have no price —
    that stays a runtime ``unknown_price`` flag (PRD user story 24 talks about
    *references*, not pricing; pricing arrives via Slice 3 approvals).
    """
    bad = tmp_path / "recipes.yaml"
    _write(
        bad,
        """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }

mappings:
  - { item_id: i-1, sku_id: does-not-exist }
""",
    )

    with pytest.raises(ConfigError, match="references sku_id 'does-not-exist'"):
        load_recipes(bad)


def test_recipe_missing_required_field_raises_config_error(tmp_path: Path) -> None:
    """A recipe missing ``sku_id`` (or ``name`` / ``segment`` / ``ingredients``)
    raises ``ConfigError`` with the field name in the message.
    """
    bad = tmp_path / "recipes.yaml"
    _write(
        bad,
        """
recipes:
  - name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
""",
    )

    with pytest.raises(ConfigError, match="recipe #0.sku_id is required"):
        load_recipes(bad)


def test_invalid_segment_raises_config_error(tmp_path: Path) -> None:
    """A segment value outside ``cafe`` / ``bar`` raises ``ConfigError``."""
    bad = tmp_path / "recipes.yaml"
    _write(
        bad,
        """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bakery
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
""",
    )

    with pytest.raises(ConfigError, match="segment must be one of"):
        load_recipes(bad)


def test_catalog_exposes_loaded_mappings(tmp_path: Path) -> None:
    """``RecipeCatalog.mappings()`` round-trips the mappings the loader gave it.

    A caller that wants the loaded mappings without re-parsing the YAML or
    keeping a second copy alongside the catalog can read them straight off
    the catalog. This pins that ``mappings()`` returns the same
    ``SkuMapping``s the file declared, in declaration order.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }

mappings:
  - { item_id: i-1, sku_id: chang-draft-500 }
  - { item_id: i-2, sku_id: espresso-latte }
""",
    )

    catalog = load_recipes(recipes_yaml)

    assert catalog.mappings() == (
        SkuMapping(item_id="i-1", sku_id="chang-draft-500"),
        SkuMapping(item_id="i-2", sku_id="espresso-latte"),
    )


def test_unpriced_ingredient_is_not_a_startup_error(tmp_path: Path) -> None:
    """A recipe ingredient with no entry in the cost book loads fine.

    This is the agreed scope: missing *references* (a dangling mapping) are a
    startup error, but a missing *price* is not — the engine surfaces unpriced
    ingredients at runtime via the ``unknown_price`` flag (see ``margin.py``).
    Rejecting them at startup would make that runtime path unreachable and
    conflict with the engine's design.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
""",
    )
    costs_yaml = tmp_path / "costs.yaml"
    _write(costs_yaml, "costs: {}\n")

    # No prices defined — both load without error.
    catalog = load_recipes(recipes_yaml)
    book = load_costs(costs_yaml)

    assert catalog.recipe_for_sku("chang-draft-500") is not None
    assert book.price("chang-keg") is None


# --- AC: assignees round-trip through YAML (slice 4) -------------------------


def test_load_assignees_produces_partner_list(tmp_path: Path) -> None:
    """An ``assignees.yaml`` with two partners yields two ``Assignee`` objects
    in file order.

    Worked example. The Wave 1 default list (Daniel, Noi) loads into two
    ``Assignee``s keyed by their ids. Order is preserved so the login role
    selector renders them in the same order the file lists them.

    The role selector is populated from this list (slice 4); adding the future
    manager is a YAML entry, not a code change (PRD user story 31).
    """
    assignees_yaml = tmp_path / "assignees.yaml"
    _write(
        assignees_yaml,
        """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
""",
    )

    assignees = load_assignees(assignees_yaml)

    assert assignees == [
        Assignee(assignee_id="daniel", name="Daniel"),
        Assignee(assignee_id="noi", name="Noi"),
    ]


def test_missing_assignees_block_raises_config_error(tmp_path: Path) -> None:
    """A file without a top-level ``assignees`` list fails loudly.

    A login page with no roles is a broken deploy; surface it at startup
    rather than rendering an empty selector.
    """
    bad = tmp_path / "assignees.yaml"
    _write(bad, "partners: []\n")

    with pytest.raises(ConfigError, match="missing top-level 'assignees' list"):
        load_assignees(bad)


def test_empty_assignees_list_raises_config_error(tmp_path: Path) -> None:
    """An empty ``assignees`` list fails loudly — no one could log in."""
    bad = tmp_path / "assignees.yaml"
    _write(bad, "assignees: []\n")

    with pytest.raises(ConfigError, match="at least one entry"):
        load_assignees(bad)


def test_assignee_missing_required_field_raises_config_error(
    tmp_path: Path,
) -> None:
    """An assignee missing ``assignee_id`` (or ``name``) fails loudly with the
    field named in the message."""
    bad = tmp_path / "assignees.yaml"
    _write(
        bad,
        """
assignees:
  - name: Daniel
""",
    )

    with pytest.raises(ConfigError, match="assignee #0.assignee_id is required"):
        load_assignees(bad)


def test_duplicate_assignee_id_raises_config_error(tmp_path: Path) -> None:
    """Two assignees with the same id fail loudly.

    The signed-cookie payload carries one ``assignee_id``; a duplicate id
    would make attribution ambiguous (which Daniel acted?). Reject it at
    startup.
    """
    bad = tmp_path / "assignees.yaml"
    _write(
        bad,
        """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: daniel
    name: Daniel Two
""",
    )

    with pytest.raises(ConfigError, match="duplicate assignee_id 'daniel'"):
        load_assignees(bad)