"""CSV template + two-tier preview for cash-spend import (issue #121, #83).

The bulk path for cash-basis supplier purchases: the tool generates a
template pre-filled with the controlled vocabulary (every supplier + every
spend bucket, including retired ones); the partner fills in entry rows
offline in Excel; the upload previews what will land before anything does.

The format mirrors :mod:`tangerine.upload` but carries its own shape:

- A read-only **reference section** at the top of the file (#83 decision 1):
  a ``# vendor`` block listing every supplier (slug + display name), then a
  ``# bucket`` block listing every spend bucket including retired ones.
  The parser skips these rows by their leading ``# vendor`` / ``# bucket``
  tag so they round-trip untouched through re-upload.
- The **entry header** ``kind, date, supplier_id, description, bucket_id,
  amount, vat_inclusive`` then blank entry rows the partner fills in.
- ``kind`` is currently a free column reserved for forward-compatibility
  with :mod:`tangerine.upload`'s ``mapping`` / ``cost`` vocabulary; today
  every entry row is a cash-spend row, so the parser ignores it.

Two-tier preview (#83 decision 2):

- **Hard errors** block apply (unknown vendor/bucket, bad date/amount,
  missing required field, bad ``vat_inclusive``).
- **Soft warnings** don't block (duplicate within-file or against-ledger,
  retired bucket) — the partner can apply anyway.

Defaults (#83 decision 4 + ADR-0010 d4): ``vat_inclusive`` blank → **FALSE**
(the opposite of :mod:`tangerine.upload`'s TRUE default — "default false so
the migration never makes a number worse by guessing wrong"). ``description``
blank → empty string (allowed).

Pure functions over :class:`~tangerine.cash_spend.CashSpendEntry` + the
supplier/bucket vocabularies; the web layer owns HTTP and the store owns
writes. The atomic apply (#83 decision 3) wraps N ``create_cash_spend``
calls in one ``SqliteConfigStore.batch()`` under a single ``session_id``;
``/audit`` shows the import as one reversible stroke.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .cash_spend import CashSpendEntry
from .storage.config_store import SpendBucket
from .types import Supplier

#: The template's header row — the seven importer columns. The leading
#: ``kind`` column mirrors :mod:`tangerine.upload`'s vocabulary shape so a
#: future combined surface stays open; today every entry row is a cash-spend
#: row and the parser ignores the column.
TEMPLATE_COLUMNS: tuple[str, ...] = (
    "kind",
    "date",
    "supplier_id",
    "description",
    "bucket_id",
    "amount",
    "vat_inclusive",
)


def generate_template_csv(
    *,
    suppliers: Sequence[Supplier],
    buckets: Sequence[SpendBucket],
) -> str:
    """The downloadable template per #83 decision 1.

    Emits a read-only ``# vendor`` reference block (every supplier, slug +
    display name), a read-only ``# bucket`` reference block (every spend
    bucket including retired ones, slug + display name), then the header
    row + a single blank entry row. The reference rows are skipped by the
    parser via their leading ``# vendor`` / ``# bucket`` tag (#83 d1).

    Retired buckets are included so a partner importing historical rows
    under a now-retired bucket can read the slug — the parser does not
    refuse retired buckets (that is a soft warning, #83 d2), but the
    new-entry *form* does.
    """
    out = io.StringIO()
    writer = csv.writer(out)

    # --- Reference section (read-only context for the partner reading the
    # file in Excel; the parser skips these rows by their leading tag). The
    # two cells after the tag are ``slug, display name`` — enough to copy a
    # supplier_id / bucket_id into an entry row without flipping screens.
    writer.writerow(["# vendor", "supplier_id", "name"])
    for supplier in sorted(suppliers, key=lambda s: s.supplier_id):
        writer.writerow(["# vendor", supplier.supplier_id, supplier.name])

    writer.writerow(["# bucket", "bucket_id", "name"])
    # Spend buckets sort seed-first then by creation (the store's own order);
    # the template reproduces that so the file reads top-to-bottom like the
    # admin page.
    for bucket in buckets:
        retired_marker = " (retired)" if bucket.retired_at else ""
        writer.writerow(["# bucket", bucket.bucket_id, f"{bucket.name}{retired_marker}"])

    # --- The entry section: the header the parser keys on, then a single
    # blank row so Excel keeps the columns wide enough to type into.
    writer.writerow(TEMPLATE_COLUMNS)
    writer.writerow([""] * len(TEMPLATE_COLUMNS))

    return out.getvalue()


@dataclass(frozen=True)
class ImportRow:
    """One parsed entry row the importer will apply on confirm.

    Carries the parsed :class:`CashSpendEntry` (the store consumes this
    shape directly) plus the source row number for the preview's "Row N"
    labelling. The per-invoice grouping (sibling rows sharing date +
    supplier) is a derived preview fact exposed on :class:`ImportPreview`,
    not stored here.
    """

    row_number: int
    entry: CashSpendEntry


@dataclass(frozen=True)
class InvoiceGroup:
    """The derived per-invoice preview grouping (#83 d1 / ADR-0010 decision A).

    Cash-spend has no parent row — the invoice total is the derived fact
    ``SUM(amount) WHERE date=X AND supplier_id=Y``. This grouping is the
    cash-spend analog of :mod:`tangerine.upload`'s per-row ``old_net →
    new_net`` diff: the partner's sanity check that "yes, that Makro bill
    was 4 sibling rows totalling 4,200" before they hit Apply.
    """

    date: date
    supplier_id: str
    sibling_count: int
    invoice_total: Decimal


@dataclass(frozen=True)
class RowError:
    """One hard error that blocks apply (#83 d2).

    Numbered as Excel shows it (header is row 1, the reference blocks above
    it are rows 1..N — a partner reading "Row 47" jumps to Excel's row 47).
    """

    row_number: int
    message: str


@dataclass(frozen=True)
class RowWarning:
    """One soft warning that does not block apply (#83 d2).

    Duplicates (same ``date + supplier_id + amount`` within-file or
    against-ledger) and retired buckets warn but apply when the partner
    confirms — historical imports under a now-retired bucket must not fail.
    """

    row_number: int
    message: str


@dataclass(frozen=True)
class ImportPreview:
    """Everything ``POST /admin/cash-spend/import/preview`` shows before
    anything lands.

    ``errors`` non-empty blocks the apply (#83 d2): a typo in row 47 must
    not silently duplicate a 4,200 THB bill, so the partner fixes and
    re-uploads rather than applying a file the tool only half-understood.
    ``warnings`` never block — they are the partner's call to make.

    ``invoices`` is the per-invoice grouping over ``new_rows`` (the
    partner's sanity check that the sibling rows total what the paper
    receipt says); it is empty when ``new_rows`` is empty.
    """

    new_rows: tuple[ImportRow, ...]
    invoices: tuple[InvoiceGroup, ...]
    errors: tuple[RowError, ...]
    warnings: tuple[RowWarning, ...]

    @property
    def has_rows(self) -> bool:
        return bool(self.new_rows)


def parse_import(
    text: str,
    *,
    suppliers: Sequence[Supplier],
    buckets: Sequence[SpendBucket],
    existing_rows: Sequence[CashSpendEntry],
) -> ImportPreview:
    """Parse an uploaded cash-spend CSV and produce the two-tier preview.

    Per-row parse producing :class:`ImportRow` new-entries (with derived
    per-invoice grouping for preview), :class:`RowError` hard errors, and
    :class:`RowWarning` soft warnings per #83 decision 2.

    The parser skips any row whose first cell starts with ``# vendor`` or
    ``# bucket`` (#83 decision 1) and the header row. Blank rows are
    skipped. Entry rows are validated against the controlled vocabularies
    (``suppliers`` for ``supplier_id``, ``buckets`` for ``bucket_id``
    including retired) and the existing ledger (duplicates warn).
    """
    known_suppliers = {s.supplier_id for s in suppliers}
    known_buckets = {b.bucket_id for b in buckets}
    retired_buckets = {b.bucket_id for b in buckets if b.retired_at}
    # The against-ledger duplicate check keys on (date, supplier_id, amount)
    # — the #83 d2 "duplicate within-file or against-ledger" definition.
    ledger_keys: set[tuple[str, str, Decimal]] = {
        (e.date.isoformat(), e.supplier_id, e.amount) for e in existing_rows
    }

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return ImportPreview(
            new_rows=(),
            invoices=(),
            errors=(RowError(1, "the file is empty"),),
            warnings=(),
        )

    # Locate the entry header (the row whose first cell is exactly "kind").
    # Reference rows above it (``# vendor`` / ``# bucket``) and any stray
    # blank rows are skipped wholesale; everything below the header is an
    # entry row.
    header_index = _find_header(rows)
    if header_index is None:
        return ImportPreview(
            new_rows=(),
            invoices=(),
            errors=(
                RowError(
                    1,
                    "missing header row — expected 'kind,date,supplier_id,"
                    "description,bucket_id,amount,vat_inclusive'",
                ),
            ),
            warnings=(),
        )
    header = [cell.strip() for cell in rows[header_index]]
    missing = [col for col in TEMPLATE_COLUMNS if col not in header]
    if missing:
        return ImportPreview(
            new_rows=(),
            invoices=(),
            errors=tuple(
                RowError(header_index + 1, f"missing column: {col}")
                for col in missing
            ),
            warnings=(),
        )
    index = {col: header.index(col) for col in header}

    new_rows: list[ImportRow] = []
    errors: list[RowError] = []
    warnings: list[RowWarning] = []
    # Within-file duplicate detection: keys seen so far in this file.
    seen_in_file: set[tuple[str, str, Decimal]] = set()

    for offset, cells in enumerate(rows[header_index + 1 :], start=1):
        row_number = header_index + 1 + offset  # Excel's 1-based row number
        # Skip reference rows that snuck below the header (defensive — the
        # template emits them above, but a partner's hand-edit might not).
        first = cells[0].strip() if cells else ""
        if first.startswith("# vendor") or first.startswith("# bucket"):
            continue
        # Skip blank rows (the template's trailing blank row, or gaps).
        if not any(cell.strip() for cell in cells):
            continue

        def cell(column: str) -> str:
            i = index[column]
            return cells[i].strip() if i < len(cells) else ""

        parsed = _parse_entry_row(
            row_number,
            date_str=cell("date"),
            supplier_id=cell("supplier_id"),
            description=cell("description"),
            bucket_id=cell("bucket_id"),
            amount_str=cell("amount"),
            vat_str=cell("vat_inclusive"),
            known_suppliers=known_suppliers,
            known_buckets=known_buckets,
        )
        if isinstance(parsed, RowError):
            errors.append(parsed)
            continue
        entry = parsed
        # Soft warnings (#83 d2): duplicate within-file, duplicate against
        # the ledger, retired bucket. None block apply.
        key = (entry.date.isoformat(), entry.supplier_id, entry.amount)
        if key in seen_in_file:
            warnings.append(
                RowWarning(
                    row_number,
                    "duplicate of an earlier row in this file "
                    "(same date + supplier + amount)",
                )
            )
        elif key in ledger_keys:
            warnings.append(
                RowWarning(
                    row_number,
                    "matches a row already in the ledger "
                    "(same date + supplier + amount)",
                )
            )
        seen_in_file.add(key)
        if entry.bucket_id in retired_buckets:
            warnings.append(
                RowWarning(
                    row_number,
                    f"bucket '{entry.bucket_id}' is retired — allowed for "
                    "historical import",
                )
            )
        new_rows.append(ImportRow(row_number=row_number, entry=entry))

    invoices = _group_invoices(new_rows)
    return ImportPreview(
        new_rows=tuple(new_rows),
        invoices=tuple(invoices),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _find_header(rows: list[list[str]]) -> int | None:
    """The index of the entry header row (first cell exactly ``kind``).

    Reference rows (``# vendor`` / ``# bucket``) never collide — they start
    with ``#``. Returns ``None`` if no header is found.
    """
    for i, cells in enumerate(rows):
        if cells and cells[0].strip() == "kind":
            return i
    return None


def _parse_entry_row(
    row_number: int,
    *,
    date_str: str,
    supplier_id: str,
    description: str,
    bucket_id: str,
    amount_str: str,
    vat_str: str,
    known_suppliers: set[str],
    known_buckets: set[str],
) -> CashSpendEntry | RowError:
    """Parse one entry row's cells into a ``CashSpendEntry`` or a hard error.

    Hard errors (#83 d2): unknown ``supplier_id``, unknown ``bucket_id``
    (retired or live — retired is a *soft* warning, unknown is *hard*),
    malformed ``date``, malformed or non-positive ``amount``, missing
    required field (``date`` / ``supplier_id`` / ``bucket_id`` / ``amount``),
    malformed ``vat_inclusive``.

    Defaults (#83 d4 + ADR-0010 d4): ``vat_inclusive`` blank → ``False``;
    ``description`` blank → empty string.
    """
    # --- Required fields (missing = hard error).
    if not date_str:
        return RowError(row_number, "missing date")
    if not supplier_id:
        return RowError(row_number, "missing supplier_id")
    if not bucket_id:
        return RowError(row_number, "missing bucket_id")
    if not amount_str:
        return RowError(row_number, "missing amount")

    # --- Controlled-vocabulary FK checks (unknown = hard error).
    if supplier_id not in known_suppliers:
        return RowError(row_number, f"unknown supplier_id {supplier_id!r}")
    if bucket_id not in known_buckets:
        return RowError(
            row_number, f"unknown bucket_id {bucket_id!r}"
        )

    # --- Date parse (ISO-8601 calendar date).
    try:
        parsed_date = date.fromisoformat(date_str)
    except ValueError:
        return RowError(row_number, f"malformed date {date_str!r}")

    # --- Amount parse (Decimal, must be positive).
    try:
        amount = Decimal(amount_str)
    except InvalidOperation:
        return RowError(row_number, f"malformed amount {amount_str!r}")
    if amount <= 0:
        return RowError(
            row_number, f"amount must be positive, got {amount_str!r}"
        )

    # --- vat_inclusive parse (blank → False; TRUE/FALSE + the usual
    # truthy/falsy variants). Mirrors :mod:`tangerine.upload`'s ``_parse_vat``
    # but inverts the blank default (ADR-0010 d4).
    vat = _parse_vat_default_false(vat_str)
    if vat is None:
        return RowError(
            row_number,
            f"vat_inclusive must be TRUE or FALSE, got {vat_str!r}",
        )

    return CashSpendEntry(
        # row_id is assigned by the store on insert; 0 is the placeholder
        # the create form also uses.
        row_id=0,
        date=parsed_date,
        supplier_id=supplier_id,
        description=description,
        bucket_id=bucket_id,
        amount=amount,
        vat_inclusive=vat,
    )


def _parse_vat_default_false(cell: str) -> bool | None:
    """TRUE/FALSE (Excel's spelling) plus the usual truthy/falsy variants.

    Blank defaults to **False** — the opposite of :mod:`tangerine.upload`'s
    TRUE default (ADR-0010 d4: "default false so the migration never makes a
    number worse by guessing wrong"). Returns ``None`` for a value that is
    neither blank, a recognized truth, nor a recognized falsity.
    """
    normalized = cell.strip().lower()
    if normalized in ("", "false", "0", "no", "n"):
        return False
    if normalized in ("true", "1", "yes", "y"):
        return True
    return None


def _group_invoices(rows: Sequence[ImportRow]) -> list[InvoiceGroup]:
    """Group parsed rows into the per-invoice preview (#83 d1 / ADR-0010 A).

    Sibling rows sharing ``(date, supplier_id)`` collapse into one
    :class:`InvoiceGroup` whose ``invoice_total`` is ``SUM(amount)`` — the
    derived invoice-total fact the partner reconciles against the paper
    receipt. Order is first-seen, so the preview reads top-to-bottom like
    the file.
    """
    totals: dict[tuple[date, str], Decimal] = {}
    counts: dict[tuple[date, str], int] = {}
    order: list[tuple[date, str]] = []
    for row in rows:
        key = (row.entry.date, row.entry.supplier_id)
        if key not in totals:
            totals[key] = Decimal("0")
            counts[key] = 0
            order.append(key)
        totals[key] += row.entry.amount
        counts[key] += 1
    return [
        InvoiceGroup(
            date=d,
            supplier_id=sid,
            sibling_count=counts[(d, sid)],
            invoice_total=totals[(d, sid)],
        )
        for (d, sid) in order
    ]


__all__ = [
    "ImportPreview",
    "ImportRow",
    "InvoiceGroup",
    "RowError",
    "RowWarning",
    "TEMPLATE_COLUMNS",
    "generate_template_csv",
    "parse_import",
]
