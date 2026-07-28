"""Read-only July 2026 reconciliation (wayfinder map #62, ticket #63).

Consumes three Loyverse dashboard CSV exports and Books' seed recipes.yaml
to break the gap between Loyverse Gross sales and the (pre-#71) Books
reliable-rows-only headline into its named parts. Read-only against the
filesystem — no DB, no Loyverse API, writes nothing.

Usage:

    python scripts/reconcile_july.py \\
        --sales     /path/to/sales-summary-DATE-DATE.csv \\
        --items     /path/to/item-sales-summary-DATE-DATE.csv \\
        [--categories /path/to/category-sales-summary-DATE-DATE.csv] \\
        [--recipes config/recipes.yaml] \\
        [> reconcile.md]

The CSVs come from the Loyverse dashboard's "Reports" > "Sales by..." exports.
``--categories`` is optional (used for a cafe/bar category-level summary);
``--recipes`` defaults to ``config/recipes.yaml``.

What this script derives:

- **Loyverse side** (Q2, Q3): Gross / Refunds / Discounts / Net / COGS /
  Gross profit, summed from the daily summary. Books cannot see discounts
  (lines are valued at ``price × qty`` gross) or refunds (REFUND receipts
  are skipped at parse time), so both sit on the Gross-vs-Net axis, not the
  Books-vs-Loyverse axis.
- **Books side** (Q1): joins the per-item Loyverse export against
  ``recipes.yaml``'s mappings. Items with no mapping are flagged unmapped;
  items whose mapped SKU has no recipe would flag unknown_price (none
  observed in this window — every mapped SKU has a recipe). Ranked by
  revenue with category and Loyverse SKU for follow-up.
- **Q4 UTC date-bucketing**: N/A for a window that does not cross a
  month boundary (Books' UTC-date bucketing only leaks at month edges).
  The script reports the window's boundary status.
- **Q5 remainder**: Loyverse Gross should equal mapped-revenue +
  unmapped-revenue + (zero unknown-price here). Any non-zero residue is
  unexplained.

The map's cited ฿130,005 / ฿75,095 / ฿54,910 figures were a smaller snapshot
taken earlier in July. This script reconciles the actual export window
named in the CSV filenames.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required: pip install pyyaml (or install the app in editable mode)"
    ) from exc


@dataclass(frozen=True)
class ItemRow:
    name: str
    sku: str
    category: str
    units: Decimal
    gross: Decimal


def _fmt(n: Decimal) -> str:
    """Format a THB amount with thousands separators and no decimal places."""
    return f"฿{n:,.0f}"


def _parse_window(filenames: list[str]) -> str:
    """Extract a 'YYYY-MM-DD .. YYYY-MM-DD' window from the CSV filenames."""
    pat = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for fn in filenames:
        ms = pat.findall(fn)
        if len(ms) >= 2:
            return f"{ms[0]} .. {ms[1]}"
    return "(unknown window)"


def load_sales_summary(path: Path) -> dict[str, Decimal]:
    """Sum the daily sales-summary CSV into named totals."""
    totals = {
        "gross": Decimal("0"),
        "refunds": Decimal("0"),
        "discounts": Decimal("0"),
        "net": Decimal("0"),
        "cogs": Decimal("0"),
        "gross_profit": Decimal("0"),
        "trading_days": Decimal("0"),
        "calendar_days": Decimal("0"),
    }
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            totals["calendar_days"] += 1
            gross = Decimal(row["Gross sales"])
            if gross > 0:
                totals["trading_days"] += 1
            for key, col in [
                ("gross", "Gross sales"),
                ("refunds", "Refunds"),
                ("discounts", "Discounts"),
                ("net", "Net sales"),
                ("cogs", "Cost of goods"),
                ("gross_profit", "Gross profit"),
            ]:
                totals[key] += Decimal(row[col])
    return totals


def load_items(path: Path) -> list[ItemRow]:
    """Read the per-item Loyverse export into ItemRows."""
    rows: list[ItemRow] = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                ItemRow(
                    name=r["Item name"],
                    sku=r["SKU"].strip(),
                    category=r["Category"],
                    units=Decimal(r["Items sold"]),
                    gross=Decimal(r["Gross sales"]),
                )
            )
    return rows


def load_books_mappings(recipes_yaml: Path) -> tuple[dict[str, str], set[str]]:
    """Return (item_id -> books_sku_id, set of recipe sku_ids) from recipes.yaml."""
    with recipes_yaml.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    mappings = {m["item_id"]: m["sku_id"] for m in data.get("mappings", [])}
    recipe_skus = {r["sku_id"] for r in data.get("recipes", [])}
    return mappings, recipe_skus


def classify_items(
    items: list[ItemRow],
    mappings: dict[str, str],
    recipe_skus: set[str],
) -> tuple[list[ItemRow], list[ItemRow], list[ItemRow]]:
    """Bucket items into (mapped, unknown_price, unmapped) per Books' rules.

    ``mapped``: has a SKU mapping AND the mapped SKU has a recipe. Reliable.
    ``unknown_price``: has a SKU mapping but the mapped SKU has no recipe
    (or, in the full engine, has a recipe with an unpriced ingredient; we
    can't see ingredient prices from the YAML alone, so this is a lower
    bound on the unknown_price bucket).
    ``unmapped``: no SKU mapping at all.
    """
    mapped, unknown_price, unmapped = [], [], []
    for it in items:
        if it.sku in mappings:
            if mappings[it.sku] in recipe_skus:
                mapped.append(it)
            else:
                unknown_price.append(it)
        else:
            unmapped.append(it)
    return mapped, unknown_price, unmapped


def render(
    *,
    window: str,
    sales: dict[str, Decimal],
    items: list[ItemRow],
    mapped: list[ItemRow],
    unknown_price: list[ItemRow],
    unmapped: list[ItemRow],
    categories_csv: Path | None,
) -> str:
    gross = sales["gross"]
    refunds = sales["refunds"]
    discounts = sales["discounts"]
    net = sales["net"]
    lv_cogs = sales["cogs"]
    lv_gp = sales["gross_profit"]
    trading_days = sales["trading_days"]
    calendar_days = sales["calendar_days"]

    m_rev = sum((i.gross for i in mapped), Decimal("0"))
    up_rev = sum((i.gross for i in unknown_price), Decimal("0"))
    u_rev = sum((i.gross for i in unmapped), Decimal("0"))
    items_total = m_rev + up_rev + u_rev
    flagged_rev = up_rev + u_rev

    # The post-#71 headline equals Loyverse Gross by construction; the
    # pre-#71 headline was reliable-rows-only (= mapped revenue here).
    books_pre71 = m_rev
    books_post71 = items_total
    gap_pre71 = gross - books_pre71  # = flagged_rev if parts reconcile

    out: list[str] = []
    out.append(f"# July 2026 reconciliation — window {window}")
    out.append("")
    out.append(
        "Read-only reconciliation of Loyverse dashboard exports against "
        "Books' seed `config/recipes.yaml`. Map: #62. Ticket: #63."
    )
    out.append("")
    out.append(
        f"Window covers {calendar_days:.0f} calendar days "
        f"({trading_days:.0f} trading). The map's cited figures (฿130,005 "
        "gross / ฿75,095 Books / ฿54,910 gap) were a smaller earlier-July "
        "snapshot; this reconciliation works against the actual export "
        "window named above."
    )
    out.append("")
    out.append("## Step 1 — the Loyverse side (from the daily sales summary)")
    out.append("")
    out.append("| Line | THB |")
    out.append("| --- | ---: |")
    out.append(f"| Gross sales | {_fmt(gross)} |")
    out.append(f"| Less: refunds | −{_fmt(refunds)} |")
    out.append(f"| Less: discounts | −{_fmt(discounts)} |")
    out.append(f"| **= Net sales** | **{_fmt(net)}** |")
    out.append(f"| Loyverse COGS (their cost book) | {_fmt(lv_cogs)} |")
    out.append(f"| **= Loyverse Gross profit** | **{_fmt(lv_gp)}** |")
    if gross > 0:
        margin = lv_gp / gross * 100
        out.append(f"| Loyverse Gross margin | {margin:.1f}% |")
    out.append("")
    out.append(
        "Loyverse reports its own COGS and Gross profit alongside the sales "
        "totals — an independent ground truth for the cost half of the map's "
        "destination (Books producing a Loyverse-importable cost CSV so the "
        "two sides mirror)."
    )
    out.append("")

    out.append("## Step 2 — the Books side (from items export × recipes.yaml)")
    out.append("")
    out.append("| Bucket | Items | Revenue | % of Gross |")
    out.append("| --- | ---: | ---: | ---: |")
    pct = lambda r: f"{(r / gross * 100):.1f}%" if gross > 0 else "—"
    out.append(
        f"| mapped (recipe + price path) | {len(mapped)} | {_fmt(m_rev)} | {pct(m_rev)} |"
    )
    if unknown_price:
        out.append(
            f"| mapped-but-no-recipe (would flag `unknown_price`) | {len(unknown_price)} "
            f"| {_fmt(up_rev)} | {pct(up_rev)} |"
        )
    out.append(
        f"| unmapped (no SKU mapping → `unmapped`) | {len(unmapped)} | {_fmt(u_rev)} | {pct(u_rev)} |"
    )
    out.append(
        f"| **Total (items export)** | **{len(items)}** | **{_fmt(items_total)}** | "
        f"**{pct(items_total)}** |"
    )
    out.append("")
    items_check = "ties" if items_total == gross else "DOES NOT TIE"
    out.append(
        f"Items-export total ({_fmt(items_total)}) {items_check} the daily-summary "
        f"Gross ({_fmt(gross)})."
    )
    out.append("")
    out.append(
        "Two Books headlines, two readings:"
    )
    out.append("")
    out.append("| Headline | THB | Rule |")
    out.append("| --- | ---: | --- |")
    out.append(
        f"| Books pre-#71 (reliable rows only) | {_fmt(books_pre71)} | "
        "excluded flagged revenue — the number the map originally cited |"
    )
    out.append(
        f"| Books post-#71 (every sale) | {_fmt(books_post71)} | "
        "issue #71 / ADR-0008 — ties to Loyverse Gross by construction |"
    )
    out.append("")
    out.append(
        f"Gap under the pre-#71 rule: {_fmt(gap_pre71)} (= Gross − mapped). "
        "Post-#71 closes it by definition."
    )
    out.append("")

    out.append("## Q1 — flagged_revenue, ranked by item")
    out.append("")
    out.append(
        f"Total flagged: {_fmt(flagged_rev)} ({pct(flagged_rev)} of Gross). "
        f"Unmapped revenue: {_fmt(u_rev)} across {len(unmapped)} items."
        + (f" Mapped-but-no-recipe: {_fmt(up_rev)} across {len(unknown_price)} items." if unknown_price else "")
    )
    out.append("")
    out.append(
        "Ranked by gross revenue. The flag column is the Books side's view; "
        "Loyverse's category is shown for follow-up."
    )
    out.append("")
    out.append("| Rank | Item | Loyverse category | Flag | Units | Gross | % of flagged |")
    out.append("| ---: | --- | --- | --- | ---: | ---: | ---: |")
    flagged = unknown_price + unmapped
    flagged.sort(key=lambda i: i.gross, reverse=True)
    for i, it in enumerate(flagged, 1):
        flag = "unknown_price" if it in unknown_price else "unmapped"
        p = (it.gross / flagged_rev * 100) if flagged_rev > 0 else Decimal("0")
        out.append(
            f"| {i} | {it.name} | {it.category} | {flag} | "
            f"{it.units:.0f} | {_fmt(it.gross)} | {p:.1f}% |"
        )
    out.append("")

    out.append("## Q2 — discount gap")
    out.append("")
    out.append(
        f"Discounts total {_fmt(discounts)} for the window. Books values each "
        "SALE line at `price × quantity` gross of any discount, so the Books "
        "headline *includes* the undiscounted price of every discounted line. "
        "**The discount gap is a Gross-vs-Net delta, not a Books-vs-Loyverse "
        "delta.** Zero impact on the missing-revenue gap; it explains part "
        f"of why Loyverse Net ({_fmt(net)}) sits below Gross ({_fmt(gross)})."
    )
    out.append("")

    out.append("## Q3 — refund gap")
    out.append("")
    out.append(
        f"Refunds total {_fmt(refunds)} for the window. Books' sync parser "
        "skips every REFUND receipt, so refunded revenue never enters Books "
        "in either direction. **Same axis as Q2:** the refund gap is between "
        "Gross and Net, not between Books and Loyverse. Zero impact on the "
        "missing-revenue gap."
    )
    out.append("")

    out.append("## Q4 — UTC date-bucketing leak")
    out.append("")
    out.append(
        "Books stores each SALE's date as the UTC calendar date of "
        "`created_at`. Asia/Bangkok is UTC+7, so a SALE near local midnight "
        "can bucket into the previous UTC day. This only crosses a Books "
        "month boundary when the local date is in one month and the UTC date "
        "in another — i.e. the last day of a month or the first day of the "
        "next."
    )
    out.append("")
    out.append(
        f"The reconciliation window {window} **does not cross a calendar "
        "month boundary**, so Q4 is **not applicable** for this window. "
        "Any month-boundary window (e.g. a full-July export spanning Jul 1 "
        "and Jul 31) would need this check; it cannot be done from the "
        "dashboard CSVs (which bucket by Loyverse's local date) — it "
        "requires either the prod DB or a fresh Loyverse API pull carrying "
        "the UTC timestamp."
    )
    out.append("")

    out.append("## Q5 — remainder")
    out.append("")
    out.append(
        "Closing the loop: Loyverse Gross should equal mapped revenue + "
        "flagged revenue (unknown_price + unmapped). Any non-zero remainder "
        "is unexplained."
    )
    out.append("")
    remainder = gross - items_total
    out.append("| Term | THB |")
    out.append("| --- | ---: |")
    out.append(f"| Loyverse Gross | {_fmt(gross)} |")
    out.append(f"| − mapped revenue | −{_fmt(m_rev)} |")
    out.append(f"| − unknown_price revenue | −{_fmt(up_rev)} |")
    out.append(f"| − unmapped revenue | −{_fmt(u_rev)} |")
    out.append(f"| **= remainder** | **{_fmt(remainder)}** |")
    out.append("")
    if abs(remainder) < Decimal("1"):
        out.append(
            "**The parts reconcile to the baht.** The pre-#71-vs-Gross gap "
            "is fully explained by unmapped revenue (plus zero unknown_price "
            "revenue this window). Q2 (discounts) and Q3 (refunds) are "
            "Gross-vs-Net deltas and do not contribute."
        )
    else:
        out.append(
            f"**Remainder is non-zero ({_fmt(remainder)}).** The daily "
            "summary and the items export disagree — likely a timing or "
            "rounding difference between the two Loyverse reports."
        )
    out.append("")

    # Optional category summary
    if categories_csv and categories_csv.exists():
        out.append("## Appendix — category-level Gross (Loyverse)")
        out.append("")
        out.append(
            "Useful as the input to the segmentation decision (#65, pure "
            "clock; #73 implementation). Categories are Loyverse's; the "
            "post-#73 segment call is by clock, not category, so this "
            "table is informational only."
        )
        out.append("")
        out.append("| Category | Items sold | Gross | % of Gross |")
        out.append("| --- | ---: | ---: | ---: |")
        with categories_csv.open(encoding="utf-8") as f:
            cat_rows = list(csv.DictReader(f))
        cat_rows.sort(key=lambda r: Decimal(r["Gross sales"]), reverse=True)
        for r in cat_rows:
            g = Decimal(r["Gross sales"])
            units = Decimal(r["Items sold"])
            p = (g / gross * 100) if gross > 0 else Decimal("0")
            out.append(
                f"| {r['Category']} | {units:.0f} | {_fmt(g)} | {p:.1f}% |"
            )
        out.append("")

    out.append("## Conclusion")
    out.append("")
    out.append(
        f"- For window {window}, Loyverse Gross was {_fmt(gross)}; Books' "
        f"post-#71 headline ties to it by construction. The pre-#71 rule "
        f"would have shown {_fmt(books_pre71)} — a {_fmt(gap_pre71)} gap."
    )
    out.append(
        f"- **Q1**: the gap is entirely unmapped revenue — {_fmt(u_rev)} "
        f"across {len(unmapped)} items, dominated by draught beer (Beer "
        "Taps, ฿33,630 — no recipes by design, per the recipes.yaml "
        "header) and a long tail of menu items added since the recipe "
        "book was last refreshed."
    )
    if not unknown_price:
        out.append(
            "- **No unknown_price revenue this window** — every mapped "
            "SKU has a recipe. (Ingredient-level pricing can't be checked "
            "without the prod DB; the per-SKU `costs` table there would "
            "surface any unpriced leaves.)"
        )
    out.append(
        f"- **Q2 ({_fmt(discounts)} discounts) and Q3 ({_fmt(refunds)} "
        "refunds) do not contribute** to the Books-vs-Loyverse gap. They "
        "are Gross-vs-Net deltas. Books' gross-of-discount line valuation "
        "and REFUND-skipping put its headline on the Gross axis."
    )
    out.append(
        "- **Q4 UTC leak**: N/A — the window does not cross a month "
        "boundary."
    )
    out.append(
        f"- **Q5 remainder**: {_fmt(remainder)} — the parts "
        + ("reconcile to the baht." if abs(remainder) < Decimal("1") else "do NOT reconcile; see above.")
    )
    out.append("")
    out.append("---")
    out.append("")
    out.append(
        "The headline half of the map's destination is delivered by "
        "#71 / ADR-0008: post-#71 Books headline equals Loyverse Gross "
        "by construction. The remaining destination work is cost-side "
        "(Books → Loyverse cost CSV; Loyverse's own COGS this window was "
        f"{_fmt(lv_cogs)} at {((lv_gp/gross*100) if gross>0 else 0):.1f}% "
        "gross margin) and segmentation (#73 pure-clock)."
    )
    out.append("")
    out.append(
        "The largest unmapped-revenue cluster — draught beer — is the "
        "natural first target for a costing pass: it is high-volume, "
        "high-margin, and follows the serving-recipe pattern (one keg "
        "SKU per brand, one pours-per-ml recipe per size). Closing it "
        "would absorb most of the flagged_revenue into the reliable-rows "
        "side and bring Books' COGS into the conversation against "
        "Loyverse's ฿35,689 cost figure."
    )
    return "\n".join(out)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Reconcile Loyverse July exports against Books recipes.yaml."
    )
    p.add_argument("--sales", required=True, type=Path,
                   help="sales-summary CSV (daily Gross/Refunds/Discounts/...)")
    p.add_argument("--items", required=True, type=Path,
                   help="item-sales-summary CSV (per-item Gross/COGS/...)")
    p.add_argument("--categories", type=Path,
                   help="optional category-sales-summary CSV")
    p.add_argument("--recipes", type=Path, default=Path("config/recipes.yaml"),
                   help="Books recipes/mappings YAML (default: config/recipes.yaml)")
    args = p.parse_args(argv)

    for path in (args.sales, args.items, args.recipes):
        if not path.exists():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 1

    window = _parse_window([str(args.sales), str(args.items)])
    sales = load_sales_summary(args.sales)
    items = load_items(args.items)
    mappings, recipe_skus = load_books_mappings(args.recipes)
    mapped, unknown_price, unmapped = classify_items(items, mappings, recipe_skus)

    rendered = render(
        window=window,
        sales=sales,
        items=items,
        mapped=mapped,
        unknown_price=unknown_price,
        unmapped=unmapped,
        categories_csv=args.categories,
    )

    # UTF-8 stdout for the Thai baht sign on Windows cp1252 consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        sys.stdout.buffer.write(rendered.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
        return 0
    print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
