"""CSV template + upload reconciliation for mappings and costs (Wave 1.5, Slice 3).

The bulk path for flat config data (~400 rows): the tool generates a template
pre-filled with every Loyverse item (mappings) and every known SKU (costs);
the partner fills in the blanks offline in Excel; the upload previews what
will change before anything lands.

The format is one CSV with a ``kind`` column (``mapping`` / ``cost``) instead
of the spreadsheet tabs an XLSX would carry — the issue chose CSV, and Excel
filters on the column just as well. Pure functions over the same shapes the
coverage engine consumes; the web layer owns HTTP and the store owns writes.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .loyverse.store import MenuItem
from .storage.config_store import CostRow, net_price_per_unit
from .types import SkuMapping, SkuRecord

#: The template's header row. ``item_*`` columns are filled on mapping rows,
#: ``sku_name``/``unit``/pack columns on cost rows; ``sku_id`` is the one
#: column both kinds share (the mapping's target / the cost's subject).
TEMPLATE_COLUMNS = (
    "kind",
    "item_id",
    "item_name",
    "sku_id",
    "sku_name",
    "unit",
    "pack_price",
    "pack_quantity",
    "vat_inclusive",
)


@dataclass(frozen=True)
class MappingChange:
    """One item→SKU assignment the upload will apply on confirm."""

    row_number: int
    item_id: str
    old_sku_id: str | None
    new_sku_id: str


@dataclass(frozen=True)
class CostChange:
    """One cost update the upload will apply on confirm.

    ``new_net`` is derived here, at preview time, from the row's pack inputs
    — so the number the partner confirms is exactly the number that lands.
    """

    row_number: int
    sku_id: str
    pack_price: Decimal
    pack_quantity: Decimal
    vat_inclusive: bool
    old_net: Decimal | None
    new_net: Decimal


@dataclass(frozen=True)
class RowError:
    """One rejected row, numbered as Excel shows it (header is row 1)."""

    row_number: int
    message: str


@dataclass(frozen=True)
class UploadPreview:
    """Everything ``POST /upload`` shows before anything lands.

    ``errors`` non-empty blocks the confirm: a typo in row 147 must not
    silently corrupt the cost book, so the partner fixes and re-uploads
    rather than applying a file the tool only half-understood.
    """

    mapping_changes: tuple[MappingChange, ...]
    cost_changes: tuple[CostChange, ...]
    errors: tuple[RowError, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.mapping_changes or self.cost_changes)


def parse_upload(
    text: str,
    *,
    skus: list[SkuRecord],
    mappings: list[SkuMapping],
    cost_rows: list[CostRow],
    produced_sku_ids: set[str] | None = None,
) -> UploadPreview:
    """Reconcile an uploaded CSV against current state.

    A row only becomes a change when it *differs* from what is stored — the
    template comes back with every pre-filled row intact, and re-uploading
    an untouched template must be a no-op. Blank cells mean "leave as is":
    a mapping row with no ``sku_id`` stays unmapped; a cost row with no pack
    inputs keeps its current price. A blank ``vat_inclusive`` on a filled
    cost row defaults to TRUE, matching the editor's checkbox default.

    ``produced_sku_ids`` is the set of SKUs that have a recipe. A cost row
    targeting one is a per-row error (issue #37): a produced SKU's cost is
    derived from its recipe, never typed — the "derived only" rule the cost
    form enforces, held on the bulk path too so a spreadsheet cannot slip a
    direct price past it.
    """
    known_skus = {s.sku_id for s in skus}
    produced = produced_sku_ids or set()
    mapping_by_item = {m.item_id: m.sku_id for m in mappings}
    cost_by_sku = {c.sku_id: c for c in cost_rows}

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return UploadPreview((), (), (RowError(1, "the file is empty"),))
    header = [cell.strip() for cell in rows[0]]
    missing = [col for col in _REQUIRED_COLUMNS if col not in header]
    if missing:
        return UploadPreview(
            (),
            (),
            tuple(RowError(1, f"missing column: {col}") for col in missing),
        )
    index = {col: header.index(col) for col in header}

    mapping_changes: list[MappingChange] = []
    cost_changes: list[CostChange] = []
    errors: list[RowError] = []

    for row_number, cells in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in cells):
            continue

        def cell(column: str) -> str:
            i = index[column]
            return cells[i].strip() if i < len(cells) else ""

        kind = cell("kind").lower()
        if kind == "mapping":
            change_or_error = _parse_mapping_row(
                row_number,
                item_id=cell("item_id"),
                sku_id=cell("sku_id"),
                known_skus=known_skus,
                mapping_by_item=mapping_by_item,
            )
            if isinstance(change_or_error, RowError):
                errors.append(change_or_error)
            elif change_or_error is not None:
                mapping_changes.append(change_or_error)
        elif kind == "cost":
            cost_or_error = _parse_cost_row(
                row_number,
                sku_id=cell("sku_id"),
                pack_price=cell("pack_price"),
                pack_quantity=cell("pack_quantity"),
                vat_inclusive=cell("vat_inclusive"),
                known_skus=known_skus,
                produced=produced,
                cost_by_sku=cost_by_sku,
            )
            if isinstance(cost_or_error, RowError):
                errors.append(cost_or_error)
            elif cost_or_error is not None:
                cost_changes.append(cost_or_error)
        else:
            errors.append(
                RowError(row_number, f"unknown kind {kind!r} (mapping or cost)")
            )

    return UploadPreview(tuple(mapping_changes), tuple(cost_changes), tuple(errors))


def _parse_mapping_row(
    row_number: int,
    *,
    item_id: str,
    sku_id: str,
    known_skus: set[str],
    mapping_by_item: dict[str, str],
) -> MappingChange | RowError | None:
    if not item_id:
        return RowError(row_number, "mapping row has no item_id")
    if not sku_id:
        return None  # blank = the partner left this item unmapped, no change
    if sku_id not in known_skus:
        return RowError(row_number, f"unknown SKU {sku_id!r}")
    old = mapping_by_item.get(item_id)
    if old == sku_id:
        return None
    return MappingChange(
        row_number=row_number, item_id=item_id, old_sku_id=old, new_sku_id=sku_id
    )


def _parse_cost_row(
    row_number: int,
    *,
    sku_id: str,
    pack_price: str,
    pack_quantity: str,
    vat_inclusive: str,
    known_skus: set[str],
    produced: set[str],
    cost_by_sku: dict[str, CostRow],
) -> CostChange | RowError | None:
    if not sku_id:
        return RowError(row_number, "cost row has no sku_id")
    if not pack_price and not pack_quantity:
        return None  # blank = the partner left this cost as is
    if sku_id not in known_skus:
        return RowError(row_number, f"unknown SKU {sku_id!r}")
    if sku_id in produced:
        return RowError(
            row_number,
            f"{sku_id!r} is a produced SKU — its cost is derived from its "
            "recipe and cannot be entered directly",
        )
    try:
        price = Decimal(pack_price)
        quantity = Decimal(pack_quantity)
    except InvalidOperation:
        return RowError(
            row_number,
            f"malformed number (pack_price={pack_price!r},"
            f" pack_quantity={pack_quantity!r})",
        )
    if price < 0 or quantity <= 0:
        return RowError(
            row_number, "pack_price must be ≥ 0 and pack_quantity > 0"
        )
    vat = _parse_vat(vat_inclusive)
    if vat is None:
        return RowError(
            row_number,
            f"vat_inclusive must be TRUE or FALSE, got {vat_inclusive!r}",
        )

    current = cost_by_sku.get(sku_id)
    if (
        current is not None
        and current.pack_price == price
        and current.pack_quantity == quantity
        and current.vat_inclusive == vat
    ):
        return None  # the pre-filled row came back untouched
    return CostChange(
        row_number=row_number,
        sku_id=sku_id,
        pack_price=price,
        pack_quantity=quantity,
        vat_inclusive=vat,
        old_net=current.price_per_unit_net if current is not None else None,
        new_net=net_price_per_unit(price, quantity, vat),
    )


def _parse_vat(cell: str) -> bool | None:
    """TRUE/FALSE (Excel's spelling) plus the usual truthy/falsy variants.

    Blank defaults to True — the same default as the editor's checkbox
    (Makro, VAT-registered, is the dominant supplier).
    """
    normalized = cell.strip().lower()
    if normalized in ("", "true", "1", "yes", "y"):
        return True
    if normalized in ("false", "0", "no", "n"):
        return False
    return None


#: Columns an upload must carry. ``item_name``/``sku_name``/``unit`` are
#: context for the partner reading the file in Excel, not inputs — a file
#: without them still parses.
_REQUIRED_COLUMNS = (
    "kind",
    "item_id",
    "sku_id",
    "pack_price",
    "pack_quantity",
    "vat_inclusive",
)


def generate_template_csv(
    *,
    menu: dict[str, MenuItem],
    skus: list[SkuRecord],
    mappings: list[SkuMapping],
    cost_rows: list[CostRow],
) -> str:
    """The downloadable template, pre-filled with current state.

    One ``mapping`` row per Loyverse item the sync has seen — unmapped items
    first (they are the blanks the partner is here to fill), ``sku_id``
    pre-filled where a mapping exists. Then one ``cost`` row per known SKU,
    with unit, current pack inputs (blank for YAML-migrated rows, which never
    had them) and the VAT flag pre-filled.
    """
    mapping_by_item = {m.item_id: m.sku_id for m in mappings}
    cost_by_sku = {c.sku_id: c for c in cost_rows}

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(TEMPLATE_COLUMNS)

    items = sorted(
        menu.values(), key=lambda i: (i.item_id in mapping_by_item, i.item_id)
    )
    for item in items:
        writer.writerow(
            (
                "mapping",
                item.item_id,
                item.name,
                mapping_by_item.get(item.item_id, ""),
                "",
                "",
                "",
                "",
                "",
            )
        )

    for sku in sorted(skus, key=lambda s: s.sku_id):
        cost = cost_by_sku.get(sku.sku_id)
        writer.writerow(
            (
                "cost",
                "",
                "",
                sku.sku_id,
                sku.name,
                sku.unit or "",
                str(cost.pack_price) if cost and cost.pack_price is not None else "",
                (
                    str(cost.pack_quantity)
                    if cost and cost.pack_quantity is not None
                    else ""
                ),
                ("TRUE" if cost.vat_inclusive else "FALSE") if cost else "",
            )
        )

    return out.getvalue()
