"""Persistence layer for Wave 1.

A SQLite-backed ``LoyverseStore`` implementation
(:class:`~tangerine.storage.sqlite_store.SqliteLoyverseStore`) plus a minimal
forward-only SQL migration runner that creates the schema on first run.
"""
