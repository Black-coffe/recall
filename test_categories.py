"""Офлайн-смок-тест напрямків (Phase 14.1): casefold-унікальність + керування.

Без моделей / Ollama / мережі. Підіймає МІНІМАЛЬНИЙ Flask лише з memory_bp на
тимчасовій БД (повна міграційна схема через init_database) і перевіряє:
  A. Міграція v20: колонка categories.name_norm + засіви мають name_norm.
  B. Створення: name_norm = casefold(name); кириличний дубль («Фонд» vs «фонд»)
     відбивається 409 (SQLite COLLATE NOCASE цього НЕ ловив — суть фікса).
  C. Перейменування: оновлює name_norm; колізія casefold → 409; виключає себе.
  D. Перенесення/обʼєднання (merge): записи src → dst; delete_source видаляє src.
  E. Видалення напрямку: посилання (transcriptions.category_id) обнуляються,
     самі записи лишаються.
  F. _norm_cat: casefold + стиск пробілів.

Запуск:  .venv/Scripts/python.exe test_categories.py
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from flask import Flask

from app.db.migrations import init_database
from app.blueprints.memory import memory_bp, _norm_cat


_fail = 0


def check(cond, msg):
    global _fail
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        _fail += 1


def _make_app(db_path):
    init_database(db_path)
    app = Flask(__name__)
    app.config["DATABASE"] = db_path
    app.config["TESTING"] = True
    app.register_blueprint(memory_bp)
    return app


def _cat_id(client, name):
    r = client.post("/api/memory/categories", json={"name": name})
    return r


def _insert_tx(db_path, category_id):
    """Вставити мінімальний транскрипт із заданим напрямком, повернути id."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, category_id) VALUES (?, ?, ?)",
        ("file", "smoke.mp3", category_id))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def _tx_category(db_path, tid):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?", (tid,)).fetchone()
    conn.close()
    return row[0] if row else "MISSING"


def main():
    print("\n=== Напрямки: casefold-унікальність + керування ===\n")

    # --- F. _norm_cat ----------------------------------------------------
    check(_norm_cat("  Фонд   Робота  ") == "фонд робота", "F1 _norm_cat: casefold + стиск пробілів")
    check(_norm_cat("AI / Професія") == _norm_cat("ai / професія"), "F2 _norm_cat: регістр не впливає (кирилиця+латиниця)")

    tmpdir = tempfile.mkdtemp(prefix="recall_cat_test_")
    db_path = os.path.join(tmpdir, "test.db")
    app = _make_app(db_path)
    client = app.test_client()

    # --- A. Міграція -----------------------------------------------------
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(categories)").fetchall()}
    check("name_norm" in cols, "A1 міграція v20: колонка categories.name_norm існує")
    seeded = conn.execute("SELECT name, name_norm FROM categories WHERE name_norm IS NOT NULL").fetchall()
    check(len(seeded) >= 5, "A2 засіви напрямків мають name_norm backfill")
    check(all(nn == _norm_cat(n) for n, nn in seeded), "A3 name_norm засівів == casefold(name)")
    conn.close()

    # --- B. Створення + casefold-дубль -----------------------------------
    r1 = client.post("/api/memory/categories", json={"name": "Проєкт Альфа"})
    check(r1.status_code == 200 and r1.get_json().get("success"), "B1 створення нового напрямку → 200")
    new_id = r1.get_json()["id"]

    conn = sqlite3.connect(db_path)
    nn = conn.execute("SELECT name_norm FROM categories WHERE id = ?", (new_id,)).fetchone()[0]
    conn.close()
    check(nn == "проєкт альфа", "B2 збережений name_norm = lowercase/casefold ключ")

    r2 = client.post("/api/memory/categories", json={"name": "проєкт альфа"})
    check(r2.status_code == 409, "B3 кириличний casefold-дубль («проєкт альфа») → 409 (фікс NOCASE-дірки)")

    r2b = client.post("/api/memory/categories", json={"name": "  ПРОЄКТ   альфа "})
    check(r2b.status_code == 409, "B4 дубль із іншим регістром+пробілами → 409")

    r3 = client.post("/api/memory/categories", json={"name": "   "})
    check(r3.status_code == 400, "B5 порожня назва → 400")

    # --- C. Перейменування -----------------------------------------------
    rB = client.post("/api/memory/categories", json={"name": "Проєкт Бета"})
    beta_id = rB.get_json()["id"]
    rc1 = client.patch(f"/api/memory/categories/{beta_id}", json={"name": "проєкт альфа"})
    check(rc1.status_code == 409, "C1 перейменування у casefold-зайняту назву → 409")
    rc2 = client.patch(f"/api/memory/categories/{beta_id}", json={"name": "Проєкт Гамма"})
    check(rc2.status_code == 200, "C2 валідне перейменування → 200")
    conn = sqlite3.connect(db_path)
    nn2 = conn.execute("SELECT name_norm FROM categories WHERE id = ?", (beta_id,)).fetchone()[0]
    conn.close()
    check(nn2 == "проєкт гамма", "C3 перейменування оновило name_norm")
    rc3 = client.patch(f"/api/memory/categories/{beta_id}", json={"name": "Проєкт Гамма", "color": "#ff0000"})
    check(rc3.status_code == 200, "C4 «перейменування» на ту саму назву (виключає себе) → 200")

    # --- D. Перенесення / обʼєднання -------------------------------------
    tid = _insert_tx(db_path, new_id)          # транскрипт у «Проєкт Альфа»
    rm = client.post(f"/api/memory/categories/{new_id}/merge",
                     json={"target_id": beta_id, "delete_source": True})
    check(rm.status_code == 200 and rm.get_json().get("moved") == 1, "D1 merge: 1 запис перенесено")
    check(_tx_category(db_path, tid) == beta_id, "D2 merge: transcript.category_id → цільовий напрямок")
    conn = sqlite3.connect(db_path)
    gone = conn.execute("SELECT 1 FROM categories WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    check(gone is None, "D3 merge delete_source: джерело видалено")

    rm2 = client.post(f"/api/memory/categories/{beta_id}/merge", json={"target_id": beta_id})
    check(rm2.status_code == 400, "D4 merge у самого себе → 400")

    # --- E. Видалення обнуляє посилання ----------------------------------
    tid2 = _insert_tx(db_path, beta_id)
    rd = client.delete(f"/api/memory/categories/{beta_id}")
    check(rd.status_code == 200, "E1 видалення напрямку → 200")
    check(_tx_category(db_path, tid2) is None, "E2 видалення: transcript.category_id обнулено (запис лишився)")
    conn = sqlite3.connect(db_path)
    still = conn.execute("SELECT 1 FROM transcriptions WHERE id = ?", (tid2,)).fetchone()
    conn.close()
    check(still is not None, "E3 сам транскрипт НЕ видалено")

    print(f"\n=== Підсумок: {'усі пройшли' if _fail == 0 else str(_fail) + ' впало'} ===\n")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
