"""`transcript_ai` service + `POST /api/transcription/<id>/ai-job`: cache, jobs, the recording guard.

Spec `mcp-pipeline`, story 03. Claude is faked at the service seam (`text_polishing.*` and
`enrichment.enrich_transcription` as seen from `transcript_ai`); the job queue is real, driven by
a manual executor so a test decides when a queued job runs. Fixtures are Cyrillic.
"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import Future

import pytest
from flask import Flask

from app import state
from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database
from app.services import embeddings, enrichment, transcript_ai
from app.services.job_queue import JobQueue

TEXT = "Андрій: бюджет Барселони на третій квартал, Олена погоджує до пʼятниці"
POLISHED = "Андрій: Бюджет Барселони на третій квартал. Олена погоджує до пʼятниці."


class _ManualExecutor:
    def __init__(self):
        self.pending: list = []

    def submit(self, fn):
        fut = Future()
        self.pending.append((fn, fut))
        return fut

    def run_all(self):
        futs = []
        while self.pending:
            fn, fut = self.pending.pop(0)
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except Exception as e:  # noqa: BLE001 - the job queue already recorded it
                fut.set_exception(e)
            futs.append(fut)
        return futs


class _Claude:
    """Fake text_polishing seam; counts every paid call."""

    def __init__(self):
        self.calls: list = []

    def polish_transcript(self, text, model=None):
        self.calls.append("polish")
        return {"polished_text": POLISHED, "model": "claude-test", "input_tokens": 10,
                "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0}

    def polish_diarized_transcript(self, segments, speaker_map, model=None):
        return self.polish_transcript("", model=model)

    def summarize_transcript(self, text=None, diarized_text=None, model=None):
        self.calls.append("summarize")
        return {"summary": "Обговорили бюджет", "key_points": ["бюджет"], "action_items": ["Олена погоджує"],
                "model": "claude-test", "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0}

    def analyze_sentiment(self, text, model=None):
        self.calls.append("sentiment")
        return {"speakers": [{"name": "Андрій", "tone": "позитивний"}], "model": "claude-test"}

    def translate_transcript(self, text=None, target_lang=None, diarized_text=None, model=None):
        self.calls.append("translate")
        return {"translated_text": "Andrii: the Barcelona budget", "model": "claude-test",
                "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0}

    def extract_topics(self, text=None, diarized_text=None, model=None):
        self.calls.append("topics")
        return {"topics": ["бюджет", "Барселона"], "model": "claude-test"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "ai.db")
    init_database(db)
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config.update(TESTING=True, DATABASE=db)
    ex = _ManualExecutor()
    queue = JobQueue(executor=ex, slow_executor=ex)
    monkeypatch.setattr(state, "job_queue", queue, raising=False)
    monkeypatch.setattr(state, "metrics", None, raising=False)
    claude = _Claude()
    tp = transcript_ai.text_polishing
    monkeypatch.setattr(tp, "is_available", lambda: True)
    for name in ("polish_transcript", "polish_diarized_transcript", "summarize_transcript",
                 "analyze_sentiment", "translate_transcript", "extract_topics"):
        monkeypatch.setattr(tp, name, getattr(claude, name))
    rec = {"on": False}
    monkeypatch.setattr(transcript_ai, "recording_active", lambda: rec["on"])
    enrich_calls: list = []

    def _enrich(db_path, tid, *, model=None, force=False, effort="medium"):
        enrich_calls.append(tid)
        with sqlite3.connect(db_path) as c:
            c.execute("UPDATE transcriptions SET enriched_at = CURRENT_TIMESTAMP, enriched_model = 'claude-test', "
                      "enrichment_version = ?, embedded_at = CURRENT_TIMESTAMP, embedding_model = ?, "
                      "embedding_version = ? WHERE id = ?",
                      (enrichment.ENRICHMENT_VERSION, embeddings.EMBED_MODEL, embeddings.EMBED_VERSION, tid))
        return {"transcription_id": tid, "status": "done", "card": {"status": "enriched"},
                "embed": {"status": "embedded"}}

    monkeypatch.setattr(enrichment, "enrich_transcription", _enrich)
    monkeypatch.setattr(enrichment, "any_available", lambda: True)
    return type("Env", (), {"app": app, "client": app.test_client(), "db": db, "ex": ex, "queue": queue,
                            "claude": claude, "rec": rec, "enrich_calls": enrich_calls, "mp": monkeypatch})()


def _tx(env, **cols) -> int:
    base = {"source_type": "file", "source_name": "Дзвінок", "transcript_text": TEXT, "language": "uk",
            "segments": json.dumps([{"start": 0, "end": 2, "text": TEXT}], ensure_ascii=False)}
    base.update(cols)
    keys = ", ".join(base)
    with sqlite3.connect(env.db) as c:
        cur = c.execute(f"INSERT INTO transcriptions ({keys}) VALUES ({', '.join('?' * len(base))})",
                        tuple(base.values()))
        return cur.lastrowid


def _diarized(env) -> int:
    segs = [{"start": 0, "end": 2, "text": "бюджет Барселони", "speaker": "SPEAKER_00"}]
    tid = _tx(env, segments=json.dumps(segs, ensure_ascii=False))
    with sqlite3.connect(env.db) as c:
        sid = c.execute("INSERT INTO speakers (name) VALUES ('Андрій')").lastrowid
        c.execute("INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) "
                  "VALUES (?, 'SPEAKER_00', ?)", (tid, sid))
    return tid


def _col(env, tid, col):
    with sqlite3.connect(env.db) as c:
        return c.execute(f"SELECT {col} FROM transcriptions WHERE id = ?", (tid,)).fetchone()[0]


def _job(env, tid, **body):
    return env.client.post(f"/api/transcription/{tid}/ai-job", json=body)


# ============================================================ contract + cache


def test_contract_names():
    assert transcript_ai.KINDS == ("polish", "summarize", "sentiment", "translate", "topics", "enrich")
    assert callable(transcript_ai.cached) and callable(transcript_ai.run)
    assert issubclass(transcript_ai.TranscriptAIError, Exception)


def test_cached_reads_each_column_without_claude(env):
    tid = _tx(env, polished_text=POLISHED,
              summary_json=json.dumps({"summary": "Бюджет", "key_points": [], "action_items": []},
                                      ensure_ascii=False),
              sentiment_json=json.dumps({"speakers": [{"name": "Андрій"}], "model": "m"}, ensure_ascii=False),
              translations_json=json.dumps({"en": {"text": "Budget", "model": "m"}}),
              topics_json=json.dumps({"topics": ["бюджет"], "model": "m"}, ensure_ascii=False))
    c = lambda k, **kw: transcript_ai.cached(k, tid, db_path=env.db, **kw)  # noqa: E731
    assert c("polish") == {"polished_text": POLISHED}
    assert c("summarize")["summary"] == "Бюджет"
    assert c("sentiment")["speakers"] == [{"name": "Андрій"}]
    assert c("translate", target_lang="en") == {"translated_text": "Budget", "target_lang": "en", "model": "m"}
    assert c("translate", target_lang="ru") is None
    assert c("topics") == {"topics": ["бюджет"]}
    assert c("enrich") is None
    assert env.claude.calls == []


def test_cached_enrich_only_when_done(env):
    tid = _tx(env, topics_json=json.dumps({"topics": ["бюджет"]}, ensure_ascii=False))
    assert transcript_ai.cached("enrich", tid, db_path=env.db) is None
    with sqlite3.connect(env.db) as c:
        c.execute("UPDATE transcriptions SET enriched_at = '2026-10-01', enriched_model = 'm', "
                  "enrichment_version = ?, embedded_at = '2026-10-01', embedding_model = ?, "
                  "embedding_version = ? WHERE id = ?",
                  (enrichment.ENRICHMENT_VERSION, embeddings.EMBED_MODEL, embeddings.EMBED_VERSION, tid))
    hit = transcript_ai.cached("enrich", tid, db_path=env.db)
    assert hit["status"] == "done" and hit["topics"] == ["бюджет"]
    assert env.enrich_calls == []


def test_sync_routes_keep_contract(env):
    tid = _tx(env)
    r = env.client.post(f"/api/transcription/{tid}/polish", json={})
    body = r.get_json()
    assert r.status_code == 200 and body["cached"] is False and body["polished_text"] == POLISHED
    assert body["usage"]["input_tokens"] == 10
    assert _col(env, tid, "polished_text") == POLISHED
    r = env.client.post(f"/api/transcription/{tid}/polish", json={})
    assert r.get_json() == {"success": True, "cached": True, "polished_text": POLISHED}
    r = env.client.post(f"/api/transcription/{tid}/polish", json={"force": True})
    assert r.get_json()["cached"] is False
    assert env.claude.calls == ["polish", "polish"]

    r = env.client.post(f"/api/transcription/{tid}/topics", json={})
    assert r.get_json() == {"success": True, "cached": False, "topics": ["бюджет", "Барселона"],
                            "model": "claude-test"}
    r = env.client.post(f"/api/transcription/{tid}/translate", json={"target_lang": "en"})
    assert r.get_json()["translated_text"].startswith("Andrii")
    r = env.client.post(f"/api/transcription/{tid}/translate", json={"target_lang": "uk"})
    assert r.status_code == 400 and "error_code" not in r.get_json()
    r = env.client.post(f"/api/transcription/{tid}/sentiment", json={})
    assert r.status_code == 400  # not diarized
    r = env.client.post(f"/api/transcription/{tid}/summarize", json={})
    assert r.get_json()["summary"] == "Обговорили бюджет"
    r = env.client.post("/api/transcription/999999/summarize", json={})
    assert r.status_code == 404 and r.get_json() == {"success": False, "error": r.get_json()["error"]}


def test_sync_sentiment_on_diarized(env):
    tid = _diarized(env)
    r = env.client.post(f"/api/transcription/{tid}/sentiment", json={})
    assert r.status_code == 200 and r.get_json()["speakers"][0]["name"] == "Андрій"


def test_sync_missing_key_is_400_without_code(env):
    env.mp.setattr(transcript_ai.text_polishing, "is_available", lambda: False)
    tid = _tx(env)
    for kind in ("polish", "summarize", "sentiment", "topics"):
        r = env.client.post(f"/api/transcription/{tid}/{kind}", json={})
        assert r.status_code == 400 and set(r.get_json()) == {"success", "error"}


# ============================================================ /ai-job


@pytest.mark.parametrize("kind", ["polish", "summarize", "topics"])
def test_ai_job_cache_hit_never_calls_claude(env, kind):
    tid = _tx(env, polished_text=POLISHED,
              summary_json=json.dumps({"summary": "Бюджет", "key_points": [], "action_items": []},
                                      ensure_ascii=False),
              topics_json=json.dumps({"topics": ["бюджет"]}, ensure_ascii=False))
    r = _job(env, tid, kind=kind)
    body = r.get_json()
    assert r.status_code == 200 and body["cached"] is True and body["status"] == "done"
    assert body["result"] == transcript_ai.cached(kind, tid, db_path=env.db)
    assert env.queue.list(kind="transcript_ai") == []
    env.ex.run_all()
    assert env.claude.calls == []


def test_ai_job_enrich_cache_hit_never_calls_enrich(env):
    tid = _tx(env)
    with sqlite3.connect(env.db) as c:
        c.execute("UPDATE transcriptions SET enriched_at = '2026-10-01', enrichment_version = ?, "
                  "embedded_at = '2026-10-01', embedding_model = ?, embedding_version = ? WHERE id = ?",
                  (enrichment.ENRICHMENT_VERSION, embeddings.EMBED_MODEL, embeddings.EMBED_VERSION, tid))
    r = _job(env, tid, kind="enrich")
    assert r.status_code == 200 and r.get_json()["cached"] is True
    assert env.enrich_calls == []


@pytest.mark.parametrize("kind,body", [
    ("polish", {}), ("summarize", {}), ("topics", {}), ("translate", {"target_lang": "en"}),
])
def test_ai_job_miss_runs_job_once(env, kind, body):
    tid = _tx(env)
    r = _job(env, tid, kind=kind, **body)
    assert r.status_code == 202
    out = r.get_json()
    assert out["cached"] is False and out["job_id"]
    job = env.queue.get(out["job_id"])
    assert job.kind == "transcript_ai" and job.meta["transcription_id"] == tid and job.meta["kind"] == kind
    [fut] = env.ex.run_all()
    res = fut.result()
    assert job.state == "completed" and res["kind"] == kind and res["transcription_id"] == tid
    assert env.claude.calls == [kind]
    sync = env.client.post(f"/api/transcription/{tid}/{kind}", json=body).get_json()
    assert sync["cached"] is True
    # The job's result is what the sync route answered on its miss: the cached projection matches.
    for key, val in sync.items():
        if key not in ("success", "cached"):
            assert res["result"][key] == val
    assert env.claude.calls == [kind]


def test_ai_job_result_equals_sync_body(env):
    tid_a, tid_b = _tx(env), _tx(env)
    r = _job(env, tid_a, kind="topics")
    [fut] = env.ex.run_all()
    sync = env.client.post(f"/api/transcription/{tid_b}/topics", json={}).get_json()
    sync.pop("success"), sync.pop("cached")
    assert fut.result()["result"] == sync
    assert r.status_code == 202


def test_ai_job_enrich_miss_and_recording_guard(env):
    tid = _tx(env)
    env.rec["on"] = True
    r = _job(env, tid, kind="enrich")
    assert r.status_code == 409 and r.get_json()["error_code"] == "RECORDING_ACTIVE"
    assert env.queue.list(kind="transcript_ai") == []

    env.rec["on"] = False
    r = _job(env, tid, kind="enrich")
    assert r.status_code == 202
    job = env.queue.get(r.get_json()["job_id"])
    env.rec["on"] = True  # a recording starts before the job runs
    env.ex.run_all()
    assert job.state == "failed" and job.error_code == "RECORDING_ACTIVE"
    assert env.enrich_calls == [] and _col(env, tid, "enriched_at") is None

    env.rec["on"] = False
    r = _job(env, tid, kind="enrich")
    env.ex.run_all()
    assert env.enrich_calls == [tid]
    assert env.queue.get(r.get_json()["job_id"]).state == "completed"


@pytest.mark.parametrize("kind,body", [("polish", {}), ("translate", {"target_lang": "ru"})])
def test_other_kinds_not_blocked_by_recording(env, kind, body):
    tid = _tx(env)
    env.rec["on"] = True
    r = _job(env, tid, kind=kind, **body)
    assert r.status_code == 202
    [fut] = env.ex.run_all()
    assert fut.result()["kind"] == kind


@pytest.mark.parametrize("body", [
    {"kind": "rewrite"}, {}, {"kind": "translate"}, {"kind": "translate", "target_lang": "de"},
    {"kind": "polish", "force": True}, {"kind": "polish", "model": "claude-opus"},
])
def test_ai_job_bad_arguments(env, body):
    tid = _tx(env)
    r = env.client.post(f"/api/transcription/{tid}/ai-job", json=body)
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_ARGUMENT"
    assert env.queue.list(kind="transcript_ai") == []


def test_ai_job_unknown_record(env):
    r = _job(env, 999999, kind="polish")
    assert r.status_code == 404 and r.get_json()["error_code"] == "NOT_FOUND"


def test_ai_job_missing_key_matches_sync(env):
    env.mp.setattr(transcript_ai.text_polishing, "is_available", lambda: False)
    tid = _tx(env)
    sync = env.client.post(f"/api/transcription/{tid}/topics", json={})
    r = _job(env, tid, kind="topics")
    assert r.status_code == sync.status_code == 400
    assert r.get_json()["error"] == sync.get_json()["error"]
    assert r.get_json()["error_code"] == "BAD_REQUEST"
    assert env.queue.list(kind="transcript_ai") == []


def test_ai_job_dedupes_live_job(env):
    tid = _tx(env)
    first = _job(env, tid, kind="translate", target_lang="en").get_json()["job_id"]
    r = _job(env, tid, kind="translate", target_lang="en")
    assert r.status_code == 200 and r.get_json() == {"success": True, "cached": False, "created": False,
                                                      "job_id": first}
    other = _job(env, tid, kind="translate", target_lang="ru")
    assert other.status_code == 202 and other.get_json()["job_id"] != first
    env.ex.run_all()
    assert env.claude.calls == ["translate", "translate"]
