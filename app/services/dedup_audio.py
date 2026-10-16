"""Audio/YouTube/recording duplicates: text hash on ingest + an offline pass (Wave A).

The same call gets into the archive twice (a file re-upload, a repeated
video download, a second transcription of the same recording) - and both copies
then live on as full records: chunked, embedded and competing for
RAG result slots with identical text.

**What the module does:**

* `hash_for(source_type, text)` - sha256 of the normalized text for
  `file`/`youtube`/`recording` (Telegram and documents are NOT touched: documents
  have their own dedup on `content_hash`, in Telegram a duplicate is normal).
* `find_original(...)` - an existing NON-duplicate with the same hash; ingest sets
  `duplicate_of` on the new record and builds no chunks for it.
* `mark_duplicates(...)` - an offline pass over the already accumulated archive: computes
  hashes where missing, groups, keeps the smallest id in a group as the original, sets
  `duplicate_of` on the rest. Idempotent, with `--dry-run`.

A duplicate record is NOT deleted and gets no `deleted_at` - it stays in the
Library marked «дубль #N», together with its comments, file and
tasks. `retrieval.search` removes it from search (the `duplicate_of IS
NULL` filter next to `deleted_at IS NULL`).

CLI:
    python -m app.services.dedup_audio mark --dry-run
    python -m app.services.dedup_audio mark
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Optional

from app.db.connection import get_db_connection
# We do not duplicate the hash and normalization: documents (16E) already have them and in exactly this
# pair - sha256 over the text that the parser has already run through `_normalize_text`.
# Audio has to be normalized explicitly (whisper returns raw text), so
# the private function is imported as is, not rewritten alongside.
from app.services.document_parser import _normalize_text, content_hash

logger = logging.getLogger(__name__)

#: Source types that are deduplicated. Telegram and document are deliberately excluded.
AUDIO_SOURCE_TYPES = ("file", "youtube", "recording")


def hash_for(source_type: Optional[str], text: Optional[str]) -> Optional[str]:
    """sha256 of the normalized text for audio sources. None - if it is not our type
    or the text is empty (emptiness does not make records duplicates of each other)."""
    if source_type not in AUDIO_SOURCE_TYPES:
        return None
    norm = _normalize_text(text or "")
    if not norm:
        return None
    return content_hash(norm)


def find_original(conn, content_hash_value: Optional[str],
                  exclude_id: Optional[int] = None) -> Optional[dict]:
    """The oldest live NON-duplicate with the same hash. None - if there is no duplicate.

    `duplicate_of IS NULL` in the criterion keeps the chain flat: the third copy
    points to the same original as the second, not to the second copy.
    """
    if not content_hash_value:
        return None
    sql = ("SELECT id, source_name FROM transcriptions "
           "WHERE content_hash = ? AND deleted_at IS NULL AND duplicate_of IS NULL "
           f"AND source_type IN ({','.join('?' * len(AUDIO_SOURCE_TYPES))})")
    params: list = [content_hash_value, *AUDIO_SOURCE_TYPES]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    sql += " ORDER BY id LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ============================================================
# Offline pass
# ============================================================

def mark_duplicates(db_path: str, *, dry_run: bool = False) -> dict:
    """Mark duplicates among the already accumulated audio records.

    Hashes are computed on the fly (in the archive before Wave A the `content_hash` column in
    audio is empty) and, unless it is a dry run, written to the DB - otherwise ingest of
    the next copy would again see nothing.
    """
    ph = ",".join("?" * len(AUDIO_SOURCE_TYPES))
    stats = {"dry_run": dry_run, "scanned": 0, "groups": 0, "duplicates": 0,
             "hashes_written": 0, "marked": 0, "groups_detail": []}
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, source_name, source_type, content_hash, duplicate_of, "
            "transcript_text FROM transcriptions "
            f"WHERE source_type IN ({ph}) AND deleted_at IS NULL ORDER BY id",
            list(AUDIO_SOURCE_TYPES),
        ).fetchall()

        by_hash: dict[str, list[dict]] = {}
        hash_fixes: list[tuple] = []
        for r in rows:
            stats["scanned"] += 1
            h = hash_for(r["source_type"], r["transcript_text"])
            if not h:
                continue
            if r["content_hash"] != h:
                hash_fixes.append((h, r["id"]))
            by_hash.setdefault(h, []).append(
                {"id": r["id"], "source_name": r["source_name"],
                 "source_type": r["source_type"], "duplicate_of": r["duplicate_of"]})

        marks: list[tuple] = []
        for h, group in by_hash.items():
            if len(group) < 2:
                continue
            original = group[0]              # smallest id - rows are already ORDER BY id
            dups = group[1:]
            stats["groups"] += 1
            stats["duplicates"] += len(dups)
            todo = [d for d in dups if d["duplicate_of"] != original["id"]]
            marks += [(original["id"], d["id"]) for d in todo]
            stats["groups_detail"].append({
                "hash": h[:12],
                "original": {"id": original["id"], "source_name": original["source_name"]},
                "duplicates": [{"id": d["id"], "source_name": d["source_name"]} for d in dups],
                "count": len(group),
                "to_mark": len(todo),
            })

        stats["hashes_written"] = len(hash_fixes) if not dry_run else 0
        stats["marked"] = len(marks) if not dry_run else 0
        stats["pending_hashes"] = len(hash_fixes)
        stats["pending_marks"] = len(marks)
        if not dry_run and (hash_fixes or marks):
            if hash_fixes:
                conn.executemany(
                    "UPDATE transcriptions SET content_hash = ? WHERE id = ?", hash_fixes)
            if marks:
                conn.executemany(
                    "UPDATE transcriptions SET duplicate_of = ? WHERE id = ?", marks)
            conn.commit()

    logger.info("dedup_audio.mark: scanned=%(scanned)s groups=%(groups)s "
                "duplicates=%(duplicates)s marked=%(marked)s dry_run=%(dry_run)s", stats)
    return stats


# ============================================================
# CLI
# ============================================================

def _print_report(stats: dict) -> None:
    for g in stats["groups_detail"]:
        o = g["original"]
        print(f"[{g['hash']}] original #{o['id']} «{o['source_name']}» "
              f"- copies: {len(g['duplicates'])} (new marks: {g['to_mark']})")
        for d in g["duplicates"]:
            print(f"    duplicate #{d['id']} «{d['source_name']}»")
    summary = {k: v for k, v in stats.items() if k != "groups_detail"}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _cmd(args) -> int:
    if args.command == "mark":
        stats = mark_duplicates(args.db, dry_run=args.dry_run)
    else:  # pragma: no cover
        return 2
    _print_report(stats)
    return 0


def main(argv: Optional[list] = None) -> int:
    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(
        prog="dedup_audio",
        description="Mark duplicate audio/YouTube/recordings (duplicate_of).")
    p.add_argument("--db", default=default_db)
    # --dry-run must work from any place in the line: a subparser with the same
    # dest would overwrite the parent namespace with its default False, so
    # the top-level flag goes to a separate dest, and the final value is the OR of both.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true",
                   help="Do not write anything to the DB")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Do not write anything to the DB")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("mark", parents=[common],
                   help="Groups of identical texts → duplicate_of on the copies")

    args = p.parse_args(argv)
    args.dry_run = args.dry_run or args.dry_run_pre
    return _cmd(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
