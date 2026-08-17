"""Speaker management endpoints (Phase 10.4).

CRUD над глобальною таблицею speakers + bulk-rename per-transcription.

Endpoints:
- GET    /api/speakers                              — список (з опц. ?q= для autocomplete)
- POST   /api/speakers                              — створити нового
- PUT    /api/speakers/<id>                         — перейменувати/змінити колір
- DELETE /api/speakers/<id>                         — видалити (заборонено для is_self=1)
- PATCH  /api/transcriptions/<id>/speakers          — bulk: {raw_label: name|null}
- POST   /api/speakers/merge                        — обʼєднати кількох в одного (снапшот для undo)
- POST   /api/speakers/unmerge/<merge_id>           — відкотити merge зі снапшоту (T4.6)
- GET    /api/speakers/<id>/samples                 — приклади фраз (merge-preview, T4.6)

Cyrillic case-insensitive matching робиться на app-рівні (Python
str.lower() підтримує Unicode case-folding, на відміну від SQLite
COLLATE NOCASE який працює лише для ASCII).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections import defaultdict

from flask import Blueprint, current_app, jsonify, request

from app.repositories import speakers as speakers_repo
from app.repositories import transcriptions as tx_repo


logger = logging.getLogger(__name__)
speakers_bp = Blueprint('speakers', __name__)


# Базова валідація імені: 1-100 символів, не лише пробіли. Inline regex
# для запобігання SQL/HTML-ін'єкцій ми не робимо тут — параметризовані
# запити SQLite + escapeHTML на frontend закривають це.
_NAME_MAX_LEN = 100
_COLOR_RE = re.compile(r'^#?[0-9a-fA-F]{6}$')


def _get_db():
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


def _normalize_name(name: str) -> str:
    """Trim + collapse internal whitespace. Не lower-cas-ить
    (відображення зберігає оригінальний регістр)."""
    return ' '.join(name.split())


def _validate_name(raw: str | None) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, "name мусить бути string"
    name = _normalize_name(raw)
    if not name:
        return None, "name не може бути порожнім"
    if len(name) > _NAME_MAX_LEN:
        return None, f"name занадто довгий (max {_NAME_MAX_LEN})"
    return name, None


def _validate_color(raw) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None  # ok, optional
    if not isinstance(raw, str):
        return None, "color мусить бути string '#RRGGBB' або null"
    if not _COLOR_RE.match(raw):
        return None, "color має бути hex '#RRGGBB' або 'RRGGBB'"
    return raw if raw.startswith('#') else f'#{raw}', None


def _find_speaker_case_insensitive(conn, name: str):
    """Шукає спікера з case-insensitive matching (Cyrillic-safe).

    SQLite COLLATE NOCASE працює тільки для ASCII, тому фільтруємо в Python.
    Для невеликих таблиць (<1000 рядків) це O(n) і прийнятно.
    """
    target = name.lower()
    for row in conn.execute("SELECT id, name FROM speakers"):
        if row['name'].lower() == target:
            return row
    return None


def _serialize_speaker(row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'color': row['color'],
        'is_self': bool(row['is_self']),
        'usage_count': row['usage_count'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


# ---------------------------------------------------------------- list / autocomplete

@speakers_bp.route('/api/speakers', methods=['GET'])
def list_speakers():
    """Список спікерів. ?q= для autocomplete (starts-with, case-insensitive).

    Сортування: usage_count desc, потім name asc. Це дає найвищу
    "ймовірну" підказку першою (часто вживане ім'я).
    """
    q_raw = (request.args.get('q') or '').strip()
    limit = max(1, min(50, request.args.get('limit', 50, type=int)))

    with _get_db() as conn:
        rows = conn.execute('''
            SELECT id, name, color, is_self, usage_count, created_at, updated_at
            FROM speakers
            ORDER BY usage_count DESC, name COLLATE NOCASE ASC
        ''').fetchall()

    if q_raw:
        # Cyrillic-safe starts-with: Python str.lower() обробляє Unicode
        q_lower = q_raw.lower()
        filtered = [r for r in rows if r['name'].lower().startswith(q_lower)]
    else:
        filtered = rows

    return jsonify({
        'speakers': [_serialize_speaker(r) for r in filtered[:limit]],
        'total': len(filtered),
    })


# ---------------------------------------------------------------- create

@speakers_bp.route('/api/speakers', methods=['POST'])
def create_speaker():
    """Створити нового спікера. Якщо ім'я (case-insensitive) вже існує —
    повертає 409 із посиланням на існуючий ID."""
    data = request.get_json(silent=True) or {}
    name, name_err = _validate_name(data.get('name'))
    if name_err:
        return jsonify({'success': False, 'error': name_err}), 400
    color, color_err = _validate_color(data.get('color'))
    if color_err:
        return jsonify({'success': False, 'error': color_err}), 400

    with _get_db() as conn:
        existing = _find_speaker_case_insensitive(conn, name)
        if existing:
            full = speakers_repo.get_full_by_id(conn, existing['id'])
            return jsonify({
                'success': False,
                'error': 'Спікер з таким іменем уже існує',
                'existing_speaker': _serialize_speaker(full),
            }), 409

        c = conn.cursor()
        c.execute(
            'INSERT INTO speakers (name, color) VALUES (?, ?)',
            (name, color),
        )
        new_id = c.lastrowid
        conn.commit()
        row = speakers_repo.get_full_by_id(conn, new_id)

    return jsonify({'success': True, 'speaker': _serialize_speaker(row)}), 201


# ---------------------------------------------------------------- update

@speakers_bp.route('/api/speakers/<int:speaker_id>', methods=['PUT'])
def update_speaker(speaker_id):
    """Перейменувати або змінити колір. is_self не редагується через цей API."""
    data = request.get_json(silent=True) or {}
    fields_to_update: list[tuple[str, object]] = []

    if 'name' in data:
        name, name_err = _validate_name(data.get('name'))
        if name_err:
            return jsonify({'success': False, 'error': name_err}), 400
        fields_to_update.append(('name', name))
    if 'color' in data:
        color, color_err = _validate_color(data.get('color'))
        if color_err:
            return jsonify({'success': False, 'error': color_err}), 400
        fields_to_update.append(('color', color))

    if not fields_to_update:
        return jsonify({'success': False, 'error': 'Нічого не задано для оновлення'}), 400

    with _get_db() as conn:
        existing = conn.execute(
            'SELECT id, name FROM speakers WHERE id = ?', (speaker_id,)
        ).fetchone()
        if not existing:
            return jsonify({'success': False, 'error': 'Спікер не знайдено'}), 404

        # При rename: перевірити на конфлікт case-insensitive (виключаючи цей же id)
        new_name = next((v for k, v in fields_to_update if k == 'name'), None)
        if new_name and new_name.lower() != existing['name'].lower():
            conflict = _find_speaker_case_insensitive(conn, new_name)
            if conflict and conflict['id'] != speaker_id:
                return jsonify({
                    'success': False,
                    'error': f'Ім\'я "{new_name}" вже використовується',
                }), 409

        set_clause = ', '.join(f'{k} = ?' for k, _ in fields_to_update)
        params = [v for _, v in fields_to_update] + [speaker_id]
        conn.execute(
            f'UPDATE speakers SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            params,
        )
        conn.commit()
        row = speakers_repo.get_full_by_id(conn, speaker_id)

    return jsonify({'success': True, 'speaker': _serialize_speaker(row)})


# ---------------------------------------------------------------- delete

@speakers_bp.route('/api/speakers/<int:speaker_id>', methods=['DELETE'])
def delete_speaker(speaker_id):
    """Видалити спікера. Заборонено для is_self=1 (потрібен системі).

    Phase 10.3 spec: ON DELETE SET NULL у transcription_speaker_map → всі
    рядки з цим speaker_id отримають NULL (raw_label лишиться, frontend
    показуватиме як "Спікер N" знову). foreign_keys=ON у connection
    забезпечує спрацьовування cascade.
    """
    with _get_db() as conn:
        row = conn.execute(
            'SELECT id, is_self FROM speakers WHERE id = ?', (speaker_id,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Спікер не знайдено'}), 404
        if row['is_self']:
            return jsonify({
                'success': False,
                'error': '"Ви" не можна видалити — використовується для mic-стріму',
            }), 400
        conn.execute('DELETE FROM speakers WHERE id = ?', (speaker_id,))
        conn.commit()

    return jsonify({'success': True})


# ---------------------------------------------------------------- bulk rename per transcript

@speakers_bp.route('/api/transcriptions/<int:transcription_id>/speakers', methods=['PATCH'])
def patch_transcription_speakers(transcription_id):
    """Bulk-mapping raw_label → ім'я для конкретного transcript-а.

    Body: {
        "mapping": {
            "SPEAKER_00": "Андрій",         // створить/знайде → встановить speaker_id
            "SPEAKER_01": null,             // очистить mapping (стане unnamed)
            "self": "Андрій"                // переприв'язати self до іншого імені
        }
    }

    Returns: повний оновлений speaker map після операції + інкремент
    usage_count для новостворених прив'язок.
    """
    data = request.get_json(silent=True) or {}
    mapping = data.get('mapping', {})
    if not isinstance(mapping, dict):
        return jsonify({'success': False, 'error': 'mapping має бути обʼєктом'}), 400

    with _get_db() as conn:
        if not tx_repo.exists(conn, transcription_id):
            return jsonify({'success': False, 'error': 'Транскрипт не знайдено'}), 404

        c = conn.cursor()
        usage_increments: list[int] = []  # speaker_ids на яких треба підняти usage_count

        # Phase 10.6: для voice fingerprinting беремо embedding з map
        from app.services.diarization_service import (
            average_embeddings, blob_to_embedding, embedding_to_blob,
        )

        for raw_label, value in mapping.items():
            if not isinstance(raw_label, str) or not raw_label:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                # Очистити mapping
                c.execute(
                    'UPDATE transcription_speaker_map SET speaker_id = NULL '
                    'WHERE transcription_id = ? AND raw_label = ?',
                    (transcription_id, raw_label),
                )
                continue

            # Знайти або створити спікера за іменем
            name, err = _validate_name(value)
            if err:
                return jsonify({
                    'success': False,
                    'error': f'Невірне імʼя для {raw_label}: {err}',
                }), 400
            existing = _find_speaker_case_insensitive(conn, name)
            if existing:
                speaker_id = existing['id']
            else:
                c.execute('INSERT INTO speakers (name) VALUES (?)', (name,))
                speaker_id = c.lastrowid

            # Phase 10.6: дістаємо embedding з transcription_speaker_map для
            # цього raw_label (зберіг його diarization step при transcribe).
            map_row = c.execute(
                'SELECT embedding FROM transcription_speaker_map '
                'WHERE transcription_id = ? AND raw_label = ?',
                (transcription_id, raw_label),
            ).fetchone()
            new_embedding = blob_to_embedding(map_row['embedding']) if map_row else None

            # Upsert mapping. Якщо рядка ще немає (наприклад, fresh frontend
            # додає mapping для labels що не виявились автоматично) —
            # створимо. PRIMARY KEY на (transcription_id, raw_label) це
            # підтримує через INSERT OR REPLACE.
            c.execute(
                'INSERT OR REPLACE INTO transcription_speaker_map '
                '(transcription_id, raw_label, speaker_id, embedding) '
                'VALUES (?, ?, ?, ?)',
                (transcription_id, raw_label, speaker_id,
                 map_row['embedding'] if map_row else None),
            )
            usage_increments.append(speaker_id)

            # Running average у speakers.embedding. Вага старого = usage_count
            # (скільки разів цей speaker_id вже вживався), нового = 1.
            if new_embedding:
                speaker_row = c.execute(
                    'SELECT embedding, usage_count FROM speakers WHERE id = ?',
                    (speaker_id,),
                ).fetchone()
                old_embedding = blob_to_embedding(speaker_row['embedding']) if speaker_row else None
                old_usage = speaker_row['usage_count'] if speaker_row else 0
                merged = average_embeddings(
                    old_embedding, new_embedding,
                    weight_a=max(1, old_usage),  # min 1 щоб не ділити на 0 при першому
                    weight_b=1.0,
                )
                merged_blob = embedding_to_blob(merged)
                c.execute(
                    'UPDATE speakers SET embedding = ? WHERE id = ?',
                    (merged_blob, speaker_id),
                )

        # Інкремент usage_count для нових прив'язок (унікально по speaker_id)
        for sid in set(usage_increments):
            c.execute(
                'UPDATE speakers SET usage_count = usage_count + 1, '
                'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (sid,),
            )
        conn.commit()

        # Повертаємо оновлений map
        speaker_rows = conn.execute('''
            SELECT m.raw_label, m.speaker_id, s.name, s.color, s.is_self
            FROM transcription_speaker_map m
            LEFT JOIN speakers s ON s.id = m.speaker_id
            WHERE m.transcription_id = ?
        ''', (transcription_id,)).fetchall()

    return jsonify({
        'success': True,
        'speakers': [
            {
                'raw_label': r['raw_label'],
                'speaker_id': r['speaker_id'],
                'name': r['name'],
                'color': r['color'],
                'is_self': bool(r['is_self']) if r['is_self'] is not None else False,
            }
            for r in speaker_rows
        ],
    })


# ---------------------------------------------------------------- stats (Phase 12.10)

def _entity_index(conn) -> dict:
    """Написання людини → id сутності графа, коли воно однозначне.

    Потрібно, щоб звести два шари: «Слава Верес» у діаризації і «Veres
    Viacheslav» у переписці — це одна людина, але таблиці про це не знають.
    Граф знає частину звʼязків (у сутності 478 «Юлія» серед аліасів є «Julia
    Bondarenko»), тож беремо ЙОГО звʼязки, а не здогадуємось за схожістю імен.

    Беремо ЛИШЕ `type='person'`: обидва списки — про людей, і привʼязка означає
    «це та сама людина». Без цього фільтра канал або бот, чиє імʼя збігається з
    назвою проєкту чи організації, отримав би `entity_id` і потрапив у лічильник
    зведених — тобто шари «зійшлися б» через сутність, яка людиною не є.

    Ключі згортаємо `casefold()` у Python: SQLite `LOWER` не чіпає кирилицю.
    Неоднозначні написання (те саме імʼя веде до кількох сутностей) свідомо
    лишаємо без id — здогад тут коштував би дорожче за прочерк.
    """
    idx: dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT id, canonical_name AS name FROM entities "
                          "WHERE type = 'person'"):
        if r['name']:
            idx[r['name'].casefold().strip()].add(r['id'])
    for r in conn.execute('SELECT a.entity_id, a.alias FROM entity_aliases a '
                          "JOIN entities e ON e.id = a.entity_id WHERE e.type = 'person'"):
        if r['alias']:
            idx[r['alias'].casefold().strip()].add(r['entity_id'])
    return {k: next(iter(v)) for k, v in idx.items() if len(v) == 1}


@speakers_bp.route('/api/speakers/stats', methods=['GET'])
def speakers_stats():
    """Агреговані статистики кожного спікера через всі transcripts.

    Повертає для кожного спікера: transcripts_count, total_seconds (sum
    end-start всіх segments цього speaker'а), words_count (split text),
    first_seen, last_seen (created_at найстарішого / найновішого
    transcript'у де він присутній).

    O(N) по transcriptions з diarization. Парсимо segments JSON у
    Python — без JSON1 SQLite extension робити це SQL'ом неможливо.

    **Два шари, а не один.** `speakers` — це діаризація, тобто ГОЛОСИ зі
    дзвінків і записів: частина записів. Решта архіву — переписка, де
    автор відомий точно (`tg_sender`, 35 людей на повідомлень) і де немає
    ні секунд, ні діаризації. Поки відповідь мовчала про це, «статистика по
    кожному спікеру» описувала дрібна частка архіву й читалась як увесь.

    Тому в тілі три частини: `speakers` (голоси), `tg_senders` (переписка) і
    `coverage` (що саме порахували). Числа шарів НЕ додаються: секунди мовлення
    і кількість повідомлень — різні одиниці.
    """
    with _get_db() as conn:
        # Base stats
        rows = conn.execute('''
            SELECT s.id, s.name, s.color, s.is_self, s.usage_count, s.created_at, s.updated_at,
                   -- Рахуємо по `t`, а не по `m`: звʼязка в мапі лишається й
                   -- після мʼякого видалення запису, тож `m` дав би видалене.
                   COUNT(DISTINCT t.id) as transcripts_count,
                   -- Дата ПОДІЇ, а не заливки. `created_at` тут означав «коли
                   -- запис потрапив в архів», тоді як у `tg_senders` поруч
                   -- лежить дата повідомлення: однаково названі поля з різним
                   -- сенсом штовхають саме до того порівняння шарів, від якого
                   -- застерігає опис тулза.
                   MAX(COALESCE(t.meeting_date, substr(t.created_at,1,10))) as last_seen,
                   MIN(COALESCE(t.meeting_date, substr(t.created_at,1,10))) as first_seen
            FROM speakers s
            LEFT JOIN transcription_speaker_map m ON m.speaker_id = s.id
            -- Умова видалення саме в ON, а не в WHERE: інакше LEFT JOIN стає
            -- внутрішнім і спікери без записів зникають зі списку.
            LEFT JOIN transcriptions t ON t.id = m.transcription_id
                                      AND t.deleted_at IS NULL
            GROUP BY s.id
            ORDER BY s.usage_count DESC, s.name COLLATE NOCASE
        ''').fetchall()

        # Map (tx_id, raw_label) → speaker_id для агрегації по segments
        map_rows = conn.execute(
            'SELECT transcription_id, raw_label, speaker_id '
            'FROM transcription_speaker_map WHERE speaker_id IS NOT NULL'
        ).fetchall()

        by_tx: dict[int, dict[str, int]] = defaultdict(dict)
        for r in map_rows:
            by_tx[r['transcription_id']][r['raw_label']] = r['speaker_id']

        seconds_per_speaker: dict[int, float] = defaultdict(float)
        words_per_speaker: dict[int, int] = defaultdict(int)

        if by_tx:
            tx_rows = tx_repo.get_many_by_ids(conn, list(by_tx.keys()), columns=('id', 'segments'))
            for tx in tx_rows:
                if not tx['segments']:
                    continue
                try:
                    segs = json.loads(tx['segments'])
                except (json.JSONDecodeError, TypeError):
                    continue
                labels = by_tx[tx['id']]
                for seg in segs:
                    raw = seg.get('speaker')
                    if not raw or raw not in labels:
                        continue
                    sid = labels[raw]
                    try:
                        dur = float(seg.get('end', 0)) - float(seg.get('start', 0))
                    except (TypeError, ValueError):
                        dur = 0
                    if dur > 0:
                        seconds_per_speaker[sid] += dur
                    text = seg.get('text') or ''
                    if text:
                        words_per_speaker[sid] += len(text.split())

        ent_idx = _entity_index(conn)

        # Другий шар: автори переписки. Одиниці тут інші (повідомлення, чати),
        # тому це окремий список, а не дописані рядки до `speakers`.
        tg_rows = conn.execute('''
            SELECT tg_sender AS name, COUNT(*) AS messages,
                   COUNT(DISTINCT tg_chat_id) AS chats,
                   MIN(COALESCE(meeting_date, substr(created_at,1,10))) AS first_seen,
                   MAX(COALESCE(meeting_date, substr(created_at,1,10))) AS last_seen
            FROM transcriptions
            WHERE source_type = 'telegram' AND deleted_at IS NULL
              AND tg_sender IS NOT NULL AND tg_sender <> ''
            GROUP BY tg_sender
            ORDER BY messages DESC
        ''').fetchall()

        # `deleted_at IS NULL` тут обовʼязковий: видалення одного запису мʼяке
        # (ставить `deleted_at`), а рядки `transcription_speaker_map` лишаються
        # до масового чищення. Без фільтра чисельник тримав би видалене, поки
        # знаменник його вже не рахує, — і «частина записів» завищувало б
        # покриття діаризації. Саме той клас твердження, проти якого ця гілка.
        diarized = conn.execute(
            'SELECT COUNT(DISTINCT m.transcription_id) AS n '
            'FROM transcription_speaker_map m '
            'JOIN transcriptions t ON t.id = m.transcription_id '
            'WHERE t.deleted_at IS NULL'
        ).fetchone()['n']
        total_tx = conn.execute(
            'SELECT COUNT(*) AS n FROM transcriptions WHERE deleted_at IS NULL'
        ).fetchone()['n']

    tg_senders = [{
        'name': r['name'],
        'messages': r['messages'],
        'chats': r['chats'],
        'first_seen': r['first_seen'],
        'last_seen': r['last_seen'],
        # Той самий графовий звʼязок, що й у спікерів: якщо в обох шарах стоїть
        # той самий entity_id — це одна людина, і це знає граф, а не здогад.
        'entity_id': ent_idx.get((r['name'] or '').casefold().strip()),
    } for r in tg_rows]

    speakers = []
    total_seconds_all = 0.0
    for r in rows:
        sid = r['id']
        ts = round(seconds_per_speaker.get(sid, 0.0), 1)
        total_seconds_all += ts
        speakers.append({
            'id': sid,
            'name': r['name'],
            'color': r['color'],
            'is_self': bool(r['is_self']),
            'usage_count': r['usage_count'],
            'transcripts_count': r['transcripts_count'] or 0,
            'total_seconds': ts,
            'words_count': words_per_speaker.get(sid, 0),
            'first_seen': r['first_seen'],
            'last_seen': r['last_seen'],
            'created_at': r['created_at'],
            'entity_id': ent_idx.get((r['name'] or '').casefold().strip()),
        })

    linked = sum(1 for s in tg_senders if s['entity_id'])
    return jsonify({
        'speakers': speakers,
        'total_speakers': len(speakers),
        'total_seconds': round(total_seconds_all, 1),
        'tg_senders': tg_senders,
        'total_tg_senders': len(tg_senders),
        'coverage': {
            'diarized_transcripts': diarized,
            'total_transcripts': total_tx,
            'telegram_messages': sum(s['messages'] for s in tg_senders),
            'tg_senders_linked_to_graph': linked,
            'note': (
                f'`speakers` — голоси з діаризації: {diarized} записів із {total_tx}. '
                f'Решта архіву — переписка, де автор відомий точно і рахується в '
                f'`tg_senders` (без секунд і слів мовлення). Одиниці шарів різні, '
                f'їх не додають. Спільна людина видна через `entity_id`: він '
                f'проставлений у {linked} із {len(tg_senders)} відправників, '
                f'решту граф просто не знає під цим написанням'
            ),
        },
    })


# ---------------------------------------------------------------- speaker timeline (Phase 12.23)

@speakers_bp.route('/api/speakers/<int:speaker_id>/timeline', methods=['GET'])
def speaker_timeline(speaker_id):
    """Активність speaker'а по днях за останні N днів (default: 30).

    Для кожного дня в інтервалі повертає:
    - transcripts_count (DISTINCT transcript_ids де speaker присутній)
    - total_seconds (сума duration всіх segments цього speaker'а)
    - words_count

    Frontend будує bar chart day-by-day.
    """
    days = max(1, min(365, request.args.get('days', 30, type=int)))

    with _get_db() as conn:
        speaker = conn.execute(
            'SELECT id, name FROM speakers WHERE id = ?', (speaker_id,)
        ).fetchone()
        if not speaker:
            return jsonify({'success': False, 'error': 'Спікер не знайдено'}), 404

        # Усі transcripts де цей speaker присутній за останні N днів.
        rows = conn.execute('''
            SELECT t.id, t.created_at, t.segments, m.raw_label
            FROM transcriptions t
            JOIN transcription_speaker_map m ON m.transcription_id = t.id
            WHERE m.speaker_id = ?
              AND t.created_at >= datetime('now', ? || ' days')
            ORDER BY t.created_at DESC
        ''', (speaker_id, f'-{days}')).fetchall()

    # Aggregation per-day. Key — YYYY-MM-DD.
    by_day: dict[str, dict] = defaultdict(lambda: {
        'transcripts': set(),
        'total_seconds': 0.0,
        'words_count': 0,
    })

    for r in rows:
        try:
            day = r['created_at'][:10]  # 'YYYY-MM-DD HH:MM:SS' → 'YYYY-MM-DD'
            segs = json.loads(r['segments']) if r['segments'] else []
        except (json.JSONDecodeError, TypeError):
            continue
        bucket = by_day[day]
        bucket['transcripts'].add(r['id'])
        for seg in segs:
            if seg.get('speaker') != r['raw_label']:
                continue
            try:
                dur = float(seg.get('end', 0)) - float(seg.get('start', 0))
            except (TypeError, ValueError):
                dur = 0
            if dur > 0:
                bucket['total_seconds'] += dur
            text = seg.get('text') or ''
            if text:
                bucket['words_count'] += len(text.split())

    # Перетворити set у count + serialize
    timeline = [
        {
            'date': day,
            'transcripts_count': len(b['transcripts']),
            'total_seconds': round(b['total_seconds'], 1),
            'words_count': b['words_count'],
        }
        for day, b in sorted(by_day.items())
    ]

    return jsonify({
        'speaker_id': speaker_id,
        'speaker_name': speaker['name'],
        'days': days,
        'timeline': timeline,
        'summary': {
            'total_seconds': round(sum(d['total_seconds'] for d in timeline), 1),
            'total_words': sum(d['words_count'] for d in timeline),
            'active_days': len(timeline),
            'total_transcripts': len(set().union(*(b['transcripts'] for b in by_day.values()))),
        },
    })


# ---------------------------------------------------------------- merge speakers (Phase 12.8)

def _blob_b64(blob) -> str | None:
    return base64.b64encode(blob).decode('ascii') if blob else None


def _b64_blob(s) -> bytes | None:
    return base64.b64decode(s) if s else None


@speakers_bp.route('/api/speakers/merge', methods=['POST'])
def merge_speakers():
    """Об'єднати кілька спікерів у одного.

    Body: {
        "keep_id": 3,                    // який залишити
        "merge_ids": [7, 12]             // які влити та видалити
    }

    Дії:
    1. Усі transcription_speaker_map.speaker_id ∈ merge_ids → keep_id
    2. speakers.embedding keep_id оновлюється як weighted average
       (вага = usage_count кожного, мін 1).
    3. speakers.usage_count keep_id += sum(usage_count merge_ids)
    4. DELETE merge_ids зі speakers.

    Заборонено:
    - keep_id у merge_ids
    - merge_ids містить is_self=1 (системний "Ви")

    T4.6 (REMEDIATION_PLAN Волна 2): перед кроками 1-4 записує before-снапшот
    (видалені speakers-рядки + попередній map-мапінг + entities.speaker_id,
    які FK ON DELETE SET NULL обнулить) у ``speaker_merges`` — щоб
    ``POST /api/speakers/unmerge/<merge_id>`` міг відкотити merge протягом
    grace-періоду (``RECALL_SOFTDELETE_GRACE_DAYS``). Response додатково
    містить ``merge_id`` для цього undo.
    """
    data = request.get_json(silent=True) or {}
    keep_id = data.get('keep_id')
    merge_ids = data.get('merge_ids', [])

    if not isinstance(keep_id, int) or keep_id <= 0:
        return jsonify({'success': False, 'error': 'keep_id мусить бути позитивним int'}), 400
    if not isinstance(merge_ids, list) or not merge_ids:
        return jsonify({'success': False, 'error': 'merge_ids мусить бути непорожнім списком'}), 400
    try:
        merge_ids = [int(x) for x in merge_ids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'merge_ids має містити лише числа'}), 400
    if keep_id in merge_ids:
        return jsonify({'success': False, 'error': 'keep_id не може бути у merge_ids'}), 400

    from app.services.diarization_service import (
        average_embeddings, blob_to_embedding, embedding_to_blob,
    )

    with _get_db() as conn:
        c = conn.cursor()
        keep_row = c.execute(
            'SELECT id, name, color, is_self, embedding, usage_count, created_at, updated_at '
            'FROM speakers WHERE id = ?',
            (keep_id,),
        ).fetchone()
        if not keep_row:
            return jsonify({'success': False, 'error': 'keep_id не знайдено'}), 404

        # Verify усі merge_ids існують та не є is_self
        placeholders = ','.join('?' * len(merge_ids))
        merge_rows = c.execute(
            f'SELECT id, name, color, is_self, embedding, usage_count, created_at, updated_at '
            f'FROM speakers WHERE id IN ({placeholders})',
            merge_ids,
        ).fetchall()
        if len(merge_rows) != len(set(merge_ids)):
            return jsonify({'success': False, 'error': 'Деякі merge_ids не знайдено'}), 404
        if any(r['is_self'] for r in merge_rows):
            return jsonify({
                'success': False,
                'error': '"Ви" (is_self=1) не можна злити з іншим спікером',
            }), 400

        # --- T4.6: before-снапшот (ДО будь-яких мутацій) ---
        map_before = c.execute(
            f'SELECT transcription_id, raw_label, speaker_id '
            f'FROM transcription_speaker_map WHERE speaker_id IN ({placeholders})',
            merge_ids,
        ).fetchall()
        entities_before = c.execute(
            f'SELECT id, speaker_id FROM entities WHERE speaker_id IN ({placeholders})',
            merge_ids,
        ).fetchall()
        before_json = json.dumps({
            'keep': {
                'id': keep_row['id'],
                'embedding_b64': _blob_b64(keep_row['embedding']),
                'usage_count': keep_row['usage_count'],
            },
            'merged_speakers': [
                {
                    'id': r['id'], 'name': r['name'], 'color': r['color'],
                    'is_self': r['is_self'], 'usage_count': r['usage_count'],
                    'embedding_b64': _blob_b64(r['embedding']),
                    'created_at': r['created_at'], 'updated_at': r['updated_at'],
                }
                for r in merge_rows
            ],
            'map_relinked': [
                {'transcription_id': r['transcription_id'], 'raw_label': r['raw_label'],
                 'speaker_id': r['speaker_id']}
                for r in map_before
            ],
            'entities_relinked': [
                {'id': r['id'], 'speaker_id': r['speaker_id']} for r in entities_before
            ],
        }, ensure_ascii=False)

        # 1. Re-link map: усі map'и з merge_ids → keep_id.
        # Якщо у одному transcript є рядки з різними raw_label на різні
        # merge_ids — після UPDATE усі вкажуть на keep_id, що ОК
        # (PRIMARY KEY на (transcription_id, raw_label), не speaker_id).
        c.execute(
            f'UPDATE transcription_speaker_map SET speaker_id = ? '
            f'WHERE speaker_id IN ({placeholders})',
            [keep_id] + merge_ids,
        )
        relinked = c.rowcount

        # 2. Average embeddings — weighted by usage_count, мін 1.
        keep_embedding = blob_to_embedding(keep_row['embedding'])
        keep_weight = max(1, keep_row['usage_count'] or 0)
        merged_embedding = keep_embedding
        merged_weight = keep_weight
        for r in merge_rows:
            other = blob_to_embedding(r['embedding'])
            if other is None:
                continue
            other_w = max(1, r['usage_count'] or 0)
            merged_embedding = average_embeddings(
                merged_embedding, other,
                weight_a=merged_weight, weight_b=other_w,
            )
            merged_weight += other_w

        new_blob = embedding_to_blob(merged_embedding) if merged_embedding is not None else None

        # 3. Update keep: embedding + bumped usage_count
        total_usage = (keep_row['usage_count'] or 0) + sum(r['usage_count'] or 0 for r in merge_rows)
        c.execute(
            'UPDATE speakers SET embedding = ?, usage_count = ?, '
            'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (new_blob, total_usage, keep_id),
        )

        # 4. Delete merge_ids (entities.speaker_id → FK ON DELETE SET NULL,
        # тому знімок entities_relinked вище зроблено ДО цього кроку)
        c.execute(
            f'DELETE FROM speakers WHERE id IN ({placeholders})',
            merge_ids,
        )

        # 5. Persist before-снапшот для undo
        merged_at = time.time()
        c.execute(
            'INSERT INTO speaker_merges (keep_id, merged_speaker_ids, before_json, merged_at) '
            'VALUES (?, ?, ?, ?)',
            (keep_id, json.dumps(merge_ids), before_json, merged_at),
        )
        merge_id = c.lastrowid

        conn.commit()

        kept = conn.execute(
            'SELECT id, name, color, is_self, usage_count, created_at, updated_at '
            'FROM speakers WHERE id = ?', (keep_id,),
        ).fetchone()

    logger.info(
        f"[merge] keep_id={keep_id} ({keep_row['name']}) absorbed "
        f"{len(merge_rows)} speakers ({[r['name'] for r in merge_rows]}); "
        f"relinked {relinked} map rows, total_usage={total_usage}, merge_id={merge_id}"
    )
    return jsonify({
        'success': True,
        'speaker': _serialize_speaker(kept),
        'merged_count': len(merge_rows),
        'relinked_map_rows': relinked,
        'merge_id': merge_id,
    })


# ---------------------------------------------------------------- unmerge (T4.6)

@speakers_bp.route('/api/speakers/unmerge/<int:merge_id>', methods=['POST'])
def unmerge_speakers(merge_id):
    """Відкотити merge_speakers зі снапшоту (undo), поки він не прострочений
    (RECALL_SOFTDELETE_GRACE_DAYS) і ще не був відновлений раніше.

    Дії (дзеркальні до merge):
    1. Пересоздати видалені speakers-рядки (той самий id — AUTOINCREMENT
       дозволяє явний INSERT з id; жоден новий speaker відтоді не міг зайняти
       це число).
    2. Повернути transcription_speaker_map.speaker_id для КОНКРЕТНИХ
       (transcription_id, raw_label) пар, що змінив цей merge (map_before) —
       НЕ всі поточні рядки на keep_id (щоб не займати чужі пізніші зміни).
    3. Повернути entities.speaker_id, обнулений FK ON DELETE SET NULL —
       лише якщо він і досі NULL (не перезаписуємо ручні зміни після merge).
    4. Відновити embedding/usage_count keep_id зі снапшоту.
    5. Позначити speaker_merges.restored_at.

    409, якщо снапшот вже використано (restored_at IS NOT NULL) або
    прострочено grace-періодом і прибрано purge'ом (тоді рядка вже нема — 404).
    """
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute(
            'SELECT id, keep_id, before_json, restored_at FROM speaker_merges WHERE id = ?',
            (merge_id,),
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Merge не знайдено (можливо, прострочено)'}), 404
        if row['restored_at'] is not None:
            return jsonify({'success': False, 'error': 'Цей merge вже відновлено'}), 409

        try:
            snap = json.loads(row['before_json'])
        except (json.JSONDecodeError, TypeError):
            return jsonify({'success': False, 'error': 'Пошкоджений снапшот merge'}), 500

        # 1. Пересоздати видалені speakers (INSERT OR IGNORE — якщо хтось уже
        # створив нового спікера з тим самим id, теоретично неможливо через
        # AUTOINCREMENT, але про всяк випадок не валимось).
        for sp in snap.get('merged_speakers', []):
            c.execute(
                'INSERT OR IGNORE INTO speakers '
                '(id, name, color, is_self, usage_count, embedding, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (sp['id'], sp['name'], sp.get('color'), sp.get('is_self', 0),
                 sp.get('usage_count', 0), _b64_blob(sp.get('embedding_b64')),
                 sp.get('created_at'), sp.get('updated_at')),
            )

        # 2. Повернути map-рядки саме цього merge (тільки якщо raw_label ще
        # належить транскрипту — рядок міг бути видалений/змінений відтоді).
        for m in snap.get('map_relinked', []):
            c.execute(
                'UPDATE transcription_speaker_map SET speaker_id = ? '
                'WHERE transcription_id = ? AND raw_label = ?',
                (m['speaker_id'], m['transcription_id'], m['raw_label']),
            )

        # 3. Повернути entities.speaker_id — лише де він і досі NULL (щоб не
        # затерти ручне перепризначення користувача після merge).
        for e in snap.get('entities_relinked', []):
            c.execute(
                'UPDATE entities SET speaker_id = ? WHERE id = ? AND speaker_id IS NULL',
                (e['speaker_id'], e['id']),
            )

        # 4. Відновити embedding/usage_count keep_id зі снапшоту.
        keep_snap = snap.get('keep')
        if keep_snap:
            c.execute(
                'UPDATE speakers SET embedding = ?, usage_count = ?, '
                'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (_b64_blob(keep_snap.get('embedding_b64')), keep_snap.get('usage_count', 0),
                 keep_snap['id']),
            )

        # 5. Позначити снапшот використаним.
        c.execute(
            'UPDATE speaker_merges SET restored_at = ? WHERE id = ?',
            (time.time(), merge_id),
        )
        conn.commit()

        restored_ids = [sp['id'] for sp in snap.get('merged_speakers', [])]

    logger.info(
        f"[unmerge] merge_id={merge_id}: відновлено speakers={restored_ids}, "
        f"keep_id={row['keep_id']}"
    )
    return jsonify({
        'success': True,
        'merge_id': merge_id,
        'restored_speaker_ids': restored_ids,
        'keep_id': row['keep_id'],
    })


# ---------------------------------------------------------------- merge-preview samples (T4.6)

@speakers_bp.route('/api/speakers/<int:speaker_id>/samples', methods=['GET'])
def speaker_samples(speaker_id):
    """Кілька прикладів фраз спікера — легкий merge-preview (T4.6), щоб фронт
    показав приклади ДО об'єднання без важких агрегацій stats/timeline.

    Query: ?limit=N (default 5, max 20).
    Returns: {success, speaker_id, speaker_name, samples: [{transcription_id,
    source_name, start, end, text}]}.
    """
    limit = max(1, min(20, request.args.get('limit', 5, type=int)))
    with _get_db() as conn:
        speaker = conn.execute(
            'SELECT id, name FROM speakers WHERE id = ?', (speaker_id,)
        ).fetchone()
        if not speaker:
            return jsonify({'success': False, 'error': 'Спікер не знайдено'}), 404

        rows = conn.execute('''
            SELECT m.transcription_id, m.raw_label, t.source_name, t.segments
            FROM transcription_speaker_map m
            JOIN transcriptions t ON t.id = m.transcription_id
            WHERE m.speaker_id = ? AND t.deleted_at IS NULL
            ORDER BY t.created_at DESC
        ''', (speaker_id,)).fetchall()

    samples: list[dict] = []
    for r in rows:
        if len(samples) >= limit:
            break
        try:
            segs = json.loads(r['segments']) if r['segments'] else []
        except (json.JSONDecodeError, TypeError):
            continue
        for seg in segs:
            if len(samples) >= limit:
                break
            if seg.get('speaker') != r['raw_label']:
                continue
            text = (seg.get('text') or '').strip()
            if not text:
                continue
            samples.append({
                'transcription_id': r['transcription_id'],
                'source_name': r['source_name'],
                'start': seg.get('start'),
                'end': seg.get('end'),
                'text': text,
            })

    return jsonify({
        'success': True,
        'speaker_id': speaker_id,
        'speaker_name': speaker['name'],
        'samples': samples,
    })
