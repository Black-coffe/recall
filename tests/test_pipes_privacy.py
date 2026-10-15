"""K4 story 02: фільтр приватного перед ботом труб."""
from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

import pytest

from app.db.migrations import init_database
from app.services import pipes, pipes_privacy
from app.services.pipes import parse_pipe, run_pipe
from app.services.pipes_privacy import MASK, filter_data, scrub_text


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "r.db")
    init_database(path)
    conn = sqlite3.connect(path)
    # «Юридичне» → підпроєкт «Суди»; 2 — «Особисте» з міграцій.
    conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES "
                 "(15, 'Юридичне', 'юридичне', 5)")
    conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES "
                 "(16, 'Суди', 'суди', 15)")
    for tid, cat in ((1, 1), (2, 2), (4, 16), (5, 5)):
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (?, 'file', 'запис', ?)", (tid, cat))
    conn.execute("INSERT INTO action_items (id, transcription_id, task) VALUES "
                 "(101, 4, 'Подати позов до суду')")
    conn.execute("INSERT INTO action_items (id, transcription_id, task) VALUES "
                 "(102, 5, 'Оновити кошторис')")
    conn.commit()
    conn.close()
    monkeypatch.delenv("RECALL_PRIVATE_CATEGORIES", raising=False)
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(tmp_path / "private.txt"))
    return path


def _words(tmp_path, *words):
    (tmp_path / "private.txt").write_text("\n".join(words), encoding="utf-8")


def _digest():
    return {"weekly_digest": {
        "this_week": [
            {"id": 1, "task": "Надіслати звіт", "transcription_id": 1},
            {"id": 2, "task": "Купити ліки мамі", "transcription_id": 2},
            {"id": 3, "task": "Підписати мирову", "transcription_id": 4},
        ],
        "dropped": [{"id": 101, "task": "Подати позов до суду"},
                    {"id": 102, "task": "Оновити кошторис"}],
        "stale_topics": [{"id": 9, "canonical_name": "Петренко", "type": "person"},
                         {"id": 10, "canonical_name": "Бюджет Q4", "type": "topic"}],
        "coverage": {"total": 5},
    }}


class TestFilterData:
    def test_private_directions_hidden_with_descendants(self, db):
        out, hidden = filter_data(_digest(), db_path=db)
        tasks = [i["task"] for i in out["weekly_digest"]["this_week"]]
        assert tasks == ["Надіслати звіт"]
        # Забута обіцянка без transcription_id — напрямок через action_items.
        assert [i["task"] for i in out["weekly_digest"]["dropped"]] == ["Оновити кошторис"]
        assert hidden == 3
        assert out["weekly_digest"]["coverage"] == {"total": 5}

    def test_stop_word_any_case_hides_item_without_source(self, db, tmp_path):
        _words(tmp_path, "ПЕТРЕНКО")
        out, hidden = filter_data(_digest(), db_path=db)
        names = [i["canonical_name"] for i in out["weekly_digest"]["stale_topics"]]
        assert names == ["Бюджет Q4"] and hidden == 4

    def test_db_failure_fails_closed(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            out, hidden = filter_data(_digest(), db_path=str(tmp_path / "нема" / "r.db"))
        wd = out["weekly_digest"]
        assert wd["this_week"] == [] and wd["dropped"] == []
        # Теми без джерела перевіряються лише стоп-словами — лишаються.
        assert len(wd["stale_topics"]) == 2 and hidden == 5
        assert "приховано" in caplog.text

    def test_ambiguous_namesake_hidden(self, db):
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES "
                     "(30, 'Особисте', 'особисте', 5)")
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (8, 'file', 'запис', 30)")
        conn.commit()
        conn.close()
        data = {"open_questions": {"questions": [{"question": "Коли?", "transcription_id": 8}]}}
        out, hidden = filter_data(data, db_path=db)
        assert out["open_questions"]["questions"] == [] and hidden == 1

    def test_no_file_no_words_and_unreadable_raises(self, db, tmp_path):
        assert filter_data({"x": {"l": [{"name": "Петренко"}]}}, db_path=db)[1] == 0
        (tmp_path / "private.txt").write_bytes(b"\xff\xfe\xfa")
        with pytest.raises(pipes_privacy.PrivacyError):
            filter_data({"x": {"l": []}}, db_path=db)


class TestScrub:
    @pytest.mark.parametrize("raw,gone", [
        ("IBAN UA213223130000026007233566001 для оплати", "UA21"),
        ("IBAN UA21 3223 1300 0002 6007 2335 6600 1 для оплати", "3223"),
        ("картка 4149 6293 1234 5678 до кінця", "4149"),
        ("картка 4149629312345678 до кінця", "4149"),
        ("дзвонити +380 67 123 45 67 зранку", "380"),
        ("дзвонити 067 123 45 67 зранку", "067"),
        ("пиши olena.k@example.com завтра", "olena"),
        ("бюджет 12 000 грн на місяць", "12 000"),
        ("інвестиція $5k на старт", "5k"),
        ("аванс €300 до пʼятниці", "300"),
        ("бюджет 5к USD на місяць", "5к"),
        # Ревʼю раунду 1: тисячі словом і російські валюти.
        ("позика 12 тисяч доларів до весни", "12"),
        ("взяли 13 тысяч гривен наличными", "13"),
        ("счёт 5000 долларов за сервер", "5000"),
        ("ещё 5 тыс. долларов сверху", "5 тыс"),
        ("оренда 30 тис. грн щомісяця", "30"),
        ("кредит 2 млн рублей", "2 млн"),
        ("раунд $1,500,000 закрито", "500"),
        ("дзвонити (067) 123-45-67 зранку", "123"),
    ])
    def test_masks(self, db, raw, gone):
        text, leaked = scrub_text(raw)
        assert MASK in text and gone not in text and leaked == []

    @pytest.mark.parametrize("raw", ["дедлайн 2026-10-16, задача №4512", "зустріч 16.10 о 9:00",
                                     "Q3 2026, 3 задачі"])
    def test_dates_and_ids_untouched(self, db, raw):
        assert scrub_text(raw) == (raw, [])

    def test_leaked_stop_word_reported(self, db, tmp_path):
        _words(tmp_path, "петренко")
        assert scrub_text("Зателефонувати Петренку")[1] == []      # морфології нема
        assert scrub_text("Зателефонувати ПЕТРЕНКО")[1] == ["петренко"]


def test_hidden_line_plural():
    assert pipes_privacy.hidden_line(1) == "🔒 1 пункт приховано, дивись у Recall"
    assert "3 пункти" in pipes_privacy.hidden_line(3)
    assert "11 пунктів" in pipes_privacy.hidden_line(11)


# ------------------------------------------------------- у run_pipe

def _pipe():
    return parse_pipe("---\nschedule: 0 9 * * 1\ntools: weekly_digest\nchannel: telegram\n"
                      "budget_usd: 1\n---\nПерекажи.", "звід")


class _Client:
    def __init__(self, text):
        self.text, self.calls = text, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text=self.text)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=10,
                                                     cache_read_input_tokens=0))


@pytest.fixture
def claude(monkeypatch):
    def make(text):
        c = _Client(text)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(pipes, "_get_client", lambda: c)
        return c
    return make


def test_hidden_items_never_reach_claude_and_line_added(db, claude, monkeypatch):
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda d, n: _digest()["weekly_digest"])
    c = claude("Звід: надіслати звіт.")
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=sent.append)
    prompt = c.calls[0]["messages"][0]["content"]
    assert "Купити ліки мамі" not in prompt and "Подати позов" not in prompt
    assert "Надіслати звіт" in prompt
    assert res["status"] == "ok"
    assert sent[0].endswith("🔒 3 пункти приховано, дивись у Recall")


def test_no_line_when_nothing_hidden(db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest",
                        lambda d, n: {"this_week": [{"task": "Надіслати звіт",
                                                     "transcription_id": 1}]})
    sent = []
    run_pipe(_pipe(), db_path=db, deliver=sent.append)
    assert "Надіслати звіт" in sent[0] and "🔒" not in sent[0]


def test_masks_applied_to_delivered_text(db, claude, monkeypatch):
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda d, n: {"this_week": []})
    claude("Оплатити 12 000 грн на UA213223130000026007233566001")
    sent = []
    run_pipe(_pipe(), db_path=db, deliver=sent.append)
    assert "12 000" not in sent[0] and "UA21" not in sent[0]


def test_stop_word_surviving_render_blocks_and_tells_owner(db, claude, monkeypatch, tmp_path):
    """Власник 15.10.2026: «Если фильтр приватного остановит брифинг, бот просто промолчит:
    не будет ни свода, ни сообщения об ошибке.» — брифінгу нема, повідомлення є."""
    _words(tmp_path, "Петренко")
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda d, n: {"this_week": []})
    claude("Нагадати ПЕТРЕНКО про зустріч")          # модель дописала від себе
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=sent.append)
    assert res["status"] == "blocked" and len(sent) == 1
    assert "не надіслано" in sent[0] and "фільтр приватного" in sent[0]
    assert "стоп-листа" in sent[0]
    assert "петренко" not in sent[0].casefold() and "зустріч" not in sent[0]
    row = pipes.list_runs(db)[0]
    assert row["status"] == "blocked" and "петренко" not in (row["error"] or "").casefold()


def test_unreadable_private_file_sends_only_notice(db, monkeypatch, tmp_path):
    (tmp_path / "private.txt").write_bytes(b"\xff\xfe\xfa")
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest",
                        lambda d, n: {"this_week": [{"task": "Надіслати звіт"}]})
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=sent.append)
    assert res["status"] == "blocked" and len(sent) == 1
    assert "private.txt" in sent[0] and "Надіслати звіт" not in sent[0]


def test_blocked_notice_failure_keeps_history(db, monkeypatch, tmp_path):
    (tmp_path / "private.txt").write_bytes(b"\xff\xfe\xfa")
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda d, n: {"this_week": []})

    def boom(text):
        raise ConnectionError("нема мережі")
    res = run_pipe(_pipe(), db_path=db, deliver=boom)
    assert res["status"] == "blocked"
    assert pipes.list_runs(db)[0]["status"] == "blocked"
