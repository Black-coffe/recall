"""Re-read with OCR the Telegram photos that were left without text (spec tg-photo-ocr).

Before 4.10.0 OCR never worked in Recall: Tesseract was not installed, and since 14.08 even
``ocr_available()`` crashed on an unimported helper. Photo ingest (``_parse_media``)
silently stored the stub «[фото без тексту]» or only the caption. This pass re-reads
the originals from disk with the same ``document_parser`` and drives the record through the same path as
ingest: text (with passwords/tokens masked, ADR-030) → ``source_name`` →
re-indexing (``chunk_and_embed_transcription``) → graph (``tg_entities``).

    python -m app.services.tg_photo_ocr run --dry-run      # only count
    python -m app.services.tg_photo_ocr run [--limit N]    # write

Idempotent: a record whose text already contains the recognized body is skipped.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

STUB = '[фото без тексту]'
PARSE_FAILED = 'parse_failed'
# A photo whose OCR succeeded at ingest is stored with doc_type = the file extension.
PHOTO_TYPES = ('photo', 'jpg', 'jpeg', 'png', 'webp', 'heic')
_WORKERS = max(2, min(12, (os.cpu_count() or 4) // 2))


def _caption_of(text: Optional[str]) -> str:
    """Caption = the current text, if it is not the stub (OCR had not worked before)."""
    t = (text or '').strip()
    return '' if t.startswith(STUB) else t


def _ocr(path: str) -> str:
    """Text from the image (already masked in document_parser); '' — there is no text."""
    from app.services import document_parser
    try:
        parsed = document_parser.parse_document(path, filename=os.path.basename(path))
    except document_parser.NoTextFound:
        return ''
    return (parsed.get('text') or '').strip()


def _source_name(chat: Optional[str], chat_id, text: str) -> str:
    """Like ``telegram._source_name`` for a photo: «[TG] chat: snippet»."""
    name = chat or f'chat {chat_id}'
    snippet = ' '.join((text or '').split())[:60] or 'фото'
    return f'[TG] {name}: {snippet}'


def _reindex(db_path: str, tid: int) -> bool:
    """True — the chunks were really written. A refusal (``unavailable``, epoch guard) is not counted as success:
    on 13.10.2026 a pass without .env took the old model, the epoch guard refused, and the counter said 606."""
    from app.services import embeddings, tg_entities
    ok = False
    try:
        res = embeddings.chunk_and_embed_transcription(db_path, tid, force=True) or {}
        ok = res.get('status') == 'embedded'
        if not ok:
            logger.warning("[tg-photo-ocr] tx=%s not re-indexed: %s", tid, res)
    except Exception:
        logger.warning("[tg-photo-ocr] tx=%s not re-indexed", tid, exc_info=True)
    try:
        tg_entities.link_message(db_path, tid)
    except Exception:
        logger.warning("[tg-photo-ocr] tx=%s: entities not linked", tid, exc_info=True)
    return ok


def run(db_path: str, *, dry_run: bool = True, limit: Optional[int] = None,
        reindex: bool = True) -> dict:
    from app.services import document_parser
    if not document_parser.ocr_available():
        return {'error': document_parser.ocr_unavailability_reason()}
    with get_db_connection(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, transcript_text, file_path, tg_chat_title, tg_chat_id, model_used, doc_type "
            f"FROM transcriptions WHERE source_type = 'telegram' AND deleted_at IS NULL "
            f"AND doc_type IN ({','.join('?' * len(PHOTO_TYPES))}) ORDER BY id", PHOTO_TYPES)]
    if limit:
        rows = rows[:int(limit)]
    stats = {'total': len(rows), 'missing_file': 0, 'with_text': 0, 'no_text': 0,
             'already': 0, 'updated': 0, 'errors': 0, 'dry_run': dry_run}
    todo = []
    changed: list[int] = []
    from app.services import screen_secrets
    for r in rows:
        if r['doc_type'] != 'photo':
            # doc_type = extension only when OCR at ingest SUCCEEDED: the whole text
            # of the record is that old reading, not a caption. A new OCR would give a slightly different
            # variant of the same text, so we append nothing — we only finish masking
            # (a record could land between the OCR revival and the mask's release).
            current = (r['transcript_text'] or '').strip()
            remasked = screen_secrets.mask_text(current, screen_secrets.scan_text(current)) or ''
            if remasked != current:
                stats['remasked'] = stats.get('remasked', 0) + 1
                if not dry_run:
                    with get_db_connection(db_path) as conn:
                        conn.execute("UPDATE transcriptions SET transcript_text = ? WHERE id = ?",
                                     (remasked, r['id']))
                        conn.commit()
                    changed.append(r['id'])
            else:
                stats['already'] += 1
            continue
        if not r['file_path'] or not os.path.isfile(r['file_path']):
            stats['missing_file'] += 1
        else:
            todo.append(r)

    def _one(r):
        try:
            return r, _ocr(r['file_path']), None
        except Exception as e:  # noqa: BLE001 — one broken file must not fail the pass
            return r, '', e

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for r, body, err in pool.map(_one, todo):
            if err is not None:
                stats['errors'] += 1
                logger.warning("[tg-photo-ocr] tx=%s: %s", r['id'], err)
                continue
            if not body:
                stats['no_text'] += 1
                if r['model_used'] == PARSE_FAILED and not dry_run:
                    with get_db_connection(db_path) as conn:   # parsing succeeded now — remove the mark
                        conn.execute("UPDATE transcriptions SET model_used = NULL WHERE id = ?",
                                     (r['id'],))
                        conn.commit()
                continue
            stats['with_text'] += 1
            current = (r['transcript_text'] or '').strip()
            if body in current:
                stats['already'] += 1
                continue
            # a photo accepted after the OCR revival but before the mask (4.10.0): the text is already
            # recognized, just not masked — mask what lies
            from app.services import screen_secrets
            remasked = screen_secrets.mask_text(current, screen_secrets.scan_text(current)) or ''
            if remasked != current and body in remasked:
                stats['remasked'] = stats.get('remasked', 0) + 1
                if not dry_run:
                    with get_db_connection(db_path) as conn:
                        conn.execute("UPDATE transcriptions SET transcript_text = ? WHERE id = ?",
                                     (remasked, r['id']))
                        conn.commit()
                    changed.append(r['id'])
                continue
            caption = _caption_of(current)
            text = f'{caption}\n\n{body}'.strip() if caption else body
            if dry_run:
                stats['updated'] += 1
                continue
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE transcriptions SET transcript_text = ?, source_name = ?, "
                    "model_used = 'ocr-backfill' WHERE id = ?",
                    (text, _source_name(r['tg_chat_title'], r['tg_chat_id'], text), r['id']))
                conn.commit()
            stats['updated'] += 1
            changed.append(r['id'])
    stats['reindexed'] = (sum(1 for tid in changed if _reindex(db_path, tid))
                          if (reindex and not dry_run) else 0)
    stats['reindex_failed'] = len(changed) - stats['reindexed'] if (reindex and not dry_run) else 0
    return stats


def reindex_backfilled(db_path: str) -> dict:
    """Re-index records the pass has already appended text to (``model_used='ocr-backfill'``)."""
    with get_db_connection(db_path) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM transcriptions WHERE source_type = 'telegram' "
            "AND model_used = 'ocr-backfill' AND deleted_at IS NULL")]
    done = sum(1 for tid in ids if _reindex(db_path, tid))
    return {'total': len(ids), 'reindexed': done, 'reindex_failed': len(ids) - done}


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog='python -m app.services.tg_photo_ocr',
                                description='OCR of Telegram photos that were left without text.')
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--dry-run', action='store_true')
    r.add_argument('--limit', type=int, default=None)
    r.add_argument('--db', default='whisper_history.db')
    ri = sub.add_parser('reindex', help='Re-index records the pass has already appended text to')
    ri.add_argument('--db', default='whisper_history.db')
    args = p.parse_args(argv)
    # The CLI starts outside app.py, which is what loads .env: without it the model and the embedding epoch
    # come from defaults, and the epoch guard refuses to write chunks.
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), '.env'))
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.cmd == 'reindex':
        res = reindex_backfilled(args.db)
        print(res)
        return 1 if res['reindex_failed'] else 0
    res = run(args.db, dry_run=args.dry_run, limit=args.limit)
    print(res)
    return 1 if 'error' in res else 0


if __name__ == '__main__':
    sys.exit(main())
