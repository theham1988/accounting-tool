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

import re
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
    now_epoch: int | None = None,
):
    """A ``create_app`` wired against real YAML seeded into a fresh SQLite DB.

    ``now_epoch`` pins the audit clock — needed by tests that assert
    as-of-date pricing, where *when* an edit was made decides which days
    it re-costs (Wave 2 slice 1).
    """
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
        now_epoch=now_epoch,
    )


def _first_revert_entry_id(audit_html: str) -> str:
    """The entry id of the newest entry's Revert form on the audit page."""
    match = re.search(r'action="/audit/(\d+)/revert"', audit_html)
    assert match is not None, "no per-entry revert form on the audit page"
    return match.group(1)


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
    today) shows a margin of 95 − 50 × 0.177570 = 86.12 on that day's
    review, without touching YAML.

    Under as-of-date pricing (Wave 2 slice 1) the edit takes effect on the
    partner-facing day it was saved — the app's *today* — so it governs
    today's sales onward (tomorrow's 9am review of today) and never
    re-costs yesterday.
    """
    today = date(2026, 7, 2)
    app = _build_app(
        tmp_path,
        recipes_yaml=_RECIPES_YAML,
        costs_yaml=_COSTS_YAML,
        today=today,
        now_epoch=int(datetime(2026, 7, 2, 12, tzinfo=timezone.utc).timestamp()),
    )
    _seed_sale(app.state.db_path, item_id="i-croissant", day=today, price="95")
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

    # The edit's own day reflects the new cost: 95 − 8.88 = 86.12.
    review_html = client.get("/review", params={"day": today.isoformat()}).text
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
    # Under as-of-date pricing an uploaded cost takes effect on the app's
    # *today* (the partner-facing save date), so tests that want the new
    # cost to govern a sale put the sale on today.
    app = _build_app(
        tmp_path,
        recipes_yaml=_UPLOAD_RECIPES_YAML,
        costs_yaml=_UPLOAD_COSTS_YAML,
        today=date(2026, 7, 2),
        now_epoch=int(datetime(2026, 7, 2, 12, tzinfo=timezone.utc).timestamp()),
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
    _seed_sale(app.state.db_path, item_id="i-croissant", day=date(2026, 7, 2), price="95")
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
    # The cost landed: 95 − 50 × 0.177570 = 86.12 on the upload day's review.
    review_html = client.get("/review", params={"day": "2026-07-02"}).text
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
  egg: { price: "4.00", updated_at: "2026-06-01" }  # 120/30
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


# --- AC: picker offers only purchasable SKUs and preps (issue #35) ----------


def test_ingredient_picker_offers_only_purchasables_and_preps(
    tmp_path: Path,
) -> None:
    """The picker offers every purchasable SKU — including flour, costed but
    not yet used by any recipe — and never a sold-only dish: ``latte`` is
    produced (it has a recipe) and not a prep, so it cannot honestly be an
    ingredient and is absent from its own editor's picker.

    This deliberately inverts the pre-#35 test that asserted ``latte``
    appears: under SKU roles a mis-click can no longer pick a dish as an
    ingredient and silently produce a garbage cost.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    html = client.get("/skus/latte").text

    picker = html.split('name="ingredient_sku_id"')[1].split("</select>")[0]
    # Every purchasable SKU is offered, even ones no recipe uses yet.
    for sku_id in ("beans", "milk", "flour"):
        assert f'value="{sku_id}"' in picker
    # The sold-only dish is not: it is produced and not a prep.
    assert 'value="latte"' not in picker
    # The ingredient reference is a dropdown, not free text: no text input
    # carries the ingredient_sku_id name.
    assert '<input type="text" name="ingredient_sku_id"' not in html
    # Only purchasables (plus the inline-create affordance's sentinel)
    # appear as options.
    option_values = [
        part.split('"')[0]
        for part in picker.split('value="')[1:]
    ]
    known = {"beans", "milk", "flour", "egg", "__create__"}
    assert set(option_values) <= known


def test_ingredient_picker_offers_preps(tmp_path: Path) -> None:
    """A prep — a produced SKU declared usable inside other recipes — is
    offered by the picker alongside the purchasables.

    Worked example. ``oba-sauce`` has its own recipe and is consumed by the
    poke bowl, so the seed auto-flags it prep (usage is the declaration).
    Opening the poke bowl's editor offers oba-sauce as an ingredient; the
    poke bowl itself (produced, not prep) is absent.
    """
    prep_recipes_yaml = """
recipes:
  - sku_id: oba-sauce
    name: Oba Sauce
    segment: cafe
    ingredients:
      - { sku_id: soy, quantity: "30" }
  - sku_id: poke-bowl
    name: Poke Bowl
    segment: cafe
    ingredients:
      - { sku_id: oba-sauce, quantity: "25" }
      - { sku_id: rice, quantity: "100" }

mappings:
  - { item_id: i-poke, sku_id: poke-bowl }
"""
    prep_costs_yaml = """
costs:
  soy: { price: "0.05", updated_at: "2026-06-01" }  # 1 l bottle
  rice: { price: "0.03", updated_at: "2026-06-01" }  # 5 kg bag
"""
    app = _build_app(
        tmp_path, recipes_yaml=prep_recipes_yaml, costs_yaml=prep_costs_yaml
    )
    client = _authed_client(app)

    html = client.get("/skus/poke-bowl").text

    picker = html.split('name="ingredient_sku_id"')[1].split("</select>")[0]
    # The prep and the purchasables are offered...
    for sku_id in ("oba-sauce", "soy", "rice"):
        assert f'value="{sku_id}"' in picker
    # ...the sold-only dish is not.
    assert 'value="poke-bowl"' not in picker


# --- AC: sold-only dishes are rejected server-side at recipe save (issue #35)


def test_saving_a_recipe_with_a_dish_as_ingredient_is_rejected(
    tmp_path: Path,
) -> None:
    """The picker filter is not merely cosmetic: a crafted POST that names a
    sold-only dish (``latte``) as an ingredient is rejected server-side
    with a message naming the offending SKU, and nothing lands.

    Worked example. ``flour`` is purchasable; a POST gives it a recipe
    containing the latte. The save returns 400, the message names latte,
    and flour still has no recipe (it stays purchasable).
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus/flour/recipe",
        data={"ingredient_sku_id": ["latte"], "quantity": ["50"]},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "latte" in response.text
    # The message says why: a dish is not an allowed ingredient.
    assert "ingredient" in response.text.lower()
    # Nothing landed: flour still has no recipe of its own.
    editor_html = client.get("/skus/flour").text
    rows = editor_html.split('id="recipe-rows"')[1].split("</ol>")[0]
    assert "selected" not in rows


# --- AC: cycle rejection at recipe save, direct or transitive (issue #35) ---

# Two preps in a chain: marinade goes into oba-sauce, oba-sauce into the
# poke bowl. Both are auto-flagged prep on seed (usage is the declaration),
# so both are legal picker options — which is exactly what makes a cycle
# *possible* to attempt and why the save must catch it.
_CYCLE_RECIPES_YAML = """
recipes:
  - sku_id: marinade
    name: House Marinade
    segment: cafe
    ingredients:
      - { sku_id: soy, quantity: "50" }
  - sku_id: oba-sauce
    name: Oba Sauce
    segment: cafe
    ingredients:
      - { sku_id: marinade, quantity: "30" }
  - sku_id: poke-bowl
    name: Poke Bowl
    segment: cafe
    ingredients:
      - { sku_id: oba-sauce, quantity: "25" }

mappings:
  - { item_id: i-poke, sku_id: poke-bowl }
"""

_CYCLE_COSTS_YAML = """
costs:
  soy: { price: "0.05", updated_at: "2026-06-01" }  # 1 l bottle
"""


def test_saving_a_recipe_that_would_create_a_transitive_cycle_is_rejected(
    tmp_path: Path,
) -> None:
    """A save that would make a prep contain itself through another prep is
    rejected with a message naming the loop, and nothing lands.

    Worked example. Oba-sauce already contains marinade. Editing *marinade*
    to contain oba-sauce would close the loop marinade → oba-sauce →
    marinade — costing could never terminate. The save returns 400, the
    message spells out the loop path, and marinade's stored recipe still
    holds its original soy row.
    """
    app = _build_app(
        tmp_path, recipes_yaml=_CYCLE_RECIPES_YAML, costs_yaml=_CYCLE_COSTS_YAML
    )
    client = _authed_client(app)

    response = client.post(
        "/skus/marinade/recipe",
        data={"ingredient_sku_id": ["oba-sauce"], "quantity": ["30"]},
        follow_redirects=False,
    )

    assert response.status_code == 400
    # The message names the loop: both participants, in path order.
    assert "marinade" in response.text
    assert "oba-sauce" in response.text
    assert "cycle" in response.text.lower() or "loop" in response.text.lower()
    # Nothing landed: marinade still holds its original soy row.
    editor_html = client.get("/skus/marinade").text
    assert 'value="soy" selected' in editor_html
    assert 'value="oba-sauce" selected' not in editor_html


def test_saving_a_recipe_that_contains_itself_is_rejected(tmp_path: Path) -> None:
    """The direct flavour of the same rule: a prep cannot contain itself.

    Oba-sauce is a prep, so it is a legal picker option in other recipes —
    but a save putting oba-sauce inside its own recipe is the one-step
    loop, rejected with the same loop-naming message.
    """
    app = _build_app(
        tmp_path, recipes_yaml=_CYCLE_RECIPES_YAML, costs_yaml=_CYCLE_COSTS_YAML
    )
    client = _authed_client(app)

    response = client.post(
        "/skus/oba-sauce/recipe",
        data={"ingredient_sku_id": ["oba-sauce"], "quantity": ["10"]},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "oba-sauce" in response.text
    assert "cycle" in response.text.lower() or "loop" in response.text.lower()
    # The stored recipe is untouched.
    editor_html = client.get("/skus/oba-sauce").text
    assert 'value="marinade" selected' in editor_html


# --- AC: prep flag editable from the recipe editor, audited, revertable -----


def test_prep_toggle_persists_and_widens_the_picker(tmp_path: Path) -> None:
    """The recipe editor carries a prep checkbox; ticking it and saving
    persists the flag, and the SKU immediately becomes offerable as an
    ingredient in other editors.

    Worked example. The latte starts as a sold-only dish: its editor's
    checkbox is unchecked and flour's picker does not offer it. The partner
    declares it a prep (batch-brewed latte base, say) and saves; reopening
    shows the box checked, and flour's picker now offers latte.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    editor_html = client.get("/skus/latte").text
    assert 'name="prep"' in editor_html
    checkbox = editor_html.split('name="prep"')[1].split(">")[0]
    assert "checked" not in checkbox
    assert 'value="latte"' not in client.get("/skus/flour").text

    response = client.post(
        "/skus/latte/recipe",
        data={
            "ingredient_sku_id": ["beans", "milk"],
            "quantity": ["18", "150"],
            "prep": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    editor_html = client.get("/skus/latte").text
    checkbox = editor_html.split('name="prep"')[1].split(">")[0]
    assert "checked" in checkbox
    assert 'value="latte"' in client.get("/skus/flour").text


def test_prep_toggle_is_audited_and_revertable(tmp_path: Path) -> None:
    """Flipping the prep flag is a config edit like any other: it lands in
    the audit log with before/after recipe snapshots, and the per-entry
    revert restores the flag (latte drops back out of other pickers).
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    client.post(
        "/skus/latte/recipe",
        data={
            "ingredient_sku_id": ["beans", "milk"],
            "quantity": ["18", "150"],
            "prep": "1",
        },
        follow_redirects=False,
    )

    audit_html = client.get("/audit").text
    assert "prep" in audit_html
    entry_id = _first_revert_entry_id(audit_html)
    response = client.post(f"/audit/{entry_id}/revert", follow_redirects=False)
    assert response.status_code == 303

    editor_html = client.get("/skus/latte").text
    checkbox = editor_html.split('name="prep"')[1].split(">")[0]
    assert "checked" not in checkbox
    assert 'value="latte"' not in client.get("/skus/flour").text


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
    """A spoon of egg (a purchasable whose g/ml unit is unconfirmed — its
    "120/30" cost comment derives no unit) has no meaning; the save is
    rejected with an error naming the problem rather than guessing a
    conversion — a wrong guess is silent margin corruption.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus/latte/recipe",
        data={"ingredient_sku_id": ["egg"], "quantity": ["1 tbsp"]},
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


# --- AC: yield field rendered in the output SKU's own unit (issue #34) ------


def test_recipe_editor_renders_yield_field_in_the_output_skus_unit(
    tmp_path: Path,
) -> None:
    """The editor offers a yield input labelled in the output SKU's own
    unit — "yields ___ g" for a gram-denominated sauce, and the "units"
    fallback for a SKU whose unit was never confirmed.

    Issue #34: yield is a decimal quantity of the output SKU, so the label
    must say *what* is being counted. The latte (unit never confirmed)
    falls back to "units"; a partner-created gram sauce reads "g".
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)

    # The latte's unit is unconfirmed -> generic "units" label.
    latte_html = client.get("/skus/latte").text
    assert 'name="yield_qty"' in latte_html
    assert 'yield-field__unit">units<' in latte_html

    # A partner-created g-denominated SKU labels its yield in grams.
    client.post(
        "/skus",
        data={"sku_id": "ahi-sauce", "name": "Ahi Sauce", "unit": "g", "price": "0.20"},
        follow_redirects=False,
    )
    sauce_html = client.get("/skus/ahi-sauce").text
    assert 'name="yield_qty"' in sauce_html
    assert 'yield-field__unit">g<' in sauce_html


# --- AC: estimated yields are labelled as estimates (issue #34) --------------

# A prep fixture: the ahi sauce is consumed by the poke bowl, so the seed
# backfills its yield with the input sum (124 g) marked estimated. The bowl
# itself is a plain dish whose yield 1 stays measured.
_PREP_RECIPES_YAML = """
recipes:
  - sku_id: sauce-ahi
    name: Ahi Sauce
    segment: cafe
    ingredients:
      - { sku_id: soy-sauce, quantity: "100" }
      - { sku_id: mirin, quantity: "24" }
  - sku_id: poke-bowl
    name: Poke Bowl
    segment: cafe
    ingredients:
      - { sku_id: sauce-ahi, quantity: "25" }
      - { sku_id: rice, quantity: "200" }

mappings:
  - { item_id: i-poke, sku_id: poke-bowl }
"""

_PREP_COSTS_YAML = """
costs:
  soy-sauce: { price: "0.05", updated_at: "2026-06-01" }  # 1 l bottle
  mirin: { price: "0.30", updated_at: "2026-06-01" }  # 500 ml bottle
  rice: { price: "0.04", updated_at: "2026-06-01" }  # 5 kg bag
"""


def _prep_app(tmp_path: Path, today: date | None = None):  # type: ignore[no-untyped-def]
    return _build_app(
        tmp_path,
        recipes_yaml=_PREP_RECIPES_YAML,
        costs_yaml=_PREP_COSTS_YAML,
        today=today or date(2026, 7, 2),
    )


def test_estimated_yield_is_labelled_and_a_measured_one_is_not(
    tmp_path: Path,
) -> None:
    """The sauce's backfilled yield (124, the input sum) renders with an
    "estimated" badge; the poke bowl's migrated yield (1, fixed) does not.

    Issue #34 AC: "an unmeasured prep's yield ... clearly labelled an
    estimate", so a partner reading the editor knows the number is the
    no-loss default, not a weighed batch.
    """
    app = _prep_app(tmp_path)
    client = _authed_client(app)

    sauce_html = client.get("/skus/sauce-ahi").text
    assert 'name="yield_qty"' in sauce_html
    assert 'value="124"' in sauce_html
    assert "estimated" in sauce_html.split('class="yield-field"')[1].split("</label>")[0]

    bowl_html = client.get("/skus/poke-bowl").text
    assert 'value="1"' in bowl_html
    assert "estimated" not in bowl_html.split('class="yield-field"')[1].split("</label>")[0]


# --- AC: a measured yield replaces the estimate and drops the label ---------


def test_entering_a_measured_yield_replaces_the_estimate(tmp_path: Path) -> None:
    """The partner weighs an ahi batch at 61 g and types it in; the saved
    yield is 61 marked measured, the badge disappears, and the margin uses
    the measured number.

    Issue #34 AC: "Entering a measured yield replaces the estimate and
    drops the estimate label."
    """
    app = _prep_app(tmp_path)
    client = _authed_client(app)

    response = client.post(
        "/skus/sauce-ahi/recipe",
        data={
            "ingredient_sku_id": ["soy-sauce", "mirin"],
            "quantity": ["100", "24"],
            "yield_qty": "61",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    editor_html = client.get("/skus/sauce-ahi").text
    assert 'value="61"' in editor_html
    yield_block = editor_html.split('class="yield-field"')[1].split("</label>")[0]
    assert "estimated" not in yield_block


def test_row_edits_recompute_an_estimated_yield_but_not_a_measured_one(
    tmp_path: Path,
) -> None:
    """Editing an estimated prep's rows re-derives the estimate from the new
    input sum; a measured yield is untouched by row edits.

    Issue #34 AC: "Editing a recipe's ingredient rows recomputes an
    estimated yield; a measured (partner-entered) yield is untouched by row
    edits." The form posts the yield it displayed; the server tells a
    partner-typed measurement (value changed) from an untouched estimate
    (value equal to the stored one) — no client JS involved.
    """
    app = _prep_app(tmp_path)
    client = _authed_client(app)

    # Estimated: bumping soy 100 -> 150 recomputes the estimate 124 -> 174.
    client.post(
        "/skus/sauce-ahi/recipe",
        data={
            "ingredient_sku_id": ["soy-sauce", "mirin"],
            "quantity": ["150", "24"],
            "yield_qty": "124",  # displayed value, untouched by the partner
        },
        follow_redirects=False,
    )
    editor_html = client.get("/skus/sauce-ahi").text
    assert 'value="174"' in editor_html
    yield_block = editor_html.split('class="yield-field"')[1].split("</label>")[0]
    assert "estimated" in yield_block

    # Measured: weigh the batch (160 g), then edit a row — 160 stays.
    client.post(
        "/skus/sauce-ahi/recipe",
        data={
            "ingredient_sku_id": ["soy-sauce", "mirin"],
            "quantity": ["150", "24"],
            "yield_qty": "160",
        },
        follow_redirects=False,
    )
    client.post(
        "/skus/sauce-ahi/recipe",
        data={
            "ingredient_sku_id": ["soy-sauce", "mirin"],
            "quantity": ["150", "30"],
            "yield_qty": "160",  # displayed value, untouched
        },
        follow_redirects=False,
    )
    editor_html = client.get("/skus/sauce-ahi").text
    assert 'value="160"' in editor_html
    yield_block = editor_html.split('class="yield-field"')[1].split("</label>")[0]
    assert "estimated" not in yield_block


# --- AC: zero/negative yields rejected with a clear message ------------------


def test_zero_or_negative_yield_is_rejected_with_a_clear_message(
    tmp_path: Path,
) -> None:
    """A yield of 0 (or below) is a typo, never a recipe — the engine would
    divide by it. The save is refused with a message naming the problem,
    and the stored recipe is unchanged.
    """
    app = _prep_app(tmp_path)
    client = _authed_client(app)

    for bad in ("0", "-5"):
        response = client.post(
            "/skus/sauce-ahi/recipe",
            data={
                "ingredient_sku_id": ["soy-sauce", "mirin"],
                "quantity": ["100", "24"],
                "yield_qty": bad,
            },
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "Yield must be greater than zero" in response.text

    # The stored recipe still carries the backfilled estimate.
    editor_html = client.get("/skus/sauce-ahi").text
    assert 'value="124"' in editor_html


# --- AC: yield changes are audited and revertable -----------------------------


def test_yield_change_is_audited_and_revertable(tmp_path: Path) -> None:
    """Measuring the sauce at 61 g writes an audit entry whose revert
    restores the previous estimated yield — badge and all.

    Issue #34 AC: "Changing a yield writes an audit entry and is
    revertable, like any other recipe edit."
    """
    app = _prep_app(tmp_path)
    client = _authed_client(app)

    client.post(
        "/skus/sauce-ahi/recipe",
        data={
            "ingredient_sku_id": ["soy-sauce", "mirin"],
            "quantity": ["100", "24"],
            "yield_qty": "61",
        },
        follow_redirects=False,
    )

    audit_html = client.get("/audit").text
    assert "sauce-ahi" in audit_html
    entry_id = _first_revert_entry_id(audit_html)
    revert = client.post(f"/audit/{entry_id}/revert", follow_redirects=False)
    assert revert.status_code == 303

    # The estimate is back: 124, labelled estimated.
    editor_html = client.get("/skus/sauce-ahi").text
    assert 'value="124"' in editor_html
    yield_block = editor_html.split('class="yield-field"')[1].split("</label>")[0]
    assert "estimated" in yield_block


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
    # The swapped-in picker obeys the same role filter as the editor page:
    # the just-created purchasable is offered, the sold-only dish is not.
    assert 'value="latte"' not in fragment
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


# =============================================================================
# Audit log + revert + daily review diff link (Wave 1.5, Slice 5 / issue 27)
# =============================================================================

# The safety net that replaces the code-review gate removed by ADR-0003: every
# config edit writes an audit_log row; GET /audit renders the paper trail with
# per-change and per-session revert; the 9am review links the diff.


# --- AC: every config edit writes an audit_log row with who/when/old/new ----


def test_saving_a_cost_is_recorded_in_the_audit_log(tmp_path: Path) -> None:
    """Given Daniel enters pack price 380 THB for a 2 kg block of butter with
    VAT inclusive, the audit log shows the edit: butter's cost, changed by
    daniel, from the seeded 0.200000 THB/g net to the new 0.177570 — the
    paper trail ADR-0003 promises in place of the removed code-review gate.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app, assignee_id="daniel")

    client.post(
        "/skus/butter/cost",
        data={"pack_price": "380", "pack_quantity": "2000", "vat_inclusive": "1"},
        follow_redirects=False,
    )

    response = client.get("/audit")
    assert response.status_code == 200
    html = response.text
    # The entry names the changed row, the actor, and both values.
    assert "butter" in html
    assert "daniel" in html
    assert "0.200000" in html  # old: the seeded net price
    assert "0.177570" in html  # new: 380 / 2000 / 1.07


def test_saving_a_recipe_is_recorded_in_the_audit_log(tmp_path: Path) -> None:
    """Given Noi trims the latte's milk from 200 ml to 150 ml, the audit log
    shows the recipe edit — changed by noi, with the old and new ingredient
    rows both visible so a wrong quantity is traceable to its edit.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app, assignee_id="noi")

    client.post(
        "/skus/latte/recipe",
        data={"ingredient_sku_id": ["beans", "milk"], "quantity": ["18", "150"]},
        follow_redirects=False,
    )

    html = client.get("/audit").text
    assert "latte" in html
    assert "noi" in html
    assert "150" in html  # the new milk quantity
    assert "200" in html  # the old milk quantity


def test_mapping_changes_are_recorded_in_the_audit_log(tmp_path: Path) -> None:
    """A confirmed upload lands a mapping (mystery soda -> soda SKU) and a
    cost — and both edits appear in the audit log, attributed to the partner
    who confirmed, so a bulk load leaves the same paper trail as a one-row
    edit.
    """
    app = _upload_app(tmp_path)
    client = _authed_client(app, assignee_id="noi")

    client.post("/upload", data={"csv_text": _FILLED_CSV, "confirm": "1"})

    html = client.get("/audit").text
    # The mapping edit: the item gained its SKU.
    assert "i-mystery" in html
    assert "soda" in html
    # The cost edit, in the same trail, same actor.
    assert "butter" in html
    assert "0.177570" in html
    assert "noi" in html


def test_creating_a_sku_is_recorded_in_the_audit_log(tmp_path: Path) -> None:
    """Creating a SKU from an unmapped item's "create new SKU…" path is
    three edits in one stroke — the SKU, its price, the item's mapping —
    and all three land in the audit log.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app, assignee_id="daniel")

    client.post(
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

    html = client.get("/audit").text
    # The SKU creation (no old value — it is a created row).
    assert "House Soda" in html
    # Its per-unit price.
    assert "0.03" in html
    # The mapping made in the same stroke.
    assert "i-mystery" in html
    assert "daniel" in html


# --- AC: POST /audit/{entry_id}/revert undoes a single edit; logged ---------


def test_reverting_an_audit_entry_undoes_that_change_and_is_logged(
    tmp_path: Path,
) -> None:
    """Given Daniel fat-fingers butter's pack price (3800 instead of 380),
    the audit page offers a Revert button on that entry; clicking it
    restores the seeded 0.200000 THB/g — tomorrow's review shows the old
    margin again — and the revert itself appears as a new log entry, so
    even the undo has a paper trail.
    """
    today = date(2026, 7, 2)
    app = _build_app(
        tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML, today=today
    )
    _seed_sale(app.state.db_path, item_id="i-croissant", day=date(2026, 7, 1), price="95")
    client = _authed_client(app, assignee_id="daniel")
    client.post(
        "/skus/butter/cost",
        data={"pack_price": "3800", "pack_quantity": "2000", "vat_inclusive": "1"},
        follow_redirects=False,
    )

    # The audit page offers a per-entry revert control.
    audit_html = client.get("/audit").text
    assert "/revert" in audit_html
    entry_id = _first_revert_entry_id(audit_html)
    response = client.post(f"/audit/{entry_id}/revert", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/audit"
    # The bad cost is gone: the editor shows the seeded net again...
    editor_html = client.get("/skus/butter").text
    assert "1.775701" not in editor_html  # 3800 / 2000 / 1.07
    assert "0.20" in editor_html
    # ...and the review margin is back: 95 − 50 × 0.20 = 85.00.
    review_html = client.get("/").text
    assert "85.00" in review_html
    # The revert itself is logged: the trail now holds two entries.
    reverted_html = client.get("/audit").text
    assert reverted_html.count('<tr class="audit-row') == 2


def test_reverting_a_sku_creation_removes_the_sku(tmp_path: Path) -> None:
    """A created row has no before-state, so reverting the creation deletes
    it: the accidentally-created SKU disappears from the catalog and its
    editor 404s — and the removal is logged like any other change.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)
    client.post(
        "/skus",
        data={"sku_id": "oops", "name": "Oops", "unit": "g", "price": ""},
        follow_redirects=False,
    )
    assert client.get("/skus/oops").status_code == 200

    audit_html = client.get("/audit").text
    entry_id = _first_revert_entry_id(audit_html)
    response = client.post(f"/audit/{entry_id}/revert", follow_redirects=False)

    assert response.status_code == 303
    assert client.get("/skus/oops").status_code == 404
    assert "oops" not in client.get("/skus").text
    # The removal is logged: the trail shows the entry marked as removed.
    reverted_html = client.get("/audit").text
    assert "removed" in reverted_html


def test_reverting_a_recipe_edit_restores_the_previous_rows(tmp_path: Path) -> None:
    """Reverting a recipe edit restores the previous ingredient rows whole —
    the latte goes back to beans 18 / milk 200 after a bad save changed
    both quantities.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app)
    client.post(
        "/skus/latte/recipe",
        data={"ingredient_sku_id": ["beans", "milk"], "quantity": ["25", "100"]},
        follow_redirects=False,
    )

    audit_html = client.get("/audit").text
    entry_id = _first_revert_entry_id(audit_html)
    response = client.post(f"/audit/{entry_id}/revert", follow_redirects=False)

    assert response.status_code == 303
    editor_html = client.get("/skus/latte").text
    assert 'value="18"' in editor_html
    assert 'value="200"' in editor_html
    assert 'value="25"' not in editor_html


def test_reverting_an_older_entry_keeps_later_edits_to_other_fields(
    tmp_path: Path,
) -> None:
    """"Exactly that one change": Noi bumps the latte's quantities (edit 1),
    then separately sets a 90% target margin (edit 2). Reverting edit 1
    restores the original quantities — but edit 2's target margin survives,
    because the revert touches only the fields edit 1 changed.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app, assignee_id="noi")
    client.post(
        "/skus/latte/recipe",
        data={"ingredient_sku_id": ["beans", "milk"], "quantity": ["25", "100"]},
        follow_redirects=False,
    )
    quantities_entry_id = _first_revert_entry_id(client.get("/audit").text)
    client.post(
        "/skus/latte/recipe",
        data={
            "ingredient_sku_id": ["beans", "milk"],
            "quantity": ["25", "100"],
            "target_gross_margin_pct": "90",
        },
        follow_redirects=False,
    )

    response = client.post(
        f"/audit/{quantities_entry_id}/revert", follow_redirects=False
    )

    assert response.status_code == 303
    editor_html = client.get("/skus/latte").text
    # The quantity change is undone...
    assert 'value="18"' in editor_html
    assert 'value="200"' in editor_html
    assert 'value="25"' not in editor_html
    # ...but the later, separate target-margin edit survives the revert.
    assert 'value="90"' in editor_html


def test_a_revert_carries_the_partners_reason_into_the_log(tmp_path: Path) -> None:
    """The revert form offers an optional reason; whatever the partner types
    lands on the revert's own audit entry — ADR-0003's record of intent:
    who changed what, when, and why.
    """
    app = _recipe_app(tmp_path)
    client = _authed_client(app, assignee_id="daniel")
    client.post(
        "/skus/beans/cost",
        data={"pack_price": "800", "pack_quantity": "1000"},
        follow_redirects=False,
    )

    audit_html = client.get("/audit").text
    assert 'name="reason"' in audit_html  # the form offers the reason input
    entry_id = _first_revert_entry_id(audit_html)
    client.post(
        f"/audit/{entry_id}/revert",
        data={"reason": "fat-fingered the pack price"},
        follow_redirects=False,
    )

    assert "fat-fingered the pack price" in client.get("/audit").text


# --- AC: "Revert this session" undoes all edits sharing a session_id --------


def test_reverting_a_session_undoes_all_its_edits_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The panic undo: Daniel broke something during his browser session but
    doesn't know what. "Revert this session" undoes everything he saved in
    it — a beans cost and a latte recipe edit — while Noi's flour cost,
    saved from her own login, stays exactly as she left it.
    """
    app = _recipe_app(tmp_path)
    noi = _authed_client(app, assignee_id="noi")
    noi.post(
        "/skus/flour/cost",
        data={"pack_price": "100", "pack_quantity": "1000"},
        follow_redirects=False,
    )
    daniel = _authed_client(app, assignee_id="daniel")
    daniel.post(
        "/skus/beans/cost",
        data={"pack_price": "800", "pack_quantity": "1000"},
        follow_redirects=False,
    )
    daniel.post(
        "/skus/latte/recipe",
        data={"ingredient_sku_id": ["beans", "milk"], "quantity": ["25", "100"]},
        follow_redirects=False,
    )

    # The audit page offers a per-session revert; the newest entries are
    # Daniel's, so the first session form on the page is his session.
    audit_html = daniel.get("/audit").text
    assert 'action="/audit/session/' in audit_html
    session_id = audit_html.split('action="/audit/session/')[1].split("/revert")[0]
    response = daniel.post(
        f"/audit/session/{session_id}/revert", follow_redirects=False
    )

    assert response.status_code == 303
    # Both of Daniel's edits are undone...
    assert "0.65" in daniel.get("/skus/beans").text  # seeded beans net
    latte_html = daniel.get("/skus/latte").text
    assert 'value="18"' in latte_html
    assert 'value="200"' in latte_html
    # ...while Noi's edit survives: 100 / 1000 = 0.10 THB/g.
    assert "0.10" in daniel.get("/skus/flour").text


# --- AC: review shows "N changes since last review" link; diff behind it ----


def test_review_links_changes_since_the_partner_last_reviewed_them(
    tmp_path: Path,
) -> None:
    """After Noi edits a cost, Daniel's 9am review shows "1 change since
    last review" linking to the audit page, where the unreviewed entry is
    highlighted. Pressing "Mark as reviewed" (an explicit POST — merely
    loading the page never moves the mark) clears the nag from his next
    load. Noi has not marked yet, so *her* review still shows it — each
    partner sanity-checks the diff for themselves.
    """
    app = _recipe_app(tmp_path)
    noi = _authed_client(app, assignee_id="noi")
    noi.post(
        "/skus/beans/cost",
        data={"pack_price": "800", "pack_quantity": "1000"},
        follow_redirects=False,
    )

    daniel = _authed_client(app, assignee_id="daniel")
    review_html = daniel.get("/").text
    assert "1 change since last review" in review_html
    assert 'href="/audit"' in review_html

    # The audit page highlights the diff the link promised — the entry he
    # has not reviewed — and offers the explicit Mark-as-reviewed control.
    audit_html = daniel.get("/audit").text
    assert "audit-row--unreviewed" in audit_html
    assert 'action="/audit/reviewed"' in audit_html
    # Viewing alone is not reviewing: the nag survives a mere page load.
    assert "1 change since last review" in daniel.get("/").text

    # Marking as reviewed clears the nag and the highlight.
    daniel.post("/audit/reviewed", follow_redirects=False)
    assert "since last review" not in daniel.get("/").text
    assert "audit-row--unreviewed" not in daniel.get("/audit").text

    # Noi hasn't marked the diff reviewed yet, so her review still nags.
    assert "1 change since last review" in noi.get("/").text


def test_the_diff_shows_changed_fields_only_with_old_and_new_values(
    tmp_path: Path,
) -> None:
    """The diff is a diff, not a row dump. Butter re-costed at 428 THB per
    2 kg VAT-inclusive derives the *same* net (0.200000) the seed already
    stored — so the diff shows the newly-recorded pack fields
    (— → 428, — → 2000) but neither the unchanged net price nor the
    unchanged VAT flag.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)
    client.post(
        "/skus/butter/cost",
        data={"pack_price": "428", "pack_quantity": "2000", "vat_inclusive": "1"},
        follow_redirects=False,
    )

    html = client.get("/audit").text

    # Changed fields are spelled out old → new ("—" is the empty side).
    assert "pack_price" in html
    assert "428" in html
    assert "pack_quantity" in html
    assert "2000" in html
    # Unchanged fields stay out of the diff: same net, same VAT flag.
    assert "price_per_unit_net" not in html
    assert "vat_inclusive" not in html


# --- AC: banner (not just link) when changes are in the last 24 hours -------


def test_changes_in_the_last_24_hours_surface_as_a_banner(tmp_path: Path) -> None:
    """An edit made moments ago can't be missed: the review shows it as a
    banner (an alert, like the stale-data banner), not just a line of text
    — the quiet-day case where a partner might otherwise skip the diff.
    """
    app = _recipe_app(tmp_path)
    noi = _authed_client(app, assignee_id="noi")
    noi.post(
        "/skus/beans/cost",
        data={"pack_price": "800", "pack_quantity": "1000"},
        follow_redirects=False,
    )

    review_html = _authed_client(app, assignee_id="daniel").get("/").text

    assert "config-changes-banner" in review_html
    banner = review_html.split("config-changes-banner")[1].split("</section>")[0]
    assert 'role="alert"' in banner
    assert "1 change since last review" in banner
    assert 'href="/audit"' in banner


def test_older_unreviewed_changes_show_the_link_but_not_the_banner(
    tmp_path: Path,
) -> None:
    """An unreviewed change from three days ago still deserves its link —
    the partner never looked — but not the can't-miss-it banner, which is
    reserved for the last 24 hours. The edit happens under a clock pinned
    at T (stamping the audit entry at T); the review is read under a
    second app over the same database with the clock pinned at T + 3 days
    — the store's audit timestamps and the banner's "now" ride the same
    injectable clock, so the ageing is deterministic end to end.
    """
    edit_epoch = int(datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc).timestamp())
    recipes_path = _write(tmp_path / "recipes.yaml", _RECIPE_EDITOR_RECIPES_YAML)
    costs_path = _write(tmp_path / "costs.yaml", _RECIPE_EDITOR_COSTS_YAML)
    assignees_path = _write_assignees(tmp_path)

    def _app_at(now_epoch: int):  # type: ignore[no-untyped-def]
        return create_app(
            db_path=str(tmp_path / "tangerine.db"),
            recipes_path=str(recipes_path),
            costs_path=str(costs_path),
            assignees_path=str(assignees_path),
            passphrase=_TEST_PASSPHRASE,
            signing_secret=_TEST_SIGNING_SECRET,
            today=date(2026, 7, 5),
            now_epoch=now_epoch,
        )

    noi = _authed_client(_app_at(edit_epoch), assignee_id="noi")
    noi.post(
        "/skus/beans/cost",
        data={"pack_price": "800", "pack_quantity": "1000"},
        follow_redirects=False,
    )

    three_days_later = _app_at(edit_epoch + 3 * 24 * 60 * 60)
    review_html = _authed_client(three_days_later, assignee_id="daniel").get("/").text

    assert "1 change since last review" in review_html
    assert "config-changes-banner" not in review_html


# --- AC: all audit routes gated behind existing auth middleware -------------


def test_audit_routes_require_auth(tmp_path: Path) -> None:
    """The audit log and both revert surfaces redirect an unauthenticated
    request to ``/login``, exactly like the rest of the app — the paper
    trail and the undo are partner-only.
    """
    app = _recipe_app(tmp_path)
    client = TestClient(app)

    for method, url in (
        ("get", "/audit"),
        ("post", "/audit/reviewed"),
        ("post", "/audit/1/revert"),
        ("post", "/audit/session/abc123/revert"),
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


# =============================================================================
# Sold-as-is quick-create (issue 38 / parent PRD 33)
# =============================================================================

# A directly-sold purchasable (beer, wine, soft drinks — the whole bar segment
# is currently uncosted by design) gets a one-line "serving recipe" so it costs
# through the same item → SKU → recipe path as every dish. From an unmapped
# Loyverse item's row, one stroke creates: the purchasable SKU (receipt-priced),
# its serving recipe, a produced sold SKU the recipe outputs, and the mapping.
#
# Worked example for the tests below. A bottled Chang is bought as a 6-pack at
# 360 THB VAT-inclusive (so 60 THB/bottle net of VAT, and a 330 ml bottle ->
# 0.181308 THB/ml). One sale consumes one bottle = 330 ml, so COGS = 60 THB
# and a 100 THB sell price yields a 40 THB margin. The sold SKU is
# auto-derived as ``beer-chang:served`` (unit ``unit`` — one bottle per sale);
# the purchasable ``beer-chang`` (unit ``ml``) is what the receipt prices and
# what a future beer cocktail would re-use per ml.


def test_sold_as_is_quick_create_costs_an_unmapped_item(tmp_path: Path) -> None:
    """From an unmapped Loyverse item, posting the sold-as-is quick-create
    creates the purchasable, its serving recipe, the sold SKU, and the
    mapping in one stroke — and tomorrow's review costs the sale through
    the same path as every dish.

    This is the tracer bullet: it proves the whole flow end-to-end before
    the per-AC tests fill in the audit, picker, costing-variants, and
    entry-point-link behaviours.
    """
    today = date(2026, 7, 2)
    app = _recipe_app(tmp_path, today=today)
    _seed_menu(
        app.state.db_path,
        [_menu_item("i-chang", "Chang Bottle", "100", segment=Segment.BAR)],
    )
    _seed_sale(app.state.db_path, item_id="i-chang", day=today, price="100")
    client = _authed_client(app)

    response = client.post(
        "/items/i-chang/sold-as-is",
        data={
            "sku_id": "beer-chang",
            "name": "Chang Bottle",
            "unit": "ml",
            "pack_price": "360",
            "pack_quantity": "1980",  # 6 × 330 ml
            "vat_inclusive": "1",
            "serving_size": "330",
        },
        follow_redirects=False,
    )

    # The stroke lands on the new sold SKU's editor.
    assert response.status_code == 303
    assert response.headers["location"] == "/skus/beer-chang:served"
    # The item is no longer unmapped: it is mapped to the produced sold SKU.
    items_html = client.get("/items", params={"item": "i-chang"}).text
    assert "item-row--unmapped" not in items_html
    assert "beer-chang:served" in items_html
    # Tomorrow's review costs the sale through the serving recipe:
    # 360 / 1980 / 1.07 = 0.169923 THB/ml x 330 ml = 56.07 THB COGS,
    # so a 100 THB sell price yields a 43.93 THB margin.
    review_html = client.get("/review", params={"day": today.isoformat()}).text
    assert "43.93" in review_html


# --- AC: all edits gated behind existing auth middleware (issue 38) ----------


def test_sold_as_is_routes_require_auth(tmp_path: Path) -> None:
    """The quick-create's GET form and POST handler both redirect an
    unauthenticated request to ``/login`` — exactly like every other write
    surface in the app. Nothing lands.
    """
    app = _recipe_app(tmp_path)
    client = TestClient(app)

    for method, url in (
        ("get", "/items/i-chang/sold-as-is"),
        ("post", "/items/i-chang/sold-as-is"),
    ):
        response = getattr(client, method)(url, follow_redirects=False)
        assert response.status_code == 302, f"{method} {url}"
        assert response.headers["location"] == "/login", f"{method} {url}"


# --- AC: reachable from an unmapped item's row (issue 38) --------------------


def test_sold_as_is_entry_point_links_from_unmapped_item_row(
    tmp_path: Path,
) -> None:
    """An unmapped item's row in ``/items`` carries a 'sold as-is…' link
    beside the existing 'create new SKU…' option — the daily review's
    unmapped section deep-links straight into fixing itself. The link
    carries the item id so the form is pre-targeted.
    """
    app = _recipe_app(tmp_path)
    _seed_menu(
        app.state.db_path,
        [
            _menu_item("i-latte", "Cafe Latte", "120"),
            _menu_item("i-chang", "Chang Bottle", "100", segment=Segment.BAR),
        ],
    )
    client = _authed_client(app)

    items_html = client.get("/items").text
    # The unmapped bar item's row carries both options; the mapped cafe
    # item's row carries neither. Slice by <tr> so the link's own item id
    # in its href does not split the row short.
    rows = items_html.split("<tr class=")
    chang_row = next(r for r in rows if "i-chang" in r)
    latte_row = next(r for r in rows if "i-latte" in r)
    assert 'href="/items/i-chang/sold-as-is"' in chang_row
    assert "sold as-is" in chang_row.lower()
    # The mapped cafe item (latte is seeded mapped) carries neither option.
    assert "sold-as-is" not in latte_row
    assert "create new SKU" not in latte_row

    # The form itself is reachable and posts to the quick-create handler.
    form_html = client.get("/items/i-chang/sold-as-is").text
    assert 'action="/items/i-chang/sold-as-is"' in form_html
    for field in (
        "sku_id", "name", "unit",
        "pack_price", "pack_quantity", "vat_inclusive", "serving_size",
    ):
        assert f'name="{field}"' in form_html


# --- AC: the serving recipe is an ordinary recipe — visible & editable ------


def test_serving_recipe_is_visible_and_editable_in_the_editor(
    tmp_path: Path,
) -> None:
    """After the quick-create, opening the sold SKU's editor shows the
    serving recipe as an ordinary recipe row — the purchasable selected at
    the serving quantity — and saving an edit through the normal recipe
    editor (``POST /skus/{sku}/recipe``) updates it. There is no separate
    'serving recipe' kind; CONTEXT.md's glossary treats them as the
    uninteresting-recipe case.
    """
    today = date(2026, 7, 2)
    app = _recipe_app(tmp_path, today=today)
    _seed_menu(
        app.state.db_path,
        [_menu_item("i-chang", "Chang Bottle", "100", segment=Segment.BAR)],
    )
    client = _authed_client(app)
    client.post(
        "/items/i-chang/sold-as-is",
        data={
            "sku_id": "beer-chang", "name": "Chang Bottle", "unit": "ml",
            "pack_price": "360", "pack_quantity": "1980", "vat_inclusive": "1",
            "serving_size": "330",
        },
        follow_redirects=False,
    )

    # The sold SKU's editor shows the serving recipe as one populated row.
    editor_html = client.get("/skus/beer-chang:served").text
    assert 'action="/skus/beer-chang:served/recipe"' in editor_html
    rows = editor_html.split('id="recipe-rows"')[1].split("</ol>")[0]
    assert 'value="beer-chang" selected' in rows
    assert 'value="330"' in rows
    # The yield is 1 unit (one sale), marked measured — not estimated.
    yield_field = editor_html.split('name="yield_qty"')[1].split(">")[0]
    assert 'value="1"' in yield_field

    # Editing through the ordinary recipe editor (e.g. the partner realises
    # a bottle is 320 ml, not 330) updates the serving recipe like any other.
    response = client.post(
        "/skus/beer-chang:served/recipe",
        data={
            "ingredient_sku_id": ["beer-chang"],
            "quantity": ["320"],
            "yield_qty": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/skus/beer-chang:served"
    reloaded = client.get("/skus/beer-chang:served").text
    assert 'value="320"' in reloaded
    assert 'value="330"' not in reloaded


# --- AC: the purchasable is reusable as an ingredient (issue 38, US 18) -----


def test_created_purchasable_is_pickable_in_other_recipes(tmp_path: Path) -> None:
    """The purchasable the quick-create prices (``beer-chang``, per-ml) is
    an ordinary purchasable SKU, so it appears in every other recipe's
    ingredient picker — a future beer cocktail uses ``beer-chang x 165`` (ml)
    exactly like milk in a latte. The produced sold SKU (``beer-chang:served``)
    is not pickable: it is a produced non-prep, the same role rule every
    other dish obeys.
    """
    app = _recipe_app(tmp_path)
    _seed_menu(
        app.state.db_path,
        [_menu_item("i-chang", "Chang Bottle", "100", segment=Segment.BAR)],
    )
    client = _authed_client(app)
    client.post(
        "/items/i-chang/sold-as-is",
        data={
            "sku_id": "beer-chang", "name": "Chang Bottle", "unit": "ml",
            "pack_price": "360", "pack_quantity": "1980", "vat_inclusive": "1",
            "serving_size": "330",
        },
        follow_redirects=False,
    )

    # Opening an existing recipe's editor (the latte) offers the new
    # purchasable, and does NOT offer the produced sold SKU.
    latte_html = client.get("/skus/latte").text
    picker = latte_html.split('name="ingredient_sku_id"')[1].split("</select>")[0]
    assert 'value="beer-chang"' in picker
    assert 'value="beer-chang:served"' not in picker

    # The purchasable really is an ingredient: saving a 'beer cocktail' recipe
    # that uses 165 ml of it lands and costs through the ordinary path.
    response = client.post(
        "/skus/flour/recipe",
        data={
            "ingredient_sku_id": ["beer-chang"],
            "quantity": ["165"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    flour_html = client.get("/skus/flour").text
    assert 'value="beer-chang" selected' in flour_html
    assert 'value="165"' in flour_html


# --- AC: all writes audited; session revert removes everything (issue 38) ---


def test_sold_as_is_writes_are_audited_and_session_revert_removes_them(
    tmp_path: Path,
) -> None:
    """The quick-create is five edits in one stroke — the purchasable SKU,
    its cost, the sold SKU, the serving recipe, the mapping — and all five
    land in the audit log attributed to the partner. The session revert
    (the panic undo) removes every one of them cleanly: the item goes back
    to unmapped, the purchasable and sold SKUs vanish, and the next review
    surfaces the sale as unmapped revenue again.
    """
    today = date(2026, 7, 2)
    app = _recipe_app(tmp_path, today=today)
    _seed_menu(
        app.state.db_path,
        [_menu_item("i-chang", "Chang Bottle", "100", segment=Segment.BAR)],
    )
    _seed_sale(app.state.db_path, item_id="i-chang", day=today, price="100")
    client = _authed_client(app, assignee_id="daniel")
    client.post(
        "/items/i-chang/sold-as-is",
        data={
            "sku_id": "beer-chang", "name": "Chang Bottle", "unit": "ml",
            "pack_price": "360", "pack_quantity": "1980", "vat_inclusive": "1",
            "serving_size": "330",
        },
        follow_redirects=False,
    )

    # Every created/changed fact is in the audit trail, attributed to daniel.
    audit_html = client.get("/audit").text
    for needle in (
        "Chang Bottle",        # the purchasable SKU creation
        "360",                 # the receipt cost
        "Chang Bottle (serving)",  # the sold SKU creation
        "330",                 # the serving recipe ingredient quantity
        "i-chang",             # the mapping
        "daniel",
    ):
        assert needle in audit_html, needle

    # The session revert (all five edits share the one login session) is the
    # panic undo. Extract the session id and POST it.
    assert 'action="/audit/session/' in audit_html
    session_id = audit_html.split('action="/audit/session/')[1].split("/revert")[0]
    response = client.post(
        f"/audit/session/{session_id}/revert", follow_redirects=False
    )
    assert response.status_code == 303

    # The item is unmapped again; both SKUs are gone.
    items_html = client.get("/items", params={"item": "i-chang"}).text
    assert "item-row--unmapped" in items_html
    assert "beer-chang" not in items_html
    skus_html = client.get("/skus").text
    assert "beer-chang" not in skus_html
    # The sale resurfaces as unmapped revenue — no costable recipe resolves.
    review_html = client.get("/review", params={"day": today.isoformat()}).text
    assert "unmapped" in review_html.lower()
    assert "43.93" not in review_html


# --- AC: draught and bottle styles both cost correctly (issue 38) -----------


def test_draught_serving_costs_through_the_same_path_as_bottle(
    tmp_path: Path,
) -> None:
    """A draught-style serving (473 ml of a 30,000 ml keg purchasable) costs
    through the same serving-recipe path as a bottle-style serving. The keg
    is bought at 4,500 THB for 30 L VAT-inclusive -> 0.140187 THB/ml; one pint
    of 473 ml is 66.31 THB of beer; sold at 180 THB yields a 113.69 THB
    margin. The serving-recipe shape (one ingredient line, yield 1) is the
    same as the bottle case — only the purchasable's unit and the serving
    quantity differ.
    """
    today = date(2026, 7, 2)
    app = _recipe_app(tmp_path, today=today)
    _seed_menu(
        app.state.db_path,
        [_menu_item("i-pint", "Draught Pint", "180", segment=Segment.BAR)],
    )
    _seed_sale(app.state.db_path, item_id="i-pint", day=today, price="180")
    client = _authed_client(app)
    client.post(
        "/items/i-pint/sold-as-is",
        data={
            "sku_id": "beer-keg", "name": "Draught Beer Keg", "unit": "ml",
            "pack_price": "4500", "pack_quantity": "30000", "vat_inclusive": "1",
            "serving_size": "473",
        },
        follow_redirects=False,
    )

    review_html = client.get("/review", params={"day": today.isoformat()}).text
    # 4500 / 30000 / 1.07 = 0.140187 THB/ml x 473 ml = 66.31 THB COGS;
    # 180 - 66.31 = 113.69 THB margin.
    assert "113.69" in review_html


def test_sold_sku_inherits_the_items_segment(tmp_path: Path) -> None:
    """A sold-as-is item sold from the bar segment carries through to the
    segment-contribution-margin view as bar revenue/cost — the sold SKU
    inherits the Loyverse item's segment, so a Chang sale does not silently
    inflate the cafe row. The purchasable stays segment-NULL (it may feed
    both cafe and bar; an ingredient carries no segment of its own).
    """
    today = date(2026, 7, 2)
    app = _recipe_app(tmp_path, today=today)
    _seed_menu(
        app.state.db_path,
        [_menu_item("i-chang", "Chang Bottle", "100", segment=Segment.BAR)],
    )
    _seed_sale(app.state.db_path, item_id="i-chang", day=today, price="100")
    client = _authed_client(app)
    client.post(
        "/items/i-chang/sold-as-is",
        data={
            "sku_id": "beer-chang", "name": "Chang Bottle", "unit": "ml",
            "pack_price": "360", "pack_quantity": "1980", "vat_inclusive": "1",
            "serving_size": "330",
        },
        follow_redirects=False,
    )

    # The daily review's segment table attributes the sale to Bar, not Cafe.
    review_html = client.get("/review", params={"day": today.isoformat()}).text
    seg_rows = review_html.split("segment-cm__row")
    bar_row = next(r for r in seg_rows if "Bar" in r)
    cafe_row = next(r for r in seg_rows if "Cafe" in r)
    assert "100.00 THB" in bar_row  # the Chang sale's revenue
    assert "0.00 THB" in cafe_row.split("segment-cm__revenue")[1][:60]
