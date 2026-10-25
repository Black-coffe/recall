"""Mail sync service and poller (gmail-ingest-04).

Pulls mail from every enabled label: first sync SINCE today - MAIL_BACKFILL_MONTHS,
then incremental by UID. Each email is one ``transcriptions`` row (``source_type='email'``),
attachments go through ``document_ingest.ingest_file`` with masking. Flask-free.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime
from typing import Optional

from app import state
from app.core import settings
from app.db.connection import get_db_connection
from app.services import document_ingest, mail_accounts, mail_imap, mail_parse

logger = logging.getLogger(__name__)

NO_PASSWORD = "NO_PASSWORD"

_INSERT_EMAIL = """INSERT INTO transcriptions
    (source_type, source_name, title, transcript_text, category_id, meeting_date,
     mail_account_id, mail_message_id, mail_thread_key, mail_in_reply_to,
     mail_from, mail_to, mail_subject, mail_folder, mail_enrich)
    VALUES ('email', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


# ============================================================
# Dates
# ============================================================

def _months_ago(today: date, months: int) -> date:
    y, m = divmod(today.year * 12 + (today.month - 1) - int(months), 12)
    m += 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    last = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(today.day, last))


# ============================================================
# Persistence
# ============================================================

def _find_email(db_path: str, account_id: int, message_id: str) -> Optional[int]:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM transcriptions WHERE mail_account_id = ? AND mail_message_id = ?",
            (account_id, message_id)).fetchone()
    return row["id"] if row else None


def _insert_email(db_path: str, account_id: int, label: dict, p) -> tuple:
    """INSERT one email. Returns (transcription_id, created). A race on idx_tx_mail_msg
    is a duplicate, not an error."""
    subject = (p.subject or "").strip()
    sender_addr = (p.sender or "").strip()
    with get_db_connection(db_path) as conn:
        try:
            cur = conn.execute(_INSERT_EMAIL, (
                subject or sender_addr, subject or None, p.body, label.get("category_id"),
                p.meeting_date, account_id, p.message_id, p.thread_key, p.in_reply_to,
                p.sender, p.to, p.subject, label["folder"], 1 if label.get("enrich") else 0))
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM transcriptions WHERE mail_account_id = ? AND mail_message_id = ?",
                (account_id, p.message_id)).fetchone()
            if row:
                return row["id"], False
            raise
        tid = cur.lastrowid
        conn.commit()
    return tid, True


def _docs_dir(account_id: int) -> str:
    from config import Config
    base = os.path.abspath(getattr(Config, "DOCUMENTS_FOLDER", "documents"))
    target = os.path.abspath(os.path.join(base, "mail", str(int(account_id))))
    if not target.startswith(base + os.sep):
        raise ValueError("attachment dir escapes the documents folder")
    return target


def _safe_name(original: str) -> str:
    stem, ext = os.path.splitext(original or "")
    ext = re.sub(r"[^a-z0-9.]", "", ext.lower())[:10]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:60] or "attachment"
    return f"{uuid.uuid4().hex[:10]}_{stem}{ext}"


_TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".ini", ".cfg", ".conf", ".env",
             ".yaml", ".yml", ".xml", ".html", ".htm"}


def _is_text_attachment(att) -> bool:
    ctype = (att.content_type or "").lower()
    ext = os.path.splitext(att.filename or "")[1].lower()
    return ctype.startswith("text/") or ext in _TEXT_EXT


def _masked_bytes(att) -> bytes:
    """Bytes to put on disk: text attachments are masked before the write, so the
    original file (served by get_original_file) never carries a secret. Binary ones
    are written and kept as-is, as manual documents are (owner-accepted); only their
    parsed text is masked."""
    if not _is_text_attachment(att):
        return att.data
    data = att.data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1251")
        except UnicodeDecodeError:
            text = data.decode("utf-8", "replace")
    return mail_parse.mask(text).encode("utf-8")


def _submit_embed(db_path: str, tid: int) -> str:
    """Embed-only job (as tg_embed); synchronous when there is no job queue."""
    try:
        from app.services import embeddings
        if not embeddings.is_available():
            return "unavailable"
        if state.job_queue is None:
            embeddings.chunk_and_embed_transcription(db_path, tid)
            return "sync"

        def _job(job, _tid=tid, _db=db_path):
            return embeddings.chunk_and_embed_transcription(_db, _tid)

        state.job_queue.submit("mail_embed", _job, meta={"transcription_id": tid})
        return "queued"
    except Exception as e:  # noqa: BLE001
        logger.warning("[mail] embed for tx=%s not run (continuing): %s", tid, e)
        return "error"


def _ingest_attachments(db_path: str, account_id: int, label: dict, email_tid: int, p) -> dict:
    stats = {"linked": 0, "skipped_big": 0}
    limit = settings.env_int("MAIL_ATTACH_MAX_MB") * 1024 * 1024
    enrich = bool(label.get("enrich"))
    for att in p.attachments:
        if att.inline:
            continue
        if len(att.data) > limit:
            stats["skipped_big"] += 1
            continue
        path = None
        try:
            d = _docs_dir(account_id)
            os.makedirs(d, exist_ok=True)
            path = os.path.abspath(os.path.join(d, _safe_name(att.filename)))
            if not path.startswith(d + os.sep):
                continue
            with open(path, "wb") as f:
                f.write(_masked_bytes(att))
            res = document_ingest.ingest_file(
                db_path, path, original_name=att.filename or os.path.basename(path),
                category_id=label.get("category_id"), enrich=enrich, describe_tables=enrich,
                text_filter=mail_parse.mask)
        except Exception as e:  # noqa: BLE001
            logger.warning("[mail] attachment failed (continuing): %s", type(e).__name__)
            res = {"status": "error", "transcription_id": None}
        status, dtid = res.get("status"), res.get("transcription_id")
        if status in ("created", "duplicate") and dtid:
            with get_db_connection(db_path) as conn:
                if status == "created":
                    conn.execute("UPDATE transcriptions SET mail_enrich = ? WHERE id = ?",
                                 (1 if enrich else 0, dtid))
                conn.execute(
                    "INSERT OR IGNORE INTO mail_attachments "
                    "(email_tid, document_tid, filename, content_type, size_bytes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (email_tid, dtid, att.filename, att.content_type, len(att.data)))
                conn.commit()
            stats["linked"] += 1
            if status == "created" and not enrich:
                _submit_embed(db_path, dtid)
        if status != "created" and path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.debug("[mail] could not remove unused attachment file")
    return stats


def _store_email(db_path: str, account_id: int, label: dict, raw: bytes) -> bool:
    """Parse and persist one message. True when a new row was created."""
    p = mail_parse.parse_message(raw)
    if _find_email(db_path, account_id, p.message_id) is not None:
        return False
    tid, created = _insert_email(db_path, account_id, label, p)
    if not created:
        return False
    try:
        _ingest_attachments(db_path, account_id, label, tid, p)
    except Exception as e:  # noqa: BLE001
        logger.warning("[mail] attachments for tx=%s failed: %s", tid, type(e).__name__)
    _submit_embed(db_path, tid)
    if label.get("enrich"):
        document_ingest.submit_enrichment(tid, db_path)
    return True


# ============================================================
# Sync
# ============================================================

def _fetch_store(db_path: str, imap, account_id: int, label: dict, uids: list) -> tuple:
    """Fetch and store ``uids``. Returns (new_count, smallest UID whose store failed or None):
    the caller keeps its watermark below a failed message so the next poll fetches it again."""
    new, failed = 0, None
    for uid, raw in imap.fetch_raw(uids):
        try:
            if _store_email(db_path, account_id, label, raw):
                new += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[mail] message uid=%s in label %s not stored, retried next poll: %s",
                           uid, label["id"], type(e).__name__)
            failed = uid if failed is None else min(failed, uid)
    return new, failed


def _watermark(uids: list, failed) -> int:
    if failed is not None:
        return int(failed) - 1
    return max(uids) if uids else 0


def _sync_label(db_path: str, imap, account_id: int, label: dict, months: Optional[int],
                today: date, first_months_cap: Optional[int] = None) -> int:
    info = imap.examine(label["folder"])
    uv = info["uidvalidity"]
    synced_since = label.get("synced_since")
    fields: dict = {"uidvalidity": uv}
    first = not synced_since or label.get("last_uid") is None
    reset = (not first) and label.get("uidvalidity") is not None and label["uidvalidity"] != uv
    new = 0

    if first:
        n = months if months is not None else settings.env_int("MAIL_BACKFILL_MONTHS")
        if first_months_cap is not None:
            n = min(n, first_months_cap)
        since = _months_ago(today, n)
        uids = imap.search_since(since)
        got, failed = _fetch_store(db_path, imap, account_id, label, uids)
        new += got
        fields.update(synced_since=since.isoformat(), last_uid=_watermark(uids, failed))
    elif reset:
        uids = imap.search_since(date.fromisoformat(synced_since))
        got, failed = _fetch_store(db_path, imap, account_id, label, uids)
        new += got
        fields["last_uid"] = _watermark(uids, failed)
    elif months is None:
        uids = imap.search_after_uid(label["last_uid"])
        got, failed = _fetch_store(db_path, imap, account_id, label, uids)
        new += got
        if uids:
            fields["last_uid"] = _watermark(uids, failed)

    if months is not None and not first:
        current = date.fromisoformat(synced_since)
        since = _months_ago(today, months)
        if since < current:
            uids = imap.search_since(since, current)
            got, failed = _fetch_store(db_path, imap, account_id, label, uids)
            new += got
            if failed is None:
                fields["synced_since"] = since.isoformat()

    fields.update(last_sync_at=datetime.now().isoformat(timespec="seconds"), last_error_code=None)
    mail_accounts.record_label_sync(db_path, label["id"], **fields)
    return new


def sync_account(db_path: str, account_id: int, months: Optional[int] = None,
                 today: Optional[date] = None, first_months_cap: Optional[int] = None) -> dict:
    """Sync every enabled label of one account. Failures become ``last_error_code``."""
    today = today or date.today()
    labels = mail_accounts.enabled_labels(db_path, account_id)
    result = {"account_id": account_id, "new": 0, "labels": 0, "errors": 0}
    if not labels:
        return result
    address = labels[0]["address"]
    password = mail_accounts.get_password(address)
    if not password:
        mail_accounts.record_account_poll(db_path, account_id, error_code=NO_PASSWORD)
        result["errors"] += 1
        return result
    try:
        imap = mail_imap.connect(address, password)
    except mail_imap.MailError as exc:
        logger.warning("[mail] account %s connect failed: %s", account_id, exc.code)
        mail_accounts.record_account_poll(db_path, account_id, error_code=exc.code)
        result["errors"] += 1
        return result
    try:
        for label in labels:
            try:
                result["new"] += _sync_label(db_path, imap, account_id, label, months, today,
                                                   first_months_cap)
                result["labels"] += 1
            except mail_imap.MailError as exc:
                logger.warning("[mail] label %s failed: %s", label["id"], exc.code)
                mail_accounts.record_label_sync(db_path, label["id"], last_error_code=exc.code)
                result["errors"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mail] label %s failed: %s", label["id"], type(exc).__name__)
                mail_accounts.record_label_sync(db_path, label["id"],
                                                last_error_code=mail_imap.PROTOCOL)
                result["errors"] += 1
    finally:
        imap.close()
    mail_accounts.record_account_poll(db_path, account_id, error_code=None)
    return result


def sync_all(db_path: str, months: Optional[int] = None, today: Optional[date] = None,
             first_months_cap: Optional[int] = None) -> list:
    ids = sorted({lb["account_id"] for lb in mail_accounts.enabled_labels(db_path)})
    out = []
    for aid in ids:
        try:
            out.append(sync_account(db_path, aid, months=months, today=today,
                                    first_months_cap=first_months_cap))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mail] account %s sync crashed: %s", aid, type(exc).__name__)
    return out


# ============================================================
# Poller
# ============================================================

class MailPoller:
    """Daemon thread: polls every MAIL_POLL_MINUTES, wakes on ``trigger()``."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._requests: list = []
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="MailPoller", daemon=True)
        self._thread.start()
        # The start line is how a release proves the poller runs (docs/defects/shipped-but-not-restarted.md).
        logger.info("Mail poller started (every %s min)", max(1, settings.env_int("MAIL_POLL_MINUTES")))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def trigger(self, account_id: Optional[int] = None, months: Optional[int] = None,
                first_months_cap: Optional[int] = None) -> None:
        with self._lock:
            self._requests.append((account_id, months, first_months_cap))
        self._wake.set()

    def run_once(self) -> None:
        """One loop iteration: queued requests, otherwise the periodic poll."""
        with self._lock:
            reqs, self._requests = self._requests, []
        try:
            if reqs:
                for account_id, months, cap in reqs:
                    if account_id is None:
                        sync_all(self.db_path, months=months, first_months_cap=cap)
                    else:
                        sync_account(self.db_path, account_id, months=months,
                                     first_months_cap=cap)
            elif settings.env_bool("MAIL_POLL_ENABLED"):
                sync_all(self.db_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mail] poll iteration failed: %s", type(exc).__name__)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            interval = max(1, settings.env_int("MAIL_POLL_MINUTES")) * 60
            self._wake.wait(interval)
            self._wake.clear()
