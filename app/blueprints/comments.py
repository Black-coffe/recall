"""Comments layer: HTTP API (Wave 1).

Endpoints:
- GET    /api/comments?target_type=&target_id=            — comments of one card
- GET    /api/comments/recent?kind=&target_type=&limit=   — feed of the latest
- POST   /api/comments/counts  {target_type, ids: [...]}  — counters for a list
- POST   /api/comments   {target_type, target_id, body, kind?, pinned?, anchor_time?}
- PATCH  /api/comments/<id>    {body?, kind?, pinned?, weight?, anchor_time?}
- DELETE /api/comments/<id>                               — soft-delete
- POST   /api/comments/<id>/restore
- GET    /api/comments/meta                               — kinds, weights, targets (for the UI)

Indexing (chunk + vector) is NOT done in the request: an embedding is a GPU operation,
and a POST from a card must not wait for it. The work goes to `state.job_queue`, and if
there is no queue (tests, CLI context) it runs synchronously, because a comment that was never
indexed is worse than a slow POST.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from app import state
from app.core.i18n import request_lang, tr
from app.db.connection import get_db_connection
from app.services import comments as svc


logger = logging.getLogger(__name__)
comments_bp = Blueprint('comments', __name__)


def _db() -> str:
    return current_app.config['DATABASE']


def _link_graph(comment_id: int, db_path: str) -> None:
    """Local link to the graph (Wave 3, layer A) — free, at ingest.

    In the `finally` of the indexing task, not after it: if chunking fails (OOM
    on the GPU, model failure), the comment must not also drop out of the graph — this is
    exactly the silent hole through which the 4.5.3 pass once "worked" with zero calls
    (memory/offline-pass-not-wired-to-ingest). The link does not need vectors.
    """
    try:
        svc.link_entities_for_comment(db_path, comment_id)
    except Exception:
        logger.warning("[comments] graph for #%s not refreshed", comment_id,
                       exc_info=True)


def _schedule_index(comment_id: int, db_path: str) -> str:
    """Queue indexing; without a queue, do it synchronously.

    A queue error is swallowed with a log, but NOT silently: the comment is already saved and
    visible on the card, just not in search yet — the backfill (`reindex`) will
    pick it up, because `embedded_at` stayed NULL.
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
        logger.warning("[comments] indexing of #%s not queued — left "
                       "for reindex", comment_id, exc_info=True)
        _link_graph(comment_id, db_path)
        return "deferred"


@comments_bp.route('/api/comments/meta', methods=['GET'])
def meta():
    """Reference for the frontend: comment kinds with weights and the list of targets.
    Kept on the server so the UI and ranking do not diverge in the definition of
    weights (the classic trap of a duplicated constant)."""
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
    """`target_id` is deliberately read as a STRING and not cast to int here:
    a recording session is addressed as `rec_<hex>`, and casting in the blueprint would just
    discard it. The key kind is known to the service (`TARGETS`) — one source of truth."""
    target_type = (request.args.get('target_type') or '').strip()
    target_id = request.args.get('target_id')
    if not target_type or not target_id:
        return jsonify({'success': False,
                        'error': tr('knowledge.comments.err.target_required')}), 400
    if target_type not in svc.TARGETS:
        return jsonify({'success': False,
                        'error': tr('knowledge.comments.err.unknown_target', target_type=target_type)}), 400
    try:
        return jsonify({'comments': svc.list_for(_db(), target_type, target_id)})
    except svc.CommentError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@comments_bp.route('/api/comments/recent', methods=['GET'])
def recent():
    """Feed of comments for the "Comments" page — rows + denominator + facets."""
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
    """POST, not GET: Library lists give up to 3000 ids at a time, and such a
    request does not fit in a query string."""
    data = request.get_json(silent=True) or {}
    target_type = (data.get('target_type') or '').strip()
    ids = data.get('ids') or []
    if target_type not in svc.TARGETS:
        return jsonify({'success': False,
                        'error': tr('knowledge.comments.err.unknown_target', target_type=target_type)}), 400
    if not isinstance(ids, list):
        return jsonify({'success': False, 'error': tr('knowledge.comments.err.ids_array')}), 400
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
            # No int(): string targets (a recording session) are cast in the service
            # by key kind from TARGETS.
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
        return jsonify({'success': False, 'error': tr('knowledge.comments.err.target_id_bad')}), 400
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
        return jsonify({'success': False, 'error': tr('knowledge.comments.err.not_found')}), 404
    if not c['indexed']:
        c['index_status'] = _schedule_index(comment_id, _db())
    return jsonify({'success': True, 'comment': c})


@comments_bp.route('/api/comments/<int:comment_id>', methods=['DELETE'])
def delete(comment_id):
    if not svc.delete(_db(), comment_id):
        return jsonify({'success': False, 'error': tr('knowledge.comments.err.not_found')}), 404
    return jsonify({'success': True, 'id': comment_id})


@comments_bp.route('/api/comments/<int:comment_id>/analyze', methods=['POST'])
def analyze(comment_id):
    """Analyze a comment via Claude: tasks + entities (Wave 3, layer B).

    Synchronous on purpose, even though it is a network call: the analysis is started by a
    PERSON with a button on a specific comment and waits for the result to see right away
    what was extracted. A background task would give "it will appear sometime"
    — exactly what you do not want from a paid action. The call is short (input is two
    sentences, effort='low').
    """
    data = request.get_json(silent=True) or {}
    res = svc.analyze(_db(), comment_id,
                      model=data.get('model'),
                      force=bool(data.get('force')))
    status = res.get('status')
    if status == 'not_found':
        return jsonify({'success': False, 'error': tr('knowledge.comments.err.not_found')}), 404
    if status == 'unavailable':
        return jsonify({'success': False, 'result': res,
                        'error': tr('knowledge.comments.err.no_api_key')}), 503
    if status == 'no_anchor':
        # 409, not 400: the request is valid, this card just has nowhere to put
        # a task (a file without a transcript, an entity, a direction).
        return jsonify({'success': False, 'result': res,
                        'error': tr('knowledge.comments.err.no_transcript')}), 409
    if status == 'retry_needed':
        return jsonify({'success': False, 'result': res,
                        'error': tr('knowledge.comments.err.claude_silent')}), 502
    return jsonify({'success': True, 'result': res,
                    'comment': svc.get(_db(), comment_id)})


@comments_bp.route('/api/comments/<int:comment_id>/derived', methods=['GET'])
def derived(comment_id):
    """What this comment has already produced: tasks and graph links.

    A separate endpoint, not a field in `GET /api/comments`: the card's list is read
    on every click on the badge, while derivatives are needed only when the panel is open
    and the comment was really analyzed.
    """
    c = svc.get(_db(), comment_id)
    if not c:
        return jsonify({'success': False, 'error': tr('knowledge.comments.err.not_found')}), 404
    with get_db_connection(_db()) as conn:
        tasks = [dict(r) for r in conn.execute(
            "SELECT id, task, owner_name, due, due_date, status "
            "FROM action_items WHERE comment_id = ? ORDER BY id", (comment_id,))]
    return jsonify({'success': True, 'analyzed': c['analyzed'],
                    'action_items': tasks})


@comments_bp.route('/api/comments/<int:comment_id>/restore', methods=['POST'])
def restore(comment_id):
    if not svc.restore(_db(), comment_id):
        return jsonify({'success': False, 'error': tr('knowledge.comments.err.nothing_to_restore')}), 404
    # A restored comment must return to search too, not only to the card.
    return jsonify({'success': True, 'id': comment_id,
                    'index_status': _schedule_index(comment_id, _db())})
