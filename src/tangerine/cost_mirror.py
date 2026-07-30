"""Cost-mirror: round-trip a Loyverse items export with Books' costs filled in.

Issue #101 (parent spec #100): the partner-facing cost-mirror UX minus the
audit trail. A partner uploads a Loyverse back-office items export (the
canonical row set with ``Handle``, ``SKU``, ``Name``, ``Price``, ``Cost``,
etc.); Books parses it, resolves a net per-unit recipe cost for each mapped
variant via the existing :class:`~tangerine.margin.CostResolver`, diffs its
number against the ``Cost`` the export carries, and shows a drift report.
On partner confirm, Books writes the filled round-trip CSV (every Loyverse
column preserved verbatim; only ``Cost`` overwritten for costable rows;
``Cost`` left blank for uncostable rows) and serves it for download.

Three rules this module holds end-to-end:

- **Round-trip shape** — the emitted file is the uploaded file with only the
  ``Cost`` cell touched per row. Every column is preserved (no retype, no
  reorder); the header row is byte-identical to the upload. Loyverse fails
  the import on a renamed header (#72 §3), so Books snapshots the uploaded
  header rather than typing one.
- **Blank, never zero, for uncostable rows** — mapped + costable items get
  :meth:`CostResolver.cost_per_unit` rounded 2 dp half-up; mapped-but-
  unknown-price items (``resolver.has_unknown_price(recipe)`` true) and
  unmapped items (no recipe for that SKU) get a **blank** ``Cost`` cell with
  the row otherwise intact. Blank doesn't overwrite, so Loyverse's existing
  cost stays untouched (#72 §2). Zero is never emitted — it would zero
  Loyverse's COGS for that item.
- **Detect, report, overwrite on confirm** — the drift diff the partner sees
  at prepare time is the visibility layer over an unconditional overwrite.
  Books is source of truth: on confirm, ``Cost`` is overwritten for every
  costable row, drifted or not. The diff flags the four states ("filled",
  "no Books cost: unmapped", "no Books cost: unknown-price", "differs:
  Loyverse X → Books Y").

Format facts inherited from #72: target column ``Cost``, joined on ``SKU``
per variant (the exact key Books already uses); money is digits + point only
(``45.50``, never ``฿45.50`` or ``45,50``); encoding UTF-8 **with BOM** (the
safe choice for Thai item names opened in Excel); hard limits 5 MB / 10 000
items (Tangerine ≈ 232) — this slice does not enforce the limits (Loyverse
itself does, on re-import); it preserves whatever the upload carried.

Pure engine — no I/O, no storage imports. The store supplies reads
(:meth:`~tangerine.storage.config_store.SqliteConfigStore.recipes`,
:meth:`~tangerine.storage.config_store.SqliteConfigStore.mappings`,
:meth:`~tangerine.storage.config_store.SqliteConfigStore.cost_book`); the web
layer (:mod:`tangerine.web.app`) owns HTTP. This composes with the existing
cost book — it does not alter it.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from .cost import CostBook
from .margin import CostResolver
from .recipes import RecipeCatalog
from .types import Money

#: The Loyverse items-export column this mirror targets (#72 §1). Joined to
#: Books on ``SKU`` per variant row — the exact key Books already keys sales
#: and mappings on, so the join is by construction.
COST_COLUMN = "Cost"

#: The three columns a Loyverse items export must carry for Books to cost it.
#: Missing any one raises :class:`InvalidLoyverseExportError` naming it (AC:
#: "wrong-file / missing-column errors render a clear error page, not a
#: corrupt file"). Loyverse itself requires ``SKU``; ``Handle`` groups
#: variants of one item; ``Cost`` is the column Books fills.
REQUIRED_COLUMNS = ("Handle", "SKU", "Cost")

#: Two-decimal quantisation with half-up rounding — ``0.125`` → ``0.13``, not
#: Python's default banker's-rounding ``0.12``. A THB cent is a real cent;
#: Loyverse's own display rounds the same way, so the number the partner sees
#: in Books' diff matches the number that lands in Loyverse after import.
_CENTS = Decimal("0.01")

#: The UTF-8 BOM, written at the head of every emitted file. The safe choice
#: for Thai item names opened in Excel (#72 §3); Loyverse's own export-to-
#: LibreOffice flow specifies ``Character set: Unicode (UTF-8)``.
_BOM = "\ufeff"


class DriftStatus(str, Enum):
    """The four states a drift row carries — what the diff page renders.

    The three "no Books cost" / "differs" labels are the visibility layer;
    underneath, costable rows (``FILLED`` and ``DIFFERS``) get an
    unconditional ``Cost`` overwrite on confirm (Books is source of truth),
    and uncostable rows keep their ``Cost`` blank.
    """

    #: A costable row (mapped + fully priced) whose uploaded ``Cost`` matches
    #: Books' number, or whose uploaded ``Cost`` was blank (nothing in
    #: Loyverse to differ from). The cell is filled with Books' cost; on
    #: confirm this is an overwrite (blank → Books) or a no-op (match).
    FILLED = "filled"

    #: A costable row whose uploaded ``Cost`` disagrees with Books' number.
    #: Both values are carried so the diff page can render "differs: Loyverse
    #: X → Books Y". On confirm Books overwrites with its number.
    DIFFERS = "differs"

    #: A mapped row whose recipe has an unpriced ingredient (directly or
    #: recursively through a prep, ADR-0005 decision 3). Books cannot compute
    #: a cost, so the ``Cost`` cell is **blanked** (never zeroed) — Loyverse's
    #: existing cost is preserved across the re-import (#72 §2).
    NO_BOOKS_COST_UNKNOWN_PRICE = "no Books cost: unknown-price"

    #: A row whose SKU has no recipe in Books (no mapping reached it). Books
    #: has nothing to say about it; the ``Cost`` cell is **blanked** (never
    #: zeroed) — same preservation rule as unknown-price.
    NO_BOOKS_COST_UNMAPPED = "no Books cost: unmapped"


@dataclass(frozen=True)
class DriftRow:
    """One export row's diff against Books — what the prepare page renders.

    ``loyverse_cost`` is the ``Cost`` cell the upload carried (``None`` when
    blank). ``books_cost`` is :meth:`CostResolver.cost_per_unit` rounded 2 dp
    half-up for costable rows, ``None`` for uncostable rows. ``drift`` is True
    only when both are present and disagree — the flag the diff page uses to
    draw attention; an unconditional overwrite on confirm means a non-drift
    costable row is still written (slice 1 fills; drift detection is the
    visibility layer over the overwrite).

    ``row_number`` is 1-based with the header as row 1 (the way Excel shows
    it), so a wrong-file error can name the offending row in the partner's
    own frame.
    """

    row_number: int
    sku: str
    name: str
    status: DriftStatus
    loyverse_cost: Money | None
    books_cost: Money | None
    drift: bool


@dataclass(frozen=True)
class PrepareResult:
    """The parsed export joined to Books' costs, ready to render or emit.

    Carries the uploaded rows verbatim (``header`` + ``rows``) so
    :func:`emit_filled_csv` can reproduce them byte-for-byte with only the
    ``Cost`` cell touched, and the per-row diff (:attr:`drift_rows`) the
    prepare page renders. Counts are the roll-ups the diff page (and, in
    slice 2, the ``loyverse_exports`` audit row) carries.
    """

    #: The uploaded header row, preserved exactly (BOM stripped). The emitted
    #: file's header is byte-identical to this — Loyverse fails the import on
    #: a renamed header (#72 §3), so Books snapshots it.
    header: tuple[str, ...]

    #: The uploaded data rows, each as the raw cell list the parser produced
    #: (column order = ``header`` order). Untouched by costing; the emitter
    #: walks them and overwrites only the ``Cost`` cell.
    rows: tuple[tuple[str, ...], ...]

    #: The per-row diff in upload order. Length == ``len(rows)`` (every row
    #: appears, costable or not — the uncostable ones surface as
    #: "no Books cost" lines, not silently dropped).
    drift_rows: tuple[DriftRow, ...]

    #: Total data rows in the upload (== ``len(rows)``).
    item_count: int

    #: Costable rows — the count the partner sees as "N items Books will
    #: cost". ``FILLED`` + ``DIFFERS`` rows together.
    filled_count: int

    #: Rows whose ``Cost`` cell Books will change on confirm — costable rows
    #: whose uploaded ``Cost`` differs from Books' number **or** is blank
    #: (Books adds a cost Loyverse didn't have). A costable row whose
    #: uploaded ``Cost`` already matches Books' is **not** a change. Slice 2's
    #: zero-drift-confirm AC pins this: a confirm where every costable row
    #: matches records ``changed_count = 0``. (The overwrite itself is still
    #: unconditional — Books is source of truth — but "changed" is the
    #: partner-facing, audit-row notion of what moved.)
    changed_count: int


@dataclass(frozen=True)
class InvalidLoyverseExportError(ValueError):
    """Raised when an upload is not a Loyverse items export Books can cost.

    Carries the missing column list so the prepare route can render a clear
    error page ("missing column: SKU") rather than producing a corrupt file.
    The AC's "wrong-file / missing-column errors render a clear error page,
    not a corrupt file".
    """

    missing_columns: tuple[str, ...]

    def __str__(self) -> str:
        if not self.missing_columns:
            return "not a Loyverse items export"
        names = ", ".join(self.missing_columns)
        return f"missing required column(s): {names}"


def prepare(
    *,
    csv_text: str,
    recipes: RecipeCatalog,
    cost: CostBook,
) -> PrepareResult:
    """Parse a Loyverse items export and join Books' costs onto each row.

    Returns a :class:`PrepareResult` carrying the verbatim rows (for
    round-trip emission) and the per-row diff (for the prepare page). Raises
    :class:`InvalidLoyverseExportError` when the upload is missing any of
    ``Handle`` / ``SKU`` / ``Cost`` — the wrong-file guard.

    A row's classification:

    - **costable** — ``recipes.recipe_for_sku(sku)`` resolves and
      :meth:`CostResolver.has_unknown_price` is False; ``books_cost`` is
      :meth:`CostResolver.cost_per_unit` rounded 2 dp half-up.
    - **unknown-price** — the recipe resolves but an ingredient (recursively)
      has no cost-book price; ``books_cost`` is None, the cell will blank.
    - **unmapped** — no recipe for that SKU; ``books_cost`` is None, the cell
      will blank.

    A costable row is ``DIFFERS`` when its uploaded ``Cost`` is present and
    disagrees with ``books_cost`` (rounded comparison), else ``FILLED``. A
    blank uploaded ``Cost`` is ``FILLED`` (nothing in Loyverse to differ
    from). Drift detection is the visibility layer; the emitter overwrites
    ``Cost`` for every costable row regardless.
    """
    header, data_rows = _parse(csv_text)

    resolver = CostResolver(recipes, cost)
    drift_rows: list[DriftRow] = []
    filled_count = 0
    changed_count = 0

    for index, row in enumerate(data_rows, start=2):  # row 1 is the header
        sku = _cell(row, header, "SKU")
        name = _cell(row, header, "Name") if "Name" in header else ""
        loyverse_raw = _cell(row, header, COST_COLUMN)
        loyverse_cost = _parse_money(loyverse_raw) if loyverse_raw.strip() else None

        recipe = recipes.recipe_for_sku(sku) if sku else None
        if recipe is None:
            status = DriftStatus.NO_BOOKS_COST_UNMAPPED
            books_cost: Money | None = None
            drift = False
        elif resolver.has_unknown_price(recipe):
            status = DriftStatus.NO_BOOKS_COST_UNKNOWN_PRICE
            books_cost = None
            drift = False
        else:
            books_cost = _round_money(resolver.cost_per_unit(recipe))
            filled_count += 1
            rounded_loyverse = (
                _round_money(loyverse_cost) if loyverse_cost is not None else None
            )
            if rounded_loyverse is not None and rounded_loyverse != books_cost:
                status = DriftStatus.DIFFERS
                drift = True
                changed_count += 1
            else:
                status = DriftStatus.FILLED
                drift = False
                # A blank Loyverse Cost that Books now fills counts as a
                # change — the partner is adding a cost Loyverse didn't have.
                # A matching Loyverse Cost (rounded) does not. Slice 2's
                # zero-drift-confirm AC pins this: a confirm where Books'
                # numbers already match the upload records ``changed_count=0``.
                if rounded_loyverse is None:
                    changed_count += 1

        drift_rows.append(
            DriftRow(
                row_number=index,
                sku=sku,
                name=name,
                status=status,
                loyverse_cost=loyverse_cost,
                books_cost=books_cost,
                drift=drift,
            )
        )

    return PrepareResult(
        header=header,
        rows=data_rows,
        drift_rows=tuple(drift_rows),
        item_count=len(data_rows),
        filled_count=filled_count,
        changed_count=changed_count,
    )


def emit_filled_csv(result: PrepareResult) -> str:
    """Emit the round-trip CSV: every uploaded column preserved, only
    ``Cost`` touched.

    For each costable row the ``Cost`` cell is ``books_cost`` (digits + point
    only, 2 dp). For each uncostable row (unknown-price or unmapped) the
    ``Cost`` cell is **blank** — never zero — so Loyverse's existing cost is
    preserved across the re-import (#72 §2). The header row is written
    byte-identical to the upload (BOM prepended for Excel/Thai compatibility,
    #72 §3). Every other column rides through verbatim.

    The emitter reads only :attr:`PrepareResult.drift_rows` for the
    cost/blank decision and :attr:`PrepareResult.rows` for the cell values —
    it never re-derives costs, so a stale ``PrepareResult`` cannot drift from
    what ``prepare`` computed.
    """
    out = io.StringIO()
    out.write(_BOM)
    writer = csv.writer(out)
    writer.writerow(result.header)

    cost_index = result.header.index(COST_COLUMN)
    for row, drift in zip(result.rows, result.drift_rows, strict=True):
        new_cost = (
            _format_money(drift.books_cost)
            if drift.books_cost is not None
            else ""
        )
        # Copy the row cell-for-cell, overwriting only the Cost cell. A row
        # shorter than the header (trailing blanks trimmed by an exporter)
        # is padded out so the Cost cell always lands in the right column.
        cells = list(row)
        while len(cells) <= cost_index:
            cells.append("")
        cells[cost_index] = new_cost
        writer.writerow(cells)

    return out.getvalue()


def drift_payload_json(result: PrepareResult) -> str:
    """The per-SKU drift payload that lands in ``loyverse_exports`` (slice 2).

    A JSON array of ``{sku, name, loyverse_cost, books_cost}`` for the rows
    the diff flagged as ``DIFFERS`` — the costable rows whose uploaded
    ``Cost`` both *was present* and *disagreed* with Books' number. These are
    the rows where Books overwrites a value Loyverse actually held (the
    "differs: Loyverse X → Books Y" lines), so "what did we overwrite and
    when" is answerable from the payload alone without reconstruction.

    Money values serialise as their 2-dp string form (``"0.99"``,
    ``"0.20"``), matching the cells the emitted CSV carries and what the diff
    page rendered — one truth, three surfaces. A zero-drift confirm (no
    ``DIFFERS`` rows) yields ``"[]"``; PRD user story 9 still records the row.
    Rows whose ``Cost`` was blank (a ``FILLED`` row Books adds a cost to) are
    not drift — Loyverse had nothing there to differ from — so they do not
    appear here, even though they count toward ``PrepareResult.changed_count``.
    """
    entries = [
        {
            "sku": row.sku,
            "name": row.name,
            "loyverse_cost": str(row.loyverse_cost),
            "books_cost": str(row.books_cost),
        }
        for row in result.drift_rows
        if row.drift
    ]
    return json.dumps(entries)


# --- internals ---------------------------------------------------------------


def _parse(csv_text: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Split ``csv_text`` into a (header, data rows) pair.

    Strips a leading UTF-8 BOM (Excel prepends one when saving CSV; the upload
    route also decodes ``utf-8-sig``, but the engine is defensive — a BOM in
    the header would otherwise read as ``\\ufeffHandle`` and fail the
    required-column check for ``Handle``). Raises
    :class:`InvalidLoyverseExportError` if any required column is absent.
    """
    reader = csv.reader(io.StringIO(csv_text.lstrip(_BOM)))
    rows = list(reader)
    if not rows:
        raise InvalidLoyverseExportError(missing_columns=REQUIRED_COLUMNS)
    header = tuple(cell.strip() for cell in rows[0])
    missing = tuple(col for col in REQUIRED_COLUMNS if col not in header)
    if missing:
        raise InvalidLoyverseExportError(missing_columns=missing)
    data_rows = tuple(tuple(row) for row in rows[1:])
    return header, data_rows


def _cell(row: tuple[str, ...], header: tuple[str, ...], column: str) -> str:
    """One cell's value, stripped; ``""`` when the column is absent or the
    row is shorter than the header (an exporter that trimmed trailing
    blanks)."""
    index = header.index(column)
    return row[index].strip() if index < len(row) else ""


def _parse_money(text: str) -> Money | None:
    """Parse a Loyverse ``Cost`` cell into a Decimal, or None if malformed.

    Loyverse writes money as digits + point only (#72 §3); a stray symbol
    or thousands separator means the file is not what Books expected, so the
    row is treated as having no Loyverse cost (it will be overwritten on
    confirm by Books' number, which is the safe direction — Books is source
    of truth).
    """
    try:
        return Money(text.strip())
    except (ValueError, SyntaxError):
        return None


def _round_money(value: Money) -> Money:
    """Quantise to 2 dp with half-up rounding — ``0.125`` → ``0.13``."""
    return Money(value).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _format_money(value: Money) -> str:
    """Render a Books cost as Loyverse expects: digits + point, 2 dp.

    ``Decimal``'s str of a 2-dp value is already ``"0.20"`` / ``"123.45"`` —
    no symbol, no thousands separator, no trailing zeros beyond 2 dp. The
    quantise in :func:`_round_money` guarantees the 2-dp shape; this just
    stringifies.
    """
    return str(_round_money(value))


__all__ = [
    "COST_COLUMN",
    "REQUIRED_COLUMNS",
    "DriftStatus",
    "DriftRow",
    "PrepareResult",
    "InvalidLoyverseExportError",
    "prepare",
    "emit_filled_csv",
    "drift_payload_json",
]
