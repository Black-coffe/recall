"""Карточка docs/defects/brief-link-localhost.md: посилання брифінгу мають відкриватись з телефона.

Використання: check_brief_links.py <pipes_cards.py> — вантажить файл як модуль карток, ставить
``RECALL_PUBLIC_URL`` (адреса, видима з телефона) і ``RECALL_API_URL=http://127.0.0.1:5050``,
рендерить брифінг з усіма родинами посилань (задача, «ще N →», забута обіцянка, питання, тема,
виправлення) і падає (exit 1), якщо хоч одне посилання на Recall веде не на публічну адресу.

База береться так, як її бере труба: ``link_base()`` модуля; модуль без нього — як труба до K5-03,
тобто ``RECALL_API_URL``. Посилання на Telegram (``https://t.me/``) — не Recall, не перевіряються.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PUBLIC = "https://home-pc.example.ts.net"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"cards_under_check_{abs(hash(path))}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _db(tmp: str) -> str:
    from app.db.migrations import init_database
    path = os.path.join(tmp, "links.db")
    init_database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) "
                 "VALUES (901, 'Перевірка', 'перевірка', NULL)")
    for i in range(1, 9):
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (?, 'recording', ?, 901)", (i, f"Зустріч {i}"))
        conn.execute("INSERT INTO action_items (id, transcription_id, task) VALUES (?, ?, ?)",
                     (100 + i, i, f"Задача {i}"))
    conn.commit()
    conn.close()
    return path


def _data() -> dict:
    tasks = [{"id": 100 + i, "task": f"Задача {i}", "transcription_id": i,
              "due_date": "2026-10-20"} for i in range(1, 8)]
    return {
        "weekly_digest": {
            "this_week": tasks, "overdue": [], "no_date": [],
            "dropped": [{"id": 108, "task": "Задача 8", "meeting_date": "2026-10-01"}],
            "stale_topics": [{"id": 5, "canonical_name": "Тема", "days_silent": 40}],
            "corrections": [{"target_type": "transcription", "target_id": 2,
                             "target_name": "Зустріч 2", "body": "насправді інакше"}],
        },
        "open_questions": {"questions": [
            {"transcription_id": 3, "question": "Коли?", "silence_h": 30,
             "link": "https://t.me/c/1/2"}]},
    }


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else ROOT / "app" / "services" / "pipes_cards.py"
    os.environ["RECALL_PUBLIC_URL"] = PUBLIC
    os.environ["RECALL_API_URL"] = "http://127.0.0.1:5050"
    import logging
    logging.disable(logging.CRITICAL)
    mod = _load(target)
    base = mod.link_base() if hasattr(mod, "link_base") else os.environ["RECALL_API_URL"]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        kw = {"db_path": _db(tmp), "today": date(2026, 10, 19), "words": []}
        try:
            text, _ = mod.render("Звід", _data(), **kw)
        except TypeError:                      # до K5-03 base_url був обовʼязковим
            text, _ = mod.render("Звід", _data(), base_url=base, **kw)
    hrefs = re.findall(r'href="([^"]+)"', text)
    bad = [h for h in hrefs if not h.startswith("https://t.me/") and not h.startswith(PUBLIC + "/")]
    if not hrefs:
        print(f"{target}: у брифінгу нема жодного посилання")
        return 1
    for h in bad:
        print(f"{target}: посилання не відкриється з телефона — {h}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
