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


# =============================================================================
# Recipe editor with inline ingredient creation (Wave 1.5, Slice 4 / issue 26)
# =============================================================================

# The latte recipe: 18 g of beans then 200 ml of milk, in that order. Costs
# carry pack-size comments (no Makro/ARO marker, so prices seed net as-is):
# beans 0.65/g, milk 0.02/ml. Flour (0.05/g) is a costed ingredient no recipe
# uses yet — it exists so shorthand tests can compare "1 tbsp" of a g-SKU
# against an ml-SKU.
_RECIPE_EDITOR_RECIPES_YAML = """
recipes:
  - sku_id: latte
    name: Cafe Latte
    segment: cafe
    ingredients:
      - { sku_id: beans, quantity: "18" }
      - { sku_id: milk, quantity: "200" }

mappings:
  - { item_id: i-latte, sku_id: latte }
"""

_RECIPE_EDITOR_COSTS_YAML = """
costs:
  beans: { price: "0.65", updated_at: "2026-06-01" }  # 1 kg bag
  milk: { price: "0.02", updated_at: "2026-06-01" }  # 2 l bottle
  flour: { price: "0.05", updated_at: "2026-06-01" }  # 1 kg bag
"""


def _recipe_app(tmp_path: Path, today: date | None = None):  # type: ignore[no-untyped-def]
    return _build_app(
        tmp_path,
        recipes_yaml=_RECIPE_EDITOR_RECIPES_YAML,
        costs_yaml=_RECIPE_EDITOR_COSTS_YAML,
        today=today or date(2026, 7, 2),
    )


# --- AC: recipe editor renders existing ingredients as editable rows --------


def test_recipe_editor_renders_existing_ingredients_as_editable_rows(
    tmp_path: Path,
) -> None:
    """Opening the latte's editor shows its recipe as editable rows — an
    ingredient picker plus a quantity input per row — with the rows in the
    recipe's stored order (beans first, then milk).
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.get("/skus/latte")

    assert response.status_code == 200
    html = response.text
    # Each ingredient row is a picker + a quantity input, posting to the
    # recipe-save route.
    assert 'action="/skus/latte/recipe"' in html
    assert 'name="ingredient_sku_id"' in html
    assert 'name="quantity"' in html
    assert 'value="18"' in html
    assert 'value="200"' in html
    # Rows come back in stored position order: beans before milk.
    beans_row = html.index('value="beans" selected')
    milk_row = html.index('value="milk" selected')
    assert beans_row < milk_row


# --- AC: picker offers only existing SKUs (no orphan references possible) ---


def test_ingredient_picker_offers_only_existing_skus(tmp_path: Path) -> None:
    """The picker is a dropdown over the SKUs the DB actually knows —
    including flour, costed but not yet used by any recipe — and never a
    free-text field, so the partner cannot type a ``sku_id`` that points
    at nothing.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    html = client.get("/skus/latte").text

    picker = html.split('name="ingredient_sku_id"')[1].split("</select>")[0]
    # Every existing SKU is offered, even ones no recipe uses yet.
    for sku_id in ("beans", "milk", "flour", "latte"):
        assert f'value="{sku_id}"' in picker
    # The ingredient reference is a dropdown, not free text: no text input
    # carries the ingredient_sku_id name.
    assert '<input type="text" name="ingredient_sku_id"' not in html
    # Only real SKUs appear as options (plus the inline-create affordance,
    # which deliberately carries a non-SKU sentinel value).
    option_values = [
        part.split('"')[0]
        for part in picker.split('value="')[1:]
    ]
    known = {"beans", "milk", "flour", "latte", "__create__"}
    assert set(option_values) <= known


# --- AC: saving a recipe updates DB; tomorrow's 9am review reflects it ------


def test_saving_a_recipe_updates_db_and_the_next_review(tmp_path: Path) -> None:
    """Given the partner bumps the latte's beans from 18 g to 20 g, the
    saved recipe costs 20 × 0.65 + 200 × 0.02 = 17.00 — so yesterday's
    120 THB latte shows a margin of 103.00 on the next review, without
    touching YAML.
    """
    app = _recipe_app(tmp_path, today=date(2026, 7, 2))
    _seed_sale(app.state.db_path, item_id="i-latte", day=date(2026, 7, 1), price="120")
    client = _authed_client(app)

    response = client.post(
        "/skus/latte/recipe",
        data={"ingredient_sku_id": ["beans", "milk"], "quantity": ["20", "200"]},
        follow_redirects=False,
    )

    # Saving lands back on the editor, which now shows the new quantity.
    assert response.status_code == 303
    assert response.headers["location"] == "/skus/latte"
    editor_html = client.get("/skus/latte").text
    assert 'value="20"' in editor_html

    # The next review reflects the edited recipe: 120 − 17.00 = 103.00.
    review_html = client.get("/").text
    assert "103.00" in review_html


# --- AC: quantity shorthand converts to the ingredient's canonical unit -----
# --- AC: conversion uses the ingredient's unit field (ml for milk, g for flour)


def test_quantity_shorthand_converts_via_the_ingredients_unit(
    tmp_path: Path,
) -> None:
    """``1 tbsp`` of milk (an ml SKU) is stored as 15, and ``1 tbsp`` of
    flour (a g SKU) is also stored as 15 — the shorthand names a spoon,
    the ingredient's unit decides what the 15 means. The stored numbers
    are canonical: the editor shows 15, not the shorthand.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus/latte/recipe",
        data={
            "ingredient_sku_id": ["milk", "flour"],
            "quantity": ["1 tbsp", "1 tbsp"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    editor_html = client.get("/skus/latte").text
    # Both rows stored canonically as 15 (ml for milk, g for flour); the
    # shorthand itself never reaches the stored value.
    assert editor_html.count('value="15"') == 2
    assert 'value="1 tbsp"' not in editor_html


# --- AC: shorthand vocabulary (tbsp, tsp, pinch, knob, pepper grind) --------


def test_shorthand_vocabulary_covers_the_thai_spoon_measures(
    tmp_path: Path,
) -> None:
    """Every measure the partner actually thinks in converts: 1 tsp → 5,
    2 knobs → 20, 1 pinch → 2, 1 pepper grind → 0.2. (A recipe may use the
    same ingredient in several stages, so four milk rows is legal.)
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus/latte/recipe",
        data={
            "ingredient_sku_id": ["milk", "milk", "milk", "milk"],
            "quantity": ["1 tsp", "2 knobs", "1 pinch", "1 pepper grind"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    editor_html = client.get("/skus/latte").text
    for stored in ('value="5"', 'value="20"', 'value="2"', 'value="0.2"'):
        assert stored in editor_html


def test_shorthand_for_a_countable_ingredient_is_rejected_clearly(
    tmp_path: Path,
) -> None:
    """A spoon of latte (a SKU with no confirmed g/ml unit) has no meaning;
    the save is rejected with an error naming the problem rather than
    guessing a conversion — a wrong guess is silent margin corruption.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus/latte/recipe",
        data={"ingredient_sku_id": ["latte"], "quantity": ["1 tbsp"]},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "tbsp" in response.text
    # Nothing landed: the stored recipe still holds beans 18 / milk 200.
    editor_html = client.get("/skus/latte").text
    assert 'value="18"' in editor_html
    assert 'value="200"' in editor_html


def test_negative_or_zero_quantities_are_rejected(tmp_path: Path) -> None:
    """A negative or zero quantity is never a real recipe row, only a typo —
    and a negative row would silently *subtract* from COGS. The save is
    rejected and the stored recipe is untouched.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    for bad_qty in ("-5", "0"):
        response = client.post(
            "/skus/latte/recipe",
            data={"ingredient_sku_id": ["beans"], "quantity": [bad_qty]},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "positive" in response.text

    editor_html = client.get("/skus/latte").text
    assert 'value="18"' in editor_html
    assert 'value="200"' in editor_html


def test_saving_an_empty_recipe_is_rejected(tmp_path: Path) -> None:
    """A recipe with no ingredient rows would be costed as zero COGS — the
    margin engine can't tell "no ingredients" from "free" — so the save is
    rejected and the stored recipe is untouched.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus/latte/recipe",
        data={"target_gross_margin_pct": "80"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "at least one ingredient" in response.text
    editor_html = client.get("/skus/latte").text
    assert 'value="18"' in editor_html
    assert 'value="200"' in editor_html


# --- AC: editable rows support add/remove/reorder ----------------------------


def test_saving_rows_in_a_new_order_persists_that_order(tmp_path: Path) -> None:
    """The rows are saved in the order they were posted — milk moved above
    beans stays above beans on the next load (and a removed row is simply
    absent from the post, so remove falls out of the same behaviour).
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus/latte/recipe",
        data={"ingredient_sku_id": ["milk", "beans"], "quantity": ["200", "18"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    editor_html = client.get("/skus/latte").text
    assert editor_html.index('value="milk" selected') < editor_html.index(
        'value="beans" selected'
    )
    # The editor offers add / remove / reorder controls on the rows.
    assert "recipe-row__add" in editor_html
    assert "recipe-row__remove" in editor_html
    assert "recipe-row__up" in editor_html


# --- AC: live cost preview shows per-row and total recipe cost ---------------


def test_recipe_preview_shows_per_row_and_total_cost(tmp_path: Path) -> None:
    """As the partner edits, the preview spells out each row's arithmetic —
    ``0.65/g × 18 g = 11.70 THB`` — and the recipe total (15.70), so a
    typo'd quantity is a visibly wrong number before save, not after.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.get(
        "/skus/latte/recipe-preview",
        params=[
            ("ingredient_sku_id", "beans"),
            ("quantity", "18"),
            ("ingredient_sku_id", "milk"),
            ("quantity", "200"),
        ],
    )

    assert response.status_code == 200
    html = response.text
    # Per-row derivation: unit price × quantity = row cost.
    assert "0.65" in html
    assert "11.70" in html
    # Milk's row: 200 × 0.02 = 4.00.
    assert "4.00" in html
    # The total below the rows: 11.70 + 4.00 = 15.70.
    assert "15.70" in html
    # The editor page wires its rows to this preview endpoint.
    editor_html = client.get("/skus/latte").text
    assert "/skus/latte/recipe-preview" in editor_html


def test_recipe_preview_converts_shorthand_too(tmp_path: Path) -> None:
    """The preview applies the same shorthand conversion as the save —
    ``1 tbsp`` of milk previews as 15 ml → 0.30 THB — so what the partner
    sees while typing is exactly what saving would store.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.get(
        "/skus/latte/recipe-preview",
        params=[("ingredient_sku_id", "milk"), ("quantity", "1 tbsp")],
    )

    assert response.status_code == 200
    assert "15" in response.text  # the converted quantity
    assert "0.30" in response.text  # 15 × 0.02


# --- AC: target gross margin % settable per recipe ---------------------------


def test_target_gross_margin_is_settable_and_flags_the_review(
    tmp_path: Path,
) -> None:
    """Setting the latte's target margin to 90% persists with the recipe —
    and since yesterday's 120 THB latte actually ran 86.92%, the next
    review flags it below target (the engine already knew how; this
    surfaces the input).
    """
    app = _recipe_app(tmp_path, today=date(2026, 7, 2))
    _seed_sale(app.state.db_path, item_id="i-latte", day=date(2026, 7, 1), price="120")
    client = _authed_client(app)

    response = client.post(
        "/skus/latte/recipe",
        data={
            "ingredient_sku_id": ["beans", "milk"],
            "quantity": ["18", "200"],
            "target_gross_margin_pct": "90",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    # The editor round-trips the saved target.
    editor_html = client.get("/skus/latte").text
    assert 'name="target_gross_margin_pct"' in editor_html
    assert 'value="90"' in editor_html
    # The review flags the latte: actual 86.92% < target 90%.
    review_html = client.get("/").text
    assert "Below target margin" in review_html
    assert "86.92" in review_html


# --- AC: creating a new SKU opens the same editor with empty rows -----------


def test_creating_a_sku_lands_in_the_editor_with_empty_rows(
    tmp_path: Path,
) -> None:
    """``POST /skus`` with (sku_id, name, unit, price) creates the SKU —
    priced per-unit net, ready to use as an ingredient — and redirects to
    the same recipe editor every SKU gets, with no ingredient rows yet.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus",
        data={"sku_id": "oat-milk", "name": "Oat Milk", "unit": "ml", "price": "0.09"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/skus/oat-milk"
    editor_html = client.get("/skus/oat-milk").text
    # The same editor, empty: the recipe form is offered with no stored rows.
    assert 'action="/skus/oat-milk/recipe"' in editor_html
    assert "selected" not in editor_html.split('id="recipe-rows"')[1].split("</ol>")[0]
    # The given price is stored as the per-unit net cost.
    assert "0.09" in editor_html
    # The new SKU joins the catalog: visible in the SKU view and usable as
    # an ingredient in any picker.
    assert "oat-milk" in client.get("/skus").text
    latte_html = client.get("/skus/latte").text
    assert 'value="oat-milk"' in latte_html


# --- AC: "Create new SKU…" inline sub-form creates and auto-selects ---------


def test_inline_create_sku_returns_a_picker_with_the_new_sku_selected(
    tmp_path: Path,
) -> None:
    """The picker offers a "Create new SKU…" option that reveals a tiny
    inline sub-form (sku_id, name, unit, price). Submitting it over HTMX
    creates the ingredient and swaps back a picker with the new SKU
    already selected — the partner finishes the recipe without leaving
    the page.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    # The editor offers the inline-create affordance in each row.
    editor_html = client.get("/skus/latte").text
    assert 'value="__create__"' in editor_html
    assert "Create new SKU" in editor_html
    assert 'hx-post="/skus"' in editor_html

    # Submitting the sub-form over HTMX returns a replacement picker with
    # the new ingredient selected, not a page redirect.
    response = client.post(
        "/skus",
        data={"sku_id": "oat-milk", "name": "Oat Milk", "unit": "ml", "price": "0.09"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    fragment = response.text
    assert 'name="ingredient_sku_id"' in fragment
    assert 'value="oat-milk" selected' in fragment
    # The SKU really exists now — it is priced and usable everywhere.
    assert 'value="oat-milk"' in client.get("/skus/latte").text


# --- AC: "New SKU" button and item coverage's inline option, same editor ----


def test_new_sku_button_and_item_coverage_option_open_the_same_editor(
    tmp_path: Path,
) -> None:
    """The SKU view's "New SKU" button and an unmapped item's
    "create new SKU…" option both lead to the same create form — and the
    item-coverage path carries the item along, so the created SKU is
    mapped to it in the same stroke.
    """
    app = _recipe_app(tmp_path)
    _seed_menu(
        app.state.db_path,
        [
            _menu_item("i-latte", "Cafe Latte", "120"),
            _menu_item("i-mystery", "Mystery Soda", "60"),
        ],
    )
    client = _authed_client(app)

    # Entry point 1: the SKU view's New SKU button.
    assert 'href="/skus/new"' in client.get("/skus").text
    # Entry point 2: the unmapped item's inline option, carrying the item.
    items_html = client.get("/items").text
    assert 'href="/skus/new?item_id=i-mystery"' in items_html

    # Both land on the same create form, posting to POST /skus.
    form_html = client.get("/skus/new", params={"item_id": "i-mystery"}).text
    assert 'action="/skus"' in form_html
    for field in ("sku_id", "name", "unit", "price"):
        assert f'name="{field}"' in form_html

    # Creating from the item path maps the item and opens the new editor.
    response = client.post(
        "/skus",
        data={
            "sku_id": "soda",
            "name": "House Soda",
            "unit": "ml",
            "price": "0.03",
            "item_id": "i-mystery",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/skus/soda"
    mapped_html = client.get("/items", params={"item": "i-mystery"}).text
    assert "item-row--unmapped" not in mapped_html
    assert "soda" in mapped_html


# --- AC: all edits gated behind existing auth middleware ---------------------


def test_recipe_edit_routes_require_auth(tmp_path: Path) -> None:
    """Every new write surface — saving a recipe, creating a SKU, the
    create form — redirects an unauthenticated request to ``/login``,
    exactly like the rest of the app. Nothing lands.
    """
    app = _recipe_app(tmp_path)
    client = TestClient(app)

    for method, url in (
        ("post", "/skus/latte/recipe"),
        ("post", "/skus"),
        ("get", "/skus/new"),
        ("get", "/skus/latte/recipe-preview"),
    ):
        response = getattr(client, method)(url, follow_redirects=False)
        assert response.status_code == 302, f"{method} {url}"
        assert response.headers["location"] == "/login", f"{method} {url}"


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
