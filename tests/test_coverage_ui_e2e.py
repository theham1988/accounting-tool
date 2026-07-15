"""SKU view + item coverage view UI seam (Wave 1.5, Slice 2).

Per ADR-0003 / issue 24: two read-only surfaces so the partner can see the
whole menu's mapping health without scanning YAML by eye. The genuine
external boundary is HTTP (driven through FastAPI's ``TestClient``); recipes/
costs/mappings are seeded into a real SQLite DB exactly as the migrator
seeds them in production, and menu items are recorded exactly as a sync
would record them.

Per the PRD's testing rules these tests assert on the rendered HTML's visible
content and structure, not on implementation details.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from tangerine.loyverse.store import MenuItem, MenuSnapshot
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Segment
from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

D = Decimal

_TEST_PASSPHRASE = "coverage-ui-test-passphrase"
_TEST_SIGNING_SECRET = "coverage-ui-test-signing-secret"


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
""",
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


def _build_app(tmp_path: Path, *, recipes_yaml: str, costs_yaml: str):  # type: ignore[no-untyped-def]
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
    )


def _seed_menu(db_path: str, items: list[MenuItem]) -> None:
    """Record a menu snapshot into ``db_path``, exactly as a sync would."""
    store = SqliteLoyverseStore.connect(db_path)
    store.record_menu_snapshot(
        MenuSnapshot(items=tuple(items)), at=datetime(2026, 6, 30, tzinfo=timezone.utc)
    )
    store.close()


_RECIPES_YAML = """
recipes:
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }

mappings:
  - { item_id: i-latte, sku_id: espresso-latte }
"""

_COSTS_YAML = """
costs:
  beans-arabica: { price: "2", updated_at: "2026-06-01" }
  milk-fresh: { price: "0.025", updated_at: "2026-06-01" }
"""


# --- AC: `GET /skus` is gated behind the existing auth middleware -----------


def test_get_skus_requires_auth(tmp_path: Path) -> None:
    """An unauthenticated ``GET /skus`` is redirected to ``/login``, matching
    every other protected route in the app.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = TestClient(app)

    response = client.get("/skus", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# --- AC: `GET /skus` renders one row per SKU with mapping/health/cost -------


def test_get_skus_renders_one_row_per_sku_with_full_coverage_picture(tmp_path: Path) -> None:
    """The SKU view shows the latte (active, green, priced), its beans
    ingredient (prep-internal, green), and a dangling leftover SKU (red) —
    the three classifications and a costed row, all on one page.
    """
    recipes_yaml = _RECIPES_YAML.replace(
        "mappings:", "  - sku_id: orphan-sku\n    name: Orphan\n    segment: cafe\n    ingredients: []\n\nmappings:"
    )
    app = _build_app(tmp_path, recipes_yaml=recipes_yaml, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    response = client.get("/skus")

    assert response.status_code == 200
    html = response.text
    assert "Espresso Latte" in html
    assert "beans-arabica" in html
    assert "orphan-sku" in html
    # Status badges surface the worst problems at a glance.
    assert "Dangling" in html or "dangling" in html.lower()
    # Health colour hooks are present as data (not asserting exact CSS).
    assert "sku-row--green" in html
    assert "sku-row--red" in html
    # The latte's derived per-unit cost (20*2 + 200*0.025 = 45.00) is shown.
    assert "45.00" in html


# --- AC: the SKU view shows each SKU's role (issue #35) ---------------------


def test_skus_page_shows_each_skus_role(tmp_path: Path) -> None:
    """Each SKU row carries its role — purchasable / produced / prep — so a
    partner can see at a glance why a SKU is or is not directly priceable.

    Worked example. Beans are bought (no recipe): purchasable. The latte
    has a recipe and is only sold: produced. The oba sauce has a recipe
    *and* the latte consumes it, so the seed flags it prep: its row says
    prep, not merely produced.
    """
    recipes_yaml = """
recipes:
  - sku_id: oba-sauce
    name: Oba Sauce
    segment: cafe
    ingredients:
      - { sku_id: soy, quantity: "30" }
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: oba-sauce, quantity: "10" }

mappings:
  - { item_id: i-latte, sku_id: espresso-latte }
"""
    app = _build_app(tmp_path, recipes_yaml=recipes_yaml, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    html = client.get("/skus").text

    def row_for(sku_id: str) -> str:
        return html.split(f'href="/skus/{sku_id}"')[1].split("</li>")[0]

    # Role hooks are class-suffixed (like the health dots), so "prep" here
    # cannot be satisfied by the unrelated "prep-internal" classification
    # label that also appears in some rows.
    assert "sku-role--purchasable" in row_for("beans-arabica")
    assert "purchasable" in row_for("beans-arabica").lower()
    assert "sku-role--produced" in row_for("espresso-latte")
    assert "sku-role--prep" not in row_for("espresso-latte")
    assert "sku-role--prep" in row_for("oba-sauce")


# --- AC: mobile-first, consistent with the existing review page CSS --------


def test_skus_page_has_viewport_meta_and_linked_brand_stylesheets(
    tmp_path: Path,
) -> None:
    """The SKU view extends the base layout and links the brand stylesheets."""
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    response = client.get("/skus")

    assert response.status_code == 200
    html = response.text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert '/static/tokens/colors.css' in html
    assert '/static/app.css' in html
    assert '/static/review.css' not in html


# --- AC: `GET /items` is gated behind the existing auth middleware ----------


def test_get_items_requires_auth(tmp_path: Path) -> None:
    """An unauthenticated ``GET /items`` is redirected to ``/login``."""
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = TestClient(app)

    response = client.get("/items", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# --- AC: `GET /items` renders one row per Loyverse item, unmapped bubble to top


def test_get_items_renders_one_row_per_item_unmapped_bubbled_to_top(tmp_path: Path) -> None:
    """The item coverage view shows both the mapped latte (with its SKU's
    chain health and derived margin) and an unmapped soda — with the
    unmapped item appearing first in the rendered order.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    _seed_menu(
        app.state.db_path,
        [
            _menu_item("i-latte", "Espresso Latte", "120"),
            _menu_item("i-mystery", "Mystery Soda", "60"),
        ],
    )
    client = _authed_client(app)

    response = client.get("/items")

    assert response.status_code == 200
    html = response.text
    assert "Espresso Latte" in html
    assert "Mystery Soda" in html
    assert "unmapped" in html.lower()
    # Unmapped bubbles above the mapped, healthy row.
    assert html.index("Mystery Soda") < html.index("Espresso Latte")
    # The mapped row's chain health + derived margin are visible
    # (revenue 120 - cost 45 = 75.00 margin).
    assert "item-row--green" in html
    assert "75.00" in html


# --- AC: daily review needs_attention deep-links here, filtered to the item


def test_get_items_with_item_filter_shows_only_that_item(tmp_path: Path) -> None:
    """``?item=<id>`` (the daily review's deep-link target) filters the table
    down to a single row, with a visible way back to the full list.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    _seed_menu(
        app.state.db_path,
        [
            _menu_item("i-latte", "Espresso Latte", "120"),
            _menu_item("i-mystery", "Mystery Soda", "60"),
        ],
    )
    client = _authed_client(app)

    response = client.get("/items", params={"item": "i-mystery"})

    assert response.status_code == 200
    html = response.text
    assert "Mystery Soda" in html
    assert "Espresso Latte" not in html
    # A way back to the unfiltered view is present.
    assert 'href="/items"' in html


def _menu_item(item_id: str, name: str, price: str, segment: Segment = Segment.CAFE) -> MenuItem:
    return MenuItem(item_id=item_id, name=name, sell_price=D(price), segment=segment)


def test_items_page_has_viewport_meta_and_linked_brand_stylesheets(
    tmp_path: Path,
) -> None:
    """The item coverage view extends the base layout and links brand CSS."""
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    response = client.get("/items")

    assert response.status_code == 200
    html = response.text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert '/static/tokens/colors.css' in html
    assert '/static/app.css' in html
    assert '/static/review.css' not in html


# --- Wave 3 #46: Stock screen redesign (tabs, chips, summary, footer) ------


def test_stock_tabs_link_between_items_and_skus(tmp_path: Path) -> None:
    """MENU ITEMS / INGREDIENT SKUS tabs link between the two routes with the
    active tab marked on each page.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    client = _authed_client(app)

    items_html = client.get("/items").text
    assert "<!--section:stock-tabs-->" in items_html
    assert 'href="/skus"' in items_html
    assert "stock-tabs__link--active" in items_html

    skus_html = client.get("/skus").text
    assert "<!--section:stock-tabs-->" in skus_html
    assert 'href="/items"' in skus_html
    assert "stock-tabs__link--active" in skus_html


def test_stock_filter_chips_are_deep_linkable(tmp_path: Path) -> None:
    """Filter chips ALL / NEEDS WORK / RED / HEALTHY narrow each list via a
    query param and are deep-linkable from the chip row.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    _seed_menu(
        app.state.db_path,
        [
            _menu_item("i-latte", "Espresso Latte", "120"),
            _menu_item("i-mystery", "Mystery Soda", "60"),
        ],
    )
    client = _authed_client(app)

    items_html = client.get("/items").text
    assert 'href="/items?filter=needs-work"' in items_html
    assert 'href="/items?filter=red"' in items_html
    assert 'href="/items?filter=healthy"' in items_html
    assert 'href="/skus/new"' in items_html

    filtered = client.get("/items", params={"filter": "healthy"}).text
    assert "Espresso Latte" in filtered
    assert "Mystery Soda" not in filtered
    assert "stock-chips__link--active" in filtered


def test_stock_summary_and_footer_show_counts(tmp_path: Path) -> None:
    """A summary sentence states coverage; the footer shows bulk-edit and the
    shown count.
    """
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    _seed_menu(
        app.state.db_path,
        [
            _menu_item("i-latte", "Espresso Latte", "120"),
            _menu_item("i-mystery", "Mystery Soda", "60"),
        ],
    )
    client = _authed_client(app)

    items_html = client.get("/items").text
    assert "can&rsquo;t be costed" in items_html or "can't be costed" in items_html
    assert 'href="/upload"' in items_html
    assert "Showing 2 of 2" in items_html

    skus_html = client.get("/skus").text
    assert "need work" in skus_html
    assert "Showing" in skus_html


def test_stock_empty_filter_shows_friendly_reset(tmp_path: Path) -> None:
    """A filter matching nothing shows the friendly empty state with SHOW ALL."""
    app = _build_app(tmp_path, recipes_yaml=_RECIPES_YAML, costs_yaml=_COSTS_YAML)
    _seed_menu(
        app.state.db_path,
        [_menu_item("i-mystery", "Mystery Soda", "60")],
    )
    client = _authed_client(app)

    html = client.get("/items", params={"filter": "healthy"}).text

    assert "Nothing matches this filter" in html
    assert "Show all" in html
    assert 'href="/items"' in html
