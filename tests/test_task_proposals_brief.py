"""task-approval-05: the daily Telegram batch of task proposals (Yes / No), the expiry pass,
the Monday stuck block and the proposals counter line."""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from datetime import date, datetime, timedelta

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import brief_actions as ba
from app.services import categories as cats
from app.services import commitments, pipes, pipes_cards
from app.services.pipes import run_pipe

CHAT = "777"
NOW = datetime(2026, 10, 27, 9, 0)
TODAY = NOW.date()


class Fake:
    """Bot API transport: records (method, params), answers ok."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, data):
        method = url.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in urllib.parse.parse_qs(data.decode()).items()} if data else {}
        self.calls.append((method, params))
        return json.dumps({"ok": True, "result": True}).encode()

    def rows(self):
        p = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]
        return json.loads(p["reply_markup"])["inline_keyboard"]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_PRIVATE_CATEGORIES", "Закрите тест")
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(tmp_path / "private.txt"))
    (tmp_path / "private.txt").write_text("таємниця\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.setenv("TELEGRAM_BOT_CHAT_ID", CHAT)
    monkeypatch.setenv("RECALL_PUBLIC_URL", "https://recall.test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ba._KEYBOARDS.clear()
    yield
    ba._KEYBOARDS.clear()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "p.db")
    init_database(path)
    with get_db_connection(path) as conn:
        private = cats.create_category(conn, "Закрите тест")["id"]
        work = cats.create_category(conn, "Робота тест")["id"]
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (1, 'recording', 'Зустріч', ?)", (work,))
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (2, 'recording', 'Особисте', ?)", (private,))
        conn.commit()
    return path


def _add(db, iid, task, *, tid=1, due=None, importance=2, status="proposed",
         shown=None, for_whom="owner", decided=None):
    with get_db_connection(db) as conn:
        conn.execute(
            "INSERT INTO action_items (id, transcription_id, task, status, due_date, importance, "
            "for_whom, confidence, first_shown_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iid, tid, task, status, due, importance, for_whom, 0.9, shown, decided))
        conn.commit()


def _seed_queue(db, n=12):
    """`n` plain proposals (ids 100..), a private-direction one (1) and a stop-word one (2),
    both with the nearest deadline so they would take the first slots if not filtered."""
    _add(db, 1, "Приватна справа", tid=2, due="2026-10-01")
    _add(db, 2, "Обговорити таємниця з юристом", due="2026-10-01")
    for i in range(n):
        _add(db, 100 + i, f"Задача номер {i}", due=f"2026-11-{10 + i:02d}" if i < 6 else None,
             importance=3 if i % 2 else 1)


def _events(db, action):
    with get_db_connection(db) as conn:
        return conn.execute("SELECT item_id, source FROM action_item_events WHERE action = ? "
                            "ORDER BY id", (action,)).fetchall()


def _shown(db):
    with get_db_connection(db) as conn:
        return {r[0] for r in conn.execute(
            "SELECT id FROM action_items WHERE first_shown_at IS NOT NULL")}


def _pipe():
    return next(p for p in pipes.load_pipes() if p.name == "task-proposals")


# ------------------------------------------------------------- the tool

def test_tool_expires_first_then_selects_ten_in_queue_order_without_hidden(db):
    _seed_queue(db)
    _add(db, 50, "Стара пропозиція", shown=(NOW - timedelta(days=4)).isoformat())
    res = pipes.TOOLS["task_proposals"](db, NOW)
    assert res["expired"] == 1
    assert [(r["item_id"], r["source"]) for r in _events(db, "expire")] == [(50, "system")]
    expected = [r["id"] for r in commitments.proposal_queue(db, limit=100)
                if r["id"] not in (1, 2)][:10]
    assert [it["id"] for it in res["items"]] == expected
    assert len(res["items"]) == 10 and 1 not in expected and 2 not in expected
    assert res["queued"] == 14 and res["hidden"] == 2


def test_tool_without_action_items_is_not_collected(tmp_path):
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    res = pipes.TOOLS["task_proposals"](path, NOW)
    assert res["reason"] and res["items"] == []
    assert pipes.not_collected({"task_proposals": res})


# ------------------------------------------------------------- run_pipe

def test_run_marks_shown_only_rendered_ids_after_delivery(db):
    _seed_queue(db)
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=lambda t, **kw: sent.append((t, kw)), now=NOW,
                   buttons=True)
    assert res["status"] == "ok" and res["rendered_by"] == "plain"
    rows = sent[0][1]["reply_markup"]["inline_keyboard"]
    ids = [int(r[0]["callback_data"].split(":")[2]) for r in rows]
    assert len(ids) == 10
    assert _shown(db) == set(ids)
    assert sorted(r["item_id"] for r in _events(db, "show")) == sorted(ids)
    assert {r["source"] for r in _events(db, "show")} == {"bot"}
    # Same day again: the same unpressed cards, no new show events.
    sent2 = []
    run_pipe(_pipe(), db_path=db, deliver=lambda t, **kw: sent2.append((t, kw)), now=NOW,
             buttons=True)
    rows2 = sent2[0][1]["reply_markup"]["inline_keyboard"]
    assert [int(r[0]["callback_data"].split(":")[2]) for r in rows2] == ids
    assert len(_events(db, "show")) == 10


def test_failed_delivery_and_cli_mark_nothing(db):
    _seed_queue(db, n=3)

    def boom(text, **kw):
        if kw.get("reply_markup"):
            raise RuntimeError("telegram down")

    res = run_pipe(_pipe(), db_path=db, deliver=boom, now=NOW, buttons=True)
    assert res["status"] == "error"
    assert _shown(db) == set() and _events(db, "show") == []
    out = []
    res = run_pipe(_pipe(), db_path=db, deliver=out.append, now=NOW)
    assert res["status"] == "ok" and out
    assert _shown(db) == set() and _events(db, "show") == []


def test_skip_if_empty_and_hidden_only_is_not_skipped(db):
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=lambda t, **kw: sent.append(t), now=NOW,
                   buttons=True)
    assert res["status"] == "skipped" and sent == []
    _add(db, 1, "Приватна справа", tid=2)
    res = run_pipe(_pipe(), db_path=db, deliver=lambda t, **kw: sent.append(t), now=NOW,
                   buttons=True)
    assert res["status"] == "ok" and "Приватна" not in sent[0]
    assert _shown(db) == set()


def test_leaked_stop_word_blocks_the_send(db, monkeypatch):
    _seed_queue(db, n=2)
    # The data filter is bypassed: the second line (the render guard) must stop it.
    monkeypatch.setattr(pipes.pipes_privacy, "filter_data", lambda data, db_path: (data, 0))
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=lambda t, **kw: sent.append((t, kw)), now=NOW,
                   buttons=True)
    assert res["status"] == "blocked"
    assert all("таємниця" not in t and not kw for t, kw in sent)
    assert _shown(db) == set()


# ------------------------------------------------------------- render

def test_render_numbered_cards_masked_with_task_link(db):
    _add(db, 7, "Подзвонити +380 67 123 45 67 щодо $5000", due="2026-11-02", importance=3,
         for_whom="promised_to_owner")
    data = {"task_proposals": pipes.TOOLS["task_proposals"](db, NOW)}
    buttons, props = [], []
    text, leaked = pipes_cards.render(pipes_cards.title_for(["task_proposals"]), data,
                                      db_path=db, today=TODAY, buttons=buttons, proposals=props)
    assert not leaked and buttons == [7] and props == [7]
    assert set(re.findall(r"</?([a-z]+)", text)) <= {"b", "i", "a"}
    assert "<b>1.</b>" in text
    assert 'href="https://recall.test/transcript/1?task=7"' in text
    assert "380" not in text and "5000" not in text and "•••" in text
    assert pipes_cards._t("proposals.forWhom.promised_to_owner") in text
    assert pipes_cards._t("proposals.importance.3") in text and "02.11" in text
    assert text.count(pipes_cards._t("proposals.title")) == 1


# ------------------------------------------------------------- buttons

def _cq(data, kb, *, chat=CHAT):
    return {"id": "q", "data": data, "from": {"id": 1, "username": "o"},
            "message": {"message_id": 9, "chat": {"id": int(chat)}, "reply_markup": kb}}


def _status(db, iid):
    with get_db_connection(db) as conn:
        return conn.execute("SELECT status FROM action_items WHERE id=?", (iid,)).fetchone()[0]


def test_keyboard_yes_no_rows_and_callback_budget():
    big = 2 ** 63 - 1
    kb = ba.keyboard([5, big], proposal_ids=frozenset({5, big}))
    rows = kb["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == ["a:1:5:approve", "a:1:5:reject"]
    assert all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)
    plain = ba.keyboard([5])["inline_keyboard"][0]
    assert plain[0]["callback_data"] == "a:1:5:done"


@pytest.mark.parametrize("action,status", [("approve", "open"), ("reject", "rejected")])
def test_press_decides_and_undo_restores_proposed(db, action, status):
    _add(db, 7, "Підготувати бриф")
    kb = ba.keyboard([7], proposal_ids=frozenset({7}))
    f = Fake()
    ba.handle(_cq(f"a:1:7:{action}", kb), db_path=db, transport=f)
    assert _status(db, 7) == status
    with get_db_connection(db) as conn:
        ev = conn.execute("SELECT id, source FROM action_item_events WHERE item_id=7 "
                          "ORDER BY id DESC").fetchone()
    assert ev["source"] == "bot"
    row = f.rows()[0]
    assert row[0]["callback_data"] == "x" and row[1]["callback_data"] == f"u:1:{ev['id']}"
    ba.handle(_cq(f"u:1:{ev['id']}", kb), db_path=db, transport=f)
    assert _status(db, 7) == "proposed"
    assert [b["callback_data"] for b in f.rows()[0]] == ["a:1:7:approve", "a:1:7:reject"]


def test_press_from_foreign_chat_is_refused(db):
    _add(db, 7, "Підготувати бриф")
    kb = ba.keyboard([7], proposal_ids=frozenset({7}))
    assert ba.handle(_cq("a:1:7:approve", kb, chat="999"), db_path=db,
                     transport=Fake()) == ba._t("noAccess")
    assert _status(db, 7) == "proposed"


# ------------------------------------------------------------- weekly digest

def test_digest_stuck_block_with_k5_rows_and_stats_line(db):
    _add(db, 30, "Застрягла задача", status="open",
         decided=(NOW - timedelta(days=20)).isoformat(timespec="seconds"))
    data = {"weekly_digest": pipes.TOOLS["weekly_digest"](db, NOW)}
    assert [it["id"] for it in data["weekly_digest"]["stuck"]] == [30]
    buttons = []
    text, leaked = pipes_cards.render("T", data, db_path=db, today=TODAY, buttons=buttons)
    assert not leaked
    assert pipes_cards._t("stuck") in text
    assert pipes_cards._t("stuckOld", days=commitments.STUCK_DAYS) in text
    assert 30 in buttons
    kb = ba.keyboard(buttons)
    n = buttons.index(30) + 1
    assert kb["inline_keyboard"][n - 1][0]["callback_data"] == f"a:{n}:30:done"
    stats = pipes_cards._t("proposals.stats", shown=0, pressed=0, days=0, window=7, active=1)
    assert html_unescape(stats) in html_unescape(text)


def test_stats_line_reads_zeros_on_an_empty_archive(db):
    data = {"weekly_digest": pipes.TOOLS["weekly_digest"](db, NOW)}
    text, _ = pipes_cards.render("T", data, db_path=db, today=TODAY)
    zero = pipes_cards._t("proposals.stats", shown=0, pressed=0, days=0, window=7, active=0)
    assert html_unescape(zero) in html_unescape(text)


def html_unescape(s):
    import html
    return html.unescape(s)


# ------------------------------------------------------------- pipe file + texts

def test_pipe_file_loads():
    pipe = _pipe()
    assert pipe.schedule.expr == "0 9 * * *" and pipe.tools == ["task_proposals"]
    assert pipe.budget_usd == 0 and pipe.skip_if_empty is True


def test_new_texts_have_uk_and_en():
    from app.core.i18n_catalog.telegram import MESSAGES
    keys = [k for k in MESSAGES if k.startswith(("telegram.brief.proposals.",
                                                 "telegram.brief.stuck"))]
    keys += ["telegram.btn.yes", "telegram.btn.no", "telegram.btn.label.approve",
             "telegram.btn.label.reject"]
    assert len(keys) >= 16
    for k in keys:
        assert set(MESSAGES[k]) >= {"uk", "en"}
