"""Шар коментарів — HTTP API (Волна 1).

Endpoints:
- GET    /api/comments?target_type=&target_id=            — коментарі однієї картки
- GET    /api/comments/recent?kind=&target_type=&limit=   — стрічка останніх
- POST   /api/comments/counts  {target_type, ids: [...]}  — лічильники для списку
- POST   /api/comments   {target_type, target_id, body, kind?, pinned?, anchor_time?}
- PATCH  /api/comments/<id>    {body?, kind?, pinned?, weight?, anchor_time?}
- DELETE /api/comments/<id>                               — soft-delete
- POST   /api/comments/<id>/restore
- GET    /api/comments/meta                               — типи, ваги, цілі (для UI)

Індексація (чанк + вектор) НЕ виконується в запиті: ембединг — GPU-операція,
і POST з картки не має чекати на неї. Робота йде в `state.job_queue`, а якщо
черги немає (тести, CLI-контекст) — синхронно, бо коментар, який ніколи не
проіндексувався, гірший за повільний POST.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from app import state
from app.db.connection import get_db_connection
from app.services import comments as svc


logger = logging.getLogger(__name__)
comments_bp = Blueprint('comments', __name__)


def _db() -> str:
    return current_app.config['DATABASE']


def _link_graph(comment_id: int, db_path: str) -> None:
    """Локальний звʼязок із графом (Волна 3, шар А) — безкоштовно, на інжесті.

    У `finally` індексаційної задачі, а не після неї: якщо чанкінг упаде (OOM
    на відеокарті, збій моделі), коментар не має ще й випасти з графа — це
    рівно та тиха діра, через яку прохід 4.5.3 колись «працював» нулем викликів
    (memory/offline-pass-not-wired-to-ingest). Звʼязок векторів не потребує.
    """
    try:
        svc.link_entities_for_comment(db_path, comment_id)
    except Exception:
        logger.warning("[comments] граф для #%s не оновлено", comment_id,
                       exc_info=True)


def _schedule_index(comment_id: int, db_path: str) -> str:
    """Поставити індексацію у чергу; без черги — зробити синхронно.

    Помилку черги ковтаємо з логом, але НЕ мовчки: коментар уже збережений і
    видимий на картці, просто ще не в пошуку — бекфіл (`reindex`) його
    підбере, бо `embedded_at` лишився NULL.
    """
    try:
        if state.job_queue is not None:
            def _job(job, _cid=comment_id, _db=db_path):
                try:
                    return svc.index_comment(_db, _cid)
                finally:
                    _link_graph(_cid, _db)
            state.job_queue.submit("comment_index", _job,
                                   meta={"comment_id": comment_id})
            return "queued"
        try:
            return svc.index_comment(db_path, comment_id).get("status", "unknown")
        finally:
            _link_graph(comment_id, db_path)
    except Exception:
        logger.warning("[comments] індексацію #%s не поставлено — лишається "
                       "для reindex", comment_id, exc_info=True)
        _link_graph(comment_id, db_path)
        return "deferred"


@comments_bp.route('/api/comments/meta', methods=['GET'])
def meta():
    """Довідник для фронтенду: типи коментарів із вагами і список цілей.
    Тримається на сервері, щоб UI і ранжування не розійшлись у визначенні
    ваг (класична пастка дубльованої константи)."""
    return jsonify({
        'kinds': [{'key': k, 'weight': w, 'attaches': k in svc.ATTACH_KINDS}
                  for k, w in sorted(svc.KIND_WEIGHTS.items(),
                                     key=lambda kv: -kv[1])],
        'default_kind': svc.DEFAULT_KIND,
        'targets': sorted(svc.TARGETS),
        'max_body_chars': svc.MAX_BODY_CHARS,
    })


@comments_bp.route('/api/comments', methods=['GET'])
def list_comments():
    """`target_id` навмисно читається як РЯДОК і не приводиться до int тут:
    сесія запису адресується `rec_<hex>`, і приведення в блюпринті просто
    відкидало б її. Вид ключа знає сервіс (`TARGETS`) — одне місце правди."""
    target_type = (request.args.get('target_type') or '').strip()
    target_id = request.args.get('target_id')
    if not target_type or not target_id:
        return jsonify({'success': False,
                        'error': 'потрібні target_type і target_id'}), 400
    if target_type not in svc.TARGETS:
        return jsonify({'success': False,
                        'error': f'невідомий тип цілі: {target_type}'}), 400
    try:
        return jsonify({'comments': svc.list_for(_db(), target_type, target_id)})
    except svc.CommentError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@comments_bp.route('/api/comments/recent', methods=['GET'])
def recent():
    """Стрічка коментарів для сторінки «Коментарі» — рядки + знаменник + фасети."""
    return jsonify(svc.list_recent(
        _db(),
        limit=request.args.get('limit', default=50, type=int),
        offset=request.args.get('offset', default=0, type=int),
        kind=(request.args.get('kind') or None),
        target_type=(request.args.get('target_type') or None),
        since=(request.args.get('since') or None),
        search=((request.args.get('search') or '').strip() or None),
    ))


@comments_bp.route('/api/comments/counts', methods=['POST'])
def counts():
    """POST, а не GET: списки Бібліотеки віддають до 3000 id за раз, і такий
    запит не влазить у query-string."""
    data = request.get_json(silent=True) or {}
    target_type = (data.get('target_type') or '').strip()
    ids = data.get('ids') or []
    if target_type not in svc.TARGETS:
        return jsonify({'success': False,
                        'error': f'невідомий тип цілі: {target_type}'}), 400
    if not isinstance(ids, list):
        return jsonify({'success': False, 'error': 'ids мусить бути масивом'}), 400
    try:
        return jsonify({'counts': svc.counts_for(_db(), target_type, ids[:5000])})
    except svc.CommentError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@comments_bp.route('/api/comments', methods=['POST'])
def create():
    data = request.get_json(silent=True) or {}
    try:
        c = svc.create(
            _db(),
            target_type=(data.get('target_type') or '').strip(),
            # Без int(): рядкові цілі (сесія запису) приводяться в сервісі
            # за видом ключа з TARGETS.
            target_id=data.get('target_id'),
            body=data.get('body') or '',
            kind=(data.get('kind') or svc.DEFAULT_KIND),
            pinned=bool(data.get('pinned')),
            weight=data.get('weight'),
            anchor_time=data.get('anchor_time'),
            anchor_chunk_id=data.get('anchor_chunk_id'),
            author=data.get('author'),
            source=(data.get('source') or 'ui'),
            parent_id=data.get('parent_id'),
        )
    except svc.CommentError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'некоректний target_id'}), 400
    c['index_status'] = _schedule_index(c['id'], _db())
    return jsonify({'success': True, 'comment': c}), 201


@comments_bp.route('/api/comments/<int:comment_id>', methods=['PATCH'])
def update(comment_id):
    data = request.get_json(silent=True) or {}
    try:
        c = svc.update(
            _db(), comment_id,
            body=data.get('body'),
            kind=data.get('kind'),
            pinned=data.get('pinned'),
            weight=data.get('weight'),
            anchor_time=data.get('anchor_time'),
        )
    except svc.CommentError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    if not c:
        return jsonify({'success': False, 'error': 'Коментар не знайдено'}), 404
    if not c['indexed']:
        c['index_status'] = _schedule_index(comment_id, _db())
    return jsonify({'success': True, 'comment': c})


@comments_bp.route('/api/comments/<int:comment_id>', methods=['DELETE'])
def delete(comment_id):
    if not svc.delete(_db(), comment_id):
        return jsonify({'success': False, 'error': 'Коментар не знайдено'}), 404
    return jsonify({'success': True, 'id': comment_id})


@comments_bp.route('/api/comments/<int:comment_id>/analyze', methods=['POST'])
def analyze(comment_id):
    """Розібрати коментар через Claude: задачі + сутності (Волна 3, шар Б).

    Синхронний навмисно, попри те що це мережевий виклик: розбір запускає
    ЛЮДИНА кнопкою на конкретному коментарі й чекає результат, щоб одразу
    побачити, що витягнулось. Фонова задача тут дала б «десь колись зʼявиться»
    — саме те, чого від платної дії не хочеться. Виклик короткий (вхід — два
    речення, effort='low').
    """
    data = request.get_json(silent=True) or {}
    res = svc.analyze(_db(), comment_id,
                      model=data.get('model'),
                      force=bool(data.get('force')))
    status = res.get('status')
    if status == 'not_found':
        return jsonify({'success': False, 'error': 'Коментар не знайдено'}), 404
    if status == 'unavailable':
        return jsonify({'success': False, 'result': res,
                        'error': 'Немає ANTHROPIC_API_KEY — розбір недоступний'}), 503
    if status == 'no_anchor':
        # 409, а не 400: запит коректний, просто цій картці нема куди покласти
        # задачу (файл без транскрипта, сутність, напрямок).
        return jsonify({'success': False, 'result': res,
                        'error': 'Немає транскрипта, до якого прикріпити задачі'}), 409
    if status == 'retry_needed':
        return jsonify({'success': False, 'result': res,
                        'error': 'Claude не відповів — спробуйте ще раз'}), 502
    return jsonify({'success': True, 'result': res,
                    'comment': svc.get(_db(), comment_id)})


@comments_bp.route('/api/comments/<int:comment_id>/derived', methods=['GET'])
def derived(comment_id):
    """Що цей коментар уже породив: задачі й звʼязки графа.

    Окремий ендпоінт, а не поле в `GET /api/comments`: список картки читається
    на кожен клік по бейджу, а похідні потрібні лише коли панель відкрита і
    коментар справді розбирали.
    """
    c = svc.get(_db(), comment_id)
    if not c:
        return jsonify({'success': False, 'error': 'Коментар не знайдено'}), 404
    with get_db_connection(_db()) as conn:
        tasks = [dict(r) for r in conn.execute(
            "SELECT id, task, owner_name, due, due_date, status "
            "FROM action_items WHERE comment_id = ? ORDER BY id", (comment_id,))]
    return jsonify({'success': True, 'analyzed': c['analyzed'],
                    'action_items': tasks})


@comments_bp.route('/api/comments/<int:comment_id>/restore', methods=['POST'])
def restore(comment_id):
    if not svc.restore(_db(), comment_id):
        return jsonify({'success': False, 'error': 'Немає що відновлювати'}), 404
    # Відновлений коментар має повернутись і в пошук, а не лише на картку.
    return jsonify({'success': True, 'id': comment_id,
                    'index_status': _schedule_index(comment_id, _db())})
