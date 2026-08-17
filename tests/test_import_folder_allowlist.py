"""Тести allowlist коренів для /api/documents/import-folder (T1.3, Волна 1).

Лёгкі — реєструємо documents_bp у мінімальному Flask app (без app.py/DB),
за зразком tests/test_recording_blueprint.py. Мокаємо state.job_queue,
щоб не чіпати справжню чергу задач. RECALL_IMPORT_ROOTS підмінюється через
monkeypatch.setenv — не через .env файл проєкту.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from flask import Flask

from app import state
from app.blueprints.documents import documents_bp


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(documents_bp)
    app.config['TESTING'] = True
    app.config['DATABASE'] = str(tmp_path / "test.db")
    app.config['DOCUMENTS_FOLDER'] = str(tmp_path / "documents")

    original_job_queue = state.job_queue
    state.job_queue = MagicMock()

    yield app.test_client()

    state.job_queue = original_job_queue


def _make_folder_with_doc(base, name="sub"):
    folder = base / name
    folder.mkdir()
    (folder / "note.md").write_text("hello", encoding="utf-8")
    return folder


class TestImportFolderAllowlist:
    def test_empty_allowlist_forbids_import(self, client, tmp_path, monkeypatch):
        monkeypatch.delenv("RECALL_IMPORT_ROOTS", raising=False)
        folder = _make_folder_with_doc(tmp_path)

        r = client.post("/api/documents/import-folder", json={"path": str(folder)})

        assert r.status_code == 403
        body = r.get_json()
        assert body["success"] is False
        assert "RECALL_IMPORT_ROOTS" in body["error"]

    def test_path_outside_allowlist_returns_403(self, client, tmp_path, monkeypatch):
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        monkeypatch.setenv("RECALL_IMPORT_ROOTS", str(allowed_root))

        outside_folder = _make_folder_with_doc(tmp_path, name="outside")

        r = client.post("/api/documents/import-folder", json={"path": str(outside_folder)})

        assert r.status_code == 403
        body = r.get_json()
        assert body["success"] is False

    def test_path_inside_allowlist_root_is_accepted(self, client, tmp_path, monkeypatch):
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        monkeypatch.setenv("RECALL_IMPORT_ROOTS", str(allowed_root))

        inside_folder = _make_folder_with_doc(allowed_root, name="inbox")

        r = client.post("/api/documents/import-folder", json={"path": str(inside_folder)})

        # Проходить гейт allowlist і далі летить у звичайну логіку (job_queue
        # замоканий) — 200 зі started=True, НЕ 403.
        assert r.status_code == 200
        body = r.get_json()
        assert body["success"] is True
        assert body["started"] is True

    def test_multiple_roots_separated_by_semicolon(self, client, tmp_path, monkeypatch):
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        monkeypatch.setenv("RECALL_IMPORT_ROOTS", f"{root_a};{root_b}")

        inside_b = _make_folder_with_doc(root_b, name="inbox_b")

        r = client.post("/api/documents/import-folder", json={"path": str(inside_b)})

        assert r.status_code == 200
        assert r.get_json()["success"] is True

    def test_sibling_dir_with_matching_prefix_rejected(self, client, tmp_path, monkeypatch):
        # allowed = ".../allowed", candidate = ".../allowed_evil/..." — голий
        # startswith хибно пропустив би це; safe_path_within_any має відхилити.
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        monkeypatch.setenv("RECALL_IMPORT_ROOTS", str(allowed_root))

        evil_folder = _make_folder_with_doc(tmp_path, name="allowed_evil")

        r = client.post("/api/documents/import-folder", json={"path": str(evil_folder)})

        assert r.status_code == 403

    def test_ssh_style_path_outside_allowlist_rejected(self, client, tmp_path, monkeypatch):
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        monkeypatch.setenv("RECALL_IMPORT_ROOTS", str(allowed_root))

        ssh_like = tmp_path / ".ssh"
        ssh_like.mkdir()
        (ssh_like / "id_rsa").write_text("fake-key", encoding="utf-8")

        r = client.post("/api/documents/import-folder", json={"path": str(ssh_like)})

        assert r.status_code == 403

    def test_missing_path_still_returns_400_before_allowlist_check(
        self, client, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("RECALL_IMPORT_ROOTS", raising=False)
        r = client.post("/api/documents/import-folder", json={})
        assert r.status_code == 400
        assert r.get_json()["success"] is False
