"""Тести м'якої деградації enrichment при збої Claude-виклику (REMEDIATION_PLAN
Волна 3, T6.3).

Раніше text_polishing.extract_meeting_card() усередині _enrich_card() не був
обгорнутий у try/except — збій (мережа, вичерпані retry, некоректний JSON)
прокидався і рвав УСЮ enrich_transcription() (embed-фаза, що НЕ залежить від
Claude, теж не встигала виконатись). Тепер — "retry_needed" замість жорсткого
падіння.
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
        """Claude-виклик падає (напр. вичерпані retry на транзиентній помилці) —
        _enrich_card НЕ прокидає виняток, повертає status=retry_needed."""
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated Claude API failure after retries exhausted")

        monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card", _boom)

        res = enrichment._enrich_card(db_path, 1)
        assert res["status"] == "retry_needed"
        assert res["transcription_id"] == 1
        assert "error" in res and "simulated Claude API failure" in res["error"]

    def test_card_failure_does_not_block_independent_embed_phase(self, db_path, monkeypatch):
        """Збій card-фази не повинен зупиняти embed-фазу (локальна, без Claude) —
        це і є 'збій одного card не рушить весь прохід'."""
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

        # Card-фаза деградувала м'яко...
        assert res["card"]["status"] == "retry_needed"
        # ...але embed-фаза ВСЕ ОДНО виконалась (раніше exception з card-фази
        # рвав enrich_transcription() ще ДО виклику embed-фази).
        assert embed_calls == [1]
        assert res["embed"]["status"] == "embedded"
        assert res["status"] == "done"  # embed зробив роботу, тож загальний статус "done"

    def test_backfill_continues_after_one_card_failure(self, db_path, monkeypatch):
        """backfill() по кількох транскриптах: збій card-фази на одному записі
        не повинен зупиняти прохід по решті."""
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
        # Обидва записи оброблені (не впали ексепшеном на першому) — і жоден
        # НЕ підрахований у "failed", бо це більше не unhandled exception.
        assert result["total"] == 2
        assert result["failed"] == 0
