"""K5 k5-task-buttons: натиск кнопок брифінгу → commitments з журналом → новий ряд клавіатури."""
from __future__ import annotations

import json
import urllib.parse
from datetime import date, timedelta

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import brief_actions as ba
from app.services import tg_bot

TOKEN = "123:секрет"
CHAT = "777"


class Fake:
    """Транспорт Bot API: пише виклики (метод, параметри), відповідає з ``replies[метод]``."""

    def __init__(self, replies=None, fail=()):
        self.calls, self.replies, self.fail = [], replies or {}, set(fail)

    def __call__(self, url, data):
        method = url.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in urllib.parse.parse_qs(data.decode()).items()} if data else {}
        self.calls.append((method, params))
        if method in self.fail:
            return json.dumps({"ok": False, "description": "Bad Request: query is too old"}).encode()
        reply = self.replies.get(method, {"ok": True, "result": True})
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else {"ok": True, "result": []}
        return json.dumps(reply).encode()

    def of(self, method):
        return [p for m, p in self.calls if m == method]


@pytest.fixture(autouse=True)
def _bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_CHAT_ID", CHAT)
    ba._KEYBOARDS.clear()
    yield
    ba._KEYBOARDS.clear()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "b.db")
    init_database(path)
    with get_db_connection(path) as conn:
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name) "
                     "VALUES (1, 'recording', 'Зустріч з Оленою')")
        for iid, task in ((11, "Надіслати Олені договір"), (12, "Перевірити звіт фонду")):
            conn.execute("INSERT INTO action_items (id, transcription_id, task, status) "
                         "VALUES (?, 1, ?, 'open')", (iid, task))
        conn.commit()
    return path


def _status(db, iid):
    with get_db_connection(db) as conn:
        r = conn.execute("SELECT status, due_date FROM action_items WHERE id=?", (iid,)).fetchone()
    return r["status"], r["due_date"]


def _cq(data, *, chat=CHAT, kb=None, message_id=5):
    kb = kb or ba.keyboard([11, 12])
    return {"id": "q1", "data": data, "from": {"id": int(CHAT), "username": "andrii"},
            "message": {"message_id": message_id, "chat": {"id": int(chat)}, "reply_markup": kb}}


def _rows(fake):
    return json.loads(fake.of("editMessageReplyMarkup")[-1]["reply_markup"])["inline_keyboard"]


def test_done_changes_status_logs_actor_and_replaces_only_that_row(db):
    f = Fake()
    reply = ba.handle(_cq("a:2:12:done"), db_path=db, transport=f)
    assert _status(db, 12)[0] == "done" and _status(db, 11)[0] == "open"
    rows = _rows(f)
    assert rows[0] == ba.task_row(1, 11)
    assert rows[1][0]["text"].startswith("2 ✅") and rows[1][1]["callback_data"].startswith("u:2:")
    assert f.of("answerCallbackQuery")[0]["text"] == reply
    with get_db_connection(db) as conn:
        ev = conn.execute("SELECT source, actor FROM action_item_events").fetchone()
    assert ev["source"] == "bot" and ev["actor"] == "tg:777 @andrii"


def test_undo_restores_task_and_three_buttons(db):
    f = Fake()
    ba.handle(_cq("a:1:11:cancelled"), db_path=db, transport=f)
    kb = {"inline_keyboard": _rows(f)}
    undo = kb["inline_keyboard"][0][1]["callback_data"]
    ba.handle(_cq(undo, kb=kb), db_path=db, transport=f)
    assert _status(db, 11) == ("open", None)
    assert _rows(f)[0] == ba.task_row(1, 11)


def test_postpone_shows_choice_then_sets_due_date(db):
    f = Fake()
    ba.handle(_cq("p:1:11"), db_path=db, transport=f)
    assert _status(db, 11) == ("open", None)            # вибір терміну ще нічого не міняє
    kb = {"inline_keyboard": _rows(f)}
    assert [b["text"] for b in kb["inline_keyboard"][0]][:3] == ["1 завтра", "+тиждень", "+місяць"]
    ba.handle(_cq("a:1:11:postpone:7", kb=kb), db_path=db, transport=f)
    assert _status(db, 11) == ("open", (date.today() + timedelta(days=7)).isoformat())
    assert _rows(f)[0][0]["text"].startswith("1 ⏭ до ")
    ba.handle(_cq("b:1:11", kb={"inline_keyboard": [ba.postpone_row(1, 11)]}, message_id=6),
              db_path=db, transport=f)
    assert _rows(f)[0] == ba.task_row(1, 11)


def test_foreign_chat_refused_and_nothing_changes(db):
    f = Fake()
    assert ba.handle(_cq("a:1:11:done", chat="999"), db_path=db, transport=f) == "Немає доступу"
    assert _status(db, 11)[0] == "open" and f.of("editMessageReplyMarkup") == []
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_item_events").fetchone()[0] == 0


def test_repeat_press_on_stale_undo_answers_without_breaking(db):
    f = Fake()
    ba.handle(_cq("a:1:11:done"), db_path=db, transport=f)
    undo = _rows(f)[0][1]["callback_data"]
    ba.handle(_cq(undo), db_path=db, transport=f)
    edits = len(f.of("editMessageReplyMarkup"))
    reply = ba.handle(_cq(undo), db_path=db, transport=f)   # той самий ↩ удруге
    assert "останню" in reply and _status(db, 11)[0] == "open"
    assert len(f.of("editMessageReplyMarkup")) == edits
    assert "невідома" in ba.handle(_cq("a:1:11:знищити"), db_path=db, transport=f)
    assert "не знайдено" in ba.handle(_cq("a:1:404:done"), db_path=db, transport=f)


def test_late_press_applied_even_if_answer_refused(db):
    """Натиск, зроблений поки ПК спав: Telegram уже не приймає answerCallbackQuery, зміна лишається."""
    f = Fake(fail={"answerCallbackQuery"})
    ba.handle(_cq("a:1:11:done"), db_path=db, transport=f)
    assert _status(db, 11)[0] == "done" and f.of("editMessageReplyMarkup")


def test_poll_once_handles_callbacks_and_advances_offset(db):
    f = Fake(replies={"getUpdates": [{"ok": True, "result": [
        {"update_id": 40, "callback_query": _cq("a:1:11:done")},
        {"update_id": 41, "message": {"text": "привіт"}},
    ]}]})
    p = ba.ButtonPoller(db_path=db, transport=f)
    assert p.poll_once(timeout=0) == 1 and p.offset == 42
    assert json.loads(f.of("getUpdates")[0]["allowed_updates"]) == ["callback_query"]
    p.poll_once(timeout=0)
    assert f.of("getUpdates")[1]["offset"] == "42"
    assert _status(db, 11)[0] == "done"


def test_loop_survives_network_error(db):
    calls = []

    def transport(url, data):
        calls.append(url)
        if len(calls) >= 2:
            p.stop()
        raise OSError("мережа лежить")

    p = ba.ButtonPoller(db_path=db, transport=transport, backoff=0)
    p._loop()                                # повертається після stop, не кидає
    assert len(calls) >= 2


def test_queued_presses_keep_each_others_rows(db):
    """Ревʼю раунду 1: натиски, що чекали, поки ПК спав, несуть ПОЧАТКОВУ клавіатуру.

    Другий натиск не має повернути ряд першого до трьох кнопок і загубити його ↩."""
    f = Fake()
    ba.handle(_cq("a:1:11:done"), db_path=db, transport=f)
    ba.handle(_cq("a:2:12:cancelled"), db_path=db, transport=f)
    rows = _rows(f)
    assert _status(db, 11)[0] == "done" and _status(db, 12)[0] == "cancelled"
    assert rows[0][0]["text"].startswith("1 ✅") and rows[0][1]["callback_data"].startswith("u:1:")
    assert rows[1][0]["text"].startswith("2 🚫") and rows[1][1]["callback_data"].startswith("u:2:")
    # Інше повідомлення — своя клавіатура, не спільна.
    ba.handle(_cq("p:1:11", message_id=9), db_path=db, transport=f)
    assert _rows(f)[1] == ba.task_row(2, 12)


def test_queued_presses_on_same_task_show_latest_with_working_undo(db):
    """Ревʼю раунду 2: ✅, потім 🚫 тієї ж задачі, обидва з ПОЧАТКОВОЮ клавіатурою.

    Ряд шукається за номером задачі — показує останню дію, і її ↩ справді скасовує."""
    f = Fake()
    ba.handle(_cq("a:1:11:done"), db_path=db, transport=f)
    ba.handle(_cq("a:1:11:cancelled"), db_path=db, transport=f)
    row = _rows(f)[0]
    assert _status(db, 11)[0] == "cancelled" and row[0]["text"].startswith("1 🚫")
    ba.handle(_cq(row[1]["callback_data"]), db_path=db, transport=f)
    assert _status(db, 11)[0] == "done" and _rows(f)[0] == ba.task_row(1, 11)


# --- english-i18n-08: кнопки й відповіді мовою налаштування ----------------------

def test_en_foreign_chat_refused_in_english(db, monkeypatch):
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    f = Fake()
    assert ba.handle(_cq("a:1:11:done", chat="999"), db_path=db, transport=f) == "No access"
    assert f.of("answerCallbackQuery")[0]["text"] == "No access"
    assert _status(db, 11)[0] == "open"


def test_en_row_after_done_and_postpone_labels(db, monkeypatch):
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    f = Fake()
    reply = ba.handle(_cq("a:2:12:done"), db_path=db, transport=f)
    assert reply == "✅ done" and _status(db, 12)[0] == "done"
    row = _rows(f)[1]
    assert row[0]["text"] == "2 ✅ done" and row[1]["text"] == "↩ undo"
    assert row[0]["callback_data"] == "x" and row[1]["callback_data"].startswith("u:2:")
    labels = [b["text"] for b in ba.postpone_row(1, 11)]
    assert labels[:3] == ["1 tomorrow", "+week", "+month"]
    assert all(len(b["text"].encode()) <= 64 for b in ba.postpone_row(1, 11))
    # callback_data однакові в обох мовах
    monkeypatch.setenv("RECALL_UI_LANG", "uk")
    assert [b["callback_data"] for b in ba.postpone_row(1, 11)] == [
        b["callback_data"] for b in ba.postpone_row(1, 11, "en")]
