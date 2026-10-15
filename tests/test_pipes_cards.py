"""K5 story 01: брифінг — HTML-картки, які складає код.

Власник 15.10.2026: «прям очень кеуча текста который не хочется читать»; «чтобы там была
четкая категория, направление»; «чтобы по ссылке можно было открыть сам recall»."""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from app.db.migrations import init_database
from app.services import pipes, pipes_cards, tg_bot
from app.services.pipes import parse_pipe, run_pipe

BASE = "http://127.0.0.1:5050"
TODAY = date(2026, 10, 19)
ROOTS = {101: "Альфа-фонд", 102: "Бета-проєкт", 103: "Гамма"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(tmp_path / "private.txt"))
    monkeypatch.delenv("RECALL_PRIVATE_CATEGORIES", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("RECALL_API_URL", BASE)
    monkeypatch.delenv("RECALL_PUBLIC_URL", raising=False)


@pytest.fixture
def db(tmp_path):
    """3 кореневі напрямки × 8 задач; у кожного кореня підкатегорія, половина задач — у ній."""
    path = str(tmp_path / "c.db")
    init_database(path)
    conn = sqlite3.connect(path)
    for cid, name in ROOTS.items():
        conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES (?, ?, ?, NULL)",
                     (cid, name, name.casefold()))
        conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES (?, ?, ?, ?)",
                     (cid + 100, f"{name} · підпроєкт", f"{name} · підпроєкт".casefold(), cid))
    tid = 0
    for cid in ROOTS:
        for i in range(8):
            tid += 1
            cat = cid + 100 if i % 2 else cid
            conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                         "VALUES (?, 'recording', ?, ?)", (tid, f"Зустріч {tid}", cat))
            conn.execute("INSERT INTO action_items (id, transcription_id, task) VALUES (?, ?, ?)",
                         (1000 + tid, tid, f"Задача {tid}"))
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name) "
                 "VALUES (99, 'recording', 'Без напрямку')")
    conn.execute("INSERT INTO action_items (id, transcription_id, task) VALUES (1099, 99, 'Сирота')")
    conn.commit()
    conn.close()
    return path


def _digest(n_overdue=0):
    items = [{"id": 1000 + t, "task": f"Задача {t}", "transcription_id": t, "owner": "Андрій",
              "due_date": f"2026-10-{20 + (t % 5):02d}", "meeting": f"Зустріч {t}"}
             for t in range(1, 25)]
    return {"this_week": items[n_overdue:], "overdue": items[:n_overdue],
            "no_date": [{"id": 1099, "task": "Сирота", "transcription_id": 99}],
            "dropped": [], "stale_topics": [], "corrections": [],
            "coverage": {"total": 30, "dated": 24}}


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.ok = [], True

    def handle_starttag(self, tag, attrs):
        self.ok = self.ok and tag in ("b", "i", "a")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.ok = False


def _balanced(text: str) -> bool:
    p = _Tags()
    p.feed(text)
    p.close()
    return p.ok and not p.stack


def _render(db, data, **kw):
    return pipes_cards.render("🗓 Звід тижня", data, db_path=db, base_url=BASE, today=TODAY,
                              words=kw.pop("words", []), **kw)


def _block(text, root):
    para = next(p for p in text.split("\n\n") if p.startswith(f"🏷 <b>{root}</b>"))
    return para


def test_blocks_by_root_five_cards_and_more_link(db):
    text, leaked = _render(db, {"weekly_digest": _digest()})
    assert leaked == []
    for cid, root in ROOTS.items():
        block = _block(text, root)
        assert block.count("\n• ") == 5
        assert f'<a href="{BASE}/tasks?category_id={cid}">ще 3 →</a>' in block
    assert "підпроєкт</b>" not in text                    # підкатегорія — не окремий блок
    assert "📁 Альфа-фонд · підпроєкт" in text        # але видна в картці
    assert "Сирота" in _block(text, pipes_cards.NO_DIR)
    assert text.index("🏷 <b>Без напрямку") > text.index("🏷 <b>Гамма")


def test_card_links_to_record_with_task(db):
    text, _ = _render(db, {"weekly_digest": _digest()})
    assert f'<a href="{BASE}/transcript/1?task=1001">Задача 1</a>' in text


def test_dropped_without_tid_gets_it_from_action_items(db):
    data = {"dropped_commitments": {"dropped": [
        {"id": 1009, "task": "Задача 9", "meeting_date": "2026-10-01"}]}}
    text, _ = _render(db, data)
    assert f'<a href="{BASE}/transcript/9?task=1009">Задача 9</a>' in text
    assert "🏷 <b>Бета-проєкт</b>" in text


def test_overdue_first_oldest_deadline_first(db):
    d = _digest()
    d["overdue"] = [{"id": 1002, "task": "Задача 2", "transcription_id": 2, "due_date": "2026-10-10"},
                    {"id": 1004, "task": "Задача 4", "transcription_id": 4, "due_date": "2026-10-01"}]
    d["this_week"] = [it for it in d["this_week"] if it["id"] not in (1002, 1004)]
    block = _block(_render(db, {"weekly_digest": d})[0], "Альфа-фонд")
    cards = block.split("\n• ")[1:]
    assert "Задача 4<" in cards[0] and "прострочено з 01.10" in cards[0]
    assert "Задача 2<" in cards[1]


def test_no_markdown_escaped_and_balanced(db):
    d = _digest()
    d["this_week"][0]["task"] = "**Важливо** <script> & __так__"
    d["this_week"][0]["owner"] = "О'Коннор <boss>"
    text, _ = _render(db, {"weekly_digest": d}, headline="Тиждень: 3 протерміновано")
    assert "<script>" not in text and "&lt;script&gt; &amp;" in text
    assert "&lt;boss&gt;" in text
    assert _balanced(text)
    assert not any(line.lstrip().startswith("#") for line in text.splitlines())


def test_clean_headline_strips_markdown():
    raw = "## Головне\n**3 протерміновано** у __Фонді__\n- третій рядок\n"
    assert pipes_cards.clean_headline(raw) == "Головне\n3 протерміновано у Фонді"


def test_masks_per_field_and_stop_word_reported(db):
    d = _digest()
    d["this_week"][0]["task"] = "Оплатити на UA213223130000026007233566001, тел +380 67 123 45 67"
    d["this_week"][1]["owner"] = "Петренко"
    text, leaked = _render(db, {"weekly_digest": d}, words=["петренко"])
    assert "UA21" not in text and "123 45 67" not in text and "•••" in text
    assert leaked == ["петренко"]


def test_mask_runs_before_truncation(db):
    d = _digest()
    d["this_week"][0]["task"] = "я" * (pipes_cards.TASK_LEN - 8) + " 4111 1111 1111 1111"
    text, _ = _render(db, {"weekly_digest": d})
    assert "4111" not in text


def test_questions_link_record_and_telegram(db):
    data = {"open_questions": {"questions": [
        {"transcription_id": 3, "question": "Коли буде договір?", "asked_by": "Олена",
         "chat": "Фонд", "silence_h": 26.4, "link": "https://t.me/c/1/2"},
        {"transcription_id": 5, "question": "А звіт?", "silence_h": 50}]}}
    text, _ = _render(db, data)
    assert f'<a href="{BASE}/transcript/3">Коли буде договір?</a>' in text
    assert '<a href="https://t.me/c/1/2">у Telegram</a>' in text
    assert text.index("А звіт?") < text.index("Коли буде договір?")   # довша тиша — першою


# --------------------------------------------------------------- run_pipe

def _pipe(tools="weekly_digest"):
    return parse_pipe(f"---\nschedule: 0 9 * * 1\ntools: {tools}\nchannel: telegram\n"
                      "budget_usd: 1\n---\nГоловне.", "звід")


class _Client:
    def __init__(self, text):
        self.text, self.calls = text, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text=self.text)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=10,
                                                     cache_read_input_tokens=0))


def test_without_claude_same_message_without_headline(db, monkeypatch):
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda d, n: _digest())
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=sent.append, now=datetime(2026, 10, 19, 9))
    assert res["rendered_by"] == "plain" and res["status"] == "ok"
    assert "💡" not in sent[0] and "🏷 <b>Альфа-фонд</b>" in sent[0]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    c = _Client("**Головне:** 24 задачі цього тижня")
    monkeypatch.setattr(pipes, "_get_client", lambda: c)
    sent2 = []
    res = run_pipe(_pipe(), db_path=db, deliver=sent2.append, now=datetime(2026, 10, 19, 9))
    assert res["rendered_by"] == "claude"
    assert "💡 Головне: 24 задачі цього тижня" in sent2[0] and "**" not in sent2[0]
    assert sent2[0].replace("\n\n💡 Головне: 24 задачі цього тижня", "") == sent[0]
    assert c.calls[0]["max_tokens"] <= pipes.HEADLINE_MAX_TOKENS


def test_stop_word_in_direction_name_blocks(db, monkeypatch, tmp_path):
    """Назву напрямку дає БД, а не пункт: filter_data її не бачить — ловить картка."""
    (tmp_path / "private.txt").write_text("гамма", encoding="utf-8")
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda db_, n: _digest())
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=sent.append)
    assert res["status"] == "blocked" and len(sent) == 1
    assert "гамма" not in sent[0].casefold() and "Задача" not in sent[0]


def test_notices_are_html_escaped(db, monkeypatch):
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda d, n: (_ for _ in ()).throw(
        RuntimeError("x")))
    sent = []
    p = parse_pipe("---\nschedule: 0 9 * * 1\ntools: weekly_digest\nchannel: telegram\n---\nГ.",
                   "a<b>&c")
    run_pipe(p, db_path=db, deliver=sent.append)
    assert "«a&lt;b&gt;&amp;c»" in sent[0]


def test_long_brief_split_between_blocks_tags_intact(db, monkeypatch):
    d = _digest()
    for it in d["this_week"]:
        it["task"] = f"{it['task']} " + "довгий опис " * 12
    big = "\n\n".join([_render(db, {"weekly_digest": d})[0]] * 4)
    parts = tg_bot.split_text(big)
    assert len(parts) > 1 and all(len(p) <= tg_bot.MAX_LEN for p in parts)
    assert all(_balanced(p) for p in parts)


def test_send_passes_parse_mode(monkeypatch):
    import json
    import urllib.parse
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:t")
    monkeypatch.setenv("TELEGRAM_BOT_CHAT_ID", "7")
    calls = []

    def transport(url, data):
        calls.append(urllib.parse.parse_qs(data.decode()))
        return json.dumps({"ok": True, "result": {}}).encode()
    tg_bot.send("<b>x</b>", parse_mode="HTML", transport=transport)
    tg_bot.send("x", parse_mode=None, transport=transport)
    assert calls[0]["parse_mode"] == ["HTML"] and "parse_mode" not in calls[1]


# ---------------------- посилання з телефона (story 03, власник 15.10.2026)

def test_public_url_used_for_every_recall_link(db, monkeypatch):
    """«Ссылки в новых карточках брифинга ведут на адрес Recall на этом компьютере, поэтому с телефона
    они не откроются.»"""
    public = "https://home-pc.example.ts.net"
    monkeypatch.setenv("RECALL_PUBLIC_URL", public + "/")
    assert pipes_cards.link_base() == public
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda d, n: _digest())
    sent = []
    run_pipe(_pipe(), db_path=db, deliver=sent.append)
    hrefs = re.findall(r'href="([^"]+)"', sent[0])
    assert hrefs and all(h.startswith(public + "/") for h in hrefs)
    assert "127.0.0.1" not in sent[0]


def test_public_url_empty_falls_back_to_api_url(monkeypatch):
    monkeypatch.setenv("RECALL_PUBLIC_URL", "")
    assert pipes_cards.link_base() == BASE


def test_items_without_id_are_not_deduped_away(db):
    d = _digest()
    d["no_date"] = [{"task": "Без id перша"}, {"task": "Без id друга"}]
    text, _ = _render(db, {"weekly_digest": d})
    assert "Без id перша" in text and "Без id друга" in text


def test_no_direction_block_has_more_link(db):
    d = _digest()
    d["this_week"], d["no_date"] = [], [{"id": 5000 + i, "task": f"Сирітка {i}"} for i in range(7)]
    block = _block(_render(db, {"weekly_digest": d})[0], pipes_cards.NO_DIR)
    assert f'<a href="{BASE}/tasks?category_id=none">ще 2 →</a>' in block


# --- K5 k5-task-buttons: номери і кнопки ------------------------------------------

def test_buttons_number_shown_tasks_in_order_with_footer(db):
    from app.services import brief_actions
    ids: list = []
    text, _ = _render(db, {"weekly_digest": _digest(n_overdue=2)}, buttons=ids)
    nums = [int(n) for n in re.findall(r"<b>(\d+)\.</b> ", text)]
    assert nums == list(range(1, len(ids) + 1)) and len(ids) == len(set(ids)) > 0
    # Кожен показаний номер — задача, яку видно в тексті (за посиланням з ?task=).
    for n, iid in enumerate(ids, 1):
        assert re.search(rf"<b>{n}\.</b> <a href=\"[^\"]*\?task={iid}\"", text)
    assert brief_actions.FOOTER in text and _balanced(text)
    kb = brief_actions.keyboard(ids)
    assert len(kb["inline_keyboard"]) == len(ids)
    assert sum(len(r) for r in kb["inline_keyboard"]) <= 100


def test_without_buttons_list_message_unchanged(db):
    from app.services import brief_actions
    text, _ = _render(db, {"weekly_digest": _digest()})
    assert brief_actions.FOOTER not in text and not re.search(r"<b>\d+\.</b> ", text)


def test_buttons_capped_at_telegram_limit(db):
    from app.services import brief_actions
    ids = list(range(1, 50))
    assert sum(len(r) for r in brief_actions.keyboard(ids)["inline_keyboard"]) <= 100


def test_run_pipe_with_buttons_passes_keyboard(db, monkeypatch):
    monkeypatch.setitem(pipes.TOOLS, "weekly_digest", lambda d, n: _digest())
    sent = []
    res = run_pipe(_pipe(), db_path=db, deliver=lambda t, **kw: sent.append((t, kw)),
                   now=datetime(2026, 10, 19, 9), buttons=True)
    assert res["status"] == "ok"
    text, kw = sent[0]
    rows = kw["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"] == "1 ✅" and rows[0][0]["callback_data"].startswith("a:1:")
    assert len(rows) == len(re.findall(r"<b>\d+\.</b> ", text))


# --- english-i18n-08: службові рядки брифінгу мовою налаштування ---------------

def test_en_render_translates_service_strings_keeps_data(db, monkeypatch):
    from app.services import brief_actions
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    ids: list = []
    text, leaked = _render(db, {"weekly_digest": _digest(n_overdue=2)}, buttons=ids, hidden=3)
    assert leaked == [] and _balanced(text)
    assert "📌 Tasks" in text and "3 more →" in text and "No category" in text
    assert "overdue since" in text and "3 items hidden, see Recall" in text
    assert brief_actions.footer("en") in text and "Buttons work" in text
    assert "ще 3" not in text and "Задачі" not in text and "Без напрямку" not in text
    assert "Альфа-фонд" in text and "Сирота" in text            # дані — як є
    assert pipes_cards.title_for(["weekly_digest"]) == "🗓 Weekly digest"
    kb = brief_actions.keyboard(ids)
    assert kb["inline_keyboard"][0][0]["callback_data"].startswith("a:1:")


@pytest.mark.parametrize("setting", ["auto", "uk"])
def test_auto_and_uk_render_in_ukrainian(db, monkeypatch, setting):
    monkeypatch.setenv("RECALL_UI_LANG", setting)
    text, _ = _render(db, {"weekly_digest": _digest()}, hidden=1)
    assert "📌 Задачі" in text and "ще 3 →" in text and "1 пункт приховано" in text


def test_en_masking_and_leaked_words_still_work(db, monkeypatch):
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    d = _digest()
    d["this_week"][0]["task"] = "Оплатити на UA213223130000026007233566001, тел +380 67 123 45 67"
    d["this_week"][1]["owner"] = "Петренко"
    text, leaked = _render(db, {"weekly_digest": d}, words=["петренко"])
    assert "UA21" not in text and "123 45 67" not in text and "•••" in text
    assert leaked == ["петренко"]
