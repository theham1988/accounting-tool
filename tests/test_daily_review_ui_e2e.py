"""End-to-end UI seam (Wave 1, Slice 2).

Exercises the FastAPI web layer over the Wave-1-Slice-1 stack: a request hits a
real route, the route builds a ``DailyReview`` from the persisted data + loaded
config, and Jinja2 renders it as HTML. The genuine external boundary is HTTP
(driven through FastAPI's ``TestClient``); no internal module is mocked. The
Loyverse HTTP boundary is irrelevant to this slice — sales are seeded straight
into the SQLite store, exactly as Slice 3's sync will write them.

Per the PRD's testing rules these tests parse the rendered HTML and assert on
the visible numbers and flags — never on implementation details (how the route
is wired, which template file rendered). A reader should be able to swap the
web framework and these tests would still pin the partner-visible behaviour.

Scope (slice 2 only):
  - ``GET /`` renders yesterday's review (no login yet — slice 4).
  - ``GET /review?day=YYYY-MM-DD`` renders that day's review.
  - No "Sync now" button yet (slice 3); no day-navigation control yet (slice 5).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tangerine.loyverse.store import SaleRecord
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Money, Sale

D = Decimal


def _seeded_recipes_yaml() -> str:
    """The Wave 1 default recipes (mirrors ``config/recipes.yaml``)."""
    return """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }
"""


def _seeded_costs_yaml() -> str:
    """The Wave 1 default costs (mirrors ``config/costs.yaml``)."""
    return """
costs:
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
  beans-arabica: { price: "2", updated_at: "2026-06-01" }
  milk-fresh: { price: "0.025", updated_at: "2026-06-01" }
"""


def _sale_record(
    *,
    receipt_number: str,
    item_id: str,
    day: date,
    price: str,
    segment=None,  # type: ignore[no-untyped-def]
) -> SaleRecord:
    return SaleRecord(
        sale=Sale(
            item_id=item_id,
            timestamp=day,
            sell_price=Money(price),
            segment=segment,
        ),
        receipt_number=receipt_number,
        line_id="li-1",
    )


def _write_config(tmp_path: Path) -> tuple[str, str, str]:
    """Write recipes + costs + assignees YAML into ``tmp_path``.

    Returns ``(recipes_path, costs_path, assignees_path)``. Slice 4 added the
    assignees file (the role-selector source); the slice-2 builders thread it
    through so the same tests run unchanged against the auth gate.
    """
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_seeded_recipes_yaml(), encoding="utf-8")
    costs.write_text(_seeded_costs_yaml(), encoding="utf-8")
    assignees.write_text(_seeded_assignees_yaml(), encoding="utf-8")
    return str(recipes), str(costs), str(assignees)


def _seeded_assignees_yaml() -> str:
    """The Wave 1 default partners (mirrors ``config/assignees.yaml``)."""
    return """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


#: Stable passphrase + signing secret for the slice-2 suite. Slice 4 added the
#: auth gate; rather than rewriting every test, the builders inject these
#: explicitly so no test mutates the process environment.
_TEST_PASSPHRASE = "slice2-test-passphrase"
_TEST_SIGNING_SECRET = "slice2-test-signing-secret"


def _authed_client(app):  # type: ignore[no-untyped-def]
    """A ``TestClient`` that has already logged in as ``daniel``.

    Slice 4 gates every route behind a signed-cookie session. The slice-2
    tests assert on the review page's contents (numbers, sections, rankings)
    — they do not care about auth themselves, so this helper performs the
    login dance once and hands back a ready-to-use authenticated client.
    """
    from tangerine.web.auth import SESSION_COOKIE

    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a session cookie"
    return client


@pytest.fixture
def yesterday() -> date:
    """A fixed "yesterday" so tests do not depend on the wall clock.

    ``GET /`` renders *yesterday's* review. To keep the test deterministic we
    inject the "today" the app sees and assert against ``today - 1``.
    """
    return date(2026, 6, 24)


@pytest.fixture
def today(yesterday: date) -> date:
    return yesterday + timedelta(days=1)


# --- AC: GET / against an empty DB renders the review with zeros -----------------


def test_get_root_against_empty_db_renders_full_review_with_zeros(
    tmp_path: Path, today: date
) -> None:
    """``GET /`` against a fresh (empty) DB returns 200 and renders the full
    review structure with all-zero headline numbers plus a "no sales" note.

    Slice 3 has not shipped yet, so against an empty DB there are no sales for
    yesterday. The page must not crash or 404 — it renders the full review
    template so the partner sees the tool is wired up and waiting for data.

    The genuine boundary is HTTP (driven via FastAPI's ``TestClient``); the
    SQLite store uses an in-process tempfile DB and the config loader reads
    real YAML files written to ``tmp_path``.
    """
    app = _build_app_with_empty_db(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    # Full review structure is present (the section anchors the template uses).
    assert "Daily 9am review" in html
    # Headline numbers all zero against no sales.
    assert "0.00" in html
    # The dedicated empty-state note.
    assert "no sales" in html.lower()


# --- shared app builders --------------------------------------------------------


def _build_app_with_empty_db(
    tmp_path: Path, *, today: date
):  # type: ignore[no-untyped-def]
    """App factory call against a fresh (empty) SQLite DB at ``tmp_path``."""
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_config(tmp_path)
    # Touch the DB so the file exists (the store would create it on first
    # request anyway, but this makes the empty-state explicit).
    SqliteLoyverseStore.connect(db_path).close()
    return create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )


def _build_app_seeded(
    tmp_path: Path, *, today: date, sales: list[SaleRecord]
):  # type: ignore[no-untyped-def]
    """App factory call with ``sales`` pre-seeded into the SQLite DB."""
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_config(tmp_path)
    store = SqliteLoyverseStore.connect(db_path)
    if sales:
        store.record_sales(sales)
    store.close()
    return create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )


# --- AC: GET / renders yesterday's headline numbers matching the engine ---------


def test_get_root_renders_yesterday_headline_numbers(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``GET /`` shows yesterday's revenue, COGS, and gross margin.

    Worked example. Yesterday one Chang draft @ 120 (cost 35) and one espresso
    latte @ 120 (cost 45) were sold. The review shows revenue 240.00 THB,
    COGS 80.00 THB, gross margin 160.00 THB — the same numbers the CLI prints
    for the same seeded data.

    These are the partner-visible numbers on the page, asserted on the rendered
    HTML (not on the engine object) — the test pins what a partner actually
    sees when they open the tool at 9am.
    """
    sales = [
        _sale_record(
            receipt_number="2-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
        _sale_record(
            receipt_number="2-2",
            item_id="espresso-latte",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    # Yesterday's headline numbers appear verbatim (2-dp money formatting).
    assert "240.00" in html  # revenue
    assert "80.00" in html   # COGS
    assert "160.00" in html  # gross margin
    # The review's date label names yesterday.
    assert yesterday.isoformat() in html


# --- AC: per-segment CM rows render, with a red flag where CM < 0 ----------------


def test_get_root_renders_per_segment_cm_with_red_flag_below_zero(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Per-segment contribution-margin rows render; a red flag marks CM < 0.

    Worked example. Bar sells one Chang below cost (30 THB, cost 35 -> bar CM
    -5, red). Cafe sells a latte normally (120 THB, cost 45 -> cafe CM 75, not
    red). The page renders one row per segment with its CM, and the bar row
    carries a red-flag marker the cafe row does not.

    Asserts on rendered structure (segment names + a CSS class hook) rather
    than incidental text, so the test survives wording changes.
    """
    sales = [
        _sale_record(
            receipt_number="2-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="30",  # below cost -> bar CM -5, red
        ),
        _sale_record(
            receipt_number="2-2",
            item_id="espresso-latte",
            day=yesterday,
            price="120",  # cafe CM 75, not red
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    # Both segment names appear.
    assert "Cafe" in html or "cafe" in html
    assert "Bar" in html or "bar" in html
    # The negative bar CM (-5.00) appears; the positive cafe CM (75.00) too.
    assert "-5.00" in html
    assert "75.00" in html
    # The red flag marker is present in the page (bar is below zero).
    assert "red" in html.lower()


# --- AC: top/bottom items by margin and by volume render -----------------------


def test_get_root_renders_top_and_bottom_rankings(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The four ranking sections (top/bottom by margin, top/bottom by volume)
    each render their items.

    Worked example. Yesterday: 3× Chang Draft @ 120 (margin 85 each, 3 units)
    and 2× Espresso Latte @ 120 (margin 75 each, 2 units).

      - Top by margin: Chang (255) before Latte (150).
      - Bottom by margin: Latte (150) before Chang (255).
      - Top by volume: Chang (3) before Latte (2).
      - Bottom by volume: Latte (2) before Chang (3).

    Asserts via per-section HTML anchors so the test pins *which* section an
    item appears in, not just that the name is somewhere on the page. Uses
    BeautifulSoup only if available; otherwise falls back to slicing the HTML
    between anchor comments (kept dependency-free).
    """
    sales = [
        *[_sale_record(
            receipt_number=f"2-chang-{i}",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ) for i in range(3)],
        *[_sale_record(
            receipt_number=f"2-latte-{i}",
            item_id="espresso-latte",
            day=yesterday,
            price="120",
        ) for i in range(2)],
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    top_margin = _section(html, "top-by-margin")
    bottom_margin = _section(html, "bottom-by-margin")
    top_volume = _section(html, "top-by-volume")
    bottom_volume = _section(html, "bottom-by-volume")

    chang = "Chang Draft 500ml"
    latte = "Espresso Latte"

    # Each item appears in every ranking section (only 2 reliable items, so
    # both lists carry both — the engine does not pad to TOP_BOTTOM_COUNT).
    for section, label in [
        (top_margin, "top-by-margin"),
        (bottom_margin, "bottom-by-margin"),
        (top_volume, "top-by-volume"),
        (bottom_volume, "bottom-by-volume"),
    ]:
        assert chang in section, f"{chang} missing from {label}"
        assert latte in section, f"{latte} missing from {label}"

    # Ordering: Chang before Latte in top-by-margin and top-by-volume;
    # Latte before Chang in both bottom lists.
    assert section_index(top_margin, chang) < section_index(top_margin, latte)
    assert section_index(bottom_margin, latte) < section_index(bottom_margin, chang)
    assert section_index(top_volume, chang) < section_index(top_volume, latte)
    assert section_index(bottom_volume, latte) < section_index(bottom_volume, chang)


def _section(html: str, anchor: str) -> str:
    """Return the HTML slice for a section marked with ``<!--section:NAME-->``
    and ``<!--/section:NAME-->`` comments.

    Keeping the section delimiters as HTML comments means the test does not
    depend on any particular tag structure or CSS class — only on the anchor
    name the template and test agree on.
    """
    start = f"<!--section:{anchor}-->"
    end = f"<!--/section:{anchor}-->"
    i = html.find(start)
    j = html.find(end)
    assert i != -1 and j != -1, f"section {anchor!r} not found in HTML"
    return html[i : j + len(end)]


def section_index(section_html: str, needle: str) -> int:
    """Index of ``needle`` within a section slice (used for ordering checks)."""
    i = section_html.find(needle)
    assert i != -1, f"{needle!r} not in section"
    return i


# --- AC: below-target-margin items section renders -----------------------------


def _recipes_yaml_with_target() -> str:
    """Recipes YAML with a target gross-margin % set on the chang recipe.

    Chang @ 120 has a 70.83% gross margin (85 / 120). With a 75% target set,
    the engine flags it as below-target. The latte carries no target so it is
    never flagged.
    """
    return """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    target_gross_margin_pct: "75"
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }
"""


def _write_custom_recipes(tmp_path: Path, recipes_yaml: str) -> tuple[str, str, str]:
    """Write custom recipes + default costs + default assignees into ``tmp_path``.

    Returns ``(recipes_path, costs_path, assignees_path)``.
    """
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(recipes_yaml, encoding="utf-8")
    costs.write_text(_seeded_costs_yaml(), encoding="utf-8")
    assignees.write_text(_seeded_assignees_yaml(), encoding="utf-8")
    return str(recipes), str(costs), str(assignees)


def test_get_root_renders_below_target_section(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Items whose actual margin is below their set target appear in a
    dedicated 'below target' section.

    Worked example. Chang recipe carries a 75% target; sold @ 120 its actual
    gross margin is 70.83% (85 / 120) -> below target -> flagged. Latte has no
    target -> never flagged.

    The page renders the below-target section naming the chang item; the
    latte does not appear there.
    """
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_custom_recipes(
        tmp_path, _recipes_yaml_with_target()
    )

    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(
        [
            _sale_record(
                receipt_number="2-1",
                item_id="chang-draft-500",
                day=yesterday,
                price="120",
            ),
            _sale_record(
                receipt_number="2-2",
                item_id="espresso-latte",
                day=yesterday,
                price="120",
            ),
        ]
    )
    store.close()

    app = create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    below = _section(response.text, "below-target")
    assert "Chang Draft 500ml" in below
    assert "Espresso Latte" not in below


# --- AC: unmapped AND unknown-price rows surface together, each with reason -----


def _recipes_yaml_with_unpriced_ingredient() -> str:
    """Recipes where 'iced-latte' references an unpriced ingredient.

    The cost book (default seeded) has no entry for ``syrup-vanilla``, so a
    sale of iced-latte is mapped (it has a recipe) but its margin row is
    flagged ``unknown_price`` — excluded from totals, surfaced for review.
    """
    return """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
  - sku_id: iced-latte
    name: Iced Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }
      - { sku_id: syrup-vanilla, quantity: "15" }
"""


def test_get_root_surfaces_unmapped_and_unknown_price_rows(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Rows excluded from totals (unmapped OR unknown-price) appear in a
    'needs attention' section, each labelled with its reason.

    Worked example. Yesterday:
      - 1× chang (priced, in rankings, not flagged).
      - 1× iced-latte (recipe present, but ``syrup-vanilla`` has no cost entry
        -> unknown_price=True, excluded from totals).
      - 1× 'mystery-mocktail' (no recipe -> unmapped=True, excluded).

    The needs-attention section surfaces BOTH flagged items, each with its
    reason ('unmapped' vs 'unknown price'). Their revenue is not silently
    dropped — a partner can see what was sold but could not be costed.
    """
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_custom_recipes(
        tmp_path, _recipes_yaml_with_unpriced_ingredient()
    )

    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(
        [
            _sale_record(
                receipt_number="2-1",
                item_id="chang-draft-500",
                day=yesterday,
                price="120",
            ),
            _sale_record(
                receipt_number="2-2",
                item_id="iced-latte",
                day=yesterday,
                price="130",
            ),
            _sale_record(
                receipt_number="2-3",
                item_id="mystery-mocktail",
                day=yesterday,
                price="200",
                segment=None,  # engine falls back to BAR via shift timestamp
            ),
        ]
    )
    store.close()

    app = create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    needs = _section(response.text, "needs-attention").lower()
    # Both flagged items appear.
    assert "iced latte" in needs
    assert "mystery-mocktail" in needs
    # The reliable chang (no flags) is NOT in needs-attention.
    assert "chang draft 500ml" not in needs
    # Each row carries its reason so the partner knows which is which.
    assert "unmapped" in needs
    assert "unknown price" in needs

    # Flagged items must NOT appear in any ranking section — their margins are
    # meaningless, so including them would mislead the partner. Only the
    # reliable chang (priced, mapped) belongs in the rankings.
    for ranking_anchor in (
        "top-by-margin",
        "bottom-by-margin",
        "top-by-volume",
        "bottom-by-volume",
    ):
        ranking = _section(response.text, ranking_anchor).lower()
        assert "iced latte" not in ranking, (
            f"unknown-price item surfaced in {ranking_anchor} (should be excluded)"
        )
        assert "mystery-mocktail" not in ranking, (
            f"unmapped item surfaced in {ranking_anchor} (should be excluded)"
        )


# --- AC: needs-attention rows deep-link to the item coverage view (issue 24) --


def test_needs_attention_rows_deep_link_to_item_coverage_view(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Each needs-attention row links to ``/items?item=<id>`` — a single tap
    from the morning review straight to the "fix this one" item coverage row
    (issue 24: the SKU view + item coverage view slice).
    """
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_custom_recipes(
        tmp_path, _recipes_yaml_with_unpriced_ingredient()
    )

    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(
        [
            _sale_record(
                receipt_number="2-1",
                item_id="iced-latte",
                day=yesterday,
                price="130",
            ),
            _sale_record(
                receipt_number="2-2",
                item_id="mystery-mocktail",
                day=yesterday,
                price="200",
                segment=None,
            ),
        ]
    )
    store.close()

    app = create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    needs = _section(response.text, "needs-attention")
    assert 'href="/items?item=iced-latte"' in needs
    assert 'href="/items?item=mystery-mocktail"' in needs


# --- AC: create_app wires a recipes.yaml mapping through end-to-end ------------


def _recipes_yaml_with_item_to_sku_mapping() -> str:
    """A recipe keyed by a master SKU, sold under a *different* Loyverse item id.

    Mirrors the real ``config/recipes.yaml`` shape post-re-key: the recipe's
    own ``sku_id`` (``espresso``) is a human-readable slug, but the item that
    actually sells carries a distinct Loyverse variant SKU (``10042``) that
    only resolves to the recipe via the ``mappings:`` block.
    """
    return """
recipes:
  - sku_id: espresso
    name: Espresso
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "10" }

mappings:
  - { item_id: "10042", sku_id: espresso }
"""


def test_get_root_resolves_sale_through_recipes_yaml_mapping(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """A sold Loyverse item resolves through ``config/recipes.yaml``'s
    ``mappings:`` block all the way to the rendered headline revenue.

    Regression test: ``compute_daily_margin``/``compute_period_segment_margins``
    once rebuilt their ``RecipeCatalog`` from only the recipe list, silently
    dropping the loaded ``mappings:`` — so a mapping-only YAML edit (exactly
    what the recipe re-key was) had zero effect on the running app, and every
    real sale (whose Loyverse identity is a variant SKU, never equal to a
    recipe's own ``sku_id``) surfaced as unmapped. This drives the full HTTP
    seam (``create_app`` -> route -> template) the way a partner's browser
    does, so it catches a broken wiring point that a lower-level
    ``compute_item_margins``-with-a-hand-built-``RecipeCatalog`` test cannot.

    Worked example: one Espresso (Loyverse item id ``10042``) sold at 70 THB,
    costing 10g beans @ 2 THB/g = 20 THB -> 50 THB margin. It must show up in
    the headline revenue and NOT in the "needs attention" unmapped list.
    """
    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_custom_recipes(
        tmp_path, _recipes_yaml_with_item_to_sku_mapping()
    )

    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(
        [
            _sale_record(
                receipt_number="2-1",
                item_id="10042",
                day=yesterday,
                price="70",
            ),
        ]
    )
    store.close()

    from tangerine.web.app import create_app

    app = create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    # Headline revenue/margin reflect the mapped item, not zero.
    assert "70.00" in html  # revenue
    assert "50.00" in html  # gross margin (70 - 20)
    # The item is fully resolved (mapped + priced), so the needs-attention
    # section (only rendered when something is flagged) must be absent.
    assert "Needs attention" not in html
    assert "<!--section:needs-attention-->" not in html


# --- AC: goal progress section (rolling avg, target, met/missing, days) ---------


def test_get_root_renders_goal_progress(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The goal-progress section shows the 7-day rolling average, the 10K
    THB/day target, a met/missing indicator, and the days-in-window count.

    Worked example. Yesterday one Chang @ 120 was sold (gross margin 85). The
    trailing window is just that one day (no earlier sales), so the rolling
    average is 85.00 THB against the 10,000 THB target -> 'missing'.

    The section must surface all four signals so a partner scanning the page
    can tell at a glance whether the week is trending toward the goal.
    """
    sales = [
        _sale_record(
            receipt_number="2-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    goal = _section(response.text, "goal").lower()
    # Rolling average and target appear as 2-dp money.
    assert "85.00" in goal          # rolling average
    assert "10000.00" in goal       # 10K THB/day target
    # Status indicator: 85 < 10000 -> 'missing'. The template renders only the
    # active word, so 'met' must NOT appear when the goal is missed.
    assert "missing" in goal
    assert "met" not in goal  # not "Met" — the goal is missed this window
    # Days-in-window is named so a reader knows the window size.
    assert "1" in goal  # one day in window


# --- AC: GET /review?day=YYYY-MM-DD renders that specific day -------------------


def test_get_review_for_specific_day_renders_that_day(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``GET /review?day=YYYY-MM-DD`` renders the review for the named day,
    not yesterday.

    Worked example. Sales on two distinct days:
      - two days ago (``yesterday - 1``): 1× chang @ 120 + 1× latte @ 120
        -> revenue 240, GM 160.
      - yesterday: only 1× chang @ 120 -> revenue 120, GM 85.

    Hitting ``/review?day=<two-days-ago>`` must surface the older day's
    numbers (revenue 240.00), proving the route honours the ``day`` query
    param rather than always rendering yesterday.
    """
    two_days_ago = yesterday - timedelta(days=1)
    sales = [
        # Two days ago: full pair.
        _sale_record(
            receipt_number="2-prev-chang",
            item_id="chang-draft-500",
            day=two_days_ago,
            price="120",
        ),
        _sale_record(
            receipt_number="2-prev-latte",
            item_id="espresso-latte",
            day=two_days_ago,
            price="120",
        ),
        # Yesterday: only chang.
        _sale_record(
            receipt_number="2-now-chang",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/review", params={"day": two_days_ago.isoformat()})

    assert response.status_code == 200
    html = response.text
    # The named day's headline (revenue 240) — not yesterday's (120).
    assert "240.00" in html
    assert two_days_ago.isoformat() in html
    # Yesterday's revenue (120) is NOT the headline here. (It may appear
    # elsewhere, e.g. rankings, so we don't assert its absence globally —
    # only that the named day's label is the one shown.)
    assert "Daily 9am review" in html


def test_get_review_with_malformed_day_returns_400(
    tmp_path: Path, today: date
) -> None:
    """A malformed ``day`` query param is a client error, not a 500.

    The route validates the ISO-8601 format; an unparseable value surfaces as
    400 with a short human-readable message rather than rendering a misleading
    review for an unintended day.
    """
    app = _build_app_with_empty_db(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/review", params={"day": "not-a-date"})

    assert response.status_code == 400


# --- AC: HTML numbers equal engine numbers against the same persisted data -----


def test_rendered_numbers_match_engine_object(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Every financial number the page shows equals what ``build_daily_review``
    returns for the same date against the same persisted data.

    Guards against the route re-deriving or rounding numbers differently from
    the engine. The test builds the engine's ``DailyReview`` directly (against
    the same SQLite store + config the app uses) and asserts each headline,
    per-segment-CM, and goal number appears in the rendered HTML verbatim.

    This is the AC: "Numbers in the rendered HTML match what
    ``build_daily_review(...)`` returns for the same date against the same
    persisted data."
    """
    from tangerine.config.loader import load_costs, load_recipes
    from tangerine.daily_review import build_daily_review
    from tangerine.loyverse.source import StoreSource

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, _assignees_path = _write_config(tmp_path)

    sales = [
        _sale_record(
            receipt_number="2-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
        _sale_record(
            receipt_number="2-2",
            item_id="espresso-latte",
            day=yesterday,
            price="120",
        ),
    ]
    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(sales)

    # Build the engine object against the exact same data the app will read.
    catalog = load_recipes(recipes_path)
    cost = load_costs(costs_path)
    engine_review = build_daily_review(
        source=StoreSource(store=store, recipes=list(catalog.all()), cost=cost),
        review_date=yesterday,
    )
    store.close()

    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)
    response = client.get("/review", params={"day": yesterday.isoformat()})

    assert response.status_code == 200
    html = response.text

    def money(v) -> str:  # type: ignore[no-untyped-def]
        from decimal import Decimal

        return str(Decimal(v).quantize(Decimal("0.01")))

    # Headline numbers.
    assert money(engine_review.revenue) in html
    assert money(engine_review.cogs) in html
    assert money(engine_review.gross_margin) in html

    # Per-segment CM numbers (revenue, variable_costs, contribution_margin).
    for sm in engine_review.segment_margins:
        assert money(sm.revenue) in html
        assert money(sm.variable_costs) in html
        assert money(sm.contribution_margin) in html

    # Goal numbers.
    assert money(engine_review.goal.rolling_average) in html
    assert money(engine_review.goal.target) in html


# --- AC: renders in under one second against weeks of seeded data ---------------


def test_renders_under_one_second_against_weeks_of_data(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The 9am review renders in under one second against a few weeks of
    seeded sales — the AC's 'fast morning scan' guarantee.

    Seeds three weeks of sales (21 days × 20 sales/day = 420 sales), then
    times a single ``GET /``. The threshold is the AC's 1.0s; a healthy run
    completes in well under 100ms, so the headroom is large.
    """
    import time

    sales: list[SaleRecord] = []
    for offset in range(21):  # 21 days ending on ``yesterday``
        day = yesterday - timedelta(days=offset)
        for i in range(20):
            sales.append(
                _sale_record(
                    receipt_number=f"2-{offset}-{i}-chang",
                    item_id="chang-draft-500",
                    day=day,
                    price="120",
                )
            )
            sales.append(
                _sale_record(
                    receipt_number=f"2-{offset}-{i}-latte",
                    item_id="espresso-latte",
                    day=day,
                    price="120",
                )
            )

    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    start = time.perf_counter()
    response = client.get("/")
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert elapsed < 1.0, f"render took {elapsed:.3f}s, expected < 1.0s"


# --- AC: mobile-first responsive CSS -------------------------------------------


def test_page_has_viewport_meta_and_linked_mobile_first_css(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The page is mobile-first responsive: it carries a viewport meta tag
    and links a CSS file whose base rules apply at phone width.

    Two structural enablers must be present for the page to render readably on
    a phone:

      1. ``<meta name="viewport" content="width=device-width, initial-scale=1">``
         — without it, mobile browsers render at a desktop viewport width and
         shrink the page.
      2. A linked stylesheet whose base rules (no ``min-width`` media-query
         gate) apply at narrow widths — i.e. the layout is mobile- FIRST, not
         desktop-first with a mobile fallback.

    The test fetches the linked CSS through the app and asserts its base rules
    are not gated behind a ``min-width`` media query (which would make the
    desktop layout the default and the phone layout the override).
    """
    sales = [
        _sale_record(
            receipt_number="2-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get("/").text

    # 1. Viewport meta tag present (mobile browsers need this to render at
    # phone width rather than a virtual desktop width).
    assert 'name="viewport"' in html
    assert "width=device-width" in html

    # 2. A stylesheet is linked.
    assert 'rel="stylesheet"' in html
    css_url = _extract_css_url(html)
    assert css_url is not None, "no <link rel=stylesheet href=...> found"

    css_response = client.get(css_url)
    assert css_response.status_code == 200
    css = css_response.text
    # Base rules exist (the file is not empty / not just a media query).
    assert css.strip(), "linked CSS is empty"
    # Strip /* ... */ comments before checking the rule structure, so the
    # heuristic is not tripped by a comment that mentions @media in prose.
    import re

    rule_css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL).strip()
    assert rule_css, "linked CSS has only comments, no rules"
    # The base rules are NOT wrapped entirely in a min-width media query —
    # mobile-first means the phone layout is the default, not the override.
    # Assert that some rule (a non-empty selector with declarations) appears
    # before the first top-level @media (min-width ...) gate.
    first_min_width = rule_css.find("@media (min-width")
    first_rule_end = rule_css.find("}")
    assert first_rule_end != -1, "CSS has no rule blocks"
    if first_min_width != -1:
        assert first_rule_end < first_min_width, (
            "CSS is desktop-first: a base rule is not reachable before the "
            "first min-width media query gates wider viewports."
        )


def _extract_css_url(html: str) -> str | None:
    """Pull the href from the first ``<link rel="stylesheet" ...>`` in ``html``."""
    import re

    m = re.search(
        r'<link[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']',
        html,
    )
    return m.group(1) if m else None


# --- AC (Wave 3 #45): day-nav arrows dim at synced-range bounds -------------


def _day_nav_arrow_tag(nav: str, modifier: str) -> str:
    """Return the opening ``<a>`` tag for a day-nav arrow (``--prev`` or ``--next``)."""
    import re

    match = re.search(
        rf'<a[^>]*day-nav__arrow--{modifier}[^>]*>',
        nav,
    )
    assert match is not None, f"no day-nav {modifier} arrow in {nav!r}"
    return match.group(0)


def test_day_nav_prev_arrow_dims_at_earliest_synced_day(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The prev arrow dims when the review date is the earliest day with sales."""
    sales = [
        _sale_record(
            receipt_number="45-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/review", params={"day": yesterday.isoformat()})

    assert response.status_code == 200
    prev_tag = _day_nav_arrow_tag(_section(response.text, "day-nav"), "prev")
    assert "day-nav__arrow--dimmed" in prev_tag
    assert 'aria-disabled="true"' in prev_tag
    assert "href=" not in prev_tag


def test_day_nav_next_arrow_dims_at_latest_reviewable_day(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The next arrow dims when the review date is yesterday (latest reviewable)."""
    sales = [
        _sale_record(
            receipt_number="45-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    next_tag = _day_nav_arrow_tag(_section(response.text, "day-nav"), "next")
    assert "day-nav__arrow--dimmed" in next_tag
    assert 'aria-disabled="true"' in next_tag
    assert "href=" not in next_tag


def test_day_nav_arrows_link_when_between_bounds(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Prev and next arrows are live links when the day is between the bounds."""
    three_days_ago = yesterday - timedelta(days=2)
    two_days_ago = yesterday - timedelta(days=1)
    sales = [
        _sale_record(
            receipt_number="45-a",
            item_id="chang-draft-500",
            day=three_days_ago,
            price="120",
        ),
        _sale_record(
            receipt_number="45-b",
            item_id="chang-draft-500",
            day=two_days_ago,
            price="120",
        ),
        _sale_record(
            receipt_number="45-c",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/review", params={"day": two_days_ago.isoformat()})

    assert response.status_code == 200
    nav = _section(response.text, "day-nav")
    prev_tag = _day_nav_arrow_tag(nav, "prev")
    next_tag = _day_nav_arrow_tag(nav, "next")
    assert "day-nav__arrow--dimmed" not in prev_tag
    assert "day-nav__arrow--dimmed" not in next_tag
    assert three_days_ago.isoformat() in prev_tag
    assert yesterday.isoformat() in next_tag


# --- AC (Wave 3 #45): TOP & BOTTOM MARGIN/VOLUME toggle (query param) -------


def test_rank_toggle_defaults_to_margin(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The TOP & BOTTOM section defaults to the margin pair (issue #45)."""
    sales = [
        _sale_record(
            receipt_number="45-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    toggle = _section(response.text, "rank-toggle")
    assert "rank-toggle__link--active" in toggle
    assert 'data-rank="margin"' in response.text


def test_rank_toggle_volume_query_param_marks_volume_active(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``?rank=volume`` marks Volume active and sets ``data-rank="volume"``."""
    sales = [
        _sale_record(
            receipt_number="45-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app_seeded(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get(
        "/review",
        params={"day": yesterday.isoformat(), "rank": "volume"},
    )

    assert response.status_code == 200
    toggle = _section(response.text, "rank-toggle")
    assert "rank-toggle__link--active" in toggle
    assert 'data-rank="volume"' in response.text
    for anchor in (
        "top-by-margin",
        "bottom-by-margin",
        "top-by-volume",
        "bottom-by-volume",
    ):
        _section(response.text, anchor)


def test_invalid_rank_query_param_returns_400(
    tmp_path: Path, today: date
) -> None:
    """An unknown ``rank`` value is a client error, not a silent fallback."""
    app = _build_app_with_empty_db(tmp_path, today=today)
    client = _authed_client(app)

    response = client.get("/review", params={"rank": "revenue"})

    assert response.status_code == 400
