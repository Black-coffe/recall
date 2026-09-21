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
import threading
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
# Точковий re-embed ОДНОГО запису (історія 03)
# ============================================================
#
# Навіщо окремо від `run`. Після правки назви/опису (`PATCH /api/history/<id>`)
# застаріває рівно один запис: пара (модель, версія) у нього та сама, тому
# `stale_ids` його не бачить взагалі, а прохід по всьому архіву заради одного
# запису — години GPU. Тут — `chunk_and_embed_transcription(force=True)` на
# один id у фоновій черзі; `optimize_chunk_index` НЕ кличеться (десяток
# DELETE+INSERT сегментацію FTS не кришить, а сам optimize коштує хвилини).

#: Через скільки перевіряти знову, якщо в цей момент триває живий запис.
_DEFER_SECONDS = 30.0

#: id, які чекають на вікно без запису. Набір (а не лічильник) згортає серію
#: правок однієї картки в одну майбутню задачу: користувач править назву,
#: потім опис — переембеджувати треба один раз, і вже з обома значеннями.
_pending_lock = threading.Lock()
_pending_ids: set[int] = set()


def _recording_active() -> bool:
    """Чи триває просто зараз живий запис. Прапорець — `active_session_id`
    сервісу запису (те саме джерело, що й `GET /api/recordings/active`):
    стан читається без session_id і без блокувань черги."""
    try:
        from app import state
        service = getattr(state, "recording_service", None)
        return bool(service is not None and service.active_session_id)
    except Exception:  # noqa: BLE001 — нема сервісу/стану = запису нема
        logger.debug("[reembed] стан запису недоступний — вважаємо, що запису нема",
                     exc_info=True)
        return False


def _record_reembed_job(job, transcription_id: int, db_path: str) -> dict:
    """Тіло фонової задачі. `force=True` обовʼязковий: `embedded_at` у запису
    стоїть, пара (модель, версія) не змінилась — без force прохід вважав би
    роботу зробленою і новий префікс ніколи б не доїхав у вектори.

    Id знімається з `_pending_ids` САМЕ ТУТ, на старті тіла — до читання рядка.
    Якби його знімав планувальник одразу після `submit`, виклик, що встиг
    отримати `"pending"`, покладався б на задачу, яка вже прочитала рядок зі
    старою метою, і його правка не доїхала б у вектори взагалі."""
    with _pending_lock:
        _pending_ids.discard(int(transcription_id))
    res = embeddings.chunk_and_embed_transcription(db_path, transcription_id, force=True)
    logger.info("[reembed] точковий re-embed tx=%s: %s", transcription_id, res.get("status"))
    return res


def _submit_record_reembed(transcription_id: int, db_path: str) -> str:
    """Поставити задачу в чергу або відкласти, поки триває запис."""
    if _recording_active():
        timer = threading.Timer(_DEFER_SECONDS, _retry_record_reembed,
                                args=(transcription_id, db_path))
        timer.daemon = True
        timer.start()
        logger.info("[reembed] tx=%s відкладено на %sс — триває запис",
                    transcription_id, _DEFER_SECONDS)
        return "deferred"

    from app import state
    queue = getattr(state, "job_queue", None)
    if queue is None:
        with _pending_lock:
            _pending_ids.discard(transcription_id)
        logger.info("[reembed] tx=%s не переембеджено: черги задач немає", transcription_id)
        return "skipped"

    queue.submit("reembed_record", _record_reembed_job, transcription_id, db_path,
                 meta={"transcription_id": transcription_id, "reason": "meta_changed",
                       "db_path": db_path})
    return "queued"


def _retry_record_reembed(transcription_id: int, db_path: str) -> None:
    """Повтор із таймера: якщо запис і далі триває — ще один таймер."""
    try:
        _submit_record_reembed(transcription_id, db_path)
    except Exception:  # noqa: BLE001 — таймер у фоні, падіння нікому не долетить
        with _pending_lock:
            _pending_ids.discard(transcription_id)
        logger.exception("[reembed] tx=%s: повтор точкового re-embed зірвався",
                         transcription_id)


def schedule_record_reembed(transcription_id: int, *, db_path: str) -> str:
    """Переембедити чанки ОДНОГО запису у фоні. Повертає що сталося:

    - ``"queued"``    — задача стала в `job_queue`;
    - ``"deferred"``  — триває живий запис, повтор через `_DEFER_SECONDS`
      (слот черги при цьому НЕ займається — інакше очікування зʼїло б один із
      двох воркерів пулу);
    - ``"pending"``   — цей id уже чекає свого вікна, другої задачі не буде;
    - ``"skipped"``   — переембеджувати нічим (немає torch/моделі або черги).

    `db_path` — обовʼязковий keyword БЕЗ дефолту: дефолт «`None` = бойова БД»
    означав би, що будь-який виклик із тестової/тимчасової БД мовчки переписує
    чанки бойового архіву. Шлях завжди приходить явно від того, хто відкрив
    зʼєднання (у PATCH — `current_app.config["DATABASE"]`).
    """
    tid = int(transcription_id)
    if not embeddings.is_available():
        logger.info("[reembed] tx=%s не переембеджено: %s", tid,
                    embeddings.unavailability_reason())
        return "skipped"

    if not db_path:
        logger.info("[reembed] tx=%s не переембеджено: невідомий шлях до БД", tid)
        return "skipped"
    path = str(db_path)

    with _pending_lock:
        if tid in _pending_ids:
            logger.debug("[reembed] tx=%s уже чекає на re-embed", tid)
            return "pending"
        _pending_ids.add(tid)

    return _submit_record_reembed(tid, path)


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
