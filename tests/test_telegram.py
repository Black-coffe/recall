"""Tests for Telegram ingestion (Phase 17).

Light — without torch/whisper: we test the listener's pure helpers, the shared-secret,
path confinement and the blueprint's HTTP endpoints on a temporary DB (a minimal
Flask, without the heavy app.py). Transcription/enrichment are not touched (mocked).

Run:
    .venv/Scripts/python.exe -m pytest tests/test_telegram.py -v
"""
import asyncio
import json
import os
import types

import pytest
from flask import Flask

import telegram_common
import telegram_listener as L
from app.blueprints import telegram as tg
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import telegram_link_repair as link_repair


def _msg(**kw):
    """A fake message: only the given attributes, the rest → None via getattr."""
    return types.SimpleNamespace(**kw)


async def _noop_sleep(*_a, **_k):
    """A replacement for asyncio.sleep in pause tests: we check the logic, not the time."""
    return None


# ============================================================
# Listener: _detect_kind
# ============================================================

class TestDetectKind:
    def test_text(self):
        assert L._detect_kind(_msg(message="привіт", voice=None, document=None)) == "text"

    def test_empty_text_is_none(self):
        assert L._detect_kind(_msg(message="", document=None, photo=None)) is None

    def test_voice(self):
        assert L._detect_kind(_msg(voice=object(), message="")) == "voice"

    def test_audio(self):
        assert L._detect_kind(_msg(audio=object(), message="")) == "audio"

    def test_photo(self):
        assert L._detect_kind(_msg(photo=object(), message="підпис")) == "photo"

    def test_video(self):
        assert L._detect_kind(_msg(video=object(), message="")) == "video"

    def test_video_note(self):
        assert L._detect_kind(_msg(video_note=object(), message="")) == "video"

    def test_sticker_skipped(self):
        assert L._detect_kind(_msg(sticker=object(), message="")) is None

    def test_document_image_mime_is_photo(self):
        doc = _msg(mime_type="image/png")
        assert L._detect_kind(_msg(document=doc, message="")) == "photo"

    def test_document_pdf_is_document(self):
        doc = _msg(mime_type="application/pdf")
        assert L._detect_kind(_msg(document=doc, message="")) == "document"

    def test_document_audio_mime_is_audio(self):
        doc = _msg(mime_type="audio/ogg")
        assert L._detect_kind(_msg(document=doc, message="")) == "audio"

    def test_document_video_mime_is_video(self):
        doc = _msg(mime_type="video/mp4")
        assert L._detect_kind(_msg(document=doc, message="")) == "video"

    def test_document_no_mime_video_extension_is_video(self):
        """tg-media-policy-03: a video sent as a file without a mime — the same mkv/mov
        that Telethon did not give video/*, must not silently fall into 'document'."""
        from telethon.tl.types import DocumentAttributeFilename
        doc = _msg(mime_type=None, attributes=[DocumentAttributeFilename(file_name="запис.mkv")])
        assert L._detect_kind(_msg(document=doc, message="")) == "video"

    def test_document_stripped_mime_video_extension_is_video(self):
        """mime reduced to a generic octet-stream, the extension stays a signal."""
        from telethon.tl.types import DocumentAttributeFilename
        doc = _msg(mime_type="application/octet-stream",
                   attributes=[DocumentAttributeFilename(file_name="clip.MOV")])
        assert L._detect_kind(_msg(document=doc, message="")) == "video"

    def test_document_no_mime_non_video_extension_is_document(self):
        """An extension not in the video list — an ordinary document, as before."""
        from telethon.tl.types import DocumentAttributeFilename
        doc = _msg(mime_type=None, attributes=[DocumentAttributeFilename(file_name="звіт.pdf")])
        assert L._detect_kind(_msg(document=doc, message="")) == "document"

    def test_document_no_mime_no_filename_is_document(self):
        """Neither mime nor filename — degrades to document, as before."""
        doc = _msg(mime_type=None, attributes=[])
        assert L._detect_kind(_msg(document=doc, message="")) == "document"


# ============================================================
# Listener: links / names / types
# ============================================================

class TestChatHelpers:
    def test_link_public_username(self):
        """A public channel/supergroup (negative chat_id) with a username — a link to the username."""
        chat = _msg(username="kyivnews")
        assert L._chat_link(chat, -1009876543210, 7) == "https://t.me/kyivnews/7"

    def test_link_private_channel(self):
        chat = _msg(username=None)
        assert L._chat_link(chat, -1001234567890, 55) == "https://t.me/c/1234567890/55"

    def test_link_user_none(self):
        chat = _msg(username=None)
        assert L._chat_link(chat, 12345, 1) is None

    def test_link_chat_id_none_does_not_raise(self):
        """Telethon returns None if peer_id has none of the fields —
        `chat_id > 0` on None would raise TypeError and bring down _build_payload."""
        chat = _msg(username=None)
        assert L._chat_link(chat, None, 1) is None

    def test_link_private_chat_with_username_is_none(self):
        """A personal chat (positive chat_id) with the counterpart's username —
        has no link to a message, even if the username is filled in."""
        chat = _msg(username="andriy_petrenko", first_name="Андрій", last_name="Петренко")
        assert L._chat_link(chat, 250264900, 515482) is None

    def test_sender_name_first_last(self):
        assert L._sender_name(_msg(first_name="Іван", last_name="Петренко")) == "Іван Петренко"

    def test_sender_name_username_fallback(self):
        assert L._sender_name(_msg(first_name=None, last_name=None, title=None, username="ivan")) == "@ivan"

    def test_sender_name_none(self):
        assert L._sender_name(None) is None

    def test_chat_type(self):
        assert L._chat_type_of(_msg(broadcast=True)) == "channel"
        assert L._chat_type_of(_msg(megagroup=True)) == "group"
        assert L._chat_type_of(_msg(first_name="X")) == "user"

    def test_dialogs_carry_identity_from_the_same_pass(self):
        """/dialogs returns first name, last name, phone and the bot/deleted flags —
        from the same iter_dialogs entity, without a single get_entity per chat.
        Without them the Telegram page could not find a person by number and
        showed a deleted account as a nameless «Chat»."""
        person = _msg(first_name="Юлія", last_name="Бондаренка", username="jbondarenko",
                      phone="380671234567", bot=False, deleted=False)
        gone = _msg(first_name=None, last_name=None, username=None, phone=None, deleted=True)
        channel = _msg(broadcast=True, username="kyivnews", title="Київ")   # without first_name/phone
        rows = [
            _msg(id=167136964, name="Юлія Бондаренка", entity=person, message=None, is_group=False),
            _msg(id=8386421946, name="", entity=gone, message=None, is_group=False),
            _msg(id=-1001234567890, name="Київ", entity=channel, message=None, is_group=False),
        ]

        class _DialogIter:
            def __init__(self, items):
                self._items = list(items)
            def __aiter__(self):
                return self
            async def __anext__(self):
                if not self._items:
                    raise StopAsyncIteration
                return self._items.pop(0)

        class _Client:
            def iter_dialogs(self, limit=None):
                return _DialogIter(rows)
            async def get_entity(self, *_a, **_k):
                raise AssertionError("no request per chat — only entities from iter_dialogs")

        out = asyncio.run(L._ctl_dialogs(_Client(), {"dialog_limit": 500}, lambda: {167136964}))
        by_id = {d["id"]: d for d in out["dialogs"]}

        p = by_id[167136964]
        assert (p["first_name"], p["last_name"], p["phone"], p["username"]) == \
            ("Юлія", "Бондаренка", "380671234567", "jbondarenko")
        assert p["type"] == "user" and p["bot"] is False and p["deleted"] is False
        assert p["monitored"] is True

        assert by_id[8386421946]["deleted"] is True
        assert by_id[8386421946]["phone"] is None

        c = by_id[-1001234567890]
        assert c["type"] == "channel" and c["username"] == "kyivnews"
        assert c["first_name"] is None and c["last_name"] is None and c["phone"] is None
        assert c["bot"] is False and c["deleted"] is False


# ============================================================
# Shared-secret (telegram_common.control_token)
# ============================================================

class TestControlToken:
    def test_stable_and_persisted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELEGRAM_CONTROL_TOKEN", raising=False)
        monkeypatch.setattr(telegram_common, "_TOKEN_FILE", tmp_path / "tok")
        t1 = telegram_common.control_token()
        t2 = telegram_common.control_token()
        assert t1 == t2 and len(t1) == 64
        assert (tmp_path / "tok").read_text(encoding="ascii").strip() == t1

    def test_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CONTROL_TOKEN", "fixed-secret")
        monkeypatch.setattr(telegram_common, "_TOKEN_FILE", tmp_path / "tok")
        assert telegram_common.control_token() == "fixed-secret"
        assert not (tmp_path / "tok").exists()   # env → the file is not created


# ============================================================
# Blueprint endpoints (minimal Flask + temporary DB)
# ============================================================

@pytest.fixture
def tg_app(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_TOKEN", "testtoken")
    db = str(tmp_path / "t.db")
    init_database(db)
    media = tmp_path / "media"
    media.mkdir()
    app = Flask(__name__)
    app.config.update(DATABASE=db, TELEGRAM_CONTROL_HOST="127.0.0.1",
                      TELEGRAM_CONTROL_PORT=5599, TELEGRAM_MEDIA_DIR=str(media))
    app.register_blueprint(tg.telegram_bp)
    return app


class TestChatsEndpoints:
    def test_empty(self, tg_app):
        r = tg_app.test_client().get("/api/telegram/chats").get_json()
        assert r["success"] and r["chats"] == []

    def test_upsert_and_partial_update(self, tg_app):
        c = tg_app.test_client()
        r = c.post("/api/telegram/chats", json={
            "chat_id": -100123, "enabled": True, "title": "Fund", "chat_type": "group"}).get_json()
        assert r["success"] and r["enabled"] == 1
        # partial update: only category → enabled is NOT reset
        r2 = c.post("/api/telegram/chats", json={"chat_id": -100123, "category_id": None}).get_json()
        assert r2["enabled"] == 1
        rows = c.get("/api/telegram/chats").get_json()["chats"]
        assert len(rows) == 1 and rows[0]["title"] == "Fund"

    def test_disable(self, tg_app):
        c = tg_app.test_client()
        c.post("/api/telegram/chats", json={"chat_id": -1, "enabled": True})
        r = c.post("/api/telegram/chats", json={"chat_id": -1, "enabled": False}).get_json()
        assert r["enabled"] == 0

    def test_missing_chat_id(self, tg_app):
        assert tg_app.test_client().post("/api/telegram/chats", json={"enabled": True}).status_code == 400

    def test_status_offline(self, tg_app):
        # no listener on port 5599 → alive False, no 5xx
        r = tg_app.test_client().get("/api/telegram/status").get_json()
        assert r["alive"] is False


class TestChatsToggleEndpoint:
    """T2.6: /api/telegram/chats/toggle — CLI-only twin of /api/telegram/chats,
    protected by the shared-secret (like ingest), not by the general session auth gate."""

    def test_no_token_forbidden(self, tg_app):
        r = tg_app.test_client().post(
            "/api/telegram/chats/toggle", json={"chat_id": -1, "enabled": True})
        assert r.status_code == 403

    def test_toggle_with_token_upserts(self, tg_app):
        c = tg_app.test_client()
        hdr = {"X-Telegram-Token": "testtoken"}
        r = c.post("/api/telegram/chats/toggle",
                   json={"chat_id": -100777, "enabled": True, "title": "Fund",
                         "username": None, "chat_type": "group"},
                   headers=hdr).get_json()
        assert r["success"] and r["enabled"] == 1
        rows = c.get("/api/telegram/chats").get_json()["chats"]
        assert len(rows) == 1 and rows[0]["title"] == "Fund" and rows[0]["enabled"] == 1

    def test_toggle_does_not_clear_category(self, tg_app):
        """category_id is bound through the UI (/api/telegram/chats), CLI
        enable/disable must not erase it."""
        c = tg_app.test_client()
        hdr = {"X-Telegram-Token": "testtoken"}
        # first the UI binds a direction
        c.post("/api/telegram/chats", json={"chat_id": -5, "enabled": True, "category_id": 3})
        # then the CLI disables the chat
        c.post("/api/telegram/chats/toggle",
              json={"chat_id": -5, "enabled": False, "title": "X"}, headers=hdr)
        rows = c.get("/api/telegram/chats").get_json()["chats"]
        row = next(r for r in rows if r["chat_id"] == -5)
        assert row["enabled"] == 0
        assert row["category_id"] == 3

    def test_missing_chat_id(self, tg_app):
        r = tg_app.test_client().post(
            "/api/telegram/chats/toggle", json={"enabled": True},
            headers={"X-Telegram-Token": "testtoken"})
        assert r.status_code == 400


class TestChatTags:
    """Story 05: a tag lives on a chat the same way as a direction — retroactively over
    the whole history and inherited by new messages."""

    @staticmethod
    def _seed(app, monkeypatch):
        """The direction «Фонд Робота» → the project «Акмекорп», a project chat with a couple of
        messages. Returns (direction_id, project_id)."""
        scheduled = []
        monkeypatch.setattr(tg, "_schedule_reembed",
                            lambda ids, reason: scheduled.append((sorted(ids), reason)) or
                            {"mode": "point", "count": len(ids), "stale": 0})
        with get_db_connection(app.config["DATABASE"]) as conn:
            conn.execute("DELETE FROM categories")
            conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                         "VALUES (1, 'Фонд Робота', 'фонд робота', 1, NULL)")
            conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                         "VALUES (2, 'Акмекорп', 'акмекорп', 2, 1)")
            conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                         "category_id) VALUES (-300, 'Акмекорп чат', 'group', 1, 2)")
            conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                         "category_id) VALUES (-900, 'Чужий чат', 'group', 1, NULL)")
            for mid in (1, 2):
                conn.execute(
                    "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                    "tg_chat_id, tg_message_id, category_id) VALUES ('telegram', "
                    "'[TG] Акмекорп чат', 'x', -300, ?, 2)", (mid,))
            conn.commit()
        return scheduled

    def test_filter_by_direction_includes_project_chats_and_tags(self, tg_app, monkeypatch):
        """`?category_id=<direction>` also gives the chats of its projects (contract C2),
        and the row carries the chat's tags."""
        self._seed(tg_app, monkeypatch)
        c = tg_app.test_client()
        c.put("/api/telegram/chats/-300/tags", json={"tags": ["AI"]})

        rows = c.get("/api/telegram/chats?category_id=1").get_json()["chats"]
        assert [r["chat_id"] for r in rows] == [-300], "the project's chat is in the direction's slice"
        assert [t["name"] for t in rows[0]["tags"]] == ["AI"]

        only_project = c.get("/api/telegram/chats?category_id=2").get_json()["chats"]
        assert [r["chat_id"] for r in only_project] == [-300]
        assert c.get("/api/telegram/chats?category_id=none").get_json()["chats"][0][
            "chat_id"] == -900
        assert c.get("/api/telegram/chats?category_id=дурня").status_code == 400

    def test_put_tags_is_retroactive_and_idempotent(self, tg_app, monkeypatch):
        scheduled = self._seed(tg_app, monkeypatch)
        c = tg_app.test_client()
        r = c.put("/api/telegram/chats/-300/tags",
                  json={"tags": ["AI", "інвестори"]}).get_json()
        assert r["success"] and r["records_changed"] == 2
        assert [t["name"] for t in r["tags"]] == ["AI", "інвестори"]
        assert scheduled and scheduled[-1][1] == "chat-tags"

        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT tg.name FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
                "WHERE rt.source = 'chat'")}
        assert names == {"AI", "інвестори"}

        # the repeated same PUT — 0 changes, we do not touch the GPU
        scheduled.clear()
        again = c.put("/api/telegram/chats/-300/tags",
                      json={"tags": ["AI", "інвестори"]}).get_json()
        assert again["records_changed"] == 0
        assert all(not ids for ids, _ in scheduled), \
            "nothing changed — nothing to send to re-embed"

    def test_put_tags_unknown_chat_404(self, tg_app, monkeypatch):
        self._seed(tg_app, monkeypatch)
        r = tg_app.test_client().put("/api/telegram/chats/-777/tags", json={"tags": ["AI"]})
        assert r.status_code == 404

    def test_put_tags_requires_body(self, tg_app, monkeypatch):
        self._seed(tg_app, monkeypatch)
        assert tg_app.test_client().put("/api/telegram/chats/-300/tags",
                                        json={}).status_code == 400

    def test_new_message_inherits_chat_tags(self, tg_app, monkeypatch):
        """A new message in the chat gets both tags with `source='chat'` —
        the same way it inherits the chat's direction."""
        self._seed(tg_app, monkeypatch)
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        c = tg_app.test_client()
        c.put("/api/telegram/chats/-300/tags", json={"tags": ["AI", "інвестори"]})

        r = c.post("/api/telegram/ingest",
                   json={"kind": "text", "text": "свіже повідомлення", "chat_id": -300,
                         "message_id": 77, "chat_title": "Акмекорп чат"},
                   headers={"X-Telegram-Token": "testtoken"}).get_json()
        tid = r["transcription_id"]
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            rows = conn.execute(
                "SELECT tg.name, rt.source FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
                "WHERE rt.transcription_id = ?", (tid,)).fetchall()
            cat = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                               (tid,)).fetchone()[0]
        assert {row[0] for row in rows} == {"AI", "інвестори"}
        assert {row[1] for row in rows} == {"chat"}
        assert cat == 2, "the chat's direction/project is inherited as before"


class TestIngestAuthAndDedup:
    def test_no_token_forbidden(self, tg_app):
        r = tg_app.test_client().post("/api/telegram/ingest",
                                      json={"kind": "text", "text": "hi", "chat_id": -1, "message_id": 1})
        assert r.status_code == 403

    def test_text_persists_and_dedups(self, tg_app, monkeypatch):
        # isolate from the heavy enrichment
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        c = tg_app.test_client()
        body = {"kind": "text", "text": "привіт фонд", "chat_id": -100, "message_id": 5, "chat_title": "Fund"}
        hdr = {"X-Telegram-Token": "testtoken"}
        r = c.post("/api/telegram/ingest", json=body, headers=hdr).get_json()
        assert r["success"] and r.get("transcription_id")
        # the same message again → no duplicate is created
        r2 = c.post("/api/telegram/ingest", json=body, headers=hdr).get_json()
        assert r2.get("duplicate") is True

    def test_document_outside_media_rejected(self, tg_app, monkeypatch):
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        c = tg_app.test_client()
        outside = os.path.join(os.path.dirname(tg_app.config["TELEGRAM_MEDIA_DIR"]), "t.db")
        r = c.post("/api/telegram/ingest",
                   json={"kind": "document", "file_path": outside, "chat_id": -1, "message_id": 9},
                   headers={"X-Telegram-Token": "testtoken"})
        assert r.status_code == 400


class TestCoverage:
    """Wave 1 «See»: is the chat silent or did the listener go deaf.

    Without this «12 chats are silent for over a week» read the same as a dead
    project and as a broken ingest — there was nothing to tell them apart.
    """

    @staticmethod
    def _seed(app, chat_id, *, title="Чат", msgs=(), enabled=1):
        """A monitored chat + its messages in the archive. msgs: [(msg_id, tg_date)]."""
        from app.db.connection import get_db_connection
        with get_db_connection(app.config["DATABASE"]) as conn:
            conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled) "
                         "VALUES (?, ?, 'group', ?)", (chat_id, title, enabled))
            for mid, date in msgs:
                conn.execute(
                    "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                    "tg_chat_id, tg_message_id, tg_date) VALUES ('telegram', ?, 'x', ?, ?, ?)",
                    (f"[TG] {title}", chat_id, mid, date))
            conn.commit()

    @staticmethod
    def _live(app, monkeypatch, dialogs, limit=200):
        monkeypatch.setattr(tg, "_listener_call",
                            lambda *a, **k: (True, {"dialogs": dialogs, "limit": limit}))

    def test_offline_listener_still_returns_archive_half(self, tg_app, monkeypatch):
        self._seed(tg_app, -100777, msgs=[(5, "2026-06-01T10:00:00+00:00")])
        monkeypatch.setattr(tg, "_listener_call",
                            lambda *a, **k: (False, {"error": "listener offline"}))
        r = tg_app.test_client().get("/api/telegram/coverage").get_json()
        assert r["success"] and r["live"] is False
        row = r["chats"][0]
        assert row["archived_msgs"] == 1
        assert row["archived_last_date"].startswith("2026-06-01")
        assert row["status"] == "unknown", "without the listener we pass no verdict"
        assert row["lag_days"] is None

    def test_behind_when_telegram_newer_than_archive(self, tg_app, monkeypatch):
        self._seed(tg_app, -100777, msgs=[(5, "2026-06-01T10:00:00+00:00")])
        self._live(tg_app, monkeypatch, [{"id": -100777, "last_message_id": 42,
                                          "last_message_date": "2026-06-11T10:00:00+00:00",
                                          "unread_count": 7}])
        row = tg_app.test_client().get("/api/telegram/coverage").get_json()["chats"][0]
        assert row["status"] == "behind"
        assert row["lag_days"] == 10
        assert row["behind_messages"] == 37
        assert row["unread_count"] == 7

    def test_ok_when_archive_caught_up(self, tg_app, monkeypatch):
        self._seed(tg_app, -100777, msgs=[(42, "2026-06-11T10:00:00+00:00")])
        self._live(tg_app, monkeypatch, [{"id": -100777, "last_message_id": 42,
                                          "last_message_date": "2026-06-11T10:00:00+00:00"}])
        row = tg_app.test_client().get("/api/telegram/coverage").get_json()["chats"][0]
        assert row["status"] == "ok" and row["lag_days"] == 0

    def test_migrated_chat_is_flagged(self, tg_app, monkeypatch):
        """chat_id changes after the move to a supergroup — the listener silently goes deaf."""
        self._seed(tg_app, -5001, msgs=[(7, "2026-03-01T10:00:00+00:00")])
        self._live(tg_app, monkeypatch, [{"id": -5001, "migrated_to": -1009999,
                                          "last_message_id": 7,
                                          "last_message_date": "2026-03-01T10:00:00+00:00"}])
        row = tg_app.test_client().get("/api/telegram/coverage").get_json()["chats"][0]
        assert row["status"] == "migrated" and row["migrated_to"] == -1009999

    def test_missing_from_dialogs_and_never_ingested(self, tg_app, monkeypatch):
        self._seed(tg_app, -5002, title="Зниклий", msgs=[(7, "2026-03-01T10:00:00+00:00")])
        self._seed(tg_app, -5003, title="Порожній")
        self._live(tg_app, monkeypatch, [{"id": -5003, "last_message_id": 3,
                                          "last_message_date": "2026-03-01T10:00:00+00:00"}])
        by_id = {c["chat_id"]: c for c in
                 tg_app.test_client().get("/api/telegram/coverage").get_json()["chats"]}
        assert by_id[-5002]["status"] == "not_listed"
        assert by_id[-5003]["status"] == "never_ingested"

    def test_no_id_arithmetic_outside_supergroups(self, tg_app, monkeypatch):
        """In legacy groups message_id comes from the account's global sequence —
        an id difference there means nothing."""
        self._seed(tg_app, -5004, msgs=[(510909, "2026-06-01T10:00:00+00:00")])
        self._live(tg_app, monkeypatch, [{"id": -5004, "last_message_id": 521636,
                                          "last_message_date": "2026-06-11T10:00:00+00:00"}])
        row = tg_app.test_client().get("/api/telegram/coverage").get_json()["chats"][0]
        assert row["behind_messages"] is None
        assert row["lag_days"] == 10, "by dates the lag is visible here too"

    def test_disabled_chat_is_not_an_alarm(self, tg_app, monkeypatch):
        """A disabled chat SHOULD lag — we do not listen to it. On live data, without
        this branch the report screamed «lagging by 170 days» about 5 chats that work as
        designed, and two real problems drowned among false alarms."""
        self._seed(tg_app, -100888, msgs=[(5, "2026-02-10T10:00:00+00:00")], enabled=0)
        self._live(tg_app, monkeypatch, [{"id": -100888, "last_message_id": 900,
                                          "last_message_date": "2026-07-30T10:00:00+00:00"}])
        row = tg_app.test_client().get("/api/telegram/coverage").get_json()["chats"][0]
        assert row["status"] == "not_monitored"
        assert row["lag_days"] == 170, "the numbers themselves stay — we hide the alarm, not the facts"

    def test_dialog_window_is_reported(self, tg_app, monkeypatch):
        """Without the dialog window size «not_listed» cannot be told from «did not fit the limit»."""
        self._seed(tg_app, -5005, msgs=[(7, "2026-03-01T10:00:00+00:00")])
        self._live(tg_app, monkeypatch, [], limit=200)
        r = tg_app.test_client().get("/api/telegram/coverage").get_json()
        assert r["dialogs_seen"] == 0 and r["dialog_limit"] == 200

    def test_days_between_survives_garbage(self):
        assert tg._days_between(None, "2026-06-01") is None
        assert tg._days_between("не дата", "2026-06-01") is None
        assert tg._days_between("2026-06-11T10:00:00+00:00", "2026-06-01T23:00:00+00:00") == 10


class TestDeliveryReliability:
    """Wave 2: delivery to ingest with retries and dead-letter.

    The regression this catches: `_post_ingest` raised an exception, the handler logged it —
    and the message vanished. app.py is unavailable exactly when it restarts,
    i.e. regularly; in id gaps such a loss looks exactly like a switched-off machine.
    """

    def test_retries_then_succeeds(self, tmp_path, monkeypatch):
        calls = []

        def _flaky(url, payload):
            calls.append(1)
            if len(calls) < 2:
                raise OSError("connection refused")

        monkeypatch.setattr(L, "_post_ingest", _flaky)
        monkeypatch.setattr(L.time, "sleep", lambda *_: None)
        dl = str(tmp_path / "dl.jsonl")
        assert L._post_ingest_reliable("http://x", {"chat_id": 1, "message_id": 2}, dl) is True
        assert len(calls) == 2
        assert not os.path.exists(dl), "success must not leave a dead-letter"

    def test_gives_up_into_deadletter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "_post_ingest",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        monkeypatch.setattr(L.time, "sleep", lambda *_: None)
        dl = str(tmp_path / "dl.jsonl")
        assert L._post_ingest_reliable("http://x", {"chat_id": 7, "message_id": 9}, dl) is False
        saved = [json.loads(line) for line in open(dl, encoding="utf-8") if line.strip()]
        assert saved == [{"chat_id": 7, "message_id": 9}], "the message is saved, not lost"

    def test_replay_keeps_only_still_failing(self, tmp_path, monkeypatch):
        dl = tmp_path / "dl.jsonl"
        dl.write_text('{"message_id": 1}\n{"message_id": 2}\n', encoding="utf-8")

        def _picky(url, payload):
            if payload["message_id"] == 2:
                raise OSError("still down")

        monkeypatch.setattr(L, "_post_ingest", _picky)
        L._replay_deadletter({"deadletter": str(dl), "ingest_url": "http://x"})
        left = [json.loads(line) for line in dl.read_text(encoding="utf-8").splitlines() if line]
        assert left == [{"message_id": 2}]

    def test_replay_removes_file_when_all_delivered(self, tmp_path, monkeypatch):
        dl = tmp_path / "dl.jsonl"
        dl.write_text('{"message_id": 1}\n', encoding="utf-8")
        monkeypatch.setattr(L, "_post_ingest", lambda *a, **k: None)
        L._replay_deadletter({"deadletter": str(dl), "ingest_url": "http://x"})
        assert not dl.exists()


class TestThreadSignals:
    """Wave 4: signals that Telegram gives for free and we threw away."""

    def test_reply_and_author_are_stored(self, tg_app, monkeypatch):
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        r = tg_app.test_client().post(
            "/api/telegram/ingest",
            json={"kind": "text", "text": "так, у пʼятницю", "chat_id": -100, "message_id": 12,
                  "date": "2026-06-01T10:00:00+00:00", "sender_id": 777, "reply_to": 11,
                  "grouped_id": 555, "edit_date": "2026-06-01T10:05:00+00:00"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute(
                "SELECT tg_reply_to, tg_sender_id, tg_grouped_id, tg_edit_date "
                "FROM transcriptions WHERE id = ?", (r["transcription_id"],)).fetchone()
        assert row["tg_reply_to"] == 11, "the exact thread from Telegram, not guessing by time"
        assert row["tg_sender_id"] == 777
        assert row["tg_grouped_id"] == 555
        assert row["tg_edit_date"].startswith("2026-06-01T10:05")


class TestEditsAndDeletes:
    """Wave 4: the archive is no longer written once and for all."""

    def _add(self, tg_app, monkeypatch, mid=20, text="сума 1000"):
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        return tg_app.test_client().post(
            "/api/telegram/ingest",
            json={"kind": "text", "text": text, "chat_id": -100, "message_id": mid,
                  "date": "2026-06-01T10:00:00+00:00"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()["transcription_id"]

    def test_edit_updates_text_and_resets_vectors(self, tg_app, monkeypatch):
        """In work chats a correcting edit is the norm (amount, date, address), and
        the archive forever kept the first, wrong version."""
        tid = self._add(tg_app, monkeypatch)
        r = tg_app.test_client().post(
            "/api/telegram/edited",
            json={"chat_id": -100, "message_id": 20, "text": "сума 1200",
                  "edit_date": "2026-06-01T11:00:00+00:00"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        assert r["updated"] is True and r["transcription_id"] == tid
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text, tg_edit_date, embedded_at "
                               "FROM transcriptions WHERE id = ?", (tid,)).fetchone()
        assert row["transcript_text"] == "сума 1200"
        assert row["embedded_at"] is None, "the old vector describes the old revision"

    def test_edit_of_unknown_message_is_noop(self, tg_app, monkeypatch):
        r = tg_app.test_client().post(
            "/api/telegram/edited",
            json={"chat_id": -100, "message_id": 999, "text": "щось"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        assert r["success"] and r["skipped"] == "not_in_archive"

    def test_identical_edit_does_not_reset_vectors(self, tg_app, monkeypatch):
        tid = self._add(tg_app, monkeypatch, mid=21, text="без змін")
        r = tg_app.test_client().post(
            "/api/telegram/edited",
            json={"chat_id": -100, "message_id": 21, "text": "без змін"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        assert r.get("unchanged") is True, "we do not start a vector recount for nothing"

    def test_delete_soft_removes_from_search(self, tg_app, monkeypatch):
        tid = self._add(tg_app, monkeypatch, mid=22)
        r = tg_app.test_client().post(
            "/api/telegram/deleted",
            json={"chat_id": -100, "message_ids": [22]},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        assert r["marked"] == 1
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            assert conn.execute("SELECT deleted_at FROM transcriptions WHERE id = ?",
                                (tid,)).fetchone()["deleted_at"] is not None

    def test_edit_and_delete_require_token(self, tg_app):
        c = tg_app.test_client()
        assert c.post("/api/telegram/edited", json={"chat_id": -100, "message_id": 1,
                                                    "text": "x"}).status_code == 403
        assert c.post("/api/telegram/deleted", json={"chat_id": -100,
                                                     "message_ids": [1]}).status_code == 403


class TestMediaPlaceholder:
    """Wave 2: the media row appears IMMEDIATELY, not at the end of the background job.

    While it does not exist, the message is invisible: dedup does not see it (two whisper
    runs on one voice message + IntegrityError), the catch-up watermark does not count it,
    and when app.py crashes mid-queue the job stays 'crashed' without a restart —
    and the message vanishes without a trace.
    """

    @staticmethod
    def _queue(monkeypatch):
        submitted = []
        fake = types.SimpleNamespace(
            submit=lambda name, fn, meta=None: submitted.append((name, meta)))
        monkeypatch.setattr(tg.state, "job_queue", fake)
        return submitted

    def test_row_exists_before_job_runs(self, tg_app, monkeypatch):
        submitted = self._queue(monkeypatch)
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "v.ogg")
        open(voice, "wb").close()
        r = tg_app.test_client().post(
            "/api/telegram/ingest",
            json={"kind": "voice", "file_path": voice, "chat_id": -100, "message_id": 42,
                  "date": "2026-06-01T10:00:00+00:00"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        assert r["queued"] and r["transcription_id"]
        assert submitted and submitted[0][1]["transcription_id"] == r["transcription_id"]
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text, meeting_date FROM transcriptions "
                               "WHERE id = ?", (r["transcription_id"],)).fetchone()
        assert "очікує розпізнавання" in row["transcript_text"]
        assert row["meeting_date"] == "2026-06-01", "the event date is already on the stub"

    def test_second_ingest_of_same_voice_is_duplicate(self, tg_app, monkeypatch):
        """Earlier the second ingest also queued a job: two whisper runs on the GPU and
        an IntegrityError in the second persist, which failed the job together with the message."""
        submitted = self._queue(monkeypatch)
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "v2.ogg")
        open(voice, "wb").close()
        body = {"kind": "voice", "file_path": voice, "chat_id": -100, "message_id": 43,
                "date": "2026-06-01T10:00:00+00:00"}
        hdr = {"X-Telegram-Token": "testtoken"}
        c = tg_app.test_client()
        first = c.post("/api/telegram/ingest", json=body, headers=hdr).get_json()
        second = c.post("/api/telegram/ingest", json=body, headers=hdr).get_json()
        assert second.get("duplicate") is True
        assert second["transcription_id"] == first["transcription_id"]
        assert len(submitted) == 1, "the second job should not have been queued"

    def test_finalize_replaces_placeholder(self, tg_app, monkeypatch):
        submitted = self._queue(monkeypatch)
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "v3.ogg")
        open(voice, "wb").close()
        r = tg_app.test_client().post(
            "/api/telegram/ingest",
            json={"kind": "voice", "file_path": voice, "chat_id": -100, "message_id": 44,
                  "chat_title": "Fund", "date": "2026-06-01T10:00:00+00:00"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        tid = r["transcription_id"]
        with tg_app.app_context():
            tg._finalize_media(tg_app.config["DATABASE"], tid, kind="voice",
                               text="розпізнаний текст", prov={"chat_title": "Fund"},
                               segments_json=None, model_used="large-v3-turbo",
                               processing_time=1.5)
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text, source_name, model_used "
                               "FROM transcriptions WHERE id = ?", (tid,)).fetchone()
        assert row["transcript_text"] == "розпізнаний текст"
        assert "розпізнаний текст" in row["source_name"], "the caption is updated too"
        assert row["model_used"] == "large-v3-turbo"

    def test_persist_race_returns_existing_row(self, tg_app):
        """A window between _is_duplicate and INSERT: the conflict is caught on the UNIQUE index."""
        prov = {"chat_id": -100, "message_id": 55, "chat_title": "Fund",
                "date": "2026-06-01T10:00:00+00:00"}
        with tg_app.app_context():
            db = tg_app.config["DATABASE"]
            first = tg._persist(db, kind="text", text="раз", prov=prov, category_id=None,
                                file_path=None, doc_type=None, segments_json=None,
                                model_used=None, processing_time=0.0)
            again = tg._persist(db, kind="text", text="два", prov=prov, category_id=None,
                                file_path=None, doc_type=None, segments_json=None,
                                model_used=None, processing_time=0.0)
        assert again == first, "the race must neither duplicate the row nor fail the job"


class TestVideoNeverDownloaded:
    """tg-media-policy-01: video is NEVER downloaded — no threshold by
    size or age (owner's decision). A stub row with a link stays."""

    @staticmethod
    def _boom_download():
        async def _boom(*a, **k):
            raise AssertionError("download_media should not have been called for video")
        return _boom

    def test_listener_skips_download_for_video(self, monkeypatch):
        msg = _msg(id=1, video=object(), message="запис зустрічі українською", date=None, chat_id=-100)
        msg.download_media = self._boom_download()
        chat = _msg(title="Рада директорів", username=None)
        captured = {}
        monkeypatch.setattr(
            L, "_post_ingest_reliable",
            lambda url, payload, deadletter: (captured.update(payload) or True))
        kind = asyncio.run(L._ingest_message(
            chat, msg, {"media_dir": ".", "ingest_url": "x", "deadletter": "d.jsonl"}))
        assert kind == "video"
        assert "file_path" not in captured
        assert captured["caption"] == "запис зустрічі українською"

    def test_listener_skips_download_for_video_note(self, monkeypatch):
        msg = _msg(id=2, video_note=object(), video=None, message="", date=None, chat_id=-100)
        msg.download_media = self._boom_download()
        chat = _msg(title="Чат", username=None)
        monkeypatch.setattr(L, "_post_ingest_reliable", lambda *a, **k: True)
        kind = asyncio.run(L._ingest_message(
            chat, msg, {"media_dir": ".", "ingest_url": "x", "deadletter": "d.jsonl"}))
        assert kind == "video"

    def test_listener_skips_download_for_gif(self, monkeypatch):
        msg = _msg(id=3, gif=object(), video=None, message="", date=None, chat_id=-100)
        msg.download_media = self._boom_download()
        chat = _msg(title="Чат", username=None)
        monkeypatch.setattr(L, "_post_ingest_reliable", lambda *a, **k: True)
        kind = asyncio.run(L._ingest_message(
            chat, msg, {"media_dir": ".", "ingest_url": "x", "deadletter": "d.jsonl"}))
        assert kind == "video"

    def test_listener_skips_download_for_video_mime_document(self, monkeypatch):
        doc = _msg(mime_type="video/mp4")
        msg = _msg(id=4, document=doc, video=None, message="", date=None, chat_id=-100)
        msg.download_media = self._boom_download()
        chat = _msg(title="Чат", username=None)
        monkeypatch.setattr(L, "_post_ingest_reliable", lambda *a, **k: True)
        kind = asyncio.run(L._ingest_message(
            chat, msg, {"media_dir": ".", "ingest_url": "x", "deadletter": "d.jsonl"}))
        assert kind == "video"

    def test_listener_skips_download_for_video_extension_no_mime_document(self, monkeypatch):
        """tg-media-policy-03: a video as a file without a mime (or with an erased one) — the gate
        had to let the download pass, not only by mime video/*."""
        from telethon.tl.types import DocumentAttributeFilename
        doc = _msg(mime_type=None, attributes=[DocumentAttributeFilename(file_name="запис.mkv")])
        msg = _msg(id=5, document=doc, video=None, message="", date=None, chat_id=-100)
        msg.download_media = self._boom_download()
        chat = _msg(title="Чат", username=None)
        monkeypatch.setattr(L, "_post_ingest_reliable", lambda *a, **k: True)
        kind = asyncio.run(L._ingest_message(
            chat, msg, {"media_dir": ".", "ingest_url": "x", "deadletter": "d.jsonl"}))
        assert kind == "video"

    def test_ingest_endpoint_persists_placeholder_without_download(self, tg_app, monkeypatch):
        submitted = []
        fake_queue = types.SimpleNamespace(submit=lambda *a, **k: submitted.append((a, k)))
        monkeypatch.setattr(tg.state, "job_queue", fake_queue)
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")

        def _boom_whisper(*a, **k):
            raise AssertionError("whisper should not have been called for video")
        monkeypatch.setattr(tg.state, "whisper_manager",
                            types.SimpleNamespace(transcribe_with_progress=_boom_whisper))

        r = tg_app.test_client().post(
            "/api/telegram/ingest",
            json={"kind": "video", "caption": "запис зустрічі українською",
                  "chat_id": -100, "message_id": 77, "chat_title": "Рада директорів",
                  "link": "https://t.me/c/100/77", "date": "2026-06-01T10:00:00+00:00"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        assert r["success"] and r.get("skipped") == "video_not_downloaded"
        assert submitted == [], "no background whisper job should have been queued"
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute(
                "SELECT transcript_text, file_path, tg_link, doc_type FROM transcriptions WHERE id = ?",
                (r["transcription_id"],)).fetchone()
        assert row["file_path"] is None
        assert row["transcript_text"] == "запис зустрічі українською"
        assert row["tg_link"] == "https://t.me/c/100/77"
        # tg-media-policy-03: with a caption the row had to look like an ordinary text
        # message (doc_type=None) — now it is visible that this is a skipped video.
        assert row["doc_type"] == "video"

    def test_ingest_endpoint_placeholder_text_without_caption(self, tg_app, monkeypatch):
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        r = tg_app.test_client().post(
            "/api/telegram/ingest",
            json={"kind": "video", "chat_id": -100, "message_id": 78,
                  "link": "https://t.me/c/100/78"},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text, doc_type FROM transcriptions WHERE id = ?",
                              (r["transcription_id"],)).fetchone()
        assert "відео" in row["transcript_text"]
        assert "посилання" in row["transcript_text"]
        # tg-media-policy-03: and without a caption the row had to be recognisable as video
        # through the same channel (doc_type) that the library draws labels with.
        assert row["doc_type"] == "video"

    def test_ingest_endpoint_dedups_video_placeholder(self, tg_app, monkeypatch):
        """Dedup and the catch-up watermark must see this row like ordinary ones —
        a row without file_path must not stay invisible and be overwritten every time."""
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        c = tg_app.test_client()
        body = {"kind": "video", "chat_id": -100, "message_id": 79,
                "caption": "друге відео", "link": "https://t.me/c/100/79",
                "date": "2026-06-01T10:00:00+00:00"}
        hdr = {"X-Telegram-Token": "testtoken"}
        first = c.post("/api/telegram/ingest", json=body, headers=hdr).get_json()
        second = c.post("/api/telegram/ingest", json=body, headers=hdr).get_json()
        assert second.get("duplicate") is True
        assert second["transcription_id"] == first["transcription_id"]
        marks = L._watermarks(tg_app.config["DATABASE"], {-100})
        assert marks[-100]["msg_id"] == 79, "the catch-up watermark sees the stub row"

    def test_ingest_endpoint_never_calls_extract_audio(self, tg_app, monkeypatch):
        """Even if file_path accidentally arrives in the payload (an old listener,
        targeted repair) — extract_audio_from_video should not have been called."""
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        called = {"hit": False}

        def _boom(*a, **k):
            called["hit"] = True
            raise AssertionError("extract_audio_from_video should not have been called")
        import app.utils.audio as audio_mod
        monkeypatch.setattr(audio_mod, "extract_audio_from_video", _boom)
        fake_video = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "leaked.mp4")
        open(fake_video, "wb").close()
        r = tg_app.test_client().post(
            "/api/telegram/ingest",
            json={"kind": "video", "file_path": fake_video, "chat_id": -100, "message_id": 80},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        assert r["success"] and r.get("skipped") == "video_not_downloaded"
        assert not called["hit"]


class TestVoiceVocabulary:
    """K1: a TG voice message goes to whisper with the decode_options of the 'telegram' path and the task's DB."""

    def test_voice_passes_decode_options(self, tg_app, monkeypatch):
        from app.services import vocabulary
        seen, calls = [], []
        monkeypatch.setattr(vocabulary, "decode_options_for",
                            lambda kind, db: seen.append((kind, db)) or {"hotwords": ["Acmecorp"]})
        monkeypatch.setattr(tg.state, "whisper_manager", types.SimpleNamespace(
            transcribe_with_progress=lambda **kw: calls.append(kw) or {"text": "голосове", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "vocab.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 16)
        prov = {"chat_id": -100, "message_id": 300, "chat_title": "Fund",
                "date": "2026-06-01T10:00:00+00:00"}
        db = tg_app.config["DATABASE"]
        with tg_app.app_context():
            tid = tg._persist(db, kind="voice", text="[очікує]", prov=prov, category_id=None,
                              file_path=None, doc_type=None, segments_json=None,
                              model_used=None, processing_time=0.0)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov, db, "large-v3-turbo", "uk")
        assert seen == [("telegram", db)]
        assert calls[0]["decode_options"] == {"hotwords": ["Acmecorp"]}


class TestOriginalCleanup:
    """tg-media-policy-02: an original heavier than the threshold disappears from disk right after
    a successful text extraction; a small one stays; a failed extraction deletes
    nothing (no text — no right to throw away the source)."""

    @staticmethod
    def _fake_whisper(result):
        return types.SimpleNamespace(transcribe_with_progress=lambda **kw: result)

    @staticmethod
    def _persist_placeholder(tg_app, message_id, kind="voice"):
        prov = {"chat_id": -100, "message_id": message_id, "chat_title": "Fund",
                "date": "2026-06-01T10:00:00+00:00"}
        with tg_app.app_context():
            tid = tg._persist(tg_app.config["DATABASE"], kind=kind, text="[очікує]", prov=prov,
                              category_id=None, file_path=None, doc_type=None,
                              segments_json=None, model_used=None, processing_time=0.0)
        return tid, prov

    def test_heavy_original_deleted_after_successful_extraction(self, tg_app, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ORIGINAL_MAX_MB", "0")  # any non-empty file is «heavy»
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"text": "розпізнаний українською", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "heavy.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, prov = self._persist_placeholder(tg_app, 200)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert not os.path.isfile(voice), "the heavy original should have disappeared from disk"
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text FROM transcriptions WHERE id = ?",
                              (tid,)).fetchone()
        assert row["transcript_text"] == "розпізнаний українською", "the text stays in place"

    def test_small_original_stays_on_disk(self, tg_app, monkeypatch):
        # the default threshold (100 MB) — a file of a few bytes is clearly smaller.
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"text": "коротке голосове", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "small.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, prov = self._persist_placeholder(tg_app, 201)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert os.path.isfile(voice), "the small original stays on disk"

    def test_failed_extraction_keeps_original_even_if_heavy(self, tg_app, monkeypatch):
        """No text — no right to throw away the source, even with a zero threshold."""
        monkeypatch.setenv("TELEGRAM_ORIGINAL_MAX_MB", "0")
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"error": "модель недоступна"}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "failed.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, prov = self._persist_placeholder(tg_app, 202)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert os.path.isfile(voice), "extraction failed — the original is not deleted"
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text FROM transcriptions WHERE id = ?",
                              (tid,)).fetchone()
        assert "без розпізнаного тексту" in row["transcript_text"]

    def test_intermediate_extracted_audio_never_left_orphaned(self, tg_app, monkeypatch):
        """The defective branch kind=='video' (dead after tg-media-policy-01, nobody
        routes here) still must not leave `_audio.mp3` an orphan if it
        is ever called directly (targeted repair / old jobs in the queue)."""
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"text": "з відео", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        video = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "clip.mp4")
        open(video, "wb").close()
        extracted_path = os.path.splitext(video)[0] + "_audio.mp3"

        def _fake_extract(src, dst, add_log=None):
            with open(dst, "wb") as f:
                f.write(b"\x00" * 16)
            return True
        import app.utils.audio as audio_mod
        monkeypatch.setattr(audio_mod, "extract_audio_from_video", _fake_extract)
        tid, prov = self._persist_placeholder(tg_app, 203, kind="video")
        tg._transcribe_and_finalize(tid, video, "video", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert not os.path.isfile(extracted_path), "the intermediate _audio.mp3 must not stay an orphan"

    def test_intermediate_extracted_audio_removed_even_on_failed_extraction(self, tg_app, monkeypatch):
        """tg-media-policy-03: ffmpeg may manage to partly write
        _audio.mp3 and return failure — earlier a failure reset the path to None,
        and that partial file stayed an orphan forever."""
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        video = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "broken.mp4")
        open(video, "wb").close()
        extracted_path = os.path.splitext(video)[0] + "_audio.mp3"

        def _fake_extract_fails_but_writes_partial(src, dst, add_log=None):
            with open(dst, "wb") as f:
                f.write(b"\x00" * 4)  # a partial write before ffmpeg fails
            return False
        import app.utils.audio as audio_mod
        monkeypatch.setattr(audio_mod, "extract_audio_from_video",
                            _fake_extract_fails_but_writes_partial)
        tid, prov = self._persist_placeholder(tg_app, 204, kind="video")
        tg._transcribe_and_finalize(tid, video, "video", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert not os.path.isfile(extracted_path), \
            "the partial _audio.mp3 after a failed extraction must not stay an orphan"

    def test_empty_whisper_text_keeps_original(self, tg_app, monkeypatch):
        """Critical 1: whisper on deaf/broken audio returns {"text": ""}
        without an error key — this is NOT success, the original is not deleted."""
        monkeypatch.setenv("TELEGRAM_ORIGINAL_MAX_MB", "0")  # any non-empty file is «heavy»
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"text": "", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "silent.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, prov = self._persist_placeholder(tg_app, 205)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert os.path.isfile(voice), "empty text without error is not success, the original stays"
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text FROM transcriptions WHERE id = ?",
                              (tid,)).fetchone()
        assert "без розпізнаного тексту" in row["transcript_text"]

    def test_whitespace_only_whisper_text_keeps_original(self, tg_app, monkeypatch):
        """The same case, when whisper returns only spaces/newlines."""
        monkeypatch.setenv("TELEGRAM_ORIGINAL_MAX_MB", "0")
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"text": "   \n  ", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "blank.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, prov = self._persist_placeholder(tg_app, 206)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert os.path.isfile(voice), "spaces alone are not text, the original stays"

    def test_blank_threshold_env_does_not_crash_job(self, tg_app, monkeypatch):
        """Critical 2: an empty value in .env must not fail the job with a ValueError
        in the middle of a background thread with no retry."""
        monkeypatch.setenv("TELEGRAM_ORIGINAL_MAX_MB", "")
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"text": "текст доїхав", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "blank_env.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, prov = self._persist_placeholder(tg_app, 207)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert os.path.isfile(voice), "a bad threshold gives no right to delete"
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text FROM transcriptions WHERE id = ?",
                              (tid,)).fetchone()
        assert row["transcript_text"] == "текст доїхав", "the text had to arrive, not crash the job"

    def test_garbage_threshold_env_does_not_crash_job(self, tg_app, monkeypatch):
        """A non-numeric value in .env — the same behaviour as an empty one."""
        monkeypatch.setenv("TELEGRAM_ORIGINAL_MAX_MB", "не число")
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"text": "текст доїхав", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "garbage_env.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, prov = self._persist_placeholder(tg_app, 208)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert os.path.isfile(voice), "a non-numeric value gives no right to delete"
        from app.db.connection import get_db_connection
        with get_db_connection(tg_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT transcript_text FROM transcriptions WHERE id = ?",
                              (tid,)).fetchone()
        assert row["transcript_text"] == "текст доїхав"

    def test_negative_threshold_env_does_not_delete_everything(self, tg_app, monkeypatch):
        """A negative value used to be accepted silently and deleted ALL originals."""
        monkeypatch.setenv("TELEGRAM_ORIGINAL_MAX_MB", "-1")
        monkeypatch.setattr(tg.state, "whisper_manager",
                            self._fake_whisper({"text": "текст доїхав", "segments": []}))
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        voice = os.path.join(tg_app.config["TELEGRAM_MEDIA_DIR"], "negative_env.ogg")
        with open(voice, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, prov = self._persist_placeholder(tg_app, 209)
        tg._transcribe_and_finalize(tid, voice, "voice", "", prov,
                                    tg_app.config["DATABASE"], "large-v3-turbo", "uk")
        assert os.path.isfile(voice), "a negative threshold must not delete originals at all"


class TestCatchupWatermark:
    """Wave 2: how much the archive already knows — derived from data, not from a cursor."""

    def test_takes_max_date_per_chat(self, tg_app):
        TestCoverage._seed(tg_app, -100111, msgs=[(1, "2026-06-01T10:00:00+00:00"),
                                                  (2, "2026-06-05T10:00:00+00:00")])
        TestCoverage._seed(tg_app, -100222, msgs=[(1, "2026-01-01T10:00:00+00:00")])
        marks = L._watermarks(tg_app.config["DATABASE"], {-100111, -100222})
        assert marks[-100111]["date"].startswith("2026-06-05")
        assert marks[-100222]["date"].startswith("2026-01-01")

    def test_returns_id_of_newest_message(self, tg_app):
        """The id is needed because offset_date in Telethon is INCLUSIVE: without it the catch-up
        re-pulls the already known last message every time and downloads its
        media again (in live data one PDF lay in seven copies)."""
        TestCoverage._seed(tg_app, -100111, msgs=[(1, "2026-06-01T10:00:00+00:00"),
                                                  (77, "2026-06-05T10:00:00+00:00")])
        marks = L._watermarks(tg_app.config["DATABASE"], {-100111})
        assert marks[-100111]["msg_id"] == 77

    def test_chat_without_messages_has_no_watermark(self, tg_app):
        """An empty chat is the initial history load (owner's decision), not a catch-up."""
        TestCoverage._seed(tg_app, -100333)
        assert L._watermarks(tg_app.config["DATABASE"], {-100333}) == {}


class TestBackfillFloodWait:
    """Regression of telegram_listener: FloodWait must not cost a message."""

    def test_message_is_retried_not_skipped(self, tg_app, monkeypatch):
        from telethon.errors import FloodWaitError

        msg = _msg(id=5, message="важливе", date=None)
        ingested = []
        attempts = {"n": 0}

        async def _fake_ingest(chat, m, cfg):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise FloodWaitError(request=None, capture=1)
            ingested.append(m.id)
            return "text"

        class _Iter:
            def __init__(self): self.left = [msg]
            def __aiter__(self): return self
            async def __anext__(self):
                if not self.left:
                    raise StopAsyncIteration
                return self.left.pop()

        class _Client:
            async def get_entity(self, cid): return _msg(title="Чат", username=None)
            def iter_messages(self, chat, **kw): return _Iter()

        monkeypatch.setattr(L, "_ingest_message", _fake_ingest)
        monkeypatch.setattr(L.asyncio, "sleep", _noop_sleep)
        asyncio.run(L._backfill_task(_Client(), -100999, 10,
                                     {"backfill_delay": 0, "media_dir": "."}))
        assert ingested == [5], "after the pause the message must be RETRIED, not skipped"

    def test_floodwait_from_iterator_does_not_kill_the_pass(self, tg_app, monkeypatch):
        """FloodWait arrives from the generator boundary (__anext__), not from the loop body —
        earlier it flew past the except and killed the fire-and-forget task silently."""
        from telethon.errors import FloodWaitError

        ingested = []

        async def _fake_ingest(chat, m, cfg):
            ingested.append(m.id)
            return "text"

        class _Iter:
            def __init__(self): self.state = ["flood", _msg(id=7, message="hi", date=None)]
            def __aiter__(self): return self
            async def __anext__(self):
                if not self.state:
                    raise StopAsyncIteration
                nxt = self.state.pop(0)
                if nxt == "flood":
                    raise FloodWaitError(request=None, capture=1)
                return nxt

        class _Client:
            async def get_entity(self, cid): return _msg(title="Чат", username=None)
            def iter_messages(self, chat, **kw): return _Iter()

        monkeypatch.setattr(L, "_ingest_message", _fake_ingest)
        monkeypatch.setattr(L.asyncio, "sleep", _noop_sleep)
        asyncio.run(L._backfill_task(_Client(), -100999, 10,
                                     {"backfill_delay": 0, "media_dir": "."}))
        assert ingested == [7], "the pass must survive a pause on the history fetch"


class TestRepair:
    """Wave 3: targeted repair of gaps by specific ids."""

    def test_finds_holes_and_tail(self, tg_app):
        TestCoverage._seed(tg_app, -100999, msgs=[(10, "2026-06-01T10:00:00+00:00"),
                                                  (11, "2026-06-01T10:01:00+00:00"),
                                                  (15, "2026-06-01T10:05:00+00:00")])
        with tg_app.app_context():
            missing = tg._missing_ids(tg_app.config["DATABASE"], -100999, live_last_id=17)
        assert missing == [12, 13, 14, 16, 17], "gaps inside + the tail up to the live last one"

    def test_does_not_reach_below_first_known(self, tg_app):
        """Below min is history loading, not repair: «we lost it» must be
        told from «we have not been there yet»."""
        TestCoverage._seed(tg_app, -100999, msgs=[(100, "2026-06-01T10:00:00+00:00"),
                                                  (102, "2026-06-01T10:02:00+00:00")])
        with tg_app.app_context():
            assert tg._missing_ids(tg_app.config["DATABASE"], -100999) == [101]

    def test_empty_chat_is_backfill_not_repair(self, tg_app):
        TestCoverage._seed(tg_app, -100999)
        with tg_app.app_context():
            assert tg._missing_ids(tg_app.config["DATABASE"], -100999) == []

    def test_rejects_non_supergroup(self, tg_app):
        """In legacy groups message_id comes from the account's global sequence —
        id arithmetic there is meaningless, repair must refuse, not «fix»."""
        TestCoverage._seed(tg_app, -5555, msgs=[(510000, "2026-06-01T10:00:00+00:00")])
        r = tg_app.test_client().post("/api/telegram/repair", json={"chat_id": -5555})
        assert r.status_code == 400
        assert "супергруп" in r.get_json()["error"]

    def test_dry_run_reports_without_touching_telegram(self, tg_app, monkeypatch):
        TestCoverage._seed(tg_app, -100999, msgs=[(10, "2026-06-01T10:00:00+00:00"),
                                                  (14, "2026-06-01T10:04:00+00:00")])
        called = []
        monkeypatch.setattr(tg, "_listener_call",
                            lambda path, **k: called.append(path) or (True, {"dialogs": []}))
        r = tg_app.test_client().post("/api/telegram/repair",
                                      json={"chat_id": -100999, "dry_run": True}).get_json()
        assert r["missing_total"] == 3 and r["sample"] == [11, 12, 13]
        assert "/repair" not in called, "dry_run must not touch Telegram"

    def test_batch_is_bounded_and_reports_remaining(self, tg_app, monkeypatch):
        TestCoverage._seed(tg_app, -100999, msgs=[(1, "2026-06-01T10:00:00+00:00"),
                                                  (50, "2026-06-01T10:50:00+00:00")])
        sent = {}

        def _call(path, method="GET", body=None, timeout=60):
            if path == "/dialogs":
                return True, {"dialogs": []}
            sent["ids"] = body["ids"]
            return True, {"requested": len(body["ids"]), "found": 3, "deleted": 2,
                          "ingested": 3, "skipped": 0, "failed": 0}

        monkeypatch.setattr(tg, "_listener_call", _call)
        r = tg_app.test_client().post("/api/telegram/repair",
                                      json={"chat_id": -100999, "max_messages": 5}).get_json()
        assert len(sent["ids"]) == 5
        assert r["missing_total"] == 48 and r["remaining"] == 43
        assert r["deleted"] == 2, "deleted by the author is separated from lost by the listener"


class TestCatchup:
    """Regression: `mark[:16]` in the log — `mark` is a dict {"date", "msg_id"},
    a slice without ["date"] is treated as a key and raises KeyError (telegram-catchup-crash-01)."""

    def test_survives_log_line_when_something_was_backfilled(self, tg_app, monkeypatch, tmp_path, caplog):
        chat_id = -100111
        TestCoverage._seed(tg_app, chat_id, msgs=[(5, "2026-06-01T10:00:00+00:00")])

        entity = types.SimpleNamespace(title="Чат", username=None)
        dialog = types.SimpleNamespace(id=chat_id, entity=entity)
        new_msg = _msg(id=6, message="нове", date=None)

        class _DialogIter:
            def __aiter__(self):
                return self
            async def __anext__(self):
                if not hasattr(self, "_done"):
                    self._done = True
                    return dialog
                raise StopAsyncIteration

        class _MsgIter:
            def __init__(self):
                self.left = [new_msg]
            def __aiter__(self):
                return self
            async def __anext__(self):
                if not self.left:
                    raise StopAsyncIteration
                return self.left.pop(0)

        class _Client:
            def iter_dialogs(self, limit=None):
                return _DialogIter()
            def iter_messages(self, chat, **kw):
                return _MsgIter()

        saved = []

        async def _ok(*a, **k):
            return "text"

        monkeypatch.setattr(L, "_ingest_message", _ok)
        monkeypatch.setattr(L.asyncio, "sleep", _noop_sleep)
        monkeypatch.setattr(L, "_catchup_state_save",
                             lambda path, state: saved.append((path, dict(state))))

        cfg = {
            "catchup_enabled": True,
            "dialog_limit": 200,
            "catchup_debounce_min": 0,
            "catchup_state": str(tmp_path / "catchup.json"),
            "db_path": tg_app.config["DATABASE"],
            "backfill_delay": 0,
        }

        with caplog.at_level("INFO", logger=L.logger.name):
            stats = asyncio.run(L._catchup(_Client(), cfg, {chat_id}))

        assert stats["ingested"] == 1
        assert saved, "_catchup_state_save should have been called after the pass"
        # We prove it through the pass state, not by reasoning: no chat has failed,
        # and the log holds the shortened watermark timestamp, not an error.
        assert stats.get("failed_chats") == [], "the chat should not have failed on formatting the log line"
        watermark_lines = [r.message for r in caplog.records if "caught up" in r.message]
        assert watermark_lines, "the catch-up line should have reached the log"
        assert "2026-06-01" in watermark_lines[0], "the log must carry the shortened timestamp, not slice()"

    def test_one_chat_crash_does_not_skip_the_rest(self, tg_app, monkeypatch, tmp_path):
        """An exception in the loop body for the middle chat must not escape _catchup:
        the third chat is still caught up, and the state is saved (telegram-catchup-crash-02)."""
        chat_a, chat_b, chat_c = -100301, -100302, -100303
        for cid in (chat_a, chat_b, chat_c):
            TestCoverage._seed(tg_app, cid, msgs=[(5, "2026-06-01T10:00:00+00:00")])

        entity_a = types.SimpleNamespace(title="Чат A", username=None)
        entity_b = types.SimpleNamespace(title="Чат B", username=None)
        entity_c = types.SimpleNamespace(title="Чат C", username=None)
        dialogs = [
            types.SimpleNamespace(id=chat_a, entity=entity_a),
            types.SimpleNamespace(id=chat_b, entity=entity_b),
            types.SimpleNamespace(id=chat_c, entity=entity_c),
        ]

        class _DialogIter:
            def __init__(self, items):
                self._items = list(items)
            def __aiter__(self):
                return self
            async def __anext__(self):
                if not self._items:
                    raise StopAsyncIteration
                return self._items.pop(0)

        class _MsgIter:
            def __init__(self, msgs):
                self.left = list(msgs)
            def __aiter__(self):
                return self
            async def __anext__(self):
                if not self.left:
                    raise StopAsyncIteration
                return self.left.pop(0)

        class _Client:
            def iter_dialogs(self, limit=None):
                return _DialogIter(dialogs)
            def iter_messages(self, chat, **kw):
                if chat is entity_b:
                    raise RuntimeError("boom")
                return _MsgIter([_msg(id=6, message="нове", date=None)])

        saved = []
        ingested = []

        async def _ok(entity, msg, cfg):
            ingested.append(entity)
            return "text"

        monkeypatch.setattr(L, "_ingest_message", _ok)
        monkeypatch.setattr(L.asyncio, "sleep", _noop_sleep)
        monkeypatch.setattr(L, "_catchup_state_save",
                             lambda path, state: saved.append((path, dict(state))))

        cfg = {
            "catchup_enabled": True,
            "dialog_limit": 200,
            "catchup_debounce_min": 0,
            "catchup_state": str(tmp_path / "catchup.json"),
            "db_path": tg_app.config["DATABASE"],
            "backfill_delay": 0,
        }

        stats = asyncio.run(L._catchup(_Client(), cfg, {chat_a, chat_b, chat_c}))

        assert entity_c in ingested, "the third chat should have been caught up despite the second's failure"
        assert entity_a in ingested
        assert chat_b in stats.get("failed_chats", []), "the failed chat must be marked in stats"
        assert saved, "_catchup_state_save should have been called even when one chat fails"


class TestMeetingDate:
    """The event date = the message's day, not the ingest day.

    The regression this catches: _persist did not write meeting_date, and the whole stack dated
    a record via COALESCE(meeting_date, created_at). In live data this diverged for
    53% of the TG corpus, records by more than a month; it broke the search recency boost
    and the commitments.parse_due anchor («до пʼятниці» from the upload day).
    """

    def test_uses_message_date_not_ingest_date(self, tg_app, monkeypatch):
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        c = tg_app.test_client()
        r = c.post("/api/telegram/ingest",
                   json={"kind": "text", "text": "лютнева домовленість", "chat_id": -100,
                         "message_id": 77, "date": "2026-02-10T09:30:00+00:00"},
                   headers={"X-Telegram-Token": "testtoken"}).get_json()
        assert r["success"]
        with tg_app.app_context():
            from app.db.connection import get_db_connection
            with get_db_connection(tg_app.config["DATABASE"]) as conn:
                row = conn.execute("SELECT meeting_date, created_at FROM transcriptions "
                                   "WHERE id = ?", (r["transcription_id"],)).fetchone()
        assert row["meeting_date"] == "2026-02-10"
        assert not str(row["created_at"]).startswith("2026-02-10")   # ingest is today

    def test_missing_date_stays_null(self, tg_app, monkeypatch):
        monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")
        r = tg_app.test_client().post(
            "/api/telegram/ingest",
            json={"kind": "text", "text": "без дати", "chat_id": -100, "message_id": 78},
            headers={"X-Telegram-Token": "testtoken"}).get_json()
        with tg_app.app_context():
            from app.db.connection import get_db_connection
            with get_db_connection(tg_app.config["DATABASE"]) as conn:
                md = conn.execute("SELECT meeting_date FROM transcriptions WHERE id = ?",
                                  (r["transcription_id"],)).fetchone()["meeting_date"]
        assert md is None

    def test_helper_survives_garbage(self):
        assert tg._meeting_date(None) is None
        assert tg._meeting_date("не дата") is None
        assert tg._meeting_date("2026-02-10T09:30:00+00:00") == "2026-02-10"


class TestSafeMediaPath:
    def test_confinement(self, tg_app):
        with tg_app.app_context():
            media = tg_app.config["TELEGRAM_MEDIA_DIR"]
            inside = os.path.join(media, "x.jpg")
            open(inside, "w").close()
            assert tg._safe_media_path(inside) == os.path.realpath(inside)
            assert tg._safe_media_path(os.path.join(media, "..", "secret.db")) is None
            assert tg._safe_media_path(media + "_evil/x") is None   # prefix trick
            assert tg._safe_media_path(None) is None


# ============================================================
# Log isolation: the import under pytest does not attach a file handler
# ============================================================

class TestLogIsolation:
    def test_no_file_handler_attached_under_pytest(self):
        """telegram_listener is imported by this very file at module level
        (`import telegram_listener as L` above) — if a file handler attached
        here, every test run would append to the owner's live
        telegram_listener.log (that is how the catchup line with the test chat_ids -100111/-100301
        got there)."""
        import logging

        assert len(L._tg_handlers) == 1
        assert isinstance(L._tg_handlers[0], logging.StreamHandler)
        assert not any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in L._tg_handlers
        )
        assert not any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in L.logger.handlers
        )


# ============================================================
# telegram_link_repair CLI: --dry-run anywhere in the line
# (test-log-isolation-03 — the review measured that before the subcommand it silently
# goes out due to the dest collision in _SubParsersAction)
# ============================================================

class TestLinkRepairDryRun:
    def _seed(self, db_path):
        with get_db_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, tg_chat_id, tg_link) "
                "VALUES ('telegram', 'Андрій', 250264900, ?)",
                ("https://t.me/andriy_petrenko/515482",))
            conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, tg_chat_id, tg_link) "
                "VALUES ('telegram', 'Гурт', -1001234567890, 'https://t.me/c/1234567890/55')")
            conn.commit()

    def _bogus_link(self, db_path):
        with get_db_connection(db_path) as conn:
            return conn.execute(
                "SELECT tg_link FROM transcriptions WHERE tg_chat_id = 250264900"
            ).fetchone()["tg_link"]

    @pytest.mark.parametrize("argv", [
        ["clear-bogus-links", "--dry-run"],
        ["--dry-run", "clear-bogus-links"],
    ], ids=["dry-run-after", "dry-run-before"])
    def test_dry_run_writes_nothing_either_word_order(self, tmp_path, argv):
        db = str(tmp_path / "t.db")
        init_database(db)
        self._seed(db)
        rc = link_repair.main(["--db", db] + argv)
        assert rc == 0
        assert self._bogus_link(db) == "https://t.me/andriy_petrenko/515482", (
            "--dry-run in any word order must not write to the DB")

    def test_no_dry_run_actually_clears(self, tmp_path):
        db = str(tmp_path / "t.db")
        init_database(db)
        self._seed(db)
        rc = link_repair.main(["--db", db, "clear-bogus-links"])
        assert rc == 0
        assert self._bogus_link(db) is None

    def test_second_apply_is_a_no_op(self, tmp_path):
        db = str(tmp_path / "t.db")
        init_database(db)
        self._seed(db)
        first = link_repair.clear_bogus_private_links(db, dry_run=False)
        second = link_repair.clear_bogus_private_links(db, dry_run=False)
        assert first["cleared"] == 1
        assert second["matched"] == 0 and second["cleared"] == 0


# ---------------------------------------------------------------------------
# Listener control-API errors go through the svc_telegram catalog
# ---------------------------------------------------------------------------

class TestListenerErrorCatalog:
    def test_control_error_is_translated_to_en_and_uk(self, monkeypatch):
        from app.core.i18n import tr
        key = "svc_telegram.listener.err.chatIdRequired"
        assert tr(key, lang="en") == "chat_id is required"
        assert tr(key, lang="uk") == "chat_id обов'язковий"
        monkeypatch.setenv("RECALL_UI_LANG", "en")
        assert L._ctl_err(key) == "chat_id is required"
