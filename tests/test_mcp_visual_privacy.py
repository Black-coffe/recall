"""Фільтр приватного на виході MCP (spec mcp-visual-privacy)."""
import base64
import sqlite3

import pytest

import mcp_server as m
from app.db.migrations import init_database

JPEG = base64.b64encode(b'\xff\xd8\xff\xe0jpeg').decode()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'r.db')
    init_database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES "
                 "(15, 'Юридичне', 'юридичне', 5)")
    # 1 — робочий запис, 2 — «Особисте», 3 — проєкт під «Юридичне»
    for tid, cat in ((1, 1), (2, 2), (3, 15)):
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (?, 'recording', 'дзвінок', ?)", (tid, cat))
    for kid, tid, text in ((11, 1, 'Бюджет Q3 таблиця'), (12, 1, 'Рахунок ПЕТРЕНКО О.'),
                           (13, 2, 'Особисте фото'), (14, 3, 'Позов')):
        conn.execute("INSERT INTO video_keyframes (id, transcription_id, ts_offset_sec, ocr_text) "
                     "VALUES (?, ?, 0, ?)", (kid, tid, text))
    conn.commit()
    conn.close()
    # основа, а не слово: морфології нема, «Петренк» ловить і Петренко, і Петренку
    (tmp_path / 'private.txt').write_text('Петренк\n', encoding='utf-8')
    monkeypatch.setattr(m, 'DB_PATH', path)
    monkeypatch.setenv('RECALL_PRIVATE_WORDS_FILE', str(tmp_path / 'private.txt'))
    monkeypatch.delenv('RECALL_PRIVATE_CATEGORIES', raising=False)
    monkeypatch.setenv('RECALL_MCP_VISUAL', 'images')
    return path


@pytest.fixture
def api(monkeypatch):
    calls = []
    responses = {}

    def fake(method, path, **kw):
        calls.append(path)
        for key, val in responses.items():
            if key in path:
                return val
        return {'error': 'unexpected'}
    monkeypatch.setattr(m, '_api', fake)
    fake.calls, fake.responses = calls, responses
    return fake


def _seg(text, desc=None):
    return {'segment_id': 'x', 'start': 0, 'end': 5, 'frame_time': 1, 'frame_type': 'slide',
            'frame_url': '/api/transcription/1/frame?t=1.00',
            'ocr_text': text, 'description': desc, 'transcript': []}


@pytest.mark.parametrize('tid', [2, 3])
def test_private_record_refused_everywhere_without_api(db, api, tid):
    for call in (lambda: m.video_analyze(tid), lambda: m.get_visual_timeline(tid),
                 lambda: m.get_frame(tid, t=1), lambda: m.get_frames_for_text(tid, query='так')):
        assert call()['error_code'] == 'PRIVATE'
    assert api.calls == []


def test_privacy_check_failure_refuses(db, api, monkeypatch):
    monkeypatch.setattr(m, 'DB_PATH', db + '.нема/x.db')
    assert m.get_visual_timeline(1)['error_code'] == 'PRIVACY_CHECK_FAILED'
    assert api.calls == []


def test_timeline_drops_stop_word_segments_and_renders_markdown(db, api):
    api.responses['visual-timeline'] = {
        'transcription_id': 1, 'status': 'ready', 'title': 'дзвінок', 'total': 3,
        'items': [_seg('Бюджет Q3'), _seg('Рахунок петренко'), _seg('', 'Слайд про Петренка')]}
    res = m.get_visual_timeline(1, format='markdown')
    assert [i['ocr_text'] for i in res['items']] == ['Бюджет Q3']
    assert res['private_hidden'] == 2
    assert 'Бюджет Q3' in res['markdown'] and 'етренк' not in res['markdown']


def test_frames_for_text_filters_frames(db, api):
    api.responses['frames-for-text'] = {'transcription_id': 1, 'analyzed': True, 'matches': [
        {'segment_index': 0, 'text': 'ось тут', 'frames': [_seg('Бюджет'), _seg('ПЕТРЕНКО')]}]}
    res = m.get_frames_for_text(1, query='ось')
    assert len(res['matches'][0]['frames']) == 1 and res['private_hidden'] == 1


def test_get_frame_checks_this_frames_text_and_unverifiable_frame(db, api):
    api.responses['/frames'] = {'transcription_id': 1, 'count': 3, 'frames': [
        {'t': 1, 'ocr_text': 'Бюджет', 'frame_text': 'Бюджет', 'image_b64': JPEG},
        # представник відрізка чистий, а САМ кадр має стоп-слово
        {'t': 2, 'ocr_text': 'Бюджет', 'frame_text': 'лист Петренку', 'image_b64': JPEG},
        # OCR недоступний — перевірити нічим
        {'t': 3, 'ocr_text': 'Бюджет', 'frame_text': None, 'image_b64': JPEG},
    ]}
    out = m.get_frame(1, from_sec=0, to_sec=10)
    head, images = out[0], out[1:]
    assert head['count'] == 1 and head['private_hidden'] == 2 and len(images) == 1
    assert 'frame_text' not in head['frames'][0] and 'image_b64' not in head['frames'][0]


def test_search_visual_checks_full_frame_text_and_private(db, api):
    api.responses['/api/video/search'] = {'query': 'рахунок', 'count': 4, 'results': [
        {'transcription_id': 1, 'keyframe_id': 11, 'snippet': 'Бюджет'},
        {'transcription_id': 1, 'keyframe_id': 12, 'snippet': 'Рахунок'},   # повний текст — стоп
        {'transcription_id': 2, 'keyframe_id': 13, 'snippet': 'фото'},
        {'transcription_id': 3, 'keyframe_id': 14, 'snippet': 'Позов'},
    ]}
    res = m.search_visual('рахунок')
    assert [h['keyframe_id'] for h in res['results']] == [11]
    assert res['count'] == 1 and res['private_hidden'] == 3


def test_unreadable_stop_list_hides_text(db, api, tmp_path):
    (tmp_path / 'private.txt').write_bytes(b'\xff\xfe\xfa')
    api.responses['visual-timeline'] = {'transcription_id': 1, 'status': 'ready',
                                        'items': [_seg('Бюджет')]}
    res = m.get_visual_timeline(1)
    assert res['items'] == [] and res['private_hidden'] == 1


def test_mode_text_refuses_frames_only(db, api, monkeypatch):
    monkeypatch.setenv('RECALL_MCP_VISUAL', 'text')
    assert m.get_frame(1, t=1)['error_code'] == 'MCP_VISUAL_TEXT_ONLY'
    api.responses['visual-timeline'] = {'transcription_id': 1, 'status': 'ready',
                                        'items': [_seg('Бюджет')]}
    assert len(m.get_visual_timeline(1)['items']) == 1


@pytest.mark.parametrize('mode', ['off', 'кадри'])
def test_mode_off_or_unknown_refuses_all_five(db, api, monkeypatch, mode):
    monkeypatch.setenv('RECALL_MCP_VISUAL', mode)
    for call in (lambda: m.video_analyze(1), lambda: m.get_visual_timeline(1),
                 lambda: m.get_frame(1, t=1), lambda: m.get_frames_for_text(1, query='x'),
                 lambda: m.search_visual('x')):
        assert call()['error_code'] == 'MCP_VISUAL_OFF'
    assert api.calls == []


def test_render_frame_reports_masked_text_of_this_frame(monkeypatch):
    """frame_text — текст самого кадру з живого OCR, секрети замасковані."""
    from app.services import video_timeline as vt
    from app.services import document_parser as dp, video_analysis as va, video_frames as vf
    from PIL import Image
    monkeypatch.setattr(dp, 'ocr_available', lambda: True)
    monkeypatch.setattr(vf, 'grab_frame', lambda *a, **k: Image.new('RGB', (100, 50)))
    monkeypatch.setattr(va, 'ocr_words', lambda img: (
        'Лист Петренку\npassword: hunter2', [[0, 0, 10, 10, 'Лист'], [12, 0, 10, 10, 'Петренку'],
                                              [0, 20, 10, 10, 'password:'], [12, 20, 10, 10, 'hunter2']]))
    boxes, w, h, text = vt._live_secret_boxes('v.mp4', 1.0, None)
    assert 'Петренку' in text and 'hunter2' not in text and boxes


# ---------------------------------------------------------------------------
# Історія 02: текст з екрана в загальному пошуку (search_archive / grep_archive)
# ---------------------------------------------------------------------------

def _screen(tid, text, idx=9):
    return {'transcription_id': tid, 'chunk_index': idx, 'speaker': 'екран', 'text': text}


def test_search_archive_hides_private_and_stop_word_screen_chunks(db, api):
    api.responses['/api/memory/search'] = {'query': 'бюджет', 'chunks': [
        {'transcription_id': 1, 'chunk_index': 0, 'speaker': 'Олег', 'text': 'бюджет і Петренко'},
        _screen(1, 'Бюджет Q3'), _screen(1, 'Рахунок Петренку'), _screen(2, 'Бюджет сімʼї')]}
    res = m.search_archive('бюджет')
    texts = [c['text'] for c in res['chunks']]
    # звичайна репліка не фільтрується, навіть зі стоп-словом: фільтр лише про візуальне
    assert texts == ['бюджет і Петренко', 'Бюджет Q3']
    assert res['count'] == 2 and res['private_hidden'] == 2


def test_search_archive_mode_off_hides_all_screen(db, api, monkeypatch):
    monkeypatch.setenv('RECALL_MCP_VISUAL', 'off')
    api.responses['/api/memory/search'] = {'chunks': [
        {'transcription_id': 1, 'chunk_index': 0, 'speaker': 'Олег', 'text': 'бюджет'},
        _screen(1, 'Бюджет Q3')]}
    assert [c['speaker'] for c in m.search_archive('бюджет')['chunks']] == ['Олег']


def _grep_db(db):
    conn = sqlite3.connect(db)
    rows = [(1, 0, 'Олег', 'обговорили бюджет'), (1, 1, 'екран', 'Рахунок Петренку бюджет'),
            (1, 2, 'екран', 'Бюджет Q3 таблиця'), (2, 0, 'екран', 'бюджет сімʼї'),
            (2, 1, 'Олена', 'бюджет на відпустку')]
    conn.executemany("INSERT INTO chunks (transcription_id, chunk_index, speaker, text) "
                     "VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_grep_archive_hides_screen_matches_and_context(db):
    _grep_db(db)
    res = m.grep_archive('бюджет', context=1)
    got = {(x['transcription_id'], x['chunk_index']) for x in res['matches']}
    assert got == {(1, 0), (1, 2), (2, 1)}
    assert res['private_hidden'] == 2 and res['count'] == 3
    by = {(x['transcription_id'], x['chunk_index']): x for x in res['matches']}
    # сусід-екран зі стоп-словом прибраний із контексту; чистий екранний лишився
    assert [c['chunk_index'] for c in by[(1, 0)]['context_after']] == []
    assert [c['chunk_index'] for c in by[(1, 2)]['context_before']] == []
    assert [c['chunk_index'] for c in by[(2, 1)]['context_before']] == []


def test_grep_archive_check_failure_hides_screen_and_context(db, monkeypatch):
    _grep_db(db)

    def boom(*a, **k):
        raise m._privacy.PrivacyError('БД')
    monkeypatch.setattr(m._privacy, 'private_tids', boom)
    res = m.grep_archive('бюджет', context=1)
    assert all(x['speaker'] != 'екран' for x in res['matches'])
    assert all(x['context_before'] == [] and x['context_after'] == [] for x in res['matches'])
