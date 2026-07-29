"""E2E: cash-spend entry + aggregation (Wave 3, issue #96, parent #82).

The cash-spend data-model spine — the row that produces the HTML's
"Cost of goods — purchases (cash)" line and the per-bucket breakdown
(taps / kitchen / coffee / bakery / staff / rent). This slice lands the
``cash_spend`` table, the pure engine, the typed store CRUD, and the
partner-facing admin surface at ``/admin/cash-spend``.

One E2E seam, mirroring ``test_period_review_e2e.py`` and the spend-buckets
admin tests: through ``seed_config`` + ``SqliteConfigStore`` + FastAPI's
``TestClient`` over the real SQLite store, against the public interfaces
(``cash_spend_for_period`` + ``SqliteConfigStore``'s cash-spend CRUD +
``/admin/cash-spend``). No reaching into internals.

The worked examples are the two grilling stress-tests plus the basics:

- Single-bucket bill, no VAT — aggregation matches amount.
- Multi-bucket Makro bill (stress-test 1): one date, one supplier, two
  rows (coffee + taps), both ``vat_inclusive=True`` — per-bucket net
  totals match ``amount / 1.07``, period total matches sum of nets, and
  ``SUM(amount) WHERE date+supplier`` reconstructs the gross invoice total.
- Wet-market non-VAT bill (stress-test 2): one row, ``vat_inclusive=False``,
  bucket=kitchen — aggregation matches amount with no division; the row
  carries no segment attribution (segment is downstream, ADR-0007).
- Edit a row's amount -> audit log records it -> revert restores it.
- Filter by date range and by bucket -> only matching rows.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tangerine.cash_spend import (
    CashSpendEntry,
    CashSpendForPeriod,
    cash_spend_for_period,
)
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

_TEST_PASSPHRASE = "cash-spend-test-passphrase"
_TEST_SIGNING_SECRET = "cash-spend-test-signing-secret"


def _recipes_yaml() -> str:
    return "recipes: []\n"


def _costs_yaml() -> str:
    return "costs: {}\n"


def _assignees_yaml() -> str:
    return """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


def _write_seed_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs.write_text(_costs_yaml(), encoding="utf-8")
    assignees.write_text(_assignees_yaml(), encoding="utf-8")
    return recipes, costs, assignees


def _seeded_store(tmp_path: Path) -> SqliteConfigStore:
    """An in-memory config store seeded with the six buckets + two suppliers.

    ``seed_config`` lands the spend-bucket seed; the two suppliers
    (Makro, a VAT-registered vendor; Wet market, a non-VAT vendor) are
    the FK targets the worked examples FK into.
    """
    recipes, costs, _ = _write_seed_files(tmp_path)
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)
    store = SqliteConfigStore(conn, now=lambda: "2026-07-15T02:00:00+00:00")
    store.create_supplier("makro", name="Makro Phuket", created_by="daniel")
    store.create_supplier(
        "wet-market", name="Local wet market", created_by="daniel"
    )
    return store


@pytest.fixture
def today() -> date:
    return date(2026, 7, 16)


# =============================================================================
# AC: pure engine — cash_spend_for_period over CashSpendEntry
# =============================================================================


def test_single_bucket_bill_no_vat_aggregation_matches_amount() -> None:
    """The basic shape: one row, no VAT, one bucket.

    A 500 THB kitchen purchase at the wet market (no VAT). The period
    total is 500 THB, the kitchen bucket carries 500 THB, every other
    bucket carries nothing. No day-apportionment — the full amount lands
    on its own date.
    """
    entry = CashSpendEntry(
        row_id=1,
        date=date(2026, 7, 10),
        supplier_id="wet-market",
        description="morning veg run",
        bucket_id="kitchen",
        amount=D("500"),
        vat_inclusive=False,
    )

    result = cash_spend_for_period(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        entries=[entry],
    )

    assert isinstance(result, CashSpendForPeriod)
    assert result.total == D("500")
    assert result.by_bucket == {"kitchen": D("500")}


def test_multi_bucket_makro_bill_divides_vat_rows_by_bucket() -> None:
    """Stress-test 1 from #82's grilling: the multi-bucket Makro bill.

    One tax invoice from Makro crosses two cost families — coffee beans
    (1,200 THB) and taps glassware (3,000 THB). Both are VAT-inclusive.
    The partner enters it as **two sibling rows** sharing date + supplier,
    differing bucket + amount (decision A: invoice total is derived, not
    stored).

    Aggregation:

    - per-bucket net = amount / 1.07 (decision B + ADR-0003 decision 4):
        coffee -> 1200 / 1.07 = 1121.495327... -> 1121.50 THB (2dp)
        taps   -> 3000 / 1.07 = 2803.738317... -> 2803.74 THB (2dp)
    - period total = sum of bucket nets = 3925.24 THB
    - gross invoice total = SUM(amount) WHERE date+supplier = 4200 THB
      (this is the derived fact the partner reconciles against the paper
      receipt; the storage shape is two independent rows, no parent).
    """
    coffee = CashSpendEntry(
        row_id=1,
        date=date(2026, 7, 10),
        supplier_id="makro",
        description="HoD beans + grounds",
        bucket_id="coffee",
        amount=D("1200"),
        vat_inclusive=True,
    )
    taps = CashSpendEntry(
        row_id=2,
        date=date(2026, 7, 10),
        supplier_id="makro",
        description="HoD glassware",
        bucket_id="taps",
        amount=D("3000"),
        vat_inclusive=True,
    )

    result = cash_spend_for_period(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        entries=[coffee, taps],
    )

    # Per-bucket net totals (amount / 1.07, quantised to 2dp).
    assert result.by_bucket["coffee"] == (D("1200") / D("1.07")).quantize(D("0.01"))
    assert result.by_bucket["taps"] == (D("3000") / D("1.07")).quantize(D("0.01"))
    # Period total is the sum of the nets (the COGS-side number).
    assert result.total == result.by_bucket["coffee"] + result.by_bucket["taps"]
    # The gross invoice total reconstructs from the raw amounts (the
    # cash-side number the partner ties to the paper receipt). This is
    # the derived fact decision A names; the period total is net-of-VAT.
    gross_invoice = coffee.amount + taps.amount
    assert gross_invoice == D("4200")
    assert gross_invoice > result.total  # the VAT portion sits above COGS


def test_wet_market_non_vat_bill_has_no_division_and_no_segment() -> None:
    """Stress-test 2 from #82's grilling: the wet-market non-VAT bill.

    A 350 THB kitchen purchase at the wet market. ``vat_inclusive=False``
    so the amount lands on the bucket as-is (no / 1.07). Critically: the
    entry carries **no segment attribution** — segment is a downstream
    P&L computation against recipes (ADR-0007 pure-clock segmentation),
    not a fact of the purchase (decision D). The entry's dataclass has no
    segment field at all.
    """
    entry = CashSpendEntry(
        row_id=1,
        date=date(2026, 7, 11),
        supplier_id="wet-market",
        description="veg + herbs",
        bucket_id="kitchen",
        amount=D("350"),
        vat_inclusive=False,
    )

    result = cash_spend_for_period(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        entries=[entry],
    )

    assert result.total == D("350")  # no division
    assert result.by_bucket == {"kitchen": D("350")}
    # Segment is downstream: the entry shape has no segment field. The
    # only way to assert "carries no segment attribution" against a
    # frozen dataclass is to confirm the field does not exist.
    assert not hasattr(entry, "segment")


def test_rows_outside_the_period_window_are_excluded_entirely() -> None:
    """No day-apportionment (decision C reason 1): a row outside [start, end]
    is excluded, not fractionally counted.

    This is the key difference from ``fixed_costs_for_period``: a fixed
    cost is day-apportioned for sub-month periods because it belongs to
    the month; a cash-spend row belongs to its own date, so it either
    lands whole or stays out.
    """
    inside = CashSpendEntry(
        row_id=1,
        date=date(2026, 7, 15),
        supplier_id="makro",
        description="inside",
        bucket_id="coffee",
        amount=D("100"),
        vat_inclusive=False,
    )
    before = CashSpendEntry(
        row_id=2,
        date=date(2026, 6, 30),
        supplier_id="makro",
        description="before",
        bucket_id="coffee",
        amount=D("999"),
        vat_inclusive=False,
    )
    after = CashSpendEntry(
        row_id=3,
        date=date(2026, 8, 1),
        supplier_id="makro",
        description="after",
        bucket_id="coffee",
        amount=D("999"),
        vat_inclusive=False,
    )

    result = cash_spend_for_period(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        entries=[inside, before, after],
    )

    assert result.total == D("100")  # only the inside row counted, whole


# =============================================================================
# AC: SqliteConfigStore exposes typed CRUD for cash-spend rows
# =============================================================================


def _makro_coffee_entry(
    *,
    row_id: int = 1,
    amount: str = "1200",
    vat_inclusive: bool = True,
) -> CashSpendEntry:
    return CashSpendEntry(
        row_id=row_id,
        date=date(2026, 7, 10),
        supplier_id="makro",
        description="HoD beans",
        bucket_id="coffee",
        amount=D(amount),
        vat_inclusive=vat_inclusive,
    )


def test_create_cash_spend_row_writes_row_and_audit_entry(tmp_path: Path) -> None:
    """A created cash-spend row lands in the table and in ``audit_log``.

    The store is the seam: ``create_cash_spend`` writes one row plus one
    audit entry recording the creation (``old=None``, ``new`` = whole
    row, ``table_name='cash_spend'``). Reverts of that entry restore the
    pre-creation state (the row disappears), exactly like a created SKU.
    """
    store = _seeded_store(tmp_path)
    store.create_cash_spend(_makro_coffee_entry(), created_by="daniel")

    rows = store.cash_spend_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.supplier_id == "makro"
    assert row.bucket_id == "coffee"
    assert row.amount == D("1200")
    assert row.vat_inclusive is True
    assert row.description == "HoD beans"

    (audit,) = [
        e for e in store.audit_entries() if e.table_name == "cash_spend"
    ]
    assert audit.pk == str(row.row_id)
    assert audit.old_value is None  # creation
    assert audit.new_value["bucket_id"] == "coffee"
    assert audit.changed_by == "daniel"


def test_update_cash_spend_row_records_the_change_and_revert_restores(
    tmp_path: Path,
) -> None:
    """Edit a row's amount -> audit log records it -> revert restores it.

    The audit safety net (ADR-0003 decision 2): every config edit is
    revertable through the existing /audit machinery, with no new revert
    code. An edit's entry has both old + new snapshots, so reverting it
    surgically restores only the fields it moved (the amount), leaving a
    later edit to a different field intact.
    """
    store = _seeded_store(tmp_path)
    store.create_cash_spend(_makro_coffee_entry(amount="1200"), created_by="daniel")
    original = store.cash_spend_rows()[0]

    # Edit: amount typo, 1200 should have been 1500.
    edited = CashSpendEntry(
        row_id=original.row_id,
        date=original.date,
        supplier_id=original.supplier_id,
        description=original.description,
        bucket_id=original.bucket_id,
        amount=D("1500"),
        vat_inclusive=original.vat_inclusive,
    )
    store.update_cash_spend(edited, updated_by="daniel")

    after_edit = store.cash_spend_rows()[0]
    assert after_edit.amount == D("1500")

    # The edit is the newest cash_spend entry; revert it.
    edit_entry = next(
        e
        for e in store.audit_entries()
        if e.table_name == "cash_spend" and e.old_value is not None
    )
    store.revert_entry(edit_entry.entry_id, changed_by="daniel")

    after_revert = store.cash_spend_rows()[0]
    assert after_revert.amount == D("1200")  # amount restored
    # Everything else survived the surgical revert.
    assert after_revert.bucket_id == "coffee"
    assert after_revert.supplier_id == "makro"


def test_delete_cash_spend_row_removes_it_and_revert_restores(
    tmp_path: Path,
) -> None:
    """Deleting a duplicate row removes it; revert restores the row whole."""
    store = _seeded_store(tmp_path)
    store.create_cash_spend(_makro_coffee_entry(), created_by="daniel")
    row = store.cash_spend_rows()[0]

    store.delete_cash_spend(row.row_id, deleted_by="daniel")
    assert store.cash_spend_rows() == []

    delete_entry = next(
        e
        for e in store.audit_entries()
        if e.table_name == "cash_spend" and e.new_value is None
    )
    store.revert_entry(delete_entry.entry_id, changed_by="daniel")

    restored = store.cash_spend_rows()
    assert len(restored) == 1
    assert restored[0].amount == D("1200")
    assert restored[0].bucket_id == "coffee"


def test_cash_spend_aggregates_from_store_rows_over_a_period(
    tmp_path: Path,
) -> None:
    """The store + engine compose: store rows feed ``cash_spend_for_period``.

    This pins the seam the /admin surface and the future P&L view will
    both use: read rows from the store, hand them to the pure engine.
    """
    store = _seeded_store(tmp_path)
    store.create_cash_spend(
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 10),
            supplier_id="makro",
            description="beans",
            bucket_id="coffee",
            amount=D("1200"),
            vat_inclusive=True,
        ),
        created_by="daniel",
    )
    store.create_cash_spend(
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 10),
            supplier_id="makro",
            description="glassware",
            bucket_id="taps",
            amount=D("3000"),
            vat_inclusive=True,
        ),
        created_by="daniel",
    )

    result = cash_spend_for_period(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        entries=store.cash_spend_rows(),
    )

    assert result.by_bucket["coffee"] == (D("1200") / D("1.07")).quantize(D("0.01"))
    assert result.by_bucket["taps"] == (D("3000") / D("1.07")).quantize(D("0.01"))
    assert result.total == result.by_bucket["coffee"] + result.by_bucket["taps"]


# =============================================================================
# AC: /admin/cash-spend — filterable list, create, edit, delete, behind auth
# =============================================================================


def _build_app(tmp_path: Path, *, today: date):
    """App factory over a seeded SQLite DB (the Wave 1 UI-seam pattern)."""
    recipes, costs, assignees = _write_seed_files(tmp_path)
    db_path = str(tmp_path / "tangerine.db")
    store = SqliteLoyverseStore.connect(db_path)
    store.close()
    app = create_app(
        db_path=db_path,
        recipes_path=str(recipes),
        costs_path=str(costs),
        assignees_path=str(assignees),
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )
    # Seed the two suppliers + a couple of cash-spend rows so the list
    # has something to render and filter against.
    cfg = app.state.config_store
    cfg.create_supplier("makro", name="Makro Phuket", created_by="migration")
    cfg.create_supplier(
        "wet-market", name="Local wet market", created_by="migration"
    )
    cfg.create_cash_spend(
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 10),
            supplier_id="makro",
            description="HoD beans",
            bucket_id="coffee",
            amount=D("1200"),
            vat_inclusive=True,
        ),
        created_by="migration",
    )
    cfg.create_cash_spend(
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 11),
            supplier_id="wet-market",
            description="veg run",
            bucket_id="kitchen",
            amount=D("350"),
            vat_inclusive=False,
        ),
        created_by="migration",
    )
    return app


def _authed_client(app) -> TestClient:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a session cookie"
    return client


def test_admin_page_requires_auth(tmp_path: Path, today: date) -> None:
    """An unauthenticated request is redirected to ``/login``."""
    app = _build_app(tmp_path, today=today)
    client = TestClient(app)

    response = client.get("/admin/cash-spend", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "/login" in response.headers.get("location", "")


def test_admin_page_lists_rows_and_their_buckets(tmp_path: Path, today: date) -> None:
    """``/admin/cash-spend`` renders the seeded rows behind auth."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/admin/cash-spend")

    assert response.status_code == 200
    listing = response.text.split("<!--section:cash-spend-list-->")[1].split(
        "<!--/section:cash-spend-list-->"
    )[0]
    assert "HoD beans" in listing
    assert "coffee" in listing
    assert "veg run" in listing
    assert "kitchen" in listing


def test_filter_by_bucket_shows_only_matching_rows(
    tmp_path: Path, today: date
) -> None:
    """The bucket filter narrows the list to one bucket's rows."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/admin/cash-spend?bucket=coffee")

    assert response.status_code == 200
    listing = response.text.split("<!--section:cash-spend-list-->")[1].split(
        "<!--/section:cash-spend-list-->"
    )[0]
    assert "HoD beans" in listing  # coffee row shown
    assert "veg run" not in listing  # kitchen row filtered out


def test_filter_by_date_range_shows_only_matching_rows(
    tmp_path: Path, today: date
) -> None:
    """The date-range filter narrows the list to rows inside [start, end]."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    # 2026-07-11..2026-07-31 -> only the kitchen row (on the 11th) shows;
    # the coffee row on the 10th falls outside.
    response = client.get(
        "/admin/cash-spend?start=2026-07-11&end=2026-07-31"
    )

    assert response.status_code == 200
    listing = response.text.split("<!--section:cash-spend-list-->")[1].split(
        "<!--/section:cash-spend-list-->"
    )[0]
    assert "veg run" in listing  # 11 Jul row shown
    assert "HoD beans" not in listing  # 10 Jul row filtered out


def test_create_form_adds_a_row_and_audit_logs_it(
    tmp_path: Path, today: date
) -> None:
    """A partner-added row lands in the list and in the audit log."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post(
        "/admin/cash-spend",
        data={
            "entry_date": "2026-07-12",
            "supplier_id": "makro",
            "description": "espresso machine cleaner",
            "bucket_id": "coffee",
            "amount": "240",
            "vat_inclusive": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    listing = client.get("/admin/cash-spend").text.split(
        "<!--section:cash-spend-list-->"
    )[1].split("<!--/section:cash-spend-list-->")[0]
    assert "espresso machine cleaner" in listing
    audit = client.get("/audit").text
    assert "cash_spend" in audit


def test_edit_a_row_amount_then_audit_and_revert_round_trip(
    tmp_path: Path, today: date
) -> None:
    """The /audit safety net works for cash-spend edits: edit, see it,
    revert it, see the row restored."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    row = cfg.cash_spend_rows()[0]  # the seeded HoD beans row (1200 THB)
    assert row.amount == D("1200")

    # Edit the amount to 1500 (typo fix).
    edited = client.post(
        f"/admin/cash-spend/{row.row_id}/edit",
        data={
            "entry_date": row.date.isoformat(),
            "supplier_id": row.supplier_id,
            "description": row.description,
            "bucket_id": row.bucket_id,
            "amount": "1500",
            "vat_inclusive": "on",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    after_edit = cfg.cash_spend_rows()[0]
    assert after_edit.amount == D("1500")

    # The edit landed in /audit.
    audit_html = client.get("/audit").text
    assert "cash_spend" in audit_html

    # Revert the newest cash_spend entry (the edit) via /audit.
    edit_entry = next(
        e
        for e in cfg.audit_entries()
        if e.table_name == "cash_spend" and e.old_value is not None
    )
    reverted = client.post(
        f"/audit/{edit_entry.entry_id}/revert", follow_redirects=False
    )
    assert reverted.status_code == 303

    after_revert = cfg.cash_spend_rows()[0]
    assert after_revert.amount == D("1200")  # amount restored


def test_delete_a_row_via_route_then_audit_shows_it(
    tmp_path: Path, today: date
) -> None:
    """DEL on a row removes it; the deletion lands in the audit log."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    row = cfg.cash_spend_rows()[0]

    deleted = client.post(
        f"/admin/cash-spend/{row.row_id}/delete", follow_redirects=False
    )
    assert deleted.status_code == 303
    assert all(r.row_id != row.row_id for r in cfg.cash_spend_rows())

    audit = client.get("/audit").text
    assert "cash_spend" in audit


# =============================================================================
# AC: retired buckets are not offered in the new-entry picker
# =============================================================================


def test_retired_buckets_are_not_offered_in_the_create_picker(
    tmp_path: Path, today: date
) -> None:
    """A retired bucket stays in the spend-buckets list (history) but is
    excluded from the cash-spend create form's bucket picker."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    # Retire the 'taps' bucket via the spend-buckets store.
    cfg.retire_spend_bucket(
        "taps", retired_at=today.isoformat(), updated_by="daniel"
    )

    response = client.get("/admin/cash-spend")

    picker = response.text.split("<!--section:cash-spend-form-->")[1].split(
        "<!--/section:cash-spend-form-->"
    )[0]
    # Live buckets appear in the picker.
    assert 'value="coffee"' in picker
    assert 'value="kitchen"' in picker
    # The retired bucket is excluded.
    assert 'value="taps"' not in picker
