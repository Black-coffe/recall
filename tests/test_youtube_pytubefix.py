"""Юніт-тести YouTube pytubefix backend (app/services/youtube_pytubefix.py) — T7.5.

Офлайн, БЕЗ мережі: pytubefix.YouTube і subprocess (ffmpeg) підмінюються
фейками (модуль спроєктований з DI саме для цього — callbacks
download_progress_set/add_log/get_db_conn інжектяться параметрами). Покриває:
  - _classify_error: технічне повідомлення pytubefix → людське повідомлення.
  - quality → bitrate: наскрізний шлях від параметра quality до реального
    аргументу '-ab' в команді ffmpeg (мок-стрім, мок-subprocess).
  - Успішний прохід download_youtube_audio (save_to_library True/False).
  - Помилка на етапі YouTube(...) → error-статус з класифікованим текстом.
  - Обрізка (trim_params) — trim_audio_file мокнуто.
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
    # повідомлення, що містить і "sign in" і "unavailable" — sign-in перевіряється першим
    msg = "Sign in to confirm — video may be unavailable otherwise"
    assert yp._classify_error(msg) == "YouTube вимагає авторизації"


# ============================================================
# Fakes для pytubefix / ffmpeg / DB
# ============================================================

class FakeStream:
    def __init__(self, mime_type="audio/mp4", abr="128kbps", filesize=2048):
        self.mime_type = mime_type
        self.abr = abr
        self.filesize = filesize

    def download(self, output_path, filename):
        p = os.path.join(output_path, filename + ".mp4")
        with open(p, "wb") as f:
            f.write(b"fake-audio-bytes")
        return p


class FakeStreamsQuery:
    def __init__(self, stream):
        self._stream = stream

    def filter(self, only_audio=True):
        return self

    def order_by(self, key):
        return self

    def desc(self):
        return self

    def first(self):
        return self._stream


class FakeYouTube:
    """Замінює pytubefix.YouTube — жодного мережевого виклику."""
    last_kwargs = None

    def __init__(self, url, on_progress_callback=None, on_complete_callback=None):
        FakeYouTube.last_kwargs = dict(url=url)
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

    def __call__(self, url, on_progress_callback=None, on_complete_callback=None):
        raise Exception(self._message)


def _fake_ffmpeg_run_factory(captured_cmds, captured_kwargs=None):
    """subprocess.run замінник: НЕ запускає реальний ffmpeg — просто «конвертує»,
    записуючи фейковий вміст у вихідний mp3-шлях (останній аргумент команди), і
    записує саму команду для подальшої перевірки '-ab' бітрейту.

    ``**kwargs`` тут обовʼязковий: без нього нові іменовані параметри виклику
    (creationflags) дали б TypeError, який ковтає try/except у конвертації —
    падало б далеко від причини. Тому kwargs не просто ковтаємо, а віддаємо
    назад для перевірки."""
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
    raise AssertionError("get_db_conn НЕ мав викликатись при save_to_library=False")


# ============================================================
# quality → bitrate (наскрізний шлях, мок-стрім + мок-ffmpeg)
# ============================================================

@pytest.mark.parametrize("quality,expected_bitrate", [
    ("best", "320"), ("high", "256"), ("medium", "192"), ("low", "128"),
    ("unknown-quality", "192"),  # немає в _QUALITY_BITRATE → дефолт '192'
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

    # На Windows ffmpeg мусить стартувати без власного консольного вікна: під
    # pythonw.exe (бойовий запуск app.py) дитина без прапорця блимає чорним
    # вікном. Прапорець губиться мовчки — тому пінимо його тестом.
    assert captured_kwargs[0].get("creationflags") == NO_WINDOW

    final = progress_updates[-1]
    assert final["status"] == "completed"
    assert final["file_path"].endswith(".mp3")
    assert os.path.exists(final["file_path"])


# ============================================================
# Успішний прохід — без бібліотеки
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
    assert "database_id" not in final  # без save_to_library — БД не чіпаємо
    complete_logs = [l for l in logs if l["stage"] == "complete"]
    assert complete_logs and complete_logs[-1]["status"] == "completed"


def test_download_stream_selection_picks_highest_abr_audio_only(monkeypatch, tmp_path: Path, callbacks):
    """audio_stream = streams.filter(only_audio=True).order_by('abr').desc().first() —
    перевіряємо, що саме ЦЕЙ ланцюжок викликається (а не якийсь інший добір)."""
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
# Помилка на етапі YouTube(...)
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
# save_to_library=True — реальна тимчасова SQLite БД
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
    """T4.6: youtube_id УНІКАЛЬНИЙ — повторне завантаження раніше soft-deleted
    відео має «оживити» той самий рядок, а не впасти на UNIQUE constraint."""
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
    assert final["database_id"] == old_id  # той самий рядок, не новий INSERT

    with get_db_connection(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM audio_downloads WHERE youtube_id='abc123XYZ'").fetchone()
        assert rows["n"] == 1  # не задубльовано
        row = conn.execute("SELECT title, deleted_at FROM audio_downloads WHERE id=?", (old_id,)).fetchone()
        assert row["title"] == "Test Video"  # оновлено свіжими даними
        assert row["deleted_at"] is None  # «оживлено»


# ============================================================
# Обрізка (trim_params)
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
    assert not final["file_path"].endswith("_trimmed.mp3")  # fallback на повний файл
    trim_warns = [l for l in logs if l["stage"] == "trim" and l["status"] == "warning"]
    assert trim_warns
