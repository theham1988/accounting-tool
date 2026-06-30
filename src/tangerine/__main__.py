"""CLI entrypoint: print the daily 9am review against persisted data.

    python -m tangerine

Wave 1, Slice 1: the CLI loads recipes and costs from YAML config files, opens
the SQLite store, and builds the daily review against whatever sales are
persisted. On a fresh database the review is empty (sales arrive via Slice 3's
sync, not yet built); the CLI surfaces that gracefully rather than crashing.

Paths are configurable so tests can drive the CLI in-process without env
mutation. The real entrypoint (run by ``python -m tangerine``) reads defaults
from environment variables and delegates to :func:`main`.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

from .config.loader import load_costs, load_recipes, validate_mappings_against_menu
from .daily_review import DailyReview, build_daily_review
from .loyverse.source import StoreSource
from .storage.sqlite_store import SqliteLoyverseStore
from .types import Sale

#: Default config file locations (operator-editable, shipped with the repo).
DEFAULT_RECIPES_PATH = "config/recipes.yaml"
DEFAULT_COSTS_PATH = "config/costs.yaml"

#: Environment variable holding the SQLite database path. Lives in the
#: environment, not the repo, per ADR-0001 ("credentials and connection
#: configuration live in environment variables").
DB_PATH_ENV = "TANGERINE_DB_PATH"
DEFAULT_DB_PATH = "./tangerine.db"


def main(
    *,
    db_path: str | None = None,
    recipes_path: str | None = None,
    costs_path: str | None = None,
) -> None:
    """Print the daily review against persisted data.

    ``db_path`` defaults to ``$TANGERINE_DB_PATH`` or ``./tangerine.db``.
    ``recipes_path`` / ``costs_path`` default to the shipped config files. All
    three are explicit parameters so tests can drive the CLI without touching
    the environment.
    """
    db = db_path or os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)
    recipes_yaml = recipes_path or DEFAULT_RECIPES_PATH
    costs_yaml = costs_path or DEFAULT_COSTS_PATH

    catalog = load_recipes(recipes_yaml)
    cost = load_costs(costs_yaml)
    store = SqliteLoyverseStore.connect(db)
    try:
        # Cross-check every mapping's item_id against the synced Loyverse menu
        # before doing any work. A ghost mapping (item_id that matches nothing
        # on the menu) would silently zero every sale it should have resolved;
        # this catches it at startup instead. No-op on a fresh DB (empty menu)
        # so cold start is not blocked before the first sync.
        validate_mappings_against_menu(
            list(catalog.mappings()), store.current_menu()
        )
        source = StoreSource(
            store=store, recipes=list(catalog.all()), cost=cost
        )
        review_date = _pick_review_date(source.sales())
        review = build_daily_review(source=source, review_date=review_date)
        _print_review(review)
    finally:
        store.close()


def _pick_review_date(sales: list[Sale]) -> date:
    """The review date: the most recent day with sales, else today.

    Against an empty DB this falls back to today so the review prints an empty
    page rather than raising. Against real data it shows the latest day with
    sales — the seeded-CLI behaviour of "show the most recent review".
    """
    if not sales:
        return date.today()
    return max(s.timestamp for s in sales)


def _money(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"))


def _print_review(review: DailyReview) -> None:
    print(f"Daily 9am review for {review.day}:")
    print(
        f"  revenue:       {_money(review.revenue)} THB   "
        f"COGS: {_money(review.cogs)} THB   "
        f"gross margin: {_money(review.gross_margin)} THB"
    )
    print("  segment contribution margin:")
    for sm in review.segment_margins:
        flag = "  [RED]" if sm.is_red else ""
        print(
            f"    [{sm.segment.value}] CM={_money(sm.contribution_margin)} THB"
            f"  (revenue={_money(sm.revenue)}, variable_costs={_money(sm.variable_costs)}){flag}"
        )
    print(f"  top by margin:   {[(im.name, _money(im.gross_margin)) for im in review.top_by_margin.items]}")
    print(f"  bottom by margin:{[(im.name, _money(im.gross_margin)) for im in review.bottom_by_margin.items]}")
    print(f"  top by volume:   {[(im.name, im.units_sold) for im in review.top_by_volume.items]}")
    print(f"  bottom by volume:{[(im.name, im.units_sold) for im in review.bottom_by_volume.items]}")
    if review.below_target_items:
        print(f"  below target:    {[im.name for im in review.below_target_items]}")
    if review.unmapped_items:
        print(f"  unmapped:        {[im.item_id for im in review.unmapped_items]}")
    if review.anomaly_flags:
        print("  anomaly flags:")
        for f in review.anomaly_flags:
            print(f"    [{f.kind.value}] {f.detail}")
    goal = review.goal
    progress = "MET" if goal.met else "MISSING"
    print(
        f"  goal: {progress}  7-day avg {_money(goal.rolling_average)} THB/day "
        f"vs target {_money(goal.target)} (surplus {_money(goal.surplus)} THB; "
        f"{goal.days_in_window} days)"
    )


if __name__ == "__main__":
    main()
