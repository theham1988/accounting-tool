"""Focused unit tests on the pure ``tangerine.cost_mirror`` engine.

Issue #101 (parent spec #100): the round-trip CSV cost-mirror, slice 1
(tracer-bullet — no paper trail). The E2E seam
(``tests/test_loyverse_cost_export_e2e.py``) pins the wiring; these tests
pin the fiddly CSV edge cases directly against the pure functions, per the
spec's testing decisions: 2 dp half-up rounding, UTF-8 BOM presence, money
format (digits + point only), blank-vs-zero for uncostable rows, header
preservation, and the drift payload shape.

The engine is pure: in-memory ``RecipeCatalog`` + ``CostBook`` (built here
directly, the same shapes the store supplies in production) feed
``prepare`` / ``emit_filled_csv``; no I/O, no store imports.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tangerine.cost import CostBook
from tangerine.cost_mirror import (
    DriftRow,
    DriftStatus,
    InvalidLoyverseExportError,
    emit_filled_csv,
    prepare,
)
from tangerine.recipes import RecipeCatalog
from tangerine.types import Recipe, RecipeIngredient, Segment, SkuMapping


# --- shared fixtures ---------------------------------------------------------


def _recipe(sku_id: str, *, ingredients: tuple[RecipeIngredient, ...]) -> Recipe:
    return Recipe(
        sku_id=sku_id,
        name=sku_id,
        segment=Segment.CAFE,
        ingredients=ingredients,
        yield_qty=D("1"),
    )


def _cost_book(prices: dict[str, str]) -> CostBook:
    """A cost book built directly from ``{sku: net_price}`` (date irrelevant
    for the mirror — it costs at current state, ADR-0004 aside)."""
    from datetime import date

    return CostBook({k: (D(v), date(2026, 7, 1)) for k, v in prices.items()})


def _catalog(recipes: list[Recipe], mappings: list[SkuMapping]) -> RecipeCatalog:
    return RecipeCatalog(recipes, mappings)


# A minimal Loyverse items export — Handle, SKU, Name, Price, Cost — the
# canonical shape the partner downloads from Back Office. Books preserves
# every column; only ``Cost`` is touched. Extra columns (Track stock,
# Category, ...) ride through verbatim in the round-trip.
_BASE_EXPORT = (
    "Handle,SKU,Name,Price,Cost\n"
    "latte,latte-12oz,Caffe Latte 12oz,60.00,\n"
    "croissant,croissant,Butter Croissant,75.00,\n"
    "mystery,sku-no-recipe,Unknown Item,40.00,\n"
)


# =============================================================================
# AC: prepare joins on SKU, classifies each row, rounds 2 dp half-up
# =============================================================================


def test_prepare_classifies_each_row_into_filled_no_books_cost_or_differs() -> None:
    """A four-row export exercises the four diff states the partner sees.

    - ``latte-12oz`` maps to a fully-priced recipe -> ``FILLED`` with Books'
      cost (recipe = 0.20 THB of beans per unit).
    - ``croissant`` maps to a recipe with an unpriced ingredient ->
      ``NO_BOOKS_COST_UNKNOWN_PRICE``; the row is preserved, ``Cost`` blanked.
    - ``sku-no-recipe`` has no recipe in Books -> ``NO_BOOKS_COST_UNMAPPED``;
      the row is preserved, ``Cost`` blanked.
    - a row whose uploaded ``Cost`` already matches Books' number -> ``FILLED``
      with ``drift=False`` (the diff the partner sees is the visibility layer;
      the unconditional overwrite happens on confirm, slice 2).
    """
    recipes = [
        _recipe(
            "latte-12oz",
            ingredients=(RecipeIngredient("beans", D("10")),),
        ),
        _recipe(
            "croissant",
            ingredients=(RecipeIngredient("butter", D("50")),),
        ),
        # mocha-12oz: 12.5 g × 0.02 = 0.25 THB, matches the uploaded Cost.
        _recipe(
            "mocha-12oz",
            ingredients=(RecipeIngredient("beans", D("12.5")),),
        ),
    ]
    mappings = [
        SkuMapping(item_id="latte", sku_id="latte-12oz"),
        SkuMapping(item_id="croissant", sku_id="croissant"),
        SkuMapping(item_id="mocha", sku_id="mocha-12oz"),
    ]
    catalog = _catalog(recipes, mappings)
    cost = _cost_book({"beans": "0.020"})  # no butter price -> croissant unknown

    export = _BASE_EXPORT + "mocha,mocha-12oz,Mocha 12oz,70.00,0.25\n"
    result = prepare(csv_text=export, recipes=catalog, cost=cost)

    by_sku = {row.sku: row for row in result.drift_rows}
    assert by_sku["latte-12oz"].status is DriftStatus.FILLED
    assert by_sku["latte-12oz"].books_cost == D("0.20")
    assert by_sku["latte-12oz"].loyverse_cost is None  # blank in the upload
    assert by_sku["latte-12oz"].drift is False  # nothing in Loyverse to differ from

    assert by_sku["croissant"].status is DriftStatus.NO_BOOKS_COST_UNKNOWN_PRICE
    assert by_sku["croissant"].books_cost is None

    assert by_sku["sku-no-recipe"].status is DriftStatus.NO_BOOKS_COST_UNMAPPED
    assert by_sku["sku-no-recipe"].books_cost is None

    assert by_sku["mocha-12oz"].status is DriftStatus.FILLED
    assert by_sku["mocha-12oz"].books_cost == D("0.25")
    assert by_sku["mocha-12oz"].loyverse_cost == D("0.25")
    assert by_sku["mocha-12oz"].drift is False  # Loyverse matches Books


def test_prepare_flags_drift_when_loyverse_cost_differs_from_books() -> None:
    """A costable row whose uploaded ``Cost`` disagrees with Books' number is
    flagged ``DIFFERS`` with both values carried, so the diff page can render
    "differs: Loyverse X → Books Y"."""
    recipes = [
        _recipe("latte-12oz", ingredients=(RecipeIngredient("beans", D("10")),)),
    ]
    catalog = _catalog(
        recipes, [SkuMapping(item_id="latte", sku_id="latte-12oz")]
    )
    cost = _cost_book({"beans": "0.020"})  # 0.20 THB/latte

    export = "Handle,SKU,Name,Price,Cost\nlatte,latte-12oz,Latte,60.00,0.99\n"
    result = prepare(csv_text=export, recipes=catalog, cost=cost)

    (row,) = result.drift_rows
    assert row.status is DriftStatus.DIFFERS
    assert row.loyverse_cost == D("0.99")
    assert row.books_cost == D("0.20")
    assert row.drift is True


def test_prepare_rounds_books_cost_two_dp_half_up() -> None:
    """``0.125`` rounds to ``0.13``, not ``0.12`` (Python's default ``Decimal``
    banker's rounding would give ``0.12``). The cost mirror uses half-up so a
    THB cent is a real cent."""
    recipes = [
        # 12.5 g × 0.010 = 0.125 THB per latte — the half-up edge case.
        _recipe("latte-12oz", ingredients=(RecipeIngredient("beans", D("12.5")),)),
    ]
    catalog = _catalog(
        recipes, [SkuMapping(item_id="latte", sku_id="latte-12oz")]
    )
    cost = _cost_book({"beans": "0.010"})

    export = "Handle,SKU,Name,Price,Cost\nlatte,latte-12oz,Latte,60.00,\n"
    result = prepare(csv_text=export, recipes=catalog, cost=cost)

    (row,) = result.drift_rows
    assert row.books_cost == D("0.13")  # half-up, not banker's 0.12


# =============================================================================
# AC: emit_filled_csv — header byte-identical, Cost cell touched, blank never 0
# =============================================================================


def test_emit_preserves_header_byte_identical_and_every_column() -> None:
    """The emitted file's header row is byte-identical to the upload's (no
    retype, no reorder); every Loyverse column rides through untouched. Loyverse
    fails the import on a renamed header (#72 §3), so Books must snapshot it."""
    export = (
        "Handle,SKU,Name,Variant,Price,Track stock,Cost,Taxes\n"
        "latte,latte-12oz,Caffe Latte,12oz,60.00,off,,none\n"
    )
    recipes = [
        _recipe("latte-12oz", ingredients=(RecipeIngredient("beans", D("10")),)),
    ]
    catalog = _catalog(
        recipes, [SkuMapping(item_id="latte", sku_id="latte-12oz")]
    )
    cost = _cost_book({"beans": "0.020"})

    result = prepare(csv_text=export, recipes=catalog, cost=cost)
    filled = emit_filled_csv(result)

    # The emitted header is the uploaded header verbatim, with the UTF-8 BOM
    # prepended (tested separately). Strip the BOM for the byte-identical
    # comparison — Loyverse fails the import on a renamed header, so Books
    # snapshots the uploaded header rather than retyping it.
    filled_no_bom = filled.lstrip("\ufeff")
    filled_header = filled_no_bom.split("\r\n", 1)[0].split("\n", 1)[0]
    uploaded_header = export.split("\n", 1)[0]
    assert filled_header == uploaded_header  # byte-identical, BOM aside
    # Every uploaded column is still present in the emitted file.
    for column in ("Handle", "SKU", "Name", "Variant", "Price",
                   "Track stock", "Cost", "Taxes"):
        assert column in filled


def test_emit_writes_filled_cost_for_costable_row_blank_for_uncostable() -> None:
    """The closed rule: costable rows get ``CostResolver.cost_per_unit``
    rounded 2 dp; uncostable rows (unknown-price or unmapped) get a **blank**
    ``Cost`` cell — never ``0`` or ``0.00`` (blank doesn't overwrite Loyverse's
    existing cost, #72 §2)."""
    recipes = [
        _recipe("latte-12oz", ingredients=(RecipeIngredient("beans", D("10")),)),
        # croissant maps but its butter is unpriced -> unknown-price -> blank.
        _recipe("croissant", ingredients=(RecipeIngredient("butter", D("50")),)),
    ]
    catalog = _catalog(
        recipes,
        [
            SkuMapping(item_id="latte", sku_id="latte-12oz"),
            SkuMapping(item_id="croissant", sku_id="croissant"),
        ],
    )
    cost = _cost_book({"beans": "0.020"})  # no butter price

    export = (
        "Handle,SKU,Name,Price,Cost\n"
        "latte,latte-12oz,Latte,60.00,\n"
        "croissant,croissant,Croissant,75.00,0.40\n"
        "mystery,sku-no-recipe,Mystery,40.00,0.10\n"
    )
    result = prepare(csv_text=export, recipes=catalog, cost=cost)
    filled = emit_filled_csv(result)

    # Parse the filled CSV back to inspect the Cost cell per row.
    rows = _parse_csv(filled)
    by_sku = {r["SKU"]: r for r in rows}
    assert by_sku["latte-12oz"]["Cost"] == "0.20"  # filled
    assert by_sku["croissant"]["Cost"] == ""  # blank — unknown-price
    assert by_sku["sku-no-recipe"]["Cost"] == ""  # blank — unmapped
    # Non-Cost columns preserved verbatim on the uncostable rows.
    assert by_sku["croissant"]["Name"] == "Croissant"
    assert by_sku["croissant"]["Price"] == "75.00"
    assert by_sku["sku-no-recipe"]["Name"] == "Mystery"


def test_emit_never_writes_zero_for_an_uncostable_row() -> None:
    """Zero is never emitted for an uncostable row — it would zero Loyverse's
    COGS for that item. This pins the negative directly (the positive shape
    is the blank above): scan every emitted ``Cost`` cell, none equals ``0``,
    ``0.0``, or ``0.00``."""
    recipes = [
        _recipe("latte-12oz", ingredients=(RecipeIngredient("beans", D("10")),)),
    ]
    catalog = _catalog(
        recipes, [SkuMapping(item_id="latte", sku_id="latte-12oz")]
    )
    cost = _cost_book({"beans": "0.020"})
    export = (
        "Handle,SKU,Name,Price,Cost\n"
        "latte,latte-12oz,Latte,60.00,\n"
        "mystery,sku-no-recipe,Mystery,40.00,0.10\n"
    )
    result = prepare(csv_text=export, recipes=catalog, cost=cost)
    filled = emit_filled_csv(result)

    for row in _parse_csv(filled):
        # A blank Cost (uncostable row) is fine; a literal "0" / "0.00" is not.
        assert row["Cost"] not in ("0", "0.0", "0.00"), (
            f"row {row['SKU']} emitted a zero Cost — would wipe Loyverse COGS"
        )


def test_emit_emits_utf8_with_bom() -> None:
    """The emitted file is UTF-8 **with BOM** — the safe choice for Thai item
    names opened in Excel (#72 §3). A Thai-name row round-trips through a
    re-parse."""
    export = (
        "Handle,SKU,Name,Price,Cost\n"
        "ขนม,croissant,ครัวซองเนย,75.00,\n"
    )
    recipes = [
        _recipe("croissant", ingredients=(RecipeIngredient("butter", D("50")),)),
    ]
    catalog = _catalog(
        recipes, [SkuMapping(item_id="ขนม", sku_id="croissant")]
    )
    cost = _cost_book({"butter": "0.004"})  # 50 g × 0.004 = 0.20

    result = prepare(csv_text=export, recipes=catalog, cost=cost)
    filled_bytes = emit_filled_csv(result).encode("utf-8")

    assert filled_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    # The Thai name survives the round-trip.
    assert "ครัวซองเนย".encode("utf-8") in filled_bytes


def test_emit_money_is_digits_and_point_only() -> None:
    """``Cost`` cells are digits + point only — no baht symbol, no comma
    separators (Loyverse rejects currency symbols in the import)."""
    recipes = [
        # 1234.5 g × 0.100 = 123.45 THB — exercises hundreds place (no comma).
        _recipe("big-dish", ingredients=(RecipeIngredient("stuff", D("1234.5")),)),
    ]
    catalog = _catalog(recipes, [SkuMapping(item_id="big", sku_id="big-dish")])
    cost = _cost_book({"stuff": "0.100"})

    export = "Handle,SKU,Name,Price,Cost\nbig,big-dish,Big Dish,500.00,\n"
    result = prepare(csv_text=export, recipes=catalog, cost=cost)
    filled = emit_filled_csv(result)

    (row,) = _parse_csv(filled)
    assert row["Cost"] == "123.45"  # no "฿", no "123,45", no "123.450"


# =============================================================================
# AC: round-trip fidelity — re-uploading the produced file is zero-drift
# =============================================================================


def test_round_trip_reuploading_the_produced_file_is_zero_drift() -> None:
    """The closed-loop proof: emit a file, re-parse it as a fresh export, and
    the diff is zero (Books' number now matches what it just wrote). This is
    Loyverse's own recommended export→edit→re-import flow."""
    recipes = [
        _recipe("latte-12oz", ingredients=(RecipeIngredient("beans", D("10")),)),
    ]
    catalog = _catalog(
        recipes, [SkuMapping(item_id="latte", sku_id="latte-12oz")]
    )
    cost = _cost_book({"beans": "0.020"})

    export = "Handle,SKU,Name,Price,Cost\nlatte,latte-12oz,Latte,60.00,\n"
    first = prepare(csv_text=export, recipes=catalog, cost=cost)
    filled = emit_filled_csv(first)

    # Re-parse the filled file as a fresh upload. Books' cost hasn't changed,
    # so every costable row's ``Cost`` now matches — zero drift.
    second = prepare(csv_text=filled, recipes=catalog, cost=cost)
    assert second.changed_count == 0
    (row,) = second.drift_rows
    assert row.status is DriftStatus.FILLED
    assert row.drift is False


# =============================================================================
# AC: wrong-file / missing-column errors are loud, not corrupt files
# =============================================================================


@pytest.mark.parametrize(
    "csv_text,expected_column",
    [
        ("Name,Price\nLatte,60.00\n", "Handle"),
        ("Handle,Name,Price\nLatte,60.00\n", "SKU"),
        ("Handle,SKU,Name,Price\nlatte,latte-12oz,Latte,60.00\n", "Cost"),
    ],
)
def test_prepare_missing_required_column_raises_with_the_column_named(
    csv_text: str, expected_column: str
) -> None:
    """Uploading a CSV missing ``Handle`` / ``SKU`` / ``Cost`` raises
    ``InvalidLoyverseExportError`` naming the missing column — the diff page
    renders that message, not a corrupt file."""
    catalog = _catalog([], [])
    cost = _cost_book({})

    with pytest.raises(InvalidLoyverseExportError) as exc_info:
        prepare(csv_text=csv_text, recipes=catalog, cost=cost)

    assert expected_column in str(exc_info.value)
    assert expected_column in exc_info.value.missing_columns


def test_prepare_strips_a_utf8_bom_from_the_uploaded_header() -> None:
    """Excel saves CSV with a leading UTF-8 BOM; Books must not mistake it for
    a column name (``\\ufeffHandle`` ≠ ``Handle``). The upload route decodes
    ``utf-8-sig`` already, but the engine is defensive: a BOM in the header
    still parses to ``Handle``."""
    bom = "\ufeff"
    export = bom + "Handle,SKU,Name,Price,Cost\nlatte,latte-12oz,Latte,60.00,\n"
    recipes = [
        _recipe("latte-12oz", ingredients=(RecipeIngredient("beans", D("10")),)),
    ]
    catalog = _catalog(
        recipes, [SkuMapping(item_id="latte", sku_id="latte-12oz")]
    )
    cost = _cost_book({"beans": "0.020"})

    result = prepare(csv_text=export, recipes=catalog, cost=cost)

    (row,) = result.drift_rows
    assert row.sku == "latte-12oz"  # parsed, not "BOM+latte-12oz"


# =============================================================================
# AC: drift payload shape — counts and the per-row diff the page renders
# =============================================================================


def test_prepare_counts_match_the_row_classifications() -> None:
    """``PrepareResult.item_count`` / ``filled_count`` / ``changed_count`` are
    the roll-ups the diff page and (slice 2) the audit row will carry."""
    recipes = [
        _recipe("latte-12oz", ingredients=(RecipeIngredient("beans", D("10")),)),
        _recipe("croissant", ingredients=(RecipeIngredient("butter", D("50")),)),
        _recipe("mocha-12oz", ingredients=(RecipeIngredient("beans", D("12.5")),)),
    ]
    catalog = _catalog(
        recipes,
        [
            SkuMapping(item_id="latte", sku_id="latte-12oz"),
            SkuMapping(item_id="croissant", sku_id="croissant"),
            SkuMapping(item_id="mocha", sku_id="mocha-12oz"),
        ],
    )
    cost = _cost_book({"beans": "0.020"})  # latte=0.20, mocha=0.25, no butter

    export = (
        "Handle,SKU,Name,Price,Cost\n"
        "latte,latte-12oz,Latte,60.00,\n"        # FILLED, no Loyverse value
        "croissant,croissant,Croissant,75.00,\n"  # NO_BOOKS_COST_UNKNOWN_PRICE
        "mystery,sku-x,Mystery,40.00,\n"          # NO_BOOKS_COST_UNMAPPED
        "mocha,mocha-12oz,Mocha,70.00,0.99\n"     # DIFFERS (Loyverse 0.99 vs 0.25)
    )
    result = prepare(csv_text=export, recipes=catalog, cost=cost)

    assert result.item_count == 4
    assert result.filled_count == 2  # latte + mocha are costable
    # ``changed_count`` is rows Books will overwrite on confirm — costable
    # rows only, drifted or not (the unconditional-overwrite rule). The
    # uncostable rows keep their blank, so they are not "changes".
    assert result.changed_count == 2  # latte (blank→0.20) + mocha (0.99→0.25)


# =============================================================================
# helpers
# =============================================================================


def _parse_csv(text: str) -> list[dict[str, str]]:
    """Parse ``text`` back into rows, stripping the UTF-8 BOM if present."""
    import csv
    import io

    stripped = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(stripped))
    return list(reader)
