"""MCP mail surface (gmail-ingest-07): `email_status` proxy whitelist and `email` blocks.

Offline: a temporary SQLite via `init_database`, `_api` faked, no network.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.migrations import init_database

import mcp_server as m


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "mail.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO mail_accounts (id, address) VALUES (1, 'owner@example.test')")
    conn.execute("INSERT INTO mail_labels (account_id, folder, display_name, enabled) "
                 "VALUES (1, 'INBOX', 'Вхідні', 1)")
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text, mail_account_id, "
        "mail_message_id, mail_thread_key, mail_from, mail_to, mail_subject, mail_folder) "
        "VALUES (10, 'email', 'mail', 'Текст листа', 1, '<m1@example.test>', '<m1@example.test>', "
        "'anna@example.test', 'owner@example.test', 'Договір на підпис', 'INBOX')")
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
                 "VALUES (11, 'document', 'contract.pdf', 'Текст вкладення')")
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
                 "VALUES (12, 'document', 'other.pdf', 'Інший')")
    conn.execute("INSERT INTO mail_attachments (email_tid, document_tid, filename) "
                 "VALUES (10, 11, 'contract.pdf')")
    conn.commit()
    conn.close()
    return path


def test_get_transcript_email_block_and_attached_to(db):
    d = m.get_transcript(10)
    assert d["email"] == {
        "from": "anna@example.test", "to": "owner@example.test", "subject": "Договір на підпис",
        "account": "owner@example.test", "folder": "Вхідні", "thread_key": "<m1@example.test>",
        "message_id": "<m1@example.test>",
        "attachments": [{"document_id": 11, "filename": "contract.pdf"}],
    }
    assert "attached_to" not in d
    doc = m.get_transcript(11)
    assert doc["attached_to"] == [10]
    assert "email" not in doc
    other = m.get_transcript(12)
    assert "email" not in other and "attached_to" not in other


def test_list_recent_email_block_and_filter(db):
    res = m.list_recent(source_type="email")
    assert [i["id"] for i in res["items"]] == [10]
    assert res["items"][0]["email"]["subject"] == "Договір на підпис"
    allrows = {i["id"]: i for i in m.list_recent()["items"]}
    assert "email" in allrows[10] and "email" not in allrows[11]


def test_search_history_adds_email_block(db, monkeypatch):
    payload = {"transcriptions": [{"id": 10, "source_type": "email"},
                                  {"id": 11, "source_type": "document"}], "total": 2}
    monkeypatch.setattr(m, "_api", lambda *a, **kw: payload)
    res = m.search_history(source_type="email")
    by_id = {i["id"]: i for i in res["transcriptions"]}
    assert by_id[10]["email"]["account"] == "owner@example.test"
    assert "email" not in by_id[11]


def test_unmigrated_db_does_not_break_reads(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transcriptions (id INTEGER PRIMARY KEY, source_type TEXT, "
                 "source_name TEXT, title TEXT, description TEXT, language TEXT, model_used TEXT, "
                 "category_id INTEGER, meeting_date TEXT, doc_type TEXT, original_filename TEXT, "
                 "page_count INTEGER, summary_json TEXT, transcript_text TEXT, polished_text TEXT, "
                 "segments TEXT, tg_chat_id INTEGER, tg_chat_title TEXT, tg_sender TEXT, "
                 "tg_message_id INTEGER, tg_date TEXT, tg_link TEXT, tg_reply_to INTEGER, "
                 "tg_thread_id INTEGER, tg_thread_src TEXT, created_at TEXT)")
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
                 "VALUES (1, 'document', 'a.pdf', 'text')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "DB_PATH", path)
    with m.get_db_connection(path) as c:
        assert m._email_blocks(c, [1]) == {}
        assert m._attached_to(c, 1) == []


def test_email_status_whitelist_and_error(monkeypatch):
    payload = {
        "accounts": {"accounts": [{
            "id": 1, "address": "owner@example.test", "configured": True, "last_poll_at": "t",
            "last_error_code": None, "password": "x", "app_password": "y",
            "labels": [{"id": 2, "folder": "INBOX", "display_name": "Вхідні", "enabled": 1,
                        "records": 3, "password": "x", "app_password": "y"}]}]},
        "poller": {"running": True, "interval_min": 5, "app_password": "y"},
    }
    monkeypatch.setattr(m, "_api", lambda *a, **kw: payload)
    out = m.email_status()
    flat = repr(out)
    assert "password" not in flat
    assert out["accounts"][0]["address"] == "owner@example.test"
    assert out["accounts"][0]["labels"][0]["records"] == 3
    assert out["poller"] == {"running": True, "interval_min": 5}
    monkeypatch.setattr(m, "_api", lambda *a, **kw: {"error": "app.py is not running"})
    assert "error" in m.email_status()


def test_email_status_registered_in_about():
    assert "email_status" in m._registered_tool_names()
    assert "email_status" in m._ABOUT_PROXY
    assert "`email_status`" in m.about()


def test_get_transcript_email_thread(db):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text, meeting_date, "
        "mail_account_id, mail_message_id, mail_thread_key, mail_in_reply_to, mail_from, mail_subject, "
        "mail_folder) VALUES (20, 'email', 'mail', 'Ціна 12000 EUR?', '2026-10-05', 1, '<r1@example.test>', "
        "'<r1@example.test>', NULL, 'anna@example.test', 'Ціна', 'INBOX')")
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text, meeting_date, "
        "mail_account_id, mail_message_id, mail_thread_key, mail_in_reply_to, mail_from, mail_subject, "
        "mail_folder) VALUES (21, 'email', 'mail', 'Так, до 30 листопада.', '2026-10-06', 1, "
        "'<r2@example.test>', '<r1@example.test>', '<r1@example.test>', 'owner@example.test', "
        "'Re: Ціна', 'INBOX')")
    conn.commit()
    conn.close()
    for tid in (20, 21):
        th = m.get_transcript(tid)["thread"]
        assert th["thread_key"] == "<r1@example.test>" and th["msg_count"] == 2
        assert [x["id"] for x in th["messages"]] == [20, 21]
        assert th["messages"][1]["transcript_text"] == "Так, до 30 листопада."
        assert th["messages"][1]["meeting_date"] == "2026-10-06"
    # A lone email has no thread.
    assert "thread" not in m.get_transcript(10)
