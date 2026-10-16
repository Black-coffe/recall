"""SQLite connection helper. Keeps WAL mode, foreign_keys, row_factory.

Usage:
    with get_db_connection(db_path) as conn:
        rows = conn.execute('...').fetchall()
"""
import logging
import sqlite3
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# T2.3: explicit busy_timeout instead of the Python sqlite3 default (5s). Three processes
# (app.py, telegram_listener.py, mcp_server.py) + ThreadPoolExecutor + SSE +
# live-transcribe/copilot workers write to one WAL DB; a long transaction
# (bulk backfill enrichment, mass INSERT of chunks) can hold the lock
# >5s. 30s covers real long transactions without waiting forever
# (more would mask deadlocks). Keep the value here as the single source of truth.
BUSY_TIMEOUT_MS = 30000


@contextmanager
def get_db_connection(db_path: str):
    """Context manager with automatic connection closing.

    Args:
        db_path: path to the SQLite file (usually app.config['DATABASE']).

    Settings:
    - row_factory = sqlite3.Row → column access by name (row['id']).
    - foreign_keys = ON.
    - journal_mode = WAL → better read concurrency.
    - busy_timeout = BUSY_TIMEOUT_MS → wait for the lock instead of an immediate
      `database is locked` (see T2.3).
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        yield conn
    except sqlite3.Error as e:
        logger.error(f"Database connection error: {e}")
        raise
    finally:
        if conn is not None:
            conn.close()
