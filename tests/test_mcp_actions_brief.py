"""mcp-telegram-automation-05: the daily "MCP did this" brief with undo where an inverse exists."""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import brief_actions as ba
from app.services import categories as cats
from app.services import commitments, pipes, pipes_cards, pipes_privacy, telegram_chats
from app.services.pipes import run_pipe

CHAT = "777"
NOW = datetime(2026, 10, 25, 9, 0)


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_PRIVATE_CATEGORIES", "Закрите тест")
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(tmp_path / "private.txt"))
    (tmp_path / "private.txt").write_text("таємниця\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.setenv("TELEGRAM_BOT_CHAT_ID", CHAT)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ba._KEYBOARDS.clear()
    yield
    ba._KEYBOARDS.clear()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "m.db")
    init_database(path)
    return path


@pytest.fixture
def seeded(db):
    """Audit rows, a task event and two chat events, inside and outside the window."""
    with get_db_connection(db) as conn:
        private = cats.create_category(conn, "Закрите тест")["id"]
        work = cats.create_category(conn, "Робота тест")["id"]
        other = cats.create_category(conn, "Навчання тест")["id"]
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (1, 'recording', 'Зустріч', ?)", (work,))
        conn.execute("INSERT INTO action_items (id, transcription_id, task, status) "
                     "VALUES (11, 1, 'Надіслати Олені договір', 'open')")
        conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, enabled, category_id) VALUES "
                     "(-1001, 'Робочий чат', 1, ?), (-1002, 'Навчальний чат', 1, ?)",
                     (work, work))
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        recent = _utc(NOW - timedelta(hours=2)).strftime(fmt)
        old = _utc(NOW - timedelta(hours=30)).strftime(fmt)
        rows = [
            (recent, "unknown", "tasks", "POST", "/api/x", "memory.task_action", "done", None, 200),
            (recent, "unknown", "tasks", "POST", "/api/x", "memory.task_action", "done", None, 200),
            (recent, "unknown", "telegram", "POST", "/api/y", "telegram.disable", "disable",
             "denied:owner", 403),
            (recent, "ui", None, "POST", "/api/z", "ui.endpoint", "ui_action", None, 200),
            (old, "unknown", "old_tool", "POST", "/api/o", "old.endpoint", "old_action", None, 200),
        ]
        conn.executemany(
            "INSERT INTO mcp_actions (at, actor, claimed_tool, method, path, endpoint, action, "
            "decision, http_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    ids = {"private": private, "work": work, "other": other}
    # Real write paths: they stamp "now"; rows are then moved into / out of the window.
    task = commitments.apply_action(db, 11, "done", source="mcp", actor="mcp")
    cat_ev = telegram_chats.set_category(db, -1001, other, source="mcp", actor="mcp")
    dis_ev = telegram_chats.set_enabled(db, -1002, False, source="mcp", actor="mcp")
    ui_ev = telegram_chats.set_category(db, -1002, other, source="ui", actor="ui")
    with get_db_connection(db) as conn:
        conn.execute("UPDATE action_item_events SET at = ? WHERE id = ?",
                     ((NOW - timedelta(hours=1)).isoformat(timespec="seconds"), task["event_id"]))
        conn.execute("UPDATE tg_chat_events SET at = ?",
                     (_utc(NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
    ids.update(task_event=task["event_id"], cat_event=cat_ev["event_id"],
               dis_event=dis_ev["event_id"], ui_event=ui_ev["event_id"])
    return ids


# ------------------------------------------------------------- the tool

def test_summary_shape_window_ui_refused_and_undo(db, seeded):
    res = pipes.TOOLS["mcp_actions"](db, NOW)
    assert res["window_h"] == 24
    tools = {(g["tool"], g["action"]): g["count"] for g in res["groups"]}
    assert tools == {("tasks", "done"): 2, ("telegram", "disable"): 1}
    assert res["total"] == 3 and res["refused"] == 1
    undo = [it["undo"] for it in res["items"]]
    assert {"kind": "task", "ref": seeded["task_event"]} in undo
    assert {"kind": "chat", "ref": seeded["cat_event"]} in undo
    assert {"kind": "chat", "ref": seeded["dis_event"]} in undo
    assert all(u["ref"] != seeded["ui_event"] for u in undo)
    cat_item = next(it for it in res["items"] if it["undo"]["ref"] == seeded["cat_event"]
                    and it["undo"]["kind"] == "chat")
    assert cat_item["old"] == "Робота тест" and cat_item["new"] == "Навчання тест"
    assert cat_item["category_id"] == seeded["other"]


def test_summary_drops_events_older_than_window(db, seeded):
    with get_db_connection(db) as conn:
        conn.execute("UPDATE action_item_events SET at = ?",
                     ((NOW - timedelta(hours=30)).isoformat(timespec="seconds"),))
        conn.execute("UPDATE tg_chat_events SET at = ?",
                     (_utc(NOW - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
    assert pipes.TOOLS["mcp_actions"](db, NOW)["items"] == []


def test_summary_without_audit_table_is_not_collected(tmp_path):
    path = str(tmp_path / "bare.db")
    sqlite3.connect(path).close()
    res = pipes.TOOLS["mcp_actions"](path, NOW)
    assert res.get("reason")
    assert pipes.not_collected({"mcp_actions": res})


# ----------------------------------------------------------- privacy

def test_filter_drops_stop_word_and_private_direction_keeps_none(db, seeded):
    data = {"mcp_actions": {"groups": [], "items": [
        {"text": "Чат таємниця", "category_id": None, "undo": None},
        {"text": "Родинний чат", "category_id": seeded["private"], "undo": None},
        {"text": "Без напрямку", "category_id": None, "undo": None},
        {"text": "Робочий чат", "category_id": seeded["work"], "undo": None},
    ]}}
    out, hidden = pipes_privacy.filter_data(data, db_path=db)
    assert [it["text"] for it in out["mcp_actions"]["items"]] == ["Без напрямку", "Робочий чат"]
    assert hidden == 2


def test_leaked_word_surviving_render_blocks_the_run(db, seeded, monkeypatch):
    monkeypatch.setitem(pipes.TOOLS, "mcp_actions", lambda p, n: {
        "groups": [{"tool": "x", "action": "y", "count": 1}], "items": [], "total": 1,
        "refused": 0})
    # The data filter passes (no word yet); the word appears only in the rendered text.
    real = pipes_cards.render
    monkeypatch.setattr(pipes_cards, "render",
                        lambda *a, **k: (real(*a, **k)[0] + "\nтаємниця", []))
    sent = []
    pipe = pipes.parse_pipe("---\nschedule: 0 9 * * *\ntools: [mcp_actions]\nchannel: telegram\n"
                            "budget_usd: 0\n---\nx", "m")
    res = run_pipe(pipe, db_path=db, deliver=lambda t, **kw: sent.append(t), now=NOW)
    assert res["status"] == "blocked"
    assert all("таємниця" not in t for t in sent)


# ------------------------------------------------------------- render

def test_render_section_groups_numbered_items_and_undo_items(db, seeded):
    data = {"mcp_actions": pipes.TOOLS["mcp_actions"](db, NOW)}
    undo: list = []
    text, leaked = pipes_cards.render("T", data, db_path=db, today=date(2026, 10, 25),
                                      buttons=[], undo_items=undo)
    assert not leaked
    assert "tasks · done: 2" in text and "telegram · disable: 1" in text
    assert "<b>1.</b>" in text and "<b>3.</b>" in text
    assert [u["n"] for u in undo] == [1, 2, 3]
    assert {u["kind"] for u in undo} == {"task", "chat"}


def test_pipe_message_prints_the_section_header_once(db, seeded):
    pipe = next(p for p in pipes.load_pipes() if p.name == "mcp-actions")
    sent = []
    run_pipe(pipe, db_path=db, deliver=lambda t, **kw: sent.append(t), now=NOW, buttons=True)
    title = pipes_cards._t("mcp.title")
    assert sent and sent[0].count(title) == 1
    other, _ = pipes_cards.render("T", {"mcp_actions": pipes.TOOLS["mcp_actions"](db, NOW)},
                                  db_path=db, today=date(2026, 10, 25))
    assert other.count(title) == 1


def test_render_empty_summary_renders_no_section_and_skip_if_empty_skips(db):
    data = {"mcp_actions": pipes.TOOLS["mcp_actions"](db, NOW)}
    text, _ = pipes_cards.render("T", data, db_path=db, today=date(2026, 10, 25))
    assert "MCP" not in text.split("\n\n", 1)[1]
    pipe = next(p for p in pipes.load_pipes() if p.name == "mcp-actions")
    sent = []
    res = run_pipe(pipe, db_path=db, deliver=lambda t, **kw: sent.append(t), now=NOW)
    assert res["status"] == "skipped" and sent == []


# ------------------------------------------------------------ keyboard

def test_keyboard_undo_rows_and_callback_budget():
    big = 2 ** 63 - 1
    kb = ba.keyboard([big] * ba.MAX_ITEMS, [{"n": 999, "kind": "task", "ref": big}])
    kb2 = ba.keyboard([], [{"n": 1, "kind": "task", "ref": 5}, {"n": 2, "kind": "chat", "ref": big}])
    rows = kb2["inline_keyboard"]
    assert [r[0]["callback_data"] for r in rows] == ["m:1:t:5", f"m:2:c:{big}"]
    for row in kb["inline_keyboard"] + rows:
        for b in row:
            assert len(b["callback_data"].encode()) <= 64
    assert sum(len(r) for r in kb["inline_keyboard"]) <= ba.MAX_BUTTONS


# ------------------------------------------------------------- presses

class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, url, data):
        method = url.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in urllib.parse.parse_qs(data.decode()).items()} if data else {}
        self.calls.append((method, params))
        return json.dumps({"ok": True, "result": True}).encode()

    def of(self, method):
        return [p for m, p in self.calls if m == method]


def _cq(data, kb, chat=CHAT):
    return {"id": "q", "data": data, "from": {"id": 1, "username": "owner"},
            "message": {"message_id": 3, "chat": {"id": int(chat)}, "reply_markup": kb}}


def test_press_task_undo_calls_commitments(db, seeded):
    kb = ba.keyboard([], [{"n": 1, "kind": "task", "ref": seeded["task_event"]}])
    f = Fake()
    reply = ba.handle(_cq(f"m:1:t:{seeded['task_event']}", kb), db_path=db, transport=f)
    assert reply == ba._t("undone")
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT status FROM action_items WHERE id=11").fetchone()[0] == "open"
        ev = conn.execute("SELECT source, actor, undo_of FROM action_item_events "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    assert ev["source"] == "bot" and ev["actor"].startswith("tg:1") \
        and ev["undo_of"] == seeded["task_event"]
    row = json.loads(f.of("editMessageReplyMarkup")[-1]["reply_markup"])["inline_keyboard"][0]
    assert row == [{"text": f"1 {ba._t('undo.done')}", "callback_data": "x"}]


def test_press_chat_undo_calls_telegram_chats(db, seeded):
    kb = ba.keyboard([], [{"n": 1, "kind": "chat", "ref": seeded["dis_event"]}])
    f = Fake()
    reply = ba.handle(_cq(f"m:1:c:{seeded['dis_event']}", kb), db_path=db, transport=f)
    assert reply == ba._t("undone")
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT enabled FROM tg_monitored_chats WHERE chat_id=-1002"
                            ).fetchone()[0] == 1
    # A second press: the journal refuses, the refusal is the callback text.
    reply2 = ba.handle(_cq(f"m:1:c:{seeded['dis_event']}", kb), db_path=db, transport=f)
    assert reply2 == ba._t("undo.already")


def test_press_errors_become_text(db, seeded):
    f = Fake()
    kb = ba.keyboard([], [{"n": 1, "kind": "chat", "ref": 1}])
    assert ba.handle(_cq("m:1:c:999999", kb), db_path=db, transport=f) == \
        ba._t("undo.notFound", id=999999)
    # Not the last event of that chat and kind -> UndoNotLast.
    telegram_chats.set_category(db, -1001, seeded["work"], source="ui", actor="ui")
    assert ba.handle(_cq(f"m:1:c:{seeded['cat_event']}", kb), db_path=db, transport=f) == \
        ba._t("undo.notLast")
    # Task: a missing event is a LookupError with the service's text.
    assert ba.handle(_cq("m:1:t:999999", kb), db_path=db, transport=f)
    assert not f.of("editMessageReplyMarkup")


def test_press_from_foreign_chat_is_refused(db, seeded):
    kb = ba.keyboard([], [{"n": 1, "kind": "task", "ref": seeded["task_event"]}])
    f = Fake()
    assert ba.handle(_cq(f"m:1:t:{seeded['task_event']}", kb, chat="555"), db_path=db,
                     transport=f) == ba._t("noAccess")
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT status FROM action_items WHERE id=11").fetchone()[0] == "done"


# ---------------------------------------------------------- pipe file

def test_pipe_file_loads_and_delivers_with_buttons(db, seeded):
    pipe = next(p for p in pipes.load_pipes() if p.name == "mcp-actions")
    assert pipe.schedule.expr == "0 9 * * *" and pipe.tools == ["mcp_actions"]
    assert pipe.budget_usd == 0 and pipe.skip_if_empty is True
    sent = []
    res = run_pipe(pipe, db_path=db, deliver=lambda t, **kw: sent.append((t, kw)), now=NOW,
                   buttons=True)
    assert res["status"] == "ok" and res["rendered_by"] == "plain"
    text, kw = sent[0]
    datas = [r[0]["callback_data"] for r in kw["reply_markup"]["inline_keyboard"]]
    assert datas and all(d.startswith("m:") for d in datas)
    assert "Надіслати Олені договір" in text


def test_texts_have_uk_and_en():
    from app.core.i18n_catalog.telegram import MESSAGES
    keys = [k for k in MESSAGES if k.startswith(("telegram.brief.mcp.", "telegram.btn.undo."))]
    assert len(keys) >= 10
    for k in keys:
        assert set(MESSAGES[k]) >= {"uk", "en"}
