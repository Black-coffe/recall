"""Spec directions-projects-tags, story 04: the `tags`/`since`/`until` axes and the tree
of directions in MCP.

What exactly is proven here and why:

* **Direction expansion.** A direction's `category_id` must include its projects, while
  a project's `category_id` includes only itself. This is not cosmetics of the result but the privacy
  boundary of the slice: "show Fond" must show Acmecorp, "show Acmecorp" must not
  show the neighbouring project.
* **Unknown tag.** A filter that is not in the dictionary gives an empty result. Without the `note`
  field the agent reads it as "there is no such thing in the archive" and lies to the owner — so
  an emptiness with an unrecognized tag must be labelled.
* **Proxies do not reinterpret parameters.** `search_archive`/`ask_archive`/`research_*`
  only translate them into query/JSON; the HTTP side appears in stories 02/07,
  so `_api`/`_sse_collect` is mocked here — the test catches exactly the loss of a parameter on
  repackaging.
* **Docstrings carry the ADR-023 §C paragraphs in English.** All nine filter tools describe
  `category_id`/`project`/`tags` with the same three paragraphs, so the agent reads one contract.

Cyrillic fixtures — the note `plan-dictated-sql-ignored-own-memory` (SQLite
`lower()` does not fold Cyrillic, so any case is caught only through
`name_norm`).

Offline: a temporary SQLite via `init_database`, no network and no heavy models.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import mcp_server as m
from tests.test_mcp_registry import load_mcp_copy
from app.db.migrations import init_database
from app.services import categories


# 9 tools of contract C6: 8 with an existing `category_id` + `grep_archive`, to which it
# was added by this same story.
_FILTER_TOOLS = (
    "search_archive", "list_recent", "list_action_items", "ask_archive",
    "research_export", "research_preview", "research_summary", "search_history",
    "grep_archive",
)


def _squash(text: str) -> str:
    """We compare without regard to line breaks: the docstring is wrapped at 79 columns,
    the ADR at its own, and "verbatim" concerns words, not the place of a line break."""
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "taxonomy.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


@pytest.fixture()
def archive(db):
    """The tree "Фонд Робота → (Акмекорп, DOM)" + "Особисте" and 4 records with tags.

    The dates are deliberately different and adjacent (10/15/20 September), so that the `since`/`until`
    bounds can be checked for inclusiveness, not "roughly in that month".
    """
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    def _cat(name, parent=None, color=None, icon=None, sort_order=0):
        """`Фонд Робота` and `Особисте` are already seeded by the migration — we take the existing ones
        (creating would fail on UNIQUE name_norm)."""
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
# list_recent: direction/project, tags, period
# ============================================================

def test_direction_includes_its_projects(archive):
    """A direction's `category_id` expands into the direction + all its projects."""
    got = _ids(m.list_recent(category_id=archive["cat"]["fond"]))
    assert got == {archive["rec"]["fond"], archive["rec"]["bar"], archive["rec"]["dom"]}
    assert archive["rec"]["personal"] not in got


def test_project_includes_only_itself(archive):
    """A project's `category_id` includes neither the parent nor the neighbouring project."""
    assert _ids(m.list_recent(category_id=archive["cat"]["bar"])) == {archive["rec"]["bar"]}


def test_tags_are_or(archive):
    """Several tags — "any of them", not an intersection."""
    assert _ids(m.list_recent(tags="AI, інвестори")) == {
        archive["rec"]["fond"], archive["rec"]["bar"], archive["rec"]["dom"]}


def test_tag_matches_any_case(archive):
    """`name_norm` (casefold) — otherwise "ІНВЕСТОРИ" would not find "інвестори"."""
    assert _ids(m.list_recent(tags="ІНВЕСТОРИ")) == {
        archive["rec"]["bar"], archive["rec"]["dom"]}


def test_tags_and_category_intersect(archive):
    """A tag together with a direction — an intersection: "tag AI only in this project"."""
    assert _ids(m.list_recent(tags="AI", category_id=archive["cat"]["dom"])) == {
        archive["rec"]["dom"]}
    assert _ids(m.list_recent(tags="AI", category_id=archive["cat"]["bar"])) == set()


def test_since_until_bounds_are_inclusive(archive):
    """Both bounds are inclusive — a one-day window must find its record."""
    assert _ids(m.list_recent(since="2026-09-15", until="2026-09-15")) == {
        archive["rec"]["bar"]}
    assert _ids(m.list_recent(since="2026-09-10", until="2026-09-20")) == {
        archive["rec"]["fond"], archive["rec"]["bar"], archive["rec"]["dom"]}
    assert _ids(m.list_recent(since="2026-09-16")) == {archive["rec"]["dom"]}


def test_invalid_date_is_an_answer_not_an_exception(archive):
    """A crooked date from the agent is an `error` field, not a 500 in the stdio channel."""
    res = m.list_recent(since="вчора")
    assert isinstance(res, dict) and "error" in res


def test_unknown_tag_returns_empty_with_note(archive):
    """An emptiness due to an unrecognized tag must be labelled — otherwise the agent
    reads it as "there is no such thing in the archive"."""
    res = m.list_recent(tags="Бухгалтерія")
    assert res["items"] == []
    assert "tag not found" in res["note"] and "Бухгалтерія" in res["note"]


def test_known_tag_has_no_note(archive):
    """An ordinary result — the same envelope, `note` empty."""
    res = m.list_recent(tags="AI")
    assert isinstance(res, dict) and "items" in res
    assert res["note"] is None


# ============================================================
# list_action_items — the same axes through commitments
# ============================================================

def test_action_items_filtered_by_tag(archive):
    """Tasks are filtered by the tag of the RECORD they were extracted from."""
    res = m.list_action_items(window="all", tags="AI")
    assert [i["task"] for i in res["items"]] == ["Підготувати бюджет фонду"]


def test_action_items_direction_includes_projects(archive):
    """A direction in tasks expands the same way as in records."""
    res = m.list_action_items(window="all", category_id=archive["cat"]["fond"])
    assert {i["task"] for i in res["items"]} == {
        "Підготувати бюджет фонду", "Надіслати терміни раунду"}


def test_action_items_window_counter_uses_same_filter(archive):
    """`window_matched` counts THE SAME set as the result (otherwise "shown 1
    of 3" with one present)."""
    res = m.list_action_items(window="all", tags="AI")
    assert res["coverage"]["window_matched"] == len(res["items"]) == 1
    assert res["coverage"]["truncated"] is False


def test_action_items_period(archive):
    """`since`/`until` — by the record's meeting date, not by the task creation date."""
    res = m.list_action_items(window="all", since="2026-09-12")
    assert [i["task"] for i in res["items"]] == ["Надіслати терміни раунду"]


def test_action_items_unknown_tag_notes(archive):
    res = m.list_action_items(window="all", tags="Бухгалтерія")
    assert res["items"] == []
    assert "tag not found" in res["note"]


def test_action_items_invalid_date(archive):
    assert "error" in m.list_action_items(window="all", since="2026-13-40")


def test_action_items_coverage_denominator_matches_tag_filter(archive):
    """`coverage.total` counts THE SAME set as `items` (repair item 1 of round
    3): before that the denominator ignored `tags`, so `list_action_items(tags="AI")`
    gave a filtered result next to the archive `coverage.total`."""
    res = m.list_action_items(window="all", tags="AI")
    assert res["coverage"]["total"] == len(res["items"]) == 1


def test_action_items_coverage_denominator_unchanged_without_filters(archive):
    """Without `tags`/`since`/`until` the denominator stays the same as before
    the story — all 3 live tasks of the fixture."""
    res = m.list_action_items(window="all")
    assert res["coverage"]["total"] == 3


# ============================================================
# grep_archive — the filter before the scan
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
    assert "tag not found" in res["note"]


# ============================================================
# list_categories — a tree, not a flat list
# ============================================================

def test_list_categories_returns_tree_in_order(archive):
    """The order is a direction, right after it its projects; the color/icon fields are no longer
    lost (the old version of the tool did not select them at all)."""
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
    """The reference the agent starts from must show the tree — a flat list of
    names did not make clear which id captures what."""
    text = m.about()
    assert "Фонд Робота (3) → Акмекорп (1), DOM (1)" in text
    assert "Особисте (1)" in text


def test_about_reports_registered_tool_count(archive, monkeypatch):
    """Taxonomy neither adds nor removes tools (C6); 35 → 40 — the video visual timeline (4.10.0),
    40 → 41 — the pipe history `list_pipe_runs` (K4), 41 → 42 — the mail tool (gmail-ingest), 42 → 44 — mailbox, settings,
    44 → 45 — wait_for, 45 → 46 — list_mcp_actions, 46 → 51 — system_status and 4 D tools, 51 → 55 — retranscribe-versions tools, 55 → 68 — archive organisation (mcp-archive-org), 68 → 80 — the intake pipeline (mcp-pipeline). Counted on the full profile, not the developer's .env."""
    full = load_mcp_copy()
    monkeypatch.setattr(full, "DB_PATH", m.DB_PATH)
    n = len(full._registered_tool_names())
    assert n == 82
    assert f"{n} tools" in full.about()


# ============================================================
# Proxies: parameters arrive without reinterpretation
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
# research_export — direct-DB, the same axes
# ============================================================

def test_research_export_scoped_by_tag(archive):
    md = m.research_export("ВОДОСПАД", tags="інвестори")
    assert "Акмекорп щотижнева.mp3" in md
    assert "Особисте нагадування.mp3" not in md


def test_research_export_unknown_tag_notes(archive):
    md = m.research_export("ВОДОСПАД", tags="Бухгалтерія")
    assert md.startswith("tag not found: Бухгалтерія")


def test_research_export_invalid_date(archive):
    """A crooked date is the error envelope, not a markdown string that reads as a report."""
    res = m.research_export("ВОДОСПАД", since="вчора")
    assert isinstance(res, dict) and res["ok"] is False
    assert res["error_code"] == "INVALID_ARGUMENT" and "Filter error" in res["error"]


# Docstrings — the three ADR §C filter paragraphs, in English
# ============================================================

#: The English rendering of the ADR-023 §C block. The ADR keeps the original Ukrainian wording
#: inside its verbatim markers; the tool descriptions are English (code-language rule), so the
#: contract checked here is: every filter tool carries the same three paragraphs, word for word.
_FILTER_PARAGRAPHS = (
    "`category_id` — id of a **direction or project** from `list_categories`; a direction "
    "automatically includes all its projects, a project only itself. This is the archive's "
    "hierarchy level.",
    "`project` — **not** a hierarchy project but a graph-entity name or free text (a person, "
    "a brand): narrows to records where that name is mentioned. To narrow to a project as a "
    "section of the archive, pass its id in `category_id`, not its name in `project`.",
    "`tags` — tag names comma-separated; several = any of them. Combined with `category_id` — "
    'an intersection ("tag AI only in this direction").',
)


@pytest.mark.parametrize("tool_name", _FILTER_TOOLS)
def test_tool_docstring_carries_filter_paragraphs(tool_name):
    doc = _squash(getattr(m, tool_name).__doc__ or "")
    for paragraph in _FILTER_PARAGRAPHS:
        assert paragraph in doc, f"{tool_name}: an ADR §C filter paragraph is missing or reworded"


@pytest.mark.parametrize("tool_name", _FILTER_TOOLS)
def test_tool_docstring_is_english(tool_name):
    doc = getattr(m, tool_name).__doc__ or ""
    stripped = re.sub(r"«[^»]{0,60}»|`[^`]*`", " ", doc)
    assert not re.search("[Ѐ-ӿ]", stripped), f"{tool_name}: Cyrillic outside quoted spans"


# ============================================================
# v47 (story 20): the date error text — for the agent
# ============================================================

def test_invalid_date_error_text_is_ukrainian_for_the_agent(archive):
    """`error` is read by the agent and retold to the owner. The raw `Invalid isoformat
    string: 'not-a-date'` says neither in the owner's language nor what exactly is expected —
    both tools take the text from one `categories.parse_date`."""
    for res in (m.list_recent(since="not-a-date"),
                m.list_action_items(until="not-a-date")):
        error = res["error"]
        assert "YYYY-MM-DD" in error and "not-a-date" in error
        assert "isoformat" not in error.lower()


# ============================================================
# v47 (story 20): namesakes — the full path and explicit ambiguity
# ============================================================

def test_list_categories_carries_the_canonical_path(archive, db):
    """The agent sees the same as a person in the select: "Direction → Project". Without
    `path` projects of the same name in different directions cannot be told apart in the result."""
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
    """A 409 from `/api/memory/ask` (the non-200 body `_api` returns) — not an exception and not a
    silent answer from one of the nodes, but a note listing the paths."""
    both = ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]
    monkeypatch.setattr(m, "_api", lambda *a, **kw: {
        "success": False, "error": "ambiguous", "code": "ambiguous",
        "message": "«Маркетинг» є в кількох напрямках", "candidates": both,
        "status_code": 409})
    res = m.ask_archive("що по кошторису?", project="Маркетинг")
    assert res["candidates"] == both
    assert "Маркетинг" in res["error"]
    assert "path" in res["hint"]


# Story 23: only `ask_archive` has a text input `project`; the other eight
# accept `category_id: Optional[int]` — and the sentence about namesakes must advise the
# input the tool really accepts, not one shared template for all nine.
_CATEGORY_ID_TOOLS = tuple(t for t in _FILTER_TOOLS if t != "ask_archive")


@pytest.mark.parametrize("tool_name", _CATEGORY_ID_TOOLS)
def test_tool_docstring_tells_about_the_full_path(tool_name):
    """Tools with `category_id` advise finding the node in `list_categories` and
    passing its `id` — not the "full path" (only `ask_archive` accepts that)."""
    doc = _squash(getattr(m, tool_name).__doc__ or "")
    assert "list_categories" in doc, tool_name
    assert "`id`" in doc and "`category_id`" in doc, tool_name
    assert "pass the full path" not in doc, tool_name


def test_ask_archive_docstring_tells_about_the_full_path():
    """`ask_archive` is the only tool with a text `project`, so it is the one whose
    docstring advises the full path "Direction → Project"."""
    doc = _squash(m.ask_archive.__doc__ or "")
    assert 'the full path "Direction → Project"' in doc
    assert "`project`" in doc
    assert "list_categories" in doc
