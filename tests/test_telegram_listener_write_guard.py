"""T2.6 (REMEDIATION_PLAN Волна 2): статичний лінт-гейт проти прямого
write-SQL у telegram_listener.py.

Архітектурний інваріант: telegram_listener.py — ОКРЕМИЙ процес (Telethon),
і має писати у SQLite ЛИШЕ через HTTP-проксі на app.py (/api/telegram/ingest,
/api/telegram/chats/toggle) — не напряму. app.py лишається єдиним писарем,
менше шансів на конкурентні `database is locked` навіть із busy_timeout
(Волна 1, T2.3). Раніше цей інваріант порушувався: `cmd_toggle` робив
`conn.execute("INSERT INTO tg_monitored_chats ...")` напряму (T2.6 це
виправляє — див. telegram_listener.py::cmd_toggle, тепер POST на app.py).

Цей тест — не unit-тест конкретної функції, а СТАТИЧНИЙ ГЕЙТ: парсить AST
telegram_listener.py і падає, якщо десь з'явиться НОВИЙ `.execute(...)`
з SQL-рядком, що починається на INSERT/UPDATE/DELETE. Читання (SELECT) і
PRAGMA лишаються дозволеними — вони не пишуть у БД.

Якщо тест впав після твоєї правки:
  1. Найімовірніше — переведи запис на HTTP-проксі до app.py (як зроблено
     для ingest і chats/toggle), а не пиши в БД напряму з цього процесу.
  2. Якщо прямий запис дійсно виправданий (напр. документована легітимна
     друга «писар»-роль) — онови ALLOWLIST нижче з коментарем-обґрунтуванням
     і зафіксуй рішення в docs/REMEDIATION_PLAN.md + memory/ проєкту (Варіант
     Б з T2.6 explicitly documents dual-writer + retry/busy_timeout).
"""
from __future__ import annotations

import ast
from pathlib import Path

LISTENER_PATH = Path(__file__).resolve().parent.parent / "telegram_listener.py"

# Порожньо навмисно: telegram_listener.py наразі НЕ повинен мати жодного
# прямого write-SQL. Кожен запис (chat_id, chat, tg_monitored_chats) іде
# через HTTP на app.py. Якщо колись знадобиться легітимний виняток — додай
# сюди номер рядка з коментарем чому, а не просто розширюй мовчки.
ALLOWLIST_LINES: set[int] = set()

_WRITE_KEYWORDS = ("insert", "update", "delete", "replace")


def _sql_literal(node: ast.AST) -> str | None:
    """Витягти константний SQL-рядок з першого аргумента .execute(...), якщо
    можливо (Constant-рядок або f-string зі статичним префіксом)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string: беремо лише константні шматки — достатньо щоб побачити
        # ключове слово SQL на початку (воно завжди статичне в цьому файлі).
        parts = [v.value for v in node.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts) if parts else None
    return None


def _find_direct_writes(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source, filename=str(LISTENER_PATH))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        is_execute_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("execute", "executemany", "executescript")
        )
        if not is_execute_call or not node.args:
            continue
        sql = _sql_literal(node.args[0])
        if not sql:
            continue
        normalized = sql.strip().lstrip("(").strip().lower()
        if normalized.startswith(_WRITE_KEYWORDS) and node.lineno not in ALLOWLIST_LINES:
            hits.append((node.lineno, sql.strip().splitlines()[0][:100]))
    return hits


def test_telegram_listener_has_no_direct_write_sql():
    source = LISTENER_PATH.read_text(encoding="utf-8")
    hits = _find_direct_writes(source)
    assert not hits, (
        "telegram_listener.py містить прямий write-SQL (.execute на "
        "INSERT/UPDATE/DELETE/REPLACE) поза allowlist — порушує інваріант "
        "«лише app.py пише в БД» (T2.6, REMEDIATION_PLAN Волна 2). "
        "Перевести запис на HTTP-проксі до app.py (app/blueprints/telegram.py) "
        "або свідомо додати рядок у ALLOWLIST_LINES з поясненням.\n"
        + "\n".join(f"  line {ln}: {snippet}" for ln, snippet in hits)
    )


def test_guard_detects_direct_write_when_present():
    """Sanity-check самого гейта: переконатись що детектор дійсно ловить
    write-SQL, а не завжди повертає порожній список (інакше тест вище —
    пастка з фальшивим GREEN)."""
    fake_source = (
        "import sqlite3\n"
        "def f(conn):\n"
        "    conn.execute(\"INSERT INTO tg_monitored_chats (chat_id) VALUES (?)\", (1,))\n"
    )
    tree_hits = _find_direct_writes(fake_source)
    assert len(tree_hits) == 1
    assert "INSERT INTO tg_monitored_chats" in tree_hits[0][1]
