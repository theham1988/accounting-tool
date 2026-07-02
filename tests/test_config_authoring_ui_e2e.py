"""Cost editor + spreadsheet upload UI seam (Wave 1.5, Slice 3).

Per ADR-0003 / issue 25: the first *write* surfaces of the config authoring
wave — a cost editor that captures what the partner actually sees on a
receipt (pack price, pack quantity, VAT-inclusive flag) and derives the net
per-unit price, plus a CSV upload path for bulk-loading mappings and costs.

The genuine external boundary is HTTP (driven through FastAPI's
``TestClient``); recipes/costs/mappings are seeded into a real SQLite DB
exactly as the migrator seeds them in production. Per the PRD's testing
rules these tests assert on visible behaviour — the rendered HTML, the
resulting margin numbers — not on implementation details.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from tangerine.loyverse.store import MenuItem, MenuSnapshot
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Segment
from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

D = Decimal

_TEST_PASSPHRASE = "authoring-ui-test-passphrase"
_TEST_SIGNING_SECRET = "authoring-ui-test-signing-secret"


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _write_assignees(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "assignees.yaml",
        """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
""",
    )


def _authed_client(app, assignee_id: str = "daniel") -> TestClient:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": assignee_id},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a session cookie"
    return client


def _build_app(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    recipes_yaml: str,
    costs_yaml: str,
    today: date | None = None,
):
    """A ``create_app`` wired against real YAML seeded into a fresh SQLite DB."""
    recipes_path = _write(tmp_path / "recipes.yaml", recipes_yaml)
    costs_path = _write(tmp_path / "costs.yaml", costs_yaml)
    assignees_path = _write_assignees(tmp_path)
    return create_app(
        db_path=str(tmp_path / "tangerine.db"),
        recipes_path=str(recipes_path),
        costs_path=str(costs_path),
        assignees_path=str(assignees_path),
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
        today=today,
    )


def _seed_menu(db_path: str, items: list[MenuItem]) -> None:
    """Record a menu snapshot into ``db_path``, exactly as a sync would."""
    store = SqliteLoyverseStore.connect(db_path)
    store.record_menu_snapshot(
        MenuSnapshot(items=tuple(items)), at=datetime(2026, 6, 30, tzinfo=timezone.utc)
    )
    store.close()


def _menu_item(
    item_id: str, name: str, price: str, segment: Segment = Segment.CAFE
) -> MenuItem:
    return MenuItem(item_id=item_id, name=name, sell_price=D(price), segment=segment)


# The croissant recipe: 50 g of butter per unit. Butter is costed from a
# Makro receipt in the worked examples below, so the derived net per-unit
# price flows straight into the croissant's margin.
_RECIPES_YAML = """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "50" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
"""

_COSTS_YAML = """
costs:
  butter: { price: "0.214", updated_at: "2026-06-01" }  # Makro 2 kg block
"""


# --- AC: all edits gated behind existing auth middleware ---------------------


def test_cost_editor_page_requires_auth(tmp_path: Path) -> None:
    """An unauthenticated ``GET /skus/{sku_id}`` is redirected to ``/login``,
    matching every other protected route in the app.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = TestClient(app)

    response = client.get("/skus/butter", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# --- AC: cost editor captures pack_price, pack_quantity, vat_inclusive ------
# --- AC: VAT-inclusive checkbox defaults to checked --------------------------


def test_cost_editor_renders_pack_fields_with_vat_defaulting_to_checked(
    tmp_path: Path,
) -> None:
    """The cost editor for a SKU shows the three receipt-shaped inputs —
    pack price, pack quantity, VAT-inclusive — with the VAT checkbox
    checked by default (Makro is the dominant supplier), plus the SKU's
    unit and its current stored cost for context.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    response = client.get("/skus/butter")

    assert response.status_code == 200
    html = response.text
    # The three receipt-shaped inputs are present.
    assert 'name="pack_price"' in html
    assert 'name="pack_quantity"' in html
    assert 'name="vat_inclusive"' in html
    # The VAT checkbox defaults to checked.
    checkbox = html.split('name="vat_inclusive"')[1].split(">")[0]
    assert "checked" in checkbox
    # The SKU's identity, unit, and current stored (net) cost are visible.
    assert "butter" in html
    assert "0.20" in html  # 0.214 / 1.07 = 0.2 net, rendered at 2 dp


# --- AC: saving a cost updates DB; tomorrow's review reflects the new cost ---


def test_saving_a_cost_updates_db_and_the_next_review(tmp_path: Path) -> None:
    """Given a partner enters pack price 380 THB for a 2 kg block of butter
    with VAT inclusive, the stored cost becomes 380 / 2000 / 1.07 =
    0.177570 THB/g net — and the croissant (50 g butter, sold at 95 THB
    yesterday) shows a margin of 95 − 50 × 0.177570 = 86.12 on the review,
    without touching YAML.
    """
    today = date(2026, 7, 2)
    yesterday = date(2026, 7, 1)
    app = _build_app(
        tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML, today=today
    )
    _seed_sale(app.state.db_path, item_id="i-croissant", day=yesterday, price="95")
    client = _authed_client(app)

    response = client.post(
        "/skus/butter/cost",
        data={"pack_price": "380", "pack_quantity": "2000", "vat_inclusive": "1"},
        follow_redirects=False,
    )

    # Saving lands back on the editor, which now shows the derived net cost.
    assert response.status_code == 303
    assert response.headers["location"] == "/skus/butter"
    editor_html = client.get("/skus/butter").text
    assert "0.18" in editor_html  # 0.177570 rendered at 2 dp

    # Tomorrow's 9am review reflects the new cost: 95 − 8.88 = 86.12.
    review_html = client.get("/").text
    assert "86.12" in review_html


# --- AC: derives and shows price_per_unit_net live as partner types ---------


def test_cost_preview_derives_net_per_unit_live(tmp_path: Path) -> None:
    """The editor's inputs trigger a preview request as the partner types;
    the fragment spells out the arithmetic — ``380 / 2000 / 1.07 =
    0.177570 THB/g`` — so there is no mental arithmetic left to do.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    response = client.get(
        "/skus/butter/cost-preview",
        params={"pack_price": "380", "pack_quantity": "2000", "vat_inclusive": "1"},
    )

    assert response.status_code == 200
    assert "0.177570" in response.text
    # The editor page wires its inputs to this preview endpoint.
    editor_html = client.get("/skus/butter").text
    assert "/skus/butter/cost-preview" in editor_html


def test_cost_preview_without_vat_skips_the_division(tmp_path: Path) -> None:
    """Unchecking VAT (a wet-market purchase) derives gross-as-net:
    380 / 2000 = 0.19 THB/g, no 1.07 division.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    response = client.get(
        "/skus/butter/cost-preview",
        params={"pack_price": "380", "pack_quantity": "2000"},
    )

    assert response.status_code == 200
    assert "0.190000" in response.text


def test_cost_preview_with_incomplete_input_stays_calm(tmp_path: Path) -> None:
    """Half-typed input (empty quantity) renders an empty-ish fragment, not
    an error — the partner is mid-keystroke, not wrong.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    response = client.get(
        "/skus/butter/cost-preview",
        params={"pack_price": "380", "pack_quantity": ""},
    )

    assert response.status_code == 200
    assert "0.1" not in response.text


# --- AC: GET /upload returns a downloadable template (CSV) pre-filled -------


def test_upload_page_requires_auth_and_offers_template_and_form(tmp_path: Path) -> None:
    """``GET /upload`` is gated like everything else; once signed in it
    offers the template download and the file-upload form.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)

    unauthenticated = TestClient(app).get("/upload", follow_redirects=False)
    assert unauthenticated.status_code == 302
    assert unauthenticated.headers["location"] == "/login"

    response = _authed_client(app).get("/upload")
    assert response.status_code == 200
    html = response.text
    assert 'href="/upload/template"' in html
    assert 'enctype="multipart/form-data"' in html
    assert 'action="/upload"' in html


def test_template_csv_prefills_every_item_and_every_sku(tmp_path: Path) -> None:
    """The downloadable template carries one mapping row per Loyverse item
    (current SKU pre-filled, blank when unmapped) and one cost row per
    known SKU (unit + current VAT flag pre-filled) — the partner fills in
    the blanks offline in Excel.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    _seed_menu(
        app.state.db_path,
        [
            _menu_item("i-croissant", "Butter Croissant", "95"),
            _menu_item("i-mystery", "Mystery Soda", "60"),
        ],
    )
    client = _authed_client(app)

    response = client.get("/upload/template")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    header = lines[0]
    for column in (
        "kind",
        "item_id",
        "sku_id",
        "pack_price",
        "pack_quantity",
        "vat_inclusive",
    ):
        assert column in header
    body = "\n".join(lines[1:])
    # One mapping row per Loyverse item: the croissant pre-filled with its
    # SKU, the mystery soda's sku_id left blank for the partner to fill in.
    assert "mapping,i-croissant" in body
    assert "croissant" in body
    assert "mapping,i-mystery" in body
    # One cost row per known SKU, VAT flag pre-filled from the DB (the
    # butter row was seeded from a Makro comment → TRUE).
    assert "cost,,," in body  # cost rows leave the item columns blank
    assert "butter" in body
    assert "TRUE" in body


# --- AC: POST /upload parses, previews changes, applies on confirm ----------

# A second saleable SKU (soda, with its own recipe) so an upload can assign
# the unmapped mystery soda to something real.
_UPLOAD_RECIPES_YAML = """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "50" }
  - sku_id: soda
    name: House Soda
    segment: cafe
    ingredients:
      - { sku_id: syrup, quantity: "30" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
"""

_UPLOAD_COSTS_YAML = """
costs:
  butter: { price: "0.214", updated_at: "2026-06-01" }  # Makro 2 kg block
  syrup: { price: "0.10", updated_at: "2026-06-01" }
"""

_FILLED_CSV = """kind,item_id,item_name,sku_id,sku_name,unit,pack_price,pack_quantity,vat_inclusive
mapping,i-mystery,Mystery Soda,soda,,,,,
cost,,,butter,Butter,g,380,2000,TRUE
"""


def _upload_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = _build_app(
        tmp_path,
        recipes_yaml=_UPLOAD_RECIPES_YAML,
        costs_yaml=_UPLOAD_COSTS_YAML,
        today=date(2026, 7, 2),
    )
    _seed_menu(
        app.state.db_path,
        [
            _menu_item("i-croissant", "Butter Croissant", "95"),
            _menu_item("i-mystery", "Mystery Soda", "60"),
        ],
    )
    return app


def test_upload_previews_changes_without_applying(tmp_path: Path) -> None:
    """Uploading a filled spreadsheet shows what will change — the mystery
    soda gaining a mapping, butter's cost moving from 0.20 to 0.177570 —
    but nothing lands until the partner confirms: the item stays unmapped
    and the old cost stays current.
    """
    app = _upload_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/upload", files={"file": ("filled.csv", _FILLED_CSV, "text/csv")}
    )

    assert response.status_code == 200
    html = response.text
    # The preview names both pending changes, with old and new values.
    assert "i-mystery" in html
    assert "soda" in html
    assert "butter" in html
    assert "0.177570" in html  # the new derived net
    assert "0.20" in html  # the old stored net
    # A confirm control is offered.
    assert 'name="confirm"' in html
    # ...but nothing has been applied yet.
    items_html = client.get("/items", params={"item": "i-mystery"}).text
    assert "unmapped" in items_html.lower()
    editor_html = client.get("/skus/butter").text
    assert "0.177570" not in editor_html


def test_upload_confirm_applies_mappings_and_costs(tmp_path: Path) -> None:
    """Confirming the preview lands both kinds of change: the mystery soda
    is mapped to the soda SKU (visible in item coverage) and butter's new
    net cost flows into the croissant's margin on the next review.
    """
    app = _upload_app(tmp_path)
    _seed_sale(app.state.db_path, item_id="i-croissant", day=date(2026, 7, 1), price="95")
    client = _authed_client(app)
    preview_html = client.post(
        "/upload", files={"file": ("filled.csv", _FILLED_CSV, "text/csv")}
    ).text
    assert 'name="confirm"' in preview_html

    response = client.post(
        "/upload", data={"csv_text": _FILLED_CSV, "confirm": "1"}
    )

    assert response.status_code == 200
    assert "applied" in response.text.lower()
    # The mapping landed: the mystery soda now resolves to the soda SKU.
    items_html = client.get("/items", params={"item": "i-mystery"}).text
    assert "soda" in items_html
    assert 'item-row--unmapped' not in items_html
    # The cost landed: 95 − 50 × 0.177570 = 86.12 on tomorrow's review.
    review_html = client.get("/").text
    assert "86.12" in review_html


# --- Closing the loop: the new surfaces are reachable from the SKU view -----


def test_sku_view_links_each_row_to_its_editor_and_to_upload(tmp_path: Path) -> None:
    """The partner's fix path is one click from the diagnosis: each SKU row
    links to its cost editor, and the bulk-upload page is linked from the
    same view.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    html = client.get("/skus").text

    assert 'href="/skus/butter"' in html
    assert 'href="/upload"' in html


# --- AC: upload errors reported per-row with row numbers ---------------------


def test_upload_errors_are_reported_per_row_and_block_apply(tmp_path: Path) -> None:
    """A file with an unknown SKU reference (row 2) and a malformed number
    (row 4) reports both problems with their row numbers, offers no confirm
    control, and applies nothing — including the valid row 3 sandwiched
    between them.
    """
    bad_csv = (
        "kind,item_id,item_name,sku_id,sku_name,unit,pack_price,pack_quantity,vat_inclusive\n"
        "mapping,i-mystery,Mystery Soda,no-such-sku,,,,,\n"
        "mapping,i-croissant,Butter Croissant,soda,,,,,\n"
        "cost,,,butter,Butter,g,not-a-number,2000,TRUE\n"
    )
    app = _upload_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/upload", files={"file": ("filled.csv", bad_csv, "text/csv")}
    )

    assert response.status_code == 200
    html = response.text
    assert "no-such-sku" in html
    assert "not-a-number" in html
    # Row numbers as Excel shows them (header is row 1).
    assert ">2<" in html
    assert ">4<" in html
    # Errors block the apply: no confirm control is offered...
    assert 'name="confirm"' not in html
    # ...and the valid row 3 did not land either.
    items_html = client.get("/items", params={"item": "i-croissant"}).text
    assert ">croissant<" in items_html or "croissant</a>" in items_html
    assert "soda</a>" not in items_html


def test_upload_with_missing_column_names_the_column(tmp_path: Path) -> None:
    """A file missing a required column (``vat_inclusive``) is rejected with
    an error naming the column — the partner exported or edited the header
    away, and guessing a default for every row would be worse.
    """
    headerless_csv = (
        "kind,item_id,item_name,sku_id,sku_name,unit,pack_price,pack_quantity\n"
        "cost,,,butter,Butter,g,380,2000\n"
    )
    app = _upload_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/upload", files={"file": ("filled.csv", headerless_csv, "text/csv")}
    )

    assert response.status_code == 200
    html = response.text
    assert "missing column" in html
    assert "vat_inclusive" in html
    assert 'name="confirm"' not in html


def _seed_sale(db_path: str, *, item_id: str, day: date, price: str) -> None:
    """Record one sold unit into ``db_path``, exactly as a sync would."""
    from tangerine.loyverse.store import SaleRecord
    from tangerine.types import Sale

    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(
        [
            SaleRecord(
                sale=Sale(item_id=item_id, timestamp=day, sell_price=D(price)),
                receipt_number="r-1",
                line_id="li-1",
            )
        ]
    )
    store.close()
