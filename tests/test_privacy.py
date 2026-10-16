"""Private core (spec mcp-visual-privacy): stop-list directions, stop words, mode."""
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import privacy


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'r.db')
    init_database(path)
    conn = sqlite3.connect(path)
    # «Юридичне» is a project under «Фріланс» (as in the live DB), with one more subproject under it
    conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES "
                 "(15, 'Юридичне', 'юридичне', 5)")
    conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES "
                 "(16, 'Суди', 'суди', 15)")
    conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES "
                 "(17, 'Сімʼя', 'сімʼя', 2)")
    for tid, cat in ((1, 1), (2, 2), (3, 15), (4, 16), (5, 5), (6, None), (7, 17)):
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (?, 'file', 'запис', ?)", (tid, cat))
    conn.commit()
    conn.close()
    monkeypatch.delenv('RECALL_PRIVATE_CATEGORIES', raising=False)
    monkeypatch.setenv('RECALL_PRIVATE_WORDS_FILE', str(tmp_path / 'private.txt'))
    return path


def test_private_directions_with_descendants(db):
    assert privacy.private_tids(db, range(1, 8)) == {2, 3, 4, 7}


def test_custom_list_and_case(db, monkeypatch):
    monkeypatch.setenv('RECALL_PRIVATE_CATEGORIES', ' фонд робота ')
    assert privacy.private_tids(db, range(1, 8)) == {1}


def test_db_failure_is_privacy_error(tmp_path):
    with pytest.raises(privacy.PrivacyError):
        privacy.private_tids(str(tmp_path / 'нема' / 'r.db'), [1])


def test_stop_words_cyrillic_any_case(db, tmp_path):
    (tmp_path / 'private.txt').write_text('# коментар\nПетренко\n\nдоговір ОРЕНДИ\n', encoding='utf-8')
    words = privacy.stop_words()
    assert words == ['петренко', 'договір оренди']
    assert privacy.has_stop_word('Рахунок для ПЕТРЕНКО О.', words)
    assert privacy.has_stop_word('Договір оренди №5', words)
    assert not privacy.has_stop_word('Бюджет Q3', words)


def test_no_file_means_no_words(db):
    assert privacy.stop_words() == []
    assert not privacy.has_stop_word('будь-що')


def test_unreadable_file_is_privacy_error(db, tmp_path):
    (tmp_path / 'private.txt').write_bytes(b'\xff\xfe\xfa bad')
    with pytest.raises(privacy.PrivacyError):
        privacy.stop_words()


@pytest.mark.parametrize('raw,mode', [('', 'off'), ('images', 'images'), (' TEXT ', 'text'),
                                      ('off', 'off'), ('кадри', 'off')])
def test_visual_mode_unknown_is_off(monkeypatch, raw, mode):
    monkeypatch.setenv('RECALL_MCP_VISUAL', raw)
    assert privacy.visual_mode() == mode


def test_visual_mode_default_is_images(monkeypatch):
    monkeypatch.delenv('RECALL_MCP_VISUAL', raising=False)
    assert privacy.visual_mode() == 'images'
