"""Візуальна стрічка, ендпоінти кадрів і MCP-проксі (spec video-visual-timeline, історія 03)."""
import json
import shutil
import sqlite3

import pytest
from flask import Flask
from PIL import Image
from io import BytesIO

from app.services import video_timeline as vt

FFMPEG = shutil.which('ffmpeg')
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason='ffmpeg не в PATH')

SECRET = 'Qw3rty!2026'


@pytest.fixture
def analyzed(tmp_path, monkeypatch):
    """Синтетичний запис 0–8 червоний, 8–16 синій, 16–24 зелений; OCR — фейк із секретом
    на синьому кадрі; транскрипт — три фрази."""
    from tests.test_video_frames import _seed_recording
    from app.services import document_parser, embeddings, video_analysis, video_frames
    monkeypatch.setattr(video_frames, '_ffmpeg', lambda ff=None: FFMPEG)
    monkeypatch.setattr(document_parser, 'ocr_available', lambda: True)
    monkeypatch.setattr(embeddings, 'is_available', lambda: False)

    def _ocr(img):
        r, g, b = img.getpixel((5, 5))
        if b > 150 and r < 80:
            return (f'Форма входу\nПароль: {SECRET}',
                    [[10, 10, 80, 20, 'Форма'], [10, 60, 70, 20, 'Пароль:'],
                     [90, 60, 110, 20, SECRET]])
        if g > 100 and r < 80:
            return 'Реалізація товарів №17', [[10, 10, 150, 20, 'Реалізація']]
        return 'Таблиця бюджету Q3', [[10, 10, 150, 20, 'Таблиця']]
    monkeypatch.setattr(video_analysis, 'ocr_words', _ocr)

    db, tid = _seed_recording(tmp_path, [('red', 8), ('blue', 8), ('green', 8)])
    conn = sqlite3.connect(db)
    segs = [{'start': 1.0, 'end': 6.0, 'text': 'Дивіться, це бюджет', 'speaker': 'SPEAKER_00'},
            {'start': 9.0, 'end': 14.0, 'text': 'Ось тут вводимо пароль', 'speaker': 'SPEAKER_01'},
            {'start': 17.0, 'end': 22.0, 'text': 'А тепер реалізація', 'speaker': 'SPEAKER_00'}]
    conn.execute('UPDATE transcriptions SET segments = ? WHERE id = ?',
                 (json.dumps(segs, ensure_ascii=False), tid))
    conn.commit()
    conn.close()
    video_analysis.analyze_video(db, tid, ffmpeg=FFMPEG)
    return db, tid


@needs_ffmpeg
def test_timeline_binds_transcript_by_overlap(analyzed):
    db, tid = analyzed
    res = vt.build(db, tid)
    assert res['status'] == 'ready' and res['total'] == 3
    texts = [[s['text'] for s in it['transcript']] for it in res['items']]
    assert texts[0] == ['Дивіться, це бюджет']
    assert texts[1] == ['Ось тут вводимо пароль']
    assert SECRET not in json.dumps(res, ensure_ascii=False)
    assert '[🔒 секрет]' in res['items'][1]['ocr_text']


@needs_ffmpeg
def test_timeline_pagination_and_shift(analyzed):
    db, tid = analyzed
    page = vt.build(db, tid, limit=2)
    assert len(page['items']) == 2 and page['next_offset'] == 2
    shifted = vt.build(db, tid, shift=8.0)
    # зсув на 8 с: перший відрізок (0–8) бачить фразу з 9–14
    assert [s['text'] for s in shifted['items'][0]['transcript']] == ['Ось тут вводимо пароль']


@needs_ffmpeg
def test_markdown_export(analyzed):
    db, tid = analyzed
    md = vt.to_markdown(vt.build(db, tid))
    assert '## 00:00:00 – 00:00:08' in md and 'Говорили:' in md and 'Таблиця бюджету' in md
    assert SECRET not in md


@needs_ffmpeg
def test_frames_for_text_finds_phrase(analyzed):
    db, tid = analyzed
    res = vt.frames_for_text(db, tid, query='ВВОДИМО ПАРОЛЬ', window=1.0)
    assert len(res['matches']) == 1
    starts = [f['start'] for f in res['matches'][0]['frames']]
    assert any(7.5 <= s <= 9.5 for s in starts)


@needs_ffmpeg
def test_render_frame_redacts_secret_box(analyzed):
    db, tid = analyzed
    img, meta = vt.render_frame(db, tid, 12.0, size='thumb')
    assert meta['redacted_boxes'] == 1
    sx = img.width / 320
    assert img.getpixel((int(140 * sx), int(70 * sx))) == (0, 0, 0)     # рамка секрету
    r, g, b = img.getpixel((int(300 * sx), int(170 * sx)))
    assert b > 150                                                     # решта кадру синя


def test_build_not_analyzed_and_missing(tmp_path):
    from app.db.migrations import init_database
    db = str(tmp_path / 'r.db')
    init_database(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transcriptions (source_type, source_name) VALUES ('recording', 'Нарада')")
    conn.commit()
    conn.close()
    assert vt.build(db, 1)['status'] == 'not_analyzed'
    with pytest.raises(vt.TimelineError) as e:
        vt.build(db, 99)
    assert e.value.code == 'NOT_FOUND'


@needs_ffmpeg
def test_search_visual_cyrillic_casefold(analyzed):
    db, tid = analyzed
    res = vt.search_visual(db, 'РЕАЛІЗАЦІЯ ТОВАРІВ')
    assert res['count'] == 1 and res['results'][0]['transcription_id'] == tid
    assert vt.search_visual(db, SECRET)['count'] == 0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
def client(analyzed):
    from app.blueprints.transcription import transcription_bp
    db, tid = analyzed
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config.update(TESTING=True, DATABASE=db)
    return app.test_client(), tid


@needs_ffmpeg
def test_frame_endpoint_returns_redacted_jpeg(client):
    c, tid = client
    r = c.get(f'/api/transcription/{tid}/frame?t=12&size=thumb')
    assert r.status_code == 200 and r.mimetype == 'image/jpeg'
    assert r.headers['X-Recall-Redacted-Boxes'] == '1'
    img = Image.open(BytesIO(r.data)).convert('RGB')
    sx = img.width / 320
    assert sum(img.getpixel((int(140 * sx), int(70 * sx)))) < 40


@needs_ffmpeg
def test_frames_json_batch_and_timeline_markdown(client):
    c, tid = client
    r = c.get(f'/api/transcription/{tid}/frames?from=0&to=30&limit=2')
    body = r.get_json()
    assert r.status_code == 200 and body['count'] == 2 and body['frames'][0]['image_b64']
    md = c.get(f'/api/transcription/{tid}/visual-timeline?format=markdown').get_json()
    assert md['status'] == 'ready' and 'Говорили:' in md['markdown']
    s = c.get('/api/video/search?q=бюджету').get_json()
    assert s['count'] == 1


def test_frame_endpoint_without_video_is_no_video(tmp_path):
    from app.blueprints.transcription import transcription_bp
    from app.db.migrations import init_database
    db = str(tmp_path / 'n.db')
    init_database(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transcriptions (source_type, source_name, file_path) "
                 "VALUES ('file', 'Звук', 'x.mp3')")
    conn.commit()
    conn.close()
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config.update(TESTING=True, DATABASE=db)
    r = app.test_client().get('/api/transcription/1/frame?t=3')
    assert r.status_code == 404 and r.get_json()['error_code'] == 'NO_VIDEO'


# ---------------------------------------------------------------------------
# MCP: get_frame перетворює base64 у Image-контент
# ---------------------------------------------------------------------------

def test_mcp_get_frame_returns_image_content(monkeypatch):
    import base64
    import mcp_server as m
    from fastmcp.utilities.types import Image as McpImage
    buf = BytesIO()
    Image.new('RGB', (8, 8), 'white').save(buf, 'JPEG')
    fake = {'transcription_id': 5, 'count': 1,
            'frames': [{'t': 3.0, 'frame_text': '',
                        'image_b64': base64.b64encode(buf.getvalue()).decode()}]}
    monkeypatch.setattr(m, '_api', lambda *a, **k: json.loads(json.dumps(fake)))
    # фільтр приватного (mcp-visual-privacy) — окремо в test_mcp_visual_privacy; тут без БД
    monkeypatch.setattr(m._privacy, 'private_tids', lambda *a, **k: set())
    monkeypatch.setattr(m._privacy, 'stop_words', lambda: [])
    monkeypatch.setenv('RECALL_MCP_VISUAL', 'images')
    out = m.get_frame(5, t=3.0)
    assert isinstance(out, list) and isinstance(out[1], McpImage)
    assert 'image_b64' not in json.dumps(out[0])


def test_mcp_video_analyze_passes_only_given_params(monkeypatch):
    import mcp_server as m
    seen = {}
    monkeypatch.setattr(m._privacy, 'private_tids', lambda *a, **k: set())
    monkeypatch.setattr(m, '_api', lambda method, path, **k: seen.update(path=path, **k) or {})
    m.video_analyze(7, threshold=0.05)
    assert seen['path'] == '/api/transcription/7/analyze-video'
    assert seen['body']['threshold'] == 0.05 and 'step' not in seen['body']
    assert seen['body']['describe'] is False


# ---------------------------------------------------------------------------
# ремонт раунду 1
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_secret_appearing_mid_segment_is_redacted_on_that_frame(tmp_path, monkeypatch):
    """Рядок токена зʼявляється на 10-й с і нового відрізка не відкриває: кадр t=20
    мусить OCR-итись сам, а не брати рамки представника t≈0.7."""
    from tests.test_video_frames import _make_video
    from app.services import document_parser, embeddings, video_analysis, video_frames
    from app.db.migrations import init_database
    monkeypatch.setattr(document_parser, 'ocr_available', lambda: True)
    monkeypatch.setattr(embeddings, 'is_available', lambda: False)

    def _ocr(img):
        sx = img.width / 1280
        r, g, b = img.getpixel((int(200 * sx), int(306 * sx)))
        if r > 200 and g > 200:                     # рядок уже на екрані
            return f'Пароль: {SECRET}', [[int(100 * sx), int(300 * sx), int(60 * sx), int(14 * sx), 'Пароль:'],
                                         [int(170 * sx), int(300 * sx), int(170 * sx), int(14 * sx), SECRET]]
        return 'Порожній редактор документа', [[10, 10, 200, 20, 'Порожній']]
    monkeypatch.setattr(video_analysis, 'ocr_words', _ocr)

    sess = tmp_path / 'rec_test'
    sess.mkdir()
    video = _make_video(sess / 'video_mon0_final.mp4', [('navy', 30)], size='1280x720',
                        extra_vf="drawbox=x=100:y=300:w=240:h=14:color=white:t=fill:enable='gte(t,10)'")
    db = str(tmp_path / 'r.db')
    init_database(db)
    conn = sqlite3.connect(db)
    (sess / 'final.mp3').write_bytes(b'\0')
    conn.execute("INSERT INTO transcriptions (source_type, source_name, file_path) VALUES "
                 "('recording', 'Нарада', ?)", (str(sess / 'final.mp3'),))
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, source_type, "
                 "recording_session_id, has_video, primary_video_path) VALUES "
                 "('', 'rec_test', 'Запис', ?, 'recording', 'rec_test', 1, ?)", (str(sess / 'final.mp3'), video))
    conn.commit()
    conn.close()
    video_analysis.analyze_video(db, 1, ffmpeg=FFMPEG)
    assert vt.build(db, 1)['total'] == 1                     # рядок справді не відкрив відрізка
    img, meta = vt.render_frame(db, 1, 20.0, size='full')
    assert meta['redacted_boxes'] == 1
    sx = img.width / 1280
    assert img.getpixel((int(250 * sx), int(306 * sx))) == (0, 0, 0)


@needs_ffmpeg
def test_thumb_cache_invalidated_when_min_res_changes(tmp_path):
    from tests.test_video_frames import _make_video
    from app.services import video_frames as vf
    v = _make_video(tmp_path / 'rb.mp4', [('red', 20), ('blue', 20)])
    cache = str(tmp_path / 'cache')
    vf.select_keyframes(v, cache_dir=cache, min_res=1.0, ffmpeg=FFMPEG)
    half = vf.select_keyframes(v, cache_dir=cache, min_res=0.5, ffmpeg=FFMPEG)
    assert [round(s.start) for s in half['segments']] == [0, 20]


def test_classify_without_ocr_is_not_camera_only():
    from app.services import video_frames as vf
    seg = vf.Segment(0, 30, 0.7, 0.0, std=40.0)
    assert vf.classify(seg, ocr_chars=0, ocr_lines=0, mean_word_h=0, frame_h=1440,
                       ocr_ran=False) == 'screen_share'


@needs_ffmpeg
def test_describe_unavailable_is_not_saved_as_done(analyzed, monkeypatch):
    from app.services import video_analysis, video_vision
    db, tid = analyzed
    monkeypatch.setattr(video_vision, 'availability', lambda b=None: (False, 'нема моделі'))
    res = video_analysis.analyze_video(db, tid, describe=True, ffmpeg=FFMPEG)
    assert res['params']['describe'] is False
    calls = []
    monkeypatch.setattr(video_vision, 'availability', lambda b=None: calls.append(1) or (False, 'x'))
    again = video_analysis.analyze_video(db, tid, describe=True, ffmpeg=FFMPEG)
    assert again.get('skipped') != 'already analyzed' and calls   # спроба повторюється


def test_ui_vision_backend_without_describe_means_describe(tmp_path, monkeypatch):
    """UI-чекбокс «Claude-опис» шле лише vision_backend — це запит на опис."""
    from app.blueprints import transcription as tr
    from app.services import video_analysis
    from app.db.migrations import init_database
    db = str(tmp_path / 'u.db')
    init_database(db)
    submitted = {}

    class _Q:
        def list(self, **k):
            return []

        def submit(self, kind, fn, meta=None):
            submitted['fn'] = fn

            class J:
                id = 'job-1'
            return J()
    monkeypatch.setattr(tr.state, 'job_queue', _Q())
    monkeypatch.setattr(video_analysis, 'resolve_recording_video', lambda *a: {'primary_video_path': 'x'})
    seen = {}
    monkeypatch.setattr(video_analysis, 'analyze_video', lambda *a, **k: seen.update(k))
    app = Flask(__name__)
    app.register_blueprint(tr.transcription_bp)
    app.config.update(TESTING=True, DATABASE=db)
    r = app.test_client().post('/api/transcription/1/analyze-video', json={'vision_backend': 'claude'})
    assert r.get_json()['job_id'] == 'job-1'
    submitted['fn'](None)
    assert seen['describe'] is True and seen['vision_backend'] == 'claude'
