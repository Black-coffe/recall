"""Repository layer (T7.2): centralized SQL queries per table.

Origin: raw `conn.execute()` calls were duplicated directly in HTTP handlers
(app/blueprints/*) and services (app/services/*), often with a different set of
columns for the same "read a row by id". A schema change required a grep across
the whole project. This package is an incremental replacement: per-table functions
that accept an already open `sqlite3.Connection` (the same one that
`app.db.connection.get_db_connection()` provides) and return `sqlite3.Row`/a list.

NOT to be confused with the removed (T7.3) `app/services/database.py` (`DatabaseService`) —
that was dead code (no imports), this package is new, wired in
on purpose. Queries are moved incrementally, the most duplicated first;
the remaining increments are item P20 in docs/ROADMAP.md.
"""
