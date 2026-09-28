"""Спека directions-projects-tags, історія 04: осі `tags`/`since`/`until` і дерево
напрямків у MCP.

Що саме тут доводиться і чому:

* **Розкрив напрямку.** `category_id` напрямку мусить захоплювати його проєкти, а
  `category_id` проєкта — лише себе. Це не косметика видачі, а межа приватності
  зрізу: «покажи Фонд» зобовʼязане показати Акмекорп, «покажи Акмекорп» — не
  зобовʼязане показати сусідній проєкт.
* **Невідомий тег.** Фільтр, якого нема у словнику, дає порожню видачу. Без поля
  `note` агент прочитає її як «в архіві такого нема» і збреше власнику — тому
  порожнеча з нерозпізнаним тегом мусить бути підписана.
* **Проксі не переосмислюють параметри.** `search_archive`/`ask_archive`/`research_*`
  лише перекладають їх у query/JSON; HTTP-сторона зʼявляється в історіях 02/07,
  тому тут мокається `_api`/`_sse_collect` — тест ловить саме втрату параметра при
  переупакуванні.
* **Докстринги — дослівно з ADR-023 §C.** Очікуваний текст не переписаний у тест
  руками, а вичитується з самого ADR: інакше «дослівно» протухає першою ж правкою
  ADR і тест продовжує зеленіти на старому формулюванні.

Кириличні фікстури — памʼятка `plan-dictated-sql-ignored-own-memory` (SQLite
`lower()` не згортає кирилицю, тому будь-який регістр ловиться лише через
`name_norm`).

Офлайн: тимчасова SQLite через `init_database`, без мережі й важких моделей.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import mcp_server as m
from app.db.migrations import init_database
from app.services import categories

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADR = _REPO_ROOT / "docs" / "adr" / "023-directions-projects-tags-taxonomy.md"

# 9 тулзів контракту C6: 8 із наявним `category_id` + `grep_archive`, якому його
# цією ж історією додали.
_FILTER_TOOLS = (
    "search_archive", "list_recent", "list_action_items", "ask_archive",
    "research_export", "research_preview", "research_summary", "search_history",
    "grep_archive",
)


def _squash(text: str) -> str:
    """Порівнюємо без урахування переносів: докстринг загорнутий по 79 колонок,
    ADR — по своїх, а «дослівно» стосується слів, не місця розриву рядка."""
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "taxonomy.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


@pytest.fixture()
def archive(db):
    """Дерево «Фонд Робота → (Акмекорп, DOM)» + «Особисте» і 4 записи з тегами.

    Дати навмисно різні й сусідні (10/15/20 вересня), щоб межі `since`/`until`
    можна було перевірити на включність, а не «приблизно в тому місяці».
    """
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    def _cat(name, parent=None, color=None, icon=None, sort_order=0):
        """`Фонд Робота` і `Особисте` вже засіяні міграцією — беремо наявні
        (створення впало б на UNIQUE name_norm)."""
        key = categories.norm_name(name)
        row = conn.execute("SELECT id FROM categories WHERE name_norm = ?", (key,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO categories (name, name_norm, color, icon, sort_order, parent_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, key, color, icon, sort_order, parent))
        return cur.lastrowid

    fond = _cat("Фонд Робота")
    bar = _cat("Акмекорп", parent=fond, color="#c0392b", icon="flag", sort_order=1)
    dom = _cat("DOM", parent=fond, sort_order=2)
    personal = _cat("Особисте")

    def _rec(name, cid, meeting_date, text, tags=()):
        cur = conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "category_id, meeting_date) VALUES ('file', ?, ?, ?, ?)",
            (name, text, cid, meeting_date))
        tid = cur.lastrowid
        conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text) "
                     "VALUES (?, 0, ?)", (tid, text))
        for tag in tags:
            key = categories.norm_name(tag)
            row = conn.execute("SELECT id FROM tags WHERE name_norm = ?", (key,)).fetchone()
            tag_id = row["id"] if row else conn.execute(
                "INSERT INTO tags (name, name_norm) VALUES (?, ?)", (tag, key)).lastrowid
            conn.execute("INSERT INTO record_tags (transcription_id, tag_id) VALUES (?, ?)",
                         (tid, tag_id))
        return tid

    ids = {
        "fond": _rec("Правління фонду.mp3", fond, "2026-09-10",
                     "Обговорили бюджет і кодове слово ВОДОСПАД", tags=["AI"]),
        "bar": _rec("Акмекорп щотижнева.mp3", bar, "2026-09-15",
                    "Домовились про раунд, кодове слово ВОДОСПАД", tags=["інвестори"]),
        "dom": _rec("Nova Dance синк.mp3", dom, "2026-09-20",
                    "Репетиція і кодове слово ВОДОСПАД", tags=["AI", "інвестори"]),
        "personal": _rec("Особисте нагадування.mp3", personal, "2026-08-01",
                         "Кодове слово ВОДОСПАД у щоденнику"),
    }

    for key, task in (("fond", "Підготувати бюджет фонду"),
                      ("bar", "Надіслати терміни раунду"),
                      ("personal", "Записатись до лікаря")):
        conn.execute("INSERT INTO action_items (transcription_id, task, due_date, status) "
                     "VALUES (?, ?, NULL, 'open')", (ids[key], task))
    conn.commit()
    conn.close()
    return {"cat": {"fond": fond, "bar": bar, "dom": dom, "personal": personal},
            "rec": ids}


def _ids(res) -> set:
    items = res["items"] if isinstance(res, dict) else res
    return {r["id"] for r in items}


# ============================================================
# list_recent: напрямок/проєкт, теги, період
# ============================================================

def test_direction_includes_its_projects(archive):
    """`category_id` напрямку розкривається в напрямок + усі його проєкти."""
    got = _ids(m.list_recent(category_id=archive["cat"]["fond"]))
    assert got == {archive["rec"]["fond"], archive["rec"]["bar"], archive["rec"]["dom"]}
    assert archive["rec"]["personal"] not in got


def test_project_includes_only_itself(archive):
    """`category_id` проєкта не захоплює ні батька, ні сусідній проєкт."""
    assert _ids(m.list_recent(category_id=archive["cat"]["bar"])) == {archive["rec"]["bar"]}


def test_tags_are_or(archive):
    """Кілька тегів — «будь-який із них», не перетин."""
    assert _ids(m.list_recent(tags="AI, інвестори")) == {
        archive["rec"]["fond"], archive["rec"]["bar"], archive["rec"]["dom"]}


def test_tag_matches_any_case(archive):
    """`name_norm` (casefold) — інакше «ІНВЕСТОРИ» не знайшло б «інвестори»."""
    assert _ids(m.list_recent(tags="ІНВЕСТОРИ")) == {
        archive["rec"]["bar"], archive["rec"]["dom"]}


def test_tags_and_category_intersect(archive):
    """Тег разом із напрямком — перетин: «тег AI лише в цьому проєкті»."""
    assert _ids(m.list_recent(tags="AI", category_id=archive["cat"]["dom"])) == {
        archive["rec"]["dom"]}
    assert _ids(m.list_recent(tags="AI", category_id=archive["cat"]["bar"])) == set()


def test_since_until_bounds_are_inclusive(archive):
    """Обидві межі включно — один день як вікно мусить знаходити свій запис."""
    assert _ids(m.list_recent(since="2026-09-15", until="2026-09-15")) == {
        archive["rec"]["bar"]}
    assert _ids(m.list_recent(since="2026-09-10", until="2026-09-20")) == {
        archive["rec"]["fond"], archive["rec"]["bar"], archive["rec"]["dom"]}
    assert _ids(m.list_recent(since="2026-09-16")) == {archive["rec"]["dom"]}


def test_invalid_date_is_an_answer_not_an_exception(archive):
    """Крива дата від агента — поле `error`, а не 500 у stdio-каналі."""
    res = m.list_recent(since="вчора")
    assert isinstance(res, dict) and "error" in res


def test_unknown_tag_returns_empty_with_note(archive):
    """Порожньо через нерозпізнаний тег мусить бути підписано — інакше агент
    прочитає це як «такого в архіві нема»."""
    res = m.list_recent(tags="Бухгалтерія")
    assert res["items"] == []
    assert "тег не знайдено" in res["note"] and "Бухгалтерія" in res["note"]


def test_known_tag_has_no_note(archive):
    """Звичайна видача — той самий конверт, `note` порожній."""
    res = m.list_recent(tags="AI")
    assert isinstance(res, dict) and "items" in res
    assert res["note"] is None


# ============================================================
# list_action_items — ті самі осі через commitments
# ============================================================

def test_action_items_filtered_by_tag(archive):
    """Задачі фільтруються за тегом ЗАПИСУ, з якого їх витягли."""
    res = m.list_action_items(window="all", tags="AI")
    assert [i["task"] for i in res["items"]] == ["Підготувати бюджет фонду"]


def test_action_items_direction_includes_projects(archive):
    """Напрямок у задачах розкривається так само, як у записах."""
    res = m.list_action_items(window="all", category_id=archive["cat"]["fond"])
    assert {i["task"] for i in res["items"]} == {
        "Підготувати бюджет фонду", "Надіслати терміни раунду"}


def test_action_items_window_counter_uses_same_filter(archive):
    """`window_matched` рахує ТУ САМУ множину, що й видача (інакше «показано 1
    із 3» при одному наявному)."""
    res = m.list_action_items(window="all", tags="AI")
    assert res["coverage"]["window_matched"] == len(res["items"]) == 1
    assert res["coverage"]["truncated"] is False


def test_action_items_period(archive):
    """`since`/`until` — по даті зустрічі запису, не по даті створення задачі."""
    res = m.list_action_items(window="all", since="2026-09-12")
    assert [i["task"] for i in res["items"]] == ["Надіслати терміни раунду"]


def test_action_items_unknown_tag_notes(archive):
    res = m.list_action_items(window="all", tags="Бухгалтерія")
    assert res["items"] == []
    assert "тег не знайдено" in res["note"]


def test_action_items_invalid_date(archive):
    assert "error" in m.list_action_items(window="all", since="2026-13-40")


def test_action_items_coverage_denominator_matches_tag_filter(archive):
    """`coverage.total` рахує ТУ САМУ множину, що й `items` (ремонт п.1 раунду
    3): до цього знаменник ігнорував `tags`, тож `list_action_items(tags="AI")`
    віддавав відфільтровану видачу поруч з архівним `coverage.total`."""
    res = m.list_action_items(window="all", tags="AI")
    assert res["coverage"]["total"] == len(res["items"]) == 1


def test_action_items_coverage_denominator_unchanged_without_filters(archive):
    """Без `tags`/`since`/`until` знаменник лишається тим самим, що й до
    історії — усі 3 живі задачі фікстури."""
    res = m.list_action_items(window="all")
    assert res["coverage"]["total"] == 3


# ============================================================
# grep_archive — фільтр до сканування
# ============================================================

def test_grep_scoped_to_direction(archive):
    res = m.grep_archive("ВОДОСПАД", category_id=archive["cat"]["fond"])
    assert {h["transcription_id"] for h in res["matches"]} == {
        archive["rec"]["fond"], archive["rec"]["bar"], archive["rec"]["dom"]}


def test_grep_direction_and_tag_intersect(archive):
    res = m.grep_archive("ВОДОСПАД", category_id=archive["cat"]["fond"], tags="AI")
    assert {h["transcription_id"] for h in res["matches"]} == {
        archive["rec"]["fond"], archive["rec"]["dom"]}


def test_grep_unknown_tag_notes(archive):
    res = m.grep_archive("ВОДОСПАД", tags="Бухгалтерія")
    assert res["matches"] == []
    assert "тег не знайдено" in res["note"]


# ============================================================
# list_categories — дерево, а не плаский список
# ============================================================

def test_list_categories_returns_tree_in_order(archive):
    """Порядок — напрямок, одразу за ним його проєкти; поля color/icon більше
    не губляться (стара версія тулза їх не вибирала взагалі)."""
    tree = m.list_categories()
    by_name = {c["name"]: c for c in tree}
    names = [c["name"] for c in tree]
    assert names.index("Фонд Робота") + 1 == names.index("Акмекорп")
    assert names.index("Акмекорп") + 1 == names.index("DOM")

    fond = by_name["Фонд Робота"]
    assert fond["level"] == "direction" and fond["parent_id"] is None
    assert fond["color"] == "#1c7cf2" and fond["icon"] == "fa-building-columns"
    assert fond["items"] == 1 and fond["items_total"] == 3

    bar = by_name["Акмекорп"]
    assert bar["level"] == "project" and bar["parent_id"] == archive["cat"]["fond"]
    assert bar["items"] == 1 and bar["items_total"] == 1


def test_about_draws_the_tree(archive):
    """Довідка, з якої агент починає, мусить показувати дерево — плаский перелік
    назв не давав зрозуміти, який id що захоплює."""
    text = m.about()
    assert "Фонд Робота (3) → Акмекорп (1), DOM (1)" in text
    assert "Особисте (1)" in text


def test_about_still_reports_35_tools(archive):
    """Історія не додає і не прибирає тулзів (C6)."""
    assert len(m._registered_tool_names()) == 35
    assert "35 тулзів" in m.about()


# ============================================================
# Проксі: параметри доїжджають без переосмислення
# ============================================================

@pytest.fixture()
def api_calls(monkeypatch):
    calls: list = []

    def fake_api(method, path, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        return {"ok": True}

    def fake_sse(path, body, **kwargs):
        calls.append({"method": "SSE", "path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(m, "_api", fake_api)
    monkeypatch.setattr(m, "_sse_collect", fake_sse)
    return calls


def test_search_archive_passes_axes_as_query(api_calls):
    m.search_archive("бюджет", tags="AI, інвестори", since="2026-09-01",
                     until="2026-09-30", category_id=7)
    params = api_calls[0]["params"]
    assert params["tags"] == "AI, інвестори"
    assert params["since"] == "2026-09-01" and params["until"] == "2026-09-30"
    assert params["category_id"] == 7


def test_search_archive_omits_unset_axes(api_calls):
    m.search_archive("бюджет")
    params = api_calls[0]["params"]
    assert "tags" not in params and "since" not in params and "until" not in params


def test_ask_archive_passes_axes_as_json(api_calls):
    m.ask_archive("що вирішили?", tags="AI", since="2026-09-01", until="2026-09-30")
    body = api_calls[0]["body"]
    assert body["tags"] == "AI"
    assert body["since"] == "2026-09-01" and body["until"] == "2026-09-30"


def test_ask_archive_omits_unset_axes(api_calls):
    m.ask_archive("що вирішили?")
    body = api_calls[0]["body"]
    assert "tags" not in body and "since" not in body and "until" not in body


def test_research_preview_passes_axes_as_query(api_calls):
    m.research_preview(q="Акмекорп", tags="AI", since="2026-09-01", until="2026-09-30")
    params = api_calls[0]["params"]
    assert params["tags"] == "AI"
    assert params["since"] == "2026-09-01" and params["until"] == "2026-09-30"


def test_research_summary_passes_axes_in_body(api_calls):
    m.research_summary(q="Акмекорп", tags="AI", since="2026-09-01", until="2026-09-30")
    body = api_calls[0]["body"]
    assert body["tags"] == "AI"
    assert body["since"] == "2026-09-01" and body["until"] == "2026-09-30"


def test_research_summary_omits_unset_axes(api_calls):
    m.research_summary(q="Акмекорп")
    body = api_calls[0]["body"]
    assert "tags" not in body and "since" not in body and "until" not in body


def test_search_history_passes_axes_as_query(api_calls):
    m.search_history(search="раунд", tags="AI", since="2026-09-01", until="2026-09-30",
                     category_id=7)
    params = api_calls[0]["params"]
    assert params["tags"] == "AI"
    assert params["since"] == "2026-09-01" and params["until"] == "2026-09-30"
    assert params["category_id"] == 7


def test_search_history_omits_unset_axes(api_calls):
    m.search_history(search="раунд")
    params = api_calls[0]["params"]
    assert "tags" not in params and "since" not in params and "until" not in params


# ============================================================
# research_export — direct-DB, ті самі осі
# ============================================================

def test_research_export_scoped_by_tag(archive):
    md = m.research_export("ВОДОСПАД", tags="інвестори")
    assert "Акмекорп щотижнева.mp3" in md
    assert "Особисте нагадування.mp3" not in md


def test_research_export_unknown_tag_notes(archive):
    md = m.research_export("ВОДОСПАД", tags="Бухгалтерія")
    assert md.startswith("тег не знайдено: Бухгалтерія")


def test_research_export_invalid_date(archive):
    assert "Помилка фільтра" in m.research_export("ВОДОСПАД", since="вчора")


# ============================================================
# Докстринги — три абзаци ADR §C дослівно
# ============================================================

def _adr_paragraphs() -> list[str]:
    """Блок-цитата під «Текст для docstring … (дослівно)» — джерело істини.

    Тест читає ADR, а не копію тексту в тесті: копія розійшлася б із ADR мовчки,
    і «дослівно» перетворилось би на «дослівно станом на день написання тесту».
    """
    text = _ADR.read_text(encoding="utf-8")
    head = text.index("Текст для docstring")
    # Абзац починається з рядка `` `xxx` — ``; решта рядків блок-цитати — його
    # продовження. Різати вже склеєний текст регуляркою не можна: третій абзац
    # сам згадує `category_id` і розпався б навпіл.
    paragraphs: list[str] = []
    for line in text[head:].splitlines()[1:]:
        stripped = line.strip()
        if not stripped.startswith(">"):
            if paragraphs:
                break
            continue
        body = stripped[1:].strip()
        if re.match(r"^`(?:category_id|project|tags)` —", body):
            paragraphs.append(body)
        elif paragraphs:
            paragraphs[-1] += " " + body
    paragraphs = [_squash(p) for p in paragraphs]
    assert len(paragraphs) == 3, f"ADR §C: очікую 3 абзаци, знайшов {len(paragraphs)}"
    return paragraphs


def test_adr_block_is_parsable():
    """Якщо ADR переписали так, що блок більше не вичитується — падає ТУТ,
    а не 9 разів нижче з незрозумілої причини."""
    paragraphs = _adr_paragraphs()
    assert paragraphs[0].startswith("`category_id` —")
    assert paragraphs[1].startswith("`project` —")
    assert paragraphs[2].startswith("`tags` —")


@pytest.mark.parametrize("tool_name", _FILTER_TOOLS)
def test_tool_docstring_quotes_adr_verbatim(tool_name):
    doc = _squash(getattr(m, tool_name).__doc__ or "")
    for paragraph in _adr_paragraphs():
        assert paragraph in doc, f"{tool_name}: абзац ADR §C не дослівний"


# ============================================================
# v47 (історія 20): текст помилки дати — українською і для агента
# ============================================================

def test_invalid_date_error_text_is_ukrainian_for_the_agent(archive):
    """`error` читає агент і переказує власнику. Сире `Invalid isoformat
    string: 'not-a-date'` не каже ні мовою власника, ні що саме очікується —
    обидва тулзи беруть текст з одного `categories.parse_date`."""
    for res in (m.list_recent(since="not-a-date"),
                m.list_action_items(until="not-a-date")):
        error = res["error"]
        assert "YYYY-MM-DD" in error and "not-a-date" in error
        assert "isoformat" not in error.lower()


# ============================================================
# v47 (історія 20): тезки — повний шлях і явна неоднозначність
# ============================================================

def test_list_categories_carries_the_canonical_path(archive, db):
    """Агент бачить те саме, що людина в селекті: «Напрямок → Проєкт». Без
    `path` однойменні проєкти різних напрямків у видачі не розрізнити."""
    conn = sqlite3.connect(db)
    try:
        free = conn.execute("SELECT id FROM categories WHERE name_norm = 'фріланс'"
                            ).fetchone()[0]
        conn.execute("INSERT INTO categories (name, name_norm, slug, parent_id) "
                     "VALUES ('Акмекорп', 'акмекорп', 'freelance-acmecorp', ?)", (free,))
        conn.commit()
    finally:
        conn.close()
    paths = {c["path"] for c in m.list_categories()}
    assert {"Фонд Робота", "Фонд Робота → Акмекорп",
            "Фріланс → Акмекорп"} <= paths
    assert all(c.get("path") for c in m.list_categories())


def test_ask_archive_turns_409_ambiguous_into_a_note_with_paths(monkeypatch):
    """409 з `/api/memory/ask` (тіло не-200 `_api` віддає) — не виняток і не
    мовчазна відповідь по одному з вузлів, а нота з переліком шляхів."""
    both = ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]
    monkeypatch.setattr(m, "_api", lambda *a, **kw: {
        "success": False, "error": "ambiguous", "code": "ambiguous",
        "message": "«Маркетинг» є в кількох напрямках", "candidates": both,
        "status_code": 409})
    res = m.ask_archive("що по кошторису?", project="Маркетинг")
    assert res["candidates"] == both
    assert "Маркетинг" in res["error"]
    assert "path" in res["hint"]


# Історія 23: лише `ask_archive` має текстовий вхід `project`; решта восьми
# приймають `category_id: Optional[int]` — і речення про тезок мусить радити
# вхід, який тулз справді приймає, а не один спільний шаблон на всі дев'ять.
_CATEGORY_ID_TOOLS = tuple(t for t in _FILTER_TOOLS if t != "ask_archive")


@pytest.mark.parametrize("tool_name", _CATEGORY_ID_TOOLS)
def test_tool_docstring_tells_about_the_full_path(tool_name):
    """Тулзи з `category_id` радять знайти вузол у `list_categories` і
    передати його `id` — не «повний шлях» (той приймає лише `ask_archive`)."""
    doc = _squash(getattr(m, tool_name).__doc__ or "")
    assert "list_categories" in doc, tool_name
    assert "`id`" in doc and "`category_id`" in doc, tool_name
    assert "передавай повний шлях" not in doc, tool_name


def test_ask_archive_docstring_tells_about_the_full_path():
    """`ask_archive` — єдиний тулз із текстовим `project`, тож саме йому
    докстринг радить повний шлях «Напрямок → Проєкт»."""
    doc = _squash(m.ask_archive.__doc__ or "")
    assert "повний шлях «Напрямок → Проєкт»" in doc
    assert "`project`" in doc
    assert "list_categories" in doc
