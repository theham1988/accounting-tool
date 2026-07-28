"""E2E: spend-bucket reference surface (issue #95, parent #82).

The spend-bucket vocabulary is the controlled table cash-spend rows (slice
#96) FK into. This slice ships the table, the seed-on-empty of the HTML's
six buckets (``taps, kitchen, coffee, bakery, staff, rent``), the typed
store CRUD, and the partner-facing admin surface at ``/admin/spend-buckets``
— create / retire / delete, behind the existing auth, every write audited.

One E2E seam, mirroring ``test_period_review_e2e.py`` and the fixed-costs
admin tests: through ``seed_config`` + ``SqliteConfigStore`` + FastAPI's
``TestClient`` over the real SQLite store, against the public interfaces.
No reaching into internals.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.web.app import create_app

_TEST_PASSPHRASE = "spend-buckets-test-passphrase"
_TEST_SIGNING_SECRET = "spend-buckets-test-signing-secret"

#: The six buckets the HTML cost-breakdown column shows, in display order.
#: The seed must land them in this order so the admin page renders them
#: top-to-bottom as the partner recognises them from the printed map.
SEEDED_BUCKETS = ("taps", "kitchen", "coffee", "bakery", "staff", "rent")


def _empty_recipes_yaml() -> str:
    return "recipes: []\n"


def _costs_yaml() -> str:
    return "costs: {}\n"


def _assignees_yaml() -> str:
    return """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


def _write_seed_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_empty_recipes_yaml(), encoding="utf-8")
    costs.write_text(_costs_yaml(), encoding="utf-8")
    assignees.write_text(_assignees_yaml(), encoding="utf-8")
    return recipes, costs, assignees


def _seeded_store(tmp_path: Path) -> SqliteConfigStore:
    """An in-memory config store with the recipes/costs seed applied.

    The bucket seed runs inside ``seed_config`` (seed-on-empty), so a fresh
    DB leaves the store holding the six seeded buckets.
    """
    recipes, costs, _ = _write_seed_files(tmp_path)
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)
    return SqliteConfigStore(conn)


# --- AC: migration + seed-on-empty ------------------------------------------


def test_seed_lands_the_html_six_on_first_boot_against_an_empty_db(
    tmp_path: Path,
) -> None:
    """A fresh DB seeded through ``seed_config`` holds the HTML's six buckets.

    The buckets land in the HTML cost-breakdown's display order
    (taps/kitchen/coffee/bakery/staff/rent), none retired, so a partner
    opening the admin page on a brand-new deployment recognises the
    columns they already see on the printed map.
    """
    store = _seeded_store(tmp_path)

    buckets = store.spend_buckets()
    assert [b.bucket_id for b in buckets] == list(SEEDED_BUCKETS)
    for bucket in buckets:
        assert bucket.name  # display name is non-empty
        assert not bucket.retired_at  # seeded buckets are live


def test_seed_is_a_noop_when_the_table_already_has_rows(tmp_path: Path) -> None:
    """Seed-on-empty never clobbers a partner's edits or additions.

    A partner who has renamed a seeded bucket ('taps' -> 'Taps') and the
    table now holds their edited row must not have their work overwritten
    by re-running the seeder on the next boot. Re-running ``seed_config``
    is a no-op for the bucket table once it is non-empty.
    """
    store = _seeded_store(tmp_path)
    # Simulate a partner renaming a seeded bucket by editing the row in place.
    store._conn.execute(
        "UPDATE spend_buckets SET name = 'Taps' WHERE bucket_id = 'taps'"
    )

    # Re-run the seeder against the same connection — the partner's edits survive.
    seed_config(
        store._conn,
        recipes_path=tmp_path / "recipes.yaml",
        costs_path=tmp_path / "costs.yaml",
    )

    buckets = {b.bucket_id: b for b in store.spend_buckets()}
    assert buckets["taps"].name == "Taps"  # rename kept
    # Still exactly the six seeded buckets, no duplicates from a re-seed.
    assert set(buckets) == set(SEEDED_BUCKETS)


def test_seed_runs_against_an_existing_production_db_with_skus_already_present(
    tmp_path: Path,
) -> None:
    """An upgrading production DB (skus already populated) still gets the seed.

    The bucket seed gates on its own table being empty, not on the skus
    table: a partner whose deployment predates #95 has a populated skus
    table but an empty (or non-existent) spend_buckets table. Their next
    boot must land the six buckets alongside their existing config.
    """
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    recipes.write_text(
        """
recipes:
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans, quantity: "20" }
""",
        encoding="utf-8",
    )
    costs.write_text(
        "costs:\n  beans: { price: '2', updated_at: '2026-06-01' }\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)

    # The skus table has rows (one SKU seeded); the bucket seed still lands.
    skus_count = conn.execute("SELECT COUNT(*) FROM skus").fetchone()[0]
    assert skus_count > 0
    store = SqliteConfigStore(conn)
    assert {b.bucket_id for b in store.spend_buckets()} == set(SEEDED_BUCKETS)


def test_the_migration_is_idempotent(tmp_path: Path) -> None:
    """Running the migration runner twice against the same DB is a no-op."""
    conn = sqlite3.connect(":memory:")
    from tangerine.storage.schema import apply_migrations

    apply_migrations(conn)
    apply_migrations(conn)  # second apply must not raise

    # Table exists and is queryable.
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(spend_buckets)").fetchall()
    }
    assert {"bucket_id", "name", "retired_at"} <= cols


# --- AC: store CRUD (create), audit-logged ----------------------------------


def test_create_spend_bucket_writes_a_row_and_an_audit_entry(
    tmp_path: Path,
) -> None:
    """A partner-added bucket lands in the table and in the audit log.

    The store is the seam: ``create_spend_bucket`` writes one row, and one
    audit entry recording the creation (``old=None``, ``new`` = whole row).
    Reverts of that entry will restore the pre-creation state (the row
    disappears), exactly like a created SKU or fixed cost.
    """
    store = _seeded_store(tmp_path)
    store.create_spend_bucket(
        bucket_id="cleaning",
        name="Cleaning supplies",
        created_by="daniel",
    )

    by_id = {b.bucket_id: b for b in store.spend_buckets()}
    assert "cleaning" in by_id
    assert by_id["cleaning"].name == "Cleaning supplies"
    assert by_id["cleaning"].retired_at is None  # live

    (audit,) = [
        e for e in store.audit_entries() if e.table_name == "spend_buckets"
    ]
    assert audit.pk == "cleaning"
    assert audit.old_value is None  # creation
    assert audit.new_value["bucket_id"] == "cleaning"
    assert audit.new_value["name"] == "Cleaning supplies"
    assert audit.changed_by == "daniel"


def test_create_spend_bucket_round_trips_through_revert(tmp_path: Path) -> None:
    """Reverting a create-bucket entry deletes the row (the audit safety net).

    Slice 5 of the issue: revert round-trip. A created bucket's audit entry
    has no ``old_value``, so reverting it removes the row entirely — the
    mirror image of how reverting a created SKU or fixed cost works.
    """
    store = _seeded_store(tmp_path)
    store.create_spend_bucket(
        bucket_id="cleaning",
        name="Cleaning supplies",
        created_by="daniel",
    )
    create_entry = next(
        e for e in store.audit_entries() if e.table_name == "spend_buckets"
    )

    store.revert_entry(create_entry.entry_id, changed_by="daniel")

    assert "cleaning" not in {b.bucket_id for b in store.spend_buckets()}


# --- AC: admin surface (create), behind auth ---------------------------------


def _build_app(tmp_path: Path, *, today: date):  # type: ignore[no-untyped-def]
    """App factory over a seeded SQLite DB (the Wave 1 UI-seam pattern)."""
    recipes, costs, assignees = _write_seed_files(tmp_path)
    from tangerine.storage.sqlite_store import SqliteLoyverseStore

    db_path = str(tmp_path / "tangerine.db")
    store = SqliteLoyverseStore.connect(db_path)
    store.close()
    return create_app(
        db_path=db_path,
        recipes_path=str(recipes),
        costs_path=str(costs),
        assignees_path=str(assignees),
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )


def _authed_client(app):  # type: ignore[no-untyped-def]
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
def today() -> date:
    return date(2026, 7, 16)


def test_admin_page_lists_the_seeded_six_and_requires_auth(
    tmp_path: Path, today: date
) -> None:
    """``/admin/spend-buckets`` shows the six seeded buckets behind auth.

    An unauthenticated request is redirected to ``/login`` (the auth
    middleware gates every non-public route). An authed request renders
    the page with all six seeded buckets visible — including their display
    names — so a partner opening a fresh deployment recognises the columns
    they already see on the printed map.
    """
    app = _build_app(tmp_path, today=today)
    unauthed = TestClient(app)
    unauthed_response = unauthed.get("/admin/spend-buckets", follow_redirects=False)
    assert unauthed_response.status_code == 302  # → /login
    assert unauthed_response.headers["location"] == "/login"

    client = _authed_client(app)
    html = client.get("/admin/spend-buckets").text
    for bucket_id in SEEDED_BUCKETS:
        assert bucket_id in html


def test_admin_page_is_linked_from_admin_index(tmp_path: Path, today: date) -> None:
    """The Admin index links to the new spend-buckets surface."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    html = client.get("/admin").text

    assert 'href="/admin/spend-buckets"' in html


def test_create_form_adds_a_bucket_and_audit_logs_it(
    tmp_path: Path, today: date
) -> None:
    """Posting the add form lands a new bucket and an audit entry.

    A partner types ``cleaning`` / ``Cleaning supplies`` and presses SAVE.
    The page re-renders with the new bucket in the list, and ``/audit``
    shows the creation attributed to the signed-in partner.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post(
        "/admin/spend-buckets",
        data={"bucket_id": "cleaning", "name": "Cleaning supplies"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    listing = response.text.split("<!--section:spend-bucket-list-->")[1].split(
        "<!--/section:spend-bucket-list-->"
    )[0]
    assert "cleaning" in listing
    assert "Cleaning supplies" in listing

    audit = client.get("/audit").text
    assert "spend_buckets" in audit
    assert "cleaning" in audit
    assert "daniel" in audit


def test_create_form_rejects_an_empty_id_or_name_with_no_save(
    tmp_path: Path, today: date
) -> None:
    """A missing id or name re-renders with an inline error and writes nothing.

    Mirrors the fixed-costs form's "nothing was saved" rule: the partner
    sees what went wrong, the submitted values are echoed back, and no
    audit row is written for the failed submit.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post(
        "/admin/spend-buckets",
        data={"bucket_id": "", "name": ""},
        follow_redirects=False,
    )

    assert response.status_code == 200  # re-render, not a redirect
    assert "nothing was saved" in response.text.lower()
    # No audit row was written for the failed submit.
    audit = client.get("/audit").text
    assert "spend_buckets" not in audit


def test_create_form_rejects_a_duplicate_id(tmp_path: Path, today: date) -> None:
    """A bucket_id that already exists re-renders with a clear error.

    The controlled vocabulary's whole point is one canonical id per bucket;
    a duplicate would either silently overwrite (clobbering history) or
    raise a raw IntegrityError. The route catches it and explains.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post(
        "/admin/spend-buckets",
        data={"bucket_id": "taps", "name": "Already exists"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "already exists" in response.text.lower()
    audit = client.get("/audit").text
    assert "spend_buckets" not in audit  # nothing written


# --- AC: retire (soft), mirrors fixed-costs ending ---------------------------


def test_retire_soft_flags_a_seeded_bucket_and_audit_logs_it(
    tmp_path: Path, today: date
) -> None:
    """Retiring a seeded bucket flags it retired; the row stays for history.

    Mirrors fixed-costs ending-vs-deleting: a retired bucket stays in the
    table so historical cash-spend rows keep aggregating under it, but is
    excluded from #96's new-entry picker. On the admin page the retired
    row renders struck-through with a RETIRED <date> label, and the action
    lands in the audit log.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post(
        "/admin/spend-buckets/taps/retire", follow_redirects=True
    )

    assert response.status_code == 200
    listing = response.text.split("<!--section:spend-bucket-list-->")[1].split(
        "<!--/section:spend-bucket-list-->"
    )[0]
    # The retired row is still present (history preserved) and flagged.
    assert "taps" in listing
    assert "RETIRED" in listing
    assert today.isoformat() in listing
    # The struck-through styling marker is on the row.
    assert "spend-bucket-list__row--retired" in listing

    audit = client.get("/audit").text
    assert "spend_buckets" in audit
    assert "taps" in audit


def test_retire_route_returns_404_for_an_unknown_bucket(
    tmp_path: Path, today: date
) -> None:
    """Retiring an unknown bucket id is a 404, not a silent no-op."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post("/admin/spend-buckets/nope/retire")

    assert response.status_code == 404


# --- AC: delete (hard), guarded against referenced rows ----------------------


def test_hard_delete_an_empty_typo_bucket_removes_it(
    tmp_path: Path, today: date
) -> None:
    """A typo bucket with nothing referencing it can be hard-deleted.

    The partner adds 'cleanning' (typo), realises, and deletes it. DEL on
    an empty bucket removes the row; the audit log records the deletion
    with the before-snapshot, so revert restores it.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    client.post(
        "/admin/spend-buckets",
        data={"bucket_id": "cleanning", "name": "Cleaning (typo)"},
        follow_redirects=True,
    )
    assert "cleanning" in client.get("/admin/spend-buckets").text

    response = client.post(
        "/admin/spend-buckets/cleanning/delete", follow_redirects=True
    )

    assert response.status_code == 200
    listing = response.text.split("<!--section:spend-bucket-list-->")[1].split(
        "<!--/section:spend-bucket-list-->"
    )[0]
    assert "cleanning" not in listing
    # The audit log carries the deletion (before-snapshot recorded).
    audit = client.get("/audit").text
    assert "cleanning" in audit


def test_hard_delete_a_bucket_with_referencing_rows_is_refused(
    tmp_path: Path, today: date
) -> None:
    """Deleting a bucket that cash-spend rows reference is refused with 409.

    The FK constraint itself lands with #96; this route-level guard ships
    now so the surface is honest from day one. Until #96 lands the
    ``cash_spend`` table doesn't exist, so the in-use check answers False
    and delete proceeds — but the guard is exercised here by creating the
    table manually and inserting a referencing row, simulating #96's world.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    # Simulate #96's cash_spend table existing with a row pointing at 'taps'.
    cfg = app.state.config_store
    with cfg._lock:
        cfg._conn.execute(
            "CREATE TABLE cash_spend ("
            " id INTEGER PRIMARY KEY, bucket_id TEXT NOT NULL, amount TEXT NOT NULL"
            ")"
        )
        cfg._conn.execute(
            "INSERT INTO cash_spend (id, bucket_id, amount) VALUES (1, 'taps', '500')"
        )
        cfg._conn.commit()

    response = client.post("/admin/spend-buckets/taps/delete")

    assert response.status_code == 409
    assert "retire it instead" in response.text.lower()
    # The bucket is still there.
    assert "taps" in client.get("/admin/spend-buckets").text


def test_hard_delete_route_returns_404_for_an_unknown_bucket(
    tmp_path: Path, today: date
) -> None:
    """Deleting an unknown bucket id is a 404."""
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)

    response = client.post("/admin/spend-buckets/nope/delete")

    assert response.status_code == 404


# --- AC: audit revert round-trip via /audit ----------------------------------


def test_revert_a_bucket_create_via_the_audit_page(tmp_path: Path, today: date) -> None:
    """Pressing REVERT on a create-bucket audit entry undoes the creation.

    The audit safety net (ADR-0003 decision 2): every config edit is
    revertable through the existing /audit machinery, with no new revert
    code. A created bucket's entry has no old_value, so reverting it
    removes the row entirely.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    client.post(
        "/admin/spend-buckets",
        data={"bucket_id": "cleaning", "name": "Cleaning supplies"},
        follow_redirects=True,
    )
    # The create entry is the newest spend_buckets row on /audit.
    audit_html = client.get("/audit").text
    assert "cleaning" in audit_html

    # Find the create entry's id and revert it via the audit route.
    cfg = app.state.config_store
    create_entry = next(
        e for e in cfg.audit_entries()
        if e.table_name == "spend_buckets" and e.pk == "cleaning"
    )
    revert_response = client.post(
        f"/audit/{create_entry.entry_id}/revert",
        data={"reason": "wrong bucket"},
        follow_redirects=True,
    )
    assert revert_response.status_code == 200

    # The bucket is gone; the revert itself is logged.
    listing = client.get("/admin/spend-buckets").text.split(
        "<!--section:spend-bucket-list-->"
    )[1].split("<!--/section:spend-bucket-list-->")[0]
    assert "cleaning" not in listing
    audit_after = client.get("/audit").text
    assert "wrong bucket" in audit_after  # the revert's reason landed


def test_revert_a_bucket_delete_restores_the_row(
    tmp_path: Path, today: date
) -> None:
    """Reverting a delete-bucket entry restores the row whole.

    A deletion's audit entry carries the before-snapshot; reverting it
    re-inserts the row exactly as it was. This is the safety net for
    "I deleted the wrong bucket".
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    client.post(
        "/admin/spend-buckets",
        data={"bucket_id": "cleaning", "name": "Cleaning supplies"},
        follow_redirects=True,
    )
    client.post(
        "/admin/spend-buckets/cleaning/delete", follow_redirects=True
    )
    listing_after_delete = client.get("/admin/spend-buckets").text.split(
        "<!--section:spend-bucket-list-->"
    )[1].split("<!--/section:spend-bucket-list-->")[0]
    assert "cleaning" not in listing_after_delete

    # Find the delete entry (the newest spend_buckets entry) and revert it.
    cfg = app.state.config_store
    delete_entry = next(
        e for e in cfg.audit_entries()
        if e.table_name == "spend_buckets"
        and e.pk == "cleaning"
        and e.new_value is None
    )
    client.post(
        f"/audit/{delete_entry.entry_id}/revert", follow_redirects=True
    )

    # The row is back, exactly as it was.
    buckets = {b.bucket_id: b for b in cfg.spend_buckets()}
    assert "cleaning" in buckets
    assert buckets["cleaning"].name == "Cleaning supplies"
    assert buckets["cleaning"].retired_at is None


def test_revert_a_retire_un_flags_the_bucket(tmp_path: Path, today: date) -> None:
    """Reverting a retire entry clears the retired flag (surgical revert).

    The surgical-revert rule: only the fields the entry changed go back.
    A retire entry changed only ``retired_at`` (NULL → timestamp), so
    reverting it sets ``retired_at`` back to NULL — the bucket becomes
    live again, exactly as if the retire never happened.
    """
    app = _build_app(tmp_path, today=today)
    client = _authed_client(app)
    client.post(
        "/admin/spend-buckets/taps/retire", follow_redirects=True
    )
    cfg = app.state.config_store
    taps_after_retire = {b.bucket_id: b for b in cfg.spend_buckets()}["taps"]
    assert taps_after_retire.retired_at is not None

    # Find the retire entry and revert it.
    retire_entry = next(
        e for e in cfg.audit_entries()
        if e.table_name == "spend_buckets" and e.pk == "taps"
        and e.new_value is not None
        and e.new_value.get("retired_at") is not None
    )
    client.post(
        f"/audit/{retire_entry.entry_id}/revert", follow_redirects=True
    )

    taps_after_revert = {b.bucket_id: b for b in cfg.spend_buckets()}["taps"]
    assert taps_after_revert.retired_at is None  # back to live
