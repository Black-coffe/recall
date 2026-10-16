"""Tests for the merge logic of diarization_service (Phase 10.2).

The real pipeline (pyannote) is not loaded in the tests - it is GPU-dependent
and slow. We test the pure function ``assign_speakers_to_whisper_segments``,
which holds all the non-ML logic (overlap math, priority, fallback).
"""
from __future__ import annotations

from app.services.diarization_service import (
    SELF_LABEL,
    UNKNOWN_LABEL,
    DiarSegment,
    assign_speakers_to_whisper_segments,
    collect_unique_labels,
)


def _ws(start: float, end: float, text: str = '') -> dict:
    return {'start': start, 'end': end, 'text': text}


def test_single_speaker_assigned_when_full_overlap():
    diar = [DiarSegment(0.0, 10.0, 'SPEAKER_00')]
    whisper = [_ws(1.0, 5.0, 'hi'), _ws(6.0, 9.0, 'bye')]
    out = assign_speakers_to_whisper_segments(whisper, diar)
    assert [s['speaker'] for s in out] == ['SPEAKER_00', 'SPEAKER_00']


def test_dominant_overlap_wins():
    """Whisper 0-10, diar A:0-3, B:3-10 → B (larger overlap 7s vs 3s)."""
    diar = [
        DiarSegment(0.0, 3.0, 'SPEAKER_00'),
        DiarSegment(3.0, 10.0, 'SPEAKER_01'),
    ]
    whisper = [_ws(0.0, 10.0)]
    out = assign_speakers_to_whisper_segments(whisper, diar)
    assert out[0]['speaker'] == 'SPEAKER_01'


def test_no_overlap_marked_unknown():
    diar = [DiarSegment(0.0, 5.0, 'SPEAKER_00')]
    whisper = [_ws(20.0, 25.0)]  # entirely outside any diar segment
    out = assign_speakers_to_whisper_segments(whisper, diar)
    assert out[0]['speaker'] == UNKNOWN_LABEL


def test_self_priority_breaks_ties():
    """50/50 overlap between self and remote → self wins (the mic owner)."""
    diar = [
        DiarSegment(0.0, 5.0, SELF_LABEL),
        DiarSegment(0.0, 5.0, 'SPEAKER_00'),
    ]
    whisper = [_ws(0.0, 5.0)]
    out = assign_speakers_to_whisper_segments(whisper, diar)
    assert out[0]['speaker'] == SELF_LABEL


def test_self_loses_when_remote_has_more_overlap():
    """Self 1s, remote 4s → remote wins despite the priority."""
    diar = [
        DiarSegment(0.0, 1.0, SELF_LABEL),
        DiarSegment(1.0, 5.0, 'SPEAKER_00'),
    ]
    whisper = [_ws(0.0, 5.0)]
    out = assign_speakers_to_whisper_segments(whisper, diar)
    assert out[0]['speaker'] == 'SPEAKER_00'


def test_multiple_segments_per_speaker_aggregated():
    """If one speaker appears in several intervals,
    the overlaps are summed - a continuous one is counted together with fragmented ones."""
    diar = [
        DiarSegment(0.0, 2.0, 'SPEAKER_00'),
        DiarSegment(3.0, 5.0, 'SPEAKER_00'),  # 4s in total for SPEAKER_00
        DiarSegment(2.0, 3.0, 'SPEAKER_01'),  # 1s SPEAKER_01
    ]
    whisper = [_ws(0.0, 5.0)]
    out = assign_speakers_to_whisper_segments(whisper, diar)
    assert out[0]['speaker'] == 'SPEAKER_00'


def test_empty_diar_all_unknown():
    whisper = [_ws(0.0, 5.0), _ws(5.0, 10.0)]
    out = assign_speakers_to_whisper_segments(whisper, [])
    assert all(s['speaker'] == UNKNOWN_LABEL for s in out)


def test_empty_whisper_returns_empty():
    diar = [DiarSegment(0.0, 10.0, 'SPEAKER_00')]
    out = assign_speakers_to_whisper_segments([], diar)
    assert out == []


def test_input_segments_not_mutated():
    """assign_* must not mutate the original whisper segments."""
    diar = [DiarSegment(0.0, 5.0, 'SPEAKER_00')]
    whisper = [_ws(1.0, 4.0, 'привіт')]
    out = assign_speakers_to_whisper_segments(whisper, diar)
    assert 'speaker' in out[0]
    assert 'speaker' not in whisper[0]  # input untouched


def test_collect_unique_labels_preserves_order():
    enriched = [
        {'start': 0, 'end': 1, 'speaker': 'SPEAKER_01'},
        {'start': 1, 'end': 2, 'speaker': SELF_LABEL},
        {'start': 2, 'end': 3, 'speaker': 'SPEAKER_01'},  # the duplicate is ignored
        {'start': 3, 'end': 4, 'speaker': 'SPEAKER_00'},
    ]
    assert collect_unique_labels(enriched) == ['SPEAKER_01', SELF_LABEL, 'SPEAKER_00']


def test_collect_unique_labels_skips_none():
    enriched = [
        {'start': 0, 'end': 1, 'speaker': None},
        {'start': 1, 'end': 2},  # no speaker key
        {'start': 2, 'end': 3, 'speaker': 'SPEAKER_00'},
    ]
    assert collect_unique_labels(enriched) == ['SPEAKER_00']


def test_partial_overlap_at_boundaries():
    """Whisper 4-7, diar A:0-5, B:5-10 → A overlap 1s, B overlap 2s → B."""
    diar = [
        DiarSegment(0.0, 5.0, 'SPEAKER_00'),
        DiarSegment(5.0, 10.0, 'SPEAKER_01'),
    ]
    whisper = [_ws(4.0, 7.0)]
    out = assign_speakers_to_whisper_segments(whisper, diar)
    assert out[0]['speaker'] == 'SPEAKER_01'


def test_zero_duration_whisper_segment_handled():
    """A zero-duration whisper must not crash (start == end)."""
    diar = [DiarSegment(0.0, 5.0, 'SPEAKER_00')]
    whisper = [_ws(2.0, 2.0)]
    out = assign_speakers_to_whisper_segments(whisper, diar)
    # A point intersection = 0, hence UNKNOWN
    assert out[0]['speaker'] == UNKNOWN_LABEL
