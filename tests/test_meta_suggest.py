"""Title and description suggestions from the local model (spec recording-meta-suggest, story 01).

Offline: `local_llm` is replaced by a fake with the signature of the real `generate_json`
(memory note graceful-degradation-hides-test-fake-drift), the DB is a temporary one with migrations.
"""
from __future__ import annotations

import inspect
import json
import sqlite3

import pytest
from flask import Flask

from app import state
from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database
from app.services import local_llm, meta_suggest, record_meta

SUMMARY = {
    "summary": "Робоча сесія з Сергієм щодо налаштування Claude Code для 1С: часовий пояс, "
               "права на вимкнені обʼєкти, архітектура памʼяті.",
    "key_points": ["Часовий пояс — за Португалією", "Оновлення вікі відклали"],
}


class FakeLLM:
    """Replaces `local_llm.generate_json` + `availability`; records every call."""

    def __init__(self, data=None, error=None, available=(True, "OK")):
        self.calls: list[dict] = []
        self.data = data if data is not None else {
            "titles": ["Сергій: Claude Code для 1С", "Налаштування 1С і памʼять Claude",
                       "Часовий пояс і права в 1С"],
            "descriptions": ["Розібрали конфігурацію 1С.", "Побудували памʼять для Claude.",
                             "Домовились про часовий пояс."],
        }
        self.error = error
        self.available = available

    def generate_json(self, prompt, *, schema=None, system=None, max_tokens=512,
                      temperature=0.0, num_ctx=None, model=None, timeout=None, retries=1,
                      keep_alive=None):
        self.calls.append({"prompt": prompt, "system": system, "model": model,
                           "schema": schema, "timeout": timeout, "keep_alive": keep_alive})
        if self.error:
            raise self.error
        return {"data": self.data, "raw": {"model": model or "fake"}}

    def availability(self, force=False):
        return self.available


def test_fake_matches_real_signature():
    real = inspect.signature(local_llm.generate_json)
    fake = inspect.signature(FakeLLM().generate_json)
    assert list(real.parameters) == list(fake.parameters)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "suggest.db")
    init_database(path)
    return path


@pytest.fixture
def fake(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(local_llm, "generate_json", f.generate_json)
    monkeypatch.setattr(local_llm, "availability", f.availability)
    return f


@pytest.fixture(autouse=True)
def no_recording(monkeypatch):
    monkeypatch.setattr(state, "recording_service", None, raising=False)


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db
    return app.test_client()


def _add(db, text="Так, Серёх, давай по часовому поясу. По Киеву или по Португалии?",
         summary=None, language="ru"):
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, language, "
        "summary_json) VALUES ('recording', 'Запис 2026-10-14 08:54', ?, ?, ?)",
        (text, language, json.dumps(summary, ensure_ascii=False) if summary else None),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


class _Rec:
    def __init__(self, ids):
        self._ids = ids

    def active_session_ids(self):
        return list(self._ids)


# ---------- model input ----------

def test_summary_is_the_basis_when_present(client, db, fake):
    tid = _add(db, summary=SUMMARY)
    r = client.post(f"/api/history/{tid}/suggest-meta")
    assert r.status_code == 200
    body = r.get_json()
    assert body["basis"] == "summary" and body["summary_ready"] is True
    assert "Португалією" in fake.calls[0]["prompt"]
    assert "Серёх" not in fake.calls[0]["prompt"]


def test_excerpts_when_no_summary(client, db, fake):
    text = "початок розмови. " * 400 + "СЕРЕДИНА " + "хвіст розмови. " * 400 + "Давай, Серёх."
    tid = _add(db, text=text, language="uk")
    body = client.post(f"/api/history/{tid}/suggest-meta").get_json()
    assert body["basis"] == "excerpts" and body["summary_ready"] is False
    prompt = fake.calls[0]["prompt"]
    assert "[початок]" in prompt and "[кінець]" in prompt and "Давай, Серёх." in prompt
    assert len(prompt) < len(text)
    assert "українська" in fake.calls[0]["system"]


def test_model_comes_from_setting(client, db, fake, monkeypatch):
    monkeypatch.setenv("LOCAL_TITLE_MODEL", "qwen2.5:7b-instruct")
    tid = _add(db, summary=SUMMARY)
    client.post(f"/api/history/{tid}/suggest-meta")
    assert fake.calls[0]["model"] == "qwen2.5:7b-instruct"


def test_model_is_unloaded_right_after_by_default(client, db, fake, monkeypatch):
    monkeypatch.delenv("LOCAL_TITLE_KEEPALIVE", raising=False)
    tid = _add(db, summary=SUMMARY)
    client.post(f"/api/history/{tid}/suggest-meta")
    assert fake.calls[0]["keep_alive"] == "0"


def test_generate_json_passes_keep_alive_to_ollama(monkeypatch):
    sent = {}
    monkeypatch.setattr(local_llm, "resolve_model", lambda m=None: m or "x")
    monkeypatch.setattr(local_llm, "_post",
                        lambda path, payload, timeout: sent.update(payload) or {"response": "{}"})
    local_llm.generate_json("промпт", keep_alive="0")
    assert sent["keep_alive"] == "0"


# ---------- variant cleaning ----------

def test_variants_are_cleaned():
    long_title = "Д" * 300
    out = meta_suggest.clean_variants(
        ["«Сергій і 1С»", "сергій і 1с", "  ", 42, long_title, "Четверта", "Пʼята"],
        record_meta.TITLE_MAX)
    assert out[0] == "Сергій і 1С"
    assert len(out) == 3
    assert len(out[1]) == record_meta.TITLE_MAX
    assert "Четверта" in out


def test_long_description_is_cut_to_limit(client, db, fake):
    fake.data = {"titles": ["Назва"], "descriptions": ["Опис " * 2000]}
    tid = _add(db, summary=SUMMARY)
    body = client.post(f"/api/history/{tid}/suggest-meta").get_json()
    assert len(body["descriptions"][0]) <= record_meta.DESCRIPTION_MAX


# ---------- errors ----------

def test_missing_record_404(client, fake):
    assert client.post("/api/history/999/suggest-meta").status_code == 404
    assert fake.calls == []


def test_empty_transcript_422(client, db, fake):
    tid = _add(db, text="")
    assert client.post(f"/api/history/{tid}/suggest-meta").status_code == 422
    assert fake.calls == []


def test_ollama_down_503_with_reason(client, db, fake):
    fake.available = (False, "Ollama недоступний на http://localhost:11434")
    tid = _add(db, summary=SUMMARY)
    r = client.post(f"/api/history/{tid}/suggest-meta")
    assert r.status_code == 503
    assert "Ollama недоступний" in r.get_json()["error"]
    assert fake.calls == []


def test_llm_error_503_without_traceback(client, db, fake):
    fake.error = local_llm.LocalLLMError("The model did not return valid JSON")
    tid = _add(db, summary=SUMMARY)
    r = client.post(f"/api/history/{tid}/suggest-meta")
    assert r.status_code == 503
    assert "Traceback" not in r.get_data(as_text=True)


def test_empty_variants_503(client, db, fake):
    fake.data = {"titles": [""], "descriptions": []}
    tid = _add(db, summary=SUMMARY)
    assert client.post(f"/api/history/{tid}/suggest-meta").status_code == 503


def test_suggest_error_texts_come_from_the_catalog():
    from app.core.i18n import tr
    assert tr("svc_embed.suggest.empty", lang="en") == "The model returned empty variants - try again"
    assert tr("svc_embed.suggest.empty", lang="uk") == "Модель повернула порожні варіанти — спробуйте ще раз"
    assert tr("svc_embed.meta.title.too_long", lang="en", limit=200) == \
        "The title is too long: at most 200 characters"


# ---------- a recording is in progress: the model is not touched ----------

def test_recording_active_409_and_no_model_call(client, db, fake, monkeypatch):
    monkeypatch.setattr(state, "recording_service", _Rec(["rec_7aba448a2c394b79"]))
    tid = _add(db, summary=SUMMARY)
    r = client.post(f"/api/history/{tid}/suggest-meta", json={"recording_active": False})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "RECORDING_ACTIVE"
    assert fake.calls == []


def test_recording_finished_allows_again(client, db, fake, monkeypatch):
    monkeypatch.setattr(state, "recording_service", _Rec([]))
    tid = _add(db, summary=SUMMARY)
    assert client.post(f"/api/history/{tid}/suggest-meta").status_code == 200
    assert len(fake.calls) == 1


def test_endpoint_writes_nothing(client, db, fake):
    tid = _add(db, summary=SUMMARY)
    client.post(f"/api/history/{tid}/suggest-meta")
    conn = sqlite3.connect(db)
    title, desc = conn.execute(
        "SELECT title, description FROM transcriptions WHERE id = ?", (tid,)).fetchone()
    conn.close()
    assert title is None and desc is None
