"""Карточка docs/defects/pipe-failure-loses-brief.md: збій труби о 9:00 не має з'їдати брифінг.

Використання: check_pipe_retry.py <pipes.py> — вантажить файл як модуль труб і проганяє три
сценарії на тимчасовій БД; exit 1, якщо в будь-якому брифінг так і не доставлено:
  A. доставка впала о 9:00, о 9:20 канал живий;
  B. запуск завис у `running` (процес помер), Recall піднявся об 11:00;
  C. о 9:30 був ручний `pipes run` (у консоль), Recall піднявся о 12:00;
  D. фільтр приватного зупинив брифінг (стоп-лист не читається) — у канал має прийти
     повідомлення про це, а не тиша;
  E. зведення питань (skip_if_empty) не знає, хто власник Telegram (порожньо + `reason`) — у канал
     має прийти причина, а не тиша, як при «питань нема».
Без мережі й Claude: функцію даних підмінено, ключа нема, стоп-слів нема.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PIPE = ("---\nschedule: 0 9 * * 1\ntools: weekly_digest\nchannel: telegram\n"
        "budget_usd: 1\n---\nПерекажи звід.")
MON = datetime(2026, 10, 19)


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"pipes_under_check_{abs(hash(path))}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                # dataclass шукає свій модуль у sys.modules
    spec.loader.exec_module(mod)
    mod.TOOLS["weekly_digest"] = lambda db, now: {"this_week": [{"task": "Надіслати Олені звіт"}]}
    return mod


def _db(tmp: str, name: str) -> str:
    from app.db.migrations import init_database
    path = os.path.join(tmp, f"{name}.db")
    init_database(path)
    return path


def scenario_a(mod, db) -> bool:
    clock = {"t": MON.replace(hour=9)}
    sent, fail = [], {"on": True}

    def deliver(text):
        if fail["on"]:
            raise ConnectionError("Telegram недоступний")
        sent.append(text)
    s = mod.PipeScheduler([mod.parse_pipe(PIPE, "звід")], db_path=db, deliver=deliver,
                          clock=lambda: clock["t"])
    s.step("schedule")
    fail["on"] = False
    clock["t"] = MON.replace(hour=9, minute=20)
    s.step("schedule")
    return len(sent) == 1


def scenario_b(mod, db) -> bool:
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO pipe_runs (pipe, trigger, scheduled_for, started_at, status) "
                     "VALUES ('звід', 'schedule', '2026-10-19 09:00:00', '2026-10-19 09:00:00', "
                     "'running')")
    sent = []
    s = mod.PipeScheduler([mod.parse_pipe(PIPE, "звід")], db_path=db, deliver=sent.append,
                          clock=lambda: MON.replace(hour=11))
    s.step("catchup")
    return len(sent) == 1


def scenario_c(mod, db) -> bool:
    pipe = mod.parse_pipe(PIPE, "звід")
    mod.run_pipe(pipe, db_path=db, deliver=lambda t: None, now=MON.replace(hour=9, minute=30))
    sent = []
    s = mod.PipeScheduler([pipe], db_path=db, deliver=sent.append,
                          clock=lambda: MON.replace(hour=12))
    s.step("catchup")
    return len(sent) == 1


def scenario_d(mod, db) -> bool:
    words = os.environ["RECALL_PRIVATE_WORDS_FILE"]
    with open(words, "wb") as f:                  # нечитабельний стоп-лист → blocked
        f.write(b"\xff\xfe\xfa")
    try:
        sent = []
        mod.run_pipe(mod.parse_pipe(PIPE, "звід"), db_path=db, deliver=sent.append,
                     now=MON.replace(hour=9))
        return len(sent) == 1 and "Олені" not in sent[0]
    finally:
        os.remove(words)


def scenario_e(mod, db) -> bool:
    pipe = mod.parse_pipe(PIPE.replace("tools: weekly_digest", "tools: open_questions")
                          .replace("budget_usd: 1", "budget_usd: 1\nskip_if_empty: true"), "питання")
    mod.TOOLS["open_questions"] = lambda d, n: {"me": None, "questions": [], "skipped_group": 0,
                                                "reason": "невідомо, хто власник архіву"}
    sent = []
    mod.run_pipe(pipe, db_path=db, deliver=sent.append, now=MON.replace(hour=9))
    return len(sent) == 1 and "власник" in sent[0]


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else ROOT / "app" / "services" / "pipes.py"
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ["PIPES_RETRY_MINUTES"] = "15"
    os.environ["PIPES_RETRY_MAX"] = "6"
    import logging
    logging.disable(logging.CRITICAL)
    lost = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        os.environ["RECALL_PRIVATE_WORDS_FILE"] = os.path.join(tmp, "нема.txt")
        mod = _load(target)
        for name, fn in (("A: збій доставки о 9:00", scenario_a),
                         ("B: завислий running", scenario_b),
                         ("C: ручний запуск після 9:00", scenario_c),
                         ("D: фільтр приватного зупинив — тиша", scenario_d),
                         ("E: власник Telegram невідомий — тиша", scenario_e)):
            if not fn(mod, _db(tmp, name[0])):
                lost.append(name)
    for name in lost:
        print(f"{target}: брифінг пропав — {name}")
    return 1 if lost else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
