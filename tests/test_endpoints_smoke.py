"""Integration smoke test for all HTTP endpoints (Phase 5 safety net).

Runs Flask via test_client (in-process) — does not block port 5050.
Tests that the endpoints do not fail with 5xx and return the expected formats.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_endpoints_smoke.py -v
"""
import os
import sys

import pytest


# Without CUDA-dependent imports: WHISPER_BACKEND=openai so as not to load faster.
# But even openai whisper would load a model if asked.
# So we do NOT call /api/transcribe — only read-only endpoints.

@pytest.fixture(scope="module")
def app_module():
    """Import app.py once per module — this is slow because of Whisper init.

    Name conflict: the app/ package shadows app.py. So we import via
    importlib.spec_from_file_location directly from the file path.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)
    db_path = os.path.join(project_root, "whisper_history.db")
    if not os.path.exists(db_path):
        pytest.skip(f"DB not found at {db_path}, smoke test skipped")

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


# ===== Read-only endpoints (must always work) =====

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
        # Each model has name, info, downloaded
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
        # Must be either a snapshot with ts, or an error
        assert "cpu_percent" in data or "error" in data

    def test_polish_availability(self, client):
        r = client.get("/api/polish/availability")
        assert r.status_code == 200
        data = r.get_json()
        assert "available" in data
        # Phase 4.4+: must have models and default_model
        assert "default_model" in data
        assert "models" in data
        assert isinstance(data["models"], list)

    def test_active_library_transcriptions_empty(self, client):
        """GET /api/transcribe/active returns an empty list when there are no active jobs."""
        r = client.get("/api/transcribe/active")
        assert r.status_code == 200
        data = r.get_json()
        assert "active" in data
        assert isinstance(data["active"], list)

    def test_active_library_transcriptions_lifecycle(self, client, app_module):
        """An entry in state.active_library_transcriptions comes out through the endpoint."""
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
        # After cleanup — not returned
        r = client.get("/api/transcribe/active")
        ids = [e['audio_download_id'] for e in r.get_json()['active']]
        assert 999 not in ids

    def test_metrics(self, client):
        r = client.get("/api/metrics")
        assert r.status_code == 200
        # Prometheus format — text/plain
        assert "text/plain" in r.content_type
        body = r.get_data(as_text=True)
        # There must be a gauge for active_jobs
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
        """GET /api/history/<id> now includes entities + action_items (for the
        "Connections" block on the transcript page). We take the first existing record."""
        lst = client.get("/api/history?per_page=1").get_json()
        if not lst.get("transcriptions"):
            pytest.skip("Archive is empty — nothing to check the detail against")
        tid = lst["transcriptions"][0]["id"]
        r = client.get(f"/api/history/{tid}")
        assert r.status_code == 200
        data = r.get_json()
        assert "entities" in data and isinstance(data["entities"], list)
        assert "action_items" in data and isinstance(data["action_items"], list)
        assert "segments" in data and "speakers" in data

    def test_main_page(self, client):
        """/ now serves the new Recall shell (shell + client router)."""
        r = client.get("/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        # New shell: app container + router loader.
        assert 'class="rc-app' in body
        assert 'js/recall/router.js' in body

    def test_deep_link_serves_shell(self, client):
        """A deep link on a non-API path serves the shell (cold start works)."""
        r = client.get("/transcript/2125-build-your-own")
        assert r.status_code == 200
        assert 'class="rc-app' in r.get_data(as_text=True)

    def test_unknown_api_returns_404_not_shell(self, client):
        """An unknown /api/* → 404, NOT the HTML shell (otherwise fetch clients break)."""
        r = client.get("/api/does-not-exist")
        assert r.status_code == 404
        assert 'class="rc-app' not in r.get_data(as_text=True)

# ===== Negative cases (must return 4xx with error) =====

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
        # If ANTHROPIC_API_KEY is not set → 400
        # If set but the id does not exist → 404
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
        # The endpoint always returns 200 with status: not_found
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
        # Phase 15D: 'none' → only transcripts without a direction (category_id IS NULL).
        r = client.get("/api/history?category_id=none&per_page=20")
        assert r.status_code == 200
        data = r.get_json()
        for tx in data["transcriptions"]:
            assert tx["category_id"] is None, f"expected NULL, got {tx['category_id']}"

    def test_history_filter_category_specific(self, client, app_module):
        # If the DB has at least one transcript with a category_id — check the invariant.
        # The direction filter with the taxonomy includes its projects (cat_svc.filter_sql),
        # so the invariant is "in the subtree", not strict id equality.
        from app.db.connection import get_db_connection
        from app.services import categories as cat_svc
        all_resp = client.get("/api/history?per_page=100").get_json()
        labeled = [tx for tx in all_resp["transcriptions"] if tx.get("category_id")]
        if not labeled:
            pytest.skip("no labelled transcripts in the DB — nothing to check")
        cid = labeled[0]["category_id"]
        with get_db_connection(app_module.app.config["DATABASE"]) as conn:
            subtree = set(cat_svc.subtree_ids(conn, cid))
        r = client.get(f"/api/history?category_id={cid}&per_page=50")
        assert r.status_code == 200
        for tx in r.get_json()["transcriptions"]:
            assert tx["category_id"] in subtree

    def test_history_filter_category_invalid_is_400(self, client):
        # Spec directions-projects-tags (02): category_id now goes through
        # categories.parse_filter — a non-digit that is not 'none'/'all' is no longer
        # silently ignored (it would be a quiet filter_leak), but fails with 400.
        r = client.get("/api/history?category_id=garbage&per_page=5")
        assert r.status_code == 400
        assert r.get_json()["success"] is False

    def test_history_search_punctuation_only(self, client):
        # _sanitize_fts_query for "..." returns '' → the backend must return 0 results
        r = client.get("/api/history?search=...&per_page=5")
        assert r.status_code == 200
        data = r.get_json()
        assert data["total"] == 0


# ===== Document upload (Phase 16A) — non-mutating validation only =====

class TestDocumentUpload:
    """We check that the endpoint is registered and rejects invalid input.
    The happy path (a real upload) is NOT tested here — it writes to the live DB and
    triggers GPU embeddings; it is checked manually/separately."""

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
    """Endpoints of the live co-pilot. availability is always 200; the rest — 503 if
    COPILOT_ENABLED=False, otherwise correct 404/400 on nonexistent/bad input.
    Mutates nothing (all error paths are cut off before any write)."""

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


# ===== Document manage (Phase 16E) — non-mutating validation =====

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
        # T1.3 (Wave 1): import-folder is now gated by allowlist roots
        # (RECALL_IMPORT_ROOTS) BEFORE the isdir check — see
        # tests/test_import_folder_allowlist.py for full coverage of the gate.
        # Here we check that a path that is FORMALLY inside an allowed
        # root but does not exist itself still gives 400 "Папку не знайдено".
        monkeypatch.setenv("RECALL_IMPORT_ROOTS", str(tmp_path))
        missing = tmp_path / "no_such_subfolder_xyz123"
        r = client.post("/api/documents/import-folder", json={"path": str(missing)})
        assert r.status_code == 400
        body = r.get_json()
        assert body["success"] is False
        assert "папк" in body["error"].lower()

    def test_import_folder_no_allowlist_returns_403(self, client, monkeypatch):
        # Default "deny until allowed": an empty/unset
        # RECALL_IMPORT_ROOTS → import-folder is rejected from everywhere, even before
        # the folder existence check.
        monkeypatch.delenv("RECALL_IMPORT_ROOTS", raising=False)
        r = client.post("/api/documents/import-folder",
                        json={"path": "/no/such/folder/xyz123"})
        assert r.status_code == 403
        body = r.get_json()
        assert body["success"] is False


class TestThreadSafeProgressStoreCleanup:
    """T2.4 (Wave 2): _cleanup() must not evict active operations.

    The ThreadSafeProgressStore implementation lives in app.py (not in the app/ package),
    so we use the already warmed-up module-scoped app_module fixture
    (like the rest of this file) instead of one more full import of app.py —
    see tests/test_auth_gate.py for why NOT to spawn a second such
    import (the load_dotenv() side effect hits other tests in the session).
    """

    def _make_store(self, app_module, max_size=3):
        return app_module.ThreadSafeProgressStore(max_size=max_size)

    def test_active_entries_never_evicted_over_limit(self, app_module):
        store = self._make_store(app_module, max_size=3)
        # Fill the store with active (non-terminal) operations beyond the limit.
        for i in range(5):
            store._data[f"active-{i}"] = {"status": "processing", "progress": i}
        store._cleanup()
        # No active operation must be evicted, even beyond max_size.
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
        # 4 entries > max_size=3 → the oldest TERMINAL one (done-1) should have been evicted.
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
        # Both terminal ones should have gone, only 3 active ones remain (> max_size=2,
        # but losing active ones is forbidden).
        assert "done-1" not in store._data
        assert "done-2" not in store._data
        assert len(store._data) == 3
        assert all(v["status"] == "processing" for v in store._data.values())


class TestExecutorPoolSplit:
    """T2.5 (Wave 2): live-critical (recording_finalize) and batch — separate pools."""

    def test_live_and_batch_executors_are_distinct(self, app_module):
        assert app_module.executor is not app_module.live_executor

    def test_job_queue_routes_recording_finalize_to_live_executor(self, app_module):
        jq = app_module.job_queue
        assert jq._executor_for_kind("recording_finalize") is app_module.live_executor
        assert jq._executor_for_kind("enrichment_backfill") is jq._slow_executor
        assert jq._executor_for_kind("doc_import") is jq._slow_executor
        assert jq._slow_executor is not app_module.executor
        assert jq._executor_for_kind("youtube_download") is jq._slow_executor
        assert jq._executor_for_kind("video_analysis") is jq._slow_executor
        assert jq._executor_for_kind("enrichment") is app_module.executor


class TestApprovalGateCoverage:
    """mcp-foundations-b story 03: no destructive route of the real app escapes the gate table."""

    PLAN_A1 = {
        "transcription.delete_transcription", "transcription.bulk_delete_transcriptions",
        "memory.delete_category", "memory.merge_category", "memory.delete_tag_route",
        "memory.merge_tags_route", "memory.start_backfill", "speakers.delete_speaker",
        "speakers.merge_speakers", "telegram.tg_backfill", "settings_api.clear_anthropic_key",
        "mail.mail_accounts_delete", "audio_library.delete_audio_download",
        # mcp-mail-and-keys: soft disconnect, backfill beyond 12 months, enrich on
        "mail.mail_accounts_disconnect", "mail.mail_backfill", "mail.mail_label_enrich",
        # mcp-pipeline: folder import and document re-parse (sync route and its job twin)
        "documents.import_folder", "documents.reparse_document", "documents.reparse_job",
        # mcp-telegram-automation: enabling a chat (a new one is created from the listener's dialogs)
        "telegram.tg_enable_chat",
    }
    PLAN_A2_AND_EXTRAS = {
        "comments.delete", "bookmarks.delete_bookmark", "bookmarks.delete_saved_search",
        "speakers.unmerge_speakers", "memory.backfill_status", "telegram.tg_deleted",
        "transcription.merge_segments",
    }

    def test_every_destructive_rule_is_gated_or_exempt(self, app_module):
        import re
        from app.core import approval_gate
        loose = []
        for rule in app_module.app.url_map.iter_rules():
            name = rule.endpoint.rsplit(".", 1)[-1]
            if "DELETE" in rule.methods or re.search(r"delete|merge|purge|backfill", name):
                if rule.endpoint in approval_gate._D_ROUTES:
                    continue
                if approval_gate._D_EXEMPT.get(rule.endpoint, "").strip():
                    continue
                loose.append(f"{sorted(rule.methods)} {rule.rule} -> {rule.endpoint}")
        assert not loose, loose

    def test_gated_and_exempt_tables_match_the_plan(self, app_module):
        from app.core import approval_gate
        assert set(approval_gate._D_ROUTES) == self.PLAN_A1
        assert set(approval_gate._D_EXEMPT) == self.PLAN_A2_AND_EXTRAS
        assert all(reason.strip() for reason in approval_gate._D_EXEMPT.values())
        endpoints = {r.endpoint for r in app_module.app.url_map.iter_rules()}
        assert self.PLAN_A1 <= endpoints

    def test_gate_sits_between_auth_and_audit(self, app_module):
        names = [f.__name__ for f in app_module.app.before_request_funcs[None]]
        assert names.index("_recall_auth_gate") < names.index("_recall_approval_gate") < names.index("_audit_start")
