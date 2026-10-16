"""Tests of soft degradation of enrichment when a Claude call fails (REMEDIATION_PLAN
Wave 3, T6.3).

Previously text_polishing.extract_meeting_card() inside _enrich_card() was not
wrapped in try/except — a failure (network, retries exhausted, invalid JSON)
propagated and broke the WHOLE enrich_transcription() (the embed phase, which does NOT depend on
Claude, did not get to run either). Now — "retry_needed" instead of a hard
failure.
"""
from __future__ import annotations

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import enrichment


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "enrich_soft_degrade.db")
    init_database(p)
    with get_db_connection(p) as conn:
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, created_at) "
            "VALUES ('file', 'test-meeting', 'Привіт, це тестовий транскрипт зустрічі.', "
            "'2026-01-01 00:00:00')"
        )
        conn.commit()
    return p


class TestEnrichCardSoftDegradation:
    def test_card_failure_returns_retry_needed_not_raises(self, db_path, monkeypatch):
        """The Claude call fails (e.g. retries exhausted on a transient error) —
        _enrich_card does NOT propagate the exception, returns status=retry_needed."""
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated Claude API failure after retries exhausted")

        monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card", _boom)

        res = enrichment._enrich_card(db_path, 1)
        assert res["status"] == "retry_needed"
        assert res["transcription_id"] == 1
        assert "error" in res and "simulated Claude API failure" in res["error"]

    def test_card_failure_does_not_block_independent_embed_phase(self, db_path, monkeypatch):
        """A card-phase failure must not stop the embed phase (local, no Claude) —
        that is the point of 'one card failing does not break the whole pass'."""
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated Claude API failure")

        embed_calls = []

        def _fake_embed(db_path_, tid, force=False):
            embed_calls.append(tid)
            return {"status": "embedded", "chunks": 3}

        monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card", _boom)
        monkeypatch.setattr(enrichment.text_polishing, "is_available", lambda: True)
        monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: True)
        monkeypatch.setattr(enrichment.embeddings, "chunk_and_embed_transcription", _fake_embed)

        res = enrichment.enrich_transcription(db_path, 1)

        # The card phase degraded softly...
        assert res["card"]["status"] == "retry_needed"
        # ...but the embed phase RAN ANYWAY (previously an exception from the card phase
        # broke enrich_transcription() BEFORE the embed phase call).
        assert embed_calls == [1]
        assert res["embed"]["status"] == "embedded"
        assert res["status"] == "done"  # embed did the work, so the overall status is "done"

    def test_backfill_continues_after_one_card_failure(self, db_path, monkeypatch):
        """backfill() over several transcripts: a card-phase failure on one record
        must not stop the pass over the rest."""
        with get_db_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, transcript_text, created_at) "
                "VALUES ('file', 'second', 'Другий тестовий транскрипт.', '2026-01-02 00:00:00')"
            )
            conn.commit()

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated Claude API failure")

        monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card", _boom)
        monkeypatch.setattr(enrichment.text_polishing, "is_available", lambda: True)
        monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: False)

        result = enrichment.backfill(db_path, force=True)
        # Both records were processed (did not blow up on the first) — and neither
        # is counted in "failed", because this is no longer an unhandled exception.
        assert result["total"] == 2
        assert result["failed"] == 0
