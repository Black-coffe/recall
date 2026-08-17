"""Integration smoke-test для всех HTTP endpoints (Phase 5 safety net).

Запускає Flask через test_client (in-process) — не блокує порт 5050.
Тестує що ендпоінти не падають з 5xx, повертають очікувані формати.

Запуск:
    .venv/Scripts/python.exe -m pytest tests/test_endpoints_smoke.py -v
"""
import os
import sys

import pytest


# Без CUDA-залежного імпорту: WHISPER_BACKEND=openai щоб не вантажити faster.
# Але навіть openai whisper буде грузити модель, якщо її попросити.
# Тому ми НЕ викликаємо /api/transcribe — тільки read-only ендпоінти.

@pytest.fixture(scope="module")
def app_module():
    """Імпортуємо app.py один раз на module — це довго через Whisper init.

    Конфлікт імен: пакет app/ затіняє app.py. Тому імпортуємо через
    importlib.spec_from_file_location напряму з file path.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)
    db_path = os.path.join(project_root, "whisper_history.db")
    if not os.path.exists(db_path):
        pytest.skip(f"БД не знайдено за шляхом {db_path}, smoke-test пропущено")

    import importlib.util
    app_py_path = os.path.join(project_root, "app.py")
    spec = importlib.util.spec_from_file_location("whisper_app_module", app_py_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["whisper_app_module"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def client(app_module):
    return app_module.app.test_client()


# ===== Read-only endpoints (мають завжди працювати) =====

class TestReadOnlyEndpoints:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.get_json()
        assert "status" in data
        assert "version" in data

    def test_models(self, client):
        r = client.get("/api/models")
        assert r.status_code == 200
        models = r.get_json()
        assert isinstance(models, list)
        assert len(models) > 0
        # Кожна модель має name, info, downloaded
        for m in models:
            assert "name" in m
            assert "info" in m

    def test_system_info(self, client):
        r = client.get("/api/system_info")
        assert r.status_code == 200
        data = r.get_json()
        assert "device" in data
        assert "pytorch_version" in data

    def test_system_stats(self, client):
        r = client.get("/api/system_stats")
        assert r.status_code == 200
        data = r.get_json()
        # Має бути або snapshot з ts, або помилка
        assert "cpu_percent" in data or "error" in data

    def test_polish_availability(self, client):
        r = client.get("/api/polish/availability")
        assert r.status_code == 200
        data = r.get_json()
        assert "available" in data
        # Phase 4.4+: повинні бути models та default_model
        assert "default_model" in data
        assert "models" in data
        assert isinstance(data["models"], list)

    def test_active_library_transcriptions_empty(self, client):
        """GET /api/transcribe/active повертає порожній список без активних джобів."""
        r = client.get("/api/transcribe/active")
        assert r.status_code == 200
        data = r.get_json()
        assert "active" in data
        assert isinstance(data["active"], list)

    def test_active_library_transcriptions_lifecycle(self, client, app_module):
        """Запис у state.active_library_transcriptions виходить через endpoint."""
        from app import state
        store = state.active_library_transcriptions
        store[999] = {
            'audio_download_id': 999,
            'started_at': 12345.0,
            'stage': 'transcribing',
            'progress': 0.42,
        }
        try:
            r = client.get("/api/transcribe/active")
            assert r.status_code == 200
            ids = [e['audio_download_id'] for e in r.get_json()['active']]
            assert 999 in ids
        finally:
            store.pop(999, None)
        # Після cleanup — не повертається
        r = client.get("/api/transcribe/active")
        ids = [e['audio_download_id'] for e in r.get_json()['active']]
        assert 999 not in ids

    def test_metrics(self, client):
        r = client.get("/api/metrics")
        assert r.status_code == 200
        # Prometheus format — text/plain
        assert "text/plain" in r.content_type
        body = r.get_data(as_text=True)
        # Має бути gauge для active_jobs
        assert "whisper_active_jobs" in body

    def test_jobs_list(self, client):
        r = client.get("/api/jobs")
        assert r.status_code == 200
        data = r.get_json()
        assert "jobs" in data
        assert isinstance(data["jobs"], list)

    def test_history_list(self, client):
        r = client.get("/api/history?per_page=5")
        assert r.status_code == 200
        data = r.get_json()
        assert "transcriptions" in data
        assert "total" in data
        assert "page" in data

    def test_audio_downloads_list(self, client):
        r = client.get("/api/audio/downloads?per_page=5")
        assert r.status_code == 200
        data = r.get_json()
        assert "downloads" in data
        assert "total" in data

    def test_history_detail_has_related(self, client):
        """GET /api/history/<id> тепер включає entities + action_items (для блоку
        «Зв'язки» на сторінці транскрипту). Беремо перший наявний запис."""
        lst = client.get("/api/history?per_page=1").get_json()
        if not lst.get("transcriptions"):
            pytest.skip("Архів порожній — нема на чому перевіряти detail")
        tid = lst["transcriptions"][0]["id"]
        r = client.get(f"/api/history/{tid}")
        assert r.status_code == 200
        data = r.get_json()
        assert "entities" in data and isinstance(data["entities"], list)
        assert "action_items" in data and isinstance(data["action_items"], list)
        assert "segments" in data and "speakers" in data

    def test_main_page(self, client):
        """/ тепер віддає новий каркас Recall (shell + клієнтський роутер)."""
        r = client.get("/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        # Новий shell: app-контейнер + завантажувач роутера.
        assert 'class="rc-app' in body
        assert 'js/recall/router.js' in body

    def test_deep_link_serves_shell(self, client):
        """Deep-link на не-API шлях віддає shell (холодний старт працює)."""
        r = client.get("/transcript/2125-build-your-own")
        assert r.status_code == 200
        assert 'class="rc-app' in r.get_data(as_text=True)

    def test_unknown_api_returns_404_not_shell(self, client):
        """Невідомий /api/* → 404, НЕ HTML-shell (інакше fetch-клієнти ламаються)."""
        r = client.get("/api/does-not-exist")
        assert r.status_code == 404
        assert 'class="rc-app' not in r.get_data(as_text=True)

# ===== Negative cases (мають повертати 4xx з error) =====

class TestNegativeCases:
    def test_history_invalid_id(self, client):
        r = client.get("/api/history/999999")
        assert r.status_code == 404

    def test_youtube_info_no_url(self, client):
        r = client.post("/api/youtube/info", json={})
        assert r.status_code == 400

    def test_youtube_info_invalid_url(self, client):
        r = client.post("/api/youtube/info", json={"url": "https://example.com/video"})
        assert r.status_code == 400

    def test_audio_check_duplicate_no_url(self, client):
        r = client.post("/api/audio/check-duplicate", json={})
        assert r.status_code == 400

    def test_polish_no_key_or_invalid_id(self, client):
        # Якщо ANTHROPIC_API_KEY не встановлено → 400
        # Якщо встановлено але id неіснуючий → 404
        r = client.post("/api/transcription/999999/polish", json={})
        assert r.status_code in (400, 404)

    def test_job_not_found(self, client):
        r = client.get("/api/jobs/nonexistent_id")
        assert r.status_code == 404

    def test_cancel_nonexistent_job(self, client):
        r = client.post("/api/jobs/nonexistent_id/cancel")
        assert r.status_code == 400

    def test_download_progress_unknown_id(self, client):
        r = client.get("/api/youtube/progress/nonexistent_download")
        # Endpoint завжди повертає 200 з status: not_found
        assert r.status_code == 200
        data = r.get_json()
        assert data["status"] == "not_found"


# ===== Search endpoints =====

class TestSearchEndpoints:
    def test_history_search_empty(self, client):
        r = client.get("/api/history?search=&per_page=5")
        assert r.status_code == 200

    def test_history_search_unicode(self, client):
        r = client.get("/api/history?search=test&per_page=5")
        assert r.status_code == 200

    def test_history_filter_category_none(self, client):
        # Phase 15D: 'none' → лише транскрипти без напрямку (category_id IS NULL).
        r = client.get("/api/history?category_id=none&per_page=20")
        assert r.status_code == 200
        data = r.get_json()
        for tx in data["transcriptions"]:
            assert tx["category_id"] is None, f"очікувано NULL, отримано {tx['category_id']}"

    def test_history_filter_category_specific(self, client):
        # Якщо у БД є хоч один transcripts з category_id — перевіримо інваріант.
        all_resp = client.get("/api/history?per_page=100").get_json()
        labeled = [tx for tx in all_resp["transcriptions"] if tx.get("category_id")]
        if not labeled:
            pytest.skip("у БД немає розмічених транскриптів — нічого перевіряти")
        cid = labeled[0]["category_id"]
        r = client.get(f"/api/history?category_id={cid}&per_page=50")
        assert r.status_code == 200
        for tx in r.get_json()["transcriptions"]:
            assert tx["category_id"] == cid

    def test_history_filter_category_invalid_falls_back_to_all(self, client):
        # Не-цифра і не 'none'/'all' → ігнорується, повертаються усі (як без фільтра).
        r1 = client.get("/api/history?category_id=garbage&per_page=5")
        r2 = client.get("/api/history?per_page=5")
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.get_json()["total"] == r2.get_json()["total"]

    def test_history_search_punctuation_only(self, client):
        # _sanitize_fts_query для "..." повертає '' → backend має повертати 0 результатів
        r = client.get("/api/history?search=...&per_page=5")
        assert r.status_code == 200
        data = r.get_json()
        assert data["total"] == 0


# ===== Document upload (Phase 16A) — лише non-mutating валідація =====

class TestDocumentUpload:
    """Перевіряємо, що ендпоінт зареєстровано і він відхиляє некоректний ввід.
    Happy-path (реальне завантаження) НЕ тестуємо тут — він пише в живу БД і
    тригерить GPU-embeddings; перевіряється вручну/окремо."""

    def test_upload_no_file_returns_400(self, client):
        r = client.post("/api/documents/upload", data={})
        assert r.status_code == 400
        assert r.get_json()["success"] is False

    def test_upload_unsupported_extension_returns_400(self, client):
        import io
        data = {"document": (io.BytesIO(b"\x00\x01"), "song.mp3")}
        r = client.post("/api/documents/upload", data=data,
                        content_type="multipart/form-data")
        assert r.status_code == 400
        body = r.get_json()
        assert body["success"] is False
        assert "формат" in body["error"].lower()

    def test_upload_empty_filename_returns_400(self, client):
        import io
        data = {"document": (io.BytesIO(b"abc"), "")}
        r = client.post("/api/documents/upload", data=data,
                        content_type="multipart/form-data")
        assert r.status_code == 400


# ===== Co-pilot (Phase 19) — read-only + negative, non-mutating =====

class TestCopilot:
    """Ендпоінти живого ко-пілота. availability завжди 200; решта — 503 якщо
    COPILOT_ENABLED=False, інакше коректні 404/400 на неіснуючому/поганому вводі.
    Нічого не мутує (усі помилкові шляхи відсікаються до запису)."""

    def _enabled(self, client):
        return client.get("/api/copilot/availability").get_json().get("enabled")

    def test_availability_always_200(self, client):
        r = client.get("/api/copilot/availability")
        assert r.status_code == 200
        d = r.get_json()
        assert "enabled" in d and "local_llm_available" in d
        assert "defaults" in d and d["defaults"].get("mode") in ("light", "medium", "hard")
        assert "api_available" in d

    def test_sessions_list(self, client):
        r = client.get("/api/copilot/sessions")
        if not self._enabled(client):
            assert r.status_code == 503
            return
        assert r.status_code == 200
        assert isinstance(r.get_json().get("sessions"), list)

    def test_timeline_unknown_404(self, client):
        r = client.get("/api/copilot/999999999/timeline")
        assert r.status_code == (404 if self._enabled(client) else 503)

    def test_by_transcription_unknown(self, client):
        r = client.get("/api/copilot/by-transcription/999999999")
        if not self._enabled(client):
            assert r.status_code == 503
            return
        assert r.status_code == 200
        assert r.get_json().get("found") is False

    def test_action_bad_action_400(self, client):
        r = client.post("/api/copilot/1/action", json={"action": "nonsense"})
        assert r.status_code == (400 if self._enabled(client) else 503)

    def test_settings_no_fields_400(self, client):
        r = client.post("/api/copilot/1/settings", json={})
        assert r.status_code == (400 if self._enabled(client) else 503)

    def test_export_unknown_404(self, client):
        r = client.get("/api/copilot/999999999/export?format=md")
        assert r.status_code == (404 if self._enabled(client) else 503)

    def test_reingest_unknown_404(self, client):
        r = client.post("/api/copilot/999999999/reingest")
        assert r.status_code == (404 if self._enabled(client) else 503)


# ===== Document manage (Phase 16E) — non-mutating валідація =====

class TestDocumentManage:
    def test_reparse_nonexistent_returns_404(self, client):
        r = client.post("/api/documents/999999999/reparse")
        assert r.status_code == 404
        assert r.get_json()["success"] is False

    def test_import_folder_missing_path_returns_400(self, client):
        r = client.post("/api/documents/import-folder", json={})
        assert r.status_code == 400
        assert r.get_json()["success"] is False

    def test_import_folder_bad_dir_returns_400(self, client, monkeypatch, tmp_path):
        # T1.3 (Волна 1): import-folder тепер гейтиться allowlist-коренями
        # (RECALL_IMPORT_ROOTS) ще ДО перевірки isdir — див.
        # tests/test_import_folder_allowlist.py для повного покриття гейта.
        # Тут перевіряємо, що шлях, який ФОРМАЛЬНО у межах дозволеного
        # кореня, але сам не існує, усе ще дає 400 "Папку не знайдено".
        monkeypatch.setenv("RECALL_IMPORT_ROOTS", str(tmp_path))
        missing = tmp_path / "no_such_subfolder_xyz123"
        r = client.post("/api/documents/import-folder", json={"path": str(missing)})
        assert r.status_code == 400
        body = r.get_json()
        assert body["success"] is False
        assert "папк" in body["error"].lower()

    def test_import_folder_no_allowlist_returns_403(self, client, monkeypatch):
        # Дефолт "заборонити, поки не дозволено": порожній/не заданий
        # RECALL_IMPORT_ROOTS → import-folder відхиляється звідусіль, ще до
        # перевірки існування папки.
        monkeypatch.delenv("RECALL_IMPORT_ROOTS", raising=False)
        r = client.post("/api/documents/import-folder",
                        json={"path": "/no/such/folder/xyz123"})
        assert r.status_code == 403
        body = r.get_json()
        assert body["success"] is False


class TestThreadSafeProgressStoreCleanup:
    """T2.4 (Волна 2): _cleanup() не повинен витісняти активні операції.

    Реалізація ThreadSafeProgressStore живе в app.py (не в app/ пакеті),
    тож використовуємо вже прогрітий module-scoped app_module фікстуру
    (як і решта цього файлу) замість ще одного повного імпорту app.py —
    дивись tests/test_auth_gate.py навіщо НЕ варто плодити другий такий
    імпорт (побічний ефект load_dotenv() б'є по інших тестах в сесії).
    """

    def _make_store(self, app_module, max_size=3):
        return app_module.ThreadSafeProgressStore(max_size=max_size)

    def test_active_entries_never_evicted_over_limit(self, app_module):
        store = self._make_store(app_module, max_size=3)
        # Заповнюємо стор активними (не терминальними) операціями понад ліміт.
        for i in range(5):
            store._data[f"active-{i}"] = {"status": "processing", "progress": i}
        store._cleanup()
        # Жодна активна операція не має бути витіснена, навіть понад max_size.
        assert len(store._data) == 5
        for i in range(5):
            assert f"active-{i}" in store._data

    def test_completed_entries_evicted_first(self, app_module):
        store = self._make_store(app_module, max_size=3)
        store._data["done-1"] = {"status": "completed"}
        store._data["err-1"] = {"status": "error"}
        store._data["active-1"] = {"status": "processing"}
        store._data["active-2"] = {"status": "queued"}
        store._cleanup()
        # 4 записи > max_size=3 → мали витіснити найстаріший ТЕРМІНАЛЬНИЙ (done-1).
        assert "done-1" not in store._data
        assert "err-1" in store._data
        assert "active-1" in store._data
        assert "active-2" in store._data
        assert len(store._data) == 3

    def test_mixed_cleanup_keeps_all_active(self, app_module):
        store = self._make_store(app_module, max_size=2)
        store._data["done-1"] = {"status": "completed"}
        store._data["done-2"] = {"status": "error"}
        store._data["active-1"] = {"status": "processing"}
        store._data["active-2"] = {"status": "processing"}
        store._data["active-3"] = {"status": "processing"}
        store._cleanup()
        # Обидва терминальних мали піти, лишились лише 3 активних (> max_size=2,
        # але втрачати активні заборонено).
        assert "done-1" not in store._data
        assert "done-2" not in store._data
        assert len(store._data) == 3
        assert all(v["status"] == "processing" for v in store._data.values())


class TestExecutorPoolSplit:
    """T2.5 (Волна 2): live-critical (recording_finalize) і batch — окремі пули."""

    def test_live_and_batch_executors_are_distinct(self, app_module):
        assert app_module.executor is not app_module.live_executor

    def test_job_queue_routes_recording_finalize_to_live_executor(self, app_module):
        jq = app_module.job_queue
        assert jq._executor_for_kind("recording_finalize") is app_module.live_executor
        assert jq._executor_for_kind("enrichment_backfill") is app_module.executor
        assert jq._executor_for_kind("doc_import") is app_module.executor
        assert jq._executor_for_kind("youtube_download") is app_module.executor
