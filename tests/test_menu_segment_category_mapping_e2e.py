"""End-to-end tests for configurable Loyverse-category → segment mapping.

Implements the post-#65 reframe of issue #68: under pure-clock segmentation
(#65 / ADR-0007) a sale's *revenue* segment comes from the receipt timestamp,
not the menu item's category. But the menu item's ``segment`` still matters
for menu-shape views (``/items``, ``/skus``) and for the sold-as-is quick-
create, which inherits the menu item's segment onto the sold SKU it creates.

The bug: ``CAFE_CATEGORY_ID = "cat-cafe"`` (a slice-02 placeholder) never
matches a real Loyverse category UUID, so every menu item is tagged ``bar``.
That corrupts the menu-shape views and flows the wrong segment into sold-as-
is quick-creates.

The fix: the parser takes a configurable set of cafe category UUIDs. An item
whose ``category_id`` is in the set is cafe; everything else is bar. The
default empty set preserves the observable "everything is bar" behaviour of
the placeholder-but-never-matches bug, but honestly and with the production
UUIDs configured via ``LOYVERSE_CAFE_CATEGORY_IDS``.

These tests are written first (TDD) and currently fail because the parser
does not yet accept ``cafe_category_ids``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tangerine.loyverse.config import (
    CAFE_CATEGORY_IDS_ENV,
    cafe_category_ids_from_env,
    parse_cafe_category_ids,
)
from tangerine.loyverse.parser import parse_items_snapshot
from tangerine.loyverse.store import InMemoryLoyverseStore
from tangerine.types import Segment


def _item(
    *,
    item_id: str,
    name: str,
    sku: str,
    category_id: str,
    price: float = 100,
) -> dict[str, Any]:
    """One minimal Loyverse ``/items`` entry with an explicit ``category_id``.

    The category id is the field under test, so unlike the shared ``_item_json``
    helper in ``test_loyverse_sync_e2e.py`` it has no default — every test must
    name the category it is exercising.
    """
    return {
        "id": item_id,
        "item_name": name,
        "category_id": category_id,
        "variants": [
            {
                "variant_id": f"{item_id}-v1",
                "option1_value": name,
                "sku": sku,
                "default_price": price,
            }
        ],
    }


# --- AC 1: the parser resolves segment from a configured category set --------


def test_parse_items_snapshot_tags_cafe_when_category_is_configured_cafe() -> None:
    """An item whose ``category_id`` is in ``cafe_category_ids`` is cafe.

    This is the headline fix: the production cafe category UUID (which a
    partner dumps via ``scripts/dump_loyverse_items.py``) is configured once,
    and every item in that category carries the cafe segment.
    """
    snapshot = parse_items_snapshot(
        {
            "items": [
                _item(
                    item_id="i-latte",
                    name="Latte",
                    sku="latte",
                    category_id="deadbeef-cafe-0000-0000-000000000001",
                ),
            ]
        },
        cafe_category_ids=frozenset(
            {"deadbeef-cafe-0000-0000-000000000001"}
        ),
    )

    by_id = {mi.item_id: mi for mi in snapshot.items}
    assert by_id["latte"].segment is Segment.CAFE


def test_parse_items_snapshot_tags_bar_when_category_is_not_configured_cafe() -> None:
    """An item whose ``category_id`` is not in ``cafe_category_ids`` is bar.

    The complement of the cafe set: the venue has exactly two categories, so
    "not cafe" is bar by construction.
    """
    snapshot = parse_items_snapshot(
        {
            "items": [
                _item(
                    item_id="i-chang",
                    name="Chang Draught",
                    sku="chang-draught",
                    category_id="deadbeef-bar-0000-0000-000000000002",
                ),
            ]
        },
        cafe_category_ids=frozenset(
            {"deadbeef-cafe-0000-0000-000000000001"}
        ),
    )

    by_id = {mi.item_id: mi for mi in snapshot.items}
    assert by_id["chang-draught"].segment is Segment.BAR


def test_parse_items_snapshot_defaults_everything_to_bar_with_empty_cafe_set() -> None:
    """With no cafe categories configured (the default), every item is bar.

    This is the honest restatement of the placeholder bug: ``cat-cafe`` never
    matched a real UUID so every item was bar *de facto*; with the fix, an
    empty cafe set makes every item bar *de jure*. The behaviour is preserved
    so a fresh deployment starts correct (segment-correctness sharpens as the
    partner configures the real cafe UUID, never the reverse).
    """
    snapshot = parse_items_snapshot(
        {
            "items": [
                _item(
                    item_id="i-a",
                    name="Anything",
                    sku="a",
                    category_id="some-real-cafe-uuid-here",
                ),
            ]
        },
        cafe_category_ids=frozenset(),
    )

    by_id = {mi.item_id: mi for mi in snapshot.items}
    assert by_id["a"].segment is Segment.BAR


def test_parse_items_snapshot_accepts_multiple_cafe_categories() -> None:
    """More than one Loyverse category can map to cafe.

    The venue has one cafe category today, but Loyverse allows sub-categories
    (e.g. 'Hot Coffee' and 'Cold Brew' as distinct category UUIDs both served
    in the cafe). The set shape carries this without a code change.
    """
    snapshot = parse_items_snapshot(
        {
            "items": [
                _item(
                    item_id="i-hot",
                    name="Hot Latte",
                    sku="hot-latte",
                    category_id="cafe-cat-hot",
                ),
                _item(
                    item_id="i-cold",
                    name="Cold Brew",
                    sku="cold-brew",
                    category_id="cafe-cat-cold",
                ),
                _item(
                    item_id="i-beer",
                    name="Chang",
                    sku="chang",
                    category_id="bar-cat",
                ),
            ]
        },
        cafe_category_ids=frozenset({"cafe-cat-hot", "cafe-cat-cold"}),
    )

    by_id = {mi.item_id: mi for mi in snapshot.items}
    assert by_id["hot-latte"].segment is Segment.CAFE
    assert by_id["cold-brew"].segment is Segment.CAFE
    assert by_id["chang"].segment is Segment.BAR


# --- AC 2: the default matches the placeholder's observable behaviour --------


def test_parse_items_snapshot_default_cafe_set_is_empty() -> None:
    """Omitting ``cafe_category_ids`` is equivalent to an empty set (all bar).

    Regression guard for the empty-default contract: a caller that doesn't
    pass the parameter must observe the same "everything is bar" behaviour as
    one that explicitly passes ``frozenset()``. This is what makes the fix
    backward-compatible — the placeholder never matched either, so every
    existing caller stays correct until the partner configures the real UUID.
    """
    payload = {
        "items": [
            _item(
                item_id="i-a",
                name="A",
                sku="a",
                category_id="any-cafe-uuid",
            ),
        ]
    }

    explicit_empty = parse_items_snapshot(payload, cafe_category_ids=frozenset())
    default = parse_items_snapshot(payload)

    assert explicit_empty.items[0].segment is default.items[0].segment
    assert default.items[0].segment is Segment.BAR


# --- AC 3: a multi-variant item carries one segment across all its variants --


def test_parse_items_snapshot_cafe_segment_carried_to_every_variant() -> None:
    """A multi-variant cafe item tags every variant cafe.

    Mirrors the slice-02 rule the placeholder test asserted (segment is a
    property of the item, inherited by each variant), but against the
    configurable cafe set.
    """
    snapshot = parse_items_snapshot(
        {
            "items": [
                {
                    "id": "i-latte",
                    "item_name": "Latte",
                    "category_id": "cafe-cat",
                    "variants": [
                        {"variant_id": "v-l", "option1_value": "Large", "sku": "latte-l", "default_price": 90},
                        {"variant_id": "v-s", "option1_value": "Small", "sku": "latte-s", "default_price": 80},
                    ],
                }
            ]
        },
        cafe_category_ids=frozenset({"cafe-cat"}),
    )

    by_id = {mi.item_id: mi for mi in snapshot.items}
    assert by_id["latte-l"].segment is Segment.CAFE
    assert by_id["latte-s"].segment is Segment.CAFE


# --- AC 4: the menu snapshot stores the configured segment -------------------


def test_menu_snapshot_stores_segment_resolved_from_configured_category() -> None:
    """A snapshot taken with configured cafe categories persists cafe-segment
    rows through the store, so ``current_menu()`` carries the right segment
    into the sold-as-is quick-create (which inherits it onto the sold SKU)
    and the ``/items`` view.

    The end-to-end shape the fix exists to repair: the wrong segment on a
    menu row flows downstream into a wrong segment on a sold SKU a partner
    never hand-tags. Configuring the category UUID upstream fixes both at
    once.
    """
    store = InMemoryLoyverseStore()
    snapshot = parse_items_snapshot(
        {
            "items": [
                _item(
                    item_id="i-latte",
                    name="Latte",
                    sku="latte",
                    category_id="cafe-cat",
                ),
                _item(
                    item_id="i-chang",
                    name="Chang",
                    sku="chang",
                    category_id="bar-cat",
                ),
            ]
        },
        cafe_category_ids=frozenset({"cafe-cat"}),
    )

    store.record_menu_snapshot(snapshot, at=datetime(2026, 7, 22, tzinfo=timezone.utc))

    menu = store.current_menu()
    assert menu["latte"].segment is Segment.CAFE
    assert menu["chang"].segment is Segment.BAR


# --- AC 5: the env var parses into the set the sync consumes ----------------


def test_parse_cafe_category_ids_splits_comma_separated() -> None:
    """``LOYVERSE_CAFE_CATEGORY_IDS`` is comma-separated UUIDs.

    The partner dumps two category UUIDs from Loyverse (e.g. a 'Hot Coffee'
    sub-category and a 'Cold Brew' sub-category) and pastes them
    comma-separated; the parser splits them into a set.
    """
    assert parse_cafe_category_ids("cafe-a,cafe-b") == frozenset({"cafe-a", "cafe-b"})


def test_parse_cafe_category_ids_trims_whitespace_and_skips_empties() -> None:
    """Whitespace and stray empties do not corrupt the set.

    A partner-pasted value often carries a trailing comma or stray spaces
    (``"cafe-a, cafe-b,"``); those must not produce empty-string members
    that would never match a real category and would silently mislead a
    reader scanning the set. Robust parsing here keeps the configuration
    forgiving to type.
    """
    assert parse_cafe_category_ids("  cafe-a , cafe-b ,  , ") == frozenset(
        {"cafe-a", "cafe-b"}
    )


def test_parse_cafe_category_ids_none_or_empty_is_empty_set() -> None:
    """``None`` or an all-blank value yields the empty default.

    The documented default (ADR-0009): an unset env var means "no cafe
    category configured" → every item is bar. This is the regression guard
    for the contract the ``DEFAULT_CAFE_CATEGORY_IDS`` constant also pins.
    """
    assert parse_cafe_category_ids(None) == frozenset()
    assert parse_cafe_category_ids("") == frozenset()
    assert parse_cafe_category_ids("   ") == frozenset()


def test_parse_cafe_category_ids_single_value() -> None:
    """A single UUID (the venue's actual shape: one cafe category) parses."""
    assert parse_cafe_category_ids("deadbeef-cafe-0000-0000-000000000001") == frozenset(
        {"deadbeef-cafe-0000-0000-000000000001"}
    )


def test_cafe_category_ids_from_env_reads_resolved_env() -> None:
    """``cafe_category_ids_from_env`` reads the named variable from a dict.

    The explicit ``env`` parameter lets a test inject a fake environment
    without mutating ``os.environ``; production callers pass ``None`` to read
    the live environment. Mirrors the rest of the package's env-reading style.
    """
    fake_env = {CAFE_CATEGORY_IDS_ENV: "cafe-a,cafe-b"}

    assert cafe_category_ids_from_env(env=fake_env) == frozenset(
        {"cafe-a", "cafe-b"}
    )


def test_cafe_category_ids_from_env_unset_is_empty_set() -> None:
    """An environment without the variable yields the empty default."""
    assert cafe_category_ids_from_env(env={}) == frozenset()


# --- AC 6: the sync orchestrator forwards the set to the parser -------------


def test_sync_orchestrator_tags_menu_segments_from_configured_categories() -> None:
    """``SyncOrchestrator`` forwards ``cafe_category_ids`` to the parser.

    The orchestrator is the seam that turns configured UUIDs into menu rows'
    segments. Without this forwarding, the env var would be read but never
    applied, and the bug would persist. This test pins the wiring end-to-end:
    a category configured on the orchestrator shows up as the cafe segment
    on the persisted menu row.
    """
    from tangerine.loyverse.http import LoyverseHttpClient
    from tangerine.loyverse.sync import SyncOrchestrator
    from tests.test_loyverse_sync_e2e import (
        StubHttp,
        _credentials,
        _envelope,
        _receipts_envelope,
    )

    stub = StubHttp(
        routes={
            "/v1.0/receipts": [_receipts_envelope([], cursor=None)],
            "/v1.0/items": [
                _envelope(
                    [
                        {
                            "id": "i-latte",
                            "item_name": "Latte",
                            "category_id": "cafe-cat",
                            "sku": "latte",
                            "variants": [
                                {
                                    "variant_id": "v",
                                    "option1_value": "Latte",
                                    "sku": "latte",
                                    "default_price": 80,
                                }
                            ],
                        },
                        {
                            "id": "i-chang",
                            "item_name": "Chang",
                            "category_id": "bar-cat",
                            "sku": "chang",
                            "variants": [
                                {
                                    "variant_id": "v",
                                    "option1_value": "Chang",
                                    "sku": "chang",
                                    "default_price": 120,
                                }
                            ],
                        },
                    ],
                    cursor=None,
                )
            ],
        }
    )
    client = LoyverseHttpClient(_credentials(), urlopen=stub)
    store = InMemoryLoyverseStore()
    SyncOrchestrator(
        client=client,
        store=store,
        cafe_category_ids=frozenset({"cafe-cat"}),
    ).sync_sales_and_menu()

    menu = store.current_menu()
    assert menu["latte"].segment is Segment.CAFE
    assert menu["chang"].segment is Segment.BAR


def test_sync_orchestrator_defaults_to_empty_cafe_set_all_bar() -> None:
    """Without ``cafe_category_ids`` the orchestrator tags every item bar.

    Regression guard for the empty-default contract at the orchestrator seam:
    a caller that doesn't pass the parameter must observe the same "everything
    is bar" behaviour as one that explicitly passes ``frozenset()``. This is
    what makes a fresh deployment start correct until the partner configures
    the real cafe UUID.
    """
    from tangerine.loyverse.http import LoyverseHttpClient
    from tangerine.loyverse.sync import SyncOrchestrator
    from tests.test_loyverse_sync_e2e import (
        StubHttp,
        _credentials,
        _envelope,
        _receipts_envelope,
    )

    stub = StubHttp(
        routes={
            "/v1.0/receipts": [_receipts_envelope([], cursor=None)],
            "/v1.0/items": [
                _envelope(
                    [
                        {
                            "id": "i-a",
                            "item_name": "A",
                            "category_id": "any-cafe-cat",
                            "sku": "a",
                            "variants": [
                                {
                                    "variant_id": "v",
                                    "option1_value": "A",
                                    "sku": "a",
                                    "default_price": 80,
                                }
                            ],
                        }
                    ],
                    cursor=None,
                )
            ],
        }
    )
    client = LoyverseHttpClient(_credentials(), urlopen=stub)
    store = InMemoryLoyverseStore()
    # No cafe_category_ids — the default takes effect.
    SyncOrchestrator(client=client, store=store).sync_sales_and_menu()

    assert store.current_menu()["a"].segment is Segment.BAR
