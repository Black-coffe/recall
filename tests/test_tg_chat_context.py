"""Тести живого профілю чату (Волна 4.5.2).

Локальна модель мокається — офлайн, без Ollama. Головне, що тут перевіряється:
тверді числа беруться з SQL і НЕ віддаються моделі на переписування, вигадані
посилання на нитки відкидаються, а поріг оновлення справді стримує виклики.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.db.migrations import init_database
from app.services import tg_chat_context as ctx


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _seed(path, *, chat_id=-100, title="Робочий чат", messages=None, threads=None):
    """threads=None — створити нитку за замовчуванням; threads=[] — жодної
    (порожній список НЕ те саме, що «не передали»)."""
    if threads is None:
        threads = [(1, "Бюджет", "open")]
    conn = sqlite3.connect(path)
    for tid, label, status in threads:
        conn.execute("INSERT INTO tg_threads (id, chat_id, label, status, msg_count, "
                     "last_date) VALUES (?, ?, ?, ?, 0, '2026-06-01T10:00:00+00:00')",
                     (tid, chat_id, label, status))
    # Продовжуємо нумерацію, а не починаємо з 1: у _seed можна дозаливати
    # повідомлення в уже засіяний чат, а (chat_id, message_id) унікальні.
    start = conn.execute("SELECT COALESCE(MAX(tg_message_id), 0) FROM transcriptions "
                         "WHERE tg_chat_id = ?", (chat_id,)).fetchone()[0] + 1
    for offset, (sender, text, thread) in enumerate(messages or []):
        i = start + offset
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "tg_chat_id, tg_chat_title, tg_message_id, tg_date, tg_sender, tg_thread_id) "
            "VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"[TG] {text[:20]}", text, chat_id, title, i,
             f"2026-06-{min(i, 28):02d}T10:00:00+00:00", sender, thread))
    for row in conn.execute("SELECT id FROM tg_threads WHERE chat_id = ?",
                            (chat_id,)).fetchall():
        conn.execute("UPDATE tg_threads SET msg_count = (SELECT COUNT(*) FROM "
                     "transcriptions WHERE tg_thread_id = ?) WHERE id = ?",
                     (row[0], row[0]))
    conn.commit()
    conn.close()


def _fake_model(monkeypatch, payload):
    from app.services import local_llm
    monkeypatch.setattr(local_llm, "generate_json", lambda *a, **k: {"data": payload})


def _model_down(monkeypatch):
    from app.services import local_llm

    def _boom(*a, **k):
        raise local_llm.LocalLLMError("нема Ollama")

    monkeypatch.setattr(local_llm, "generate_json", _boom)


# ============================================================
# Тверда частина
# ============================================================

def test_participants_counted_by_sql(db):
    _seed(db, messages=[("Адам", "перше", 1), ("Адам", "друге", 1), ("Юля", "третє", 1)])
    people = ctx.participants(db, -100)
    assert [(p["name"], p["messages"]) for p in people] == [("Адам", 2), ("Юля", 1)]
    assert people[0]["first_seen"] < people[0]["last_seen"]


def test_topics_come_from_threads_not_from_model(db, monkeypatch):
    _seed(db, messages=[("Адам", "про бюджет", 1)])
    _fake_model(monkeypatch, {"summary": "звід", "roles": []})
    ctx.refresh_chat(db, -100)
    got = ctx.get_context(db, -100)
    assert [t["label"] for t in got["topics"]] == ["Бюджет"]


def test_closed_threads_are_not_current_topics(db):
    _seed(db, messages=[("Адам", "стара тема", 1)],
          threads=[(1, "Закрита", "closed")])
    assert ctx.open_threads(db, -100) == []


def test_model_cannot_rewrite_hard_numbers(db, monkeypatch):
    """Вигадане число в профілі живої людини гірше за його відсутність."""
    _seed(db, messages=[("Адам", "перше", 1), ("Адам", "друге", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "roles": [{"name": "Адам", "role": "веде облік"}]})
    ctx.refresh_chat(db, -100)
    adam = ctx.get_context(db, -100)["participants"][0]
    assert adam["messages"] == 2, "число лишається з SQL"
    assert adam["role"] == "веде облік", "від моделі береться лише опис"


# ============================================================
# Гігієна відповіді моделі
# ============================================================

def test_invented_thread_ids_are_dropped(db, monkeypatch):
    """Посилання на нитку, якої не показували, веде в нікуди."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {
        "summary": "звід",
        "open_questions": [{"question": "коли зустріч?", "thread_id": 999}],
    })
    ctx.refresh_chat(db, -100)
    q = ctx.get_context(db, -100)["open_questions"][0]
    assert "thread_id" not in q
    assert q["question"] == "коли зустріч?"


def test_valid_thread_id_is_kept(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "decisions": [{"decision": "беремо", "thread_id": 1}]})
    ctx.refresh_chat(db, -100)
    assert ctx.get_context(db, -100)["decisions"][0]["thread_id"] == 1


def test_empty_strings_are_dropped(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "open_questions": [{"question": "   "}, {"question": "справжнє"}]})
    ctx.refresh_chat(db, -100)
    qs = ctx.get_context(db, -100)["open_questions"]
    assert [q["question"] for q in qs] == ["справжнє"]


def test_degradation_keeps_hard_part(db, monkeypatch):
    """Нема моделі — профіль усе одно є, але текстові поля порожні, і це видно."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _model_down(monkeypatch)
    res = ctx.refresh_chat(db, -100)
    assert res["degraded"] is True
    got = ctx.get_context(db, -100)
    assert got["summary"] is None
    assert got["model"] is None
    assert len(got["participants"]) == 1, "тверда частина лишається"
    assert len(got["topics"]) == 1


# ============================================================
# Поріг
# ============================================================

def test_first_time_needs_refresh(db):
    _seed(db, messages=[("Адам", "текст", 1)])
    assert ctx.needs_refresh(db, -100) is True


def test_no_refresh_right_after_update(db, monkeypatch):
    """Профіль НЕ перебудовується на кожне повідомлення — це рівно ті виклики,
    від яких Волна 0 поставила гард."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    assert ctx.needs_refresh(db, -100) is False


def test_refresh_after_enough_new_messages(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    _seed(db, messages=[("Юля", f"нове {i}", 1) for i in range(ctx.MIN_NEW_MESSAGES)],
          threads=[])
    assert ctx.needs_refresh(db, -100) is True


def test_refresh_after_max_age(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    later = datetime.now() + timedelta(hours=ctx.MAX_AGE_HOURS + 1)
    assert ctx.needs_refresh(db, -100, now=later) is True


def test_refresh_all_dry_run_writes_nothing(db):
    _seed(db, messages=[("Адам", "текст", 1)])
    res = ctx.refresh_all(db, dry_run=True)
    assert res["due"] == 1
    assert ctx.get_context(db, -100) is None


def test_refresh_is_upsert_not_duplicate(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "перший"})
    ctx.refresh_chat(db, -100)
    _fake_model(monkeypatch, {"summary": "другий"})
    ctx.refresh_chat(db, -100)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM tg_chat_context").fetchone()[0] == 1
    conn.close()
    assert ctx.get_context(db, -100)["summary"] == "другий"


def test_get_context_missing_chat(db):
    assert ctx.get_context(db, -999) is None


def test_failed_refresh_does_not_erase_existing_profile(db, monkeypatch):
    """Спіймано на живому прогоні: один таймаут 32B перетворив нормальне досьє
    на порожнє, бо upsert писав None поверх зібраного."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "зібраний звід",
                              "roles": [{"name": "Адам", "role": "веде облік"}],
                              "open_questions": [{"question": "коли?"}],
                              "decisions": [{"decision": "беремо"}]})
    ctx.refresh_chat(db, -100)

    _model_down(monkeypatch)
    res = ctx.refresh_chat(db, -100)
    assert res["degraded"] is True

    got = ctx.get_context(db, -100)
    assert got["summary"] == "зібраний звід", "звід має пережити відмову моделі"
    assert got["participants"][0]["role"] == "веде облік"
    assert [q["question"] for q in got["open_questions"]] == ["коли?"]
    assert [d["decision"] for d in got["decisions"]] == ["беремо"]
    assert got["model"] is not None, "текст є, просто не свіжий"


def test_failed_refresh_still_updates_hard_part(db, monkeypatch):
    """Тверда частина від моделі не залежить — її оновлюємо навіть при відмові."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    _seed(db, messages=[("Юля", "нове", 1)], threads=[])
    _model_down(monkeypatch)
    ctx.refresh_chat(db, -100)
    got = ctx.get_context(db, -100)
    assert {p["name"] for p in got["participants"]} == {"Адам", "Юля"}
    assert got["messages_seen"] == 2


def test_first_refresh_failure_leaves_empty_text(db, monkeypatch):
    """Попереднього досьє немає — нема чого зберігати, degraded чесно порожній."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _model_down(monkeypatch)
    ctx.refresh_chat(db, -100)
    got = ctx.get_context(db, -100)
    assert got["summary"] is None and got["model"] is None
    assert len(got["participants"]) == 1


def test_model_column_records_default_model(db, monkeypatch):
    """NULL у model має означати рівно одне: тексту немає. Раніше він писався
    і при успіху, якщо модель брали за замовчуванням."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    assert ctx.get_context(db, -100)["model"]


@pytest.mark.parametrize("junk", ["null", "None", "n/a", "—", "невідомо", "  "])
def test_placeholder_roles_are_treated_as_empty(db, monkeypatch, junk):
    """Спіймано на живому прогоні: 18 учасників отримали роль рядком "null" —
    перевірки на непорожність замало, у досьє це виглядало як справжня роль."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "roles": [{"name": "Адам", "role": junk}]})
    ctx.refresh_chat(db, -100)
    assert ctx.get_context(db, -100)["participants"][0]["role"] is None


def test_real_role_survives_cleaning(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "roles": [{"name": "Адам", "role": "узгоджує бюджет"}]})
    ctx.refresh_chat(db, -100)
    assert ctx.get_context(db, -100)["participants"][0]["role"] == "узгоджує бюджет"


# ============================================================
# Свіжість досьє в MCP-тулзі (правдивість, 12.08.2026)
# ============================================================

def test_tool_says_how_many_messages_missed_the_profile(db, monkeypatch):
    """«Оновлюється за порогом» — правда, з якою нічого не зробиш.

    Досьє віддавало `updated_at`, але не казало, скільки повідомлень прийшло
    ПІСЛЯ нього: відставання на два і на двісті виглядали однаково. Тулз
    рахує різницю числом, щоб читач бачив, наскільки картці можна вірити.
    """
    import mcp_server as m

    monkeypatch.setattr(m, "DB_PATH", db)
    _seed(db, messages=[("Адам", "перше", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)

    assert m.get_chat_context(-100)["messages_after_profile"] == 0

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "tg_chat_id, tg_message_id, tg_date, tg_sender, created_at) "
        "VALUES ('telegram', '[TG] пізніше', 'пізніше', -100, 9001, "
        "'2026-06-30T10:00:00+00:00', 'Адам', '2099-01-01 00:00:00')")
    conn.commit()
    conn.close()

    assert m.get_chat_context(-100)["messages_after_profile"] == 1


def test_tool_keeps_hint_when_profile_absent(db, monkeypatch):
    """Немає досьє — помилка з підказкою, а не порожній словник із нулем."""
    import mcp_server as m

    monkeypatch.setattr(m, "DB_PATH", db)
    got = m.get_chat_context(-777)

    assert "error" in got and "refresh" in got["hint"]
    assert "messages_after_profile" not in got
