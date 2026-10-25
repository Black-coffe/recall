"""Meeting Memory API (Phase 13) - end-to-end entity graph + RAG archive.

Endpoints:
- POST /api/memory/backfill                     - bulk enrichment of history (job + SSE)
- GET  /api/memory/backfill/status              - status of the last backfill
- POST /api/memory/transcriptions/<id>/enrich   - enrich one transcript
- GET  /api/memory/entities                     - entity list (type/q/sort filter)
- GET  /api/memory/entities/<id>                - entity details: meetings + tasks + aliases
- GET  /api/memory/action-items                 - tasks (status/owner filter)
- GET  /api/memory/stats                        - summary archive statistics

Backfill progress SSE channel: "backfill" (via /api/events/<channel> events_bp).
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date as _date, timedelta as _timedelta

from flask import Blueprint, Response, current_app, jsonify, request

from app import state
from app.core import auth
from app.core.i18n import localize_sse, request_lang, tr
from app.services import categories as cats
from app.services import enrichment


logger = logging.getLogger(__name__)
memory_bp = Blueprint('memory', __name__)

# Last backfill status (for GET status; SSE gives live progress).
_backfill_state: dict = {"running": False, "processed": 0, "total": 0,
                         "done": 0, "skipped": 0, "failed": 0}
_backfill_lock = threading.Lock()


def _get_db():
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


def _is_ui() -> bool:
    """The per-boot UI token marks the owner's SPA; everything else is an agent (MCP)."""
    return auth.classify_request()[0] == 'ui'


def _err(code: str, text: str, status: int):
    """Refusal for a non-UI caller: machine `error_code` next to the human text."""
    return jsonify({"success": False, "error": text, "error_code": code}), status


#: Agent caps (ADR-042): a direction name after `clean_name`; the tag cap is `cats.TAG_NAME_MAX`.
AGENT_CATEGORY_NAME_MAX = 60
#: Agent bulk-category ceiling: `ids` only, at most this many.
AGENT_BULK_MAX = 5


def _agent_category_name_error(name):
    if not _is_ui() and len(cats.clean_name(name)) > AGENT_CATEGORY_NAME_MAX:
        return _err('INVALID_ARGUMENT',
                    tr('knowledge.memory.err.category_name_long', max=AGENT_CATEGORY_NAME_MAX), 400)
    return None


def _agent_private_move_error(moves):
    """Non-UI only: 403 when any `(old_cid, new_cid)` move takes a record out of a private
    direction (`privacy.category_change_exposes`), 503 when the check itself fails.
    The whole request is refused before anything is written."""
    if _is_ui():
        return None
    from app.services import privacy
    db = current_app.config['DATABASE']
    try:
        for old, new in moves:
            if privacy.category_change_exposes(db, old, new):
                return _err('PRIVATE_CATEGORY_CHANGE', tr('knowledge.memory.err.private_category_change'), 403)
    except privacy.PrivacyError:
        return _err('PRIVACY_CHECK_FAILED', tr('mail.err.privacyCheck'), 503)
    return None


# Normalisation, filter parsing and the `category_id` writers live in
# `app/services/categories.py` (spec directions-projects-tags, contract C2) -
# one module for all 15 filter places in two processes. What stays here are thin
# wrappers: the names are already imported by outside code (`test_categories.py`), and there
# is no reason to rename them in this story.
_norm_cat = cats.norm_name
#: (conn, cid) -> None. Now it also nulls `audio_downloads.category_id` - before this
#: spec the id of a deleted direction stayed hanging on Media library rows.
_null_category_refs = cats.null_category_refs
#: (conn, src, dst) -> None. It used to return the count of moved rows; now the ids of
#: the moved ones are returned by `cats.merge_categories` - they are what goes to re-embed.
_reassign_category_refs = cats.reassign_category_refs


def _parse_axes(source) -> tuple:
    """C4: (category_id, tags, since, until) from query parameters/a JSON body.

    `source` - anything with `.get(key)` (`request.args` or parsed JSON).
    Raises `ValueError` on a bad value - callers wrap it into a 400 (the same
    approach as `app/blueprints/research.py._parse_axes`). Earlier `category_id`
    here silently fell back to "all directions" - now, like the other axes, it
    leaks to the reader as an error (story 07 closes this debt for search/ask/
    entities/action-items)."""
    return (
        cats.parse_filter(source.get('category_id')),
        cats.parse_tags(source.get('tags')),
        cats.parse_date(source.get('since')),
        cats.parse_date(source.get('until')),
    )


def _check_project(raw) -> "str | None":
    """Text of `project` -> itself; an ambiguous name -> `CategoryError`.

    We resolve BEFORE calling `rag`: in the stream (`ask/stream`) the answer has already gone
    with status 200, and there is no way to say "409, specify the path" from there. We do not
    substitute the value - `project` is also a graph entity and a text mention
    («Ковальчука», a brand), and replacing the text with a node path would break exactly these two
    layers. Here is only a check: a name that sits on two nodes stops the request
    with a list of paths, not with a silent choice of one of them.

    Several names separated by comma/semicolon - the same parsing as in
    `scope.scope_filter_ids`: we check each one. A non-string input (number,
    object) or a list with non-string elements - `CategoryError("bad_name")`
    (minor 11 r0): earlier `str(5)` silently gave `"5"`, and the malformed form
    leaked to `scope_filter_ids`, where `list(5)` is a 500.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        parts = re.split(r"[,;]", text)
    elif isinstance(raw, (list, tuple)) and all(isinstance(x, str) for x in raw):
        text = list(raw)
        parts = text
    else:
        raise cats.CategoryError("bad_name", tr('knowledge.memory.err.project_bad'))
    with _get_db() as conn:
        for part in parts:
            if str(part).strip():
                cats.resolve_node(conn, str(part).strip())
    return text


def _schedule_reembed(ids, reason: str) -> dict:
    """Put the changed records into a targeted re-embed (direction and tags go into the
    chunk context prefix). A scheduler error does not fail the request -
    the edit is already saved, re-embed is secondary.

    `reembed` is imported INSIDE the function: it pulls in `embeddings`, and that pulls
    torch (memory note `mcp-stdio-no-heavy-models`)."""
    if not ids:
        return {"mode": "none", "count": 0, "stale": 0}
    try:
        from app.services import reembed
        return reembed.schedule_records_reembed(
            ids, db_path=current_app.config["DATABASE"], reason=reason)
    except Exception:  # noqa: BLE001 - the change is already in the DB, re-embed is secondary
        logger.exception("[memory] %s: failed to schedule re-embed for %s records",
                         reason, len(ids))
        return {"mode": "none", "count": 0, "stale": 0}


def _parse_explain(raw) -> bool:
    """Story 05 (grep-explainability): query parameter (`?explain=1`) or a field
    of the JSON body (`{"explain": true}`) -> bool. An unknown value ('', '0',
    'false', None) is read as False, not as an error - the default everywhere is
    the compact `why`, not its absence."""
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes")


def _parse_channel(raw) -> tuple:
    """Question channel for `ask_log` -> (channel, error_response|None).

    Empty -> `ui` (the web chat passes nothing). An unknown value is a 400, and
    NOT a quiet default: the channel in the log is an input filter of the golden set, and a silent
    "something unfamiliar = ui" would spoil the sample that a human then labels."""
    from app.services import rag
    if raw is None or raw == "":
        return "ui", None
    channel = str(raw).strip().lower()
    if channel not in rag.ASK_CHANNELS:
        return None, (jsonify({"success": False,
                               "error": tr('knowledge.memory.err.channel', channels='/'.join(rag.ASK_CHANNELS))}), 400)
    return channel, None


# ============================================================
# Directions / categories (Phase 14)
# ============================================================

#: Tree error code -> HTTP status (contract C4).
_CAT_STATUS = {"not_found": 404, "duplicate": 409, "has_projects": 409,
               "ambiguous": 409, "two_levels": 400, "self_parent": 400,
               "empty": 400, "bad_parent": 400, "bad_name": 400,
               "not_suggested": 400}


def _cat_error(exc: cats.CategoryError):
    """Response to an expected tree error.

    `error` stays a human text (the SPA toast shows it) and is duplicated by the
    machine `code`. The exception is `has_projects`: there contract C4 requires exactly the
    code and the list of projects, because the front has something to show ("move them first").

    `duplicate` (v47): the existing keys stay in place, and next to them appear
    `message` (the service text - it names the direction in which the name is taken) and
    `conflict` `{id, path}` - the address of the rival node. Without them a 409 on
    «Маркетинг» is indistinguishable from a 409 on «Маркетинг» in a neighbouring direction.
    `ambiguous` (v47, 409) is arranged the same way as `has_projects`: `error` is the
    machine code, `message` is the service text, `candidates` are the full paths of the nodes
    with this name. An empty response or "the first match" is forbidden here: the client
    must get a LIST and pass one path back.

    We do not touch the front: `settings.js` reads `.message` from story 09."""
    body = {"success": False, "error": str(exc), "code": exc.code}
    if exc.projects:
        body["projects"] = [{"id": p["id"], "name": p["name"]} for p in exc.projects]
    if exc.code == "has_projects":
        body["error"] = "has_projects"
        body["message"] = str(exc)
    if exc.code == "duplicate":
        body["message"] = str(exc)
        body["conflict"] = exc.conflict
    if exc.code == "ambiguous":
        body["error"] = "ambiguous"
        body["message"] = str(exc)
        body["candidates"] = exc.candidates
    return jsonify(body), _CAT_STATUS.get(exc.code, 400)


@memory_bp.route('/api/memory/categories', methods=['GET'])
def list_categories():
    """Direction tree: a direction, right after it - its projects.

    The envelope is the same as it was; each item added `parent_id`, `level`,
    `items`/`items_total`. `count` stays a synonym of `items` - current screens
    (Settings, Research) rely on it, while moving the SPA to the
    tree is stories 09/10."""
    with _get_db() as conn:
        tree = cats.list_tree(conn)
        uncat = conn.execute(
            "SELECT COUNT(*) AS n FROM transcriptions "
            "WHERE category_id IS NULL AND deleted_at IS NULL"
        ).fetchone()["n"]
    for node in tree:
        node["count"] = node["items"]
    return jsonify({"success": True, "categories": tree, "uncategorized": uncat})


def _parse_int_or_none(raw) -> "tuple[int | None, bool]":
    """`raw` -> `(value|None, ok)` - parsing via `int()`, not via
    the check "all characters are digits" (Minor 5, round 3 review): such a check
    tells the truth about "²"/"½" (Unicode digit characters that `int()` will not eat)
    - it accepted them and crashed with a 500 further down the stack instead of a 400 here.

    `None`/empty string -> `(None, True)` - the value is absent, not an error.
    A non-negative integer accepted by `int()` -> `(id, True)`. `bool` is NOT considered
    an integer (checked BEFORE `int()`, because `isinstance(True, int)` is true). The rest
    (`"abc"`, `"²"`, `-1`, an empty `[]`) -> `(None, False)`."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None, True
    if isinstance(raw, bool):
        return None, False
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, False
    return (val, True) if val >= 0 else (None, False)


def _parse_signed_int_or_none(raw) -> "tuple[int | None, bool]":
    """Like `_parse_int_or_none`, but without the non-negative restriction - for
    `sort_order` (manual ordering of nodes, not an id). The narrowing to
    non-negatives (26) hit this field too, although there is no JS sender with a negative
    value - it was not on record; here it is rolled back to the behaviour of
    23 (minor r0 p.5 / r1 p.4, 28): `-5` is accepted again, `"abc"`/`1.5`/
    `bool` are a 400 the same as in `_parse_int_or_none`."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None, True
    if isinstance(raw, bool):
        return None, False
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, False
    return val, True


def _parse_parent_id(raw) -> "tuple[int | None, tuple | None]":
    """`parent_id` of the request body -> `(id|None, error_response|None)`.

    The same parsing and the same error text for POST and PATCH (Major 7):
    `None`/empty string -> no parent (the node becomes a direction); anything else
    that is not a non-negative integer (incl. negative, `bool`, Unicode pseudo-digits) -
    a 400 instead of a quiet fall into "no parent" (or a 500 - Minor 5)."""
    val, ok = _parse_int_or_none(raw)
    if not ok:
        return None, (jsonify({"success": False, "error": tr('knowledge.memory.err.parent_id_bad')}), 400)
    return val, None


@memory_bp.route('/api/memory/categories', methods=['POST'])
def create_category():
    """Create a direction or a project. Body: {name, color?, icon?, parent_id?}.

    `parent_id` - the id of the direction inside which we create a project. Only a
    direction can be the parent (a tree of exactly two levels) - otherwise 400."""
    data = request.get_json(silent=True) or {}
    name = " ".join((data.get("name") or "").strip().split())
    if not name:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.name_empty')}), 400
    err = _agent_category_name_error(name)
    if err:
        return err
    parent_id, err = _parse_parent_id(data.get("parent_id"))
    if err:
        return err
    color = data.get("color", "#6c757d")
    icon = data.get("icon", "fa-folder")
    try:
        with _get_db() as conn:
            node = cats.create_category(conn, name, color=color, icon=icon,
                                        parent_id=parent_id,
                                        created_source=None if _is_ui() else 'mcp')
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "id": node["id"], "name": node["name"],
                    "color": node["color"], "icon": node["icon"],
                    "parent_id": node["parent_id"], "level": node["level"],
                    "category": node})


@memory_bp.route('/api/memory/categories/<int:cid>', methods=['PATCH'])
def update_category(cid: int):
    """Rename / recolour / reparent a node.
    Body: {name?, color?, icon?, sort_order?, parent_id?: int|null}.

    `parent_id: null` makes the node a direction, a number - a project inside a
    direction. The name and path go into the context prefix of the chunks of the whole
    subtree, so after a real change the records are put into a targeted re-embed."""
    data = request.get_json(silent=True) or {}
    kwargs = {}
    if "name" in data and data["name"] is not None:
        if not " ".join(str(data["name"]).strip().split()):
            return jsonify({"success": False, "error": tr('knowledge.memory.err.name_empty')}), 400
        err = _agent_category_name_error(str(data["name"]))
        if err:
            return err
        kwargs["name"] = data["name"]
    for key in ("color", "icon"):
        if key in data and data[key] is not None:
            kwargs[key] = data[key]
    if "sort_order" in data:
        sort_order, ok = _parse_signed_int_or_none(data["sort_order"])
        if not ok:
            return jsonify({"success": False, "error": tr('knowledge.memory.err.sort_order_bad')}), 400
        if sort_order is not None:
            kwargs["sort_order"] = sort_order
    if "parent_id" in data:
        parent_id, err = _parse_parent_id(data["parent_id"])
        if err:
            return err
        kwargs["parent_id"] = parent_id
    if not kwargs:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.nothing_to_update')}), 400
    # An agent may not change a private direction at all (privacy may be keyed by name).
    err = _agent_private_move_error([(cid, None)])
    if err:
        return err

    try:
        with _get_db() as conn:
            node, ids = cats.update_category(conn, cid, **kwargs)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "category": node,
                    "reembed": _schedule_reembed(ids, "category_tree_changed")})


@memory_bp.route('/api/memory/categories/<int:cid>', methods=['DELETE'])
def delete_category(cid: int):
    """Delete a node. Records (transcripts/audio/copilot/tg) become "without
    direction" - the records themselves are NOT deleted.

    A direction with projects is not deleted (409 `has_projects`): quietly lifting the
    projects to the root would scatter the archive layout without a trace."""
    try:
        with _get_db() as conn:
            moved = [r["id"] for r in conn.execute(
                "SELECT id FROM transcriptions WHERE category_id = ?", (cid,)).fetchall()]
            cats.delete_category(conn, cid)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True,
                    "reembed": _schedule_reembed(moved, "category_deleted")})


@memory_bp.route('/api/memory/categories/<int:cid>/merge', methods=['POST'])
def merge_category(cid: int):
    """Move/merge a direction. Body: {target_id: int|null, delete_source: bool}.
    All records of direction cid -> target_id (null = "without direction"). If
    delete_source=true - the direction cid is deleted after the move.

    A node with projects is neither moved nor merged (409 `has_projects`) -
    deal with the projects first."""
    data = request.get_json(silent=True) or {}
    dst, ok = _parse_int_or_none(data.get("target_id"))
    if not ok:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.target_id_bad')}), 400
    delete_source = bool(data.get("delete_source"))
    if dst == cid:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.merge_self')}), 400
    try:
        with _get_db() as conn:
            moved = cats.merge_categories(conn, dst, cid)
            if delete_source:
                cats.delete_category(conn, cid)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "moved": len(moved), "deleted": delete_source,
                    "reembed": _schedule_reembed(moved, "category_merged")})


@memory_bp.route('/api/memory/transcriptions/<int:tid>/category', methods=['PATCH'])
def set_transcription_category(tid: int):
    """Assign a direction to a transcript. Body: {category_id: int|null}."""
    data = request.get_json(silent=True) or {}
    cid, ok = _parse_int_or_none(data.get("category_id"))
    if not ok:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.category_id_bad')}), 400
    with _get_db() as conn:
        if cid is not None and not conn.execute(
                "SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone():
            return jsonify({"success": False, "error": tr('knowledge.memory.err.category_not_found')}), 404
        row = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                           (tid,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": tr('knowledge.memory.err.transcript_not_found')}), 404
        err = _agent_private_move_error([(row["category_id"], cid)])
        if err:
            return err
        changed = cats.set_category(conn, [tid], cid)
    return jsonify({"success": True, "category_id": cid,
                    "reembed": _schedule_reembed(changed, "category_changed")})


@memory_bp.route('/api/memory/transcriptions/<int:tid>/suggest-category', methods=['GET'])
def suggest_transcription_category(tid: int):
    """Direction auto-suggestion via k-NN over labelled transcripts (Phase 15C).
    Returns suggestion=None with a reason if nothing can be suggested (cold start etc.)."""
    from app.services import categorize
    try:
        res = categorize.suggest_category(current_app.config['DATABASE'], tid)
    except Exception as e:
        logger.error("[memory] suggest-category tx=%s failed: %s", tid, e, exc_info=True)
        return jsonify({"success": False, "error": tr('knowledge.memory.err.suggest_failed')}), 500
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/transcriptions/bulk-category', methods=['POST'])
def bulk_set_category():
    """Bulk-assign a direction. Body: {ids: [..], category_id} OR
    {source_type: 'youtube'|'file'|..., category_id} (all without a direction of this type)."""
    data = request.get_json(silent=True) or {}
    cid, ok = _parse_int_or_none(data.get("category_id"))
    if not ok:
        # 400 before any write (Minor 6): earlier a non-integer/unknown
        # `category_id` quietly turned into `None` and stripped the direction from ALL
        # targets instead of rejecting the request.
        return jsonify({"success": False, "error": tr('knowledge.memory.err.category_id_bad')}), 400
    ids = data.get("ids")
    src = data.get("source_type")
    if not _is_ui() and (src is not None or (isinstance(ids, (list, tuple))
                                             and len(ids) > AGENT_BULK_MAX)):
        return _err('BULK_LIMIT', tr('knowledge.memory.err.bulk_limit'), 400)
    if ids is not None:
        # `{"ids": "123"}` - a string is iterable too (by characters!), so without
        # the type check it would quietly fall apart into `[1, 2, 3]`.
        if not isinstance(ids, (list, tuple)):
            return jsonify({"success": False, "error": tr('knowledge.memory.err.ids_bad')}), 400
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": tr('knowledge.memory.err.ids_bad')}), 400
    with _get_db() as conn:
        if ids:
            targets = ids
        elif src:
            targets = [r["id"] for r in conn.execute(
                "SELECT id FROM transcriptions WHERE source_type = ? AND category_id IS NULL",
                (src,)).fetchall()]
        else:
            return jsonify({"success": False, "error": tr('knowledge.memory.err.ids_or_source')}), 400
        if not _is_ui():
            ph = ",".join("?" * len(targets))
            olds = {r[0] for r in conn.execute(
                f"SELECT category_id FROM transcriptions WHERE id IN ({ph})", targets)}
            err = _agent_private_move_error([(old, cid) for old in olds])
            if err:
                return err
        # `updated` now counts records whose direction REALLY changed (not
        # all that fell under the UPDATE): exactly this list goes to re-embed, and
        # a mismatch between "updated" and "re-embedded" would only confuse.
        changed = cats.set_category(conn, targets, cid)
    return jsonify({"success": True, "updated": len(changed),
                    "reembed": _schedule_reembed(changed, "category_bulk")})


# ============================================================
# Tags (07, contract C4 "Tags")
# ============================================================

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _parse_color(raw) -> "tuple[str | None, bool]":
    """`raw` -> `(colour|None, ok)`. `None`/absent -> `(None, True)` - the colour
    is not set, not an error. A literal `#RGB`/`#RGBA`/`#RRGGBB`/`#RRGGBBAA`
    (case-insensitive) -> `(raw, True)`. The rest (arbitrary CSS text that without
    a check landed in `style="--tag-color:..."` - Minor 12 r1) -> `(None,
    False)`."""
    if raw is None:
        return None, True
    if isinstance(raw, str) and _COLOR_RE.match(raw.strip()):
        return raw.strip(), True
    return None, False


def _tag_name_from_body(data, *, required: bool) -> "tuple[str | None, tuple | None]":
    """Extract and validate `name` from the request body. `required=False` allows
    the key to be absent (PATCH without a name change); `''`/spaces and length > 40
    (`cats.TAG_NAME_MAX`) are always a 400, regardless of `required`."""
    if "name" not in data or data["name"] is None:
        if required:
            return None, (jsonify({"success": False, "error": tr('knowledge.memory.err.tag_name_empty')}), 400)
        return None, None
    name = cats.clean_name(data["name"])
    if not name:
        return None, (jsonify({"success": False, "error": tr('knowledge.memory.err.tag_name_empty')}), 400)
    if len(name) > cats.TAG_NAME_MAX:
        return None, (jsonify({
            "success": False,
            "error": tr('knowledge.memory.err.tag_name_long', max=cats.TAG_NAME_MAX)}), 400)
    return name, None


@memory_bp.route('/api/memory/tags', methods=['GET'])
def list_tags():
    """Tag dictionary: {id, name, color, items}, by `name_norm`."""
    with _get_db() as conn:
        return jsonify({"success": True, "tags": cats.list_tags(conn)})


@memory_bp.route('/api/memory/tags', methods=['POST'])
def create_tag():
    """Find/create a dictionary tag. Body: {name, color?}.

    Idempotent by `name_norm` (ADR §B): a repeated POST of the same name
    (with another case/spaces) returns the existing tag with 200 and does not duplicate it -
    201 only for a really new row."""
    data = request.get_json(silent=True) or {}
    name, err = _tag_name_from_body(data, required=True)
    if err:
        return err
    color, ok = _parse_color(data.get("color"))
    if not ok:
        return jsonify({"success": False,
                        "error": tr('knowledge.memory.err.color_bad')}), 400
    with _get_db() as conn:
        existed = conn.execute("SELECT 1 FROM tags WHERE name_norm = ?",
                               (cats.norm_name(name),)).fetchone()
        tag = cats.ensure_tag(conn, name, color=color)
    return jsonify({"success": True, "tag": tag}), (200 if existed else 201)


@memory_bp.route('/api/memory/tags/<int:tag_id>', methods=['PATCH'])
def update_tag_route(tag_id: int):
    """Rename / recolour a tag. Body: {name?, color?}."""
    data = request.get_json(silent=True) or {}
    name, err = _tag_name_from_body(data, required=False)
    if err:
        return err
    kwargs = {}
    if name is not None:
        kwargs["name"] = name
    if "color" in data:
        color, ok = _parse_color(data["color"])
        if not ok:
            return jsonify({"success": False,
                            "error": tr('knowledge.memory.err.color_bad')}), 400
        kwargs["color"] = color
    if not kwargs:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.nothing_to_update')}), 400
    try:
        with _get_db() as conn:
            tag, ids = cats.update_tag(conn, tag_id, **kwargs)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "tag": tag,
                    "reembed": _schedule_reembed(ids, "tag_renamed")})


@memory_bp.route('/api/memory/tags/<int:tag_id>', methods=['DELETE'])
def delete_tag_route(tag_id: int):
    """Delete a tag from the dictionary - cascading removal from `record_tags` and
    `tg_chat_tags` (the schema has no FK, the cascade is done by `cats.delete_tag`)."""
    try:
        with _get_db() as conn:
            ids = cats.delete_tag(conn, tag_id)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "reembed": _schedule_reembed(ids, "tag_deleted")})


@memory_bp.route('/api/memory/tags/merge', methods=['POST'])
def merge_tags_route():
    """Merge two tags. Body: {keep_id, merge_id}. Records of merge_id move
    under keep_id, merge_id itself is deleted from the dictionary."""
    data = request.get_json(silent=True) or {}
    keep_id, keep_ok = _parse_int_or_none(data.get("keep_id"))
    merge_id, merge_ok = _parse_int_or_none(data.get("merge_id"))
    if not keep_ok or not merge_ok or keep_id is None or merge_id is None:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.tag_merge_ids')}), 400
    try:
        with _get_db() as conn:
            ids = cats.merge_tags(conn, keep_id, merge_id)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "moved": len(ids),
                    "reembed": _schedule_reembed(ids, "tags_merged")})


@memory_bp.route('/api/memory/<int:tid>/tags', methods=['PUT'])
def set_record_tags(tid: int):
    """Add/remove tags of a record. Body: {"add": [names], "remove": [names]} -
    at least one key is required. Tags from `add` that are not yet in the dictionary
    are created from the place of use (the same pattern as for a direction)."""
    data = request.get_json(silent=True) or {}
    if "add" not in data and "remove" not in data:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.tag_add_remove')}), 400
    ui = _is_ui()
    try:
        with _get_db() as conn:
            if not conn.execute("SELECT 1 FROM transcriptions WHERE id = ?", (tid,)).fetchone():
                return jsonify({"success": False, "error": tr('knowledge.memory.err.transcript_not_found')}), 404
            if not ui:
                # An agent assigns existing tags only: an unknown name is refused before any
                # write, nothing is created from the place of use.
                for name in cats.parse_tags(data.get("add") or ()):
                    if not conn.execute("SELECT 1 FROM tags WHERE name_norm = ?",
                                        (cats.norm_name(name),)).fetchone():
                        return _err('INVALID_ARGUMENT',
                                    tr('knowledge.memory.err.tag_unknown', name=name), 400)
            changed = cats.set_tags(conn, tid, add=data.get("add") or (),
                                    remove=data.get("remove") or (),
                                    source='manual' if ui else 'mcp')
            tags = cats.tags_for(conn, [tid]).get(tid, [])
            suggested = cats.suggested_tags(conn, tid)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({
        "success": True, "tags": tags, "suggested_tags": suggested,
        "reembed": _schedule_reembed([tid] if changed else [], "record_tags_changed"),
    })


@memory_bp.route('/api/memory/<int:tid>/tags/suggestions', methods=['POST'])
def resolve_tag_suggestions(tid: int):
    """Accept/dismiss Claude's suggestions. Body: {"accept": [names], "dismiss":
    [names]} - at least one non-empty list is required. An accepted one becomes
    a regular tag (`source='claude'`), both lists are removed from
    `suggested_tags_json`."""
    data = request.get_json(silent=True) or {}
    accept = data.get("accept") or []
    dismiss = data.get("dismiss") or []
    if not accept and not dismiss:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.tag_accept_dismiss')}), 400
    try:
        with _get_db() as conn:
            if not conn.execute("SELECT 1 FROM transcriptions WHERE id = ?", (tid,)).fetchone():
                return jsonify({"success": False, "error": tr('knowledge.memory.err.transcript_not_found')}), 404
            changed = cats.resolve_suggestions(conn, tid, accept=accept, dismiss=dismiss)
            tags = cats.tags_for(conn, [tid]).get(tid, [])
            suggested = cats.suggested_tags(conn, tid)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({
        "success": True, "tags": tags, "suggested_tags": suggested,
        "reembed": _schedule_reembed([tid] if changed else [], "tag_suggestions_resolved"),
    })


# ============================================================
# Enrichment
# ============================================================

@memory_bp.route('/api/memory/transcriptions/<int:tid>/enrich', methods=['POST'])
def enrich_one(tid: int):
    """Enrich one transcript (synchronously). Body: {"force": bool, "model": str}."""
    # As in backfill below: the embed phase does not need Claude - see the comment there.
    if not enrichment.any_available():
        return jsonify({"success": False,
                        "error": tr('knowledge.memory.err.no_key_no_embeddings')}), 400
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    model = data.get("model")
    try:
        res = enrichment.enrich_transcription(
            current_app.config['DATABASE'], tid, model=model, force=force,
        )
    except Exception as e:
        logger.error("[memory] enrich tx=%s failed: %s", tid, e, exc_info=True)
        return jsonify({"success": False, "error": tr('knowledge.memory.err.enrich_failed')}), 500
    if res.get("status") == "not_found":
        return jsonify({"success": False, "error": tr('knowledge.memory.err.transcript_not_found')}), 404
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/backfill', methods=['POST'])
def start_backfill():
    """Start bulk enrichment of history in the background. Body: {force, limit, model}."""
    # T6.5: gate on any_available(), not is_available(). The embed phase is local and
    # free, and after an EMBED_VERSION bump it becomes the main work
    # (the whole archive to re-embed). Gating on a Claude key alone would mean:
    # no key -> the indexer forever shows the full archive as work, and starting
    # it from the UI is impossible. Ingest has long gone through any_available()
    # (transcription.py/documents.py/copilot.py) - here was a desync.
    if not enrichment.any_available():
        return jsonify({"success": False,
                        "error": tr('knowledge.memory.err.no_key_no_embeddings')}), 400
    with _backfill_lock:
        if _backfill_state.get("running"):
            return jsonify({"success": False, "error": tr('knowledge.memory.err.backfill_running')}), 409

    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    limit = data.get("limit")
    model = data.get("model")
    effort = data.get("effort", "medium")
    if effort not in ("low", "medium", "high"):
        effort = "medium"
    db_path = current_app.config['DATABASE']

    pending = enrichment.list_unenriched_ids(db_path) if not force else None

    def _job(job):
        with _backfill_lock:
            _backfill_state.update({"running": True, "processed": 0, "total": 0,
                                    "done": 0, "skipped": 0, "failed": 0})

        def _progress(p):
            with _backfill_lock:
                _backfill_state.update(p)
                _backfill_state["running"] = True
            try:
                state.sse_broker.publish("backfill", "progress", p)
            except Exception:
                pass

        try:
            result = enrichment.backfill(
                db_path, model=model, limit=limit, force=force, effort=effort,
                progress_cb=_progress, cancel_cb=job.is_cancelled,
            )
        finally:
            with _backfill_lock:
                _backfill_state["running"] = False
        try:
            state.sse_broker.publish("backfill", "complete", {"status": "completed", **result})
        except Exception:
            pass
        return result

    job = state.job_queue.submit("enrichment_backfill", _job, meta={"kind": "backfill"})
    return jsonify({
        "success": True, "job_id": job.id, "channel": "backfill",
        "pending": len(pending) if pending is not None else None,
    })


@memory_bp.route('/api/memory/backfill/status', methods=['GET'])
def backfill_status():
    with _backfill_lock:
        return jsonify({"success": True, **_backfill_state})


@memory_bp.route('/api/memory/import', methods=['POST'])
def import_archive():
    """Import the meeting_archive corpus. Body: {"path": "<dir>"}.

    Loads .md meetings as transcripts (idempotent). Enrichment (enrich+embed)
    is to be started separately via POST /api/memory/backfill - this is a controlled
    (paid for Claude) step.
    """
    data = request.get_json(silent=True) or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.import_path')}), 400
    if not os.path.isdir(root):
        return jsonify({"success": False, "error": tr('knowledge.memory.err.dir_not_found', path=root)}), 400
    from app.services import archive_import
    res = archive_import.import_directory(current_app.config['DATABASE'], root)
    return jsonify({"success": True, **res})


# ============================================================
# Search (hybrid: vectors + FTS5)
# ============================================================

@memory_bp.route('/api/memory/search', methods=['GET'])
def search():
    """Hybrid semantic search over the archive. Query: q, k (top-k), category_id,
    tags (comma-separated), since, until (contract C4 - an invalid axis -> 400)."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.query_empty')}), 400
    try:
        k = min(max(int(request.args.get('k', 8)), 1), 30)
    except ValueError:
        k = 8
    try:
        category_id, tags, since, until = _parse_axes(request.args)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    from app.services import retrieval
    res = retrieval.search(current_app.config['DATABASE'], q, top_k=k,
                           category_id=category_id, tags=tags, since=since, until=until,
                           explain=_parse_explain(request.args.get('explain')))
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/ask', methods=['POST'])
def ask():
    """RAG "Ask the archive": question -> answer with citations.

    Body: {question, k, model, category_id, tags, since, until, project, channel}.
    `project` (Track 2) narrows the search to records where this project/person is mentioned -
    a category is not enough, because «Фонд Робота» covers more than half of the corpus. `tags`/
    `since`/`until` - contract C4, an invalid axis -> 400. `channel` (`ui`|`mcp`)
    only marks the origin of the question in `ask_log`."""
    if not enrichment.is_available():
        return jsonify({"success": False, "error": tr('knowledge.memory.err.no_api_key')}), 400
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not q:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.question_empty')}), 400
    channel, err = _parse_channel(data.get("channel"))
    if err:
        return err
    try:
        category_id, tags, since, until = _parse_axes(data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    from app.services import rag
    # The default k lives in rag._DEFAULT_TOP_K (single source of truth, env RAG_TOP_K).
    try:
        k = min(max(int(data.get("k", rag._DEFAULT_TOP_K)), 1), 20)
    except (ValueError, TypeError):
        k = rag._DEFAULT_TOP_K
    try:
        project = _check_project(data.get("project"))
    except cats.CategoryError as exc:
        return _cat_error(exc)
    try:
        res = rag.answer_question(current_app.config['DATABASE'], q, top_k=k,
                                  model=data.get("model"),
                                  category_id=category_id,
                                  project=project,
                                  explain=_parse_explain(data.get("explain")),
                                  channel=channel,
                                  tags=tags, since=since, until=until)
    except Exception as e:
        logger.error("[memory] ask failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": tr('knowledge.memory.err.rag_failed')}), 500
    return jsonify({"success": True, **res})


# Status/error texts that `answer_question_stream` streams in Ukrainian.
_ASK_SSE_KEYS = (
    'knowledge.memory.err.question_empty',
    'knowledge.memory.err.rag_failed',
    'knowledge.memory.ans.no_results',
)


@memory_bp.route('/api/memory/ask/stream', methods=['POST'])
def ask_stream():
    """RAG with answer streaming (SSE). Body: {question, k, model, category_id,
    tags, since, until, project}.

    Events: sources (at once) -> delta* (tokens) -> done | error.
    All values from request are read BEFORE the generator (outside the request context).
    """
    if not enrichment.is_available():
        return jsonify({"success": False, "error": tr('knowledge.memory.err.no_api_key')}), 400
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not q:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.question_empty')}), 400
    channel, err = _parse_channel(data.get("channel"))
    if err:
        return err
    try:
        category_id, tags, since, until = _parse_axes(data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    from app.services import rag
    try:
        k = min(max(int(data.get("k", rag._DEFAULT_TOP_K)), 1), 20)
    except (ValueError, TypeError):
        k = rag._DEFAULT_TOP_K
    model = data.get("model")
    # Track 2: slice by project/person. Read HERE, not in the generator - it
    # runs outside the request context (the same reason as for the other fields).
    try:
        project = _check_project(data.get("project"))
    except cats.CategoryError as exc:
        return _cat_error(exc)
    explain = _parse_explain(data.get("explain"))
    db_path = current_app.config['DATABASE']

    lang = request_lang()

    def generate():
        try:
            yield from localize_sse(
                rag.answer_question_stream(db_path, q, top_k=k, model=model,
                                           category_id=category_id, project=project,
                                           explain=explain, channel=channel,
                                           tags=tags, since=since, until=until),
                lang, _ASK_SSE_KEYS)
        except Exception as e:
            logger.error("[memory] ask_stream generator failed: %s", e, exc_info=True)
            yield rag._sse("error", {"error": tr('knowledge.memory.err.rag_stream_failed', lang=lang)})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@memory_bp.route('/api/memory/ask/<int:ask_id>/rate', methods=['POST'])
def rate_ask(ask_id: int):
    """Owner's rating of an answer: body {rating: 1|-1, note?} -> `ask_log`.

    This is the raw material of the golden set (A17): once a week the owner labels a dozen
    questions from the log, thumbs-down go into the set first."""
    data = request.get_json(silent=True) or {}
    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        rating = 0
    if rating not in (1, -1):
        return jsonify({"success": False, "error": tr('knowledge.memory.err.rating_bad')}), 400
    from app.services import rag
    if not rag.rate_ask(current_app.config['DATABASE'], ask_id, rating,
                        data.get("note")):
        return jsonify({"success": False, "error": tr('knowledge.memory.err.question_not_found')}), 404
    return jsonify({"success": True, "ask_id": ask_id})


# ============================================================
# Entities
# ============================================================

@memory_bp.route('/api/memory/entities', methods=['GET'])
def list_entities():
    """Entity list. Query: type, q (search by name/alias), sort, limit,
    offset, category_id, tags, since, until (contract C4 - an invalid axis -> 400)."""
    etype = request.args.get('type')
    q = (request.args.get('q') or '').strip()
    sort = request.args.get('sort', 'mentions')  # mentions | meetings | name | recent
    try:
        limit = min(int(request.args.get('limit', 100)), 500)
        offset = max(int(request.args.get('offset', 0)), 0)
    except ValueError:
        limit, offset = 100, 0

    # Significance threshold: per-meeting LLM extraction breeds one-off entities
    # (~65-83% mentioned in only 1 meeting - noise). By default we hide them
    # (min_meetings=2). An explicit search by name (q) shows rare ones too (=1).
    default_min = 1 if q else 2
    try:
        min_meetings = max(int(request.args.get('min_meetings', default_min)), 1)
    except (ValueError, TypeError):
        min_meetings = default_min

    try:
        category_id, tag_names, since_val, until_val = _parse_axes(request.args)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    where, params = [], []
    if etype in enrichment._VALID_ENTITY_TYPES:
        where.append("e.type = ?")
        params.append(etype)
    if min_meetings > 1:
        where.append("e.meeting_count >= ?")
        params.append(min_meetings)
    if q:
        where.append(
            "(e.normalized_name LIKE ? OR e.id IN "
            "(SELECT entity_id FROM entity_aliases WHERE normalized_alias LIKE ?))"
        )
        like = f"%{enrichment._normalize(q)}%"
        params.extend([like, like])
    # C4: direction (expanded to projects) / tags (OR) / period - own SQL
    # via the same `categories.filter_sql`/`tags_sql`/`date_sql` as everywhere.
    cat_frag, cat_params = cats.filter_sql('t.category_id', category_id)
    tags_frag, tags_params = cats.tags_sql('t.id', tag_names)
    date_frag, date_params = cats.date_sql(
        'COALESCE(t.meeting_date, t.created_at)', since_val, until_val)
    axes_sql = cat_frag + tags_frag + date_frag
    if axes_sql:
        where.append(
            "e.id IN (SELECT me.entity_id FROM meeting_entities me "
            "JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.deleted_at IS NULL" + axes_sql + ")")
        params.extend(cat_params + tags_params + date_params)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    order = {
        "mentions": "e.mention_count DESC",
        "meetings": "e.meeting_count DESC",
        "name": "e.canonical_name COLLATE NOCASE ASC",
        "recent": "e.updated_at DESC",
    }.get(sort, "e.mention_count DESC")

    with _get_db() as conn:
        rows = conn.execute(
            f"SELECT e.id, e.type, e.canonical_name, e.role, e.mention_count, "
            f"e.meeting_count, e.speaker_id FROM entities e {where_sql} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM entities e {where_sql}", params
        ).fetchone()["n"]

    return jsonify({
        "success": True, "total": total, "limit": limit, "offset": offset,
        "min_meetings": min_meetings,
        "entities": [dict(r) for r in rows],
    })


@memory_bp.route('/api/memory/entities/<int:eid>', methods=['GET'])
def entity_detail(eid: int):
    """Entity details: meetings (timeline), tasks as owner, aliases."""
    with _get_db() as conn:
        ent = conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        if not ent:
            return jsonify({"success": False, "error": tr('knowledge.memory.err.entity_not_found')}), 404

        meetings = conn.execute(
            "SELECT t.id, t.source_name, t.source_type, t.created_at, "
            "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date, "
            "me.mention_count, me.role_in_meeting "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ? AND t.deleted_at IS NULL "
            "ORDER BY meeting_date DESC, t.id DESC",
            (eid,),
        ).fetchall()

        actions = conn.execute(
            "SELECT ai.id, ai.task, ai.due, ai.status, ai.transcription_id, "
            "t.source_name FROM action_items ai "
            "LEFT JOIN transcriptions t ON t.id = ai.transcription_id AND t.deleted_at IS NULL "
            "WHERE ai.owner_entity_id = ? ORDER BY ai.id DESC",
            (eid,),
        ).fetchall()

        aliases = conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (eid,),
        ).fetchall()

    return jsonify({
        "success": True,
        "entity": dict(ent),
        "meetings": [dict(r) for r in meetings],
        "action_items": [dict(r) for r in actions],
        "aliases": [r["alias"] for r in aliases],
    })


# ============================================================
# Action items
# ============================================================

# Dashboard deadline windows. Mutually exclusive and exhaustive - each task falls
# into EXACTLY one bucket, otherwise the buckets on /tasks would lie with their sum.
#
# NB: deliberately different from the MCP digest windows (commitments._WINDOWS):
#  1. There `this_week` = Monday...Sunday and therefore INTERSECTS with `overdue`. For
#     the Monday digest this is fine (two separate lists), for the dashboard - no.
#  2. A calendar week degenerates toward its end: on Saturday "this week" is
#     two days, and the bucket is stably empty. So here `soon` is a SLIDING window
#     "the next 7 days from today", equally useful on any day.
_TASK_WINDOWS = ('overdue', 'soon', 'later', 'no_date', 'all')
_SOON_DAYS = 7


def _soon_end_iso(today=None) -> str:
    """ISO date of the end of the "next 7 days" window."""
    return ((today or _date.today()) + _timedelta(days=_SOON_DAYS)).isoformat()


def _task_window_sql(window: str, today=None):
    """(sql_clauses, params) for a dashboard window. Empty for 'all'."""
    t = (today or _date.today()).isoformat()
    if window == 'no_date':
        return ["ai.due_date IS NULL"], []
    if window == 'overdue':
        return ["ai.due_date IS NOT NULL", "ai.due_date < ?"], [t]
    if window == 'soon':
        return ["ai.due_date >= ?", "ai.due_date <= ?"], [t, _soon_end_iso(today)]
    if window == 'later':
        return ["ai.due_date > ?"], [_soon_end_iso(today)]
    return [], []


@memory_bp.route('/api/memory/action-items', methods=['GET'])
def list_action_items():
    """Tasks. Query: status (open|done|cancelled|stale|all), owner_entity_id,
    owner (canonical or raw name), category_id, tags (comma-separated), since,
    until, period (24h|7d|month|year|all), window (overdue|soon|later|no_date|all),
    limit (contract C4 - an invalid axis category_id/tags/since/until -> 400).

    Additionally returns ``windows`` (counters per bucket within the remaining filters)
    and ``owners`` (the owner facet) - so the dashboard shows true sums even
    when the list itself is cut off by the limit."""
    status = request.args.get('status', 'open')
    owner = request.args.get('owner_entity_id')
    owner_name = (request.args.get('owner') or '').strip()
    try:
        limit = min(int(request.args.get('limit', 200)), 1000)
    except ValueError:
        limit = 200

    window = (request.args.get('window') or 'all').strip().lower()
    if window not in _TASK_WINDOWS:
        window = 'all'

    try:
        category_id, tag_names, since_val, until_val = _parse_axes(request.args)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Three INDEPENDENT groups of conditions, each keeping its clauses together with its
    # params. This is not pedantry: below we need three different combinations of the same
    # filters (list / bucket counters / owner facet), and stitching them from
    # one flat list without binding params to conditions is impossible.
    common, common_p = [], []
    if status and status != 'all':
        common.append("ai.status = ?")
        common_p.append(status)
    # T4.6: tasks of a deleted (soft-delete) meeting must not surface
    # on the dashboard - regardless of the category filter below.
    common.append(
        "ai.transcription_id IN (SELECT id FROM transcriptions WHERE deleted_at IS NULL)")
    # Track 1: repeats of one agreement from different meetings are marked dup_of ->
    # by default we show only the canonical one (include_dups=1 to see all).
    if request.args.get('include_dups') not in ('1', 'true', 'yes'):
        common.append("ai.dup_of IS NULL")

    # The period filter - by the date of the source record (meeting_date, otherwise created_at),
    # because the user thinks "tasks of the last N" in terms of when the call was,
    # not when enrichment ran. Whitelisted SQLite modifiers.
    period = (request.args.get('period') or 'all').strip().lower()
    _period_mod = {'24h': '-1 day', '7d': '-7 days', 'month': '-1 month', 'year': '-1 year'}
    if period in _period_mod:
        common.append("date(COALESCE(t.meeting_date, t.created_at)) >= date('now', ?)")
        common_p.append(_period_mod[period])

    # C4: direction (expanded to projects) / tags (OR) / since-until - the same
    # axes as in search/ask/entities, via `categories.filter_sql`/`tags_sql`/
    # `date_sql` on `t` already joined in `_JOINS` (without a separate subquery).
    cat_frag, cat_params = cats.filter_sql('t.category_id', category_id)
    tags_frag, tags_params = cats.tags_sql('t.id', tag_names)
    date_frag, date_params = cats.date_sql(
        'COALESCE(t.meeting_date, t.created_at)', since_val, until_val)
    axes_sql = cat_frag + tags_frag + date_frag
    axes_params = cat_params + tags_params + date_params

    own, own_p = [], []
    if owner:
        own.append("ai.owner_entity_id = ?")
        own_p.append(owner)
    if owner_name:
        # Exact match, not a substring: the value comes from a facet chip (which was
        # built by the same COALESCE), and a substring would make the chip «Андрій» a
        # filter that also pulls in «Андрій Ткаченко».
        #
        # Two branches, because the same name lives in two places:
        #   1) the entity's canonical name (or raw, when there is no link) - this is the chip;
        #   2) graph aliases - after an entity merge (`entity_dedup merge`) all
        #      other spellings of a person move EXACTLY there. For the archive owner
        #      this is 40 variants, and without this branch a link like
        #      `/tasks?owner=Мельник` gave empty with live data.
        #
        # There is deliberately NO third branch - by the raw `ai.owner_name` - although the
        # MCP filter has it. On the dashboard the number on a chip must equal the length of the
        # list, and a raw name breaks that equality: task 2566 sounds like «Юлія»,
        # but is linked to the entity «Юля», i.e. lives under ANOTHER chip - and the list
        # became a row longer than the chip that opened it. For free text
        # there is MCP (`list_action_items`), where a substring is appropriate.
        # Alias comparison is by `normalized_alias`, and the key is computed by THE SAME
        # function that wrote it (`enrichment._normalize`): doing this in
        # SQL via LOWER() is impossible - it does not fold Cyrillic, so
        # case-insensitivity would be a fiction.
        own.append("(COALESCE(e.canonical_name, ai.owner_name) = ? "
                   "OR EXISTS(SELECT 1 FROM entity_aliases ea "
                   "WHERE ea.entity_id = e.id AND ea.normalized_alias = ?))")
        own_p += [owner_name, enrichment._normalize(owner_name)]

    win, win_p = _task_window_sql(window)

    def _sql(*groups):
        # common is always non-empty (deleted_at IS NULL), so `WHERE` is always there
        # BEFORE `axes_sql` is appended here (fragments with their own " AND ").
        cl, pr = [], []
        for c, p in groups:
            cl += c
            pr += p
        base = ("WHERE " + " AND ".join(cl)) if cl else ""
        return base + axes_sql, pr + axes_params

    where_sql, params = _sql((common, common_p), (own, own_p), (win, win_p))
    # Bucket counters - within the remaining filters, but WITHOUT the window: otherwise the active
    # bucket would always show itself as 100% and the rest as zeros.
    count_sql, count_p = _sql((common, common_p), (own, own_p))
    # The owner facet is a PAGE-LEVEL filter, so we count it without the window and without
    # itself: chips must show "how many tasks this person has in total",
    # not "how many of their tasks fell into the bucket that is open now".
    facet_sql, facet_p = _sql((common, common_p))

    _JOINS = ("FROM action_items ai "
              "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
              "LEFT JOIN entities e ON e.id = ai.owner_entity_id ")

    # Overdue - FRESH ON TOP (a task overdue yesterday is still alive; last year's
    # is archaeology). The remaining windows - nearest deadline first, undated at the tail.
    # Tie-break - the conversation date, then id: after Wave 5.1 id no longer matches
    # the chronology (tasks from correspondence were written last, yet speak about
    # May), and on id alone they pushed calls out of the "no date" bucket.
    order_sql = ("ORDER BY ai.due_date DESC, meeting_date DESC, ai.id DESC"
                 if window == 'overdue'
                 else "ORDER BY ai.due_date IS NULL, ai.due_date, meeting_date DESC, ai.id DESC")

    today = _date.today().isoformat()
    soon_end = _soon_end_iso()

    with _get_db() as conn:
        rows = conn.execute(
            f"SELECT ai.id, ai.task, ai.owner_name, ai.owner_entity_id, ai.due, "
            f"ai.due_date, ai.due_precision, ai.stale_at, ai.dup_of, "
            f"ai.status, ai.transcription_id, ai.source, t.source_name, t.category_id, "
            # Wave 5.1: tasks from correspondence appeared in the list, and source_name in
            # TG is the preview of the message itself («[TG] Так, звичайно, плануємо…»),
            # i.e. a caption that repeats the task text. The card needs an address:
            # the chat and the author of the reply.
            f"t.source_type, t.tg_chat_title, t.tg_sender, "
            f"COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date, "
            f"e.canonical_name AS owner_canonical "
            f"{_JOINS}{where_sql} {order_sql} LIMIT ?",
            (*params, limit),
        ).fetchall()

        wc = conn.execute(
            f"SELECT "
            f"SUM(CASE WHEN ai.due_date IS NULL THEN 1 ELSE 0 END) AS no_date, "
            f"SUM(CASE WHEN ai.due_date IS NOT NULL AND ai.due_date < ? THEN 1 ELSE 0 END) AS overdue, "
            f"SUM(CASE WHEN ai.due_date >= ? AND ai.due_date <= ? THEN 1 ELSE 0 END) AS soon, "
            f"SUM(CASE WHEN ai.due_date > ? THEN 1 ELSE 0 END) AS later, "
            f"COUNT(*) AS total "
            f"{_JOINS}{count_sql}",
            (today, today, soon_end, soon_end, *count_p),
        ).fetchone()

        _facet_and = " AND " if facet_sql else "WHERE "
        owners = [dict(r) for r in conn.execute(
            f"SELECT COALESCE(e.canonical_name, ai.owner_name) AS owner, "
            f"MIN(ai.owner_entity_id) AS owner_entity_id, COUNT(*) AS n "
            f"{_JOINS}{facet_sql}{_facet_and}"
            f"COALESCE(e.canonical_name, ai.owner_name) IS NOT NULL "
            f"GROUP BY owner ORDER BY n DESC, owner LIMIT 40",
            facet_p,
        ).fetchall()]

    windows = {k: (wc[k] or 0) for k in ('no_date', 'overdue', 'soon', 'later')}
    windows['all'] = wc['total'] or 0
    return jsonify({
        "success": True,
        "action_items": [dict(r) for r in rows],
        "windows": windows,
        "owners": owners,
        "today": today,
        "soon_end": soon_end,
        "soon_days": _SOON_DAYS,
    })


def _task_action(aid: int, action: str):
    """K5: the same write path as the brief buttons - with an event in the journal.
    The UI writes `source='web'`; an agent `source='mcp'` and a repeated identical action
    is not written again (`created:false`, the existing `event_id`)."""
    from app.services import commitments
    ui = _is_ui()
    try:
        res = commitments.apply_action(current_app.config['DATABASE'], aid, action,
                                       source='web' if ui else 'mcp',
                                       actor='web' if ui else 'mcp', dedupe=not ui)
    except LookupError:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.task_not_found'),
                        "error_code": "NOT_FOUND"}), 404
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/action-items/<int:aid>', methods=['PATCH'])
def update_action_item(aid: int):
    """Update a task status. Body: {"status": "open|done|cancelled"}."""
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('open', 'done', 'cancelled'):
        return jsonify({"success": False, "error": tr('knowledge.memory.err.status_bad')}), 400
    return _task_action(aid, status)


@memory_bp.route('/api/memory/action-items/<int:aid>/postpone', methods=['POST'])
def postpone_action_item(aid: int):
    """Postpone a task. Body: {"days": 1|7|30} -> due_date = today + days, status open."""
    from app.services import commitments
    data = request.get_json(silent=True) or {}
    days = data.get('days')
    action = f"postpone:{days}"
    if not isinstance(days, int) or isinstance(days, bool)             or action not in commitments.POSTPONE_DAYS:
        return _err('INVALID_ARGUMENT',
                    tr('knowledge.memory.err.postpone_days_bad'), 400)
    return _task_action(aid, action)


@memory_bp.route('/api/memory/action-events/<int:eid>/undo', methods=['POST'])
def undo_action_event(eid: int):
    """Undo the last event of a task. An agent undoes only `source='mcp'` events."""
    from app.services import commitments
    ui = _is_ui()
    try:
        res = commitments.undo_action(current_app.config['DATABASE'], eid,
                                      source='web' if ui else 'mcp',
                                      actor='web' if ui else 'mcp',
                                      only_source=None if ui else 'mcp')
    except LookupError as exc:
        return _err('NOT_FOUND', str(exc), 404)
    except PermissionError:
        return _err('NOT_MCP_EVENT', tr('knowledge.memory.err.not_mcp_event'), 403)
    except ValueError:
        return _err('UNDO_NOT_LAST', tr('knowledge.memory.err.undo_not_last'), 409)
    return jsonify({"success": True, "item_id": res["item_id"], "status": res["status"],
                    "due_date": res["due_date"], "undone_event_id": eid})


# ============================================================
# Statistics
# ============================================================

@memory_bp.route('/api/memory/stats', methods=['GET'])
def stats():
    from app.services import embeddings
    with _get_db() as conn:
        by_type = {
            r["type"]: r["n"] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM entities GROUP BY type"
            ).fetchall()
        }
        # Significant = mentioned in >=2 meetings (the graph backbone; the rest is one-off noise).
        by_type_sig = {
            r["type"]: r["n"] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM entities WHERE meeting_count >= 2 GROUP BY type"
            ).fetchall()
        }
        actions = {
            r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM action_items GROUP BY status"
            ).fetchall()
        }
        # T6.5: we count "embedded" with the same predicate as the indexer's work
        # list (enrichment.list_unenriched_ids) - model AND version. Otherwise
        # after an EMBED_VERSION bump one screen would show two numbers that
        # contradict each other: «векторизовано 4502/4503» and «до обробки 4503».
        tx = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END) AS enriched, "
            "SUM(CASE WHEN embedded_at IS NOT NULL AND embedding_model = ? "
            "          AND embedding_version = ? THEN 1 ELSE 0 END) AS embedded "
            "FROM transcriptions WHERE deleted_at IS NULL",
            (embeddings.EMBED_MODEL, embeddings.EMBED_VERSION),
        ).fetchone()
        chunks_total = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    return jsonify({
        "success": True,
        "entities": by_type,
        "entities_significant": by_type_sig,
        "action_items": actions,
        "transcriptions": {
            "total": tx["total"] or 0,
            "enriched": tx["enriched"] or 0,
            "embedded": tx["embedded"] or 0,
        },
        "chunks": chunks_total,
        "enrichment_available": enrichment.is_available(),
        "embeddings_available": embeddings.is_available(),
        "embedding_model": embeddings.EMBED_MODEL,
    })
