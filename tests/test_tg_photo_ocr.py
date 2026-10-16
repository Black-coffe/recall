"""Re-reading Telegram photos with OCR and marking a parse failure (spec tg-photo-ocr)."""
import sqlite3

import pytest
from flask import Flask

from app.db.migrations import init_database
from app.services import tg_photo_ocr as tpo

OCR = {
    'scan.jpg': 'Рахунок №17 ТОВ Ромашка\nПароль: [🔒 секрет]',   # document_parser already masks it
    'caption.jpg': 'Таблиця бюджету Q3',
    'cat.jpg': '',
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    from app.services import document_parser
    path = str(tmp_path / 'r.db')
    init_database(path)
    files = {}
    for name in OCR:
        f = tmp_path / name
        f.write_bytes(b'jpg')
        files[name] = str(f)
    conn = sqlite3.connect(path)
    for name, text, model in (('scan.jpg', '[фото без тексту]', 'parse_failed'),
                              ('caption.jpg', 'Глянь бюджет', None),
                              ('cat.jpg', '[фото без тексту]', 'parse_failed')):
        conn.execute("INSERT INTO transcriptions (source_type, source_name, doc_type, file_path, "
                     "transcript_text, tg_chat_title, tg_chat_id, model_used) VALUES "
                     "('telegram', '[TG] Чат: фото', 'photo', ?, ?, 'Фонд', 42, ?)",
                     (files[name], text, model))
    conn.execute("INSERT INTO transcriptions (source_type, source_name, doc_type, file_path, "
                 "transcript_text) VALUES ('telegram', 'зник', 'photo', ?, '[фото без тексту]')",
                 (str(tmp_path / 'нема.jpg'),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(document_parser, 'ocr_available', lambda: True)
    def _parse(p, filename=None):                 # like the real one: empty OCR → NoTextFound
        if not OCR[filename]:
            raise document_parser.NoTextFound('OCR не знайшов тексту на зображенні.')
        return {'text': OCR[filename]}
    monkeypatch.setattr(document_parser, 'parse_document', _parse)
    return path


def _texts(path):
    conn = sqlite3.connect(path)
    rows = conn.execute('SELECT transcript_text, model_used, source_name FROM transcriptions '
                        'ORDER BY id').fetchall()
    conn.close()
    return rows


def test_dry_run_counts_and_writes_nothing(db):
    before = _texts(db)
    res = tpo.run(db, dry_run=True, reindex=False)
    assert res['total'] == 4 and res['missing_file'] == 1
    assert res['with_text'] == 2 and res['no_text'] == 1 and res['updated'] == 2
    assert _texts(db) == before


def test_run_replaces_stub_keeps_caption_masks_and_is_idempotent(db, monkeypatch):
    calls = []
    monkeypatch.setattr(tpo, '_reindex', lambda p, tid: calls.append(tid) or True)
    res = tpo.run(db, dry_run=False)
    rows = _texts(db)
    assert rows[0][0].startswith('Рахунок №17 ТОВ Ромашка')
    assert '[🔒 секрет]' in rows[0][0]
    assert rows[0][1] == 'ocr-backfill' and rows[0][2].startswith('[TG] Фонд: Рахунок')
    assert rows[1][0] == 'Глянь бюджет\n\nТаблиця бюджету Q3'
    assert rows[2][0] == '[фото без тексту]' and rows[2][1] is None     # no text: the mark is cleared
    assert calls == [1, 2] and res['reindexed'] == 2
    again = tpo.run(db, dry_run=False)
    assert again['updated'] == 0 and again['already'] == 2
    assert _texts(db) == rows


def test_run_refuses_without_ocr(db, monkeypatch):
    from app.services import document_parser
    monkeypatch.setattr(document_parser, 'ocr_available', lambda: False)
    assert 'error' in tpo.run(db, dry_run=True)


def test_ingest_marks_real_failure_but_not_photo_without_text(tmp_path, monkeypatch):
    from app.blueprints import telegram as tg
    from app.services import document_parser

    def _boom(p, filename=None):
        raise RuntimeError('tesseract відсутній')
    monkeypatch.setattr(document_parser, 'parse_document', _boom)
    text, doc_type, structure, failed = tg._parse_media('x.jpg', '', 'photo')
    assert failed and text == '[фото без тексту]'

    def _empty(p, filename=None):
        raise document_parser.NoTextFound('OCR не знайшов тексту на зображенні.')
    monkeypatch.setattr(document_parser, 'parse_document', _empty)
    text, _, _, failed = tg._parse_media('cat.jpg', 'Кіт', 'photo')
    assert not failed and text == 'Кіт'


def test_ocr_masks_secrets_for_every_consumer(monkeypatch):
    import pytesseract
    from app.services import document_parser
    monkeypatch.setattr(pytesseract, 'image_to_string',
                        lambda img, lang=None: ('Логін admin\npassword: hunter2\n'
                                                'Токен ghp_1234567890abcdefABCDEF1234'))
    text = document_parser._ocr_image_obj(object())
    assert 'hunter2' not in text and 'ghp_' not in text and 'Логін admin' in text


def test_real_parser_raises_no_text_found_on_blank_image(tmp_path):
    from PIL import Image
    from app.services import document_parser
    if not document_parser.ocr_available():
        pytest.skip('Tesseract is unavailable')
    blank = tmp_path / 'blank.jpg'
    Image.new('RGB', (400, 300), (120, 160, 200)).save(blank)
    with pytest.raises(document_parser.NoTextFound):
        document_parser.parse_document(str(blank), filename='blank.jpg')


def test_extension_doc_type_is_only_remasked_never_appended(db):
    """doc_type='jpg' = OCR at ingest already succeeded; a repeated OCR (slightly different) is not appended."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET doc_type = 'jpg', transcript_text = ? WHERE id = 2",
                 ('Таблиця бюджету Q3 стара версія OCR\npassword: hunter2',))
    conn.execute("UPDATE transcriptions SET doc_type = 'png', transcript_text = 'Інша таблиця' WHERE id = 3")
    conn.commit()
    conn.close()
    res = tpo.run(db, dry_run=False, reindex=False)
    rows = _texts(db)
    assert rows[1][0] == 'Таблиця бюджету Q3 стара версія OCR\npassword: [🔒 секрет]'
    assert rows[2][0] == 'Інша таблиця'
    assert res['remasked'] == 1


def test_coverage_reports_unparsed_media(db, monkeypatch):
    from app.blueprints import telegram as tg
    monkeypatch.setattr(tg, '_listener_call', lambda *a, **k: (False, {'error': 'слухач вимкнений'}))
    monkeypatch.setattr(tg, '_authorized', lambda: True)
    app = Flask(__name__)
    app.register_blueprint(tg.telegram_bp)
    app.config.update(TESTING=True, DATABASE=db)
    body = app.test_client().get('/api/telegram/coverage').get_json()
    assert body['media_unparsed'] == 2


def test_run_remasks_text_recognized_before_masking_shipped(db, monkeypatch):
    """Between the OCR revival (4.10.0) and the mask, a photo text could land unmasked."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET transcript_text = ? WHERE id = 1",
                 ('Рахунок №17 ТОВ Ромашка\nПароль: Qw3rty!2026',))
    conn.commit()
    conn.close()
    calls = []
    monkeypatch.setattr(tpo, '_reindex', lambda p, tid: calls.append(tid) or True)
    res = tpo.run(db, dry_run=False)
    assert res.get('remasked') == 1 and 1 in calls
    assert 'Qw3rty' not in _texts(db)[0][0]



@pytest.mark.parametrize('text', [
    'password: [🔒 секрет]',
    'Вхід\nПароль: [🔒 секрет]\nТокен [🔒 секрет]',
    'Пароль: [🔒 секрет] і ще password: hunter2',
])
def test_masking_is_idempotent(text):
    """Round 3: an already present mask was caught as «label + value» and grew with every run."""
    from app.services import screen_secrets as ss
    once = ss.mask_text(text, ss.scan_text(text))
    twice = ss.mask_text(once, ss.scan_text(once))
    assert once == twice and 'секрет] секрет]' not in twice and 'hunter2' not in once


def test_masked_words_are_not_secrets():
    from app.services import screen_secrets as ss
    words = [[0, 0, 50, 20, 'Пароль:'], [60, 0, 20, 20, '[🔒'], [85, 0, 40, 20, 'секрет]']]
    assert ss.scan(words) == []


def test_two_runs_on_already_masked_extension_row_change_nothing(db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET doc_type = 'jpg', transcript_text = ? WHERE id = 2",
                 ('Вхід\npassword: [🔒 секрет]',))
    conn.commit()
    conn.close()
    first = tpo.run(db, dry_run=False, reindex=False)
    after_first = _texts(db)
    second = tpo.run(db, dry_run=False, reindex=False)
    assert _texts(db) == after_first
    assert after_first[1][0] == 'Вхід\npassword: [🔒 секрет]'
    assert first.get('remasked', 0) == 0 and second.get('remasked', 0) == 0


def test_epoch_refusal_is_not_counted_as_reindexed(db, monkeypatch):
    """13.10.2026: CLI without .env — the epoch guard refused to write chunks, and the counter said 606."""
    from app.services import embeddings, tg_entities
    monkeypatch.setattr(embeddings, 'chunk_and_embed_transcription',
                        lambda p, tid, force=False: {'status': 'epoch_mismatch', 'transcription_id': tid})
    monkeypatch.setattr(tg_entities, 'link_message', lambda p, tid: {})
    res = tpo.run(db, dry_run=False)
    assert res['updated'] == 2 and res['reindexed'] == 0 and res['reindex_failed'] == 2
