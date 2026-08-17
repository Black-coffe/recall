"""SQLite connection helper. Зберігає WAL-режим, foreign_keys, row_factory.

Використання:
    with get_db_connection(db_path) as conn:
        rows = conn.execute('...').fetchall()
"""
import logging
import sqlite3
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# T2.3: явний busy_timeout замість дефолту Python sqlite3 (5с). Три процеси
# (app.py, telegram_listener.py, mcp_server.py) + ThreadPoolExecutor + SSE +
# live-transcribe/copilot воркери пишуть в одну WAL-БД; довга транзакція
# (bulk backfill enrichment, масовий INSERT chunks) може тримати блокування
# >5с. 30с покриває реальні довгі транзакції без нескінченного очікування
# (більше — маскувало б дедлоки). Тримай значення тут як єдину точку правди.
BUSY_TIMEOUT_MS = 30000


@contextmanager
def get_db_connection(db_path: str):
    """Context manager з автоматичним закриттям з'єднання.

    Args:
        db_path: шлях до SQLite файлу (зазвичай app.config['DATABASE']).

    Налаштування:
    - row_factory = sqlite3.Row → доступ до колонок за іменем (row['id']).
    - foreign_keys = ON.
    - journal_mode = WAL → краща конкурентність на read.
    - busy_timeout = BUSY_TIMEOUT_MS → чекати блокування, а не миттєвий
      `database is locked` (див. T2.3).
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
        logger.error(f"Помилка підключення до БД: {e}")
        raise
    finally:
        if conn is not None:
            conn.close()
