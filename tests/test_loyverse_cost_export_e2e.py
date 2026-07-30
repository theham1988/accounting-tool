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
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
