"""Phase 23 — Story B-D: video_analysis.py offline test suite.

Tests are OFFLINE: no real ffmpeg, no Tesseract, no embedding model, no GPU.

Run:
    .venv/Scripts/python.exe test_video_analysis.py
    # or
    .venv/Scripts/python.exe -m pytest test_video_analysis.py -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from app.db.migrations import init_database
from app.services.video_analysis import (
    analyze_video,
    resolve_recording_video,
)

# ---------------------------------------------------------------------------
# Monkeypatch targets
# (these are the names AS USED inside video_analysis — checked against source)
#
#   extract_scene_keyframes  → app.services.video_analysis.extract_scene_keyframes
#   _ocr_image_obj           → app.services.document_parser._ocr_image_obj
#                              (called via `from app.services import document_parser as _dp`
#                               then `_dp._ocr_image_obj(img)`)
#   ocr_available            → app.services.document_parser.ocr_available
#   embed_texts              → app.services.embeddings.embed_texts
#                              (called via `_emb.embed_texts([clean])`)
#   embeddings.is_available  → app.services.embeddings.is_available
# ---------------------------------------------------------------------------

_PATCH_EXTRACTOR  = "app.services.video_analysis.extract_scene_keyframes"
_PATCH_OCR        = "app.services.document_parser._ocr_image_obj"
_PATCH_OCR_AVAIL  = "app.services.document_parser.ocr_available"
_PATCH_EMBED      = "app.services.embeddings.embed_texts"
_PATCH_EMB_AVAIL  = "app.services.embeddings.is_available"

_N_FRAMES = 3
# Each OCR text is >=8 non-whitespace chars → triggers chunk insertion
_FAKE_OCR = [
    f"on-screen text {i} INVOICE-{i}" for i in range(_N_FRAMES)
]


def _fake_embedding(texts: list[str], **_) -> np.ndarray:
    """Return zero vectors of shape (N, 1024), dtype float32."""
    return np.zeros((len(texts), 1024), dtype=np.float32)


def _seed_db(db_path: str, tmp_dir: Path, has_video: int = 1) -> tuple[int, str]:
    """Insert minimal rows so resolve_recording_video + analyze_video can run.

    Layout on disk:
        tmp_dir/rec_test/final.mp3          (audio file)
        tmp_dir/rec_test/video_mon0_final.mp4 (primary video, if has_video=1)

    DB rows:
        transcriptions  (source_type='recording', file_path → final.mp3)
        audio_downloads (source_type='recording', recording_session_id='rec_test',
                         has_video=?, primary_video_path=video path)
        recording_video_tracks
                        (recording_session_id='rec_test',
                         file_path=video path   ← MUST match primary_video_path
                         start_offset_sec=0.3)

    Returns (transcription_id, video_path_str).
    """
    session_dir = tmp_dir / "rec_test"
    session_dir.mkdir(parents=True, exist_ok=True)

    mp3_path = str(session_dir / "final.mp3")
    Path(mp3_path).write_bytes(b"\x00" * 128)

    video_path = str(session_dir / "video_mon0_final.mp4")
    if has_video:
        Path(video_path).write_bytes(b"\x00" * 128)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.execute(
        """
        INSERT INTO transcriptions (source_type, source_name, file_path, transcript_text)
        VALUES ('recording', 'test.mp3', ?, 'hello world')
        """,
        (mp3_path,),
    )
    tx_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        """
        INSERT INTO audio_downloads
            (youtube_url, youtube_id, title, file_path,
             source_type, recording_session_id,
             has_video, primary_video_path)
        VALUES ('', 'rec_test', 'Test rec', ?,
                'recording', 'rec_test',
                ?, ?)
        """,
        (mp3_path, has_video, video_path if has_video else None),
    )

    # recording_video_tracks.file_path MUST match primary_video_path so that
    # resolve_recording_video's JOIN finds the start_offset_sec.
    conn.execute(
        """
        INSERT INTO recording_video_tracks
            (recording_session_id, track_id, monitor_index,
             file_path, status, start_offset_sec)
        VALUES ('rec_test', 'mon0', 0, ?, 'finalized', 0.3)
        """,
        (video_path,),
    )

    conn.commit()
    conn.close()
    return tx_id, video_path


def _make_frame_factory(tmp_dir: Path):
    """Return a function that creates real tiny PNG files and returns (ts, path) list."""
    frame_dir = tmp_dir / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)

    # Minimal 1×1 white PNG (valid file so PIL.Image.open won't be needed if OCR
    # is also patched, but it's a real file so stat calls succeed).
    _TINY_PNG = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
        b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def _extractor(video_path: str, out_dir: str, **_) -> list[tuple[float, str]]:
        results = []
        for i in range(_N_FRAMES):
            p = frame_dir / f"frame_{i:04d}.png"
            p.write_bytes(_TINY_PNG)
            results.append((float(i + 1) * 5.0, str(p)))
        return results

    return _extractor


# ---------------------------------------------------------------------------
# Context-manager helper to activate all three patches together
# ---------------------------------------------------------------------------

class _AllPatched:
    """Context manager that patches extractor, OCR, and embeddings at once."""

    def __init__(self, tmp_dir: Path, ocr_results=None):
        self._extractor = _make_frame_factory(tmp_dir)
        self._ocr_results = ocr_results if ocr_results is not None else list(_FAKE_OCR)
        self._call_count = 0

    def _ocr(self, img):
        text = self._ocr_results[self._call_count % len(self._ocr_results)]
        self._call_count += 1
        return text

    def __enter__(self):
        self._p1 = patch(_PATCH_EXTRACTOR, side_effect=self._extractor)
        self._p2 = patch(_PATCH_OCR, side_effect=self._ocr)
        self._p3 = patch(_PATCH_OCR_AVAIL, return_value=True)
        self._p4 = patch(_PATCH_EMBED, side_effect=_fake_embedding)
        self._p5 = patch(_PATCH_EMB_AVAIL, return_value=True)
        for p in (self._p1, self._p2, self._p3, self._p4, self._p5):
            p.start()
        return self

    def __exit__(self, *_):
        for p in (self._p1, self._p2, self._p3, self._p4, self._p5):
            p.stop()


# ===========================================================================
# Tests
# ===========================================================================


class TestResolveFindsVideo(unittest.TestCase):
    """resolve_recording_video returns correct info for seeded transcription."""

    def test_resolve_finds_video(self):
        """Returns the recording info dict when has_video=1 and file exists."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = str(tmp / "t.db")
            init_database(db)

            tx_id, video_path = _seed_db(db, tmp, has_video=1)

            result = resolve_recording_video(db, tx_id)

            self.assertIsNotNone(result, "Expected a dict, got None")
            self.assertEqual(result["primary_video_path"], video_path)
            self.assertEqual(result["recording_session_id"], "rec_test")
            self.assertAlmostEqual(result["start_offset_sec"], 0.3, places=3)

    def test_resolve_returns_none_for_no_video(self):
        """Returns None when has_video=0."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = str(tmp / "t.db")
            init_database(db)

            tx_id, _ = _seed_db(db, tmp, has_video=0)

            result = resolve_recording_video(db, tx_id)
            self.assertIsNone(result, f"Expected None for has_video=0, got {result}")


class TestAnalyzeInsertsKeyframesAndChunks(unittest.TestCase):
    """analyze_video(force=True) inserts correct rows."""

    def test_analyze_inserts_keyframes_and_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = str(tmp / "t.db")
            init_database(db)

            tx_id, video_path = _seed_db(db, tmp, has_video=1)

            with _AllPatched(tmp):
                result = analyze_video(db, tx_id, force=True)

            self.assertTrue(result["has_video"], "has_video must be True")
            self.assertNotIn("skipped", result,
                             f"skipped must not be present on success, got {result}")
            self.assertEqual(result["keyframes"], _N_FRAMES,
                             f"Expected {_N_FRAMES} keyframes, got {result['keyframes']}")
            self.assertEqual(result["chunks_added"], _N_FRAMES,
                             f"Expected {_N_FRAMES} chunks, got {result['chunks_added']}")

            # Verify DB rows
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row

            kf_rows = conn.execute(
                "SELECT ts_offset_sec, ocr_text FROM video_keyframes "
                "WHERE transcription_id = ?",
                (tx_id,),
            ).fetchall()
            self.assertEqual(len(kf_rows), _N_FRAMES,
                             f"Expected {_N_FRAMES} video_keyframes rows, got {len(kf_rows)}")

            # ts_offset_sec = raw frame timestamp from extractor (5.0, 10.0, 15.0)
            # analyze_video stores the ts returned by extract_scene_keyframes directly.
            expected_ts = sorted(float(i + 1) * 5.0 for i in range(_N_FRAMES))
            actual_ts = sorted(r["ts_offset_sec"] for r in kf_rows)
            for got, want in zip(actual_ts, expected_ts):
                self.assertAlmostEqual(got, want, places=2,
                                       msg=f"ts_offset_sec mismatch: got {got} want {want}")

            chunk_rows = conn.execute(
                "SELECT speaker, text FROM chunks "
                "WHERE transcription_id = ? AND speaker = 'екран'",
                (tx_id,),
            ).fetchall()
            self.assertEqual(len(chunk_rows), _N_FRAMES,
                             f"Expected {_N_FRAMES} 'екран' chunk rows, got {len(chunk_rows)}")

            # video_analysis_at and count
            tx_row = conn.execute(
                "SELECT video_analysis_at, video_keyframes_count "
                "FROM transcriptions WHERE id = ?",
                (tx_id,),
            ).fetchone()
            self.assertIsNotNone(tx_row["video_analysis_at"],
                                 "video_analysis_at must be set")
            self.assertEqual(tx_row["video_keyframes_count"], _N_FRAMES)
            conn.close()


class TestIdempotentForceRerun(unittest.TestCase):
    """Running analyze_video(force=True) twice leaves stable row counts."""

    def test_idempotent_force_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = str(tmp / "t.db")
            init_database(db)

            tx_id, _ = _seed_db(db, tmp, has_video=1)

            # First run
            with _AllPatched(tmp):
                r1 = analyze_video(db, tx_id, force=True)

            # Second run (force=True clears prior rows then re-inserts)
            with _AllPatched(tmp):
                r2 = analyze_video(db, tx_id, force=True)

            conn = sqlite3.connect(db)
            kf_count = conn.execute(
                "SELECT COUNT(*) FROM video_keyframes WHERE transcription_id = ?",
                (tx_id,),
            ).fetchone()[0]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM chunks "
                "WHERE transcription_id = ? AND speaker = 'екран'",
                (tx_id,),
            ).fetchone()[0]
            conn.close()

            self.assertEqual(kf_count, _N_FRAMES,
                             f"After 2 runs: expected {_N_FRAMES} keyframes, got {kf_count}")
            self.assertEqual(chunk_count, _N_FRAMES,
                             f"After 2 runs: expected {_N_FRAMES} chunks, got {chunk_count}")
            self.assertEqual(r1["keyframes"], r2["keyframes"],
                             "Keyframe count must be stable across force reruns")


class TestNoVideoIsNoop(unittest.TestCase):
    """When transcription has no video (has_video=0), analyze_video is a no-op."""

    def test_no_video_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = str(tmp / "t.db")
            init_database(db)

            tx_id, _ = _seed_db(db, tmp, has_video=0)

            # No patches needed — resolve_recording_video returns None before any I/O
            result = analyze_video(db, tx_id, force=True)

            self.assertFalse(result["has_video"],
                             f"Expected has_video=False, got {result}")
            self.assertIn("skipped", result,
                          "Expected a 'skipped' key in result")

            # Nothing must be inserted in the DB
            conn = sqlite3.connect(db)
            kf_count = conn.execute(
                "SELECT COUNT(*) FROM video_keyframes WHERE transcription_id = ?",
                (tx_id,),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(kf_count, 0,
                             "No keyframes should be inserted when no video")


class TestEmptyOcrSkipsChunk(unittest.TestCase):
    """A frame whose OCR is empty (or < _MIN_OCR_SIGNAL chars) gets a keyframe
    row but NO chunk is inserted for it."""

    def test_empty_ocr_skips_chunk(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = str(tmp / "t.db")
            init_database(db)

            tx_id, _ = _seed_db(db, tmp, has_video=1)

            # Frame 0 → empty, frames 1 and 2 → normal OCR with signal
            ocr_results = ["", _FAKE_OCR[1], _FAKE_OCR[2]]

            with _AllPatched(tmp, ocr_results=ocr_results):
                result = analyze_video(db, tx_id, force=True)

            conn = sqlite3.connect(db)
            kf_count = conn.execute(
                "SELECT COUNT(*) FROM video_keyframes WHERE transcription_id = ?",
                (tx_id,),
            ).fetchone()[0]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM chunks "
                "WHERE transcription_id = ? AND speaker = 'екран'",
                (tx_id,),
            ).fetchone()[0]
            conn.close()

            # All 3 frames → 3 keyframe rows (even the empty-OCR one)
            self.assertEqual(kf_count, _N_FRAMES,
                             f"Expected {_N_FRAMES} keyframe rows (incl empty-OCR), "
                             f"got {kf_count}")
            # Only 2 frames had signal → 2 chunks
            self.assertEqual(chunk_count, _N_FRAMES - 1,
                             f"Expected {_N_FRAMES - 1} chunks (empty-OCR skipped), "
                             f"got {chunk_count}")
            # The returned chunk count reflects only signal frames
            self.assertEqual(result.get("chunks_added", 0), _N_FRAMES - 1,
                             f"Expected chunks_added={_N_FRAMES - 1}, got {result}")


# ===========================================================================
# __main__ runner (matches project convention: python test_*.py)
# ===========================================================================

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestResolveFindsVideo,
        TestAnalyzeInsertsKeyframesAndChunks,
        TestIdempotentForceRerun,
        TestNoVideoIsNoop,
        TestEmptyOcrSkipsChunk,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

    total = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    print()
    print(f"{'PASS' if result.wasSuccessful() else 'FAIL'} — {passed}/{total} tests passed")
    sys.exit(0 if result.wasSuccessful() else 1)
