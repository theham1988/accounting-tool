"""Minimal forward-only SQL migration runner.

Each migration is a ``.sql`` file under the ``migrations/`` subpackage named
``NNNN_name.sql``. The runner records applied ids in ``schema_migrations`` and
applies any unapplied file inside a single transaction. No Alembic — at this
scale the schema is one file and forward-only is sufficient (ADR-0001).
"""

from __future__ import annotations

import sqlite3
from importlib import resources


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply every not-yet-applied migration under ``migrations/``.

    Idempotent: the ``schema_migrations`` table tracks applied ids, so calling
    this on an already-current database is a no-op.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  id INTEGER PRIMARY KEY, applied_at TEXT NOT NULL"
        ")"
    )
    applied = {
        row[0]
        for row in conn.execute("SELECT id FROM schema_migrations").fetchall()
    }

    for name, migration_id in _ordered_migrations():
        if migration_id in applied:
            continue
        sql = _read_migration(name)
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, _now_iso(conn)),
        )
        conn.commit()


def _ordered_migrations() -> list[tuple[str, int]]:
    """``(file_name, numeric_id)`` for each migration, sorted by id."""
    files = [
        f
        for f in resources.files("tangerine.storage.migrations").iterdir()
        if f.name.endswith(".sql")
    ]
    parsed: list[tuple[str, int]] = []
    for f in files:
        stem = f.name[: -len(".sql")]
        prefix = stem.split("_", 1)[0]
        try:
            parsed.append((f.name, int(prefix)))
        except ValueError:
            continue
    parsed.sort(key=lambda kv: kv[1])
    return parsed


def _read_migration(name: str) -> str:
    return (
        resources.files("tangerine.storage.migrations")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def _now_iso(conn: sqlite3.Connection) -> str:
    """Current UTC timestamp as ISO-8601, sourced from the database."""
    row = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')").fetchone()
    return str(row[0])


__all__ = ["apply_migrations"]
