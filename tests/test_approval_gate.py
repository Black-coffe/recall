"""Approval gate (mcp-foundations-b story 03): 13 destructive routes behind the dialog.

The app here carries the seven real blueprints that own the gated routes, wired the way
`app.py` wires them (auth gate -> approval gate -> audit hooks), over a temp DB from
`init_database`. The live `whisper_history.db` is not touched; no real window can open
(`tests/conftest.py` replaces `approvals._show_dialog`). Fixtures are Cyrillic and invented.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from flask import Flask, g

from app import state as app_state
from app.blueprints import memory as memory_bp_mod
from app.blueprints import telegram as telegram_bp_mod
from app.blueprints.audio_library import audio_bp
from app.blueprints.documents import documents_bp
from app.blueprints.mail import mail_bp
from app.blueprints.memory import memory_bp
from app.blueprints.settings_api import settings_bp
from app.blueprints.speakers import speakers_bp
from app.blueprints.telegram import telegram_bp
from app.blueprints.transcription import transcription_bp
from app.core import approval_gate, audit_hooks, auth
from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.repositories import mcp_actions
from app.services import approval_summaries, approvals, enrichment
from app.services import categories as cats
from app.services.recording import state as rec_state

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "approval_gate.db")
    init_database(path)
    return path


@pytest.fixture
def audits():
    return []


@pytest.fixture
def app(db, tmp_path, monkeypatch, audits):
    monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "1")
    a = Flask(__name__, root_path=str(tmp_path))
    a.config.update(TESTING=True, DATABASE=db, TELEGRAM_CONTROL_HOST="127.0.0.1",
                    TELEGRAM_CONTROL_PORT=5599)
    for bp in (transcription_bp, memory_bp, speakers_bp, telegram_bp, settings_bp, mail_bp, audio_bp,
               documents_bp):
        a.register_blueprint(bp)
    auth.register(a)
    approval_gate.register(a)
    audit_hooks.register(a)

    @a.after_request
    def _capture(response):
        audits.append(dict(getattr(g, "recall_audit", None) or {}))
        return response

    return a


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    approval_gate.reset_counters()
    monkeypatch.setattr(app_state, "recording_service", None, raising=False)
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: {"mode": "point", "count": len(ids), "stale": 0})
    yield
    approval_gate.reset_counters()


@pytest.fixture
def side_effects(monkeypatch):
    """Stubs for the views that leave the process: the job queue and the TG listener."""
    calls = {"jobs": [], "listener": []}

    class _Job:
        id = "job-1"

    class _Queue:
        def submit(self, kind, fn, meta=None):
            calls["jobs"].append(kind)
            return _Job()

    monkeypatch.setattr(app_state, "job_queue", _Queue(), raising=False)
    monkeypatch.setattr(enrichment, "any_available", lambda: True)

    def _listener(path, method="GET", body=None, timeout=60):
        calls["listener"].append((path, body))
        return True, {"queued": True}

    monkeypatch.setattr(telegram_bp_mod, "_listener_call", _listener)
    memory_bp_mod._backfill_state["running"] = False
    return calls


def _uk(key, **kw):
    return tr(key, lang="uk", **kw)


# ------------------------------------------------------------------ seeds

def _tx(conn, title="Планерка про бюджет", segments=3, category_id=None):
    segs = [{"start": i, "end": i + 1, "text": f"фраза {i}"} for i in range(segments)]
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, title, transcript_text, segments, category_id) "
        "VALUES ('file', 'запис.mp3', ?, 'Обговорили бюджет', ?, ?)",
        (title, json.dumps(segs, ensure_ascii=False), category_id))
    conn.commit()
    return cur.lastrowid


def _alive(conn, table, col, value, extra=""):
    return conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ? {extra}", (value,)).fetchone() is not None


def _case_history(conn, env):
    tid = _tx(conn)
    return dict(method="delete", path=f"/api/history/{tid}", json=None, name="Планерка про бюджет",
                count=3, undo=True,
                present=lambda c: _alive(c, "transcriptions", "id", tid, "AND deleted_at IS NULL"))


def _case_bulk(conn, env):
    a = _tx(conn, title="Нарада з дизайну")
    b = _tx(conn, title="Дзвінок з юристом")
    return dict(method="post", path="/api/history/bulk_delete", json={"ids": [a, b]},
                name="Нарада з дизайну", count=2, undo=False,
                present=lambda c: _alive(c, "transcriptions", "id", a))


def _case_category_delete(conn, env):
    cat = cats.create_category(conn, "Тестовий напрямок")
    _tx(conn, category_id=cat["id"])
    return dict(method="delete", path=f"/api/memory/categories/{cat['id']}", json=None,
                name="Тестовий напрямок", count=1, undo=False,
                present=lambda c: _alive(c, "categories", "id", cat["id"]))


def _case_category_merge(conn, env):
    src = cats.create_category(conn, "Напрямок-джерело")
    dst = cats.create_category(conn, "Напрямок-ціль")
    _tx(conn, category_id=src["id"])
    _tx(conn, category_id=src["id"])
    return dict(method="post", path=f"/api/memory/categories/{src['id']}/merge",
                json={"target_id": dst["id"], "delete_source": True},
                name="Напрямок-джерело", count=2, undo=False,
                present=lambda c: _alive(c, "categories", "id", src["id"]))


def _case_tag_delete(conn, env):
    tid = _tx(conn)
    tag = cats.ensure_tag(conn, "бюджетний тег")
    cats.set_tags(conn, tid, add=["бюджетний тег"])
    return dict(method="delete", path=f"/api/memory/tags/{tag['id']}", json=None,
                name="бюджетний тег", count=1, undo=False,
                present=lambda c: _alive(c, "tags", "id", tag["id"]))


def _case_tag_merge(conn, env):
    tid = _tx(conn)
    keep = cats.ensure_tag(conn, "фінанси")
    merge = cats.ensure_tag(conn, "фінанси старі")
    cats.set_tags(conn, tid, add=["фінанси старі"])
    return dict(method="post", path="/api/memory/tags/merge",
                json={"keep_id": keep["id"], "merge_id": merge["id"]},
                name="фінанси старі", count=1, undo=False,
                present=lambda c: _alive(c, "tags", "id", merge["id"]))


def _case_backfill(conn, env):
    _tx(conn)
    count = len(enrichment.list_unenriched_ids(env["db"]))
    calls = env["side"]
    return dict(method="post", path="/api/memory/backfill", json={"model": "claude-тест"},
                name=_uk("approvals.kind.enrichment"), count=count, undo=False,
                present=lambda c: not calls["jobs"])


def _case_speaker_delete(conn, env):
    tid = _tx(conn)
    sid = conn.execute("INSERT INTO speakers (name, is_self, usage_count) VALUES ('Остап Тестовий', 0, 2)").lastrowid
    conn.execute("INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) "
                 "VALUES (?, 'SPEAKER_00', ?)", (tid, sid))
    conn.commit()
    return dict(method="delete", path=f"/api/speakers/{sid}", json=None, name="Остап Тестовий",
                count=1, undo=False, present=lambda c: _alive(c, "speakers", "id", sid))


def _case_speaker_merge(conn, env):
    keep = conn.execute("INSERT INTO speakers (name, is_self, usage_count) VALUES ('Марта Основна', 0, 5)").lastrowid
    dup = conn.execute("INSERT INTO speakers (name, is_self, usage_count) VALUES ('Марта дубль', 0, 4)").lastrowid
    conn.commit()
    return dict(method="post", path="/api/speakers/merge", json={"keep_id": keep, "merge_ids": [dup]},
                name="Марта дубль", count=4, undo=True,
                present=lambda c: _alive(c, "speakers", "id", dup))


def _case_telegram(conn, env):
    conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, enabled) VALUES (-100777, 'Робочий чат тест', 1)")
    conn.commit()
    calls = env["side"]
    return dict(method="post", path="/api/telegram/backfill", json={"chat_id": -100777, "limit": 50},
                name="Робочий чат тест", count=50, undo=False, present=lambda c: not calls["listener"])


def _case_anthropic(conn, env):
    env["monkeypatch"].setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")
    (Path(env["root"]) / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-test-value\n", encoding="utf-8")
    import os
    return dict(method="delete", path="/api/settings/anthropic-key", json=None, name="ANTHROPIC_API_KEY",
                count=0, undo=False, present=lambda c: bool(os.environ.get("ANTHROPIC_API_KEY")))


def _case_mail(conn, env):
    aid = conn.execute("INSERT INTO mail_accounts (address) VALUES ('скринька@приклад.укр')").lastrowid
    conn.execute("INSERT INTO mail_labels (account_id, folder, display_name) VALUES (?, 'INBOX', 'Вхідні')", (aid,))
    conn.execute("INSERT INTO mail_labels (account_id, folder, display_name) VALUES (?, 'Work', 'Робота')", (aid,))
    conn.commit()
    return dict(method="delete", path=f"/api/mail/accounts/{aid}", json=None, name="скринька@приклад.укр",
                count=2, undo=False, present=lambda c: _alive(c, "mail_accounts", "id", aid))


def _mail_account(conn):
    aid = conn.execute("INSERT INTO mail_accounts (address) VALUES ('пошта@приклад.укр')").lastrowid
    lid = conn.execute("INSERT INTO mail_labels (account_id, folder, display_name, enabled) "
                       "VALUES (?, 'INBOX', 'Вхідні', 1)", (aid,)).lastrowid
    conn.commit()
    return aid, lid


def _case_mail_disconnect(conn, env):
    aid, lid = _mail_account(conn)
    return dict(method="delete", path=f"/api/mail/accounts/{aid}/credentials", json=None,
                name="пошта@приклад.укр", count=1, undo=False,
                present=lambda c: _alive(c, "mail_labels", "id", lid, "AND enabled = 1"))


def _case_mail_backfill(conn, env):
    aid, _ = _mail_account(conn)
    calls = []

    class _Poller:
        def trigger(self, account_id=None, months=None, first_months_cap=None):
            calls.append((account_id, months))
            return True

    env["app"].extensions["mail_poller"] = _Poller()
    return dict(method="post", path="/api/mail/backfill", json={"account_id": aid, "months": 48},
                name="пошта@приклад.укр", count=48, undo=False, present=lambda c: not calls)


def _case_mail_enrich(conn, env):
    aid, lid = _mail_account(conn)
    return dict(method="post", path=f"/api/mail/labels/{lid}/enrich", json={}, name="Вхідні",
                count=0, undo=True, present=lambda c: _alive(c, "mail_labels", "id", lid, "AND enrich = 0"))


def _case_audio(conn, env):
    aid = conn.execute(
        "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path) "
        "VALUES ('https://youtu.be/x', 'yt-тест-1', 'Лекція про архіви', 'downloads/лекція.mp3')").lastrowid
    conn.commit()
    return dict(method="delete", path=f"/api/audio/downloads/{aid}", json=None, name="Лекція про архіви",
                count=1, undo=True,
                present=lambda c: _alive(c, "audio_downloads", "id", aid, "AND deleted_at IS NULL"))


def _case_import_folder(conn, env):
    root = Path(env["root"]) / "дозволені"
    folder = root / "вхідні документи"
    folder.mkdir(parents=True)
    (folder / "нотатки.md").write_text("# Нотатки\n\nПлан бюджету на квартал.", encoding="utf-8")
    env["monkeypatch"].setenv("RECALL_IMPORT_ROOTS", str(root))
    calls = env["side"]
    import os
    return dict(method="post", path="/api/documents/import-folder", json={"path": str(folder)},
                name=approval_summaries._cut(os.path.realpath(folder), 60), count=1, undo=False,
                present=lambda c: "doc_import" not in calls["jobs"])


def _document(conn, env):
    original = Path(env["root"]) / "звіт.md"
    original.write_text("# Звіт\n\nНовий текст звіту про бюджет.", encoding="utf-8")
    tid = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, original_filename, title, file_path, "
        "transcript_text) VALUES ('document', 'звіт.md', 'звіт.md', 'Звіт про бюджет', ?, 'старий текст')",
        (str(original),)).lastrowid
    conn.commit()
    return tid


def _case_reparse(conn, env):
    tid = _document(conn, env)
    return dict(method="post", path=f"/api/documents/{tid}/reparse", json=None, name="Звіт про бюджет",
                count=1, undo=False,
                present=lambda c: _alive(c, "transcriptions", "id", tid, "AND transcript_text = 'старий текст'"))


def _case_reparse_job(conn, env):
    tid = _document(conn, env)
    calls = env["side"]
    return dict(method="post", path=f"/api/documents/{tid}/reparse-job", json=None, name="Звіт про бюджет",
                count=1, undo=False, status=202, present=lambda c: "doc_import" not in calls["jobs"])


def _case_enable_chat(conn, env):
    conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, enabled) VALUES (-100778, 'Вимкнений чат тест', 0)")
    conn.commit()
    return dict(method="post", path="/api/telegram/chats/-100778/enable", json={},
                name="Вимкнений чат тест", count=0, undo=True,
                present=lambda c: _alive(c, "tg_monitored_chats", "chat_id", -100778, "AND enabled = 0"))


CASES = {
    "transcription.delete_transcription": ("transcript_delete", _case_history),
    "transcription.bulk_delete_transcriptions": ("transcript_bulk_delete", _case_bulk),
    "memory.delete_category": ("taxonomy_delete", _case_category_delete),
    "memory.merge_category": ("taxonomy_merge", _case_category_merge),
    "memory.delete_tag_route": ("taxonomy_delete", _case_tag_delete),
    "memory.merge_tags_route": ("taxonomy_merge", _case_tag_merge),
    "memory.start_backfill": ("enrich_backfill", _case_backfill),
    "speakers.delete_speaker": ("speaker_delete", _case_speaker_delete),
    "speakers.merge_speakers": ("speaker_merge", _case_speaker_merge),
    "telegram.tg_backfill": ("telegram_backfill", _case_telegram),
    "settings_api.clear_anthropic_key": ("settings_remove_anthropic_key", _case_anthropic),
    "mail.mail_accounts_delete": ("mailbox_delete", _case_mail),
    "mail.mail_accounts_disconnect": ("mailbox_disconnect", _case_mail_disconnect),
    "mail.mail_backfill": ("mailbox_backfill", _case_mail_backfill),
    "mail.mail_label_enrich": ("mailbox_label_enrich", _case_mail_enrich),
    "audio_library.delete_audio_download": ("audio_delete", _case_audio),
    "documents.import_folder": ("document_import_folder", _case_import_folder),
    "documents.reparse_document": ("document_reparse", _case_reparse),
    "documents.reparse_job": ("document_reparse", _case_reparse_job),
    "telegram.tg_enable_chat": ("telegram_enable_chat", _case_enable_chat),
}


@pytest.fixture
def seed(db, tmp_path, monkeypatch, side_effects, app):
    def _seed(endpoint):
        env = {"db": db, "root": str(tmp_path), "monkeypatch": monkeypatch, "side": side_effects, "app": app}
        with get_db_connection(db) as conn:
            return CASES[endpoint][1](conn, env)
    return _seed


def _send(client, case, headers=None):
    fn = getattr(client, case["method"])
    kwargs = {"headers": headers or {}}
    if case["json"] is not None:
        kwargs["json"] = case["json"]
    return fn(case["path"], **kwargs)


def _present(db, case):
    with get_db_connection(db) as conn:
        return case["present"](conn)


# ------------------------------------------------------------------ the 13 routes

def test_cases_cover_the_gated_table():
    assert set(CASES) == set(approval_gate._D_ROUTES)
    assert {k: v[0] for k, v in CASES.items()} == approval_gate._D_ROUTES
    assert set(approval_gate._D_ROUTES.values()) <= set(approval_gate.D_ACTIONS)


@pytest.mark.parametrize("endpoint", sorted(CASES))
def test_denied_by_default_view_does_not_run(endpoint, client, db, seed, dialog_stub, audits):
    case = seed(endpoint)
    r = _send(client, case)
    assert r.status_code == 403, r.get_json()
    body = r.get_json()
    assert body["error_code"] == "APPROVAL_DENIED" and body["reason"] == "no"
    assert _present(db, case), "the view ran despite the denial"
    text = dialog_stub.calls[-1][1]
    assert f"«{case['name']}" in text or case["name"] in text
    action = CASES[endpoint][0]
    assert _uk(f"approvals.impact.{action}", count=case["count"]) in text
    assert _uk("approvals.undo.yes" if case["undo"] else "approvals.undo.no") in text
    assert _uk("approvals.who.unknown") in text
    assert audits[-1]["action"] == action
    assert audits[-1]["decision"] == "denied:no"
    assert audits[-1]["summary"]
    assert re.fullmatch(r"[a-z_]+:.+", audits[-1]["object_ref"])


@pytest.mark.parametrize("endpoint", sorted(CASES))
def test_approved_request_runs_the_view(endpoint, client, db, seed, approve_dialogs, audits):
    case = seed(endpoint)
    r = _send(client, case)
    assert r.status_code == case.get("status", 200), r.get_json()
    assert len(approve_dialogs.calls) == 1
    assert not _present(db, case), "the approved view did not run"
    assert audits[-1]["decision"] == "approved"


# ------------------------------------------------------------------ text rules

def test_who_line_comes_first_and_differs_for_ui(client, seed, dialog_stub):
    case = seed("transcription.delete_transcription")
    _send(client, case)
    unknown_text = dialog_stub.calls[-1][1]
    case2 = seed("transcription.delete_transcription")
    _send(client, case2, headers={auth.UI_TOKEN_HEADER: auth.ui_token()})
    ui_text = dialog_stub.calls[-1][1]
    assert unknown_text.splitlines()[0] == _uk("approvals.who.unknown")
    assert ui_text.splitlines()[0] == _uk("approvals.who.ui")
    assert unknown_text.splitlines()[0] != ui_text.splitlines()[0]
    assert "⚠" in unknown_text.splitlines()[0]
    # the ui token is readable by any local process: the ui line is not proof either
    assert "«Ні»" in ui_text.splitlines()[0] and "«Ні»" in unknown_text.splitlines()[0]
    assert "«No»" in tr("approvals.who.ui", lang="en")


def test_claimed_labels_only_after_claims_label_actor_stays_unknown(client, db, seed, dialog_stub):
    case = seed("transcription.delete_transcription")
    _send(client, case, headers={"X-Recall-Actor": "mcp", "X-Recall-Tool": "t"})
    text = dialog_stub.calls[-1][1]
    claims = _uk("approvals.who.claims", actor="mcp", tool="t")
    assert claims in text
    prefix = claims.split("«")[0]
    assert text.index("«mcp»") > text.index(prefix)
    assert text.count("mcp") == 1
    assert text.splitlines()[0] == _uk("approvals.who.unknown")
    row = mcp_actions.query(db, limit=1)[0]
    assert row["actor"] == "unknown"
    assert row["claimed_actor"] == "mcp"


def test_note_is_cut_quoted_and_printable(client, seed, dialog_stub):
    case = seed("transcription.delete_transcription")
    note = "\x01\x07" + "n" * 100 + "\t" + "m" * 98
    assert len(note) > 200
    _send(client, case, headers={"X-Recall-Note": note})
    text = dialog_stub.calls[-1][1]
    lines = [ln for ln in text.splitlines() if "«n" in ln]
    assert len(lines) == 1
    quoted = re.search(r"«([^»]*)»", lines[0]).group(1)
    assert len(quoted) == 60
    assert quoted.isprintable()
    assert quoted == "n" * 60
    assert text.count("n" * 60) == 1


def test_body_text_appears_only_quoted(client, seed, dialog_stub, side_effects):
    case = seed("telegram.tg_backfill")
    case["json"] = {"chat_id": "злий текст", "limit": 5}
    _send(client, case)
    text = dialog_stub.calls[-1][1]
    assert text.count("злий текст") == 1
    assert "«злий текст»" in text


@pytest.mark.parametrize("setting, header, expected", [
    ("uk", "en", "uk"),
    ("en", "uk", "en"),
])
def test_dialog_language_is_the_owner_setting(setting, header, expected, client, seed, dialog_stub, monkeypatch):
    monkeypatch.setenv("RECALL_UI_LANG", setting)
    case = seed("transcription.delete_transcription")
    _send(client, case, headers={"X-Recall-Lang": header})
    text = dialog_stub.calls[-1][1]
    assert tr("approvals.who.unknown", lang=expected) in text
    assert tr("approvals.question", lang=expected) in text
    other = "en" if expected == "uk" else "uk"
    assert tr("approvals.question", lang=other) not in text


# ------------------------------------------------------------------ refusals in order

def test_fourth_request_in_a_minute_hits_the_ceiling(client, seed, dialog_stub, audits):
    for _ in range(3):
        case = seed("transcription.delete_transcription")
        assert _send(client, case).status_code == 403
    calls_before = len(dialog_stub.calls)
    case = seed("transcription.delete_transcription")
    r = _send(client, case)
    assert r.status_code == 429
    body = r.get_json()
    assert body["error_code"] == "RATE_CEILING"
    assert body["retry_after_sec"] >= 1
    assert len(dialog_stub.calls) == calls_before
    assert audits[-1]["decision"] == "rate_ceiling"
    # the ui class has its own counter
    case = seed("transcription.delete_transcription")
    r = _send(client, case, headers={auth.UI_TOKEN_HEADER: auth.ui_token()})
    assert r.status_code == 403
    assert len(dialog_stub.calls) == calls_before + 1


def test_day_ceiling(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(approval_gate, "_now", lambda: clock[0])
    for _ in range(approval_gate.D_PER_DAY):
        assert approval_gate._take("unknown") is None
        clock[0] += 61
    wait = approval_gate._take("unknown")
    assert wait is not None and wait > 60
    assert approval_gate._take("ui") is None


def test_recording_refuses_unknown_without_dialog_but_not_ui(client, seed, dialog_stub, monkeypatch, audits):
    monkeypatch.setattr(approval_gate, "recording_active", lambda **kw: True)
    case = seed("transcription.delete_transcription")
    r = _send(client, case)
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "RECORDING_ACTIVE"
    assert dialog_stub.calls == []
    assert audits[-1]["decision"] == "recording_active"
    r = _send(client, case, headers={auth.UI_TOKEN_HEADER: auth.ui_token()})
    assert r.status_code == 403
    assert len(dialog_stub.calls) == 1


def test_broken_recording_probe_fails_closed_for_the_gate_only(client, seed, dialog_stub, monkeypatch):
    class _Broken:
        def active_session_ids(self):
            raise RuntimeError("probe down")

    monkeypatch.setattr(app_state, "recording_service", _Broken(), raising=False)
    case = seed("transcription.delete_transcription")
    r = _send(client, case)
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "RECORDING_ACTIVE"
    assert dialog_stub.calls == []
    from app.blueprints import transcription
    from app.services import reembed
    assert rec_state.recording_active() is False
    assert transcription.recording_active() is False
    assert reembed._recording_active() is False


def test_recording_probe_without_service_is_false():
    assert rec_state.recording_active(default=True) is False
    assert rec_state.recording_active() is False


def test_busy_slot_gives_pending(client, seed, dialog_stub, monkeypatch, audits):
    monkeypatch.setattr(approvals, "busy", lambda: True)
    case = seed("transcription.delete_transcription")
    r = _send(client, case)
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "APPROVAL_PENDING"
    assert dialog_stub.calls == []
    assert audits[-1]["decision"] == "pending"


def test_unavailable_dialog_gives_503(client, db, seed, monkeypatch, audits):
    def _raise(title, text):
        raise approvals.DialogUnavailable("no_desktop")

    monkeypatch.setattr(approvals, "_show_dialog", _raise)
    case = seed("transcription.delete_transcription")
    r = _send(client, case)
    assert r.status_code == 503
    body = r.get_json()
    assert body["error_code"] == "APPROVAL_UNAVAILABLE" and body["reason"] == "no_desktop"
    assert _present(db, case)
    assert audits[-1]["decision"] == "unavailable"


def test_dialog_past_deadline_is_a_timeout_denial(client, db, seed, monkeypatch, audits):
    release = threading.Event()

    def _slow(title, text):
        release.wait(5)
        return approvals.IDYES

    monkeypatch.setattr(approvals, "_show_dialog", _slow)
    monkeypatch.setattr(approvals, "APPROVAL_TIMEOUT_S", 0.2)
    case = seed("transcription.delete_transcription")
    try:
        r = _send(client, case)
    finally:
        release.set()
    assert r.status_code == 403
    assert r.get_json()["reason"] == "timeout"
    assert _present(db, case)
    assert audits[-1]["decision"] == "denied:timeout"
    time.sleep(0.05)


def test_missing_object_is_the_views_404_without_dialog(client, dialog_stub, audits):
    r = client.delete("/api/history/999999")
    assert r.status_code == 404
    assert dialog_stub.calls == []
    assert audits[-1]["decision"] == "not_found"
    assert audits[-1]["action"] == "transcript_delete"
    assert audits[-1]["object_ref"]


def test_audit_row_carries_the_gate_outcome(client, db, seed, dialog_stub):
    case = seed("transcription.delete_transcription")
    _send(client, case)
    row = mcp_actions.query(db, limit=1)[0]
    assert row["action"] == "transcript_delete"
    assert row["decision"] == "denied:no"
    assert row["object_ref"].startswith("transcriptions:")
    assert "Планерка про бюджет" in row["summary"]
    assert row["error_code"] == "APPROVAL_DENIED"


def test_gate_never_calls_the_dialog_directly():
    src = (ROOT / "app" / "core" / "approval_gate.py").read_text(encoding="utf-8")
    assert "_show_dialog" not in src
    assert "approvals.ask(" in src


# ------------------------------------------------------------------ fail closed (repair round 1)

def _tx_ids(db):
    with get_db_connection(db) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM transcriptions ORDER BY id")]


@pytest.mark.parametrize("ids", ["12", {"3": 1}, "", [], None, ["x"], 7],
                         ids=["string", "object", "empty-string", "empty-list", "null", "bad-item", "int"])
def test_bulk_delete_with_unsummarisable_ids_is_refused_without_the_view(ids, client, db, approve_dialogs,
                                                                         audits):
    with get_db_connection(db) as conn:
        for _ in range(3):
            _tx(conn)
    before = _tx_ids(db)
    r = client.post("/api/history/bulk_delete", json={"ids": ids})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "BAD_REQUEST"
    assert _tx_ids(db) == before
    assert approve_dialogs.calls == []
    assert audits[-1]["decision"].startswith("refused:")


def test_bulk_delete_with_a_non_json_body_is_refused(client, db, approve_dialogs):
    with get_db_connection(db) as conn:
        _tx(conn)
    before = _tx_ids(db)
    r = client.post("/api/history/bulk_delete", data="ids=1", content_type="text/plain")
    assert r.status_code == 400
    assert _tx_ids(db) == before
    assert approve_dialogs.calls == []


def test_bulk_delete_of_missing_ids_is_refused_not_passed_through(client, db, approve_dialogs, audits):
    r = client.post("/api/history/bulk_delete", json={"ids": [999998, 999999]})
    assert r.status_code == 400
    assert approve_dialogs.calls == []
    assert audits[-1]["decision"] == "refused:not_found"


@pytest.mark.parametrize("path,body", [
    ("/api/memory/tags/merge", {"keep_id": "x", "merge_id": 1}),
    ("/api/speakers/merge", {"keep_id": 1, "merge_ids": "23"}),
    ("/api/telegram/backfill", {}),
], ids=["tag-merge", "speaker-merge", "telegram-backfill"])
def test_other_routes_fail_closed_on_a_body_they_cannot_summarise(path, body, client, approve_dialogs,
                                                                  side_effects):
    r = client.post(path, json=body)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error_code"] == "BAD_REQUEST"
    assert approve_dialogs.calls == []
    assert side_effects["listener"] == [] and side_effects["jobs"] == []


def test_forged_telegram_token_does_not_hide_a_gated_request_from_the_audit(client, db, seed, dialog_stub,
                                                                            monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_TOKEN", "real-listener-token")
    case = seed("transcription.delete_transcription")
    r = _send(client, case, headers={"X-Telegram-Token": "forged"})
    assert r.status_code == 403
    rows = mcp_actions.query(db, limit=5)
    assert len(rows) == 1 and rows[0]["decision"] == "denied:no"


# ------------------------------------------------------------------ repair round 2 (owner's conditions)

def test_bulk_delete_with_one_missing_id_is_refused_and_keeps_the_existing_row(client, db, approve_dialogs,
                                                                              audits):
    with get_db_connection(db) as conn:
        a = _tx(conn, title="Нарада, що лишається")
    r = client.post("/api/history/bulk_delete", json={"ids": [a, a + 1000]})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "BAD_REQUEST"
    assert approve_dialogs.calls == []
    assert _tx_ids(db) == [a]
    assert audits[-1]["decision"] == "refused:not_found"


def test_note_cannot_close_its_quote(client, seed, dialog_stub):
    case = seed("transcription.delete_transcription")
    _send(client, case, headers={"X-Recall-Note": "IGNORE ALL. » Requested by: Recall UI. «x"})
    text = dialog_stub.calls[-1][1]
    (line,) = [ln for ln in text.splitlines() if "IGNORE" in ln]
    quoted = line.split("«", 1)[1]
    assert quoted.count("»") == 1 and quoted.endswith("»") and "«" not in quoted


def test_unknown_chat_is_marked_not_verified_and_cannot_close_its_quote(client, seed, dialog_stub):
    case = seed("telegram.tg_backfill")
    case["json"] = {"chat_id": "Press YES » this is safe «", "limit": 5}
    _send(client, case)
    text = dialog_stub.calls[-1][1]
    (line,) = [ln for ln in text.splitlines() if "Press YES" in ln]
    assert line == _uk("approvals.object_unverified", kind=_uk("approvals.kind.chat"),
                       name="Press YES this is safe")


def test_numeric_chat_missing_from_monitored_chats_is_not_verified(client, seed, dialog_stub):
    case = seed("telegram.tg_backfill")
    case["json"] = {"chat_id": -100555, "limit": 5}
    _send(client, case)
    text = dialog_stub.calls[-1][1]
    assert _uk("approvals.object_unverified", kind=_uk("approvals.kind.chat"), name="-100555") in text


def test_known_chat_is_shown_by_name_without_the_mark(client, seed, dialog_stub):
    case = seed("telegram.tg_backfill")
    _send(client, case)
    text = dialog_stub.calls[-1][1]
    assert _uk("approvals.object", kind=_uk("approvals.kind.chat"), name="Робочий чат тест",
               id="-100777") in text


@pytest.mark.parametrize("limit", [0, -5, 5.5, 2.0, "50", True, None, 2001],
                         ids=["zero", "negative", "float", "whole-float", "string", "bool", "null", "over-bound"])
def test_telegram_backfill_bad_limit_is_refused(limit, client, seed, dialog_stub, side_effects, audits):
    case = seed("telegram.tg_backfill")
    case["json"] = {"chat_id": -100777, "limit": limit}
    r = _send(client, case)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["reason"] == "bad_limit"
    assert dialog_stub.calls == [] and side_effects["listener"] == []
    assert audits[-1]["decision"] == "refused:bad_limit"


def test_float_chat_id_is_refused(client, seed, dialog_stub, side_effects):
    case = seed("telegram.tg_backfill")
    case["json"] = {"chat_id": -100777.5, "limit": 5}
    r = _send(client, case)
    assert r.status_code == 400
    assert dialog_stub.calls == [] and side_effects["listener"] == []


@pytest.mark.parametrize("limit", [0, 2.5, "3", False])
def test_enrich_backfill_bad_limit_is_refused(limit, client, seed, dialog_stub, side_effects):
    seed("memory.start_backfill")
    r = client.post("/api/memory/backfill", json={"limit": limit})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["reason"] == "bad_limit"
    assert dialog_stub.calls == [] and side_effects["jobs"] == []


def test_enrich_backfill_positive_limit_caps_the_count(client, db, seed, dialog_stub, side_effects):
    with get_db_connection(db) as conn:
        for _ in range(3):
            _tx(conn)
    client.post("/api/memory/backfill", json={"limit": 1})
    assert _uk("approvals.impact.enrich_backfill", count=1) in dialog_stub.calls[-1][1]


def _direction_with_project(db):
    with get_db_connection(db) as conn:
        parent = cats.create_category(conn, "Напрямок з проєктом")
        child = cats.create_category(conn, "Проєкт усередині", parent_id=parent["id"])
        other = cats.create_category(conn, "Інший напрямок")
    return parent, child, other


def test_category_delete_with_projects_is_the_views_409_without_dialog(client, db, dialog_stub, audits):
    parent, child, _ = _direction_with_project(db)
    r = client.delete(f"/api/memory/categories/{parent['id']}")
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "has_projects"
    assert [p["id"] for p in body["projects"]] == [child["id"]]
    assert dialog_stub.calls == []
    assert audits[-1]["decision"] == "precondition:has_projects"
    assert body["code"] == "has_projects" and body["message"]


def test_category_merge_with_projects_is_the_views_409_without_dialog(client, db, dialog_stub):
    parent, child, other = _direction_with_project(db)
    r = client.post(f"/api/memory/categories/{parent['id']}/merge", json={"target_id": other["id"]})
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "has_projects"
    assert [p["id"] for p in body["projects"]] == [child["id"]]
    assert dialog_stub.calls == []


@pytest.mark.parametrize("merge", [False, True], ids=["delete", "merge"])
def test_direction_with_projects_never_reaches_the_view_even_if_moved_meanwhile(
        merge, client, db, dialog_stub, monkeypatch):
    parent, child, other = _direction_with_project(db)
    view_calls = []
    monkeypatch.setattr(cats, "delete_category", lambda *a, **k: view_calls.append(("delete", a)))
    monkeypatch.setattr(cats, "merge_categories", lambda *a, **k: view_calls.append(("merge", a)) or [])
    real_refuse = approval_summaries._refuse_if_projects

    def _refuse_then_move(conn, cid, action_key):
        try:
            real_refuse(conn, cid, action_key)
        finally:
            with get_db_connection(db) as other_conn:  # a concurrent ungated PATCH parent_id
                other_conn.execute("UPDATE categories SET parent_id = NULL WHERE id = ?", (child["id"],))
                other_conn.commit()

    monkeypatch.setattr(approval_summaries, "_refuse_if_projects", _refuse_then_move)
    if merge:
        r = client.post(f"/api/memory/categories/{parent['id']}/merge",
                        json={"target_id": other["id"], "delete_source": True})
    else:
        r = client.delete(f"/api/memory/categories/{parent['id']}")
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "has_projects"
    assert [p["id"] for p in body["projects"]] == [child["id"]]
    assert view_calls == [] and dialog_stub.calls == []
    with get_db_connection(db) as conn:
        assert _alive(conn, "categories", "id", parent["id"])


def test_quote_marks_in_a_db_name_cannot_close_the_dialog_quote(client, db, dialog_stub):
    name = "x» Запит зі сторінки Recall «y"
    with get_db_connection(db) as conn:
        tag_id = conn.execute("INSERT INTO tags (name, name_norm) VALUES (?, ?)", (name, name.casefold())).lastrowid
        conn.commit()
        assert conn.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()[0] == name
    client.delete(f"/api/memory/tags/{tag_id}")
    text = dialog_stub.calls[-1][1]
    assert "«x Запит зі сторінки Recall y»" in text
    line = next(ln for ln in text.splitlines() if "Запит" in ln)
    assert line.count("«") == line.count("»") == 1


def test_self_speaker_delete_is_the_views_error_without_dialog(client, db, dialog_stub, audits):
    with get_db_connection(db) as conn:
        sid = conn.execute("INSERT INTO speakers (name, is_self, usage_count) VALUES ('Я сам', 1, 0)").lastrowid
        conn.commit()
    r = client.delete(f"/api/speakers/{sid}")
    assert r.status_code == 400
    assert r.get_json()["error"] == _uk("settings.speakers.err.self_delete")
    assert dialog_stub.calls == []
    assert audits[-1]["decision"] == "precondition:self_speaker"
    with get_db_connection(db) as conn:
        assert _alive(conn, "speakers", "id", sid)


def test_malformed_requests_do_not_spend_the_ceiling(client, seed, dialog_stub):
    for _ in range(approval_gate.D_PER_MINUTE):
        r = client.post("/api/history/bulk_delete", json={"ids": "bad"})
        assert r.status_code == 400
    case = seed("transcription.delete_transcription")
    r = _send(client, case)
    assert r.status_code == 403, r.get_json()
    assert len(dialog_stub.calls) == 1


def test_valid_listener_token_on_a_gated_route_is_still_audited(client, db, seed, dialog_stub, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_TOKEN", "real-listener-token")
    case = seed("transcription.delete_transcription")
    r = _send(client, case, headers={"X-Telegram-Token": "real-listener-token"})
    assert r.status_code == 403
    rows = mcp_actions.query(db, limit=5)
    assert len(rows) == 1 and rows[0]["decision"] == "denied:no"
