"""Tests для API спікерів (Phase 10.4).

Використовуємо isolated Flask app з тільки speakers_bp + tmp DB
для швидких тестів. Повний smoke-flow проганяється у test_endpoints_smoke
через registered blueprint.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from app.blueprints.speakers import speakers_bp
from app.db.migrations import init_database


@pytest.fixture
def client(tmp_path: Path):
    db_path = str(tmp_path / 'speakers_test.db')
    init_database(db_path)

    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.register_blueprint(speakers_bp)
    return app.test_client()


# ---------------------------------------------------------------- list

class TestListSpeakers:
    def test_initial_state_has_only_self(self, client):
        r = client.get('/api/speakers')
        assert r.status_code == 200
        data = r.get_json()
        names = [s['name'] for s in data['speakers']]
        assert 'Ви' in names
        # is_self для seeded
        self_row = next(s for s in data['speakers'] if s['name'] == 'Ви')
        assert self_row['is_self'] is True

    def test_autocomplete_starts_with_filter(self, client):
        # Створимо кілька спікерів
        for name in ('Андрій', 'Анна', 'Богдан', 'Анастасія'):
            client.post('/api/speakers', json={'name': name})

        r = client.get('/api/speakers?q=Ан')
        names = [s['name'] for s in r.get_json()['speakers']]
        # Cyrillic case-folding: 'Ан' має матчити Андрій, Анна, Анастасія
        assert set(names) == {'Андрій', 'Анна', 'Анастасія'}
        assert 'Богдан' not in names

    def test_autocomplete_case_insensitive_cyrillic(self, client):
        client.post('/api/speakers', json={'name': 'Андрій'})

        # 'ан' lowercase має знайти Андрій
        r = client.get('/api/speakers?q=ан')
        names = [s['name'] for s in r.get_json()['speakers']]
        assert 'Андрій' in names

    def test_sort_by_usage_count_desc(self, client):
        # Створюємо двох + бамп usage в одного
        rA = client.post('/api/speakers', json={'name': 'Ada'})
        rB = client.post('/api/speakers', json={'name': 'Bob'})
        ada_id = rA.get_json()['speaker']['id']
        # Створимо transcription і прив'яжемо Ada — це інкрементує usage
        # Робимо через PATCH endpoint щоб usage_count зріс
        # Спочатку треба transcription, але в цьому тестовому додатку немає.
        # Тому просто оновимо вручну через connection.
        from app.db.connection import get_db_connection
        with get_db_connection(client.application.config['DATABASE']) as conn:
            conn.execute(
                'UPDATE speakers SET usage_count = 5 WHERE id = ?', (ada_id,)
            )
            conn.commit()

        r = client.get('/api/speakers?q=')
        names = [s['name'] for s in r.get_json()['speakers']]
        # Ada з usage=5 має бути перед Bob з usage=0 (Ви sorting variable)
        assert names.index('Ada') < names.index('Bob')


# ---------------------------------------------------------------- create

class TestCreateSpeaker:
    def test_create_new(self, client):
        r = client.post('/api/speakers', json={'name': 'Юля'})
        assert r.status_code == 201
        data = r.get_json()
        assert data['success'] is True
        assert data['speaker']['name'] == 'Юля'
        assert data['speaker']['is_self'] is False
        assert data['speaker']['usage_count'] == 0

    def test_create_duplicate_case_insensitive_returns_409(self, client):
        client.post('/api/speakers', json={'name': 'Андрій'})
        r = client.post('/api/speakers', json={'name': 'андрій'})
        assert r.status_code == 409
        data = r.get_json()
        assert 'existing_speaker' in data
        assert data['existing_speaker']['name'] == 'Андрій'

    def test_create_with_color(self, client):
        r = client.post('/api/speakers', json={'name': 'Дима', 'color': '#ff5500'})
        assert r.status_code == 201
        assert r.get_json()['speaker']['color'] == '#ff5500'

    def test_create_with_color_no_hash(self, client):
        r = client.post('/api/speakers', json={'name': 'Дима', 'color': 'ff5500'})
        assert r.status_code == 201
        assert r.get_json()['speaker']['color'] == '#ff5500'

    def test_invalid_color_rejected(self, client):
        r = client.post('/api/speakers', json={'name': 'X', 'color': 'red'})
        assert r.status_code == 400

    def test_empty_name_rejected(self, client):
        r = client.post('/api/speakers', json={'name': '   '})
        assert r.status_code == 400

    def test_name_normalized(self, client):
        r = client.post('/api/speakers', json={'name': '  Слава   Ка   '})
        # Внутрішні множинні пробіли → один, trim країв
        assert r.get_json()['speaker']['name'] == 'Слава Ка'


# ---------------------------------------------------------------- update

class TestUpdateSpeaker:
    def test_rename(self, client):
        rc = client.post('/api/speakers', json={'name': 'OldName'})
        sid = rc.get_json()['speaker']['id']

        r = client.put(f'/api/speakers/{sid}', json={'name': 'NewName'})
        assert r.status_code == 200
        assert r.get_json()['speaker']['name'] == 'NewName'

    def test_change_color(self, client):
        rc = client.post('/api/speakers', json={'name': 'X'})
        sid = rc.get_json()['speaker']['id']

        r = client.put(f'/api/speakers/{sid}', json={'color': '#abcdef'})
        assert r.status_code == 200
        assert r.get_json()['speaker']['color'] == '#abcdef'

    def test_rename_collision_returns_409(self, client):
        client.post('/api/speakers', json={'name': 'Andre'})
        r2 = client.post('/api/speakers', json={'name': 'Bob'})
        bob_id = r2.get_json()['speaker']['id']

        r = client.put(f'/api/speakers/{bob_id}', json={'name': 'andre'})
        assert r.status_code == 409

    def test_rename_to_same_name_ok(self, client):
        rc = client.post('/api/speakers', json={'name': 'Slava'})
        sid = rc.get_json()['speaker']['id']
        # Той самий case → нічого не змінюється, але і не падає
        r = client.put(f'/api/speakers/{sid}', json={'name': 'Slava'})
        assert r.status_code == 200

    def test_404_on_missing(self, client):
        r = client.put('/api/speakers/9999', json={'name': 'X'})
        assert r.status_code == 404


# ---------------------------------------------------------------- delete

class TestDeleteSpeaker:
    def test_delete_existing(self, client):
        rc = client.post('/api/speakers', json={'name': 'Temp'})
        sid = rc.get_json()['speaker']['id']
        r = client.delete(f'/api/speakers/{sid}')
        assert r.status_code == 200

        # Підтверджуємо що його немає у списку
        r2 = client.get('/api/speakers')
        names = [s['name'] for s in r2.get_json()['speakers']]
        assert 'Temp' not in names

    def test_cannot_delete_self(self, client):
        # Знаходимо seeded "Ви"
        r = client.get('/api/speakers')
        self_id = next(s for s in r.get_json()['speakers'] if s['is_self'])['id']

        r = client.delete(f'/api/speakers/{self_id}')
        assert r.status_code == 400
        assert 'не можна' in r.get_json()['error']

    def test_404_on_missing(self, client):
        r = client.delete('/api/speakers/9999')
        assert r.status_code == 404


# ---------------------------------------------------------------- patch transcription speakers

class TestPatchTranscriptionSpeakers:
    def _create_transcription(self, client) -> int:
        from app.db.connection import get_db_connection
        with get_db_connection(client.application.config['DATABASE']) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT INTO transcriptions
                  (source_type, source_name, transcript_text, segments)
                VALUES ('file', 'meet.mp3', 'привіт', '[]')
            ''')
            conn.commit()
            return c.lastrowid

    def test_create_new_mapping(self, client):
        tid = self._create_transcription(client)
        r = client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': 'Юля'}},
        )
        assert r.status_code == 200
        data = r.get_json()
        speakers = {s['raw_label']: s for s in data['speakers']}
        assert speakers['SPEAKER_00']['name'] == 'Юля'
        assert speakers['SPEAKER_00']['speaker_id'] is not None

    def test_existing_speaker_reused_and_usage_incremented(self, client):
        tid = self._create_transcription(client)
        # Створимо спікера наперед
        rc = client.post('/api/speakers', json={'name': 'Адам'})
        adam_id = rc.get_json()['speaker']['id']

        # Mapping використовує існуюче ім'я
        client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': 'адам'}},  # case insensitive
        )

        # speaker_id має бути той самий
        r = client.get('/api/speakers?q=Ад')
        adam_row = next(s for s in r.get_json()['speakers'] if s['name'] == 'Адам')
        assert adam_row['id'] == adam_id
        assert adam_row['usage_count'] == 1  # інкремент

    def test_clear_mapping_with_null(self, client):
        tid = self._create_transcription(client)
        # Спочатку прив'яжемо
        client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': 'X'}},
        )
        # Тепер очистимо
        r = client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': None}},
        )
        speakers = {s['raw_label']: s for s in r.get_json()['speakers']}
        assert speakers['SPEAKER_00']['speaker_id'] is None

    def test_clear_mapping_with_empty_string(self, client):
        tid = self._create_transcription(client)
        client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': 'X'}},
        )
        r = client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': '  '}},  # тільки whitespace
        )
        speakers = {s['raw_label']: s for s in r.get_json()['speakers']}
        assert speakers['SPEAKER_00']['speaker_id'] is None

    def test_404_on_missing_transcription(self, client):
        r = client.patch(
            '/api/transcriptions/9999/speakers',
            json={'mapping': {'SPEAKER_00': 'X'}},
        )
        assert r.status_code == 404

    def test_invalid_mapping_format_400(self, client):
        tid = self._create_transcription(client)
        r = client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': 'not-an-object'},
        )
        assert r.status_code == 400

    def test_overwrite_existing_mapping(self, client):
        tid = self._create_transcription(client)
        # Першай прив'язка → "Спікер A"
        client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': 'Перший'}},
        )
        # Друга → "Спікер B" (заміна)
        r = client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': 'Другий'}},
        )
        speakers = {s['raw_label']: s for s in r.get_json()['speakers']}
        assert speakers['SPEAKER_00']['name'] == 'Другий'
