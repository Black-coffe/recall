"""Tests для polish діаризованих транскриптів (Phase 10.7).

Покриваємо тільки pure-Python helper _format_diarized_input — справжній
Claude API call мокати не варто, перевіряється end-to-end вручну.
"""
from __future__ import annotations

import pytest

from app.services.text_polishing import _format_diarized_input


# ---------------------------------------------------------------- format helper

class TestFormatDiarizedInput:
    def test_empty_segments_returns_empty_string(self):
        assert _format_diarized_input([], {}) == ""

    def test_single_segment_with_named_speaker(self):
        segs = [{'start': 0, 'end': 2, 'text': 'Привіт', 'speaker': 'SPEAKER_00'}]
        speaker_map = {'SPEAKER_00': 'Андрій'}
        assert _format_diarized_input(segs, speaker_map) == 'Андрій: Привіт'

    def test_consecutive_same_speaker_merged_into_one_line(self):
        segs = [
            {'start': 0, 'end': 2, 'text': 'Привіт.', 'speaker': 'SPEAKER_00'},
            {'start': 2, 'end': 4, 'text': 'Як справи?', 'speaker': 'SPEAKER_00'},
            {'start': 4, 'end': 6, 'text': 'Все добре.', 'speaker': 'SPEAKER_00'},
        ]
        result = _format_diarized_input(segs, {'SPEAKER_00': 'Андрій'})
        assert result == 'Андрій: Привіт. Як справи? Все добре.'

    def test_speaker_changes_create_new_lines(self):
        segs = [
            {'start': 0, 'end': 2, 'text': 'Привіт.', 'speaker': 'SPEAKER_00'},
            {'start': 2, 'end': 4, 'text': 'Дякую.', 'speaker': 'SPEAKER_01'},
            {'start': 4, 'end': 6, 'text': 'Будь ласка.', 'speaker': 'SPEAKER_00'},
        ]
        result = _format_diarized_input(segs, {
            'SPEAKER_00': 'Андрій',
            'SPEAKER_01': 'Юля',
        })
        assert result == 'Андрій: Привіт.\n\nЮля: Дякую.\n\nАндрій: Будь ласка.'

    def test_self_label_falls_back_to_Vy(self):
        segs = [{'start': 0, 'end': 2, 'text': 'Привіт.', 'speaker': 'self'}]
        assert _format_diarized_input(segs, {}) == 'Ви: Привіт.'

    def test_self_with_named_speaker_uses_name(self):
        segs = [{'start': 0, 'end': 2, 'text': 'Привіт.', 'speaker': 'self'}]
        # Якщо self явно мапиться на ім'я (не на 'Ви') — використовуємо
        assert _format_diarized_input(segs, {'self': 'Андрій'}) == 'Андрій: Привіт.'

    def test_unknown_speaker_falls_back_to_question_mark(self):
        segs = [{'start': 0, 'end': 2, 'text': 'Hi', 'speaker': 'SPEAKER_UNKNOWN'}]
        assert _format_diarized_input(segs, {}) == '?: Hi'

    def test_speaker_nn_falls_back_to_Spiker_n_plus_1(self):
        segs = [
            {'start': 0, 'end': 2, 'text': 'A', 'speaker': 'SPEAKER_00'},
            {'start': 2, 'end': 4, 'text': 'B', 'speaker': 'SPEAKER_01'},
            {'start': 4, 'end': 6, 'text': 'C', 'speaker': 'SPEAKER_05'},
        ]
        result = _format_diarized_input(segs, {})
        assert 'Спікер 1: A' in result
        assert 'Спікер 2: B' in result
        assert 'Спікер 6: C' in result

    def test_empty_text_segments_skipped(self):
        segs = [
            {'start': 0, 'end': 1, 'text': '', 'speaker': 'SPEAKER_00'},
            {'start': 1, 'end': 2, 'text': '   ', 'speaker': 'SPEAKER_00'},
            {'start': 2, 'end': 4, 'text': 'Hi', 'speaker': 'SPEAKER_00'},
        ]
        result = _format_diarized_input(segs, {'SPEAKER_00': 'X'})
        assert result == 'X: Hi'

    def test_segments_without_speaker_field_get_question_mark(self):
        segs = [
            {'start': 0, 'end': 2, 'text': 'A'},  # no speaker
            {'start': 2, 'end': 4, 'text': 'B', 'speaker': 'SPEAKER_00'},
        ]
        result = _format_diarized_input(segs, {'SPEAKER_00': 'Юля'})
        # First segment treated as unknown speaker '?'
        assert '?: A' in result
        assert 'Юля: B' in result

    def test_partial_speaker_map_uses_fallback_for_missing(self):
        segs = [
            {'start': 0, 'end': 2, 'text': 'A', 'speaker': 'SPEAKER_00'},  # named
            {'start': 2, 'end': 4, 'text': 'B', 'speaker': 'SPEAKER_01'},  # unnamed
        ]
        result = _format_diarized_input(segs, {'SPEAKER_00': 'Андрій'})
        assert 'Андрій: A' in result
        assert 'Спікер 2: B' in result  # fallback for SPEAKER_01

    def test_strips_segment_text_whitespace(self):
        segs = [{'start': 0, 'end': 2, 'text': '  Привіт  ', 'speaker': 'SPEAKER_00'}]
        # Result is "X: Привіт" (no leading/trailing spaces around 'Привіт')
        assert _format_diarized_input(segs, {'SPEAKER_00': 'X'}) == 'X: Привіт'

    def test_format_is_deterministic_for_caching(self):
        """Same input → same output bytes (важливо для prompt caching)."""
        segs = [
            {'start': 0, 'end': 2, 'text': 'A', 'speaker': 'SPEAKER_00'},
            {'start': 2, 'end': 4, 'text': 'B', 'speaker': 'SPEAKER_01'},
        ]
        speaker_map = {'SPEAKER_00': 'X', 'SPEAKER_01': 'Y'}
        a = _format_diarized_input(segs, speaker_map)
        b = _format_diarized_input(segs, speaker_map)
        assert a == b
