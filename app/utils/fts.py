"""SQLite FTS5 helpers (Phase 4.2).

Чисті функції без залежностей від Flask/БД.
"""
import re


def sanitize_fts_query(query: str) -> str:
    """Перетворює вільний текст у безпечний FTS5 MATCH-вираз.

    Кожне слово огортаємо у лапки (FTS5 phrase syntax) — це:
    - екранує всі спецсимволи FTS5 (* " : тощо);
    - перетворює запит на AND по словах (звична поведінка для пошуку).

    Повертає '' якщо немає валідних слів — caller повинен пропустити FTS-пошук.
    """
    if not query:
        return ''
    words = re.findall(r'\w+', query, re.UNICODE)
    if not words:
        return ''
    return ' '.join(f'"{w}"' for w in words)
