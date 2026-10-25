"""Intake routes and their D gating (mcp-pipeline story 02).

Documents by path (`ingest-paths`), the `job_id` of `import-folder`, the `reparse-job` twin, the
dialog in front of folder import, re-parse and a Claude description of video frames, the
YouTube info body guard and the non-UI `force` of the audio download. The app wires the real
blueprints behind auth -> approval gate -> audit hooks over a temp DB; the dialog is the conftest
stub (No unless `approve_dialogs`). Fixtures are Cyrillic and invented.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from flask import Flask

from app import state as app_state
from app.blueprints import documents as documents_mod
from app.blueprints.audio_library import audio_bp
from app.blueprints.documents import documents_bp
from app.blueprints.transcription import transcription_bp
from app.blueprints.youtube import youtube_bp
from app.core import approval_gate, audit_hooks, auth
from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import approval_summaries, enrichment, import_roots, video_analysis

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


class _Job:
    def __init__(self, jid):
        self.id = jid


class FakeQueue:
    """Records submissions; the task functions are run by hand."""

    def __init__(self):
        self.submitted = []

    def submit(self, kind, fn, *args, job_id=None, meta=None, **kw):
        jid = job_id or f"job-{len(self.submitted) + 1}"
        self.submitted.append({"kind": kind, "fn": fn, "meta": meta or {}, "id": jid})
        return _Job(jid)

    def list(self, **kw):
        return []

    def kinds(self):
        return [s["kind"] for s in self.submitted]


class _Progress:
    def set(self, *a, **kw):
        pass


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "pipeline_intake.db")
    init_database(path)
    return path


@pytest.fixture
def queue(monkeypatch):
    q = FakeQueue()
    monkeypatch.setattr(app_state, "job_queue", q, raising=False)
    return q


@pytest.fixture
def roots(tmp_path, monkeypatch):
    root = tmp_path / "дозволені"
    root.mkdir()
    monkeypatch.setenv("RECALL_IMPORT_ROOTS", str(root))
    return root


@pytest.fixture
def docs_dir(tmp_path):
    return tmp_path / "документи-архіву"


@pytest.fixture
def client(db, tmp_path, monkeypatch, queue, docs_dir):
    monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "1")
    monkeypatch.setenv("RECALL_UI_LANG", "uk")
    monkeypatch.setattr(app_state, "recording_service", None, raising=False)
    monkeypatch.setattr(app_state, "download_progress", _Progress(), raising=False)
    monkeypatch.setattr(enrichment, "any_available", lambda: False)
    a = Flask(__name__, root_path=str(tmp_path))
    a.config.update(TESTING=True, DATABASE=db, DOCUMENTS_FOLDER=str(docs_dir),
                    MAX_CONTENT_LENGTH=1024 * 1024)
    for bp in (documents_bp, transcription_bp, audio_bp, youtube_bp):
        a.register_blueprint(bp)
    auth.register(a)
    approval_gate.register(a)
    audit_hooks.register(a)
    approval_gate.reset_counters()
    yield a.test_client()
    approval_gate.reset_counters()


def _uk(key, **kw):
    return tr(key, lang="uk", **kw)


def _doc(folder: Path, name: str, text: str) -> Path:
    p = folder / name
    p.write_text(f"# {name}\n\n{text}", encoding="utf-8")
    return p


def _rows(db):
    with get_db_connection(db) as c:
        return c.execute("SELECT id, source_name, file_path, transcript_text FROM transcriptions "
                         "ORDER BY id").fetchall()


def _slots(actor):
    return len(approval_gate._counts.get(actor, ()))


# ------------------------------------------------------------------ import roots

def test_import_roots_delegate_to_the_service(roots, monkeypatch):
    assert documents_mod._import_roots() == import_roots.roots()
    monkeypatch.setattr(import_roots, "roots", lambda: ["sentinel-корінь"])
    assert documents_mod._import_roots() == ["sentinel-корінь"]


# ------------------------------------------------------------------ ingest-paths

def test_six_paths_is_bulk_limit(client, roots, queue):
    paths = [str(_doc(roots, f"нотатка-{i}.md", f"текст {i}")) for i in range(6)]
    r = client.post("/api/documents/ingest-paths", json={"paths": paths})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "BULK_LIMIT"
    assert queue.submitted == []


@pytest.mark.parametrize("body", [{}, {"paths": []}, {"paths": "звіт.md"}, {"paths": [5]}],
                         ids=["missing", "empty", "string", "not-a-path"])
def test_bad_paths_body_is_invalid_argument(body, client, roots, queue):
    r = client.post("/api/documents/ingest-paths", json=body)
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "INVALID_ARGUMENT"
    assert queue.submitted == []


def test_one_path_outside_the_roots_refuses_all_and_starts_nothing(client, roots, tmp_path, queue):
    good = _doc(roots, "план.md", "план робіт")
    outside_dir = tmp_path / "чужа папка"
    outside_dir.mkdir()
    outside = _doc(outside_dir, "секрет.md", "не для імпорту")
    r = client.post("/api/documents/ingest-paths", json={"paths": [str(good), str(outside)]})
    assert r.status_code == 403
    body = r.get_json()
    assert body["error_code"] == "PATH_NOT_ALLOWED" and body["path"] == str(outside)
    assert queue.submitted == []
    assert _rows(client.application.config["DATABASE"]) == []


def test_non_document_extension_is_unsupported(client, roots, queue):
    audio = roots / "дзвінок.mp3"
    audio.write_bytes(b"ID3")
    r = client.post("/api/documents/ingest-paths", json={"paths": [str(audio)]})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "UNSUPPORTED_EXTENSION"
    assert queue.submitted == []


def test_too_large_file_is_refused(client, roots, queue):
    big = roots / "великий звіт.md"
    big.write_text("ж" * (1024 * 1024), encoding="utf-8")
    r = client.post("/api/documents/ingest-paths", json={"paths": [str(big)]})
    assert r.status_code == 413
    assert r.get_json()["error_code"] == "FILE_TOO_LARGE"
    assert queue.submitted == []


def test_five_good_paths_start_one_job_with_a_result_per_path(client, db, roots, queue, docs_dir):
    first = _doc(roots, "протокол наради.md", "Обговорили бюджет фонду")
    _doc_copy = roots / "копія протоколу.md"
    _doc_copy.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")  # same content
    others = [_doc(roots, f"документ {n}.md", f"Окремий текст номер {n}") for n in ("один", "два", "три")]
    paths = [str(first), str(_doc_copy)] + [str(p) for p in others]

    r = client.post("/api/documents/ingest-paths", json={"paths": paths})
    assert r.status_code == 202, r.get_json()
    body = r.get_json()
    assert body["success"] is True and body["total"] == 5 and body["job_id"]
    (sub,) = queue.submitted
    assert sub["kind"] == "doc_import" and sub["id"] == body["job_id"]
    assert sub["meta"]["actor"] == "unknown"

    result = sub["fn"](_Job(body["job_id"]))
    items = result["items"]
    assert [os.path.normcase(i["path"]) for i in items] == \
        [os.path.normcase(os.path.realpath(p)) for p in paths]
    assert [i["status"] for i in items] == ["created", "duplicate", "created", "created", "created"]
    assert all(i["transcription_id"] for i in items)
    assert items[1]["transcription_id"] == items[0]["transcription_id"]

    rows = _rows(db)
    assert len(rows) == 4
    copied = sorted(os.listdir(docs_dir))
    assert len(copied) == 4  # the duplicate is not copied
    for row in rows:
        assert os.path.dirname(os.path.realpath(row["file_path"])) == os.path.realpath(docs_dir)
    assert {row["source_name"] for row in rows} == {"протокол наради.md", "документ один.md",
                                                    "документ два.md", "документ три.md"}


def test_ingest_paths_is_not_gated(client, roots, queue, dialog_stub):
    r = client.post("/api/documents/ingest-paths", json={"paths": [str(_doc(roots, "лист.md", "текст"))]})
    assert r.status_code == 202
    assert dialog_stub.calls == []


# ------------------------------------------------------------------ import-folder / reparse

def _folder(roots, name="вхідні"):
    folder = roots / name
    folder.mkdir()
    _doc(folder, "нотатки.md", "План бюджету на квартал")
    return folder


def _document(db, tmp_path, title="Звіт про бюджет"):
    original = tmp_path / "звіт оригінал.md"
    original.write_text("# Звіт\n\nНовий текст звіту.", encoding="utf-8")
    with get_db_connection(db) as c:
        tid = c.execute(
            "INSERT INTO transcriptions (source_type, source_name, original_filename, title, file_path, "
            "transcript_text) VALUES ('document', 'звіт оригінал.md', 'звіт оригінал.md', ?, ?, "
            "'старий текст')", (title, str(original))).lastrowid
        c.commit()
    return tid


def _text(db, tid):
    with get_db_connection(db) as c:
        return c.execute("SELECT transcript_text FROM transcriptions WHERE id = ?", (tid,)).fetchone()[0]


def test_import_folder_answers_its_job_id_beside_todays_keys(client, roots, queue, approve_dialogs):
    r = client.post("/api/documents/import-folder", json={"path": str(_folder(roots))}, headers=UI)
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"success": True, "started": True, "total": 1, "channel": "doc_import",
                    "job_id": queue.submitted[0]["id"]}
    assert queue.submitted[0]["meta"]["actor"] == "ui"


def test_reparse_job_answers_202_and_runs_the_same_reparse(client, db, tmp_path, queue, approve_dialogs):
    tid = _document(db, tmp_path)
    r = client.post(f"/api/documents/{tid}/reparse-job", headers=UI)
    assert r.status_code == 202
    body = r.get_json()
    assert body == {"success": True, "job_id": queue.submitted[0]["id"]}
    sub = queue.submitted[0]
    assert sub["kind"] == "doc_import" and sub["meta"]["transcription_id"] == tid
    assert _text(db, tid) == "старий текст"
    result = sub["fn"](_Job(body["job_id"]))
    assert result["transcription_id"] == tid and result["doc_type"] == "md"
    assert "Новий текст звіту" in _text(db, tid)


def test_reparse_job_unknown_record_is_not_found(client, queue):
    r = client.post("/api/documents/999999/reparse-job", headers=UI)
    assert r.status_code == 404
    assert r.get_json()["error_code"] == "NOT_FOUND"
    assert queue.submitted == []


def test_sync_reparse_response_is_unchanged(client, db, tmp_path, approve_dialogs):
    tid = _document(db, tmp_path)
    r = client.post(f"/api/documents/{tid}/reparse", headers=UI)
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"success", "transcription_id", "doc_type", "page_count", "char_count",
                         "needs_ocr", "enrichment"}
    assert body["transcription_id"] == tid and body["enrichment"] == {"status": "unavailable"}
    r = client.post("/api/documents/999999/reparse", headers=UI)
    assert r.status_code == 404
    assert r.get_json() == {"success": False, "error": _uk("library.doc.err.not_found")}


# ------------------------------------------------------------------ gate tables

def test_gate_tables_hold_the_three_actions():
    assert {"document_import_folder", "document_reparse", "video_describe_claude"} <= set(approval_gate.D_ACTIONS)
    assert approval_gate._D_ROUTES["documents.import_folder"] == "document_import_folder"
    assert approval_gate._D_ROUTES["documents.reparse_document"] == "document_reparse"
    assert approval_gate._D_ROUTES["documents.reparse_job"] == "document_reparse"
    action, _pred = approval_gate._CONDITIONAL_D_ROUTES["transcription.analyze_video_endpoint"]
    assert action == "video_describe_claude"
    for endpoint in ("documents.import_folder", "documents.reparse_document", "documents.reparse_job",
                     "transcription.analyze_video_endpoint"):
        assert endpoint in approval_summaries.BUILDERS


# ------------------------------------------------------------------ D gating of documents

def _gated_requests(db, tmp_path, roots):
    tid = _document(db, tmp_path)
    folder = _folder(roots)
    return [
        ("/api/documents/import-folder", {"path": str(folder)}, 200),
        (f"/api/documents/{tid}/reparse", None, 200),
        (f"/api/documents/{tid}/reparse-job", None, 202),
    ], tid


@pytest.mark.parametrize("index", [0, 1, 2], ids=["import-folder", "reparse", "reparse-job"])
def test_non_ui_call_is_denied_on_no(index, client, db, tmp_path, roots, queue, dialog_stub):
    reqs, tid = _gated_requests(db, tmp_path, roots)
    path, body, _status = reqs[index]
    r = client.post(path, json=body) if body is not None else client.post(path)
    assert r.status_code == 403
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1
    assert queue.kinds() == [] and _text(db, tid) == "старий текст"


@pytest.mark.parametrize("index", [0, 1, 2], ids=["import-folder", "reparse", "reparse-job"])
def test_non_ui_call_runs_the_view_on_yes(index, client, db, tmp_path, roots, queue, approve_dialogs):
    reqs, tid = _gated_requests(db, tmp_path, roots)
    path, body, status = reqs[index]
    r = client.post(path, json=body) if body is not None else client.post(path)
    assert r.status_code == status, r.get_json()
    assert len(approve_dialogs.calls) == 1
    assert "doc_import" in queue.kinds() or "Новий текст звіту" in _text(db, tid)


@pytest.mark.parametrize("index", [0, 1, 2], ids=["import-folder", "reparse", "reparse-job"])
def test_ui_token_call_gets_the_dialog_with_the_ui_line(index, client, db, tmp_path, roots, queue, dialog_stub):
    reqs, tid = _gated_requests(db, tmp_path, roots)
    path, body, status = reqs[index]
    r = client.post(path, json=body, headers=UI) if body is not None else client.post(path, headers=UI)
    # the ui token is not a dialog exemption on a plain D route: the stub says No
    assert r.status_code == 403 and len(dialog_stub.calls) == 1
    assert dialog_stub.calls[0][1].splitlines()[0] == _uk("approvals.who.ui")


@pytest.mark.parametrize("path", ["/api/documents/999999/reparse", "/api/documents/999999/reparse-job"])
def test_unknown_record_is_the_views_404_without_a_dialog(path, client, dialog_stub):
    r = client.post(path)
    assert r.status_code == 404
    assert dialog_stub.calls == []


def test_folder_outside_the_roots_is_the_views_403_without_a_dialog(client, roots, tmp_path, queue,
                                                                     dialog_stub):
    outside = tmp_path / "чужа"
    outside.mkdir()
    _doc(outside, "секрет.md", "не для імпорту")
    r = client.post("/api/documents/import-folder", json={"path": str(outside)})
    assert r.status_code == 403
    assert r.get_json()["error_code"] == "PATH_NOT_ALLOWED"
    assert dialog_stub.calls == [] and queue.submitted == []
    assert _slots("unknown") == 0


# ------------------------------------------------------------------ analyze-video (ask 8)

@pytest.fixture
def video(db, monkeypatch):
    with get_db_connection(db) as c:
        tid = c.execute("INSERT INTO transcriptions (source_type, source_name, title) "
                        "VALUES ('recording', 'екран.mp4', 'Демо інтерфейсу')").lastrowid
        c.commit()
    monkeypatch.setattr(video_analysis, "resolve_recording_video", lambda *a: {"primary_video_path": "x"})
    return tid


@pytest.mark.parametrize("body", [{"vision_backend": "claude", "describe": True}, {"vision_backend": "claude"}],
                         ids=["describe-true", "no-describe"])
def test_non_ui_claude_description_raises_the_dialog(body, client, video, queue, dialog_stub, monkeypatch):
    r = client.post(f"/api/transcription/{video}/analyze-video", json=body)
    assert r.status_code == 403
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1
    text = dialog_stub.calls[0][1]
    assert _uk("approvals.action.video_describe_claude") in text
    assert "Демо інтерфейсу" in text
    assert queue.submitted == []


def test_claude_setting_gates_a_bare_describe(client, video, queue, dialog_stub, monkeypatch):
    from app.services import video_vision
    monkeypatch.setattr(video_vision, "VISION_BACKEND", "claude")
    r = client.post(f"/api/transcription/{video}/analyze-video", json={"describe": True})
    assert r.status_code == 403
    assert len(dialog_stub.calls) == 1 and queue.submitted == []
    # an explicit local backend stays local under the same setting
    r = client.post(f"/api/transcription/{video}/analyze-video",
                    json={"describe": True, "vision_backend": "local"})
    assert r.status_code == 200 and len(dialog_stub.calls) == 1


def test_non_ui_claude_description_runs_on_yes(client, video, queue, approve_dialogs):
    r = client.post(f"/api/transcription/{video}/analyze-video", json={"vision_backend": "claude"})
    assert r.status_code == 200 and r.get_json()["job_id"]
    assert queue.kinds() == ["video_analysis"]


@pytest.mark.parametrize("body, headers", [
    ({"vision_backend": "claude", "describe": True}, UI),
    ({"vision_backend": "claude"}, UI),
    ({"vision_backend": "local", "describe": True}, {}),
    ({"vision_backend": "local", "describe": True}, UI),
    ({"vision_backend": "claude", "describe": False}, {}),
    ({}, {}),
], ids=["ui-claude", "ui-claude-implicit", "unknown-local", "ui-local", "unknown-claude-no-describe", "empty"])
def test_ui_claude_and_any_local_run_without_dialog_or_slot(body, headers, client, video, queue, dialog_stub):
    r = client.post(f"/api/transcription/{video}/analyze-video", json=body, headers=headers)
    assert r.status_code == 200 and r.get_json()["job_id"]
    assert dialog_stub.calls == []
    assert _slots("ui") == 0 and _slots("unknown") == 0


def test_predicate_error_gates(client, video, queue, dialog_stub, monkeypatch):
    def _boom(view_args):
        raise RuntimeError("predicate down")

    monkeypatch.setitem(approval_gate._CONDITIONAL_D_ROUTES, "transcription.analyze_video_endpoint",
                        ("video_describe_claude", _boom))
    r = client.post(f"/api/transcription/{video}/analyze-video", json={"vision_backend": "local"})
    assert r.status_code == 403
    assert len(dialog_stub.calls) == 1 and queue.submitted == []


def test_non_object_body_gates(client, video, queue, dialog_stub):
    r = client.post(f"/api/transcription/{video}/analyze-video", json=["claude"])
    assert r.status_code == 403
    assert len(dialog_stub.calls) == 1 and queue.submitted == []


def test_recording_refuses_non_ui_claude_before_any_dialog(client, video, queue, dialog_stub, monkeypatch):
    monkeypatch.setattr(approval_gate, "recording_active", lambda **kw: True)
    r = client.post(f"/api/transcription/{video}/analyze-video", json={"vision_backend": "claude"})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "RECORDING_ACTIVE"
    assert dialog_stub.calls == [] and queue.submitted == []
    # local analysis during a recording stays as today
    r = client.post(f"/api/transcription/{video}/analyze-video", json={"vision_backend": "local"})
    assert r.status_code == 200 and dialog_stub.calls == []


def test_no_video_is_the_views_answer_without_a_dialog(client, video, dialog_stub, monkeypatch):
    monkeypatch.setattr(video_analysis, "resolve_recording_video", lambda *a: None)
    r = client.post(f"/api/transcription/{video}/analyze-video", json={"vision_backend": "claude"})
    assert r.status_code == 200
    assert r.get_json()["error_code"] == "NO_VIDEO"
    assert dialog_stub.calls == []


# ------------------------------------------------------------------ dialog texts

@pytest.mark.parametrize("lang", ["uk", "en"])
def test_dialog_texts_come_from_the_db_and_body(lang, client, db, tmp_path, roots, video, queue, dialog_stub,
                                                monkeypatch):
    monkeypatch.setenv("RECALL_UI_LANG", lang)
    with get_db_connection(db) as c:
        cid = c.execute("INSERT INTO categories (name) VALUES ('Фонд розвитку')").lastrowid
        c.commit()
    folder = roots / ("дуже довга назва папки з документами фонду " * 3).strip()
    folder.mkdir()
    _doc(folder, "нотатки.md", "текст")
    tid = _document(db, tmp_path, title="Звіт для фонду")

    client.post("/api/documents/import-folder", json={"path": str(folder), "recursive": True, "category_id": cid})
    client.post(f"/api/documents/{tid}/reparse-job")
    client.post(f"/api/transcription/{video}/analyze-video", json={"vision_backend": "claude"})
    folder_text, reparse_text, video_text = (call[1] for call in dialog_stub.calls)

    cut = approval_summaries._cut(os.path.realpath(folder), 60)
    assert len(cut) == 60 and cut.endswith("…")
    assert tr("approvals.object_noid", lang=lang, kind=tr("approvals.kind.folder_recursive", lang=lang),
              name=cut) in folder_text
    assert tr("approvals.extra.direction", lang=lang, name="Фонд розвитку") in folder_text
    assert tr("approvals.impact.document_import_folder", lang=lang, count=1) in folder_text
    assert tr("approvals.action.document_import_folder", lang=lang) in folder_text

    assert tr("approvals.object", lang=lang, kind=tr("approvals.kind.record", lang=lang),
              name="Звіт для фонду", id=tid) in reparse_text
    assert tr("approvals.extra.source_file", lang=lang, name="звіт оригінал.md") in reparse_text
    assert tr("approvals.action.document_reparse", lang=lang) in reparse_text

    assert tr("approvals.object", lang=lang, kind=tr("approvals.kind.record", lang=lang),
              name="Демо інтерфейсу", id=video) in video_text
    assert tr("approvals.impact.video_describe_claude", lang=lang) in video_text
    for text in (folder_text, reparse_text, video_text):
        assert tr("approvals.question", lang=lang) in text


# ------------------------------------------------------------------ audio / YouTube

def test_youtube_info_without_a_body_is_invalid_argument(client):
    r = client.post("/api/youtube/info")
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "INVALID_ARGUMENT"
    r = client.post("/api/youtube/info", data="url=x", content_type="text/plain")
    assert r.status_code == 400


def _audio_row(db):
    with get_db_connection(db) as c:
        aid = c.execute(
            "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path) VALUES "
            "('https://youtu.be/dQw4w9WgXcQ', 'dQw4w9WgXcQ', 'Лекція про архіви', 'downloads/лекція.mp3')"
        ).lastrowid
        c.commit()
    return aid


def test_non_ui_force_cannot_redownload(client, db, queue):
    aid = _audio_row(db)
    body = {"url": "https://youtu.be/dQw4w9WgXcQ", "force": True}
    r = client.post("/api/audio/download", json=body)
    assert r.status_code == 200
    assert r.get_json()["status"] == "exists" and r.get_json()["download_id"] == aid
    assert queue.submitted == []
    r = client.post("/api/audio/download", json=body, headers=UI)
    assert r.get_json()["status"] == "started"
    assert queue.kinds() == ["audio_download"]
