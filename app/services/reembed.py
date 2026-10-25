"""Offline re-embed of the archive on a SNAPSHOT (Wave B, story 05, contract C5).

**Why a separate module.** A backfill over HTTP (`enrichment.backfill` ->
`job_queue`) drags in the Claude card, queues and the live application process.
Moving to another (model, version) pair of embeddings is an operation of a different kind:
local, long, no money and no API, and it is done on a SNAPSHOT of the live DB
(`VACUUM INTO`, `scripts/backup_db.ps1`) so that search "before" and "after" can be compared
with the gate (`evals/gate.py`) BEFORE touching the live archive.

**What it does.** Takes records whose pair (`embedding_model`, `embedding_version`)
does not match the current one (`embeddings.EMBED_MODEL`/`EMBED_VERSION`, both from
env, story 04) and re-embeds each through `chunk_and_embed_transcription`,
i.e. with the new cutting, the new model and the chunk context prefix
(`build_context_prefix`) in one pass. At the end `optimize_chunk_index()`:
without it a mass DELETE+INSERT over `chunks` shatters `chunks_fts` into segments and
search degrades 9s -> 64s (measured on T6.5).

**What it does NOT do.** Does not touch comments: they have their own index and their own
tool (`python -m app.services.comments reindex`), and a reminder about it is printed.
Does not run on the live DB without an explicit `--yes-live`.

**Where the pair comes from.** `EMBED_MODEL`/`EMBED_VERSION` are read from the PROCESS
ENVIRONMENT at the import of `embeddings` (story 04), not from the `.env` of this call:
`.env` is loaded by `app.py`, and doing the same here would silently change
constants of another module in every test that imports this one. So the pair for
a run on a snapshot is set by the caller, and the pass PRINTS it at the start and in the summary,
so a mismatch is visible rather than guessed:

    $env:EMBED_MODEL="Qwen/Qwen3-Embedding-0.6B"; $env:EMBED_VERSION="N"

where N is a number STRICTLY above any pair the DB holds (`epoch-check` shows
the list of pairs and the verdict). A number that already exists in the DB would mix two formats
of the context prefix in one epoch, so a pass on such a pair refuses
to start (`embeddings.epoch_status`, ADR-017).

CLI:
    python -m app.services.reembed run --db snapshot.db --dry-run
    python -m app.services.reembed run --db snapshot.db --limit 50
    python -m app.services.reembed epoch-check --db snapshot.db
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from app.db.connection import get_db_connection
from app.services import embeddings
from app.services.recording.state import recording_active as _recording_active
from app.services.enrichment import optimize_chunk_index

logger = logging.getLogger(__name__)


# ============================================================
# Live DB guard (pattern: evals/graph_links.py)
# ============================================================

def _live_db_path() -> Optional[str]:
    """Absolute path of the live DB (`Config.DATABASE`). A separate function: tests
    replace it without touching the real `config.py`/file system."""
    try:
        from config import Config
    except Exception:  # noqa: BLE001 - config may be unavailable in CI/venv without .env
        logger.debug("[reembed] config.Config unavailable; live DB guard disabled",
                     exc_info=True)
        return None
    return str((Config.BASE_DIR / Config.DATABASE).resolve())


def is_live_db(db_path: str) -> bool:
    """Whether `db_path` matches the live DB by absolute path."""
    live = _live_db_path()
    if live is None:
        return False
    return str(Path(db_path).resolve()) == live


# ============================================================
# Selecting stale records
# ============================================================

#: A record goes into the work if its (model, version) pair does not match the
#: current one. `IS NOT ?` instead of `<> ?` because NULL (embedded before the pair
#: was written at all) is also "stale", while `<>` on NULL gives NULL.
#: Deleted records and duplicates are not re-embedded: they are not in search (Wave A), so
#: spending GPU time on them is pointless. Empty text is filtered out here, not by a
#: skipped counter: otherwise such records would stay "stale" forever and
#: a repeated pass would never give zero.
_STALE_WHERE = (
    "deleted_at IS NULL AND duplicate_of IS NULL "
    "AND (embedding_model IS NOT ? OR embedding_version IS NOT ?) "
    "AND (TRIM(COALESCE(polished_text, '')) <> '' "
    "     OR TRIM(COALESCE(transcript_text, '')) <> '')"
)


def _stale_params() -> list:
    return [embeddings.EMBED_MODEL, embeddings.EMBED_VERSION]


def stale_ids(db_path: str, limit: Optional[int] = None) -> list[int]:
    """ids of records whose (model, version) pair does not match the current one."""
    sql = f"SELECT id FROM transcriptions WHERE {_STALE_WHERE} ORDER BY id"
    params = _stale_params()
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with get_db_connection(db_path) as conn:
        return [int(r["id"]) for r in conn.execute(sql, params).fetchall()]


def plan(db_path: str, limit: Optional[int] = None) -> dict:
    """How many records and characters wait for re-embed (for `--dry-run`).

    Characters are the length of the very text that goes into cutting
    (`polished_text`, otherwise `transcript_text`): this is the only estimate of the
    volume of GPU work available in advance; chunks are counted only during the pass."""
    ids = stale_ids(db_path, limit)
    chars = 0
    if ids:
        with get_db_connection(db_path) as conn:
            ph = ",".join("?" * len(ids))
            row = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(COALESCE(NULLIF(polished_text, ''), "
                "transcript_text, ''))), 0) AS n FROM transcriptions "
                f"WHERE id IN ({ph})", ids,
            ).fetchone()
            chars = int(row["n"]) if row else 0
    return {
        "total": len(ids),
        "chars": chars,
        "model": embeddings.EMBED_MODEL,
        "version": embeddings.EMBED_VERSION,
        "dry_run": True,
    }


def run(db_path: str, limit: Optional[int] = None, dry_run: bool = False) -> dict:
    """Re-embed stale records. Idempotent: a repeated run after a
    successful pass finds no work (the pair is saved on every record).

    `optimize_chunk_index` is called EXACTLY once and only if something
    was actually rewritten; on a zero pass it is redundant.

    Story 17: if the current epoch was already built by ANOTHER prefix format, the pass
    does not start at all (even `--dry-run`): on such a pair `stale_ids` would give
    0, and writing new prefixes next to old ones would mix two formats in
    one `retrieval.search` selection."""
    with get_db_connection(db_path) as conn:
        epoch = embeddings.epoch_status(conn)
    if not epoch["ok"]:
        msg = embeddings.epoch_mismatch_message(epoch)
        logger.error("[reembed] pass did not start: %s", msg)
        return {"status": "epoch_mismatch", "reason": epoch["reason"],
                "message": msg, "pair": epoch["pair"], "db_format": epoch["db_format"],
                "total": 0, "done": 0, "skipped": 0, "failed": 0}

    if dry_run:
        return plan(db_path, limit)

    if not embeddings.is_available():
        return {"status": "unavailable", "reason": embeddings.unavailability_reason(),
                "total": 0, "done": 0, "skipped": 0, "failed": 0}

    ids = stale_ids(db_path, limit)
    t0 = time.time()
    done = skipped = failed = 0
    for tid in ids:
        try:
            res = embeddings.chunk_and_embed_transcription(db_path, tid)
        except Exception:  # noqa: BLE001 - one bad record must not fail the pass
            failed += 1
            logger.exception("[reembed] tx=%s: re-embed failed", tid)
            continue
        if res.get("status") == "embedded":
            done += 1
        else:
            skipped += 1
            logger.debug("[reembed] tx=%s skipped: %s", tid, res.get("status"))

    if done:
        optimize_chunk_index(db_path)

    result = {"status": "ok", "total": len(ids), "done": done, "skipped": skipped,
              "failed": failed, "seconds": round(time.time() - t0, 1),
              "optimized": bool(done), "model": embeddings.EMBED_MODEL,
              "version": embeddings.EMBED_VERSION}
    logger.info("[reembed] finished: %s", result)
    return result


# ============================================================
# Single-record re-embed (story 03)
# ============================================================
#
# Why separate from `run`. After an edit of title/description (`PATCH /api/history/<id>`)
# exactly one record goes stale: its (model, version) pair is the same, so
# `stale_ids` does not see it at all, and a pass over the whole archive for one
# record is hours of GPU. Here it is `chunk_and_embed_transcription(force=True)` on
# one id in a background queue; `optimize_chunk_index` is NOT called (a dozen
# DELETE+INSERT do not shatter FTS segmentation, and optimize itself costs minutes).

#: How long to wait before checking again if a live recording is in progress at the moment.
_DEFER_SECONDS = 30.0

#: ids waiting for a window without recording. A set (not a counter) collapses a series of
#: edits of one card into one future task: the user edits the title,
#: then the description, and re-embedding is needed once, with both values.
_pending_lock = threading.Lock()
_pending_ids: set[int] = set()


def _record_reembed_job(job, transcription_id: int, db_path: str) -> dict:
    """Body of the background task. `force=True` is mandatory: the record's `embedded_at`
    is set and the (model, version) pair did not change; without force the pass would consider
    the work done and the new prefix would never reach the vectors.

    The id is removed from `_pending_ids` RIGHT HERE, at the start of the body, before the row is read.
    If the scheduler removed it right after `submit`, a call that managed to
    get `"pending"` would rely on a task that had already read the row with the
    old meta, and its edit would not reach the vectors at all."""
    with _pending_lock:
        _pending_ids.discard(int(transcription_id))
    res = embeddings.chunk_and_embed_transcription(db_path, transcription_id, force=True)
    logger.info("[reembed] single re-embed tx=%s: %s", transcription_id, res.get("status"))
    return res


def _submit_record_reembed(transcription_id: int, db_path: str) -> str:
    """Queue the task or defer it while a recording is in progress."""
    if _recording_active():
        timer = threading.Timer(_DEFER_SECONDS, _retry_record_reembed,
                                args=(transcription_id, db_path))
        timer.daemon = True
        timer.start()
        logger.info("[reembed] tx=%s deferred for %ss: recording in progress",
                    transcription_id, _DEFER_SECONDS)
        return "deferred"

    from app import state
    queue = getattr(state, "job_queue", None)
    if queue is None:
        with _pending_lock:
            _pending_ids.discard(transcription_id)
        logger.info("[reembed] tx=%s not re-embedded: no job queue", transcription_id)
        return "skipped"

    queue.submit("reembed_record", _record_reembed_job, transcription_id, db_path,
                 meta={"transcription_id": transcription_id, "reason": "meta_changed",
                       "db_path": db_path})
    return "queued"


def _retry_record_reembed(transcription_id: int, db_path: str) -> None:
    """Retry from the timer: if the recording is still going, one more timer."""
    try:
        _submit_record_reembed(transcription_id, db_path)
    except Exception:  # noqa: BLE001 - timer in the background, a failure would reach nobody
        with _pending_lock:
            _pending_ids.discard(transcription_id)
        logger.exception("[reembed] tx=%s: retry of single re-embed failed",
                         transcription_id)


def schedule_record_reembed(transcription_id: int, *, db_path: str) -> str:
    """Re-embed the chunks of ONE record in the background. Returns what happened:

    - ``"queued"``    - the task was put into `job_queue`;
    - ``"deferred"``  - a live recording is in progress, retry in `_DEFER_SECONDS`
      (the queue slot is NOT taken meanwhile, otherwise waiting would eat one of the
      two pool workers);
    - ``"pending"``   - this id is already waiting for its window, there will be no second task;
    - ``"skipped"``   - nothing to re-embed with (no torch/model or queue).

    `db_path` is a mandatory keyword WITHOUT a default: the default "`None` = live DB"
    would mean that any call from a test/temporary DB silently rewrites
    the chunks of the live archive. The path always comes explicitly from whoever opened
    the connection (in PATCH, `current_app.config["DATABASE"]`).
    """
    tid = int(transcription_id)
    if not embeddings.is_available():
        logger.info("[reembed] tx=%s not re-embedded: %s", tid,
                    embeddings.unavailability_reason())
        return "skipped"

    if not db_path:
        logger.info("[reembed] tx=%s not re-embedded: unknown DB path", tid)
        return "skipped"
    path = str(db_path)

    with _pending_lock:
        if tid in _pending_ids:
            logger.debug("[reembed] tx=%s is already waiting for re-embed", tid)
            return "pending"
        _pending_ids.add(tid)

    return _submit_record_reembed(tid, path)


# ============================================================
# Single re-embed of a LIST of records (spec directions-projects-tags, C3)
# ============================================================
#
# Direction, project and tags travel into the chunk context prefix just like the title
# and description, so every change of them makes vectors stale. But here the change can be mass:
# bulk direction labelling, reparenting a node with hundreds of records, retroactive
# attaching of a tag to a TG chat. Queueing one task per record in a queue with two
# workers would clog it for an hour; so from _BATCH_THRESHOLD records
# ONE task goes, which at the end runs `optimize_chunk_index` exactly once (a mass
# DELETE+INSERT over chunks shatters chunks_fts; measured 9s -> 64s on T6.5).

#: From how many live ids we switch from N single tasks to one batch task.
_BATCH_THRESHOLD = 20


def _live_and_stale(db_path: str, ids: list[int]) -> tuple[list[int], list[int]]:
    """Split ids into "in the current epoch" and "stale".

    A stale record (another `(model, version)` pair or no chunks at all) does not go
    into single re-embed: the mass `run` pass will rebuild it whole and with the
    new cutting, and a single task would do the same work earlier and for nothing.
    """
    if not ids:
        return [], []
    ph = ",".join("?" * len(ids))
    sql = (f"SELECT id FROM transcriptions WHERE id IN ({ph}) "
           "AND embedding_model IS ? AND embedding_version IS ? "
           "AND EXISTS (SELECT 1 FROM chunks c WHERE c.transcription_id = transcriptions.id)")
    with get_db_connection(db_path) as conn:
        live = {int(r["id"]) for r in conn.execute(
            sql, [*ids, embeddings.EMBED_MODEL, embeddings.EMBED_VERSION]).fetchall()}
    return [i for i in ids if i in live], [i for i in ids if i not in live]


def _batch_reembed_job(job, ids: list[int], db_path: str, reason: str) -> dict:
    """Body of the batch task: re-embed the list and optimise the index once.

    Each id is removed from `_pending_ids` BEFORE its row is read, for the same
    reason as in the single task: a call that got "already scheduled"
    must rely on a task that will read the row AFTER its edit.

    `remaining` are the ids the loop has NOT yet visited; it is computed from the full
    list BEFORE the loop, to survive even an exception from the iteration itself (not from
    `chunk_and_embed_transcription`; that one is caught below and does not stop the batch).
    `finally` releases from `_pending_ids` the tail that was left on `break`
    (cancellation) or on any exception that left the loop; otherwise these ids
    would be stuck in `_pending_ids` until the process restarts and `schedule_record_reembed`
    would return `"pending"` for them forever.

    Story 17 (minor 7): this tail is printed as a `WARNING` with the LIST of ids and the
    reason: cancellation does not put ids back in the queue (cancellation is cancellation),
    so the log is the only trace of what exactly stayed un-re-embedded. The id on which the
    loop stopped (cancellation or a mechanics exception) is put back in this list:
    it is not re-embedded either."""
    done = skipped = failed = 0
    items = list(ids)
    remaining = {int(tid) for tid in items}
    stop_reason = None
    current = None
    try:
        for tid in items:
            tid = int(tid)
            current = tid
            remaining.discard(tid)
            with _pending_lock:
                _pending_ids.discard(tid)
            if job is not None and job.is_cancelled():
                logger.info("[reembed] batch %s cancelled at tx=%s", reason, tid)
                # The id on which cancellation was caught is also NOT processed; it must
                # get into the log together with the rest of the tail (the line above already removed
                # it from `_pending_ids`, a repeated removal is harmless).
                remaining.add(tid)
                stop_reason = "cancelled"
                break
            try:
                res = embeddings.chunk_and_embed_transcription(db_path, tid, force=True)
            except Exception:  # noqa: BLE001 - one bad record does not fail the batch
                failed += 1
                logger.exception("[reembed] batch %s: tx=%s re-embed failed", reason, tid)
                continue
            if res.get("status") == "embedded":
                done += 1
            else:
                skipped += 1
                logger.debug("[reembed] batch %s: tx=%s skipped (%s)", reason, tid,
                             res.get("status"))
    except BaseException as exc:   # noqa: BLE001 - only to name the reason in the log
        stop_reason = type(exc).__name__
        if current is not None:
            remaining.add(current)
        raise
    finally:
        if remaining:
            with _pending_lock:
                _pending_ids.difference_update(remaining)
            logger.warning("[reembed] batch %s: %d ids stayed un-re-embedded "
                           "(%s), not returned to the queue: %s",
                           reason, len(remaining), stop_reason or "unknown",
                           sorted(remaining))
    if done:
        optimize_chunk_index(db_path)
    result = {"status": "ok", "total": len(ids), "done": done, "skipped": skipped,
              "failed": failed, "optimized": bool(done), "reason": reason}
    logger.info("[reembed] batch %s finished: %s", reason, result)
    return result


def _submit_batch_reembed(ids: list[int], db_path: str, reason: str) -> str:
    """Queue the batch or defer it while a live recording is in progress."""
    if _recording_active():
        timer = threading.Timer(_DEFER_SECONDS, _retry_batch_reembed,
                                args=(ids, db_path, reason))
        timer.daemon = True
        timer.start()
        logger.info("[reembed] batch %s (%s records) deferred for %ss: recording in progress",
                    reason, len(ids), _DEFER_SECONDS)
        return "deferred"

    from app import state
    queue = getattr(state, "job_queue", None)
    if queue is None:
        with _pending_lock:
            for tid in ids:
                _pending_ids.discard(int(tid))
        logger.info("[reembed] batch %s not queued: no job queue", reason)
        return "skipped"

    queue.submit("reembed_batch", _batch_reembed_job, list(ids), db_path, reason,
                 meta={"ids": list(ids), "db_path": db_path, "reason": reason})
    return "queued"


def _retry_batch_reembed(ids: list[int], db_path: str, reason: str) -> None:
    try:
        _submit_batch_reembed(ids, db_path, reason)
    except Exception:  # noqa: BLE001 - timer in the background, a failure would reach nobody
        with _pending_lock:
            for tid in ids:
                _pending_ids.discard(int(tid))
        logger.exception("[reembed] batch %s: retry failed", reason)


def schedule_records_reembed(ids, *, db_path: str, reason: str) -> dict:
    """Schedule a single re-embed of a LIST of records.

    Returns `{"mode": "point"|"batch"|"none", "count": ..., "stale": ...}`:

    - ``point`` - <= `_BATCH_THRESHOLD` live ids, one task per record
      (`schedule_record_reembed`, with all its guarantees: dedup by
      `_pending_ids`, deferral during a live recording);
    - ``batch`` - more, one `reembed_batch` task with `optimize_chunk_index`
      at the end;
    - ``none``  - nothing to schedule or nothing to schedule with (empty list, no
      torch/model, unknown `db_path`).

    `stale` is how many ids were filtered out as "not in the current epoch" (the mass
    `run` will take them). `db_path` is a mandatory keyword without a default, for the same reason
    as in `schedule_record_reembed`: the default "live DB" would mean a call from
    a temporary DB rewrites the chunks of the live archive.
    """
    ids = sorted({int(i) for i in (ids or [])})
    if not ids:
        return {"mode": "none", "count": 0, "stale": 0}
    if not embeddings.is_available():
        logger.info("[reembed] %s: %s records not re-embedded (%s)", reason, len(ids),
                    embeddings.unavailability_reason())
        return {"mode": "none", "count": 0, "stale": 0}
    if not db_path:
        logger.info("[reembed] %s: %s records not re-embedded: unknown DB path",
                    reason, len(ids))
        return {"mode": "none", "count": 0, "stale": 0}

    live, stale = _live_and_stale(str(db_path), ids)
    if stale:
        logger.info("[reembed] %s: %s ids outside the current epoch; `run` will take them",
                    reason, len(stale))
    if not live:
        return {"mode": "none", "count": 0, "stale": len(stale)}

    if len(live) <= _BATCH_THRESHOLD:
        for tid in live:
            schedule_record_reembed(tid, db_path=str(db_path))
        return {"mode": "point", "count": len(live), "stale": len(stale)}

    with _pending_lock:
        to_submit = [tid for tid in live if tid not in _pending_ids]
        _pending_ids.update(to_submit)
    if not to_submit:
        # All live ids are already waiting for a single task; a duplicate is not needed, it
        # will read the current row on execution anyway.
        return {"mode": "none", "count": 0, "stale": len(stale)}
    _submit_batch_reembed(to_submit, str(db_path), reason)
    return {"mode": "batch", "count": len(to_submit), "stale": len(stale)}


# ============================================================
# CLI
# ============================================================

def _ro_uri(db_path: str) -> str:
    """URI for `sqlite3.connect(..., uri=True)` in `mode=ro`: SQLite
    physically forbids writing (model - `evals/snapshot.py`)."""
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def epoch_check(db_path: Optional[str] = None) -> int:
    """Step 1 of the migration runbook: whether the current prefix format may be written into the
    current epoch, BEFORE the application restart and WITHOUT any write.

    Prints the archive's `(model, version)` pairs, the DB's memory of each epoch's format
    (`embedding_epochs`, v46), the code's current pair with `PREFIX_FORMAT` and the verdict.
    Exit code: 0 for `ok`/`new_epoch`, 1 for `format_mismatch`, 2 if the DB is not found.
    `--yes-live` is not needed: the connection is opened `mode=ro`.
    """
    path = db_path or _live_db_path()
    if not path:
        print("[reembed] --db not given and the live DB could not be determined",
              file=sys.stderr)
        return 2
    if not os.path.exists(path):
        print(f"[reembed] DB not found: {path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(_ro_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print(f"[epoch-check] DB: {path} (read-only)")
        rows = conn.execute(
            "SELECT embedding_model AS m, embedding_version AS v, COUNT(*) AS n "
            "FROM transcriptions WHERE embedding_model IS NOT NULL "
            "GROUP BY 1, 2 ORDER BY 2, 1").fetchall()
        print("[epoch-check] pairs in transcriptions:"
              + ("" if rows else " none (empty archive)"))
        for r in rows:
            print(f"    {r['m']} / v{r['v']} - {r['n']} records")

        try:
            epochs = conn.execute(
                "SELECT model, version, prefix_format FROM embedding_epochs "
                "ORDER BY version, model").fetchall()
        except sqlite3.OperationalError:
            epochs = None
        if epochs is None:
            print("[epoch-check] embedding_epochs: no table (DB below v46); "
                  "existing pairs count as format 1, as the migration stamps them")
        else:
            print("[epoch-check] embedding_epochs:"
                  + ("" if epochs else " empty"))
            for r in epochs:
                print(f"    {r['model']} / v{r['version']} - format {r['prefix_format']}")

        status = embeddings.epoch_status(conn)
        model, version = status["pair"]
        print(f"[epoch-check] code: {model} / v{version}, "
              f"PREFIX_FORMAT={embeddings.PREFIX_FORMAT}")
        if status["reason"] == "format_mismatch":
            print(f"[epoch-check] VERDICT format_mismatch: "
                  f"{embeddings.epoch_mismatch_message(status)}", file=sys.stderr)
            return 1
        if status["reason"] == "new_epoch":
            print("[epoch-check] VERDICT new_epoch: the epoch does not exist yet; the format is free, "
                  "you can restart and run the pass.")
        else:
            print("[epoch-check] VERDICT ok: the epoch was built with this very format.")
        return 0
    finally:
        conn.close()


def _print_result(res: dict) -> None:
    if res.get("status") == "epoch_mismatch":
        print(f"[reembed] Refusal: {res['message']}", file=sys.stderr)
        return
    if res.get("dry_run"):
        print(f"[reembed] dry-run: {res['total']} records, {res['chars']} characters "
              f"-> pair {res['model']} / v{res['version']}")
        print("[reembed] nothing written.")
        return
    if res.get("status") == "unavailable":
        print(f"[reembed] embeddings unavailable: {res.get('reason')}")
        return
    print(f"[reembed] done: done={res['done']} skipped={res['skipped']} "
          f"failed={res['failed']} in {res['seconds']}s "
          f"({res['model']} / v{res['version']})")
    print(f"[reembed] chunks_fts optimize: {'performed' if res['optimized'] else 'not needed'}")
    print("[reembed] reminder: the comments layer has its own index: "
          "`python -m app.services.comments reindex`")


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        prog="reembed",
        description="Re-embed the archive on a SNAPSHOT under the current pair (EMBED_MODEL, EMBED_VERSION).")
    # --dry-run must work from any place in the command line (as in dedup_audio):
    # a subparser with the same dest would overwrite the parent namespace with its own
    # default False, so the top-level flag goes into a separate dest and the final
    # value is the OR of both.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true",
                   help="Write nothing to the DB: only the count and characters")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", required=True,
                        help="Path to a DB SNAPSHOT (VACUUM INTO - scripts/backup_db.ps1)")
    common.add_argument("--dry-run", action="store_true",
                        help="Write nothing to the DB: only the count and characters")
    common.add_argument("--limit", type=int, default=None,
                        help="Process no more than N records (trial of the pass)")
    common.add_argument("--yes-live", action="store_true",
                        help="Allow a file that matches Config.DATABASE "
                             "(forbidden by default: this pass is for a snapshot)")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", parents=[common],
                   help="Re-embed records whose (model, version) pair is stale")
    # epoch-check does NOT take `common`: it writes nothing (`mode=ro`), so neither
    # --yes-live, nor --limit, nor --dry-run is needed, and --db is optional
    # here: without it we look at the live DB.
    ep = sub.add_parser("epoch-check",
                        help="Whether the current prefix format may be written into the current "
                             "epoch (read-only; step 1 of the migration runbook)")
    ep.add_argument("--db", default=None,
                    help="Path to the DB (default: the live Config.DATABASE)")

    args = p.parse_args(argv)

    if args.command == "epoch-check":
        return epoch_check(args.db)

    args.dry_run = args.dry_run or args.dry_run_pre

    if not os.path.exists(args.db):
        print(f"[reembed] DB not found: {args.db}", file=sys.stderr)
        return 2

    if is_live_db(args.db) and not args.yes_live:
        print(f"[reembed] Refusal: {args.db!r} is the live DB (Config.DATABASE). "
              "The pass rewrites ALL chunks of the archive; do it on a SNAPSHOT "
              "(VACUUM INTO - scripts/backup_db.ps1). If this is a deliberate choice, "
              "add --yes-live.", file=sys.stderr)
        return 2

    print(f"[reembed] current pair: {embeddings.EMBED_MODEL} / v{embeddings.EMBED_VERSION}")
    res = run(args.db, limit=args.limit, dry_run=args.dry_run)
    _print_result(res)
    if res.get("status") == "unavailable":
        return 2
    return 1 if res.get("status") == "epoch_mismatch" else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
