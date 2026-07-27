"""End-to-end seam for the July reconciliation helper
(scripts/reconcile_july.py).

The script joins Loyverse dashboard CSV exports against Books'
``config/recipes.yaml`` mappings to break the pre-#71-vs-Gross gap into
its named parts (Q1 flagged composition, Q2 discounts, Q3 refunds, Q4 UTC
leak, Q5 remainder). The genuine external boundary is the CSV shape; these
tests pin:

- Q1: every Loyverse item falls into exactly one of (mapped /
  unknown_price / unmapped), and the three buckets' revenue sums to the
  Gross total;
- Q2/Q3: discounts and refunds come straight off the daily summary;
- Q5: the parts reconcile to the baht for a clean fixture;
- the Books side's pre-#71 headline equals the mapped bucket's revenue.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

from scripts.reconcile_july import (
    ItemRow,
    classify_items,
    load_books_mappings,
    load_items,
    load_sales_summary,
    render,
)


_REPO = Path(__file__).resolve().parents[1]
_RECIPES = _REPO / "config" / "recipes.yaml"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


_SALES_CSV = """\
Date,Gross sales,Refunds,Discounts,Net sales,Cost of goods,Gross profit,Margin,Taxes
7/3/26,160.00,0.00,0.00,160.00,40.00,120.00,75.00%,0.00
7/2/26,250.00,5.00,0.00,245.00,0.00,245.00,100.00%,0.00
7/1/26,90.00,0.00,30.00,60.00,80.00,10.00,11.11%,0.00
"""

_ITEMS_CSV = """\
Item name,SKU,Category,Items sold,Gross sales,Items refunded,Refunds,Discounts,Net sales,Cost of goods,Gross profit,Margin,Taxes
Mapped Coffee,10004,Coffee,2.000,160.00,0.000,0.00,0.00,160.00,40.00,120.00,75.00%,0.00
Unmapped Beer,99999,Beer Taps,5.000,250.00,0.000,0.00,0.00,250.00,0.00,250.00,100.00%,0.00
Another Unmapped,99998,Snacks,1.000,90.00,0.000,0.00,30.00,60.00,80.00,10.00,11.11%,0.00
"""

# Minimal recipes.yaml: one recipe (americano-hot) and one mapping (10004 -> americano-hot).
# Mirrors the shape of config/recipes.yaml so load_books_mappings works.
_RECIPES_YAML = """\
recipes:
  - sku_id: americano-hot
    name: Americano
    segment: cafe
    ingredients:
      - { sku_id: beans, quantity: "18" }
mappings:
  - { item_id: "10004", sku_id: americano-hot }
"""


def test_classify_buckets_every_item_and_reconciles(tmp_path: Path) -> None:
    recipes_path = tmp_path / "recipes.yaml"
    _write(recipes_path, _RECIPES_YAML)
    sales_path = tmp_path / "sales.csv"
    _write(sales_path, _SALES_CSV)
    items_path = tmp_path / "items.csv"
    _write(items_path, _ITEMS_CSV)

    sales = load_sales_summary(sales_path)
    items = load_items(items_path)
    mappings, recipe_skus = load_books_mappings(recipes_path)
    mapped, unknown_price, unmapped = classify_items(items, mappings, recipe_skus)

    # Every item lands in exactly one bucket.
    assert len(mapped) + len(unknown_price) + len(unmapped) == len(items)
    # The one mapped SKU (10004 -> americano-hot, which has a recipe) is mapped.
    assert [it.name for it in mapped] == ["Mapped Coffee"]
    # The other two are unmapped; nothing is unknown_price.
    assert len(unmapped) == 2
    assert unknown_price == []

    # The buckets' revenue sums to the items-export total, which equals Gross.
    m_rev = sum((i.gross for i in mapped), D("0"))
    u_rev = sum((i.gross for i in unmapped), D("0"))
    up_rev = sum((i.gross for i in unknown_price), D("0"))
    assert m_rev + u_rev + up_rev == D("500.00")
    assert sales["gross"] == D("500.00")


def test_unknown_price_bucket_catches_mapped_sku_without_recipe(
    tmp_path: Path,
) -> None:
    recipes_path = tmp_path / "recipes.yaml"
    # recipes has no entry for americano-hot; the mapping points at a SKU
    # with no recipe → unknown_price, not mapped.
    _write(
        recipes_path,
        """\
recipes:
  - sku_id: something-else
    name: Something Else
    segment: cafe
    ingredients:
      - { sku_id: beans, quantity: "1" }
mappings:
  - { item_id: "10004", sku_id: americano-hot }
""",
    )
    items_path = tmp_path / "items.csv"
    _write(items_path, _ITEMS_CSV)

    items = load_items(items_path)
    mappings, recipe_skus = load_books_mappings(recipes_path)
    mapped, unknown_price, unmapped = classify_items(items, mappings, recipe_skus)

    assert mapped == []
    assert [it.name for it in unknown_price] == ["Mapped Coffee"]
    assert len(unmapped) == 2


def test_render_includes_each_q_section_and_reconciles(tmp_path: Path) -> None:
    recipes_path = tmp_path / "recipes.yaml"
    _write(recipes_path, _RECIPES_YAML)
    sales_path = tmp_path / "sales.csv"
    _write(sales_path, _SALES_CSV)
    items_path = tmp_path / "items.csv"
    _write(items_path, _ITEMS_CSV)
    cat_path = tmp_path / "cats.csv"
    _write(
        cat_path,
        """\
Category,Items sold,Gross sales,Items refunded,Refunds,Discounts,Net sales,Cost of goods,Gross profit,Margin,Taxes
Coffee,2.000,160.00,0.000,0.00,0.00,160.00,40.00,120.00,75.00%,0.00
Beer Taps,5.000,250.00,0.000,0.00,0.00,250.00,0.00,250.00,100.00%,0.00
""",
    )

    sales = load_sales_summary(sales_path)
    items = load_items(items_path)
    mappings, recipe_skus = load_books_mappings(recipes_path)
    mapped, unknown_price, unmapped = classify_items(items, mappings, recipe_skus)

    out = render(
        window="2026-07-01 .. 2026-07-03",
        sales=sales,
        items=items,
        mapped=mapped,
        unknown_price=unknown_price,
        unmapped=unmapped,
        categories_csv=cat_path,
    )

    # Each named section is present.
    for section in [
        "## Step 1 — the Loyverse side",
        "## Step 2 — the Books side",
        "## Q1 — flagged_revenue, ranked by item",
        "## Q2 — discount gap",
        "## Q3 — refund gap",
        "## Q4 — UTC date-bucketing leak",
        "## Q5 — remainder",
        "## Appendix — category-level Gross",
        "## Conclusion",
    ]:
        assert section in out, f"missing section: {section}"

    # Q2/Q3 surface the actual cited numbers from the daily summary.
    # Discounts: 0 + 0 + 30 = 30. Refunds: 0 + 5 + 0 = 5.
    assert "฿30 discounts" in out
    assert "฿5 refunds" in out
    assert "−฿30" in out  # the Less: discounts line
    assert "−฿5" in out  # the Less: refunds line

    # Q5 remainder is zero and the script says so.
    assert "**= remainder** | **฿0**" in out
    assert "reconcile to the baht" in out

    # The pre-#71 headline equals the mapped revenue (160 THB here).
    assert "Books pre-#71 (reliable rows only) | ฿160" in out


def test_load_books_mappings_against_real_seed() -> None:
    """The repo's seed recipes.yaml loads and exposes the expected shape.

    Pins the contract the script relies on (mappings: list of {item_id,
    sku_id}; recipes: list with sku_id) — if the seed shape ever drifts,
    this trips before a partner runs the script and gets nonsense.
    """
    mappings, recipe_skus = load_books_mappings(_RECIPES)
    assert len(mappings) > 0, "expected mappings in seed recipes.yaml"
    assert len(recipe_skus) > 0
    # Every seed recipe is cafe-segment (CONTEXT.md/pin from charting).
    # We don't read segment here — just that recipe_skus and mappings
    # are non-empty and the file parses cleanly.
    sample_item = next(iter(mappings))
    assert isinstance(sample_item, str)
    assert isinstance(mappings[sample_item], str)
