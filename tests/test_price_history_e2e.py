"""E2E: price-as-of-date lookup + daily review on as-of pricing (Wave 2 slice 1).

ADR-0004 decision 2: every reporting surface costs sales at the net price in
effect on the sale's date, reconstructed from the audit log (each cost edit
snapshots the row's old/new ``price_per_unit_net`` + ``changed_at``);
pre-cutover sales use the seed price. Issue #28 is the foundation slice: the
lookup itself, plus the daily review moved onto it so a cost edit no longer
re-states history.

Each test is a worked example over the public interfaces (``PriceHistory``,
``SqliteConfigStore``, ``build_daily_review``) — no reaching into internals.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from tangerine.cost import CostBook
from tangerine.daily_review import build_daily_review
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import InMemoryLoyverseStore, SaleRecord
from tangerine.price_history import PriceChange, PriceHistory
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.types import Sale


def _seeded_store(
    tmp_path: Path, costs_yaml: str, clock: dict[str, str]
) -> SqliteConfigStore:
    """An in-memory config store seeded from a costs YAML (no recipes).

    ``clock["now"]`` is the injectable audit timestamp — mutate it between
    saves to simulate edits on different days.
    """
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text("recipes: []\nmappings: []\n", encoding="utf-8")
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(costs_yaml, encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    return SqliteConfigStore(conn, now=lambda: clock["now"])


# --- price_as_of: the lookup itself ------------------------------------------


def test_never_edited_sku_resolves_to_its_current_price_on_any_date() -> None:
    """A SKU with no cost-edit history costs at its seed/current price.

    Pre-cutover there is no audit history at all, so every SKU takes this
    path: the price the cost book holds today *is* the price on any past
    date (issue #28: "falling back to the seed/current price when the SKU
    has no cost-edit history").
    """
    history = PriceHistory(
        current=CostBook({"butter": (D("0.50"), date(2026, 6, 1))}),
        changes=[],
    )

    assert history.price_as_of("butter", date(2026, 7, 3)) == D("0.50")
    assert history.price_as_of("butter", date(2025, 1, 1)) == D("0.50")


def test_edited_once_seed_price_before_the_edit_new_price_from_its_day_on() -> None:
    """Issue #28's worked example: butter repriced on the 15th.

    The 3rd (before the edit) still costs at the seed price; the 15th and
    after cost at the new price. The edit's own day takes the new price —
    the partner repriced that morning, so that day's sales carry it.
    """
    history = PriceHistory(
        current=CostBook({"butter": (D("0.60"), date(2026, 7, 15))}),
        changes=[
            PriceChange(
                sku_id="butter",
                changed_on=date(2026, 7, 15),
                old_price=D("0.50"),
                new_price=D("0.60"),
            ),
        ],
    )

    assert history.price_as_of("butter", date(2026, 7, 3)) == D("0.50")
    assert history.price_as_of("butter", date(2026, 7, 15)) == D("0.60")
    assert history.price_as_of("butter", date(2026, 7, 20)) == D("0.60")


def test_edited_more_than_once_dates_between_edits_take_the_price_then_in_effect() -> None:
    """Issue #28 AC: a SKU edited twice resolves correctly between the edits.

    Milk repriced on 10 Jul and again on 20 Jul: the 5th costs at the
    seed, the 14th at the first edit's price, the 25th at the second's.
    """
    history = PriceHistory(
        current=CostBook({"milk": (D("0.030"), date(2026, 7, 20))}),
        changes=[
            PriceChange(
                sku_id="milk",
                changed_on=date(2026, 7, 10),
                old_price=D("0.020"),
                new_price=D("0.025"),
            ),
            PriceChange(
                sku_id="milk",
                changed_on=date(2026, 7, 20),
                old_price=D("0.025"),
                new_price=D("0.030"),
            ),
        ],
    )

    assert history.price_as_of("milk", date(2026, 7, 5)) == D("0.020")
    assert history.price_as_of("milk", date(2026, 7, 14)) == D("0.025")
    assert history.price_as_of("milk", date(2026, 7, 25)) == D("0.030")


def test_first_ever_price_reaches_back_over_the_days_before_it_was_entered() -> None:
    """A SKU's first-ever price also covers the days before it was entered.

    A cost row born from an edit (no seed) has no *recorded* price before
    that day — but history there was unknown, not different. The first
    known price is the only honest number available for those days, so the
    reach-back costs them instead of leaving them flagged unknown-price
    forever (the "authored everything today, history still shows nothing"
    gap). A later repricing still governs only from its own day.
    """
    history = PriceHistory(
        current=CostBook({"oat-milk": (D("0.045"), date(2026, 7, 12))}),
        changes=[
            PriceChange(
                sku_id="oat-milk",
                changed_on=date(2026, 7, 12),
                old_price=None,
                new_price=D("0.045"),
            ),
        ],
    )

    assert history.price_as_of("oat-milk", date(2026, 7, 5)) == D("0.045")
    assert history.price_as_of("oat-milk", date(2026, 7, 12)) == D("0.045")


def test_reach_back_takes_the_first_price_not_a_later_repricing() -> None:
    """Days before the creation cost at the *first* entered price.

    Oat milk is first priced on 12 Jul and repriced on 20 Jul: the 5th
    reaches back to the first price, the 25th carries the repricing. The
    repricing is a real forward-only change (ADR-0004 decision 2); only
    the unknown days before the first price are back-filled.
    """
    history = PriceHistory(
        current=CostBook({"oat-milk": (D("0.050"), date(2026, 7, 20))}),
        changes=[
            PriceChange(
                sku_id="oat-milk",
                changed_on=date(2026, 7, 12),
                old_price=None,
                new_price=D("0.045"),
            ),
            PriceChange(
                sku_id="oat-milk",
                changed_on=date(2026, 7, 20),
                old_price=D("0.045"),
                new_price=D("0.050"),
            ),
        ],
    )

    assert history.price_as_of("oat-milk", date(2026, 7, 5)) == D("0.045")
    assert history.price_as_of("oat-milk", date(2026, 7, 15)) == D("0.045")
    assert history.price_as_of("oat-milk", date(2026, 7, 25)) == D("0.050")


def test_a_reverted_creation_does_not_reach_back() -> None:
    """A creation undone by revert leaves no price on any date.

    The revert deleted the cost row — the creation was declared a mistake
    (Slice 5: a creation's revert deletes the row), so its price must not
    resurface as the cost of earlier days.
    """
    history = PriceHistory(
        current=CostBook(),  # the revert removed the row
        changes=[
            PriceChange(
                sku_id="typo-sku",
                changed_on=date(2026, 7, 12),
                old_price=None,
                new_price=D("0.045"),
            ),
            PriceChange(
                sku_id="typo-sku",
                changed_on=date(2026, 7, 13),
                old_price=D("0.045"),
                new_price=None,
            ),
        ],
    )

    assert history.price_as_of("typo-sku", date(2026, 7, 5)) is None
    assert history.price_as_of("typo-sku", date(2026, 7, 14)) is None


def test_unknown_sku_has_no_price_on_any_date() -> None:
    history = PriceHistory(current=CostBook(), changes=[])

    assert history.price_as_of("never-heard-of-it", date(2026, 7, 1)) is None


# --- cost_book_as_of: the margin engine's view --------------------------------


def test_cost_book_as_of_is_a_cost_book_frozen_at_that_date() -> None:
    """The margin engine consumes a ``CostBook``; ``cost_book_as_of`` builds
    one whose every price is the as-of-date answer.

    Butter was repriced on the 15th; oat milk did not exist until the 12th.
    The book as of the 3rd holds butter's seed price, and oat milk's
    first-ever price reaching back (effective-dated to the day it landed).
    """
    history = PriceHistory(
        current=CostBook(
            {
                "butter": (D("0.60"), date(2026, 7, 15)),
                "oat-milk": (D("0.045"), date(2026, 7, 12)),
                "beans": (D("0.80"), date(2026, 6, 1)),
            }
        ),
        changes=[
            PriceChange(
                sku_id="butter",
                changed_on=date(2026, 7, 15),
                old_price=D("0.50"),
                new_price=D("0.60"),
            ),
            PriceChange(
                sku_id="oat-milk",
                changed_on=date(2026, 7, 12),
                old_price=None,
                new_price=D("0.045"),
            ),
        ],
    )

    book = history.cost_book_as_of(date(2026, 7, 3))

    butter = book.price("butter")
    assert butter is not None and butter.price == D("0.50")
    oat_milk = book.price("oat-milk")
    assert oat_milk is not None and oat_milk.price == D("0.045")
    assert oat_milk.updated_at == date(2026, 7, 12)
    beans = book.price("beans")
    assert beans is not None and beans.price == D("0.80")


# --- the audit log is the history: SqliteConfigStore.price_history -----------


def test_price_history_reconstructed_from_real_cost_edits(tmp_path: Path) -> None:
    """Cost edits saved through the store become the price history.

    Worked example over the real audit log. Butter is seeded at a net 0.50
    THB/g; on 15 Jul the partner saves a new pack price deriving 0.60. The
    store's ``price_history()`` answers the seed price for the 3rd and the
    new price for the 15th onward — no new table, just the audit trail
    Slice 5 already writes.
    """
    clock = {"now": "2026-07-15T02:00:00+00:00"}
    store = _seeded_store(
        tmp_path,
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
        clock,
    )

    store.save_cost(
        "butter",
        pack_price=D("600"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 15),
    )

    history = store.price_history()
    assert history.price_as_of("butter", date(2026, 7, 3)) == D("0.50")
    assert history.price_as_of("butter", date(2026, 7, 15)) == D("0.600000")
    assert history.price_as_of("butter", date(2026, 7, 20)) == D("0.600000")


def test_early_morning_edit_takes_effect_on_the_partners_day_not_the_utc_day(
    tmp_path: Path,
) -> None:
    """An edit before ~7am local lands on the partner's calendar day.

    The audit clock is UTC but the venue runs at UTC+7: a repricing at
    01:30 local on the 16th is stamped ``changed_at`` 18:30 UTC *on the
    15th*. The save also records the partner-facing effective date
    (``updated_on`` = the app's local today, the 16th) in the cost row,
    and that date must govern — otherwise the edit would silently re-cost
    the 15th, a day already reviewed (issue #28: editing a cost never
    changes a previously-viewable day's numbers).
    """
    clock = {"now": "2026-07-15T18:30:00+00:00"}  # 16 Jul, 01:30 at UTC+7
    store = _seeded_store(
        tmp_path,
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
        clock,
    )

    store.save_cost(
        "butter",
        pack_price=D("600"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 16),
    )

    history = store.price_history()
    assert history.price_as_of("butter", date(2026, 7, 15)) == D("0.50")
    assert history.price_as_of("butter", date(2026, 7, 16)) == D("0.600000")


def test_price_history_ignores_non_cost_audit_entries(tmp_path: Path) -> None:
    """Recipe/mapping/SKU edits share the audit log but are not price changes."""
    clock = {"now": "2026-07-10T02:00:00+00:00"}
    store = _seeded_store(
        tmp_path,
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
        clock,
    )

    store.create_sku("oat-drink", name="Oat drink", unit="ml", created_by="noi")

    history = store.price_history()
    assert history.price_as_of("butter", date(2026, 7, 20)) == D("0.50")


# --- the daily review on as-of-date pricing -----------------------------------


def _sale(item_id: str, day: date, price: str, line: str) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=D(price)),
        receipt_number=f"r-{day.isoformat()}",
        line_id=line,
    )


def test_editing_a_cost_does_not_change_an_earlier_days_review(
    tmp_path: Path,
) -> None:
    """Issue #28's end-to-end behavior, through the real stores.

    A croissant (recipe: 10 g butter) sells on 3 Jul and on 16 Jul. On
    15 Jul the partner repricing butter doubles its net cost. Viewed on the
    16th, the 3 Jul review still shows the 3 Jul margin — bit-for-bit what
    it showed before the edit — while the 16 Jul review carries the new
    butter price. Previously both days would have been costed at the
    render-time price (the latent Wave 1 bug ADR-0004 decision 2 fixes).
    """
    clock = {"now": "2026-07-15T02:00:00+00:00"}
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text(
        """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "10" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
""",
        encoding="utf-8",
    )
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    config_store = SqliteConfigStore(conn, now=lambda: clock["now"])

    loyverse_store = InMemoryLoyverseStore()
    loyverse_store.record_sales(
        [
            _sale("i-croissant", date(2026, 7, 3), "80", "l-1"),
            _sale("i-croissant", date(2026, 7, 16), "80", "l-2"),
        ]
    )
    source = StoreSource(store=loyverse_store, config=config_store)

    before_edit = build_daily_review(source=source, review_date=date(2026, 7, 3))
    assert before_edit.cogs == D("5")  # 10 g x 0.50 THB/g

    # 15 Jul: butter now 1000 THB per kg -> net 1.00 THB/g.
    config_store.save_cost(
        "butter",
        pack_price=D("1000"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 15),
    )

    after_edit = build_daily_review(source=source, review_date=date(2026, 7, 3))
    assert after_edit.cogs == D("5")
    assert after_edit.gross_margin == before_edit.gross_margin
    assert after_edit.daily.item_margins == before_edit.daily.item_margins

    new_day = build_daily_review(source=source, review_date=date(2026, 7, 16))
    assert new_day.cogs == D("10")  # 10 g x 1.00 THB/g


def test_leaf_price_edit_reprices_prep_containing_dish_for_later_days_only(
    tmp_path: Path,
) -> None:
    """Issue #36 + ADR-0004 decision 2: derived costing composes with as-of-
    date pricing by construction. A leaf price edit reprices a prep-
    containing dish for later days only; earlier days keep the old derived
    cost — the prep's per-gram cost is recomputed against each day's cost
    book, never stored.

    A poke bowl's recipe uses 25 g of ahi sauce. Ahi sauce is a prep whose
    recipe is 100 g soy + 24 g mirin, yield 61 g. Soy sells on both 3 Jul
    and 16 Jul; on 15 Jul the partner doubles soy's net cost. The 3 Jul
    review still shows the 3 Jul derived sauce cost; the 16 Jul review
    carries the new one.

    This is the property the spreadsheet-vs-engine split risks losing if
    the spreadsheet cached the prep's per-gram cost anywhere. The engine
    never does — it re-derives against each day's cost book.
    """
    clock = {"now": "2026-07-15T02:00:00+00:00"}
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text(
        """
recipes:
  - sku_id: sauce-ahi
    name: Ahi Sauce
    segment: cafe
    yield: "61"
    yield_estimated: false
    ingredients:
      - { sku_id: soy-sauce, quantity: "100" }
      - { sku_id: mirin, quantity: "24" }
  - sku_id: poke-bowl
    name: Poke Bowl
    segment: cafe
    ingredients:
      - { sku_id: sauce-ahi, quantity: "25" }

mappings:
  - { item_id: i-poke, sku_id: poke-bowl }
""",
        encoding="utf-8",
    )
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(
        """
costs:
  soy-sauce: { price: "0.05", updated_at: "2026-06-01" }  # soy
  mirin: { price: "0.30", updated_at: "2026-06-01" }  # mirin
""",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    config_store = SqliteConfigStore(conn, now=lambda: clock["now"])

    loyverse_store = InMemoryLoyverseStore()
    loyverse_store.record_sales(
        [
            _sale("i-poke", date(2026, 7, 3), "200", "l-1"),
            _sale("i-poke", date(2026, 7, 16), "200", "l-2"),
        ]
    )
    source = StoreSource(store=loyverse_store, config=config_store)

    # Before edit: sauce batch = 100×0.05 + 24×0.30 = 5 + 7.20 = 12.20,
    # divided by 61 g yield = 0.20/g; 25 g in the dish = 5.00 THB.
    before_edit = build_daily_review(source=source, review_date=date(2026, 7, 3))
    assert before_edit.cogs == D("5")

    # 15 Jul: soy repriced 5x to 0.25/g.
    config_store.save_cost(
        "soy-sauce",
        pack_price=D("250"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 15),
    )

    # 3 Jul review unchanged — as-of-date pricing composes with derived
    # costing: the resolver runs against the 3-Jul cost book.
    after_edit = build_daily_review(source=source, review_date=date(2026, 7, 3))
    assert after_edit.cogs == D("5")
    assert after_edit.daily.item_margins == before_edit.daily.item_margins

    # 16 Jul review: new soy price. Sauce = 100×0.25 + 24×0.30 = 25 + 7.20
    # = 32.20, / 61 = 0.5278.../g; 25 g × 0.5278... ≈ 13.20 THB.
    # Decimal: 25 * 32.20 / 61 = 805 / 61 = 13.1967... -> round to 2dp the
    # engine actually carries. We assert the full-precision cogs Decimal
    # rather than guessing the rounding — the resolver divides straight
    # through without quantising.
    new_day = build_daily_review(source=source, review_date=date(2026, 7, 16))
    # Derived: 25 × ((100 × 0.25 + 24 × 0.30) / 61) = 25 × 32.20 / 61
    #        = 805 / 61 (THB — exact decimal, not quantised mid-derivation).
    assert new_day.cogs == D("805") / D("61")


def test_daily_review_totals_include_prep_containing_dish(tmp_path: Path) -> None:
    """A dish whose recipe uses a prep counts in the daily review totals —
    revenue, COGS, and gross margin — once its prep is fully priced.

    Issue #36 isn't just a per-item change: the higher-level surfaces the
    partner sees (daily review's revenue/COGS/margin rollups) must carry
    prep-containing dishes too. Before #36 such a dish was flagged
    ``unknown_price`` and excluded from totals; with derived costing it
    contributes like any other reliable row.

    Worked example. Two dishes sold on 3 Jul:
      - plain-rice (200 g rice at 0.03/g = 6 THB cost, 50 THB revenue)
      - poke-bowl (25 g ahi sauce at 0.20/g = 5 THB cost, 200 THB revenue)
    Daily totals: revenue 250, COGS 11, gross margin 239.
    """
    clock = {"now": "2026-07-03T02:00:00+00:00"}
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text(
        """
recipes:
  - sku_id: sauce-ahi
    name: Ahi Sauce
    segment: cafe
    yield: "61"
    yield_estimated: false
    ingredients:
      - { sku_id: soy-sauce, quantity: "100" }
      - { sku_id: mirin, quantity: "24" }
  - sku_id: poke-bowl
    name: Poke Bowl
    segment: cafe
    ingredients:
      - { sku_id: sauce-ahi, quantity: "25" }
  - sku_id: plain-rice
    name: Plain Rice
    segment: cafe
    ingredients:
      - { sku_id: rice, quantity: "200" }

mappings:
  - { item_id: i-poke, sku_id: poke-bowl }
  - { item_id: i-rice, sku_id: plain-rice }
""",
        encoding="utf-8",
    )
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(
        """
costs:
  soy-sauce: { price: "0.05", updated_at: "2026-06-01" }
  mirin: { price: "0.30", updated_at: "2026-06-01" }
  rice: { price: "0.03", updated_at: "2026-06-01" }
""",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    config_store = SqliteConfigStore(conn, now=lambda: clock["now"])

    loyverse_store = InMemoryLoyverseStore()
    loyverse_store.record_sales(
        [
            _sale("i-poke", date(2026, 7, 3), "200", "l-1"),
            _sale("i-rice", date(2026, 7, 3), "50", "l-2"),
        ]
    )
    source = StoreSource(store=loyverse_store, config=config_store)

    review = build_daily_review(source=source, review_date=date(2026, 7, 3))

    # 200 (poke) + 50 (rice) = 250 THB revenue.
    assert review.revenue == D("250")
    # 5 (poke sauce) + 6 (rice) = 11 THB COGS.
    assert review.cogs == D("11")
    # 250 - 11 = 239 THB gross margin.
    assert review.gross_margin == D("239")


def test_authoring_an_item_today_heals_the_days_it_sold_unmapped(
    tmp_path: Path,
) -> None:
    """The back-fill story end to end: history self-heals once authored.

    An oat latte sold on 3 Jul while the tool knew nothing about it — no
    mapping, no recipe, no priced ingredient — so that day's review carried
    it as unmapped, outside the headline numbers. On the 18th the partner
    authors the whole chain in the UI: ingredient SKU + its first cost,
    sold SKU + recipe, item mapping. Mappings and recipes always read at
    current state, and the first-ever price reaches back, so re-opening the
    3 Jul review now shows the sale fully costed — no backfill job, no
    migration, just the next page load.
    """
    clock = {"now": "2026-07-18T02:00:00+00:00"}
    store = _seeded_store(
        tmp_path,
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # unrelated seed
""",
        clock,
    )

    loyverse_store = InMemoryLoyverseStore()
    loyverse_store.record_sales([_sale("i-oat-latte", date(2026, 7, 3), "120", "l-1")])
    source = StoreSource(store=loyverse_store, config=store)

    before = build_daily_review(source=source, review_date=date(2026, 7, 3))
    assert any(im.item_id == "i-oat-latte" for im in before.unmapped_items)
    assert before.cogs == D("0")

    # 18 Jul: the partner authors the whole chain through the store.
    store.create_sku("oat-milk", name="Oat milk", unit="ml", created_by="daniel")
    store.save_cost(
        "oat-milk",
        pack_price=D("45"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 18),
    )
    store.create_sku("oat-latte", name="Oat Latte", unit="unit", created_by="daniel")
    store.save_recipe(
        "oat-latte",
        ingredients=[("oat-milk", D("200"))],
        yield_qty=D("1"),
        yield_estimated=False,
        updated_by="daniel",
    )
    store.save_mapping("i-oat-latte", "oat-latte", updated_by="daniel")

    healed = build_daily_review(source=source, review_date=date(2026, 7, 3))
    assert not any(im.item_id == "i-oat-latte" for im in healed.unmapped_items)
    assert healed.revenue == D("120")
    # 200 ml x 0.045 THB/ml = 9 THB.
    assert healed.cogs == D("9")
    assert healed.gross_margin == D("111")
