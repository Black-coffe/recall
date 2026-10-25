"""Audit store: repository + request hooks (mcp-foundations-b-02)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask, Response, g, jsonify

from app.core import audit_hooks, auth
from app.db.migrations import init_database
from app.repositories import mcp_actions
from app.services import retention


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "audit.db")
    init_database(path)
    return path


def _rows(path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM mcp_actions ORDER BY id").fetchall()
    finally:
        conn.close()


def _iso(days_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(**kw) -> dict:
    base = {"actor": "unknown", "method": "POST", "path": "/api/вигадка/1", "http_status": 200}
    base.update(kw)
    return base


# ------------------------------------------------------------------ repository

def test_insert_returns_id_and_query_newest_first(db):
    a = mcp_actions.insert(db, _row(summary="видалити запис"))
    b = mcp_actions.insert(db, _row(summary="злити теги"))
    assert b > a
    rows = mcp_actions.query(db)
    assert [r["id"] for r in rows] == [b, a]
    assert rows[1]["summary"] == "видалити запис"


def test_query_filters(db):
    mcp_actions.insert(db, _row(at=_iso(5), actor="ui", path="/api/теги/1", http_status=200,
                                action="taxonomy_delete", decision="approved"))
    mcp_actions.insert(db, _row(at=_iso(1), actor="unknown", path="/api/спікери/2", http_status=403,
                                action="speaker_delete", decision="denied:no"))
    mcp_actions.insert(db, _row(at=_iso(0.1), actor="unknown", path="/api/спікери/3", http_status=500))
    assert len(mcp_actions.query(db, since=_iso(2))) == 2
    assert len(mcp_actions.query(db, actor="ui")) == 1
    assert len(mcp_actions.query(db, decision="denied:no")) == 1
    assert len(mcp_actions.query(db, action="speaker_delete")) == 1
    assert len(mcp_actions.query(db, path_prefix="/api/спікери")) == 2
    assert len(mcp_actions.query(db, status_min=400)) == 2
    newest = mcp_actions.query(db)[0]["id"]
    assert len(mcp_actions.query(db, before_id=newest)) == 2


def test_query_limit_is_clamped(db):
    for i in range(205):
        mcp_actions.insert(db, _row(summary=f"запис {i}"))
    assert len(mcp_actions.query(db, limit=500)) == 200
    assert len(mcp_actions.query(db, limit=0)) == 1
    assert len(mcp_actions.query(db, limit=-5)) == 1


def test_purge_older_than_deletes_only_old_rows(db):
    mcp_actions.insert(db, _row(at=_iso(100)))
    mcp_actions.insert(db, _row(at=_iso(91)))
    mcp_actions.insert(db, _row(at=_iso(10)))
    assert retention.purge_mcp_actions(db) == 2
    assert len(_rows(db)) == 1
    assert mcp_actions.purge_older_than(db, days=90) == 0


# ----------------------------------------------------------------------- hooks

@pytest.fixture()
def app(db):
    app = Flask(__name__)
    app.config["DATABASE"] = db

    @app.before_request
    def _rid():
        g.request_id = "abc123"

    audit_hooks.register(app)

    @app.route("/api/x", methods=["GET", "POST"])
    def x():
        return jsonify(ok=True)

    @app.route("/api/missing", methods=["POST"])
    def missing():
        return jsonify(error_code="NOT_FOUND"), 404

    @app.route("/api/boom", methods=["POST"])
    def boom():
        raise RuntimeError("boom")

    @app.route("/api/stream", methods=["POST"])
    def stream():
        return Response((c for c in ("a", "b")), mimetype="application/json")

    @app.route("/api/skipped", methods=["POST"])
    def skipped():
        return jsonify(ok=True)

    @app.route("/api/audited", methods=["POST"])
    def audited():
        return jsonify(ok=True)

    @app.route("/other", methods=["POST"])
    def other():
        return jsonify(ok=True)

    return app


def test_post_is_audited_as_unknown(app, db):
    r = app.test_client().post("/api/x")
    assert r.status_code == 200
    (row,) = _rows(db)
    assert row["method"] == "POST" and row["path"] == "/api/x"
    assert row["endpoint"] == "x" and row["actor"] == "unknown"
    assert row["request_id"] == "abc123" and row["http_status"] == 200
    assert row["duration_ms"] >= 0 and row["error_code"] is None
    assert row["decision"] is None and row["action"] is None


def test_ui_token_gives_ui_actor(app, db):
    app.test_client().post("/api/x", headers={auth.UI_TOKEN_HEADER: auth.ui_token()})
    assert _rows(db)[0]["actor"] == "ui"


def test_claimed_labels_do_not_change_actor(app, db):
    app.test_client().post("/api/x", headers={"X-Recall-Actor": "mcp", "X-Recall-Tool": "t"})
    row = _rows(db)[0]
    assert (row["claimed_actor"], row["claimed_tool"], row["actor"]) == ("mcp", "t", "unknown")


def test_error_code_from_json_body(app, db):
    app.test_client().post("/api/missing")
    row = _rows(db)[0]
    assert row["http_status"] == 404 and row["error_code"] == "NOT_FOUND"


def test_raising_view_leaves_one_500_row(app, db):
    app.test_client().post("/api/boom")
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["http_status"] == 500 and rows[0]["error_code"] == "INTERNAL_ERROR"


def test_raising_view_with_propagation_leaves_one_row(app, db):
    app.config["PROPAGATE_EXCEPTIONS"] = True
    with pytest.raises(RuntimeError):
        app.test_client().post("/api/boom")
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["error_code"] == "INTERNAL_ERROR"


def test_get_listener_and_non_api_are_not_audited(app, db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_TOKEN", "t0k")
    c = app.test_client()
    c.get("/api/x")
    c.post("/api/x", headers={"X-Telegram-Token": "t0k"})
    c.post("/other")
    assert _rows(db) == []


@pytest.mark.parametrize("header", ["forged", "t0k-plus", " "])
def test_forged_telegram_token_is_still_audited(app, db, monkeypatch, header):
    monkeypatch.setenv("TELEGRAM_CONTROL_TOKEN", "t0k")
    app.test_client().post("/api/x", headers={"X-Telegram-Token": header})
    assert len(_rows(db)) == 1


def test_audit_skip_endpoint_is_not_audited(app, db, monkeypatch):
    monkeypatch.setattr(audit_hooks, "_AUDIT_SKIP", {"skipped"})
    c = app.test_client()
    c.post("/api/skipped")
    assert _rows(db) == []
    c.post("/api/audited")
    assert len(_rows(db)) == 1


def test_no_body_or_query_string_is_stored(app, db):
    app.test_client().post("/api/x?token=секрет-в-запиті",
                           json={"password": "секрет-в-тілі"})
    row = _rows(db)[0]
    blob = " ".join(str(v) for v in tuple(row))
    assert "секрет-в-запиті" not in blob and "секрет-в-тілі" not in blob
    assert "token=" not in blob and "password" not in blob


def test_streamed_response_has_status_and_no_error_code(app, db):
    app.test_client().post("/api/stream")
    row = _rows(db)[0]
    assert row["http_status"] == 200 and row["error_code"] is None


def test_gate_fields_are_stored(app, db):
    @app.before_request
    def _gate():
        g.recall_audit = {"action": "transcript_delete", "object_ref": "transcription:7",
                          "summary": "Видалити «Дзвінок з Андрієм»", "decision": "denied:no"}

    app.test_client().post("/api/x")
    row = _rows(db)[0]
    assert row["action"] == "transcript_delete"
    assert row["object_ref"] == "transcription:7"
    assert row["summary"] == "Видалити «Дзвінок з Андрієм»"
    assert row["decision"] == "denied:no"


def test_failing_insert_is_logged_and_response_unchanged(app, tmp_path, caplog):
    app.config["DATABASE"] = str(tmp_path / "no_such_dir" / "x.db")
    with caplog.at_level("WARNING"):
        r = app.test_client().post("/api/x")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert any("could not write mcp_actions" in m for m in caplog.messages)


# ----------------------------------------------------------------------- repair round 2

@pytest.mark.parametrize("kind", ["anthropic_key", "mail"])
def test_connect_save_is_audited_with_the_token_redacted(kind, db, tmp_path, monkeypatch):
    from app.blueprints.connect import connect_bp
    from app.services import connect_links, settings_secrets
    from app.services import mail_accounts as ma

    secret = "sk-ant-api03-ZZauditsecretZZ1234"
    monkeypatch.setattr(settings_secrets, "save_anthropic_key", lambda s: None)
    monkeypatch.setattr(ma, "connect_account", lambda path, address, pw: {"labels": []})
    a = Flask(__name__, root_path=str(tmp_path),
              template_folder=str(Path(__file__).resolve().parent.parent / "templates"))
    a.config.update(TESTING=True, DATABASE=db)
    a.register_blueprint(connect_bp)
    audit_hooks.register(a)
    connect_links._links.clear()
    link = connect_links.issue(kind, subject="ivan.test@example.com" if kind == "mail" else None)
    r = a.test_client().post(f"/connect/{kind}/{link['token']}", data={"secret": secret},
                             base_url="http://127.0.0.1:5050")
    assert r.status_code == 200
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["path"] == f"/connect/{kind}/*"
    blob = " ".join(str(v) for v in tuple(rows[0]))
    assert link["token"] not in blob and secret not in blob


def test_connect_unknown_kind_path_is_fully_redacted(db, tmp_path):
    from app.blueprints.connect import connect_bp
    a = Flask(__name__, root_path=str(tmp_path))
    a.config.update(TESTING=True, DATABASE=db)
    a.register_blueprint(connect_bp)
    audit_hooks.register(a)
    a.test_client().post("/connect/tok-in-kind/abc", base_url="http://127.0.0.1:5050")
    (row,) = _rows(db)
    assert row["path"] == "/connect/*"


def test_listener_token_does_not_skip_a_gated_route(db, monkeypatch):
    from app.core import approval_gate
    monkeypatch.setenv("TELEGRAM_CONTROL_TOKEN", "t0k")
    a = Flask(__name__)
    a.config["DATABASE"] = db

    @a.route("/api/history/<int:transcription_id>", methods=["DELETE"], endpoint="gated")
    def gated(transcription_id):
        return jsonify(ok=True)

    monkeypatch.setitem(approval_gate._D_ROUTES, "gated", "transcript_delete")
    audit_hooks.register(a)
    a.test_client().delete("/api/history/5", headers={"X-Telegram-Token": "t0k"})
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["endpoint"] == "gated"
