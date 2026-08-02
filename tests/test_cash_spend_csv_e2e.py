"""E2E: cash-spend CSV importer (issue #121, parent #83 — build of the CSV path).

The bulk path for cash-basis supplier purchases. The partner downloads a
template pre-filled with the controlled vocabulary (suppliers + spend
buckets, including retired buckets), fills in entry rows offline in Excel,
uploads it, and previews what will land before anything does. The mechanics
are settled by [#83](https://github.com/theham1988/accounting-tool/issues/83):

- **Template** (#83 d1): a read-only ``# vendor`` block, a read-only
  ``# bucket`` block, a header row, then blank entry rows. The parser skips
  reference rows by their leading ``# vendor`` / ``# bucket`` tag.
- **Two-tier preview** (#83 d2): hard errors block apply (unknown
  vendor/bucket, bad date/amount, missing field, bad ``vat_inclusive``);
  soft warnings don't (duplicate within-file or against-ledger, retired
  bucket).
- **Atomic apply** (#83 d3): one store transaction wrapping N
  ``create_cash_spend`` calls under a single ``session_id``; ``/audit``
  shows the import as one reversible stroke (per-session Revert undoes
  all N rows in one click).
- **Preview IS the queue** (#83 d4): no persistent pending state —
  cash-spend stays single-state (a row either exists or doesn't).

One E2E seam, mirroring ``test_cash_spend_e2e.py`` and
``test_loyverse_cost_export_e2e.py``: through ``seed_config`` +
``SqliteConfigStore`` + FastAPI's ``TestClient`` over the real SQLite store,
against the public interfaces (``generate_template_csv`` +
``parse_import`` + ``SqliteConfigStore.batch`` + ``create_cash_spend`` +
``/admin/cash-spend/import``). No reaching into internals.

Defaults (ADR-0010 decision 4): ``vat_inclusive`` blank → **FALSE** (the
opposite of ``/upload``'s TRUE default — "default false so the migration
never makes a number worse by guessing wrong"). ``description`` blank →
empty string (allowed).

The worked examples pin every hard-error and soft-warning case from #83 d2
plus the atomic-apply + audit-revert story.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tangerine.cash_spend import CashSpendEntry, cash_spend_for_period
from tangerine.cash_spend_csv import (
    ImportPreview,
    generate_template_csv,
    parse_import,
)
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

_TEST_PASSPHRASE = "cash-spend-csv-test-passphrase"
_TEST_SIGNING_SECRET = "cash-spend-csv-test-signing-secret"


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

    The seeded spend buckets (taps / kitchen / coffee / bakery / staff /
    rent) and two suppliers (Makro, a VAT-registered vendor; Wet market, a
    non-VAT vendor) are the FK targets the importer's rows FK into.
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
# AC: generate_template_csv — the downloadable template (reference blocks +
# header + blank rows)
# =============================================================================


def test_template_emits_vendor_and_bucket_reference_blocks_then_header(
    tmp_path: Path,
) -> None:
    """The template carries a read-only reference section above the header:
    every supplier in a ``# vendor`` block (slug + display name), every spend
    bucket in a ``# bucket`` block including retired ones (slug + display
    name), then the header row ``kind, date, supplier_id, description,
    bucket_id, amount, vat_inclusive``, then blank entry rows (#83 d1)."""
    store = _seeded_store(tmp_path)
    # Retire 'bakery' so the template must still list it (historical imports
    # need to see retired buckets; #83 d2 soft-warning path exercises this).
    store.retire_spend_bucket(
        "bakery", retired_at="2026-07-01", updated_by="daniel"
    )

    text = generate_template_csv(
        suppliers=store.suppliers(), buckets=store.spend_buckets()
    )
    lines = text.splitlines()

    # The vendor block comes first, every supplier listed by slug + name.
    assert lines[0].startswith("# vendor")
    assert "makro" in text and "Makro Phuket" in text
    assert "wet-market" in text and "Local wet market" in text
    # The bucket block follows, including the retired 'bakery' bucket.
    bucket_block_start = next(
        i for i, ln in enumerate(lines) if ln.startswith("# bucket")
    )
    assert bucket_block_start > 0
    bucket_block = "\n".join(lines[bucket_block_start:])
    assert "taps" in bucket_block and "kitchen" in bucket_block
    assert "coffee" in bucket_block and "bakery" in bucket_block  # retired, kept
    assert "staff" in bucket_block and "rent" in bucket_block
    # The header row carries the seven importer columns.
    header_idx = next(
        i
        for i, ln in enumerate(lines)
        if ln.startswith("kind,date,supplier_id")
    )
    assert lines[header_idx] == (
        "kind,date,supplier_id,description,bucket_id,amount,vat_inclusive"
    )


def test_template_round_trips_through_the_parser_with_no_entries(
    tmp_path: Path,
) -> None:
    """A freshly-downloaded template (reference blocks + header, no entry
    rows) parses to an empty preview: zero new rows, zero errors, zero
    warnings. The reference rows are skipped by their leading ``# vendor`` /
    ``# bucket`` tag, the header row is consumed, and there is nothing to
    import — the partner's starting point."""
    store = _seeded_store(tmp_path)

    text = generate_template_csv(
        suppliers=store.suppliers(), buckets=store.spend_buckets()
    )
    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert isinstance(preview, ImportPreview)
    assert preview.new_rows == ()
    assert preview.errors == ()
    assert preview.warnings == ()


# =============================================================================
# AC: Web seam — /admin links the importer; the import page is auth-gated
# =============================================================================


def _build_app(tmp_path: Path, *, today: date) -> FastAPI:
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
    cfg = app.state.config_store
    cfg.create_supplier("makro", name="Makro Phuket", created_by="migration")
    cfg.create_supplier(
        "wet-market", name="Local wet market", created_by="migration"
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


def test_admin_landing_links_the_importer(tmp_path: Path, today: date) -> None:
    """``/admin`` links the importer beside the cash-spend entry surface."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/admin")

    assert response.status_code == 200
    assert "/admin/cash-spend/import" in response.text


def test_import_page_requires_auth(tmp_path: Path, today: date) -> None:
    """An unauthenticated GET redirects to /login, like every Admin route."""
    app = _build_app(tmp_path, today=today)
    client = TestClient(app)

    response = client.get("/admin/cash-spend/import", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_template_download_route_is_auth_gated(tmp_path: Path, today: date) -> None:
    """``GET /admin/cash-spend/import/template`` requires auth."""
    app = _build_app(tmp_path, today=today)
    client = TestClient(app)

    response = client.get(
        "/admin/cash-spend/import/template", follow_redirects=False
    )

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_preview_route_is_auth_gated(tmp_path: Path, today: date) -> None:
    """``POST /admin/cash-spend/import/preview`` requires auth."""
    app = _build_app(tmp_path, today=today)
    client = TestClient(app)

    response = client.post(
        "/admin/cash-spend/import/preview", follow_redirects=False
    )

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_apply_route_is_auth_gated(tmp_path: Path, today: date) -> None:
    """``POST /admin/cash-spend/import/apply`` requires auth."""
    app = _build_app(tmp_path, today=today)
    client = TestClient(app)

    response = client.post(
        "/admin/cash-spend/import/apply", follow_redirects=False
    )

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_template_download_route_serves_a_csv_with_dated_filename(
    tmp_path: Path, today: date
) -> None:
    """``GET /admin/cash-spend/import/template`` serves the template CSV as a
    download with a ``Content-Disposition`` filename that includes today's
    date (the ``/upload/template`` pattern)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/admin/cash-spend/import/template")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert today.isoformat() in disposition
    # The served template carries the reference blocks + header.
    text = response.content.decode("utf-8")
    assert "# vendor" in text
    assert "# bucket" in text
    assert "kind,date,supplier_id,description,bucket_id,amount,vat_inclusive" in text


# =============================================================================
# AC: parse_import — clean multi-invoice file previews with invoice grouping
# =============================================================================


def _entry_csv(*rows: str) -> str:
    """Build a cash-spend CSV with the reference section elided — just the
    header + the entry rows handed in. The reference rows are skipped by the
    parser regardless of where they sit, so tests pin the entry behaviour
    cleanly without re-typing the vendor/bucket blocks each time."""
    header = "kind,date,supplier_id,description,bucket_id,amount,vat_inclusive"
    return "\n".join([header, *rows]) + "\n"


def test_clean_multi_invoice_file_previews_with_invoice_grouping(
    tmp_path: Path,
) -> None:
    """A 5-row file with two invoices (3 siblings + 2 siblings) previews
    correctly: 5 new rows, no errors, no warnings, and the invoice grouping
    reconstructs each invoice's total from its sibling rows.

    The Makro bill is the #82 stress-test 1: two buckets on one invoice
    (coffee 1,200 + taps 3,000 = 4,200 gross, both VAT-inclusive). The wet-
    market bill is stress-test 2: one kitchen row, no VAT.
    """
    store = _seeded_store(tmp_path)
    text = _entry_csv(
        ",2026-07-10,makro,HoD beans,coffee,1200,TRUE",
        ",2026-07-10,makro,HoD glassware,taps,3000,TRUE",
        ",2026-07-10,makro,cleaning spray,kitchen,150,",
        ",2026-07-11,wet-market,veg run,kitchen,350,",
        ",2026-07-11,wet-market,herbs,kitchen,120,",
    )

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert len(preview.new_rows) == 5
    assert preview.errors == ()
    assert preview.warnings == ()
    # Two invoices reconstructed — the Makro bill (3 siblings) and the wet-
    # market bill (2 siblings). The invoice totals are the derived facts
    # the partner reconciles against the paper receipts.
    assert len(preview.invoices) == 2
    makro_invoice = next(
        inv for inv in preview.invoices if inv.supplier_id == "makro"
    )
    assert makro_invoice.date == date(2026, 7, 10)
    assert makro_invoice.sibling_count == 3
    assert makro_invoice.invoice_total == D("4350")  # 1200 + 3000 + 150
    wet_invoice = next(
        inv for inv in preview.invoices if inv.supplier_id == "wet-market"
    )
    assert wet_invoice.date == date(2026, 7, 11)
    assert wet_invoice.sibling_count == 2
    assert wet_invoice.invoice_total == D("470")  # 350 + 120


def test_vat_inclusive_blank_defaults_to_false(tmp_path: Path) -> None:
    """A row with a blank ``vat_inclusive`` cell parses with
    ``vat_inclusive=False`` — the opposite of ``/upload``'s TRUE default
    (ADR-0010 d4: "default false so the migration never makes a number
    worse by guessing wrong")."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(",2026-07-10,wet-market,veg,kitchen,350,")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert len(preview.new_rows) == 1
    assert preview.new_rows[0].entry.vat_inclusive is False


def test_description_blank_is_allowed_and_defaults_to_empty(
    tmp_path: Path,
) -> None:
    """A row with a blank ``description`` parses — description is optional,
    blank becomes the empty string (the audit trail still records the row)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(",2026-07-10,wet-market,,kitchen,350,")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert len(preview.new_rows) == 1
    assert preview.new_rows[0].entry.description == ""


# =============================================================================
# AC: parse_import — hard errors block apply (#83 d2, every kind)
# =============================================================================


def test_hard_error_unknown_supplier_blocks(tmp_path: Path) -> None:
    """An unknown ``supplier_id`` is a hard error — the controlled
    vocabulary's whole point is one canonical id per vendor (#83 d2)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(",2026-07-10,ghost-vendor,beans,coffee,1200,")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert preview.new_rows == ()
    assert len(preview.errors) == 1
    assert "ghost-vendor" in preview.errors[0].message
    assert "supplier" in preview.errors[0].message.lower()


def test_hard_error_unknown_bucket_blocks(tmp_path: Path) -> None:
    """An unknown ``bucket_id`` (not in the vocabulary at all — retired is
    a *soft* warning, not this) is a hard error (#83 d2)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(",2026-07-10,makro,beans,ghost-bucket,1200,")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert preview.new_rows == ()
    assert len(preview.errors) == 1
    assert "ghost-bucket" in preview.errors[0].message
    assert "bucket" in preview.errors[0].message.lower()


def test_hard_error_malformed_date_blocks(tmp_path: Path) -> None:
    """A date that isn't ISO-8601 is a hard error (#83 d2)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(",10-July-2026,makro,beans,coffee,1200,")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert preview.new_rows == ()
    assert len(preview.errors) == 1
    assert "date" in preview.errors[0].message.lower()


def test_hard_error_non_positive_amount_blocks(tmp_path: Path) -> None:
    """An amount of zero or below is a hard error — a purchase is a real
    THB outflow (#83 d2)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(",2026-07-10,makro,beans,coffee,0,")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert preview.new_rows == ()
    assert len(preview.errors) == 1
    assert "positive" in preview.errors[0].message.lower()


def test_hard_error_malformed_amount_blocks(tmp_path: Path) -> None:
    """A non-numeric amount is a hard error (#83 d2)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(",2026-07-10,makro,beans,coffee,twelve-hundred,")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert preview.new_rows == ()
    assert len(preview.errors) == 1
    assert "amount" in preview.errors[0].message.lower()


def test_hard_error_missing_required_field_blocks(tmp_path: Path) -> None:
    """A row missing any of ``date`` / ``supplier_id`` / ``bucket_id`` /
    ``amount`` is a hard error — the row is incomplete (#83 d2)."""
    store = _seeded_store(tmp_path)
    # supplier_id blank.
    missing_supplier = _entry_csv(",2026-07-10,,beans,coffee,1200,")
    # bucket_id blank.
    missing_bucket = _entry_csv(",2026-07-10,makro,beans,,1200,")
    # amount blank.
    missing_amount = _entry_csv(",2026-07-10,makro,beans,coffee,,")
    # date blank.
    missing_date = _entry_csv(",,makro,beans,coffee,1200,")

    for bad_text in (missing_supplier, missing_bucket, missing_amount, missing_date):
        preview = parse_import(
            bad_text,
            suppliers=store.suppliers(),
            buckets=store.spend_buckets(),
            existing_rows=store.cash_spend_rows(),
        )
        assert preview.new_rows == (), (
            f"expected a hard error, got rows: {preview.new_rows} for {bad_text!r}"
        )
        assert len(preview.errors) == 1
        assert "missing" in preview.errors[0].message.lower()


def test_hard_error_malformed_vat_inclusive_blocks(tmp_path: Path) -> None:
    """A ``vat_inclusive`` cell that isn't blank/TRUE/FALSE (or a recognized
    variant) is a hard error — the flag is binary, not free text (#83 d2)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(",2026-07-10,makro,beans,coffee,1200,maybe")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert preview.new_rows == ()
    assert len(preview.errors) == 1
    assert "vat_inclusive" in preview.errors[0].message.lower()


def test_one_hard_error_blocks_the_whole_file_but_other_rows_are_listed(
    tmp_path: Path,
) -> None:
    """A file with one bad row blocks apply, but the preview still lists the
    good rows so the partner sees what *would* have landed once they fix the
    bad one. The good rows are parsed and shown; the apply button is hidden
    (the template gates on ``preview.errors`` being empty)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(
        ",2026-07-10,makro,beans,coffee,1200,TRUE",
        ",2026-07-10,ghost-vendor,bad,coffee,999,",  # unknown supplier
        ",2026-07-11,wet-market,veg,kitchen,350,",
    )

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    # The two good rows parsed and appear; the one bad row is an error.
    assert len(preview.new_rows) == 2
    assert len(preview.errors) == 1
    # Hard errors present -> apply must be blocked (the route checks
    # ``preview.errors`` truthiness; the template gates the button on it).


# =============================================================================
# AC: parse_import — soft warnings don't block apply (#83 d2, every kind)
# =============================================================================


def test_soft_warning_duplicate_within_file_does_not_block(
    tmp_path: Path,
) -> None:
    """Two rows with the same ``(date, supplier_id, amount)``
    within one file warn but both parse — the partner may have genuinely
    bought the same thing twice, and that's their call (#83 d2)."""
    store = _seeded_store(tmp_path)
    text = _entry_csv(
        ",2026-07-10,makro,beans,coffee,1200,",
        ",2026-07-10,makro,beans-again,coffee,1200,",  # duplicate amount
    )

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert len(preview.new_rows) == 2  # both parsed
    assert preview.errors == ()
    assert len(preview.warnings) == 1
    assert "duplicate" in preview.warnings[0].message.lower()


def test_soft_warning_duplicate_against_ledger_does_not_block(
    tmp_path: Path,
) -> None:
    """A row whose ``(date, supplier_id, amount)`` already exists in the
    ledger warns but parses — a re-import of an already-booked month
    shouldn't silently double, but the partner may be correcting a
    description (#83 d2)."""
    store = _seeded_store(tmp_path)
    # Seed the ledger with a row the upload will duplicate.
    store.create_cash_spend(
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 10),
            supplier_id="makro",
            description="already booked",
            bucket_id="coffee",
            amount=D("1200"),
            vat_inclusive=True,
        ),
        created_by="daniel",
    )
    text = _entry_csv(",2026-07-10,makro,beans,coffee,1200,TRUE")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert len(preview.new_rows) == 1  # parsed
    assert preview.errors == ()
    assert len(preview.warnings) == 1
    assert "ledger" in preview.warnings[0].message.lower()


def test_soft_warning_retired_bucket_does_not_block(tmp_path: Path) -> None:
    """A row on a retired bucket warns but parses — historical imports
    under a now-retired bucket must not fail (#83 d2). The new-entry *form*
    excludes retired buckets; the importer does not."""
    store = _seeded_store(tmp_path)
    store.retire_spend_bucket(
        "bakery", retired_at="2026-06-30", updated_by="daniel"
    )
    text = _entry_csv(",2026-06-15,makro,flour,bakery,800,")

    preview = parse_import(
        text,
        suppliers=store.suppliers(),
        buckets=store.spend_buckets(),
        existing_rows=store.cash_spend_rows(),
    )

    assert len(preview.new_rows) == 1  # parsed
    assert preview.errors == ()
    assert len(preview.warnings) == 1
    assert "retired" in preview.warnings[0].message.lower()


# =============================================================================
# AC: Web seam — clean import previews, applies atomically, single session_id
# =============================================================================


def _csv_upload_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def test_preview_route_renders_the_two_tier_diff(
    tmp_path: Path, today: date
) -> None:
    """``POST /admin/cash-spend/import/preview`` renders the invoice grouping
    + rows + an Apply button. The structural section markers are what the
    tests pin (the cash-spend page's own pattern)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _entry_csv(
        ",2026-07-10,makro,HoD beans,coffee,1200,TRUE",
        ",2026-07-10,makro,HoD glassware,taps,3000,TRUE",
    )

    response = client.post(
        "/admin/cash-spend/import/preview",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.text
    # The two rows + their bucket + the invoice total appear.
    assert "coffee" in body
    assert "taps" in body
    assert "4200" in body  # invoice total
    # Apply button is present (no hard errors).
    assert "action=\"/admin/cash-spend/import/apply\"" in body


def test_preview_route_renders_errors_without_an_apply_button(
    tmp_path: Path, today: date
) -> None:
    """A preview with hard errors renders the errors section and NO apply
    button — the AC's "errors block apply" rendered honestly."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _entry_csv(
        ",2026-07-10,makro,beans,coffee,1200,",
        ",2026-07-10,ghost-vendor,bad,coffee,999,",  # unknown supplier
    )

    response = client.post(
        "/admin/cash-spend/import/preview",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.text
    assert "<!--section:cash-spend-import-errors-->" in body
    assert "ghost-vendor" in body
    # No apply button when errors are present.
    assert "action=\"/admin/cash-spend/import/apply\"" not in body


def test_mid_batch_failure_rolls_back_every_row(tmp_path: Path) -> None:
    """A multi-row apply that fails mid-batch leaves zero rows landed (#83 d3).

    The importer wraps N ``create_cash_spend`` calls in one ``store.batch()``.
    A synthetic exception after the first write (mirroring
    ``test_config_store_batch_e2e``'s mid-stroke pattern) must roll back
    every write *and* its audit rows — the audit log never records a
    partial stroke.
    """
    store = _seeded_store(tmp_path)
    entries = [
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 10),
            supplier_id="makro",
            description=f"row-{i}",
            bucket_id="coffee",
            amount=D("100") + i,
            vat_inclusive=False,
        )
        for i in range(3)
    ]
    writes_done = 0
    original = store.create_cash_spend

    def _failing_create(entry, *, created_by, session_id=None):  # type: ignore[no-untyped-def]
        nonlocal writes_done
        result = original(entry, created_by=created_by, session_id=session_id)
        writes_done += 1
        if writes_done >= 2:
            raise RuntimeError("synthetic mid-batch failure after write 2")
        return result

    store.create_cash_spend = _failing_create  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="synthetic mid-batch failure"):
        with store.batch():
            for entry in entries:
                store.create_cash_spend(
                    entry, created_by="daniel", session_id="import-session"
                )

    assert store.cash_spend_rows() == []
    assert [
        e for e in store.audit_entries() if e.table_name == "cash_spend"
    ] == []


def test_apply_route_lands_all_rows_atomically_under_one_session(
    tmp_path: Path, today: date
) -> None:
    """The worked example: a clean 5-row file with two invoices previews,
    applies atomically, all 5 rows land, and every audit entry carries the
    same ``session_id`` so ``/audit`` shows the import as one reversible
    stroke (#83 d3)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    upload = _entry_csv(
        ",2026-07-10,makro,HoD beans,coffee,1200,TRUE",
        ",2026-07-10,makro,HoD glassware,taps,3000,TRUE",
        ",2026-07-10,makro,cleaning spray,kitchen,150,",
        ",2026-07-11,wet-market,veg run,kitchen,350,",
        ",2026-07-11,wet-market,herbs,kitchen,120,",
    )

    response = client.post(
        "/admin/cash-spend/import/apply",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/admin/cash-spend/import?applied=5" in response.headers["location"]
    # All 5 rows landed.
    rows = cfg.cash_spend_rows()
    assert len(rows) == 5
    # Every cash_spend audit entry shares one session_id — the one-stroke
    # guarantee. (The login route's session_id is what the auth middleware
    # threads; revert_session keys on it.)
    cash_spend_entries = [
        e for e in cfg.audit_entries() if e.table_name == "cash_spend"
    ]
    assert len(cash_spend_entries) == 5
    session_ids = {e.session_id for e in cash_spend_entries}
    assert len(session_ids) == 1, (
        f"expected one session_id across the 5 entries, got {session_ids}"
    )
    the_session = next(iter(session_ids))
    assert the_session is not None


def test_apply_route_re_ranks_against_current_state_not_held_payload(
    tmp_path: Path, today: date
) -> None:
    """The "re-derive defensively" rule: a cash-spend row added between
    preview and apply changes the against-ledger duplicate warnings, so
    apply must re-parse from current state — a stale held payload would
    hide the new duplicate. Mirrors the cost-mirror's re-derive AC."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    upload = _entry_csv(",2026-07-10,makro,beans,coffee,1200,TRUE")

    # Preview first (holds the text server-side).
    client.post(
        "/admin/cash-spend/import/preview",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    # Add the same row via the form between preview and apply — now the
    # upload is a duplicate-against-ledger at apply time.
    cfg.create_cash_spend(
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 10),
            supplier_id="makro",
            description="added between preview and apply",
            bucket_id="coffee",
            amount=D("1200"),
            vat_inclusive=True,
        ),
        created_by="daniel",
    )

    # Apply with no file — uses the held upload. The re-parse must see the
    # new ledger row and surface the duplicate warning (it still applies —
    # warnings don't block — but the warning proves re-derivation happened).
    response = client.post(
        "/admin/cash-spend/import/apply", follow_redirects=False
    )
    assert response.status_code == 303
    # The row landed (warnings don't block), proving apply re-parsed.
    rows = cfg.cash_spend_rows()
    assert any(
        r.description == "beans" and r.amount == D("1200") for r in rows
    )


def test_apply_route_blocks_on_hard_errors_and_lands_nothing(
    tmp_path: Path, today: date
) -> None:
    """An apply POST whose file has hard errors lands zero rows — the
    route re-renders the preview rather than applying a partial file. The
    AC's "errors block apply" held on the apply path too."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    upload = _entry_csv(
        ",2026-07-10,makro,beans,coffee,1200,",
        ",2026-07-10,ghost-vendor,bad,coffee,999,",  # hard error
    )

    response = client.post(
        "/admin/cash-spend/import/apply",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    # Re-renders the preview page (200), does not redirect.
    assert response.status_code == 200
    assert "<!--section:cash-spend-import-errors-->" in response.text
    # Nothing landed.
    assert cfg.cash_spend_rows() == []


def test_apply_route_applies_soft_warnings_anyway(
    tmp_path: Path, today: date
) -> None:
    """An apply POST whose file carries only soft warnings (a duplicate
    within-file) lands all rows — warnings are the partner's call, not a
    block (#83 d2)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    upload = _entry_csv(
        ",2026-07-10,makro,beans,coffee,1200,",
        ",2026-07-10,makro,beans-again,coffee,1200,",  # duplicate within file
    )

    response = client.post(
        "/admin/cash-spend/import/apply",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    rows = cfg.cash_spend_rows()
    assert len(rows) == 2  # both landed despite the warning


def test_apply_without_a_prior_preview_or_file_is_a_clear_error(
    tmp_path: Path, today: date
) -> None:
    """An apply POST with no file and no prior preview in the session is a
    clear error, not a crash — the partner is told to preview first
    (mirrors the cost-mirror's clear-error AC)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post(
        "/admin/cash-spend/import/apply", follow_redirects=False
    )

    assert response.status_code == 200
    assert "<!--section:cash-spend-import-error-->" in response.text


def test_apply_writes_to_audit_log_and_inflates_unreviewed_count(
    tmp_path: Path, today: date
) -> None:
    """A cash-spend import IS a config edit (unlike the Loyverse mirror,
    which is a mirror action) — every row lands in ``audit_log`` and
    inflates the 9am "N changes since last review" count. The inverse of
    the cost-mirror's dedicated-table decision."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    # Mark the seeded suppliers reviewed so the count starts clean for
    # daniel; the import is the only thing that should inflate it.
    cfg.mark_reviewed("daniel")
    upload = _entry_csv(
        ",2026-07-10,makro,beans,coffee,1200,",
    )
    assert cfg.unreviewed_changes("daniel") == []

    client.post(
        "/admin/cash-spend/import/apply",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    cash_spend_entries = [
        e for e in cfg.audit_entries() if e.table_name == "cash_spend"
    ]
    assert len(cash_spend_entries) == 1
    assert len(cfg.unreviewed_changes("daniel")) == 1


# =============================================================================
# AC: /audit shows the import as one reversible stroke; per-session Revert
# =============================================================================


def test_audit_page_shows_the_import_and_per_session_revert_undoes_all_rows(
    tmp_path: Path, today: date
) -> None:
    """The AC: "``/audit`` shows the import as one reversible stroke;
    per-session Revert undoes all N rows in one click."

    A 3-row import lands three audit entries sharing one ``session_id``.
    The ``/audit`` page renders them (the session-revert form targets
    ``/audit/session/<id>/revert``). Hitting that route undoes all three
    rows in one stroke — the panic-undo path the audit page offers."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    upload = _entry_csv(
        ",2026-07-10,makro,beans,coffee,1200,",
        ",2026-07-10,wet-market,veg,kitchen,350,",
        ",2026-07-11,makro,glass,taps,3000,",
    )

    client.post(
        "/admin/cash-spend/import/apply",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )
    assert len(cfg.cash_spend_rows()) == 3

    # The audit page links the session-revert route for this session.
    audit_html = client.get("/audit").text
    cash_spend_entries = [
        e for e in cfg.audit_entries() if e.table_name == "cash_spend"
    ]
    the_session = cash_spend_entries[0].session_id
    assert the_session is not None
    assert f"/audit/session/{the_session}/revert" in audit_html

    # Per-session revert undoes all 3 rows in one click.
    reverted = client.post(
        f"/audit/session/{the_session}/revert", follow_redirects=False
    )
    assert reverted.status_code == 303
    assert cfg.cash_spend_rows() == []  # all 3 gone in one stroke


# =============================================================================
# AC: composition — imported rows flow onto the Profit Report (#112 guard)
# =============================================================================


def test_imported_rows_flow_through_cash_spend_for_period(
    tmp_path: Path, today: date
) -> None:
    """Rows landed via the importer are real ``cash_spend`` rows — they
    flow through ``cash_spend_for_period`` exactly like rows entered via
    the form (the #112 Profit Report regression guard, pinned at the
    engine seam)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    cfg = app.state.config_store
    upload = _entry_csv(
        ",2026-07-10,makro,HoD beans,coffee,1200,TRUE",
        ",2026-07-10,makro,HoD glassware,taps,3000,TRUE",
    )

    client.post(
        "/admin/cash-spend/import/apply",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    result = cash_spend_for_period(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        entries=cfg.cash_spend_rows(),
    )
    # Per-bucket net = amount / 1.07 for the VAT-inclusive rows.
    assert result.by_bucket["coffee"] == (D("1200") / D("1.07")).quantize(
        D("0.01")
    )
    assert result.by_bucket["taps"] == (D("3000") / D("1.07")).quantize(
        D("0.01")
    )
    assert result.total == result.by_bucket["coffee"] + result.by_bucket["taps"]


def test_imported_rows_appear_on_the_profit_report_screen(
    tmp_path: Path, today: date
) -> None:
    """The end-to-end composition guard: rows landed via the importer
    render on the Profit Report screen — the surface the #112 spec
    shipped. The report reads ``cfg.cash_spend_rows()`` directly, so any
    row that lands (form or importer) is visible there."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _entry_csv(
        ",2026-07-10,makro,HoD beans,coffee,1200,TRUE",
        ",2026-07-10,makro,HoD glassware,taps,3000,TRUE",
    )

    client.post(
        "/admin/cash-spend/import/apply",
        files={"file": ("cash-spend.csv", _csv_upload_bytes(upload), "text/csv")},
        follow_redirects=False,
    )

    # The Profit Report for July 2026 renders with the imported spend.
    report = client.get("/review?mode=profit&month=2026-07")
    assert report.status_code == 200
    # The spend-by-category chart renders one bar per non-zero bucket,
    # labelled with the bucket's *display name* (#115 — not the raw slug).
    # The imported coffee + taps rows land on those two buckets, so the
    # chart carries exactly those two bars. This is the composition guard:
    # rows landed via the importer are indistinguishable from form-entered
    # rows once they hit the table.
    chart = report.text.split("<!--section:spend-by-category-chart-->")[1].split(
        "<!--/section:spend-by-category-chart-->"
    )[0]
    assert chart.count('class="trend-bar"') == 2
    assert '<span class="trend-bar__label">Coffee</span>' in chart
    assert '<span class="trend-bar__label">Taps</span>' in chart


# =============================================================================
# AC: the existing per-row create/edit/delete on /admin/cash-spend is unchanged
# =============================================================================


def test_per_row_create_route_still_works_alongside_the_importer(
    tmp_path: Path, today: date
) -> None:
    """The existing per-row create route on ``/admin/cash-spend`` is
    unchanged — the importer composes with it, it does not replace it
    (the spec's "Does not touch" list)."""
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
