"""What the approval dialog says: one summary builder per destructive route, from the DB only.

``build`` reads the object the request names (view args and the JSON body) and returns a
``Summary``. ``None`` is returned only when the object named by the path id does not exist, so
the view answers its own 404 and no window opens. Any other request the builder cannot describe
(a malformed body, an object named in the body that is missing) raises ``Unsummarisable`` and
the gate refuses it without running the view: the view must never act on a body the dialog did
not show. A precondition the view would refuse (a direction with projects, the self speaker)
raises ``Precondition`` carrying the view's own status and body: the gate answers it itself and
never runs the view, so a concurrent change between the check and the view cannot slip a
destructive call past the dialog. Some preconditions hold for non-``ui`` callers only
(``_NON_UI_CHECKS``: a private direction, a job running on the record).
``render`` turns a summary into the dialog text in the owner's language. Text supplied by the
requester (the note, the claimed labels) is only ever quoted; names come from the DB.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from app.core.i18n import tr

logger = logging.getLogger(__name__)

NAME_MAX = 80
VERSIONS_KEPT = 3  # ``transcript_versions.evict``'s ``keep``
TG_BACKFILL_LIMIT_MAX = 2000  # the Telegram view's own bound (the SPA clamps to 20..2000)
_QUOTES = "«»"
_KIND_TABLE = {
    "record": "transcriptions", "records": "transcriptions", "direction": "categories",
    "tag": "tags", "speaker": "speakers", "speakers": "speakers",
    "enrichment": "transcriptions", "enrichment_force": "transcriptions",
    "chat": "tg_monitored_chats", "setting": "env", "mailbox": "mail_accounts", "mailboxes": "mail_accounts",
    "mail_label": "mail_labels",
    "audio": "audio_downloads",
    "folder": "folder", "folder_recursive": "folder",
}


class Unsummarisable(Exception):
    """The request cannot be described in the dialog; the gate refuses it (fail closed)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Precondition(Exception):
    """The view would refuse this request; the gate answers with the view's status and body."""

    def __init__(self, reason: str, status: int, body: dict):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Summary:
    action: str
    kind: str
    name: str
    obj_id: str
    count: int
    undo: bool
    extra: str | None = None


def object_ref(summary: Summary) -> str:
    return f"{_KIND_TABLE.get(summary.kind, summary.kind)}:{summary.obj_id or '*'}"


def _cut(text, limit: int = NAME_MAX) -> str:
    """A dialog label: one line, the dialog's own quote marks stripped, at most ``limit``."""
    text = " ".join(_unquote(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unquote(text) -> str:
    """Requester text without the dialog's own quote marks, so it cannot close its quote."""
    return "".join(ch for ch in str(text or "") if ch not in _QUOTES)


def _positive_limit(raw, bound: int | None) -> int | None:
    """``limit`` from a body: absent -> None; anything but an int in 1..bound raises."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or (bound is not None and raw > bound):
        raise Unsummarisable("bad_limit")
    return raw


def _int_or_none(raw) -> tuple[int | None, bool]:
    """Same reading as the memory blueprint: absent -> (None, True), bad -> (None, False)."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None, True
    if isinstance(raw, bool):
        return None, False
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, False
    return (val, True) if val >= 0 else (None, False)


def _record_name(row) -> str:
    for key in ("title", "source_name"):
        value = row[key]
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"#{row['id']}"


def _segment_count(raw) -> int:
    try:
        segments = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return 0
    return len(segments) if isinstance(segments, list) else 0


def _db_path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


# ------------------------------------------------------------------ builders

def _transcript_delete(conn, va, body):
    tid = va.get("transcription_id")
    row = conn.execute(
        "SELECT id, title, source_name, segments FROM transcriptions "
        "WHERE id = ? AND deleted_at IS NULL", (tid,)).fetchone()
    if row is None:
        return None
    return Summary("transcript_delete", "record", _cut(_record_name(row)), str(row["id"]),
                   _segment_count(row["segments"]), True)


def _transcript_bulk_delete(conn, va, body):
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids:
        raise Unsummarisable("bad_ids")
    try:
        ids = sorted({int(i) for i in ids})
    except (TypeError, ValueError):
        raise Unsummarisable("bad_ids") from None
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, source_name FROM transcriptions WHERE id IN ({marks}) ORDER BY id",
        ids).fetchall()
    if len(rows) != len(ids):
        # Only the records the dialog lists may be deleted: a missing id could be a record
        # ingested while the dialog is open, which nobody approved.
        raise Unsummarisable("not_found")
    names = "; ".join(_record_name(r) for r in rows[:3])
    if len(rows) > 3:
        names += "; …"
    obj = ",".join(str(r["id"]) for r in rows)
    return Summary("transcript_bulk_delete", "records", _cut(names), _cut(obj, 60), len(rows), False)


def _category(conn, cid):
    return conn.execute("SELECT id, name FROM categories WHERE id = ?", (cid,)).fetchone()


def _refuse_if_projects(conn, cid, action_key: str) -> None:
    """The memory view's 409 ``has_projects`` body, raised as a ``Precondition``."""
    from app.services import categories as cats
    try:
        cats._assert_no_projects(conn, cid, tr(action_key))
    except cats.CategoryError as exc:
        raise Precondition("has_projects", 409, {
            "success": False, "error": "has_projects", "code": "has_projects", "message": str(exc),
            "projects": [{"id": p["id"], "name": p["name"]} for p in exc.projects]}) from None


def _category_delete(conn, va, body):
    row = _category(conn, va.get("cid"))
    if row is None:
        return None
    _refuse_if_projects(conn, row["id"], "svc_taxonomy.action.delete_direction")
    count = conn.execute("SELECT COUNT(*) FROM transcriptions WHERE category_id = ?",
                         (row["id"],)).fetchone()[0]
    return Summary("taxonomy_delete", "direction", _cut(row["name"]), str(row["id"]), count, False)


def _category_merge(conn, va, body):
    body = body if isinstance(body, dict) else {}
    dst, ok = _int_or_none(body.get("target_id"))
    if not ok:
        raise Unsummarisable("bad_target")
    src = _category(conn, va.get("cid"))
    if src is None:
        return None
    if dst == src["id"]:
        raise Unsummarisable("merge_self")
    target = None
    if dst is not None:
        target = _category(conn, dst)
        if target is None:
            raise Unsummarisable("not_found")
    _refuse_if_projects(conn, src["id"], "svc_taxonomy.action.merge_direction")
    count = conn.execute("SELECT COUNT(*) FROM transcriptions WHERE category_id = ?",
                         (src["id"],)).fetchone()[0]
    return Summary("taxonomy_merge", "direction", _cut(src["name"]), str(src["id"]), count, False,
                   extra=_cut(target["name"]) if target is not None else None)


def _tag(conn, tag_id):
    return conn.execute("SELECT id, name FROM tags WHERE id = ?", (tag_id,)).fetchone()


def _tag_uses(conn, tag_id) -> int:
    return conn.execute("SELECT COUNT(*) FROM record_tags WHERE tag_id = ?", (tag_id,)).fetchone()[0]


def _tag_delete(conn, va, body):
    row = _tag(conn, va.get("tag_id"))
    if row is None:
        return None
    return Summary("taxonomy_delete", "tag", _cut(row["name"]), str(row["id"]),
                   _tag_uses(conn, row["id"]), False)


def _tag_merge(conn, va, body):
    body = body if isinstance(body, dict) else {}
    keep_id, keep_ok = _int_or_none(body.get("keep_id"))
    merge_id, merge_ok = _int_or_none(body.get("merge_id"))
    if not keep_ok or not merge_ok or keep_id is None or merge_id is None or keep_id == merge_id:
        raise Unsummarisable("bad_ids")
    keep, merge = _tag(conn, keep_id), _tag(conn, merge_id)
    if keep is None or merge is None:
        raise Unsummarisable("not_found")
    return Summary("taxonomy_merge", "tag", _cut(merge["name"]), str(merge["id"]),
                   _tag_uses(conn, merge["id"]), False, extra=_cut(keep["name"]))


def _backfill(conn, va, body):
    body = body if isinstance(body, dict) else {}
    force = bool(body.get("force"))
    model = body.get("model")
    limit = _positive_limit(body.get("limit"), None)
    if force:
        count = conn.execute("SELECT COUNT(*) FROM transcriptions WHERE deleted_at IS NULL").fetchone()[0]
    else:
        from app.services import enrichment
        count = len(enrichment.list_unenriched_ids(_db_path(conn)))
    if limit is not None:
        count = min(count, limit)
    return Summary("enrich_backfill", "enrichment_force" if force else "enrichment", "", "", count,
                   False, extra=_cut(model, 60) if isinstance(model, str) and model.strip() else None)


def _speaker_delete(conn, va, body):
    row = conn.execute("SELECT id, name, is_self FROM speakers WHERE id = ?",
                       (va.get("speaker_id"),)).fetchone()
    if row is None:
        return None
    if row["is_self"]:
        raise Precondition("self_speaker", 400, {
            "success": False, "error": tr("settings.speakers.err.self_delete")})
    count = conn.execute("SELECT COUNT(*) FROM transcription_speaker_map WHERE speaker_id = ?",
                         (row["id"],)).fetchone()[0]
    return Summary("speaker_delete", "speaker", _cut(row["name"]), str(row["id"]), count, False)


def _speaker_merge(conn, va, body):
    body = body if isinstance(body, dict) else {}
    keep_id, merge_ids = body.get("keep_id"), body.get("merge_ids")
    if not isinstance(keep_id, int) or keep_id <= 0:  # mirrors the view (a bool passes there too)
        raise Unsummarisable("bad_ids")
    if not isinstance(merge_ids, list) or not merge_ids:
        raise Unsummarisable("bad_ids")
    try:
        merge_ids = sorted({int(x) for x in merge_ids})
    except (TypeError, ValueError):
        raise Unsummarisable("bad_ids") from None
    if keep_id in merge_ids:
        raise Unsummarisable("merge_self")
    keep = conn.execute("SELECT id, name FROM speakers WHERE id = ?", (keep_id,)).fetchone()
    marks = ",".join("?" * len(merge_ids))
    rows = conn.execute(
        f"SELECT id, name, is_self, usage_count FROM speakers WHERE id IN ({marks}) ORDER BY id",
        merge_ids).fetchall()
    if keep is None or len(rows) != len(merge_ids):
        raise Unsummarisable("not_found")
    if any(r["is_self"] for r in rows):
        raise Precondition("self_speaker", 400, {
            "success": False, "error": tr("settings.speakers.err.self_merge")})
    names = ", ".join(r["name"] for r in rows)
    count = sum(int(r["usage_count"] or 0) for r in rows)
    obj = ",".join(str(r["id"]) for r in rows)
    return Summary("speaker_merge", "speakers" if len(rows) > 1 else "speaker", _cut(names),
                   _cut(obj, 60), count, True, extra=_cut(keep["name"]))


def _telegram_backfill(conn, va, body):
    body = body if isinstance(body, dict) else {}
    chat_id = body.get("chat_id")
    if chat_id is None:
        raise Unsummarisable("bad_chat")
    if "limit" in body and body["limit"] is None:
        raise Unsummarisable("bad_limit")  # the view would pass None on, not its default 200
    limit = _positive_limit(body.get("limit", 200), TG_BACKFILL_LIMIT_MAX)
    # Any other value reaches the listener, so it is always asked about; only an integer id
    # is shown as an id. A chat Recall does not know is shown with the "not verified" mark and
    # its requester text stays inside the quote.
    if isinstance(chat_id, (bool, float)):
        raise Unsummarisable("bad_chat")  # the listener would truncate it to another id
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        cid = None
    row = None
    if cid is not None:
        row = conn.execute("SELECT chat_id, title, username FROM tg_monitored_chats WHERE chat_id = ?",
                           (cid,)).fetchone()
    if row is not None:
        return Summary("telegram_backfill", "chat", _cut(_unquote(row["title"] or row["username"] or cid)),
                       str(cid), limit, False)
    return Summary("telegram_backfill", "chat", _cut(_unquote(chat_id), 60) or "-", "", limit, False,
                   extra="unverified")


def _telegram_repair(conn, va, body):
    """A real repair (gated as ``telegram_backfill`` for a non-ui caller): the chat and the batch
    size the view would fetch. A body the view refuses is answered with the view's own 400."""
    if not isinstance(body, dict):
        raise Unsummarisable("bad_body")
    if body.get("chat_id") is None:
        raise Precondition("bad_chat", 400, {"success": False, "error": tr("telegram.err.chatIdRequired")})
    try:
        cid = int(body["chat_id"])
        count = max(1, min(int(body.get("max_messages", 200)), 500))
    except (TypeError, ValueError):
        raise Precondition("bad_numbers", 400, {
            "success": False, "error": tr("telegram.err.repairNumbers")}) from None
    if not str(cid).startswith("-100"):
        raise Precondition("not_supergroup", 400, {
            "success": False, "error": tr("telegram.err.repairSupergroup")})
    row = conn.execute("SELECT chat_id, title, username FROM tg_monitored_chats WHERE chat_id = ?",
                       (cid,)).fetchone()
    if row is not None:
        return Summary("telegram_backfill", "chat", _cut(_unquote(row["title"] or row["username"] or cid)),
                       str(cid), count, False)
    return Summary("telegram_backfill", "chat", _cut(str(cid), 60), "", count, False, extra="unverified")


def _telegram_enable_chat(conn, va, body):
    """Enable a chat: its title from Recall, else from the listener's dialogs (one 10 s call);
    whether the row is new, and the direction asked for."""
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise Unsummarisable("bad_body")
    category_id = body.get("category_id")
    direction = None
    if category_id is not None:
        if isinstance(category_id, bool) or not isinstance(category_id, int):
            raise Unsummarisable("bad_category")
        direction = _category(conn, category_id)
        if direction is None:
            raise Unsummarisable("not_found")
    chat_id = va.get("chat_id")
    row = conn.execute("SELECT title, username FROM tg_monitored_chats WHERE chat_id = ?",
                       (chat_id,)).fetchone()
    state, name = "known", None
    if row is not None:
        name = row["title"] or row["username"] or str(chat_id)
    else:
        from app.blueprints import telegram as tg
        ok, data = tg._listener_call("/dialogs", timeout=10)
        for d in ((data or {}).get("dialogs") or []) if ok else []:
            if isinstance(d, dict) and d.get("id") == chat_id:
                state, name = "new", d.get("title") or d.get("username") or str(chat_id)
                break
        if name is None:
            raise Unsummarisable("not_found")
    extra = state + ("|" + _cut(direction["name"]) if direction is not None else "")
    return Summary("telegram_enable_chat", "chat", _cut(_unquote(name)), str(chat_id), 0, True, extra=extra)


def _anthropic_key(conn, va, body):
    present = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    return Summary("settings_remove_anthropic_key", "setting", "ANTHROPIC_API_KEY", "", 0, False,
                   extra="yes" if present else "no")


def _mailbox(conn, va, body):
    row = conn.execute("SELECT id, address FROM mail_accounts WHERE id = ?",
                       (va.get("account_id"),)).fetchone()
    if row is None:
        return None
    count = conn.execute("SELECT COUNT(*) FROM mail_labels WHERE account_id = ?",
                         (row["id"],)).fetchone()[0]
    return Summary("mailbox_delete", "mailbox", _cut(row["address"]), str(row["id"]), count, False)


def _mailbox_disconnect(conn, va, body):
    row = conn.execute("SELECT id, address FROM mail_accounts WHERE id = ?",
                       (va.get("account_id"),)).fetchone()
    if row is None:
        return None
    count = conn.execute("SELECT COUNT(*) FROM mail_labels WHERE account_id = ? AND enabled = 1",
                         (row["id"],)).fetchone()[0]
    return Summary("mailbox_disconnect", "mailbox", _cut(row["address"]), str(row["id"]), count, False)


def _mailbox_backfill(conn, va, body):
    # The body decides what runs, so a value the view would not take is refused before the dialog.
    if not isinstance(body, dict):
        raise Unsummarisable("bad_body")
    months = body.get("months")
    if isinstance(months, bool) or not isinstance(months, int) or not 12 < months <= 120:
        raise Unsummarisable("bad_months")
    account_id = body.get("account_id")
    if account_id is None:
        return Summary("mailbox_backfill", "mailboxes", "", "", months, False)
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        raise Unsummarisable("bad_account")
    row = conn.execute("SELECT id, address FROM mail_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise Precondition("account_not_found", 404, {
            "success": False, "error": tr("mail.err.accountNotFound"), "error_code": "NOT_FOUND"})
    return Summary("mailbox_backfill", "mailbox", _cut(row["address"]), str(row["id"]), months, False)


def _mailbox_label_enrich(conn, va, body):
    row = conn.execute("SELECT l.id, l.display_name, l.folder, a.address FROM mail_labels l "
                       "JOIN mail_accounts a ON a.id = l.account_id WHERE l.id = ?",
                       (va.get("label_id"),)).fetchone()
    if row is None:
        return None
    return Summary("mailbox_label_enrich", "mail_label", _cut(row["display_name"] or row["folder"]),
                   str(row["id"]), 0, True, extra=_cut(row["address"]))


def _audio_delete(conn, va, body):
    row = conn.execute("SELECT id, title FROM audio_downloads WHERE id = ? AND deleted_at IS NULL",
                       (va.get("download_id"),)).fetchone()
    if row is None:
        return None
    return Summary("audio_delete", "audio", _cut(row["title"]), str(row["id"]), 1, True)


def _retranscribe_evict(conn, va, body):
    """Ask 9: a non-ui re-run that would drop a version. A body the route would refuse is
    refused here with the route's own answer, before any dialog."""
    from app.services import retranscribe
    from app.services import transcript_versions as tv
    body = body if isinstance(body, dict) else {}
    tid = va.get("transcription_id")
    model, language = body.get("model"), body.get("language")
    try:
        if not isinstance(model, str) or not model.strip():
            raise retranscribe.RetranscribeError("MODEL_NOT_INSTALLED")
        if language is not None and not isinstance(language, str):
            raise Precondition("bad_language", 400, {
                "success": False, "error": tr("transcript.err.bad_language"),
                "error_code": "INVALID_ARGUMENT"})
        retranscribe.precheck(tid, model.strip(), db_path=_db_path(conn))
    except retranscribe.RetranscribeError as exc:
        status, payload = retranscribe.error_body(exc.code, **exc.extra)
        raise Precondition(exc.code.lower(), status, payload) from None
    row = conn.execute("SELECT id, title, source_name FROM transcriptions "
                       "WHERE id = ? AND deleted_at IS NULL", (tid,)).fetchone()
    if row is None:
        return None
    victim = tv.evict_candidate(conn, row["id"], keep=VERSIONS_KEPT)
    if victim is None:
        return None  # nothing would be dropped any more: the view runs, eviction stays refused
    version = f"{victim['model_used'] or '-'}, {victim['created_at'] or '-'}"
    return Summary("transcript_version_evict", "record", _cut(_record_name(row)), str(row["id"]),
                   VERSIONS_KEPT, False, extra=_cut(version, 60))


def _import_folder(conn, va, body):
    """Folder import: the folder (as resolved inside the roots), recursion, file count and the
    direction. A folder the view would refuse (outside the roots, missing, empty) is answered
    with the view's own status and body, and the dialog never opens."""
    from app.blueprints import documents
    if body is not None and not isinstance(body, dict):
        raise Unsummarisable("bad_body")
    plan = documents.import_folder_plan(body or {})
    if "refusal" in plan:
        status, payload = plan["refusal"]
        raise Precondition(str(payload.get("error_code") or "bad_folder").lower(), status, payload)
    direction = None
    if plan["category_id"] is not None:
        direction = _category(conn, plan["category_id"])
        if direction is None:
            raise Unsummarisable("not_found")
    return Summary("document_import_folder", "folder_recursive" if plan["recursive"] else "folder",
                   _cut(plan["folder"], 60), "", len(plan["files"]), False,
                   extra=_cut(direction["name"]) if direction is not None else None)


def _document_reparse(conn, va, body):
    """Re-parse (sync route and job twin): the record and its original file name. A record that
    is not a document is the view's 404; a missing original is the view's 400, before any dialog."""
    from app.blueprints import documents
    from app.services import record_meta
    try:
        row = documents.reparse_target(_db_path(conn), va.get("tid"))
    except documents.ReparseError as exc:
        raise Precondition("no_original", exc.status, exc.body) from None
    if row is None:
        return None
    source = row["original_filename"] or row["source_name"] or os.path.basename(row["file_path"])
    return Summary("document_reparse", "record", _cut(record_meta.display_name(row)), str(row["id"]),
                   1, False, extra=_cut(source, 60))


def _video_describe_claude(conn, va, body):
    """Ask 8: a non-ui request to describe a record's video frames through Claude."""
    from app.services import record_meta, video_analysis
    tid = va.get("tid")
    row = conn.execute("SELECT id, title, source_name FROM transcriptions "
                       "WHERE id = ? AND deleted_at IS NULL", (tid,)).fetchone()
    if row is None:
        return None
    if video_analysis.resolve_recording_video(_db_path(conn), row["id"]) is None:
        raise Precondition("no_video", 200, {
            "success": False, "reason": "no video", "error_code": "NO_VIDEO",
            "error": tr("transcript.err.no_video_in_record", tid=row["id"])})
    return Summary("video_describe_claude", "record", _cut(record_meta.display_name(row)),
                   str(row["id"]), 0, False)


BUILDERS = {
    "transcription.delete_transcription": _transcript_delete,
    "transcription.bulk_delete_transcriptions": _transcript_bulk_delete,
    "memory.delete_category": _category_delete,
    "memory.merge_category": _category_merge,
    "memory.delete_tag_route": _tag_delete,
    "memory.merge_tags_route": _tag_merge,
    "memory.start_backfill": _backfill,
    "speakers.delete_speaker": _speaker_delete,
    "speakers.merge_speakers": _speaker_merge,
    "telegram.tg_backfill": _telegram_backfill,
    "settings_api.clear_anthropic_key": _anthropic_key,
    "mail.mail_accounts_delete": _mailbox,
    "mail.mail_accounts_disconnect": _mailbox_disconnect,
    "mail.mail_backfill": _mailbox_backfill,
    "mail.mail_label_enrich": _mailbox_label_enrich,
    "audio_library.delete_audio_download": _audio_delete,
    "transcription.retranscribe_transcription": _retranscribe_evict,
    "documents.import_folder": _import_folder,
    "documents.reparse_document": _document_reparse,
    "documents.reparse_job": _document_reparse,
    "transcription.analyze_video_endpoint": _video_describe_claude,
    "telegram.tg_enable_chat": _telegram_enable_chat,
    "telegram.tg_repair": _telegram_repair,
}


# ------------------------------------------------------------------ non-ui preconditions

def _refuse_private(db_path: str, old_id, new_id) -> None:
    from app.services import privacy
    try:
        exposes = privacy.category_change_exposes(db_path, old_id, new_id)
    except privacy.PrivacyError:
        raise Precondition("privacy_check_failed", 503, {
            "success": False, "error": tr("approvals.err.privacy_check"),
            "error_code": "PRIVACY_CHECK_FAILED"}) from None
    if exposes:
        raise Precondition("private_category", 403, {
            "success": False, "error": tr("approvals.err.private_category"),
            "error_code": "PRIVATE_CATEGORY_CHANGE"})


def _private_delete(conn, va, body) -> None:
    _refuse_private(_db_path(conn), int(va.get("cid")), None)


def _private_merge(conn, va, body) -> None:
    dst, _ok = _int_or_none((body if isinstance(body, dict) else {}).get("target_id"))
    _refuse_private(_db_path(conn), int(va.get("cid")), dst)


def _job_active(conn, va, body) -> None:
    from app.services import job_queue
    job_id = job_queue.active_job_for_transcription(va.get("transcription_id"))
    if job_id:
        raise Precondition("job_active", 409, {
            "success": False, "error": tr("transcript.err.job_active"), "error_code": "JOB_ACTIVE",
            "job_id": job_id})


_NON_UI_CHECKS = {
    "memory.delete_category": _private_delete,
    "memory.merge_category": _private_merge,
    "transcription.delete_transcription": _job_active,
}


def build(action: str, endpoint: str, conn, view_args: dict, body: dict | None, *,
          actor: str) -> Summary | None:
    """Summary of what the request would do; None only when the path's object is missing.

    Raises ``Unsummarisable`` for every other request the dialog cannot describe, and
    ``Precondition`` for one the view (or, for a non-``ui`` ``actor``, this spec) refuses.
    """
    builder = BUILDERS[endpoint]
    summary = builder(conn, view_args or {}, body)
    if summary is not None and summary.action != action:
        raise ValueError(f"approval summary action mismatch: {endpoint} {summary.action} != {action}")
    check = _NON_UI_CHECKS.get(endpoint)
    if summary is not None and check is not None and actor != "ui":
        check(conn, view_args or {}, body)
    return summary


# ------------------------------------------------------------------ rendering

def _object_line(s: Summary, lang: str) -> str:
    kind = tr(f"approvals.kind.{s.kind}", lang=lang)
    if s.kind == "chat" and s.extra == "unverified":
        return tr("approvals.object_unverified", lang=lang, kind=kind, name=s.name)
    if not s.name:
        return tr("approvals.object_bare", lang=lang, kind=kind)
    if not s.obj_id:
        return tr("approvals.object_noid", lang=lang, kind=kind, name=s.name)
    return tr("approvals.object", lang=lang, kind=kind, name=s.name, id=s.obj_id)


def _extra_line(s: Summary, lang: str) -> str | None:
    if s.action in ("taxonomy_merge", "speaker_merge"):
        if s.extra is None:
            return tr("approvals.extra.merge_into_none", lang=lang)
        return tr("approvals.extra.merge_into", lang=lang, name=s.extra)
    if s.action == "enrich_backfill":
        if s.extra is None:
            return tr("approvals.extra.model_default", lang=lang)
        return tr("approvals.extra.model", lang=lang, model=s.extra)
    if s.action == "transcript_version_evict":
        return tr("approvals.extra.version_drop", lang=lang, version=s.extra)
    if s.action == "mailbox_label_enrich":
        return tr("approvals.extra.mailbox", lang=lang, name=s.extra)
    if s.action == "document_import_folder":
        if s.extra is None:
            return tr("approvals.extra.direction_none", lang=lang)
        return tr("approvals.extra.direction", lang=lang, name=s.extra)
    if s.action == "document_reparse":
        return tr("approvals.extra.source_file", lang=lang, name=s.extra)
    if s.action == "telegram_enable_chat":
        state, _, direction = (s.extra or "known").partition("|")
        line = tr("approvals.extra.tg_chat_new" if state == "new" else "approvals.extra.tg_chat_known", lang=lang)
        if direction:
            line += " " + tr("approvals.extra.direction", lang=lang, name=direction)
        return line
    if s.action == "settings_remove_anthropic_key":
        return tr("approvals.extra.key_present" if s.extra == "yes" else "approvals.extra.key_absent",
                  lang=lang)
    return None


def who_lines(*, actor: str, claimed_actor: str | None, claimed_tool: str | None, lang: str) -> list[str]:
    if actor == "ui":
        return [tr("approvals.who.ui", lang=lang)]
    lines = [tr("approvals.who.unknown", lang=lang)]
    if claimed_actor or claimed_tool:
        lines.append(tr("approvals.who.claims", lang=lang,
                        actor=claimed_actor or "-", tool=claimed_tool or "-"))
    return lines


def render(summary: Summary, *, actor: str, claimed_actor: str | None, claimed_tool: str | None,
           note: str | None, lang: str, timeout_s: float = 45.0) -> str:
    """Dialog text: who asks first, then verb, object, impact, undo, note, deadline, question."""
    lines = who_lines(actor=actor, claimed_actor=claimed_actor, claimed_tool=claimed_tool, lang=lang)
    lines.append("")
    lines.append(tr(f"approvals.action.{summary.action}", lang=lang))
    lines.append(_object_line(summary, lang))
    extra = _extra_line(summary, lang)
    if extra:
        lines.append(extra)
    lines.append(tr(f"approvals.impact.{summary.action}", lang=lang, count=summary.count))
    lines.append(tr("approvals.undo.yes" if summary.undo else "approvals.undo.no", lang=lang))
    if note:
        lines.append(tr("approvals.note", lang=lang, note=note))
    lines.append("")
    lines.append(tr("approvals.deadline", lang=lang, seconds=int(timeout_s)))
    lines.append(tr("approvals.question", lang=lang))
    return "\n".join(lines)


def one_line(summary: Summary, lang: str) -> str:
    """Short audit form: verb, object, impact."""
    return " ".join([
        tr(f"approvals.action.{summary.action}", lang=lang),
        _object_line(summary, lang) + ".",
        tr(f"approvals.impact.{summary.action}", lang=lang, count=summary.count),
    ])
