"""E2E: suppliers reference surface (Wave 2, issue #94).

Parent issue #82 ("model cash-basis supplier spend") is split into three
vertical slices; this one ships the controlled vendor list the cash-spend
entry surface (the third slice) will FK into. Vendors are a controlled
list, not free-form, because free-form lets "Makro" / "Makro Phuket" /
"makro" drift and break per-vendor aggregation (decision 2a of #82).

The surface is one seam: the public ``SqliteConfigStore`` methods plus the
``/admin/suppliers`` route via the test client. Every write goes through
the existing ``audit_log`` machinery with ``table_name='suppliers'`` — no
new audit path — so ``/audit`` shows the change and the existing per-entry
Revert restores it without any new revert code (ADR-0003, the fixed-costs
precedent). Mirrors ``test_period_review_e2e.py`` and the fixed-costs
admin tests for shape.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.storage.schema import apply_migrations
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Supplier
from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

_TEST_PASSPHRASE = "suppliers-ui-test-passphrase"
_TEST_SIGNING_SECRET = "suppliers-ui-test-signing-secret"


def _recipes_yaml() -> str:
    return """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
"""


def _costs_yaml() -> str:
    return """
costs:
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
"""


def _assignees_yaml() -> str:
    return """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


def _store(tmp_path: Path) -> SqliteConfigStore:
    """An empty-supplier, freshly-seeded config store (the migration ran)."""
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs = tmp_path / "costs.yaml"
    costs.write_text(_costs_yaml(), encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)
    return SqliteConfigStore(conn, now=lambda: "2026-07-15T02:00:00+00:00")


# =============================================================================
# AC: SqliteConfigStore exposes typed read/write methods for suppliers
# =============================================================================


def test_suppliers_starts_empty_and_can_list_after_a_create(
    tmp_path: Path,
) -> None:
    """The store exposes ``suppliers()`` returning the dormant ``Supplier`` type.

    A freshly migrated database has no suppliers (the table ships empty —
    no seed). After one ``create_supplier`` the list returns it as the
    in-place ``Supplier(supplier_id, name)`` type from ``types.py``
    (decision E of #82: reuse the dormant type, do not relocate it),
    ordered by ``supplier_id``.
    """
    store = _store(tmp_path)

    assert store.suppliers() == []

    store.create_supplier("makro-phuket", name="Makro Phuket", created_by="daniel")

    assert store.suppliers() == [Supplier("makro-phuket", "Makro Phuket")]


def test_get_supplier_returns_the_row_or_none(tmp_path: Path) -> None:
    """``get_supplier`` is the existence + lookup check the route needs."""
    store = _store(tmp_path)
    store.create_supplier("fat-dolphin", name="Fat Dolphin", created_by="daniel")

    assert store.get_supplier("fat-dolphin") == Supplier("fat-dolphin", "Fat Dolphin")
    assert store.get_supplier("nope") is None


def test_create_rejects_a_duplicate_id(tmp_path: Path) -> None:
    """``supplier_id`` is the PK the cash-spend rows in #96 will FK to.

    Re-creating one id is a partner typo (two "makro" rows). The store
    refuses — returning False — rather than silently clobbering the
    existing row's name. Nothing is written, nothing is audited.
    """
    store = _store(tmp_path)
    store.create_supplier("makro", name="Makro Phuket", created_by="daniel")

    again = store.create_supplier("makro", name="Makro", created_by="daniel")

    assert again is False
    # The original row is untouched.
    assert store.get_supplier("makro") == Supplier("makro", "Makro Phuket")


def test_update_renames_a_supplier_and_returns_false_for_unknown(
    tmp_path: Path,
) -> None:
    """Editing is a rename (typo fix); the id is immutable."""
    store = _store(tmp_path)
    store.create_supplier("makro", name="Makr Phuket", created_by="daniel")

    updated = store.update_supplier("makro", name="Makro Phuket", updated_by="noi")
    missing = store.update_supplier("ghost", name="Ghost", updated_by="noi")

    assert updated is True
    assert missing is False
    assert store.get_supplier("makro") == Supplier("makro", "Makro Phuket")


# =============================================================================
# AC: writes go through audit_log with table_name='suppliers'; Revert works
# =============================================================================


def test_each_write_is_audited_and_revert_restores_it(tmp_path: Path) -> None:
    """The ADR-0003 pattern, unchanged: every write appends an ``audit_log``
    row keyed on ``table_name='suppliers'``, and the existing per-entry
    Revert undoes the change without any new revert code.

    A create → revert deletes the row (the create's ``old_value`` is None);
    an edit → revert restores the prior name; a delete → revert restores
    the row. Three strokes, one existing revert path.
    """
    store = _store(tmp_path)
    store.create_supplier("makro", name="Makro", created_by="daniel")

    # Create's audit entry: old=None, new=the row.
    (create_entry,) = [
        e for e in store.audit_entries() if e.table_name == "suppliers"
    ]
    assert create_entry.old_value is None
    assert create_entry.new_value["supplier_id"] == "makro"
    assert create_entry.new_value["name"] == "Makro"

    # Reverting the create removes the row (creation's undo is a delete).
    assert store.revert_entry(create_entry.entry_id, changed_by="daniel") is True
    assert store.suppliers() == []

    # An edit writes a new audit row; reverting it restores the prior name.
    store.create_supplier("makro", name="Makro", created_by="daniel")
    store.update_supplier("makro", name="Makro Phuket", updated_by="noi")
    assert store.get_supplier("makro").name == "Makro Phuket"
    edit_entry = next(
        e
        for e in store.audit_entries()
        if e.table_name == "suppliers" and e.new_value["name"] == "Makro Phuket"
    )
    assert store.revert_entry(edit_entry.entry_id, changed_by="daniel") is True
    assert store.get_supplier("makro").name == "Makro"

    # A delete writes an audit row with new=None; reverting it brings the
    # row back whole.
    store.delete_supplier("makro", deleted_by="daniel")
    assert store.suppliers() == []
    delete_entry = next(
        e
        for e in store.audit_entries()
        if e.table_name == "suppliers" and e.new_value is None
    )
    assert store.revert_entry(delete_entry.entry_id, changed_by="daniel") is True
    assert store.get_supplier("makro") == Supplier("makro", "Makro")


# =============================================================================
# AC: referential-integrity guard — delete refused when a row references it
# =============================================================================


def test_supplier_in_use_is_false_on_a_fresh_database(tmp_path: Path) -> None:
    """``supplier_in_use`` is the forward-looking guard #96's FK will enforce.

    No cash-spend rows exist yet (that table lands with #96), so every
    supplier reports not-in-use. The guard ships now so the route can
    refuse a delete that would break referential integrity the moment the
    FK constraint is real.
    """
    store = _store(tmp_path)
    store.create_supplier("makro", name="Makro", created_by="daniel")

    assert store.supplier_in_use("makro") is False


def _seed_cash_spend_referencing(
    conn: sqlite3.Connection, supplier_id: str
) -> None:
    """Stand up a not-yet-FK'd cash-spend row referencing ``supplier_id``.

    Slice #96 will own the ``cash_spend`` table and its FK to
    ``suppliers.supplier_id``; until then, this test simulates that future
    state by creating the referencing row directly, so the route-level
    guard (which queries for any referencing rows before allowing the
    delete) has something to refuse against. The guard is forward-looking
    but correct — it refuses a delete that #96's FK would refuse too.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cash_spend ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  supplier_id TEXT NOT NULL,"
        "  amount TEXT NOT NULL,"
        "  spent_on TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO cash_spend (supplier_id, amount, spent_on)"
        " VALUES (?, ?, ?)",
        (supplier_id, "500", "2026-07-15"),
    )


def test_supplier_in_use_is_true_when_a_cash_spend_row_references_it(
    tmp_path: Path,
) -> None:
    """The guard sees the referencing row and reports in-use.

    ``delete_supplier`` then refuses with a clear message (the exception
    carries the supplier id and the count, so the route can surface it
    partner-readably).
    """
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs = tmp_path / "costs.yaml"
    costs.write_text(_costs_yaml(), encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)
    store = SqliteConfigStore(conn, now=lambda: "2026-07-15T02:00:00+00:00")
    store.create_supplier("makro", name="Makro", created_by="daniel")
    _seed_cash_spend_referencing(conn, "makro")

    assert store.supplier_in_use("makro") is True

    refused = store.delete_supplier("makro", deleted_by="daniel")
    assert refused is False
    # The row survives — the delete was refused.
    assert store.get_supplier("makro") == Supplier("makro", "Makro")


def test_delete_supplier_returns_false_for_unknown(tmp_path: Path) -> None:
    """Deleting an id that was never there is a no-op + False (not a 500)."""
    store = _store(tmp_path)
    assert store.delete_supplier("ghost", deleted_by="daniel") is False


# =============================================================================
# AC: migration is idempotent and runs cleanly against a production-shaped DB
# =============================================================================


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Running ``apply_migrations`` twice is a no-op (no double-apply).

    The migration runner records applied ids in ``schema_migrations``; a
    second call must skip every already-applied file. This is the safety
    property that lets the migration land on a production server that
    reboots nightly.
    """
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs = tmp_path / "costs.yaml"
    costs.write_text(_costs_yaml(), encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)

    # The suppliers table exists; applying migrations again must not raise
    # (e.g. UNIQUE constraint on schema_migrations.id, or CREATE TABLE
    # failing because the table already exists).
    apply_migrations(conn)  # must not raise

    # The schema_migrations row for 0009 is recorded exactly once.
    count = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE id = 9"
    ).fetchone()[0]
    assert count == 1


def test_migration_lands_cleanly_on_a_production_shaped_db(
    tmp_path: Path,
) -> None:
    """The suppliers migration adds its table without touching existing data.

    A production-shaped DB has recipes, costs, mappings, fixed costs, audit
    rows, and sales. Booting the new code must add the ``suppliers`` table
    (empty) and leave every prior table's rows intact — no data loss, the
    acceptance criterion "runs cleanly against the existing production
    database without data loss".
    """
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs = tmp_path / "costs.yaml"
    costs.write_text(_costs_yaml(), encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)

    store = SqliteConfigStore(conn, now=lambda: "2026-07-15T02:00:00+00:00")
    # Plant representative rows in every prior audited table, plus a sale.
    store.create_sku(
        "extra-sku", name="Extra", unit="g", created_by="daniel"
    )
    store.save_cost(
        "chang-keg",
        pack_price=Decimal("70"),
        pack_quantity=Decimal("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 10),
    )
    store.save_mapping("i-extra", "chang-draft-500", updated_by="daniel")
    store.create_fixed_cost(
        label="Rent",
        category="rent",
        amount=Decimal("50000"),
        kind="recurring",
        period=(2026, 7),
        created_by="daniel",
    )
    conn.execute(
        "INSERT INTO sales (receipt_number, line_id, item_id, timestamp,"
        " sell_price, quantity, segment)"
        " VALUES ('r-1', 'l-1', 'i-extra', '2026-07-10', '120', 1, 'bar')"
    )
    conn.commit()
    audit_before = store.audit_entries()
    costs_before = store.cost_rows()
    skus_before = store.skus()
    mappings_before = store.mappings()
    fixed_before = store.fixed_costs()
    sales_before = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]

    # Re-applying migrations is the production-reboot path. Must not raise,
    # must not drop data.
    apply_migrations(conn)

    # Suppliers table now exists and is empty; everything else is byte-for-byte
    # identical to before the reboot.
    assert store.suppliers() == []
    assert store.audit_entries() == audit_before
    assert store.cost_rows() == costs_before
    assert store.skus() == skus_before
    assert store.mappings() == mappings_before
    assert store.fixed_costs() == fixed_before
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == sales_before


# =============================================================================
# AC: /admin/suppliers renders the list; create / edit / delete behind auth
# =============================================================================


def _build_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs = tmp_path / "costs.yaml"
    costs.write_text(_costs_yaml(), encoding="utf-8")
    assignees = tmp_path / "assignees.yaml"
    assignees.write_text(_assignees_yaml(), encoding="utf-8")
    db_path = str(tmp_path / "tangerine.db")
    SqliteLoyverseStore.connect(db_path).close()
    return create_app(
        db_path=db_path,
        recipes_path=str(recipes),
        costs_path=str(costs),
        assignees_path=str(assignees),
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
        today=date(2026, 7, 15),
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


def _first_revert_entry_id(audit_html: str) -> str:
    """The entry id of the newest entry's Revert form on the audit page."""
    match = re.search(r'action="/audit/(\d+)/revert"', audit_html)
    assert match is not None, "no per-entry revert form on the audit page"
    return match.group(1)


def test_suppliers_page_redirects_to_login_when_unauthenticated(
    tmp_path: Path,
) -> None:
    """The Wave 1.5 admin-surface pattern: every /admin route is auth-gated."""
    app = _build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/admin/suppliers", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_add_edit_delete_round_trip_renders_and_persists(tmp_path: Path) -> None:
    """The partner flow: open the list, add a vendor, rename it, delete it.

    Each step re-renders the list with the current state, so the partner
    sees what the next surface will read. The rename and the delete each
    land in the audit log and revert from there.

    Asserts on the rendered supplier-list rows (the ``fixed-cost-list__label``
    span) rather than whole-page substring matches, so the page's intro
    example copy does not leak into the assertions.
    """
    client = _authed_client(_build_app(tmp_path))

    def _list_labels() -> set[str]:
        page = client.get("/admin/suppliers").text
        return set(re.findall(r'class="fixed-cost-list__label"[^>]*>([^<]+)<', page))

    # Empty list on first open (the empty-state copy, not a row).
    page = client.get("/admin/suppliers")
    assert page.status_code == 200
    assert "No suppliers" in page.text

    # Add Makro, with a typo.
    created = client.post(
        "/admin/suppliers",
        data={"supplier_id": "makro", "name": "Makr Phuket"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/admin/suppliers"
    assert _list_labels() == {"Makr Phuket"}

    # Rename — typo fix.
    renamed = client.post(
        "/admin/suppliers/makro/edit",
        data={"name": "Makro Phuket"},
        follow_redirects=False,
    )
    assert renamed.status_code == 303
    assert _list_labels() == {"Makro Phuket"}

    # Delete.
    deleted = client.post(
        "/admin/suppliers/makro/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert _list_labels() == set()


def test_create_validates_required_fields_and_echoes_them(tmp_path: Path) -> None:
    """A missing id or name re-renders with an inline error and the submitted
    values — the Wave 1.5 admin-surface pattern: never a bare 400, never a
    silent save.

    Asserts the form was re-rendered (200) with the error marker and the
    echoed name in the form field, and — crucially — that no supplier row
    was created (the list stays empty).
    """
    client = _authed_client(_build_app(tmp_path))

    page = client.post(
        "/admin/suppliers",
        data={"supplier_id": "", "name": "Makro"},
        follow_redirects=False,
    )

    assert page.status_code == 200
    assert "nothing was saved" in page.text.lower()
    # The submitted name is echoed back into the form field so the partner
    # does not retype it.
    assert 'value="Makro"' in page.text
    # And nothing landed: the rendered list is empty (the empty-state copy).
    assert "No suppliers" in client.get("/admin/suppliers").text


def test_delete_blocked_when_supplier_in_use_is_surfaced_partner_readably(
    tmp_path: Path,
) -> None:
    """The route-level referential-integrity guard.

    A supplier with a referencing cash-spend row cannot be deleted; the
    page surfaces a clear partner-readable message rather than a 500. The
    row survives. (#96's FK will enforce this at the DB layer; the guard
    ships now against the empty table and stands ready.)
    """
    client = _authed_client(_build_app(tmp_path))
    client.post(
        "/admin/suppliers",
        data={"supplier_id": "makro", "name": "Makro"},
        follow_redirects=False,
    )

    # Simulate #96 by inserting a referencing cash_spend row directly through
    # the app's public ``db_path`` state (the same file the running server
    # uses), then close that connection so the app's own store sees the row
    # on its next read.
    conn = sqlite3.connect(client.app.state.db_path)
    try:
        _seed_cash_spend_referencing(conn, "makro")
        conn.commit()
    finally:
        conn.close()

    page = client.post(
        "/admin/suppliers/makro/delete",
        follow_redirects=False,
    )

    # The route refuses with a re-rendered page (200) carrying the message.
    assert page.status_code == 200
    assert "in use" in page.text.lower() or "cannot delete" in page.text.lower()
    # The supplier still exists.
    assert "makro" in client.get("/admin/suppliers").text


# =============================================================================
# AC: /audit shows supplier changes and Revert restores them
# =============================================================================


def test_supplier_changes_show_in_audit_and_revert_restores(tmp_path: Path) -> None:
    """Create a supplier through the route, see it in /audit, revert it."""
    client = _authed_client(_build_app(tmp_path))
    client.post(
        "/admin/suppliers",
        data={"supplier_id": "fat-dolphin", "name": "Fat Dolphin"},
        follow_redirects=False,
    )

    audit_html = client.get("/audit").text
    assert "suppliers" in audit_html
    assert "fat-dolphin" in audit_html

    entry_id = _first_revert_entry_id(audit_html)
    revert = client.post(f"/audit/{entry_id}/revert", follow_redirects=False)
    assert revert.status_code == 303

    page = client.get("/admin/suppliers")
    assert "fat-dolphin" not in page.text
