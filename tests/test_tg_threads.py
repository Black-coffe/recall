"""Тести ниток розмови в Telegram-чатах (Волна 4.5).

Локальна модель мокається — тести офлайн, без Ollama і без GPU. Те, що тут
перевіряється, — саме логіка каскаду і його поведінка при відмовах, бо
розділяльної здатності у косинуса на цих даних немає (заміряно: reply-пари
0.828 проти випадкових 0.827), і вся правильність тримається на тому, як ми
поводимось із відповіддю моделі та без неї.
"""
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import tg_threads


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _msg(path, *, chat_id=-100, chat_title="Робочий чат", msg_id=1, date=None,
         sender="Автор", text="повідомлення", reply_to=None, vec=None):
    """TG-повідомлення + (опційно) чанк з вектором, як після ембедингу."""
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "tg_chat_id, tg_chat_title, tg_message_id, tg_date, tg_sender, tg_reply_to) "
        "VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"[TG] {chat_title}: {text[:20]}", text, chat_id, chat_title, msg_id,
         date or "2026-06-01T10:00:00+00:00", sender, reply_to))
    tid = cur.lastrowid
    if vec is not None:
        blob = np.asarray(vec, dtype=np.float32).tobytes()
        conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text, embedding) "
                     "VALUES (?, 0, ?, ?)", (tid, text, blob))
    conn.commit()
    conn.close()
    return tid


def _rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return out


# ============================================================
# Сплески
# ============================================================

def _at(minutes):
    return (datetime(2026, 6, 1, 10, 0) + timedelta(minutes=minutes)).isoformat()


def test_bursts_split_on_long_silence():
    msgs = [{"tg_date": _at(0)}, {"tg_date": _at(10)},
            {"tg_date": _at(10 + tg_threads.BURST_GAP_MIN + 1)}]
    bursts = tg_threads.segment_bursts(msgs)
    assert [len(b) for b in bursts] == [2, 1]


def test_bursts_keep_dense_conversation_together():
    msgs = [{"tg_date": _at(i * 5)} for i in range(6)]
    assert len(tg_threads.segment_bursts(msgs)) == 1


def test_bursts_survive_broken_dates():
    """Дата, яку не розібрати, не має рвати сплеск на друзки."""
    msgs = [{"tg_date": _at(0)}, {"tg_date": "не дата"}, {"tg_date": _at(5)}]
    assert len(tg_threads.segment_bursts(msgs)) == 1


def test_batches_respect_message_cap(monkeypatch):
    monkeypatch.setattr(tg_threads, "MAX_BATCH_MSGS", 3)
    burst = [{"transcript_text": "x"} for _ in range(7)]
    assert [len(b) for b in tg_threads._batches(burst)] == [3, 3, 1]


def test_batches_respect_char_cap(monkeypatch):
    monkeypatch.setattr(tg_threads, "MAX_BATCH_CHARS", 100)
    burst = [{"transcript_text": "я" * 80} for _ in range(4)]
    assert all(len(b) <= 2 for b in tg_threads._batches(burst))


# ============================================================
# Розбір відповіді моделі
# ============================================================

def _fake_llm(monkeypatch, payload):
    """Підмінити локальну модель. Патчимо сам модуль local_llm, бо split_batch
    імпортує його всередині функції."""
    from app.services import local_llm
    monkeypatch.setattr(local_llm, "generate_json",
                        lambda *a, **k: {"data": payload})


def _batch(n):
    return [{"tg_sender": "A", "tg_date": _at(i), "transcript_text": f"текст {i}"}
            for i in range(n)]


def test_split_groups_messages(monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Зустріч"},
                                        {"msgs": [3], "label": "Оплата"}]})
    got = tg_threads.split_batch("чат", _batch(3), [])
    assert [g["msgs"] for g in got] == [[1, 2], [3]]
    assert got[0]["label"] == "Зустріч"


def test_split_returns_none_when_model_down(monkeypatch):
    """Модель недоступна → None, щоб викликач чесно позначив деградацію,
    а не вигадав нитку."""
    from app.services import local_llm

    def _boom(*a, **k):
        raise local_llm.LocalLLMError("Ollama недоступний")

    monkeypatch.setattr(local_llm, "generate_json", _boom)
    assert tg_threads.split_batch("чат", _batch(2), []) is None


def test_split_ignores_invented_thread_ids(monkeypatch):
    """Модель охоче вигадує id ниток; беремо лише ті, що самі показали."""
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "x", "continues": 999}]})
    got = tg_threads.split_batch("чат", _batch(1), [{"id": 7, "label": "справжня"}])
    assert got[0]["continues"] is None


def test_split_accepts_known_thread_id(monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "x", "continues": 7}]})
    got = tg_threads.split_batch("чат", _batch(1), [{"id": 7, "label": "справжня"}])
    assert got[0]["continues"] == 7


def test_split_recovers_messages_model_forgot(monkeypatch):
    """Повідомлення, яке модель не згадала, не має зникнути з архіву.

    Куди саме воно потрапляє — див. test_forgotten_message_joins_nearest_group;
    тут перевіряється лише те, що жодне не загублено."""
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "тема"}]})
    got = tg_threads.split_batch("чат", _batch(3), [])
    covered = sorted(i for g in got for i in g["msgs"])
    assert covered == [1, 2, 3]
    assert any(g.get("orphan") or g.get("orphan_msgs") for g in got), \
        "відновлення має бути позначене як здогадка"


def test_split_drops_out_of_range_indexes(monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 99], "label": "тема"}]})
    got = tg_threads.split_batch("чат", _batch(2), [])
    assert all(all(1 <= i <= 2 for i in g["msgs"]) for g in got)


def test_split_never_assigns_message_twice(monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "a"},
                                        {"msgs": [2, 3], "label": "b"}]})
    got = tg_threads.split_batch("чат", _batch(3), [])
    covered = [i for g in got for i in g["msgs"]]
    assert sorted(covered) == [1, 2, 3]
    assert len(covered) == len(set(covered))


# ============================================================
# Каскад
# ============================================================

def test_backfill_dry_run_writes_nothing(db, monkeypatch):
    _msg(db, msg_id=1, date=_at(0))
    _msg(db, msg_id=2, date=_at(5))
    res = tg_threads.backfill(db, dry_run=True)
    assert res["messages"] == 2 and res["bursts"] == 1
    assert _rows(db, "SELECT * FROM tg_threads") == []


def test_backfill_groups_burst_by_model(db, monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Зустріч"},
                                        {"msgs": [3], "label": "Оплата"}]})
    for i in range(1, 4):
        _msg(db, msg_id=i, date=_at(i * 5), text=f"повідомлення {i}")
    tg_threads.backfill(db, dry_run=False)
    threads = _rows(db, "SELECT id, label, msg_count FROM tg_threads ORDER BY id")
    assert len(threads) == 2
    assert {t["msg_count"] for t in threads} == {2, 1}
    assert {t["label"] for t in threads} == {"Зустріч", "Оплата"}


def test_reply_to_overrides_model(db, monkeypatch):
    """tg_reply_to — знання самого Telegram, воно сильніше за здогадку моделі."""
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Перша"}]})
    _msg(db, msg_id=1, date=_at(0), text="питання")
    tg_threads.backfill(db, dry_run=False)
    first = _rows(db, "SELECT id FROM tg_threads")[0]["id"]

    # Друге повідомлення — відповідь на перше, але модель кладе його в нову тему.
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Зовсім інша"}]})
    _msg(db, msg_id=2, date=_at(5), text="відповідь", reply_to=1)
    _msg(db, msg_id=3, date=_at(6), text="ще")
    tg_threads.backfill(db, dry_run=False)

    got = _rows(db, "SELECT tg_thread_id, tg_thread_src FROM transcriptions "
                    "WHERE tg_message_id = 2")[0]
    assert got["tg_thread_id"] == first
    assert got["tg_thread_src"] == "reply"


def test_degradation_marks_source_not_silently(db, monkeypatch):
    """Без моделі сплеск лишається однією ниткою, але це ВИДНО в даних —
    інакше деградацію не відрізнити від рішення і не перерахувати потім."""
    from app.services import local_llm
    monkeypatch.setattr(local_llm, "generate_json",
                        lambda *a, **k: (_ for _ in ()).throw(local_llm.LocalLLMError("нема")))
    _msg(db, msg_id=1, date=_at(0))
    _msg(db, msg_id=2, date=_at(5))
    tg_threads.backfill(db, dry_run=False)
    srcs = {r["tg_thread_src"] for r in _rows(db, "SELECT tg_thread_src FROM transcriptions")}
    assert srcs == {"burst"}


def test_lonely_message_is_single_not_degradation(db):
    """Повідомлення, єдине у своєму сплеску, моделі не потребує — і не має
    виглядати як збій моделі."""
    _msg(db, msg_id=1, date=_at(0))
    tg_threads.backfill(db, dry_run=False, use_llm=False)
    assert _rows(db, "SELECT tg_thread_src FROM transcriptions")[0]["tg_thread_src"] == "single"


def test_backfill_is_idempotent(db, monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Тема"}]})
    _msg(db, msg_id=1, date=_at(0))
    _msg(db, msg_id=2, date=_at(5))
    tg_threads.backfill(db, dry_run=False)
    tg_threads.backfill(db, dry_run=False)
    assert len(_rows(db, "SELECT id FROM tg_threads")) == 1


def test_centroid_built_from_message_vectors(db, monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Тема"}]})
    _msg(db, msg_id=1, date=_at(0), vec=[1.0, 0.0])
    _msg(db, msg_id=2, date=_at(5), vec=[0.0, 1.0])
    tg_threads.backfill(db, dry_run=False)
    blob = _rows(db, "SELECT centroid FROM tg_threads")[0]["centroid"]
    got = np.frombuffer(blob, dtype=np.float32)
    assert pytest.approx(1.0, abs=1e-5) == float(np.linalg.norm(got))
    assert got[0] == pytest.approx(got[1], abs=1e-5)


def test_candidates_ranked_by_similarity_without_threshold(db, monkeypatch):
    """Косинус тут лише впорядковує кандидатів. Жодного абсолютного порога —
    на живих даних він необґрунтований (розділення +0.02σ)."""
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Далека"}]})
    _msg(db, msg_id=1, date=_at(0), vec=[0.0, 1.0])
    tg_threads.backfill(db, dry_run=False)
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Близька"}]})
    _msg(db, msg_id=2, date=_at(20), vec=[1.0, 0.0])
    tg_threads.backfill(db, dry_run=False)

    import sqlite3 as s
    conn = s.connect(db)
    conn.row_factory = s.Row
    cands = tg_threads._candidate_threads(
        conn, -100, _at(30), np.array([1.0, 0.0]))
    conn.close()
    near = [r["id"] for r in _rows(db, "SELECT id FROM tg_threads ORDER BY id")][-1]
    assert cands[0]["id"] == near, "найсхожіша нитка має бути першою"
    assert len(cands) == 2, "низька схожість не має ВІДКИДАТИ кандидата"


def test_lonely_thread_gets_label_without_model(db):
    """Сплеск з одного повідомлення моделі не показують — але нитка без назви
    марна як кандидат на продовження, тож назва береться з тексту."""
    _msg(db, msg_id=1, date=_at(0), text="Треба узгодити бюджет на вересень")
    tg_threads.backfill(db, dry_run=False, use_llm=False)
    label = _rows(db, "SELECT label FROM tg_threads")[0]["label"]
    assert label and label.startswith("Треба узгодити бюджет")


def test_fallback_label_skips_media_placeholders():
    """«[фото без тексту]» — службова позначка, а не назва теми."""
    msgs = [{"transcript_text": "[фото без тексту]"},
            {"transcript_text": "Ось рахунок на оплату"}]
    assert tg_threads._fallback_label(msgs) == "Ось рахунок на оплату"


# ============================================================
# Живий інжест
# ============================================================

def test_incoming_is_provisional(db):
    """На інжесті рішення попереднє: модель ще не питали."""
    tid = _msg(db, msg_id=1, date=_at(0))
    tg_threads.assign_incoming(db, tid)
    row = _rows(db, "SELECT tg_thread_id, tg_thread_src FROM transcriptions")[0]
    assert row["tg_thread_id"] is not None
    assert row["tg_thread_src"] == "pending"


def test_incoming_joins_ongoing_burst(db):
    t1 = _msg(db, msg_id=1, date=_at(0))
    t2 = _msg(db, msg_id=2, date=_at(10))
    tg_threads.assign_incoming(db, t1)
    tg_threads.assign_incoming(db, t2)
    ids = {r["tg_thread_id"] for r in _rows(db, "SELECT tg_thread_id FROM transcriptions")}
    assert len(ids) == 1


def test_incoming_starts_new_thread_after_silence(db):
    t1 = _msg(db, msg_id=1, date=_at(0))
    t2 = _msg(db, msg_id=2, date=_at(tg_threads.BURST_GAP_MIN + 30))
    tg_threads.assign_incoming(db, t1)
    tg_threads.assign_incoming(db, t2)
    ids = {r["tg_thread_id"] for r in _rows(db, "SELECT tg_thread_id FROM transcriptions")}
    assert len(ids) == 2


def test_incoming_follows_reply_across_silence(db):
    """Відповідь через добу все одно належить своїй нитці — це знає Telegram."""
    t1 = _msg(db, msg_id=1, date=_at(0))
    tg_threads.assign_incoming(db, t1)
    first = _rows(db, "SELECT tg_thread_id FROM transcriptions WHERE id = ?", (t1,))[0]
    t2 = _msg(db, msg_id=2, date=_at(60 * 30), reply_to=1)
    tg_threads.assign_incoming(db, t2)
    got = _rows(db, "SELECT tg_thread_id, tg_thread_src FROM transcriptions WHERE id = ?", (t2,))[0]
    assert got["tg_thread_id"] == first["tg_thread_id"]
    assert got["tg_thread_src"] == "reply"


def test_resettle_replaces_provisional_with_model_split(db, monkeypatch):
    for i in (1, 2):
        tid = _msg(db, msg_id=i, date=_at(i * 5), text=f"текст {i}")
        tg_threads.assign_incoming(db, tid)
    assert len({r["tg_thread_id"] for r in _rows(db, "SELECT tg_thread_id FROM transcriptions")}) == 1

    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Перша"},
                                        {"msgs": [2], "label": "Друга"}]})
    res = tg_threads.resettle_pending(db, now=datetime(2026, 6, 2))
    assert res["resettled"] == 2
    rows = _rows(db, "SELECT tg_thread_id, tg_thread_src FROM transcriptions")
    assert len({r["tg_thread_id"] for r in rows}) == 2
    assert {r["tg_thread_src"] for r in rows} == {"single"}


def test_resettle_leaves_fresh_burst_alone(db, monkeypatch):
    """Сплеск, який ще триває, чіпати не можна — розмова не договорила."""
    tid = _msg(db, msg_id=1, date=datetime.now().isoformat())
    tg_threads.assign_incoming(db, tid)
    res = tg_threads.resettle_pending(db)
    assert res["resettled"] == 0
    assert _rows(db, "SELECT tg_thread_src FROM transcriptions")[0]["tg_thread_src"] == "pending"


def test_resettle_does_not_touch_model_decisions(db, monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Тема"}]})
    _msg(db, msg_id=1, date=_at(0))
    _msg(db, msg_id=2, date=_at(5))
    tg_threads.backfill(db, dry_run=False)
    before = _rows(db, "SELECT tg_thread_id FROM transcriptions ORDER BY id")
    assert tg_threads.resettle_pending(db, now=datetime(2026, 6, 2))["resettled"] == 0
    assert _rows(db, "SELECT tg_thread_id FROM transcriptions ORDER BY id") == before


# ============================================================
# Закриття і зведення
# ============================================================

def test_close_idle_threads(db, monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Стара"}]})
    _msg(db, msg_id=1, date=_at(0))
    tg_threads.backfill(db, dry_run=False)
    closed = tg_threads.close_idle_threads(db, now=datetime(2027, 1, 1))
    assert closed == 1
    assert _rows(db, "SELECT status FROM tg_threads")[0]["status"] == "closed"


def test_closed_thread_is_not_offered_as_candidate(db, monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Стара"}]})
    _msg(db, msg_id=1, date=_at(0))
    tg_threads.backfill(db, dry_run=False)
    tg_threads.close_idle_threads(db, now=datetime(2027, 1, 1))

    import sqlite3 as s
    conn = s.connect(db)
    conn.row_factory = s.Row
    assert tg_threads._candidate_threads(conn, -100, _at(10), None) == []
    conn.close()


def test_stats_report_degradations(db, monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Тема"}]})
    _msg(db, msg_id=1, date=_at(0))
    _msg(db, msg_id=2, date=_at(5))
    tg_threads.backfill(db, dry_run=False)
    got = tg_threads.stats(db)
    assert got["threads"] == 1
    assert got["messages_assigned"] == 2
    assert got["by_source"].get("llm") == 2


# ============================================================
# Закриття за обсягом (дефект першого проходу)
# ============================================================

def test_thread_closes_at_volume_cap(db, monkeypatch):
    """Без стелі нитка росте, доки в чат пишуть частіше за THREAD_IDLE_DAYS:
    на першому проході ниток зібрали значна частина архіву, найбільша — повідомлень
    за два місяці, і всередині неї чотири різні теми. Це чат усередині чату."""
    monkeypatch.setattr(tg_threads, "THREAD_MAX_MSGS", 3)
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2, 3], "label": "Тема"}]})
    for i in (1, 2, 3):
        _msg(db, msg_id=i, date=_at(i))
    tg_threads.backfill(db, dry_run=False)
    assert _rows(db, "SELECT status FROM tg_threads")[0]["status"] == "closed"


def test_full_thread_is_not_offered_for_continuation(db, monkeypatch):
    monkeypatch.setattr(tg_threads, "THREAD_MAX_MSGS", 2)
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Повна"}]})
    _msg(db, msg_id=1, date=_at(0))
    _msg(db, msg_id=2, date=_at(5))
    tg_threads.backfill(db, dry_run=False)

    import sqlite3 as s
    conn = s.connect(db)
    conn.row_factory = s.Row
    assert tg_threads._candidate_threads(conn, -100, _at(10), None) == []
    conn.close()


def test_new_conversation_starts_fresh_thread_after_cap(db, monkeypatch):
    """Наступна розмова йде окремою ниткою, а не доліплюється до переповненої."""
    monkeypatch.setattr(tg_threads, "THREAD_MAX_MSGS", 2)
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Перша"}]})
    _msg(db, msg_id=1, date=_at(0))
    _msg(db, msg_id=2, date=_at(5))
    tg_threads.backfill(db, dry_run=False)
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Друга"}]})
    _msg(db, msg_id=3, date=_at(10))
    tg_threads.backfill(db, dry_run=False)
    assert len(_rows(db, "SELECT id FROM tg_threads")) == 2


def test_incoming_does_not_extend_full_thread(db, monkeypatch):
    monkeypatch.setattr(tg_threads, "THREAD_MAX_MSGS", 1)
    t1 = _msg(db, msg_id=1, date=_at(0))
    tg_threads.assign_incoming(db, t1)
    t2 = _msg(db, msg_id=2, date=_at(5))
    tg_threads.assign_incoming(db, t2)
    ids = {r["tg_thread_id"] for r in _rows(db, "SELECT tg_thread_id FROM transcriptions")}
    assert len(ids) == 2


# ============================================================
# Промахи моделі кладемо до сусідів, а не в самотні нитки
# ============================================================

def test_forgotten_message_joins_nearest_group(monkeypatch):
    """Окрема нитка на кожен промах — це гарантовано нитка без контексту
    (повідомлень, 10.дрібна частка архіву, на першому проході)."""
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Перша"},
                                        {"msgs": [4], "label": "Друга"}]})
    got = tg_threads.split_batch("чат", _batch(4), [])
    covered = sorted(i for g in got for i in g["msgs"])
    assert covered == [1, 2, 3, 4]
    host = [g for g in got if 3 in g["msgs"]][0]
    assert host["label"] == "Перша", "3 ближче до групи [1,2], ніж до [4]"


def test_forgotten_message_is_marked_as_guess(monkeypatch):
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Тема"}]})
    got = tg_threads.split_batch("чат", _batch(2), [])
    assert got[0]["orphan_msgs"] == [2]


def test_guess_and_decision_get_different_provenance(db, monkeypatch):
    """Один src на всю групу зробив би здогадку невідрізненною від рішення."""
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2], "label": "Тема"}]})
    for i in (1, 2, 3):
        _msg(db, msg_id=i, date=_at(i))
    tg_threads.backfill(db, dry_run=False)
    got = {r["tg_message_id"]: r["tg_thread_src"] for r in
           _rows(db, "SELECT tg_message_id, tg_thread_src FROM transcriptions")}
    assert got[1] == got[2] == "llm"
    assert got[3] == "orphan"
    ids = {r["tg_thread_id"] for r in _rows(db, "SELECT tg_thread_id FROM transcriptions")}
    assert len(ids) == 1, "промах приєднався до сусідів, а не завів свою нитку"


def test_orphan_alone_in_batch_still_gets_thread(monkeypatch):
    """Модель не повернула жодної групи — приєднуватись нема до чого."""
    _fake_llm(monkeypatch, {"threads": [{"msgs": [], "label": "порожня"}]})
    got = tg_threads.split_batch("чат", _batch(2), [])
    assert got is not None
    assert sorted(i for g in got for i in g["msgs"]) == [1, 2]


def test_volume_cap_never_splits_one_conversation(db, monkeypatch):
    """Стеля мʼяка навмисно: група, яку модель визнала однією темою, заходить
    цілком. Розірвати звʼязну розмову заради круглого числа гірше за
    накопичення — сенс нитки в тому, що вона ціла."""
    monkeypatch.setattr(tg_threads, "THREAD_MAX_MSGS", 2)
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1, 2, 3, 4], "label": "Одна розмова"}]})
    for i in range(1, 5):
        _msg(db, msg_id=i, date=_at(i))
    tg_threads.backfill(db, dry_run=False)
    threads = _rows(db, "SELECT msg_count, status FROM tg_threads")
    assert len(threads) == 1
    assert threads[0]["msg_count"] == 4, "розмова не розрізана"
    assert threads[0]["status"] == "closed", "але нитка закрита — далі не росте"


def test_stale_thread_is_not_offered_even_if_open(db, monkeypatch):
    """Вікно кандидатів — головний регулятор злипання, а не судження моделі:
    на прогоні з вікном 21 день максимальний розрив усередині нитки упирався
    рівно в 21 при медіані 2.8. Модель бере зі списку те, що їй показали."""
    _fake_llm(monkeypatch, {"threads": [{"msgs": [1], "label": "Давня"}]})
    _msg(db, msg_id=1, date=_at(0))
    tg_threads.backfill(db, dry_run=False)

    import sqlite3 as s
    conn = s.connect(db)
    conn.row_factory = s.Row
    far = _at(60 * 24 * (tg_threads.THREAD_IDLE_DAYS + 1))
    assert tg_threads._candidate_threads(conn, -100, far, None) == []
    near = _at(60 * 12)
    assert len(tg_threads._candidate_threads(conn, -100, near, None)) == 1
    conn.close()
