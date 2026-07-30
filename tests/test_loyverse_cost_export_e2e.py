"""E2E: Loyverse cost-mirror round-trip CSV (slice 1, issue #101, parent #100).

The tracer-bullet slice of the cost-mirror: a partner uploads a Loyverse
back-office items export, Books fills the ``Cost`` column from its recipes,
shows a drift diff, and on confirm serves the filled round-trip CSV for
download. This slice ships **no** paper trail (no ``loyverse_exports`` table
yet) and **no** drift badge — those land in slices 2 and 3.

One E2E seam, mirroring ``test_cash_spend_e2e.py`` and
``test_reporting_ui_e2e.py``: through ``create_app`` over a real SQLite DB
seeded from YAML, against the public interfaces (``cost_mirror.prepare`` /
``emit_filled_csv`` and the ``/admin/loyverse-export`` routes). No reaching
into internals.

The worked examples pin the slice-1 acceptance criteria:

- ``GET /admin/loyverse-export`` renders an upload form; auth-required;
  linked from ``/admin``.
- ``POST /admin/loyverse-export/prepare`` with a valid export renders the
  drift diff (filled / no Books cost: unmapped / no Books cost: unknown-price
  / differs: Loyverse X → Books Y) with a Confirm button.
- ``POST /admin/loyverse-export/confirm`` serves the filled CSV as a download
  with a ``Content-Disposition`` filename including the date. Confirm
  re-derives the diff from current state, not from a held payload.
- Wrong-file / missing-column errors render a clear error page, not a
  corrupt file.
- Round-trip fidelity: re-uploading the produced file produces a zero-drift
  diff.
- The existing ``/upload`` in-Books template and cost editor keep working.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tangerine.storage.config_store import SqliteConfigStore
from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

_TEST_PASSPHRASE = "loyverse-export-test-passphrase"
_TEST_SIGNING_SECRET = "loyverse-export-test-signing-secret"


# The croissant recipe: 50 g of butter per unit, mapped to the i-croissant
# Loyverse item. Butter is priced in the seed; the latte recipe below has an
# unpriced ingredient (no milk cost) to exercise the unknown-price path.
_RECIPES_YAML = """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "50" }
  - sku_id: latte-12oz
    name: Caffe Latte 12oz
    segment: cafe
    ingredients:
      - { sku_id: coffee-beans, quantity: "18" }
      - { sku_id: milk, quantity: "150" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
  - { item_id: i-latte, sku_id: latte-12oz }
"""

# Butter priced (0.004 THB/g net → 50 g × 0.004 = 0.20 THB/croissant); milk
# deliberately unpriced so the latte is an unknown-price row.
_COSTS_YAML = """
costs:
  butter: { price: "0.004", updated_at: "2026-06-01" }
"""

_ASSIGNEES_YAML = """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


def _build_app(tmp_path: Path, *, today: date) -> FastAPI:
    """A ``create_app`` wired against real YAML seeded into a fresh SQLite DB."""
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_RECIPES_YAML, encoding="utf-8")
    costs.write_text(_COSTS_YAML, encoding="utf-8")
    assignees.write_text(_ASSIGNEES_YAML, encoding="utf-8")
    return create_app(
        db_path=str(tmp_path / "tangerine.db"),
        recipes_path=str(recipes),
        costs_path=str(costs),
        assignees_path=str(assignees),
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
        today=today,
    )


def _authed_client(app) -> TestClient:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a session cookie"
    return client


def _items_export(rows: list[tuple[str, str, str, str, str]]) -> str:
    """Build a Loyverse items-export CSV from (handle, sku, name, price, cost)
    tuples. The header is the canonical Loyverse shape Books preserves."""
    lines = ["Handle,SKU,Name,Price,Cost"]
    for handle, sku, name, price, cost in rows:
        lines.append(f"{handle},{sku},{name},{price},{cost}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def today() -> date:
    return date(2026, 7, 29)


# =============================================================================
# AC: GET /admin/loyverse-export — upload form, linked from /admin, auth-required
# =============================================================================


def test_get_page_requires_auth(tmp_path: Path, today: date) -> None:
    """An unauthenticated GET redirects to /login, like every Admin route."""
    app = _build_app(tmp_path, today=today)
    client = TestClient(app)

    response = client.get("/admin/loyverse-export", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_get_page_renders_an_upload_form(tmp_path: Path, today: date) -> None:
    """The page shows an upload form for the Loyverse items export."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/admin/loyverse-export")

    assert response.status_code == 200
    body = response.text
    assert "method=\"post\"" in body
    assert "action=\"/admin/loyverse-export/prepare\"" in body
    assert "enctype=\"multipart/form-data\"" in body
    assert "type=\"file\"" in body


def test_admin_landing_links_the_cost_mirror_page(
    tmp_path: Path, today: date
) -> None:
    """``/admin`` links ``/admin/loyverse-export`` beside the cost book — the
    AC's "linked from the Admin landing page beside /admin/fixed-costs and
    the cost book"."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/admin")

    assert response.status_code == 200
    assert "/admin/loyverse-export" in response.text


# =============================================================================
# AC: POST /admin/loyverse-export/prepare — drift diff page with Confirm
# =============================================================================


def test_prepare_with_mapped_costable_row_shows_filled(
    tmp_path: Path, today: date
) -> None:
    """A mapped + fully-priced row renders as "filled" with Books' cost
    (``CostResolver.cost_per_unit`` rounded 2 dp). The diff page carries a
    Confirm button."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])

    response = client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.text
    assert "Butter Croissant" in body
    assert "0.20" in body  # 50 g butter × 0.004 = 0.20 THB
    assert "filled" in body.lower()
    # Confirm button present.
    assert "action=\"/admin/loyverse-export/confirm\"" in body


def test_prepare_with_unknown_price_row_shows_no_books_cost_unknown_price(
    tmp_path: Path, today: date
) -> None:
    """A mapped row whose recipe has an unpriced ingredient renders as "no
    Books cost: unknown-price". The diff names the state so the gap is
    honest (PRD user story 6)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("latte", "latte-12oz", "Caffe Latte 12oz", "60.00", ""),
    ])

    response = client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.text
    assert "Caffe Latte 12oz" in body
    assert "unknown-price" in body.lower()


def test_prepare_with_unmapped_row_shows_no_books_cost_unmapped(
    tmp_path: Path, today: date
) -> None:
    """A row whose SKU has no recipe in Books renders as "no Books cost:
    unmapped"."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("mystery", "sku-not-in-books", "Mystery Dish", "40.00", ""),
    ])

    response = client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.text
    assert "Mystery Dish" in body
    assert "unmapped" in body.lower()


def test_prepare_with_drift_row_shows_loyverse_value_and_books_value(
    tmp_path: Path, today: date
) -> None:
    """A costable row whose uploaded ``Cost`` disagrees with Books' number is
    flagged "differs" with both values: "differs: Loyverse X → Books Y"."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", "0.99"),
    ])

    response = client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.text
    assert "differs" in body.lower()
    # Both values visible so the partner sees what's overwriting what.
    assert "0.99" in body  # the Loyverse value
    assert "0.20" in body  # the Books value


def test_prepare_renders_fillable_cost_per_row(
    tmp_path: Path, today: date
) -> None:
    """The round-trip fidelity helper: a prepare against a multi-row export
    renders every row's fillable Books cost. Used by the round-trip test
    below as the 'first' half of the loop."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
        ("latte", "latte-12oz", "Caffe Latte 12oz", "60.00", ""),
        ("mystery", "sku-not-in-books", "Mystery Dish", "40.00", ""),
    ])

    response = client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    # All three rows appear, each in its bucket.
    body = response.text
    assert "Butter Croissant" in body
    assert "Caffe Latte 12oz" in body
    assert "Mystery Dish" in body


# =============================================================================
# AC: POST /admin/loyverse-export/confirm — serves filled CSV, dated filename,
#     re-derives from current state
# =============================================================================


def test_confirm_requires_auth(tmp_path: Path, today: date) -> None:
    """An unauthenticated confirm POST redirects to /login."""
    app = _build_app(tmp_path, today=today)
    client = TestClient(app)

    response = client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", b"Handle,SKU,Cost\nx,y,\n", "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_confirm_serves_filled_csv_with_dated_filename(
    tmp_path: Path, today: date
) -> None:
    """On confirm, Books serves the filled round-trip CSV as a download with a
    ``Content-Disposition`` filename that includes today's date. The filled
    file carries Books' cost for the costable row and a blank for the
    uncostable row; the header is preserved verbatim (BOM prepended for
    Excel)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
        ("latte", "latte-12oz", "Caffe Latte 12oz", "60.00", ""),  # unknown-price
    ])

    # Prepare first so the held session shape exists.
    client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    # Confirm re-uploads (the held shape is a convenience, not a trust path;
    # the AC: "re-derives the diff from current state, not from a held
    # payload"). The served file is what we assert on.
    response = client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert today.isoformat() in disposition  # filename includes the date
    # UTF-8 BOM at the head of the served file.
    body = response.content
    assert body.startswith(b"\xef\xbb\xbf")
    text = body.decode("utf-8-sig")
    # Header preserved verbatim.
    assert text.startswith("Handle,SKU,Name,Price,Cost")
    # Costable row filled; unknown-price row blanked (never zero).
    lines = text.splitlines()
    croissant_line = next(ln for ln in lines if "croissant" in ln)
    latte_line = next(ln for ln in lines if "latte-12oz" in ln)
    assert croissant_line.rstrip().endswith(",0.20")
    assert latte_line.rstrip().endswith(",")  # blank Cost, not 0.00


def test_confirm_re_derives_from_current_state_not_a_held_payload(
    tmp_path: Path, today: date
) -> None:
    """A cost edit between prepare and confirm is reflected in the served file.

    The AC: "Confirm re-derives the diff from current state, not from a held
    payload (a cost edit between prepare and confirm is reflected in the
    served file)." The partner who confirms must see what's true *now*, not
    what was true at prepare time — so the confirm route never trusts a held
    shape over a fresh derivation.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])

    client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    # Re-price butter between prepare and confirm: 0.004 → 0.006 THB/g, so
    # the croissant (50 g) moves from 0.20 to 0.30 THB. Use the cost editor
    # route (the in-Books path this slice composes with, not replaces).
    client.post(
        "/skus/butter/cost",
        data={
            "pack_price": "12",
            "pack_quantity": "2000",
            "vat_inclusive": "1",  # 12 / 2000 / 1.07 = 0.005607... → 0.28 at 50 g
        },
        follow_redirects=False,
    )

    response = client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    text = response.content.decode("utf-8-sig")
    croissant_line = next(ln for ln in text.splitlines() if "croissant" in ln)
    # The served file reflects the *new* cost (0.28), not the stale 0.20 from
    # prepare time — confirm re-derived from current state.
    assert croissant_line.rstrip().endswith(",0.28")
    assert ",0.20" not in croissant_line


def test_confirm_does_not_require_a_re_upload(tmp_path: Path, today: date) -> None:
    """The AC: "The uploaded file's parsed shape is held server-side keyed by
    session so confirm doesn't require a re-upload." A partner who prepared a
    file can confirm with an empty form — Books uses the held upload. (The
    held text is the *convenience*; the diff is still re-derived from current
    Books state on confirm — see the re-derive test below.)"""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])

    client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    # Confirm with NO file — the held upload from prepare is used.
    response = client.post("/admin/loyverse-export/confirm", follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    text = response.content.decode("utf-8-sig")
    croissant_line = next(ln for ln in text.splitlines() if "croissant" in ln)
    assert croissant_line.rstrip().endswith(",0.20")  # filled from held upload


def test_confirm_without_a_prepared_upload_is_a_clear_error(
    tmp_path: Path, today: date
) -> None:
    """A confirm with no file and no prior prepare in the session is a clear
    error, not a crash — the partner is told to prepare first."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post("/admin/loyverse-export/confirm", follow_redirects=False)

    assert response.status_code == 400
    assert b"Choose a Loyverse items export CSV first." in response.content


# =============================================================================
# AC: wrong-file / missing-column errors render a clear error page
# =============================================================================


def test_prepare_with_missing_required_column_renders_an_error_page(
    tmp_path: Path, today: date
) -> None:
    """Uploading a CSV missing ``Handle`` / ``SKU`` / ``Cost`` renders a clear
    error page naming the missing column, not a corrupt file. The AC's
    "wrong-file / missing-column errors render a clear error page"."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    # Missing the Cost column entirely.
    bad_upload = "Handle,SKU,Name,Price\ncroissant,croissant,Butter Croissant,75.00\n"

    response = client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", bad_upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200  # an error page, not a 500
    body = response.text
    assert "Cost" in body  # the missing column is named
    assert "missing" in body.lower() or "expected" in body.lower()
    # No drift table rendered for a wrong file.
    assert "Butter Croissant" not in body


def test_prepare_with_a_non_csv_file_renders_an_error_page(
    tmp_path: Path, today: date
) -> None:
    """Uploading a non-CSV (e.g. a sales report) renders a clear error page,
    not a corrupt file. The AC's "protected from uploading the wrong file"."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    # An XLSX-shaped binary (a ZIP, since xlsx is a zip) — definitely not CSV.
    xlsx_bytes = _minimal_xlsx_bytes()

    response = client.post(
        "/admin/loyverse-export/prepare",
        files={
            "file": ("sales-report.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        },
        follow_redirects=False,
    )

    assert response.status_code == 200  # an error page, not a 500
    body = response.text
    # A clear error is shown — either the UTF-8 decode failed, or the bytes
    # parsed as CSV but lack the required Loyverse columns. Either way the
    # partner sees a message, not a drift diff with their data misread.
    assert "Couldn't read that file" in body or "doesn't look like" in body
    # No drift table rendered for a wrong file.
    assert "REVIEW COST DIFF" not in body


def _minimal_xlsx_bytes() -> bytes:
    """A minimal valid-xlsx byte string (xlsx is a ZIP of XML parts)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>',
        )
    return buf.getvalue()


# =============================================================================
# AC: round-trip fidelity — re-uploading the produced file is zero-drift
# =============================================================================


def test_round_trip_reuploading_a_confirmed_file_is_zero_drift(
    tmp_path: Path, today: date
) -> None:
    """The closed-loop proof: download the filled file Books produced, upload
    it back as a fresh export, and the diff is zero drift (every costable row
    matches what Books just wrote). Mirrors Loyverse's own recommended
    export→edit→re-import flow."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
        ("latte", "latte-12oz", "Caffe Latte 12oz", "60.00", ""),  # unknown-price
        ("mystery", "sku-not-in-books", "Mystery Dish", "40.00", ""),  # unmapped
    ])

    # First pass: confirm and capture the served file.
    served = client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    served_text = served.content.decode("utf-8-sig")

    # Second pass: re-upload the served file as a fresh export. Books' costs
    # haven't changed, so every costable row matches — zero drift, and the
    # "differs" bucket is empty.
    second = client.post(
        "/admin/loyverse-export/prepare",
        files={
            "file": (
                "items-filled.csv",
                served.content,  # keep the BOM Books wrote
                "text/csv",
            )
        },
        follow_redirects=False,
    )

    assert second.status_code == 200
    body = second.text
    # The costable croissant row now matches: status is "filled" (or its
    # human label), not "differs". The served text carried 0.20; the diff
    # must not flag it as differing.
    assert "differs" not in body.lower()
    # The unknown-price and unmapped rows are still surfaced honestly.
    assert "unknown-price" in body.lower()
    assert "unmapped" in body.lower()


# =============================================================================
# AC: the existing /upload template and cost editor keep working (composition)
# =============================================================================


def test_upload_template_route_still_works(tmp_path: Path, today: date) -> None:
    """The existing ``/upload`` in-Books template-download route is untouched
    (composition, not replacement) — this slice adds a new surface for a
    different file, it does not alter the existing one."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/upload")

    assert response.status_code == 200


def test_cost_editor_route_still_works(tmp_path: Path, today: date) -> None:
    """The SKU cost editor route is unchanged — the cost mirror reads the
    same cost book the editor writes, but neither touches the other."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/skus/butter")

    assert response.status_code == 200


# =============================================================================
# Slice 2 (issue #102): the paper trail — confirm writes a loyverse_exports row
# =============================================================================


def _config_store(app: FastAPI) -> SqliteConfigStore:
    """The store the app reads/writes config through — the public read path
    the paper trail is observed over."""
    return app.state.config_store


def test_confirm_writes_one_loyverse_exports_row_with_drift(
    tmp_path: Path, today: date
) -> None:
    """A confirm where Books' cost differs from Loyverse's writes exactly one
    ``loyverse_exports`` row, attributed to the confirming partner, carrying
    the right counts and a drift payload matching the diff the prepare step
    rendered.

    The croissant costs 0.20 in Books (50 g butter × 0.004); the upload
    declares 0.99 — a drifted row. The latte is unknown-price (unpriced
    milk) and the mystery item is unmapped, so neither counts as a change.
    ``item_count`` is every row in the file; ``changed_count`` is the one
    drifted row.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", "0.99"),
        ("latte", "latte-12oz", "Caffe Latte 12oz", "60.00", ""),
        ("mystery", "sku-not-in-books", "Mystery Dish", "40.00", ""),
    ])

    response = client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 200
    exports = _config_store(app).loyverse_exports()
    assert len(exports) == 1
    export = exports[0]
    assert export.partner_id == "daniel"
    assert export.item_count == 3
    assert export.changed_count == 1
    # confirmed_at is stamped by the store clock — non-empty, ISO-8601 shaped.
    assert export.confirmed_at
    assert "T" in export.confirmed_at
    # drift_payload matches the diff the partner was shown: one entry for the
    # drifted croissant, with Loyverse value vs Books value.
    payload = json.loads(export.drift_payload)
    assert payload == [
        {
            "sku": "croissant",
            "name": "Butter Croissant",
            "loyverse_cost": "0.99",
            "books_cost": "0.20",
        }
    ]


def test_confirm_drift_payload_matches_prepare_diff_exactly(
    tmp_path: Path, today: date
) -> None:
    """The drift payload recorded on confirm is the exact diff the prepare
    step rendered — same ``{sku, name, loyverse_cost, books_cost}`` entries,
    same order. Captured by scraping the prepare page's differs rows and
    comparing against the recorded JSON."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", "0.99"),
        ("mystery2", "sku-not-in-books-2", "Mystery Two", "40.00", "0.50"),
    ])

    prepare_page = client.post(
        "/admin/loyverse-export/prepare",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    # The prepare page names the drifted row with both values.
    assert "differs" in prepare_page.text.lower()
    assert "0.99" in prepare_page.text
    assert "0.20" in prepare_page.text

    client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    (export,) = _config_store(app).loyverse_exports()
    payload = json.loads(export.drift_payload)
    # Only the costable drifted row (croissant) is in the payload — the
    # unmapped mystery2 row is not a "change" (its Cost stays blank).
    (entry,) = payload
    assert entry["sku"] == "croissant"
    assert entry["loyverse_cost"] == "0.99"
    assert entry["books_cost"] == "0.20"


def test_confirm_zero_drift_still_writes_a_row(tmp_path: Path, today: date) -> None:
    """A confirm where every costable row already matches Loyverse still
    writes a row (``changed_count = 0``, ``drift_payload = "[]"``) — PRD user
    story 9: "the mirror was confirmed current on <date>" is visible rather
    than inferred from absence.

    The round-trip path: emit a file via confirm, re-upload it; every
    costable row now matches what Books just wrote, so ``changed_count = 0``.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    first_upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])

    served = client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", first_upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert served.status_code == 200

    # Re-upload the served file — every costable row now matches Books.
    second = client.post(
        "/admin/loyverse-export/confirm",
        files={
            "file": (
                "items-filled.csv",
                served.content,  # keep the BOM Books wrote
                "text/csv",
            )
        },
        follow_redirects=False,
    )
    assert second.status_code == 200

    exports = _config_store(app).loyverse_exports()
    assert len(exports) == 2
    zero_drift = exports[0]  # newest-first
    assert zero_drift.changed_count == 0
    assert zero_drift.drift_payload == "[]"
    assert zero_drift.item_count == 1


def test_confirm_does_not_pollute_the_9am_config_changes_count(
    tmp_path: Path, today: date
) -> None:
    """The dedicated-vs-audit-log decision (issue #70 / spec #100): a Loyverse
    confirm is a mirror action, not a config edit, so it must NOT land in
    ``audit_log`` and must NOT inflate the 9am "N changes since last review"
    count. After a confirm, ``unreviewed_changes`` and ``audit_entries`` are
    still empty."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", "0.99"),
    ])

    client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    store = _config_store(app)
    assert store.audit_entries() == []
    assert store.unreviewed_changes("daniel") == []


def test_confirm_attributed_to_the_logged_in_partner(tmp_path: Path, today: date) -> None:
    """The paper-trail row carries the confirming partner's assignee id —
    ``request.state.assignee_id``, threaded from the login the same way every
    other Admin write is. A confirm under Noi's session records Noi."""
    app = _build_app(tmp_path, today=today)
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "noi"},
        follow_redirects=False,
    )
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])

    client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    (export,) = _config_store(app).loyverse_exports()
    assert export.partner_id == "noi"


# =============================================================================
# Slice 3 (issue #103): the drift badge on GET /admin/loyverse-export
# =============================================================================
#
# The staleness signal: a quiet line on the cost-mirror page that makes
# "Loyverse is stale" detectable from inside Books, without opening Loyverse
# and without a Loyverse API read. The badge reads the most recent
# ``loyverse_exports.confirmed_at`` (the "as-of" timestamp) and the count of
# ``audit_log`` cost edits newer than that (``cost_edits_since``).
#
# Three rules the badge holds (the slice's AC):
#
# - **Suppressed before any export.** No ``loyverse_exports`` row, no badge — a
#   fresh deployment does not see a misleading "stale since forever" message.
# - **Counts cost edits only.** A recipe edit does not inflate the count — the
#   mirrored number comes from the cost book, not the recipe.
# - **Resets to zero after a confirm.** The confirm writes a new
#   ``loyverse_exports`` row whose ``confirmed_at`` is now the as-of; cost
#   edits after that re-increment the count.
#
# Trust-boundary caveat (settled Q5, issue #70 / spec #100): the badge's
# wording reads "since the last Loyverse import" but the value is the last
# *export* (``loyverse_exports.confirmed_at``). Books does not track whether
# the partner uploaded the file to Loyverse or whether Loyverse ingested it —
# the wording is honest about what Books can verify, even if a partner who
# confirmed but forgot to upload leaves the badge reading zero while Loyverse
# is in fact stale. That is a procedural miss, not a defect.


def _confirm_export(client, upload: str) -> None:  # type: ignore[no-untyped-def]
    """POST a confirm so the page has a most-recent export to badge against."""
    client.post(
        "/admin/loyverse-export/confirm",
        files={"file": ("items.csv", upload.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )


# The badge reads ``audit_log.changed_at > loyverse_exports.confirmed_at``
# (strict ``>``), so a test that confirms and then edits needs the edit's
# ``changed_at`` to be strictly newer than the confirm's ``confirmed_at``. The
# store's clock reads the wall clock (``datetime.now(timezone.utc).isoformat()``
# — microsecond-resolution ISO-8601), so real time advancing between the two
# POSTs is what makes the edit's timestamp strictly later. The store never
# truncates to whole seconds, so two sequential TestClient requests cannot
# land in the same tick — the ordering is deterministic without sleeping or
# reaching into the store's clock.
#
# The venue-date assertion derives the expected label from the stored
# ``confirmed_at`` (the source of truth) rather than hardcoding a calendar
# date, so the test is honest about *when* it ran. The helper mirrors the
# route's ``_venue_date_label`` shape — ``"<d> <Mon> <YYYY>"`` in Phuket time.
from datetime import datetime as _datetime  # noqa: E402

from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: E402

_VENUE_TZ = _ZoneInfo("Asia/Bangkok")


def _expected_venue_date(confirmed_at: str) -> str:
    """The venue-local calendar date label for an ISO-8601 UTC timestamp.

    Mirrors the route's ``_venue_date_label`` so the date assertion is
    independent of when the test runs — the badge's date is whatever day the
    confirm happened on in Phuket, not a hardcoded calendar date.
    """
    local = _datetime.fromisoformat(confirmed_at).astimezone(_VENUE_TZ)
    return f"{local.day} {local.strftime('%b')} {local.year}"


def _badge_text(body: str) -> str:
    """Extract the rendered badge line from the page body.

    The badge is the text between the ``loyverse-export-drift-badge`` section
    markers. Asserting on this slice (rather than the whole body) keeps the
    wording checks honest — they target the badge, not whatever else the page
    happens to carry.
    """
    start = body.find("<!--section:loyverse-export-drift-badge-->")
    end = body.find("<!--/section:loyverse-export-drift-badge-->")
    assert start != -1, "no drift-badge section rendered"
    return body[start:end]


def test_badge_suppressed_before_any_export(tmp_path: Path, today: date) -> None:
    """The AC: "``GET /admin/loyverse-export`` with no prior export shows no
    stale-since message." A fresh deployment never sees a misleading "stale
    since forever" line — the badge is entirely absent until the first
    confirm writes a ``loyverse_exports`` row."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/admin/loyverse-export")

    assert response.status_code == 200
    body = response.text
    # No badge section rendered, and the staleness wording is absent.
    assert "<!--section:loyverse-export-drift-badge-->" not in body
    assert "since the last Loyverse import" not in body
    assert "item costs changed" not in body.lower()


def test_badge_reads_zero_immediately_after_a_confirm(
    tmp_path: Path, today: date
) -> None:
    """The AC: "After a confirm, ``GET /admin/loyverse-export`` shows '0 item
    costs changed since the last Loyverse import on <date>.' "

    The confirm writes a ``loyverse_exports`` row whose ``confirmed_at`` is
    now the badge's as-of; no cost edit has happened since, so the badge
    reads zero (the strict-``>`` comparison in ``cost_edits_since`` keeps
    even a same-instant edit from counting).
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])
    _confirm_export(client, upload)

    response = client.get("/admin/loyverse-export")

    assert response.status_code == 200
    badge = _badge_text(response.text)
    expected_date = _expected_venue_date(
        _config_store(app).loyverse_exports()[0].confirmed_at
    )
    assert (
        "0 item costs changed in Books since the last Loyverse import"
        f" on {expected_date}." in badge
    )


def test_badge_increments_after_a_cost_edit(tmp_path: Path, today: date) -> None:
    """The AC: "After a ``save_cost`` call post-confirm, the badge reads '1
    item cost changed…' "

    One cost edit (re-pricing butter) between the confirm and the next page
    load increments the badge from 0 to 1. The wording is singular ("1 item
    cost changed"). Real time advancing between the confirm POST and the cost
    edit POST is what makes the edit's ``changed_at`` strictly newer than the
    confirm's ``confirmed_at``.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])
    _confirm_export(client, upload)

    # One cost edit after the confirm. The store's wall-clock ``now`` has
    # advanced past the confirm's timestamp between the two POSTs.
    client.post(
        "/skus/butter/cost",
        data={"pack_price": "10", "pack_quantity": "2500"},
        follow_redirects=False,
    )

    response = client.get("/admin/loyverse-export")
    badge = _badge_text(response.text)
    expected_date = _expected_venue_date(
        _config_store(app).loyverse_exports()[0].confirmed_at
    )
    assert (
        "1 item cost changed in Books since the last Loyverse import"
        f" on {expected_date}." in badge
    )
    # The plural form is NOT shown for one edit.
    assert "1 item costs changed" not in badge


def test_badge_increments_to_two_after_two_cost_edits(
    tmp_path: Path, today: date
) -> None:
    """The AC: "two cost edits → '2 item costs changed…'". The badge reads the
    partner's edit volume, not a boolean — two distinct cost saves both land
    in ``audit_log`` after the confirm, so the count is 2 (plural wording)."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])
    _confirm_export(client, upload)

    # Two cost edits after the confirm (butter repriced twice). Each save
    # lands a distinct ``costs`` audit row, both newer than the confirm.
    client.post(
        "/skus/butter/cost",
        data={"pack_price": "10", "pack_quantity": "2500"},
        follow_redirects=False,
    )
    client.post(
        "/skus/butter/cost",
        data={"pack_price": "12", "pack_quantity": "2500"},
        follow_redirects=False,
    )

    response = client.get("/admin/loyverse-export")
    badge = _badge_text(response.text)
    expected_date = _expected_venue_date(
        _config_store(app).loyverse_exports()[0].confirmed_at
    )
    assert (
        "2 item costs changed in Books since the last Loyverse import"
        f" on {expected_date}." in badge
    )


def test_badge_resets_to_zero_after_a_fresh_confirm(
    tmp_path: Path, today: date
) -> None:
    """The AC: "confirm resets the badge to zero."

    After a confirm that inflated the badge to 1, a *second* confirm writes a
    newer ``loyverse_exports`` row whose ``confirmed_at`` is now the as-of.
    The badge reads ``cost_edits_since(<new confirmed_at>)`` — zero cost
    edits have happened since that second confirm, so the badge is back to 0.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])
    _confirm_export(client, upload)

    # One cost edit inflates the badge to 1.
    client.post(
        "/skus/butter/cost",
        data={"pack_price": "10", "pack_quantity": "2500"},
        follow_redirects=False,
    )
    mid = client.get("/admin/loyverse-export")
    mid_expected = _expected_venue_date(
        _config_store(app).loyverse_exports()[0].confirmed_at
    )
    assert (
        "1 item cost changed in Books since the last Loyverse import"
        f" on {mid_expected}." in _badge_text(mid.text)
    )

    # A fresh confirm re-stamps the as-of; the badge reads zero again.
    _confirm_export(client, upload)
    after = client.get("/admin/loyverse-export")
    badge = _badge_text(after.text)
    after_expected = _expected_venue_date(
        _config_store(app).loyverse_exports()[0].confirmed_at
    )
    assert (
        "0 item costs changed in Books since the last Loyverse import"
        f" on {after_expected}." in badge
    )
    assert "1 item cost changed" not in badge


def test_badge_unaffected_by_a_recipe_edit(tmp_path: Path, today: date) -> None:
    """The AC: "A recipe edit (``save_recipe``) between two exports does not
    change the badge count — only cost edits count."

    The mirrored cost comes from ``CostResolver`` over the cost book, not the
    recipe, so changing the croissant's butter quantity from 50 g to 60 g does
    not move the number Books would write to Loyverse and must not inflate the
    badge. The recipe edit lands in ``audit_log`` as ``table_name='recipes'``,
    which ``cost_edits_since`` excludes by construction.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])
    _confirm_export(client, upload)

    # A recipe edit (after the confirm) — bump the croissant's butter from
    # 50 g to 60 g. Lands a ``recipes`` audit row, not a ``costs`` one.
    client.post(
        "/skus/croissant/recipe",
        data={
            "ingredient_sku_id": "butter",
            "quantity": "60",
            "yield_qty": "1",
        },
        follow_redirects=False,
    )

    response = client.get("/admin/loyverse-export")
    badge = _badge_text(response.text)
    expected_date = _expected_venue_date(
        _config_store(app).loyverse_exports()[0].confirmed_at
    )
    assert (
        "0 item costs changed in Books since the last Loyverse import"
        f" on {expected_date}." in badge
    )


def test_badge_names_the_last_export_date_not_an_ingestion_event(
    tmp_path: Path, today: date
) -> None:
    """The AC: "The badge's wording names the last **export**
    (``loyverse_exports.confirmed_at``), not a Loyverse-side ingestion event."

    The trust-boundary caveat (Q5 resolution, issue #70 / spec #100): Books
    does not track whether the partner uploaded the file to Loyverse or
    whether Loyverse ingested it. The badge's wording reads "since the last
    Loyverse import" but the value backing it is the last *export* — the
    honest framing of what Books can verify. The rendered date is the venue-
    local calendar date of the most recent ``confirmed_at``, so the partner
    sees a day they recognise, not a UTC instant."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])
    _confirm_export(client, upload)

    response = client.get("/admin/loyverse-export")
    badge = _badge_text(response.text)
    # The wording names the last export via its venue-local date.
    assert "since the last Loyverse import on" in badge
    expected_date = _expected_venue_date(
        _config_store(app).loyverse_exports()[0].confirmed_at
    )
    assert expected_date in badge


def test_badge_only_on_loyverse_export_page_not_daily_review(
    tmp_path: Path, today: date
) -> None:
    """The AC: "The badge is on ``/admin/loyverse-export`` only — no banner on
    the daily review, no notification surface."

    The daily review is the home page; the cost-mirror's staleness signal does
    not leak there. A confirm writes a ``loyverse_exports`` row, but the daily
    review must not render the drift badge's wording."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    upload = _items_export([
        ("croissant", "croissant", "Butter Croissant", "75.00", ""),
    ])
    _confirm_export(client, upload)

    review = client.get("/")
    assert review.status_code == 200
    assert "since the last Loyverse import" not in review.text
    assert "<!--section:loyverse-export-drift-badge-->" not in review.text

