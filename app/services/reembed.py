"""Офлайн re-embed архіву на ЗНІМКУ (Волна B, історія 05, контракт C5).

**Навіщо окремий модуль.** Бекфіл через HTTP (`enrichment.backfill` →
`job_queue`) тягне з собою картку Claude, черги й живий процес застосунку.
Перехід на іншу пару (модель, версія) ембедингів — операція іншого роду:
локальна, довга, без грошей і без API, і робиться вона на ЗНІМКУ бойової БД
(`VACUUM INTO` — `scripts/backup_db.ps1`), щоб порівняти пошук «до» і «після»
гейтом (`evals/gate.py`) ПЕРЕД тим, як чіпати живий архів.

**Що робить.** Бере записи, чия пара (`embedding_model`, `embedding_version`)
не збігається з поточною (`embeddings.EMBED_MODEL`/`EMBED_VERSION` — обидві з
env, історія 04), і переембеджує кожен через `chunk_and_embed_transcription` —
тобто новою нарізкою, новою моделлю і з контекстним префіксом чанка
(`build_context_prefix`) одним проходом. У кінці — `optimize_chunk_index()`:
без нього масовий DELETE+INSERT по `chunks` кришить `chunks_fts` на сегменти і
пошук деградує 9с → 64с (заміряно на T6.5).

**Чого НЕ робить.** Не чіпає коментарі — у них власний індекс і власний
інструмент (`python -m app.services.comments reindex`), про що друкує
нагадування. Не запускається на бойовій БД без явного `--yes-live`.

**Звідки береться пара.** `EMBED_MODEL`/`EMBED_VERSION` читаються з ОТОЧЕННЯ
ПРОЦЕСУ на імпорті `embeddings` (історія 04), а не з `.env` цього виклику —
`.env` вантажить `app.py`, і робити те саме тут означало б тихо міняти
константи іншого модуля в кожному тесті, що імпортує цей. Тому пару для
прогону на знімку задає сам виклик, а прохід ДРУКУЄ її на початку і в підсумку
— розбіжність видно, а не вгадується:

    $env:EMBED_MODEL="Qwen/Qwen3-Embedding-0.6B"; $env:EMBED_VERSION="3"

CLI:
    python -m app.services.reembed run --db snapshot.db --dry-run
    python -m app.services.reembed run --db snapshot.db --limit 50
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from app.db.connection import get_db_connection
from app.services import embeddings
from app.services.enrichment import optimize_chunk_index

logger = logging.getLogger(__name__)


# ============================================================
# Гард бойової БД (зразок — evals/graph_links.py)
# ============================================================

def _live_db_path() -> Optional[str]:
    """Абсолютний шлях бойової БД (`Config.DATABASE`). Окрема функція — тести
    підміняють її, не чіпаючи справжній `config.py`/файлову систему."""
    try:
        from config import Config
    except Exception:  # noqa: BLE001 — конфіг може бути недоступний у CI/venv без .env
        logger.debug("[reembed] config.Config недоступний — гард живої БД вимкнено",
                     exc_info=True)
        return None
    return str((Config.BASE_DIR / Config.DATABASE).resolve())


def is_live_db(db_path: str) -> bool:
    """Чи збігається `db_path` з бойовою БД за абсолютним шляхом."""
    live = _live_db_path()
    if live is None:
        return False
    return str(Path(db_path).resolve()) == live


# ============================================================
# Вибірка застарілих записів
# ============================================================

#: Запис потрапляє в роботу, якщо його пара (модель, версія) не збігається з
#: поточною. `IS NOT ?` замість `<> ?` — бо NULL (ембеджено до того, як пару
#: взагалі почали писати) це теж «застаріло», а `<>` на NULL дає NULL.
#: Видалені й дублі не переембеджуються — їх немає в пошуку (Волна A), тож
#: рахувати за них GPU-час нема сенсу. Порожній текст відсіюємо тут, а не
#: лічильником skipped: інакше такі записи лишались би «застарілими» вічно і
#: повторний прохід ніколи не давав би нуля.
_STALE_WHERE = (
    "deleted_at IS NULL AND duplicate_of IS NULL "
    "AND (embedding_model IS NOT ? OR embedding_version IS NOT ?) "
    "AND (TRIM(COALESCE(polished_text, '')) <> '' "
    "     OR TRIM(COALESCE(transcript_text, '')) <> '')"
)


def _stale_params() -> list:
    return [embeddings.EMBED_MODEL, embeddings.EMBED_VERSION]


def stale_ids(db_path: str, limit: Optional[int] = None) -> list[int]:
    """id записів, чия пара (модель, версія) не збігається з поточною."""
    sql = f"SELECT id FROM transcriptions WHERE {_STALE_WHERE} ORDER BY id"
    params = _stale_params()
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with get_db_connection(db_path) as conn:
        return [int(r["id"]) for r in conn.execute(sql, params).fetchall()]


def plan(db_path: str, limit: Optional[int] = None) -> dict:
    """Скільки записів і символів чекає на re-embed (для `--dry-run`).

    Символи — довжина того самого тексту, який піде в нарізку
    (`polished_text`, інакше `transcript_text`): це єдина доступна наперед
    оцінка обсягу GPU-роботи — чанки рахуються вже під час проходу."""
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
    """Переембедити застарілі записи. Ідемпотентно: повторний запуск після
    успішного проходу не знаходить роботи (пара збережена на кожному записі).

    `optimize_chunk_index` викликається РІВНО один раз і тільки якщо щось
    справді переписано — на нульовому проході він зайвий."""
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
        except Exception:  # noqa: BLE001 — один поганий запис не має валити прохід
            failed += 1
            logger.exception("[reembed] tx=%s: не вдалось переембедити", tid)
            continue
        if res.get("status") == "embedded":
            done += 1
        else:
            skipped += 1
            logger.debug("[reembed] tx=%s пропущено: %s", tid, res.get("status"))

    if done:
        optimize_chunk_index(db_path)

    result = {"status": "ok", "total": len(ids), "done": done, "skipped": skipped,
              "failed": failed, "seconds": round(time.time() - t0, 1),
              "optimized": bool(done), "model": embeddings.EMBED_MODEL,
              "version": embeddings.EMBED_VERSION}
    logger.info("[reembed] завершено: %s", result)
    return result


# ============================================================
# CLI
# ============================================================

def _print_result(res: dict) -> None:
    if res.get("dry_run"):
        print(f"[reembed] dry-run: {res['total']} записів, {res['chars']} символів "
              f"→ пара {res['model']} / v{res['version']}")
        print("[reembed] нічого не записано.")
        return
    if res.get("status") == "unavailable":
        print(f"[reembed] embeddings недоступні: {res.get('reason')}")
        return
    print(f"[reembed] готово: done={res['done']} skipped={res['skipped']} "
          f"failed={res['failed']} за {res['seconds']}с "
          f"({res['model']} / v{res['version']})")
    print(f"[reembed] chunks_fts optimize: {'виконано' if res['optimized'] else 'не потрібен'}")
    print("[reembed] нагадування: шар коментарів має власний індекс — "
          "`python -m app.services.comments reindex`")


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        prog="reembed",
        description="Re-embed архіву на ЗНІМКУ під поточну пару (EMBED_MODEL, EMBED_VERSION).")
    # --dry-run має рятувати з будь-якого місця рядка (як у dedup_audio):
    # підпарсер із тим самим dest переписав би батьківський namespace своїм
    # дефолтом False, тому верхній флаг іде в окремий dest, а фінальне
    # значення — OR обох.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true",
                   help="Нічого не писати в БД — лише кількість і символи")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", required=True,
                        help="Шлях до ЗНІМКА БД (VACUUM INTO — scripts/backup_db.ps1)")
    common.add_argument("--dry-run", action="store_true",
                        help="Нічого не писати в БД — лише кількість і символи")
    common.add_argument("--limit", type=int, default=None,
                        help="Обробити не більше N записів (обкатка проходу)")
    common.add_argument("--yes-live", action="store_true",
                        help="Дозволити файл, що збігається з Config.DATABASE "
                             "(за замовчуванням заборонено — цей прохід для знімка)")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", parents=[common],
                   help="Переембедити записи, чия пара (модель, версія) застаріла")

    args = p.parse_args(argv)
    args.dry_run = args.dry_run or args.dry_run_pre

    if not os.path.exists(args.db):
        print(f"[reembed] БД не знайдено: {args.db}", file=sys.stderr)
        return 2

    if is_live_db(args.db) and not args.yes_live:
        print(f"[reembed] Відмова: {args.db!r} — це бойова БД (Config.DATABASE). "
              "Прохід переписує ВСІ чанки архіву; робіть його на ЗНІМКУ "
              "(VACUUM INTO — scripts/backup_db.ps1). Якщо це свідомий вибір — "
              "додайте --yes-live.", file=sys.stderr)
        return 2

    print(f"[reembed] поточна пара: {embeddings.EMBED_MODEL} / v{embeddings.EMBED_VERSION}")
    res = run(args.db, limit=args.limit, dry_run=args.dry_run)
    _print_result(res)
    return 2 if res.get("status") == "unavailable" else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
