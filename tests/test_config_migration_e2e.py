"""End-to-end config migration seam (Wave 1.5, Step 1).

The config migration moves recipes, costs, and mappings from the YAML files
into SQLite tables (ADR-0003 decision 1). The engine then reads live from
SQLite instead of from in-memory objects captured at app construction. No UI
in this step — the verifiable bar is that the 9am review produces identical
margin numbers post-migration (except the VAT fix on clearly-Makro rows).

Per the PRD testing rules the only genuine boundary here is the SQLite
connection (``:memory:`` for tests) and the filesystem (real temp YAML files).
These tests read as worked examples: a YAML file goes in; the same engine
shapes come back out through SQLite.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from tangerine.config.loader import load_costs, load_recipes
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import InMemoryLoyverseStore, SaleRecord
from tangerine.margin import compute_daily_margin
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Recipe, RecipeIngredient, Sale, Segment, SkuMapping
from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

_TEST_PASSPHRASE = "step1-migration-test-passphrase"
_TEST_SIGNING_SECRET = "step1-migration-test-signing-secret"

D = Decimal


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


# --- AC: recipes round-trip through SQLite -----------------------------------


def test_recipe_round_trips_through_sqlite(tmp_path: Path) -> None:
    """A recipe seeded from YAML is readable back as the same ``Recipe`` shape.

    Worked example. One cafe recipe (espresso latte: 20 g beans + 200 ml milk,
    target margin 70%) is seeded into an empty ``:memory:`` database. Reading
    ``SqliteConfigStore.recipes()`` yields exactly one ``Recipe`` whose every
    field — sku_id, name, segment, ingredients, yield, target margin — matches
    what the YAML expressed. This proves the migration infrastructure works
    end-to-end: DDL, seeder, and read-side all cooperate.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }
    target_gross_margin_pct: "70"
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml)

    store = SqliteConfigStore(conn)
    recipes = store.recipes()

    assert len(recipes) == 1
    recipe = recipes[0]
    assert recipe == Recipe(
        sku_id="espresso-latte",
        name="Espresso Latte",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="beans-arabica", quantity=D("20")),
            RecipeIngredient(sku_id="milk-fresh", quantity=D("200")),
        ),
        yield_units=1,
        target_gross_margin_pct=D("70"),
    )


# --- AC: costs round-trip through SQLite -------------------------------------


def test_costs_round_trip_through_sqlite(tmp_path: Path) -> None:
    """A cost seeded from YAML is readable back as the same per-unit price.

    Worked example. Two SKUs are costed in ``costs.yaml``: beans-arabica at
    2.00/g and milk-fresh at 0.025/ml, both dated 2026-06-01, both with
    comments naming *wet-market* suppliers (not Makro/ARO). After seeding,
    the cost book returns the exact same per-unit prices — no VAT adjustment
    is applied because the migrator only divides by 1.07 for clearly
    VAT-registered suppliers (ADR-0003 decision 4).
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }
""",
    )
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  beans-arabica: { price: "2", updated_at: "2026-06-01" }  # Chann Poochar farm stall
  milk-fresh: { price: "0.025", updated_at: "2026-06-01" }  # local dairy
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    book = SqliteConfigStore(conn).cost_book()

    beans = book.price("beans-arabica")
    assert beans is not None
    assert beans.price == D("2")
    assert beans.updated_at == date(2026, 6, 1)

    milk = book.price("milk-fresh")
    assert milk is not None
    assert milk.price == D("0.025")
    assert milk.updated_at == date(2026, 6, 1)


# --- AC: VAT-inclusive costs are stored net ----------------------------------


def test_makro_cost_is_stored_net_of_vat(tmp_path: Path) -> None:
    """A cost whose comment names Makro/ARO is divided by 1.07 on seed.

    Worked example. Two SKUs are costed: ``butter`` at 0.190 THB/g bought from
    Makro (VAT-registered) and ``basil`` at 0.097 THB/g bought from a wet
    market stall (no VAT). After migration the butter cost is stored *net*
    (~0.1776 THB/g) while the basil cost is stored unchanged. This is the
    latent ~7%-of-COGS bug being fixed on cutover (ADR-0003 decision 4): every
    historical margin rises slightly for VAT-registered inputs.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: croissant-butter
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "30" }
      - { sku_id: basil, quantity: "2" }
""",
    )
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  butter: { price: "0.190", updated_at: "2026-06-30" }  # MAKRO Allowrie Butter 2 kg
  basil: { price: "0.097", updated_at: "2026-06-30" }  # wet market sweet basil
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    book = SqliteConfigStore(conn).cost_book()

    # Makro: gross 0.190 / 1.07 = 0.177570... rounded to 6 places.
    butter = book.price("butter")
    assert butter is not None
    assert butter.price == D("0.177570")
    assert butter.updated_at == date(2026, 6, 30)

    # Wet market: unchanged.
    basil = book.price("basil")
    assert basil is not None
    assert basil.price == D("0.097")
    assert basil.updated_at == date(2026, 6, 30)


# --- AC: SKU unit is derived from the cost comment's pack size (ADR-0003 #3) -


def test_cost_only_sku_gets_a_skus_row_with_unit_derived_from_weight_comment(
    tmp_path: Path,
) -> None:
    """A costed ingredient with no recipe of its own still gets a ``skus`` row.

    Worked example. ``almond-ground`` is never a recipe's own ``sku_id`` — it
    only appears as a cost. Today's seeder never writes a ``skus`` row for it
    at all, so there is nowhere to record its unit. After seeding, a ``skus``
    row exists for it with ``unit='g'``, derived from the pack-size comment
    ("500 g") per ADR-0003 decision 3.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(recipes_yaml, "recipes: []\n")
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  almond-ground: { price: "0.458", updated_at: "2026-06-30" }  # ARO Almond Powder 500 g
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    row = conn.execute(
        "SELECT unit FROM skus WHERE sku_id = 'almond-ground'"
    ).fetchone()
    assert row is not None, "expected a skus row for a cost-only ingredient"
    assert row[0] == "g"


def test_volume_comment_derives_unit_ml(tmp_path: Path) -> None:
    """A pack-size comment naming a volume ("650 ml") derives ``unit='ml'``."""
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(recipes_yaml, "recipes: []\n")
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  oil-sesame: { price: "0.212", updated_at: "2026-06-30" }  # ARO Sesame Oil 650 ml
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    row = conn.execute("SELECT unit FROM skus WHERE sku_id = 'oil-sesame'").fetchone()
    assert row == ("ml",)


def test_count_comment_derives_unit_unit(tmp_path: Path) -> None:
    """A pack-size comment naming a count ("6 pcs") derives ``unit='unit'``."""
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(recipes_yaml, "recipes: []\n")
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  lemon: { price: "15.3", updated_at: "2026-06-30" }  # ARO Lemon 6 pcs
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    row = conn.execute("SELECT unit FROM skus WHERE sku_id = 'lemon'").fetchone()
    assert row == ("unit",)


def test_price_per_kg_slash_notation_derives_unit_g(tmp_path: Path) -> None:
    """A Thai-retail "X/kg" comment (no space, price-per-kg) still resolves.

    Worked example. ``chicken-thigh``'s real comment is
    ``"Makro 79/kg (06-20)"`` — the price-per-kilo shorthand used throughout
    ``costs.yaml`` for market meat/fish prices, distinct from the
    space-separated pack-size style ("500 g"). Both name a weight and must
    resolve to the same ``unit='g'``.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(recipes_yaml, "recipes: []\n")
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  chicken-thigh: { price: "0.079", updated_at: "2026-06-30" }  # Makro 79/kg (06-20)
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    row = conn.execute("SELECT unit FROM skus WHERE sku_id = 'chicken-thigh'").fetchone()
    assert row == ("g",)


def test_ambiguous_comment_leaves_unit_null_and_queryable(tmp_path: Path) -> None:
    """A comment with no pack-size token leaves ``unit`` NULL, not a guess.

    Worked example. ``egg``'s real ``costs.yaml`` comment ("Makro 120/30
    (06-20)") names neither a weight, volume, nor count token — deriving
    "unit" (each egg) would require outside knowledge the text doesn't carry.
    Per ADR-0003 decision 3 this must stay unresolved rather than guessed, and
    ``unit IS NULL`` is how a partner-confirmation UI would query for it later.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(recipes_yaml, "recipes: []\n")
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  egg: { price: "4.00", updated_at: "2026-06-30" }  # Makro 120/30 (06-20)
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    row = conn.execute("SELECT unit FROM skus WHERE sku_id = 'egg'").fetchone()
    assert row == (None,)
    ambiguous = conn.execute(
        "SELECT sku_id FROM skus WHERE unit IS NULL"
    ).fetchall()
    assert ("egg",) in ambiguous


def test_costed_recipe_output_keeps_its_segment_and_only_backfills_unit(
    tmp_path: Path,
) -> None:
    """A SKU that is both a recipe's own output and separately costed (a
    batch-brewed concentrate, say) keeps the name/segment ``_seed_recipes``
    gave it — seeding costs only fills in ``unit`` where it was unknown, it
    never clobbers an existing row's identity.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: coffee-latte-con
    name: Latte Concentrate
    segment: cafe
    ingredients:
      - { sku_id: coffee-beans-house, quantity: "100" }
""",
    )
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  coffee-latte-con: { price: "0.0858", updated_at: "2026-06-30" }  # calc 171.6 THB / 2000g batch
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    row = conn.execute(
        "SELECT name, segment, unit FROM skus WHERE sku_id = 'coffee-latte-con'"
    ).fetchone()
    assert row == ("Latte Concentrate", "cafe", "g")


# --- AC: mappings round-trip through SQLite ----------------------------------


def test_mappings_round_trip_through_sqlite(tmp_path: Path) -> None:
    """A Loyverse-item -> SKU mapping seeded from YAML is readable back.

    Worked example. One recipe (Chang Draft) is mapped from a raw Loyverse
    item id (``i-1``) that does not equal the SKU id. After seeding,
    ``SqliteConfigStore.mappings()`` returns that exact mapping — the same
    shape the item coverage view (Step 2) and the margin engine's
    ``RecipeCatalog`` both key off.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }

mappings:
  - { item_id: i-1, sku_id: chang-draft-500 }
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml)

    mappings = SqliteConfigStore(conn).mappings()

    assert mappings == [SkuMapping(item_id="i-1", sku_id="chang-draft-500")]


# --- AC: StoreSource delegates to the SQLite config store --------------------


def test_store_source_delegates_config_reads_to_sqlite(tmp_path: Path) -> None:
    """``StoreSource(store, config=SqliteConfigStore(...))`` reads live from SQLite.

    Worked example. A recipe, a cost, and a mapping are seeded into SQLite.
    A ``StoreSource`` wired with ``config=`` (no in-memory ``recipes=``/
    ``cost=``/``mappings=`` args) resolves its ``recipes()``, ``cost_book()``,
    and ``mappings()`` from the store — proving the delegation this slice
    adds, not the in-memory fallback the constructor still supports for
    existing callers.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }

mappings:
  - { item_id: i-1, sku_id: chang-draft-500 }
""",
    )
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }  # wet market keg deposit
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)
    config_store = SqliteConfigStore(conn)

    loyverse_store = InMemoryLoyverseStore()
    source = StoreSource(store=loyverse_store, config=config_store)

    assert source.recipes() == config_store.recipes()
    assert source.mappings() == config_store.mappings()
    assert source.cost_book().price("chang-keg") == config_store.cost_book().price(
        "chang-keg"
    )


def test_store_source_still_uses_in_memory_lists_without_config(tmp_path: Path) -> None:
    """Existing callers that pass ``recipes=``/``cost=``/``mappings=`` directly
    (no ``config=``) keep working unchanged — the SQLite delegation is opt-in.

    This is the backward-compatibility guarantee every pre-existing
    ``StoreSource`` call site (tests, the CLI) depends on.
    """
    loyverse_store = InMemoryLoyverseStore()
    recipe = Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )

    source = StoreSource(store=loyverse_store, recipes=[recipe])

    assert source.recipes() == [recipe]
    assert source.mappings() == []
    assert source.cost_book().price("chang-keg") is None


# --- AC: migration verification — identical margins except the VAT fix ------


def _yaml_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Two recipes (one Makro-costed, one wet-market-costed) + their costs.

    ``croissant-butter`` (cafe) uses Makro butter — its cost drops (net) after
    migration, so its margin rises. ``chang-draft-500`` (bar) uses a
    wet-market keg — unaffected, so its margin is bit-for-bit identical
    pre/post-migration. This is the PRD's Step 1 verification shape: "9am
    review shows identical numbers post-migration (except the VAT fix on
    clearly-Makro rows)."
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: croissant-butter
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "30" }
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
""",
    )
    costs_yaml = tmp_path / "costs.yaml"
    _write(
        costs_yaml,
        """
costs:
  butter: { price: "0.190", updated_at: "2026-06-30" }  # MAKRO Allowrie Butter 2 kg
  chang-keg: { price: "0.07", updated_at: "2026-06-30" }  # wet market keg deposit
""",
    )
    return recipes_yaml, costs_yaml


def _sale(item_id: str, price: str) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=date(2026, 6, 30), sell_price=D(price)),
        receipt_number="r-1",
        line_id=f"li-{item_id}",
    )


def test_migrated_margins_match_yaml_except_the_vat_fix(tmp_path: Path) -> None:
    """Same day, same sales, two sources (YAML in-memory vs SQLite-migrated):
    every field agrees except the Makro-costed item's margin, which rises.

    Worked example. One croissant sold at 60 THB (Makro butter ingredient)
    and one Chang draft sold at 120 THB (wet-market keg ingredient), same day,
    fed through two ``Source`` implementations built from the same YAML
    fixture: the pre-migration in-memory path (``load_recipes``/``load_costs``
    feeding ``StoreSource`` directly, gross costs, today's behaviour) and the
    post-migration SQLite path (``seed_config`` + ``SqliteConfigStore``, net
    costs). The Chang row is bit-for-bit identical. The croissant row's cost
    per unit, cogs, and gross margin all improve because the Makro butter
    cost is now correctly net of VAT — this is the one-time jump ADR-0003
    documents, not a regression.
    """
    recipes_yaml, costs_yaml = _yaml_fixture(tmp_path)
    sales = [_sale("croissant-butter", "60"), _sale("chang-draft-500", "120")]

    # Pre-migration: today's path — YAML loaded straight into memory.
    pre_store = InMemoryLoyverseStore()
    pre_store.record_sales(sales)
    pre_catalog = load_recipes(recipes_yaml)
    pre_cost = load_costs(costs_yaml)
    pre_source = StoreSource(
        store=pre_store, recipes=list(pre_catalog.all()), cost=pre_cost
    )
    pre_margin = compute_daily_margin(pre_source, date(2026, 6, 30))

    # Post-migration: YAML seeded into SQLite, engine reads live from there.
    post_store = InMemoryLoyverseStore()
    post_store.record_sales(sales)
    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)
    post_source = StoreSource(store=post_store, config=SqliteConfigStore(conn))
    post_margin = compute_daily_margin(post_source, date(2026, 6, 30))

    pre_by_item = {im.item_id: im for im in pre_margin.item_margins}
    post_by_item = {im.item_id: im for im in post_margin.item_margins}

    # The wet-market-costed item is untouched by the migration.
    chang_pre = pre_by_item["chang-draft-500"]
    chang_post = post_by_item["chang-draft-500"]
    assert chang_post == chang_pre

    # The Makro-costed item's margin rises: net cost is lower than gross.
    croissant_pre = pre_by_item["croissant-butter"]
    croissant_post = post_by_item["croissant-butter"]
    assert croissant_post.cost_per_unit < croissant_pre.cost_per_unit
    assert croissant_post.cogs < croissant_pre.cogs
    assert croissant_post.gross_margin > croissant_pre.gross_margin
    # Everything else about the row (identity, units, revenue) is unchanged.
    assert croissant_post.item_id == croissant_pre.item_id
    assert croissant_post.units_sold == croissant_pre.units_sold
    assert croissant_post.revenue == croissant_pre.revenue


# --- AC: create_app is wired to the SQLite-backed source --------------------


def _write_assignees(tmp_path: Path) -> Path:
    path = tmp_path / "assignees.yaml"
    _write(
        path,
        """
assignees:
  - assignee_id: daniel
    name: Daniel
""",
    )
    return path


def _authed_client(app) -> TestClient:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a session cookie"
    return client


def test_create_app_renders_review_from_sqlite_migrated_config(tmp_path: Path) -> None:
    """``GET /`` reflects the SQLite-migrated (net-of-VAT) cost, not the raw
    YAML gross cost — proving ``create_app`` now seeds and reads config from
    SQLite rather than loading it straight into memory at every startup.

    Worked example. One croissant (Makro butter, 30 g) sells for 60 THB.
    Gross costing (today's pre-migration behaviour) would show a 54.30 THB
    margin; net costing (post-migration, ADR-0003 decision 4) shows 54.67 THB.
    The headline section showing 54.67 — not 54.30 — is the observable proof
    that the web app's ``StoreSource`` is reading live from
    ``SqliteConfigStore``, seeded by ``seed_config`` on first request.
    """
    recipes_yaml, costs_yaml = _yaml_fixture(tmp_path)
    assignees_yaml = _write_assignees(tmp_path)
    db_path = str(tmp_path / "tangerine.db")
    today = date(2026, 7, 1)
    yesterday = today - timedelta(days=1)

    # Seed a sale into the SQLite DB before the app opens its own connection
    # to it, exactly as the nightly sync would ahead of a morning request.
    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(
        [
            SaleRecord(
                sale=Sale(
                    item_id="croissant-butter",
                    timestamp=yesterday,
                    sell_price=D("60"),
                ),
                receipt_number="r-1",
                line_id="li-1",
            )
        ]
    )
    store.close()

    app = create_app(
        db_path=db_path,
        recipes_path=str(recipes_yaml),
        costs_path=str(costs_yaml),
        assignees_path=str(assignees_yaml),
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )

    client = _authed_client(app)
    response = client.get("/")

    assert response.status_code == 200
    assert "54.67" in response.text
    assert "54.30" not in response.text


# --- AC: seeding is idempotent; audit_log starts empty -----------------------


def test_seed_config_twice_does_not_duplicate_rows(tmp_path: Path) -> None:
    """Calling ``seed_config`` a second time on an already-seeded DB is a no-op.

    Worked example. The same recipe is seeded twice. The second call must not
    duplicate the recipe row (which would otherwise multiply every ingredient
    row and corrupt a recipe's cost). This is what makes calling
    ``seed_config`` unconditionally on every ``create_app`` startup safe.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml)
    seed_config(conn, recipes_path=recipes_yaml)

    recipes = SqliteConfigStore(conn).recipes()
    assert len(recipes) == 1
    assert len(recipes[0].ingredients) == 1


def test_audit_log_table_exists_and_starts_empty(tmp_path: Path) -> None:
    """The ``audit_log`` table is created by migration but no row is written
    during seeding — Step 1 ships the table; Step 3+ starts writing to it.

    Worked example. After a normal seed, ``audit_log`` has zero rows. The
    table's existence (not raising ``sqlite3.OperationalError: no such
    table``) plus its emptiness are both part of Step 1's done-definition.
    """
    recipes_yaml = tmp_path / "recipes.yaml"
    _write(
        recipes_yaml,
        """
recipes:
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
""",
    )

    conn = _connect()
    seed_config(conn, recipes_path=recipes_yaml)

    row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    assert row[0] == 0
