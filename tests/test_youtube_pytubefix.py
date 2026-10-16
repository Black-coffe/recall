"""Unit tests for the YouTube pytubefix backend (app/services/youtube_pytubefix.py) - T7.5.

Offline, NO network: pytubefix.YouTube and subprocess (ffmpeg) are replaced
with fakes (the module is designed with DI exactly for this - the callbacks
download_progress_set/add_log/get_db_conn are injected as parameters). Covers:
  - _classify_error: technical pytubefix message → human-readable message.
  - quality → bitrate: end-to-end path from the quality parameter to the real
    '-ab' argument in the ffmpeg command (mock stream, mock subprocess).
  - Successful download_youtube_audio run (save_to_library True/False).
  - Failure at the YouTube(...) stage → error status with the classified text.
  - Trim (trim_params) - trim_audio_file is mocked.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import app.services.youtube_pytubefix as yp
from app.utils.proc import NO_WINDOW
from app.db.connection import get_db_connection
from app.db.migrations import init_database


# ============================================================
# _classify_error
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("Sign in to confirm you're not a bot", "YouTube вимагає авторизації"),
    ("SIGN IN required", "YouTube вимагає авторизації"),
    ("This video is unavailable", "Відео недоступне або видалене"),
    ("Private video", "Приватне відео"),
    ("Video blocked due to copyright claim", "Відео заблоковано через авторські права"),
    ("Some completely unrelated network error", "Помилка завантаження відео"),
    ("", "Помилка завантаження відео"),
])
def test_classify_error(raw, expected):
    assert yp._classify_error(raw) == expected


def test_classify_error_priority_sign_in_before_unavailable():
    # a message containing both "sign in" and "unavailable" - sign-in is checked first
    msg = "Sign in to confirm — video may be unavailable otherwise"
    assert yp._classify_error(msg) == "YouTube вимагає авторизації"


# ============================================================
# Fakes for pytubefix / ffmpeg / DB
# ============================================================

class FakeStream:
    def __init__(self, mime_type="audio/mp4", abr="128kbps", filesize=2048, is_sabr=False):
        self.mime_type = mime_type
        self.abr = abr
        self.filesize = filesize
        # SABR streams can only be downloaded through ServerAbrStream and need a session
        # PO-token - the code deliberately skips them, so the fake must carry this flag.
        self.is_sabr = is_sabr
        self.downloaded = False

    def download(self, output_path, filename):
        self.downloaded = True
        p = os.path.join(output_path, filename + ".mp4")
        with open(p, "wb") as f:
            f.write(b"fake-audio-bytes")
        return p


class FakeStreamsQuery:
    def __init__(self, *streams):
        self._streams = list(streams)

    def filter(self, only_audio=True):
        return self

    def order_by(self, key):
        return self

    def desc(self):
        return self

    def __iter__(self):
        return iter(self._streams)

    def first(self):
        return self._streams[0] if self._streams else None


class FakeYouTube:
    """Replaces pytubefix.YouTube - no network calls at all."""
    last_kwargs = None

    def __init__(self, url, client=None, on_progress_callback=None,
                 on_complete_callback=None, **kwargs):
        # ``client`` and ``**kwargs`` are mandatory: open_youtube iterates over the pytubefix
        # clients, and a fake without this parameter "fails" on each of them, and the call
        # looks like a network error rather than a drift of the fake's signature.
        FakeYouTube.last_kwargs = dict(url=url, client=client)
        self.title = "Test Video"
        self.author = "Test Author"
        self.length = 120
        self.thumbnail_url = "http://example.com/thumb.jpg"
        self.video_id = "abc123XYZ"
        self.streams = FakeStreamsQuery(FakeStream())
        self._on_progress = on_progress_callback
        self._on_complete = on_complete_callback


class FakeYouTubeRaises:
    def __init__(self, message):
        self._message = message

    def __call__(self, url, client=None, on_progress_callback=None,
                 on_complete_callback=None, **kwargs):
        raise Exception(self._message)


def _fake_ffmpeg_run_factory(captured_cmds, captured_kwargs=None):
    """subprocess.run substitute: does NOT run the real ffmpeg - it just "converts",
    writing fake content to the output mp3 path (the last command argument), and
    records the command itself for the later '-ab' bitrate check.

    ``**kwargs`` is mandatory here: without it, new keyword arguments of the call
    (creationflags) would raise a TypeError that the try/except in the conversion swallows -
    it would fail far from the cause. So kwargs are not just swallowed but handed
    back for checking."""
    def _fake_run(cmd, capture_output=True, text=True, encoding=None, errors=None,
                  timeout=None, **kwargs):
        captured_cmds.append(cmd)
        if captured_kwargs is not None:
            captured_kwargs.append(kwargs)
        out_path = cmd[-1]
        with open(out_path, "wb") as f:
            f.write(b"fake-mp3-bytes")

        class _Result:
            returncode = 0
            stderr = ""
        return _Result()
    return _fake_run


@pytest.fixture()
def callbacks():
    progress_updates = []
    logs = []

    def download_progress_set(download_id, data):
        progress_updates.append(dict(data))

    def add_log(download_id, stage, message, progress, status):
        logs.append({"stage": stage, "message": message, "progress": progress, "status": status})

    return progress_updates, logs, download_progress_set, add_log


def _noop_get_db_conn():
    raise AssertionError("get_db_conn must NOT be called with save_to_library=False")


# ============================================================
# quality → bitrate (end-to-end path, mock stream + mock ffmpeg)
# ============================================================

@pytest.mark.parametrize("quality,expected_bitrate", [
    ("best", "320"), ("high", "256"), ("medium", "192"), ("low", "128"),
    ("unknown-quality", "192"),  # not in _QUALITY_BITRATE → default '192'
])
def test_download_quality_maps_to_ffmpeg_bitrate(monkeypatch, tmp_path: Path, callbacks, quality, expected_bitrate):
    progress_updates, logs, dl_set, add_log = callbacks
    monkeypatch.setattr("pytubefix.YouTube", FakeYouTube)
    captured_cmds = []
    captured_kwargs = []
    monkeypatch.setattr(yp.subprocess, "run",
                        _fake_ffmpeg_run_factory(captured_cmds, captured_kwargs))

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl1",
        save_to_library=False, quality=quality,
        youtube_folder=str(tmp_path),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    ab_index = cmd.index("-ab")
    assert cmd[ab_index + 1] == f"{expected_bitrate}k"

    # On Windows ffmpeg must start without its own console window: under
    # pythonw.exe (the production app.py launch) a child without the flag flashes a black
    # window. The flag gets lost silently - so we pin it with a test.
    assert captured_kwargs[0].get("creationflags") == NO_WINDOW

    final = progress_updates[-1]
    assert final["status"] == "completed"
    assert final["file_path"].endswith(".mp3")
    assert os.path.exists(final["file_path"])


# ============================================================
# Successful run - without the library
# ============================================================

def test_download_success_without_library_reports_completed(monkeypatch, tmp_path: Path, callbacks):
    progress_updates, logs, dl_set, add_log = callbacks
    monkeypatch.setattr("pytubefix.YouTube", FakeYouTube)
    monkeypatch.setattr(yp.subprocess, "run", _fake_ffmpeg_run_factory([]))

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl2",
        save_to_library=False, quality="high",
        youtube_folder=str(tmp_path),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )

    final = progress_updates[-1]
    assert final["status"] == "completed"
    assert final["info"]["title"] == "Test Video"
    assert final["info"]["video_id"] == "abc123XYZ"
    assert "database_id" not in final  # without save_to_library - the DB is not touched
    complete_logs = [l for l in logs if l["stage"] == "complete"]
    assert complete_logs and complete_logs[-1]["status"] == "completed"


def test_download_stream_selection_picks_highest_abr_audio_only(monkeypatch, tmp_path: Path, callbacks):
    """audio_stream = streams.filter(only_audio=True).order_by('abr').desc().first() -
    check that exactly THIS chain is called (and not some other selection)."""
    progress_updates, logs, dl_set, add_log = callbacks
    calls = {"filter": 0, "order_by": None, "desc": 0}

    class TrackedStreamsQuery(FakeStreamsQuery):
        def filter(self, only_audio=True):
            calls["filter"] += 1
            assert only_audio is True
            return self

        def order_by(self, key):
            calls["order_by"] = key
            return self

        def desc(self):
            calls["desc"] += 1
            return self

    class TrackedYouTube(FakeYouTube):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.streams = TrackedStreamsQuery(FakeStream())

    monkeypatch.setattr("pytubefix.YouTube", TrackedYouTube)
    monkeypatch.setattr(yp.subprocess, "run", _fake_ffmpeg_run_factory([]))

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=x", "dl3", save_to_library=False,
        youtube_folder=str(tmp_path), download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )
    assert calls == {"filter": 1, "order_by": "abr", "desc": 1}


# ============================================================
# Failure at the YouTube(...) stage
# ============================================================

@pytest.mark.parametrize("raw_error,expected_user_msg", [
    ("Video is unavailable in your region", "Відео недоступне або видалене"),
    ("This is a private video", "Приватне відео"),
])
def test_download_error_reports_classified_message(monkeypatch, tmp_path: Path, callbacks, raw_error, expected_user_msg):
    progress_updates, logs, dl_set, add_log = callbacks
    monkeypatch.setattr("pytubefix.YouTube", FakeYouTubeRaises(raw_error))

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=bad", "dl4", save_to_library=False,
        youtube_folder=str(tmp_path), download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )

    assert progress_updates[-1] == {"status": "error", "error": expected_user_msg}


# ============================================================
# save_to_library=True - a real temporary SQLite DB
# ============================================================

@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    init_database(path)
    return path


def test_download_saves_new_row_to_library(monkeypatch, tmp_path: Path, callbacks, db_path):
    progress_updates, logs, dl_set, add_log = callbacks
    monkeypatch.setattr("pytubefix.YouTube", FakeYouTube)
    monkeypatch.setattr(yp.subprocess, "run", _fake_ffmpeg_run_factory([]))
    yt_folder = tmp_path / "yt"
    yt_folder.mkdir()

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl5",
        save_to_library=True, quality="best",
        youtube_folder=str(yt_folder),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=lambda: get_db_connection(db_path),
    )

    final = progress_updates[-1]
    assert final["status"] == "completed"
    assert "database_id" in final

    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT youtube_id, title, audio_quality, audio_format, deleted_at "
            "FROM audio_downloads WHERE youtube_id = ?", ("abc123XYZ",),
        ).fetchone()
    assert row is not None
    assert row["title"] == "Test Video"
    assert row["audio_quality"] == "320"
    assert row["audio_format"] == "mp3"
    assert row["deleted_at"] is None


def test_download_revives_soft_deleted_row_instead_of_duplicate_insert(monkeypatch, tmp_path: Path, callbacks, db_path):
    """T4.6: youtube_id is UNIQUE - re-downloading a previously soft-deleted
    video must "revive" the same row, not fail on the UNIQUE constraint."""
    progress_updates, logs, dl_set, add_log = callbacks
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO audio_downloads (youtube_url, youtube_id, title, author, duration, "
            "file_path, file_size, audio_quality, audio_format, download_time, deleted_at) "
            "VALUES ('u', 'abc123XYZ', 'Old Title', 'Old Author', 10, 'old.mp3', 1, '128', "
            "'mp3', 1.0, 999999.0)"
        )
        conn.commit()
        old_id = conn.execute(
            "SELECT id FROM audio_downloads WHERE youtube_id='abc123XYZ'"
        ).fetchone()["id"]

    monkeypatch.setattr("pytubefix.YouTube", FakeYouTube)
    monkeypatch.setattr(yp.subprocess, "run", _fake_ffmpeg_run_factory([]))
    yt_folder = tmp_path / "yt2"
    yt_folder.mkdir()

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl6",
        save_to_library=True, quality="best",
        youtube_folder=str(yt_folder),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=lambda: get_db_connection(db_path),
    )

    final = progress_updates[-1]
    assert final["database_id"] == old_id  # the same row, not a new INSERT

    with get_db_connection(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM audio_downloads WHERE youtube_id='abc123XYZ'").fetchone()
        assert rows["n"] == 1  # not duplicated
        row = conn.execute("SELECT title, deleted_at FROM audio_downloads WHERE id=?", (old_id,)).fetchone()
        assert row["title"] == "Test Video"  # updated with fresh data
        assert row["deleted_at"] is None  # "revived"


# ============================================================
# Trim (trim_params)
# ============================================================

def test_download_with_trim_uses_trimmed_file_and_removes_original(monkeypatch, tmp_path: Path, callbacks):
    progress_updates, logs, dl_set, add_log = callbacks
    monkeypatch.setattr("pytubefix.YouTube", FakeYouTube)
    monkeypatch.setattr(yp.subprocess, "run", _fake_ffmpeg_run_factory([]))

    def fake_trim(src, dst, start, end, add_log=None):
        with open(dst, "wb") as f:
            f.write(b"trimmed-bytes")
        return True

    monkeypatch.setattr(yp, "trim_audio_file", fake_trim)

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl7",
        save_to_library=False, quality="best",
        start_time=5.0, end_time=15.0,
        youtube_folder=str(tmp_path),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )

    final = progress_updates[-1]
    assert final["status"] == "completed"
    assert final["file_path"].endswith("_trimmed.mp3")
    assert final["info"]["trimmed"] is True
    assert os.path.exists(final["file_path"])


def test_download_trim_failure_falls_back_to_full_file(monkeypatch, tmp_path: Path, callbacks):
    progress_updates, logs, dl_set, add_log = callbacks
    monkeypatch.setattr("pytubefix.YouTube", FakeYouTube)
    monkeypatch.setattr(yp.subprocess, "run", _fake_ffmpeg_run_factory([]))
    monkeypatch.setattr(yp, "trim_audio_file", lambda *a, **k: False)

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl8",
        save_to_library=False, quality="best",
        start_time=1.0, end_time=2.0,
        youtube_folder=str(tmp_path),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )

    final = progress_updates[-1]
    assert final["status"] == "completed"
    assert not final["file_path"].endswith("_trimmed.mp3")  # fallback to the full file
    trim_warns = [l for l in logs if l["stage"] == "trim" and l["status"] == "warning"]
    assert trim_warns


# --- open_youtube: client iteration -----------------------------------------
#
# YouTube serves videoDetails of some videos only to clients with a PO-token
# (the WEB family), and raises BotDetection for the default ANDROID_VR. The iteration exists
# exactly for this - the tests make sure it does not degenerate into a single client.


class _ClientAwareFakeYouTube:
    """A fake that "opens" only for the allowed clients."""

    def __init__(self, ok_clients):
        self.ok_clients = set(ok_clients)
        self.tried = []

    def __call__(self, url, client=None, **kwargs):
        self.tried.append(client)
        if client not in self.ok_clients:
            raise RuntimeError(f"{client} detected as a bot")
        yt = FakeYouTube(url, client=client, **kwargs)
        return yt


def test_open_youtube_falls_back_to_next_client(monkeypatch):
    fake = _ClientAwareFakeYouTube({"WEB"})
    monkeypatch.setattr("pytubefix.YouTube", fake)

    yt = yp.open_youtube("https://youtube.com/watch?v=abc123XYZ")

    assert yt.title == "Test Video"
    assert fake.tried[0] == yp.YOUTUBE_CLIENTS[0]  # the default is tried first
    assert fake.tried[-1] == "WEB"


def test_open_youtube_raises_last_error_when_no_client_works(monkeypatch):
    fake = _ClientAwareFakeYouTube(set())
    monkeypatch.setattr("pytubefix.YouTube", fake)

    with pytest.raises(RuntimeError):
        yp.open_youtube("https://youtube.com/watch?v=abc123XYZ")

    assert fake.tried == list(yp.YOUTUBE_CLIENTS)  # all were tried, none silently


def test_classify_error_names_bot_detection():
    msg = "This request was detected as a bot. DO NOT OPEN AN ISSUE!"
    assert "bot detection" in yp._classify_error(msg).lower()
    assert yp._classify_error("щось інше", default="Своя помилка") == "Своя помилка"


def test_download_skips_sabr_stream_in_favour_of_plain_url(monkeypatch, tmp_path: Path, callbacks):
    """A SABR stream cannot be downloaded without a session PO-token - take an ordinary one, even a worse one."""
    progress_updates, logs, dl_set, add_log = callbacks
    sabr = FakeStream(mime_type="audio/webm", abr="160kbps", is_sabr=True)
    plain = FakeStream(mime_type="audio/mp4", abr="128kbps", is_sabr=False)

    class _SabrFirstYouTube(FakeYouTube):
        def __init__(self, url, client=None, **kwargs):
            super().__init__(url, client=client, **kwargs)
            self.streams = FakeStreamsQuery(sabr, plain)

    monkeypatch.setattr("pytubefix.YouTube", _SabrFirstYouTube)
    monkeypatch.setattr(yp.subprocess, "run", _fake_ffmpeg_run_factory([]))

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl_sabr",
        save_to_library=False, quality="best",
        youtube_folder=str(tmp_path),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )

    assert progress_updates[-1]["status"] == "completed"
    assert plain.downloaded and not sabr.downloaded


def test_download_falls_back_to_ytdlp_when_pytubefix_fails(monkeypatch, tmp_path: Path, callbacks):
    """YouTube cuts off part of the videos without a PO-token - then yt-dlp downloads, not an "error"."""
    progress_updates, logs, dl_set, add_log = callbacks

    class _FailingStream(FakeStream):
        def download(self, output_path, filename):
            raise Exception("HTTP Error 403: Forbidden")

    class _FailingYouTube(FakeYouTube):
        def __init__(self, url, client=None, **kwargs):
            super().__init__(url, client=client, **kwargs)
            self.streams = FakeStreamsQuery(_FailingStream())

    called = {}

    def fake_ytdlp(url, output_path, filename, on_progress=None):
        called["url"] = url
        path = os.path.join(output_path, filename + ".webm")
        with open(path, "wb") as f:
            f.write(b"fake-audio-bytes")
        if on_progress:
            on_progress(16, 16)
        return path

    monkeypatch.setattr("pytubefix.YouTube", _FailingYouTube)
    monkeypatch.setattr(yp, "download_audio_via_ytdlp", fake_ytdlp)
    monkeypatch.setattr(yp.subprocess, "run", _fake_ffmpeg_run_factory([]))

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl_ytdlp",
        save_to_library=False, quality="best",
        youtube_folder=str(tmp_path),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )

    assert called["url"] == "https://youtube.com/watch?v=abc123XYZ"
    assert progress_updates[-1]["status"] == "completed"


def test_download_reports_error_when_ytdlp_also_fails(monkeypatch, tmp_path: Path, callbacks):
    """The fallback must not hide a failure: if both failed - it is an error, not silence."""
    progress_updates, logs, dl_set, add_log = callbacks

    class _FailingStream(FakeStream):
        def download(self, output_path, filename):
            raise Exception("HTTP Error 403: Forbidden")

    class _FailingYouTube(FakeYouTube):
        def __init__(self, url, client=None, **kwargs):
            super().__init__(url, client=client, **kwargs)
            self.streams = FakeStreamsQuery(_FailingStream())

    def boom(*a, **k):
        raise RuntimeError("yt-dlp finished without a file")

    monkeypatch.setattr("pytubefix.YouTube", _FailingYouTube)
    monkeypatch.setattr(yp, "download_audio_via_ytdlp", boom)

    yp.download_youtube_audio(
        "https://youtube.com/watch?v=abc123XYZ", "dl_both_fail",
        save_to_library=False, quality="best",
        youtube_folder=str(tmp_path),
        download_progress_set=dl_set, add_log=add_log,
        get_db_conn=_noop_get_db_conn,
    )

    assert progress_updates[-1]["status"] == "error"


def test_classify_error_bot_detection_in_english(monkeypatch):
    """The bot-detection text is served from the svc_media catalog and follows the UI language."""
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    msg = "This request was detected as a bot. DO NOT OPEN AN ISSUE!"
    assert yp._classify_error(msg) == "YouTube blocked the request as automated (bot detection)"
    assert yp._classify_error("") == "Video download failed"
