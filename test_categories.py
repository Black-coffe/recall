"""Offline smoke test of directions (Phase 14.1): casefold uniqueness + management.

No models / Ollama / network. Brings up a MINIMAL Flask with only memory_bp on a
temporary DB (the full migration schema via init_database) and verifies:
  A. Migration v20: column categories.name_norm + the seeds have name_norm.
  B. Creation: name_norm = casefold(name); a Cyrillic duplicate («Фонд» vs «фонд»)
     is rejected with 409 (SQLite COLLATE NOCASE did NOT catch it - the point of the fix).
  C. Renaming: updates name_norm; a casefold collision -> 409; excludes itself.
  D. Move/merge: records src -> dst; delete_source deletes src.
  E. Deleting a direction: references (transcriptions.category_id) are nulled,
     the records themselves stay.
  F. _norm_cat: casefold + whitespace collapsing.

Run:  .venv/Scripts/python.exe test_categories.py
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
    """Insert a minimal transcript with the given direction, return its id."""
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
    print("\n=== Directions: casefold uniqueness + management ===\n")

    # --- F. _norm_cat ----------------------------------------------------
    check(_norm_cat("  Фонд   Робота  ") == "фонд робота", "F1 _norm_cat: casefold + whitespace collapsing")
    check(_norm_cat("AI / Професія") == _norm_cat("ai / професія"), "F2 _norm_cat: case does not matter (Cyrillic+Latin)")

    tmpdir = tempfile.mkdtemp(prefix="recall_cat_test_")
    db_path = os.path.join(tmpdir, "test.db")
    app = _make_app(db_path)
    client = app.test_client()

    # --- A. Migration ----------------------------------------------------
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(categories)").fetchall()}
    check("name_norm" in cols, "A1 migration v20: column categories.name_norm exists")
    seeded = conn.execute("SELECT name, name_norm FROM categories WHERE name_norm IS NOT NULL").fetchall()
    check(len(seeded) >= 5, "A2 direction seeds have a name_norm backfill")
    check(all(nn == _norm_cat(n) for n, nn in seeded), "A3 seeds' name_norm == casefold(name)")
    conn.close()

    # --- B. Creation + casefold duplicate --------------------------------
    r1 = client.post("/api/memory/categories", json={"name": "Проєкт Альфа"})
    check(r1.status_code == 200 and r1.get_json().get("success"), "B1 creating a new direction -> 200")
    new_id = r1.get_json()["id"]

    conn = sqlite3.connect(db_path)
    nn = conn.execute("SELECT name_norm FROM categories WHERE id = ?", (new_id,)).fetchone()[0]
    conn.close()
    check(nn == "проєкт альфа", "B2 the stored name_norm = lowercase/casefold key")

    r2 = client.post("/api/memory/categories", json={"name": "проєкт альфа"})
    check(r2.status_code == 409, "B3 Cyrillic casefold duplicate («проєкт альфа») -> 409 (the NOCASE hole fix)")

    r2b = client.post("/api/memory/categories", json={"name": "  ПРОЄКТ   альфа "})
    check(r2b.status_code == 409, "B4 duplicate with another case+spaces -> 409")

    r3 = client.post("/api/memory/categories", json={"name": "   "})
    check(r3.status_code == 400, "B5 empty name -> 400")

    # --- C. Renaming -----------------------------------------------------
    rB = client.post("/api/memory/categories", json={"name": "Проєкт Бета"})
    beta_id = rB.get_json()["id"]
    rc1 = client.patch(f"/api/memory/categories/{beta_id}", json={"name": "проєкт альфа"})
    check(rc1.status_code == 409, "C1 renaming to a casefold-taken name -> 409")
    rc2 = client.patch(f"/api/memory/categories/{beta_id}", json={"name": "Проєкт Гамма"})
    check(rc2.status_code == 200, "C2 valid rename -> 200")
    conn = sqlite3.connect(db_path)
    nn2 = conn.execute("SELECT name_norm FROM categories WHERE id = ?", (beta_id,)).fetchone()[0]
    conn.close()
    check(nn2 == "проєкт гамма", "C3 rename updated name_norm")
    rc3 = client.patch(f"/api/memory/categories/{beta_id}", json={"name": "Проєкт Гамма", "color": "#ff0000"})
    check(rc3.status_code == 200, "C4 \"rename\" to the same name (excludes itself) -> 200")

    # --- D. Move / merge -------------------------------------------------
    tid = _insert_tx(db_path, new_id)          # a transcript in «Проєкт Альфа»
    rm = client.post(f"/api/memory/categories/{new_id}/merge",
                     json={"target_id": beta_id, "delete_source": True})
    check(rm.status_code == 200 and rm.get_json().get("moved") == 1, "D1 merge: 1 record moved")
    check(_tx_category(db_path, tid) == beta_id, "D2 merge: transcript.category_id -> target direction")
    conn = sqlite3.connect(db_path)
    gone = conn.execute("SELECT 1 FROM categories WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    check(gone is None, "D3 merge delete_source: the source is deleted")

    rm2 = client.post(f"/api/memory/categories/{beta_id}/merge", json={"target_id": beta_id})
    check(rm2.status_code == 400, "D4 merge into itself -> 400")

    # --- E. Deleting nulls the references --------------------------------
    tid2 = _insert_tx(db_path, beta_id)
    rd = client.delete(f"/api/memory/categories/{beta_id}")
    check(rd.status_code == 200, "E1 deleting a direction -> 200")
    check(_tx_category(db_path, tid2) is None, "E2 deleting: transcript.category_id nulled (the record stayed)")
    conn = sqlite3.connect(db_path)
    still = conn.execute("SELECT 1 FROM transcriptions WHERE id = ?", (tid2,)).fetchone()
    conn.close()
    check(still is not None, "E3 the transcript itself is NOT deleted")

    print(f"\n=== Summary: {'all passed' if _fail == 0 else str(_fail) + ' failed'} ===\n")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
