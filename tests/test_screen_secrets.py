"""Секрети з екрана (spec video-visual-timeline, історія 02)."""
import io
import shutil
import sqlite3

import pytest
from PIL import Image

from app.services import screen_secrets as ss


def _words(*tokens, y=10):
    """Слова одного рядка з рамками [x, y, w, h, текст]."""
    out, x = [], 0
    for t in tokens:
        out.append([x, y, 10 * len(t), 20, t])
        x += 10 * len(t) + 8
    return out


@pytest.mark.parametrize('tokens, secret', [
    (('Пароль:', 'Qw3rty!2026'), 'Qw3rty!2026'),
    (('password', '=', 'hunter2'), 'hunter2'),
    (('password=hunter2',), 'hunter2'),
    (('ПАРОЛЬ', 'Сонце2026'), 'Сонце2026'),
    (('ключ:', 'abc123XYZ'), 'abc123XYZ'),
    (('sk-ant-api03-AbCdEf123456789_xyz',), 'sk-ant-api03-AbCdEf123456789_xyz'),
    (('ghp_1234567890abcdefABCDEF1234',), 'ghp_1234567890abcdefABCDEF1234'),
    (('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fw',),
     'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fw'),
    (('7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1',), '7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1'),
    (('postgres://admin:Pa55w0rd@db.local/recall',), 'postgres://admin:Pa55w0rd@db.local/recall'),
    (('AKIAIOSFODNN7EXAMPLE',), 'AKIAIOSFODNN7EXAMPLE'),
])
def test_secrets_detected(tokens, secret):
    found = ss.scan(_words(*tokens))
    assert secret in {s.value for s in found}


@pytest.mark.parametrize('tokens', [
    ('Реалізація', 'товарів', '№', '00012345', 'від', '13.10.2026'),
    ('Сума:', '12', '000', 'грн'),
    ('C:\\src\\ESSCoreData\\_tools\\Export-CoreData.ps1',),
    ('https://bauf.bitrix24.com/extranet/contacts/personal/user/1198/calendar/',),
    ('Задача', 'T-2041', 'відкрита', 'Контрагент', 'ТОВ', 'Ромашка'),
    ('Проектування_інтерфейсу_Recall',),
])
def test_ordinary_screen_text_not_flagged(tokens):
    assert ss.scan(_words(*tokens)) == []


def test_label_value_must_be_on_same_line():
    words = _words('Пароль:') + _words('Наступний', y=200)
    assert ss.scan(words) == []


def test_mask_text_and_words():
    words = _words('Пароль:', 'Qw3rty!2026')
    found = ss.scan(words)
    assert ss.mask_text('Логін admin Пароль: Qw3rty!2026', found) == 'Логін admin Пароль: ' + ss.MASK
    masked = ss.mask_words(words, found)
    assert masked[1][4] == ss.BOX_MASK and masked[1][5] == 1
    assert 'Qw3rty' not in str(masked)


def test_scan_text_in_description():
    found = ss.scan_text('На екрані форма входу, password: hunter2, і токен ghp_1234567890abcdefABCDEF1234')
    assert {'hunter2', 'ghp_1234567890abcdefABCDEF1234'} <= {s.value for s in found}


def test_redact_blackens_box():
    img = Image.new('RGB', (200, 100), 'white')
    out = ss.redact(img, [[50, 20, 40, 20]], 200, 100)
    assert out.getpixel((70, 30)) == (0, 0, 0)
    assert out.getpixel((5, 5)) == (255, 255, 255)
    assert img.getpixel((70, 30)) == (255, 255, 255)        # оригінал не чіпається


class _Tty(io.StringIO):
    def __init__(self, value='', tty=True):
        super().__init__(value)
        self._tty = tty

    def isatty(self):
        return self._tty


def _db_with_secret(tmp_path):
    from app.db.migrations import init_database
    db = str(tmp_path / 's.db')
    init_database(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transcriptions (source_type, source_name) VALUES ('recording', 'Дзвінок')")
    conn.execute("INSERT INTO video_keyframes (transcription_id, ts_offset_sec) VALUES (1, 65.0)")
    conn.execute("INSERT INTO screen_secrets (keyframe_id, kind, value) VALUES (1, 'labeled', 'Qw3rty!2026')")
    conn.commit()
    conn.close()
    return db


def test_reveal_refuses_without_tty(tmp_path):
    db = _db_with_secret(tmp_path)
    out, err = _Tty(tty=False), _Tty(tty=False)
    rc = ss.reveal(db, 1, str(tmp_path / 'o.txt'), stdin=_Tty('показати 1\n', tty=False),
                   stdout=out, stderr=err)
    assert rc != 0
    assert 'Qw3rty' not in out.getvalue() + err.getvalue()
    assert not (tmp_path / 'o.txt').exists()


def test_reveal_with_tty_writes_file_not_stdout(tmp_path):
    db = _db_with_secret(tmp_path)
    out, err = _Tty(), _Tty()
    target = tmp_path / 'secrets.txt'
    rc = ss.reveal(db, 1, str(target), stdin=_Tty('показати 1\n'), stdout=out, stderr=err)
    assert rc == 0
    assert 'Qw3rty!2026' in target.read_text(encoding='utf-8')
    assert 'Qw3rty' not in out.getvalue() + err.getvalue()
    assert str(target) in out.getvalue()


def test_reveal_wrong_confirmation_writes_nothing(tmp_path):
    db = _db_with_secret(tmp_path)
    target = tmp_path / 'x.txt'
    rc = ss.reveal(db, 1, str(target), stdin=_Tty('так\n'), stdout=_Tty(), stderr=_Tty())
    assert rc != 0 and not target.exists()


# ---------------------------------------------------------------------------
# конвеєр: жодного сирого значення поза screen_secrets
# ---------------------------------------------------------------------------

FFMPEG = shutil.which('ffmpeg')


@pytest.mark.skipif(FFMPEG is None, reason='ffmpeg не в PATH')
def test_pipeline_masks_everywhere_and_redacts_described_frame(tmp_path, monkeypatch):
    from tests.test_video_frames import _seed_recording
    from app.services import document_parser, embeddings, video_analysis, video_vision
    secret = 'Qw3rty!2026'
    words = [[100, 100, 120, 30, 'Пароль:'], [240, 100, 160, 30, secret],
             [100, 300, 300, 30, 'Контрагент']]
    monkeypatch.setattr(document_parser, 'ocr_available', lambda: True)
    monkeypatch.setattr(video_analysis, 'ocr_words',
                        lambda img: (f'Пароль: {secret}\nКонтрагент', [list(w) for w in words]))
    monkeypatch.setattr(embeddings, 'is_available', lambda: False)
    monkeypatch.setattr(video_vision, 'availability', lambda b=None: (True, 'fake'))
    monkeypatch.setattr(video_vision, 'active_model', lambda b=None: 'fake-vl')
    seen = []

    def _describe(path, backend=None, **_):
        seen.append(Image.open(path).convert('RGB').copy())
        return f'Форма входу, видно пароль {secret}'
    monkeypatch.setattr(video_vision, 'describe_frame', _describe)

    db, tid = _seed_recording(tmp_path, [('white', 6)])
    res = video_analysis.analyze_video(db, tid, describe=True, vision_backend='claude',
                                       ffmpeg=FFMPEG)
    assert res['secrets_masked'] >= 1
    conn = sqlite3.connect(db)
    for table, col in (('video_keyframes', 'ocr_text'), ('video_keyframes', 'vision_text'),
                       ('video_keyframes', 'ocr_boxes_json'), ('chunks', 'text')):
        for (val,) in conn.execute(f'SELECT {col} FROM {table}'):
            assert secret not in (val or ''), (table, col)
    assert conn.execute('SELECT value FROM screen_secrets').fetchone()[0] == secret
    conn.close()
    # кадр для опису: рамка секрету чорна (відео 320×180, OCR-кадр теж 320×180)
    assert seen, 'опис не викликався'
    img = seen[0]
    sx = img.width / 320
    assert img.getpixel((int(300 * sx), int(110 * sx))) == (0, 0, 0)


def test_symbol_only_password_is_still_masked():
    """Ідемпотентність маски не мусить вимикати маскування паролів без літер і цифр."""
    found = ss.scan_text('password: !@#$%^&*')
    assert '!@#$%^&*' in {s.value for s in found}
    assert ss.scan(_words('Пароль:', '!@#$%^&*')) != []
