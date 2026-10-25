"""MCP token budget (mcp-token-budget): get_transcript / search_archive fit `max_tokens`.

Offline: a temporary SQLite via `init_database`, `_api` faked, no network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.db.migrations import init_database

import mcp_server as m

SENTENCE = "Домовились, що Олена надішле кошторис до пʼятниці, а Петро перевірить договір. "
LONG_TEXT = "\n".join(SENTENCE * 3 for _ in range(400))          # ~95k chars, many line ends


def _insert(path: str, tid: int, text: str, polished=None, segments=None, thread_id=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text, "
        "polished_text, segments, tg_thread_id, tg_date) VALUES (?, 'recording', ?, ?, ?, ?, ?, ?)",
        (tid, f"дзвінок {tid}", text, polished, json.dumps(segments, ensure_ascii=False)
         if segments is not None else None, thread_id, f"2026-10-0{tid % 9 + 1}"))
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "budget.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


def _size(d: dict) -> int:
    return len(json.dumps(d, ensure_ascii=False, default=str))


def test_long_text_is_paged_without_loss_or_repeat(db):
    _insert(db, 1, LONG_TEXT)
    pages, offset = [], 0
    while True:
        d = m.get_transcript(1, max_tokens=5000, offset=offset)
        assert _size(d) <= 5000 * 3
        pages.append(d["transcript_text"])
        nxt = d["budget"].get("next_offset")
        if nxt is None:
            assert "truncated" not in d
            break
        assert d["truncated"] is True and d["budget"]["text_total_chars"] == len(LONG_TEXT)
        assert nxt > offset
        offset = nxt
    assert len(pages) > 1
    assert "".join(pages) == LONG_TEXT
    # pages end on a line or sentence end, never mid-word
    assert all(p.endswith(("\n", ". ")) or p.endswith(".") for p in pages[:-1])


def test_short_record_is_whole_with_a_quiet_budget_block(db):
    _insert(db, 2, "Короткий дзвінок про бюджет.")
    d = m.get_transcript(2)
    assert d["transcript_text"] == "Короткий дзвінок про бюджет."
    assert "truncated" not in d
    assert d["budget"]["truncated"] is False and d["budget"]["budget_tokens"] == 40000
    assert "next_offset" not in d["budget"]


def test_zero_means_no_limit_and_explicit_value_wins(db):
    _insert(db, 3, LONG_TEXT)
    assert m.get_transcript(3)["transcript_text"] == LONG_TEXT          # 95k chars < 120k default
    assert m.get_transcript(3, max_tokens=0)["transcript_text"] == LONG_TEXT
    small = m.get_transcript(3, max_tokens=2000)
    assert small["truncated"] is True and _size(small) <= 2000 * 3


def test_polished_text_is_not_duplicated(db):
    _insert(db, 4, "сирий текст ну е", polished="Відполірований текст.")
    d = m.get_transcript(4)
    assert d["polished_text"] == "Відполірований текст." and "transcript_text" not in d
    assert d["text_source"] == "polished"
    raw = m.get_transcript(4, prefer_raw=True)
    assert raw["transcript_text"] == "сирий текст ну е" and "polished_text" not in raw
    assert raw["text_source"] == "raw"


def test_comments_survive_and_segments_resume(db):
    from app.services import comments
    segs = [{"start": i, "end": i + 1, "speaker": "Олена", "text": SENTENCE} for i in range(300)]
    _insert(db, 5, "Текст запису.", segments=segs)
    comments.create(db, "transcription", 5, "насправді кошторис до понеділка", kind="correction")
    d = m.get_transcript(5, include_segments=True, max_tokens=3000)
    assert [c["body"] for c in d["comments"]] == ["насправді кошторис до понеділка"]
    assert d["transcript_text"] == "Текст запису."
    assert d["truncated"] is True and _size(d) <= 3000 * 3
    nxt = d["budget"]["segments_next"]
    assert nxt == len(d["segments"]) and d["budget"]["segments_total"] == 300
    rest = m.get_transcript(5, include_segments=True, max_tokens=3000, segment_offset=nxt)
    assert rest["segments"][0] == segs[nxt]


def test_thread_messages_are_cut_after_the_text(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO tg_threads (id, chat_id, label, msg_count) VALUES (1, 777, 'кошторис', 50)")
    conn.commit()
    conn.close()
    for i in range(10, 60):
        _insert(db, i, SENTENCE * 4, thread_id=1)
    d = m.get_transcript(10, max_tokens=2000)
    assert d["transcript_text"] == SENTENCE * 4
    assert 0 < len(d["thread"]["messages"]) < 50
    assert d["budget"]["thread_messages_omitted"] == 50 - len(d["thread"]["messages"])
    assert _size(d) <= 2000 * 3


# --- search_archive (story 02) ---------------------------------------------------

def _chunk(i: int, text: str) -> dict:
    return {"chunk_id": i, "transcription_id": 100 + i, "display_name": f"дзвінок {i}",
            "source_name": f"дзвінок {i}", "source_type": "recording", "meeting_date": "2026-10-01",
            "speaker": "Олена", "start_time": float(i), "end_time": float(i) + 5, "page": None,
            "text": text, "score": 1.0 - i / 100, "matched_by": ["bm25"]}


@pytest.fixture()
def search_api(db, monkeypatch):
    holder = {}
    monkeypatch.setattr(m, "_api", lambda method, path, **kw: {
        "query": kw["params"]["q"], "vector_available": True, "chunks": holder["chunks"]})
    return holder


def test_search_keeps_rank_order_and_turns_overflow_into_locators(search_api):
    chunks = [_chunk(i, SENTENCE * 20) for i in range(12)]          # ~1.6k chars each
    search_api["chunks"] = chunks
    res = m.search_archive("кошторис", top_k=12, max_tokens=2500)
    shown, locs = res["chunks"], res["locators"]
    assert shown == chunks[:len(shown)] and 0 < len(shown) < 12
    assert [loc["chunk_id"] for loc in locs] == [c["chunk_id"] for c in chunks[len(shown):len(shown) + len(locs)]]
    assert all("text" not in loc and loc["transcription_id"] for loc in locs)
    b = res["budget"]
    assert (b["shown"], b["as_locators"], b["shown"] + b["as_locators"] + b["dropped"]) == \
        (len(shown), len(locs), 12)
    assert res["truncated"] is True and _size(res) <= 2500 * 3


def test_search_default_budget_returns_chunks_unchanged(search_api):
    chunks = [_chunk(i, SENTENCE) for i in range(8)]
    search_api["chunks"] = [dict(c) for c in chunks]
    res = m.search_archive("кошторис")
    assert res["chunks"] == chunks and "locators" not in res and "truncated" not in res
    assert res["budget"]["shown"] == 8 and res["budget"]["truncated"] is False


def test_privacy_filter_runs_before_budget(search_api, monkeypatch):
    search_api["chunks"] = [_chunk(0, SENTENCE), {**_chunk(1, "пароль на екрані"), "speaker": "екран"}]
    monkeypatch.setattr(m._privacy, "filter_screen_chunks",
                        lambda db, ch: ([c for c in ch if c["speaker"] != "екран"], 1))
    res = m.search_archive("пароль", max_tokens=1)
    assert res["private_hidden"] == 1
    assert all(loc["chunk_id"] != 1 for loc in res.get("locators", []))
    assert all(c["chunk_id"] != 1 for c in res["chunks"])


def test_call_log_ok_line_carries_response_size(search_api, tmp_path):
    import asyncio
    from fastmcp import Client
    from tests.test_mcp_registry import load_mcp_copy
    search_api["chunks"] = [_chunk(i, SENTENCE * 5) for i in range(3)]
    full = load_mcp_copy()
    full._api = m._api
    log = tmp_path / "calls.log"
    mw = full._CallLogMiddleware(str(log))
    full.mcp.add_middleware(mw)
    try:
        async def go():
            async with Client(full.mcp) as c:
                await c.call_tool("search_archive", {"query": "кошторис"})
        asyncio.run(go())
    finally:
        full.mcp.middleware.remove(mw)
    lines = log.read_text(encoding="utf-8").splitlines()
    start = [ln for ln in lines if "START search_archive" in ln]
    ok = [ln for ln in lines if " OK  search_archive " in ln]
    assert start and start[0].split(" args=")[0].endswith("START search_archive")
    assert ok and ok[0].rstrip().endswith("chars") and "k chars" in ok[0]


# --- repair round 1 ---------------------------------------------------------------

def test_transcript_resource_returns_the_whole_raw_text(db):
    big = "Ж" * 210000
    _insert(db, 70, "сирий текст", polished="Відполірований текст.")
    _insert(db, 71, big)
    assert m.transcript_resource("70").endswith("\n\nсирий текст")
    assert m.transcript_resource("71").endswith("\n\n" + big)


def test_no_room_for_text_never_loops(db):
    _insert(db, 72, LONG_TEXT)
    d = m.get_transcript(72, max_tokens=150)
    b = d["budget"]
    assert d["truncated"] is True and "next_offset" not in b and b["text_no_room"] is True
    assert b["text_remaining_chars"] == len(LONG_TEXT)


def test_remaining_chars_and_null_text(db):
    _insert(db, 73, LONG_TEXT)
    b = m.get_transcript(73, max_tokens=5000)["budget"]
    assert b["text_remaining_chars"] == len(LONG_TEXT) - b["next_offset"]
    _insert(db, 74, None)
    assert m.get_transcript(74)["transcript_text"] is None
