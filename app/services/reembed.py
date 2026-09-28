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

    $env:EMBED_MODEL="Qwen/Qwen3-Embedding-0.6B"; $env:EMBED_VERSION="N"

де N — номер СТРОГО вище будь-якої пари, яку тримає БД (`epoch-check` покаже
перелік пар і вердикт). Номер, який у БД уже є, змішав би два формати
контекстного префікса в одній епосі — тому прохід на такій парі відмовляється
стартувати (`embeddings.epoch_status`, ADR-017).

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
    справді переписано — на нульовому проході він зайвий.

    Історія 17: якщо поточну епоху вже будував ІНШИЙ формат префікса, прохід
    не стартує взагалі (навіть `--dry-run`) — на такій парі `stale_ids` віддав
    би 0, а запис нових префіксів поруч зі старими змішав би два формати в
    одній вибірці `retrieval.search`."""
    with get_db_connection(db_path) as conn:
        epoch = embeddings.epoch_status(conn)
    if not epoch["ok"]:
        msg = embeddings.epoch_mismatch_message(epoch)
        logger.error("[reembed] прохід не стартував: %s", msg)
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
    """Чи триває просто зараз хоч один живий запис. Прапорець — `active_session_ids()`
    сервісу запису (те саме джерело, що й `GET /api/recordings/active`):
    `active_session_id` при двох записах дає None і пустив би переембеджинг."""
    try:
        from app import state
        service = getattr(state, "recording_service", None)
        return bool(service is not None and service.active_session_ids())
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
# Точковий re-embed СПИСКУ записів (спека directions-projects-tags, C3)
# ============================================================
#
# Напрямок, проєкт і теги їдуть у контекстний префікс чанка так само, як назва
# й опис, тож кожна їх зміна застарює вектори. Але тут зміна буває масовою:
# bulk-розмітка напрямку, reparent вузла з сотнями записів, ретроактивна
# привʼязка тега до TG-чату. Ставити по задачі на запис у чергу з двома
# воркерами означало б забити її на годину; тому від _BATCH_THRESHOLD записів
# іде ОДНА задача, яка в кінці робить `optimize_chunk_index` рівно раз (масовий
# DELETE+INSERT по chunks кришить chunks_fts — заміряно 9с → 64с на T6.5).

#: Від скількох живих id переходимо з N точкових задач на одну батчеву.
_BATCH_THRESHOLD = 20


def _live_and_stale(db_path: str, ids: list[int]) -> tuple[list[int], list[int]]:
    """Розділити id на «в поточній епосі» і «застарілі».

    Застарілий запис (інша пара `(модель, версія)` або взагалі без чанків) у
    точковий re-embed не йде: його перебудує масовий прохід `run` цілком і за
    новою нарізкою, а точкова задача зробила б ту саму роботу раніше й дарма.
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
    """Тіло батчевої задачі: переембедити список і один раз оптимізувати індекс.

    Кожен id знімається з `_pending_ids` ПЕРЕД читанням свого рядка — з тієї ж
    причини, що й у точковій задачі: виклик, який отримав «уже заплановано»,
    має спиратись на задачу, що прочитає рядок ПІСЛЯ його правки.

    `remaining` — id, які цикл ще НЕ встиг відвідати; рахується від повного
    списку ДО циклу, щоб пережити навіть виняток із самої ітерації (не з
    `chunk_and_embed_transcription` — той ловиться нижче й батчу не зупиняє).
    `finally` відпускає з `_pending_ids` хвіст, що лишився при `break`
    (скасування) або будь-якому винятку, який вийшов із циклу — інакше ці id
    застрягли б у `_pending_ids` до рестарту процесу й `schedule_record_reembed`
    для них назавжди повертав би `"pending"`.

    Історія 17 (мінор 7): цей хвіст друкується `WARNING`-ом СПИСКОМ id і
    причиною — скасування не повертає id у чергу (скасування це скасування),
    тож лог — єдиний слід того, що саме лишилось непереембедженим. Id, на якому
    цикл зупинився (скасування або виняток механіки), у цей список повертається:
    він теж не переембеджений."""
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
                logger.info("[reembed] батч %s скасовано на tx=%s", reason, tid)
                # id, на якому спіймали скасування, теж НЕ оброблений — у лог
                # він має потрапити разом із рештою хвоста (з `_pending_ids`
                # його вже зняв рядок вище, повторне зняття нешкідливе).
                remaining.add(tid)
                stop_reason = "cancelled"
                break
            try:
                res = embeddings.chunk_and_embed_transcription(db_path, tid, force=True)
            except Exception:  # noqa: BLE001 — один поганий запис не валить батч
                failed += 1
                logger.exception("[reembed] батч %s: tx=%s не вдалось переембедити", reason, tid)
                continue
            if res.get("status") == "embedded":
                done += 1
            else:
                skipped += 1
                logger.debug("[reembed] батч %s: tx=%s пропущено (%s)", reason, tid,
                             res.get("status"))
    except BaseException as exc:   # noqa: BLE001 — лише щоб назвати причину в лозі
        stop_reason = type(exc).__name__
        if current is not None:
            remaining.add(current)
        raise
    finally:
        if remaining:
            with _pending_lock:
                _pending_ids.difference_update(remaining)
            logger.warning("[reembed] батч %s: %d id лишились непереембедженими "
                           "(%s), у чергу не повертаються: %s",
                           reason, len(remaining), stop_reason or "unknown",
                           sorted(remaining))
    if done:
        optimize_chunk_index(db_path)
    result = {"status": "ok", "total": len(ids), "done": done, "skipped": skipped,
              "failed": failed, "optimized": bool(done), "reason": reason}
    logger.info("[reembed] батч %s завершено: %s", reason, result)
    return result


def _submit_batch_reembed(ids: list[int], db_path: str, reason: str) -> str:
    """Поставити батч у чергу або відкласти, поки триває живий запис."""
    if _recording_active():
        timer = threading.Timer(_DEFER_SECONDS, _retry_batch_reembed,
                                args=(ids, db_path, reason))
        timer.daemon = True
        timer.start()
        logger.info("[reembed] батч %s (%s записів) відкладено на %sс — триває запис",
                    reason, len(ids), _DEFER_SECONDS)
        return "deferred"

    from app import state
    queue = getattr(state, "job_queue", None)
    if queue is None:
        with _pending_lock:
            for tid in ids:
                _pending_ids.discard(int(tid))
        logger.info("[reembed] батч %s не поставлено: черги задач немає", reason)
        return "skipped"

    queue.submit("reembed_batch", _batch_reembed_job, list(ids), db_path, reason,
                 meta={"ids": list(ids), "db_path": db_path, "reason": reason})
    return "queued"


def _retry_batch_reembed(ids: list[int], db_path: str, reason: str) -> None:
    try:
        _submit_batch_reembed(ids, db_path, reason)
    except Exception:  # noqa: BLE001 — таймер у фоні, падіння нікому не долетить
        with _pending_lock:
            for tid in ids:
                _pending_ids.discard(int(tid))
        logger.exception("[reembed] батч %s: повтор зірвався", reason)


def schedule_records_reembed(ids, *, db_path: str, reason: str) -> dict:
    """Запланувати точковий re-embed СПИСКУ записів.

    Повертає `{"mode": "point"|"batch"|"none", "count": …, "stale": …}`:

    - ``point`` — ≤ `_BATCH_THRESHOLD` живих id, по задачі на запис
      (`schedule_record_reembed`, з усіма його гарантіями: дедуп по
      `_pending_ids`, відкладення під час живого запису);
    - ``batch`` — більше, одна задача `reembed_batch` з `optimize_chunk_index`
      у кінці;
    - ``none``  — планувати нема чого або нічим (порожній список, немає
      torch/моделі, невідомий `db_path`).

    `stale` — скільки id відсіяно як «не в поточній епосі» (їх візьме масовий
    `run`). `db_path` — обовʼязковий keyword без дефолту з тієї ж причини, що
    й у `schedule_record_reembed`: дефолт «бойова БД» означав би, що виклик із
    тимчасової БД переписує чанки бойового архіву.
    """
    ids = sorted({int(i) for i in (ids or [])})
    if not ids:
        return {"mode": "none", "count": 0, "stale": 0}
    if not embeddings.is_available():
        logger.info("[reembed] %s: %s записів не переембеджено (%s)", reason, len(ids),
                    embeddings.unavailability_reason())
        return {"mode": "none", "count": 0, "stale": 0}
    if not db_path:
        logger.info("[reembed] %s: %s записів не переембеджено — невідомий шлях до БД",
                    reason, len(ids))
        return {"mode": "none", "count": 0, "stale": 0}

    live, stale = _live_and_stale(str(db_path), ids)
    if stale:
        logger.info("[reembed] %s: %s id поза поточною епохою — їх візьме `run`",
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
        # Усі живі id уже чекають на точкову задачу — дублю не потрібен, вона
        # й так прочитає актуальний рядок на виконанні.
        return {"mode": "none", "count": 0, "stale": len(stale)}
    _submit_batch_reembed(to_submit, str(db_path), reason)
    return {"mode": "batch", "count": len(to_submit), "stale": len(stale)}


# ============================================================
# CLI
# ============================================================

def _ro_uri(db_path: str) -> str:
    """URI для `sqlite3.connect(..., uri=True)` у режимі `mode=ro` — SQLite
    фізично забороняє запис (зразок — `evals/snapshot.py`)."""
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def epoch_check(db_path: Optional[str] = None) -> int:
    """Крок 1 рунбука міграції: чи можна писати поточний формат префікса в
    поточну епоху — ДО рестарту застосунку і БЕЗ жодного запису.

    Друкує пари `(модель, версія)` архіву, памʼять БД про формат кожної епохи
    (`embedding_epochs`, v46), поточну пару коду з `PREFIX_FORMAT` і вердикт.
    Код виходу: 0 — `ok`/`new_epoch`, 1 — `format_mismatch`, 2 — БД не знайдено.
    `--yes-live` не потрібен: зʼєднання відкривається `mode=ro`.
    """
    path = db_path or _live_db_path()
    if not path:
        print("[reembed] не вказано --db і не вдалось визначити бойову БД",
              file=sys.stderr)
        return 2
    if not os.path.exists(path):
        print(f"[reembed] БД не знайдено: {path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(_ro_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print(f"[epoch-check] БД: {path} (лише читання)")
        rows = conn.execute(
            "SELECT embedding_model AS m, embedding_version AS v, COUNT(*) AS n "
            "FROM transcriptions WHERE embedding_model IS NOT NULL "
            "GROUP BY 1, 2 ORDER BY 2, 1").fetchall()
        print("[epoch-check] пари в transcriptions:"
              + ("" if rows else " немає (порожній архів)"))
        for r in rows:
            print(f"    {r['m']} / v{r['v']} — {r['n']} записів")

        try:
            epochs = conn.execute(
                "SELECT model, version, prefix_format FROM embedding_epochs "
                "ORDER BY version, model").fetchall()
        except sqlite3.OperationalError:
            epochs = None
        if epochs is None:
            print("[epoch-check] embedding_epochs: таблиці немає (БД нижче v46) — "
                  "наявні пари рахуються форматом 1, як їх стемплює міграція")
        else:
            print("[epoch-check] embedding_epochs:"
                  + ("" if epochs else " порожня"))
            for r in epochs:
                print(f"    {r['model']} / v{r['version']} — формат {r['prefix_format']}")

        status = embeddings.epoch_status(conn)
        model, version = status["pair"]
        print(f"[epoch-check] код: {model} / v{version}, "
              f"PREFIX_FORMAT={embeddings.PREFIX_FORMAT}")
        if status["reason"] == "format_mismatch":
            print(f"[epoch-check] ВЕРДИКТ format_mismatch: "
                  f"{embeddings.epoch_mismatch_message(status)}", file=sys.stderr)
            return 1
        if status["reason"] == "new_epoch":
            print("[epoch-check] ВЕРДИКТ new_epoch: епохи ще немає — формат вільний, "
                  "можна рестартувати і робити прохід.")
        else:
            print("[epoch-check] ВЕРДИКТ ok: епоху побудовано цим самим форматом.")
        return 0
    finally:
        conn.close()


def _print_result(res: dict) -> None:
    if res.get("status") == "epoch_mismatch":
        print(f"[reembed] Відмова: {res['message']}", file=sys.stderr)
        return
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
    # epoch-check НЕ бере `common`: він нічого не пише (`mode=ro`), тож ні
    # --yes-live, ні --limit, ні --dry-run йому не потрібні, а --db тут
    # опційний — без нього дивимось бойову БД.
    ep = sub.add_parser("epoch-check",
                        help="Чи можна писати поточний формат префікса в поточну "
                             "епоху (лише читання; крок 1 рунбука міграції)")
    ep.add_argument("--db", default=None,
                    help="Шлях до БД (за замовчуванням — бойова Config.DATABASE)")

    args = p.parse_args(argv)

    if args.command == "epoch-check":
        return epoch_check(args.db)

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
    if res.get("status") == "unavailable":
        return 2
    return 1 if res.get("status") == "epoch_mismatch" else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
