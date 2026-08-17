"""T8.1 (REMEDIATION_PLAN Волна 4): тести на GET /api/system/diagnostic-package
(app/blueprints/system.py).

Перевіряє критерії приймання T8.1 (простий варіант, без Sentry):
  - Ендпоінт збирає zip з manifest.json + system_info.json + logs/*.
  - У пакет попадають лише ЛОГИ, що реально існують на диску (не порожні
    записи для відсутніх процесів).
  - Redaction: значення, схожі на ключі/токени/хеші (sk-ant-…,
    ANTHROPIC_API_KEY=, TELEGRAM_API_HASH=, TELEGRAM_API_ID=, *_KEY=,
    *_TOKEN=), НЕ потрапляють у пакет у відкритому вигляді.
  - Хвіст логу обрізається до DIAGNOSTIC_LOG_TAIL_LINES (500) рядків.
  - Content-Type відповіді — application/zip.

Навмисно НЕ імпортує app.py (важкий Whisper-імпорт) — той самий паттерн, що
tests/test_settings_api.py: мінімальний Flask-застосунок лише з system_bp,
root_path=tmp_path, щоб файли логів читались із тимчасової директорії.
state.whisper_manager монкіпатчиться на None — ендпоінт має деградувати
(system_info.json = {}), а не падати 500.

Запуск:
    .venv/Scripts/python.exe -m pytest tests/test_diagnostic_package.py -v
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from flask import Flask

from app import state
from app.blueprints.system import DIAGNOSTIC_LOG_TAIL_LINES, system_bp


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    app = Flask(__name__, root_path=str(tmp_path))
    app.register_blueprint(system_bp)
    monkeypatch.setattr(state, "whisper_manager", None)
    return app


@pytest.fixture()
def client(app_module):
    return app_module.test_client()


class TestDiagnosticPackageAssembly:
    def test_returns_zip_with_manifest_and_system_info(self, client):
        r = client.get("/api/system/diagnostic-package")
        assert r.status_code == 200
        assert r.headers["Content-Type"].startswith("application/zip")
        assert "attachment" in r.headers.get("Content-Disposition", "")

        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = zf.namelist()
        assert "manifest.json" in names
        assert "system_info.json" in names

        manifest = json.loads(zf.read("manifest.json"))
        assert "generated_at" in manifest
        assert "app_version" in manifest
        assert "os" in manifest
        # git_commit не гарантований (tmp_path — не git-repo), але ключ є.
        assert "git_commit" in manifest

    def test_system_info_degrades_gracefully_without_whisper_manager(self, client):
        r = client.get("/api/system/diagnostic-package")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        sys_info = json.loads(zf.read("system_info.json"))
        assert sys_info == {}

    def test_includes_only_logs_that_exist_on_disk(self, client, tmp_path):
        (tmp_path / "whisper_ui.log").write_text("2026-07-03 INFO started\n", encoding="utf-8")
        # telegram_listener.log / mcp_server.log навмисно НЕ створюємо.

        r = client.get("/api/system/diagnostic-package")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = zf.namelist()
        assert "logs/whisper_ui.log" in names
        assert "logs/telegram_listener.log" not in names
        assert "logs/mcp_server.log" not in names

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["included_logs"] == ["whisper_ui.log"]


class TestDiagnosticPackageRedaction:
    def test_redacts_anthropic_key_and_telegram_creds(self, client, tmp_path):
        fake_key = "sk-ant-api03-FAKESECRETVALUE1234567890abcdef"
        (tmp_path / "whisper_ui.log").write_text(
            "2026-07-03 INFO started\n"
            f"ANTHROPIC_API_KEY={fake_key}\n"
            "TELEGRAM_API_HASH=deadbeefcafebabe1234\n"
            "TELEGRAM_API_ID=987654\n"
            "SECRET_KEY=super-secret-flask-key\n",
            encoding="utf-8",
        )

        r = client.get("/api/system/diagnostic-package")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        content = zf.read("logs/whisper_ui.log").decode("utf-8")

        assert fake_key not in content
        assert "deadbeefcafebabe1234" not in content
        assert "987654" not in content
        assert "super-secret-flask-key" not in content
        assert "REDACTED" in content
        # Не-секретний рядок лишається читабельним.
        assert "started" in content

    def test_redacts_bare_anthropic_key_without_assignment(self, client, tmp_path):
        """Ключ, що трапився у тексті помилки (не в парі ІМ'Я=значення)."""
        fake_key = "sk-ant-api03-BAREKEYNOTASSIGNED9876543210"
        (tmp_path / "whisper_ui.log").write_text(
            f"anthropic.APIError: invalid key {fake_key} rejected\n", encoding="utf-8",
        )
        r = client.get("/api/system/diagnostic-package")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        content = zf.read("logs/whisper_ui.log").decode("utf-8")
        assert fake_key not in content
        assert "REDACTED" in content


class TestDiagnosticPackageLogTail:
    def test_caps_log_tail_at_max_lines(self, client, tmp_path):
        total = DIAGNOSTIC_LOG_TAIL_LINES + 200
        (tmp_path / "whisper_ui.log").write_text(
            "\n".join(f"line{i}" for i in range(total)) + "\n", encoding="utf-8",
        )
        r = client.get("/api/system/diagnostic-package")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        content = zf.read("logs/whisper_ui.log").decode("utf-8")
        kept = [l for l in content.splitlines() if l]
        assert len(kept) == DIAGNOSTIC_LOG_TAIL_LINES
        assert kept[0] == f"line{total - DIAGNOSTIC_LOG_TAIL_LINES}"
        assert kept[-1] == f"line{total - 1}"
