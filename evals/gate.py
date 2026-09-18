#!/usr/bin/env python
"""Гейт retrieval на знімку БД (eval-gate, історія 01): `python -m evals.gate`.

Один k недостатньо (урок `eval-verdict-flips-with-k` — на k=8 регресія, на
k=12 плюс): гейт завжди рахує ОБИДВА k і провалюється, якщо хоч один не
проходить поріг чи регресує відносно базової лінії. Ганяється на ЗНІМКУ
(`VACUUM INTO`, ADR-003) — жива БД відкривається лише щоб зняти з неї
знімок (`evals/snapshot.py`), `retrieval.search` ніколи не бачить бойовий
файл напряму, якщо не сказано інакше явно (`--no-snapshot --yes-live`).

Retrieval-only: жодного виклику Claude (`rag.answer_question`) тут немає —
LLM-залежні метрики лишаються в `evals/run_eval.py` (A1 плану, платні,
ручний запуск).

Exit-коди (C3 плану, розширено D2/D3): `0` — пройдено; `1` — ВИКЛЮЧНО вердикт
якості (поріг, падіння відносно лінії, регресія пункту); `2` — усе інше: нема
golden, жива БД без `--yes-live`, невалідний golden-set, жоден пункт
`status="labeled"` не оцінено на якомусь k, збій інфраструктури під час
заміру (модель, знімок, диск), базова лінія іншого провенансу.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Дозволяє запускати і як `python evals/gate.py`, і як `python -m evals.gate`
# без встановлення пакету — той самий трюк, що в evals/run_eval.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.core import settings as _settings  # noqa: E402
from evals import golden_io  # noqa: E402
from evals import metrics as ev_metrics  # noqa: E402
from evals import snapshot as ev_snapshot  # noqa: E402
from evals.graph_links import is_live_db, open_readonly  # noqa: E402

#: D15.3 — пороги виведені з калібрувальної лінії (`evals/baseline.local.json`),
#: не задані з голови. Правило: `floor_0.05(виміряний recall_at_k − 2/n)`, де
#: `n` — кількість `labeled`-пунктів лінії (`_derive_min_recall_threshold`
#: нижче — та сама функція, викликана тут руками з заміряних чисел, бо
#: файл лінії поза git і не існує до першого `--write-baseline`). Запас у
#: ДВА пункти: при `n=15` один пункт важить 6.7 п.п., поріг вужчий за
#: зернистість набору ловив би розмітку, а не ранжування.
#: Замір 03.09.2026 (`evals/golden_set.local.json`, n=15, `--k 8,12`,
#: знімок `whisper_history-20260903-145235.db` — архів живий і дрейфує
#: під час роботи телеграм-слухача, тож це знімок, що ФАКТИЧНО пережив
#: власний перезамір і саботаж, не перший знятий за сесію): recall_at_k@8=
#: 0.32143, recall_at_k@12=0.53571 → поріг@8 = floor_0.05(0.32143 − 2/15) =
#: floor_0.05(0.18810) = 0.15; поріг@12 = floor_0.05(0.53571 − 2/15) =
#: floor_0.05(0.40238) = 0.40.
#: ЦЕ ДЕТЕКТОР ОБВАЛУ, не критерій якості і не детектор саботажу (D15.4) —
#: саботаж на k=8 абсолютний поріг не ловить у принципі (замір 03.09: чистий
#: 39.3% → саботаж 42.9%, вище чистого, на іншому знімку тієї самої сесії).
#: Саботаж ловить пунктова регресія проти лінії (`--max-item-regressions`,
#: дефолт 0), не цей поріг.
_DEFAULT_MIN_RECALL = "8=0.15,12=0.40"
_DEFAULT_MAX_DROP = 0.02
_DEFAULT_MAX_ITEM_REGRESSIONS = 0

#: Ремонтний раунд 1 (історія 13, знахідка при закритті історії 15): шлях, на
#: який усі приклади `evals/README.md` пишуть і читають лінію, коли
#: прапорці --baseline/--write-baseline цього ЗАПУСКУ про неї нічого не
#: кажуть. Без цього конвенційного fallback прогін без прапорців (саботажний
#: прогін, ручний замір, будь-який `python -m evals.gate --db ...`) не бачив
#: живу лінію взагалі — і прунер вільно знищував знімок, на який вона
#: показує (так S1 і померла). Шлях навмисно відносний до cwd — так само,
#: як дефолт `--db` (`whisper_history.db`): гейт документовано ганяють з
#: кореня репозиторію.
_CONVENTIONAL_BASELINE_PATH = os.path.join("evals", "baseline.local.json")


def _floor_grid(x: float, grid: float = 0.05) -> float:
    """`floor(x/grid)*grid`, стійкий до похибки float — без epsilon
    `0.30/0.05` дає `5.999999999999999` (не `6.0`), і межове значення
    провалюється в сусідню комірку нижче (`0.30` → `0.25` замість `0.30`)."""
    return math.floor(x / grid + 1e-9) * grid


def _derive_min_recall_threshold(recall_at_k: float, n: int) -> float:
    """D15.3: правило виведення дефолтного `--min-recall` з калібрувальної
    лінії — `floor_0.05(recall_at_k − 2/n)`. Чиста функція над числами
    лінії (не читає файл сама), щоб тест міг перевірити САМЕ правило на
    синтетичних `recall_at_k`/`n`, а не на переписаних константах."""
    return _floor_grid(recall_at_k - 2.0 / n)


def _protected_snapshot_paths(*baseline_paths: Optional[str]) -> list[str]:
    """Знімки, названі живими базовими лініями (D20/знахідка 5), — `auto_snapshot`
    не сміє їх прибрати, доки лінію не перезаписано. `--baseline` (проти чого
    порівнюємо) і `--write-baseline` (ціль, яку цей прогін МОЖЕ не перезаписати
    — D12: пишеться лише на PASS) обидва рахуються живими лініями. Побитий чи
    відсутній файл лінії просто не додає нічого в захист (не крашить гейт).

    Ремонт 1: `_CONVENTIONAL_BASELINE_PATH` (`evals/baseline.local.json`)
    перевіряється завжди, окрім переданих `baseline_paths` — прогін, який не
    назвав жодного прапорця, все одно не сміє знищити лінію, що фізично лежить
    на диску за конвенцією README."""
    protected: list[str] = []
    seen: set[str] = set()
    for path in (*baseline_paths, _CONVENTIONAL_BASELINE_PATH):
        if not path or path in seen:
            continue
        seen.add(path)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        snap_path = (data.get("db_snapshot") or {}).get("path")
        if snap_path:
            protected.append(snap_path)
    return protected


def _parse_k_list(spec: str) -> list[int]:
    ks = [int(x) for x in spec.split(",") if x.strip()]
    if not ks:
        raise ValueError("--k: порожній список")
    return ks


def _parse_min_recall(spec: str) -> dict[int, float]:
    """`"8=0.60,12=0.75"` → `{8: 0.60, 12: 0.75}`. k без запису в мапі —
    поріг recall_at_k для нього просто не перевіряється."""
    out: dict[int, float] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        k_str, _, v_str = part.partition("=")
        out[int(k_str)] = float(v_str)
    return out


@contextlib.contextmanager
def _scratch_copy(db_path: str) -> Iterator[str]:
    """Тимчасова копія знімка для виміру (D6/ADR-003) — `retrieval.search`
    іде через `get_db_connection` (`PRAGMA journal_mode=WAL`), що мутує файл
    лише відкриттям. Копія приймає цей побічний ефект на себе — файл, який
    гейт звітує як `db_snapshot`, лишається байт-у-байт незмінним. Свідома
    копія патерну `evals/graph_links.py::_scratch_copy` (не імпортуємо
    приватне зі сусіднього модуля — той самий підхід, що
    `evals/build_golden.py::_scratch_copy`)."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # copyfile лишається ВСЕРЕДИНІ try — інакше збій копіювання (диск
        # повний, права) лишає порожню заглушку від mkstemp на диску назавжди.
        shutil.copyfile(db_path, tmp_path)
        # Знахідка 13: якщо джерело несе `-wal`/`-shm`, копія без них — це
        # старіший стан, ніж committed (WAL тримає транзакції, ще не
        # влиті в головний файл). Копія мусить представляти те саме
        # зафіксоване наповнення, що й файл, чию ідентичність гейт звітує.
        for suffix in ("-wal", "-shm"):
            sidecar = db_path + suffix
            if os.path.exists(sidecar):
                shutil.copyfile(sidecar, tmp_path + suffix)
        yield tmp_path
    finally:
        for suffix in ("", "-wal", "-shm"):
            candidate = tmp_path + suffix
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError as exc:
                    print(f"[gate] не вдалося прибрати тимчасовий файл {candidate}: {exc}",
                          file=sys.stderr)


def _sabotage_pool(pool: list[dict], mode: str, k: int) -> list[dict]:
    """Детерміновано (seed 0) ламає ПУЛ кандидатів ПЕРЕД відсіканням до k
    (D1) — гарантовано витісняє КОЖНУ з перших k позицій пулу за межі нових
    перших k, на будь-якому k (доведено тестом на дефолтних 8,12, не лише
    на малих). `reverse` на пулі `4k` завжди задовольняє це (old index
    `i` → new index `4k-1-i` ≥ `4k-1-(k-1)` = `3k` ≥ `k`). `shuffle` тепер
    так само гарантує це навмисно: перші k позицій пулу («голова») і решта
    («хвіст») тасуються НЕЗАЛЕЖНО, тоді хвіст ставиться ПЕРЕД головою — жоден
    старий top-k елемент не потрапляє у нові перші k, доки в пулі є принаймні
    k елементів поза старою головою (виконано: пул = 4k). Стара реалізація
    (`random.Random(0).shuffle` над усім пулом) цієї гарантії не давала —
    на дефолтних k=8/12 старий top-0 виживав у новому top-k (знахідка 1)."""
    pool = list(pool)
    if mode == "reverse":
        pool.reverse()
        return pool
    if mode == "shuffle":
        head, tail = pool[:k], pool[k:]
        rnd = random.Random(0)
        rnd.shuffle(head)
        rnd.shuffle(tail)
        return tail + head
    raise ValueError(f"невідомий --sabotage {mode!r}")


def _search(db_path: str, item: dict, k: int, *,
            sabotage: Optional[str], rerank: bool = False) -> tuple[list[dict], Optional[bool]]:
    """Без саботажу (D4/знахідка 5) — те саме `top_k`, що продакшн: `k`, а не
    зріз k із запиту `k*4` (стеля коментарів `_cap_comments` рахується від
    `top_k`, тож більший запит пускає в топ те, чого продакшн туди не пустив
    би). Саботаж лишається вправі просити більший пул — інструмент, а не
    замір (Non-goals).

    `rerank` (18.09.2026) — другий етап `retrieval.search` тим самим
    cross-encoder'ом, яким його вмикає RAG-чат (`rag.py` `_RERANK_ENABLED` з
    `RECALL_RERANK_ENABLED`). Дефолт лишається False, щоб старі лінії й
    прогони не змінили сенс заднім числом; вмикає його оператор прапорцем
    `--rerank`, і прогін підписує себе в провенансі. `rerank_pool_size` не
    передаємо — продакшн теж не передає, тобто пул береться з дефолту
    `retrieval` і замір не розходиться з боєм ще й тут."""
    from app.services import retrieval

    if sabotage:
        pool_size = k * 4
        res = retrieval.search(
            db_path, item["question"], top_k=pool_size,
            category_id=item.get("category_id"), rerank=rerank,
        )
        chunks = _sabotage_pool(res["chunks"], sabotage, k)
    else:
        res = retrieval.search(
            db_path, item["question"], top_k=k,
            category_id=item.get("category_id"), rerank=rerank,
        )
        chunks = res["chunks"]
    return chunks[:k], res.get("vector_available")


def _item_hit(row: dict) -> Optional[bool]:
    """"Пройдено" на пункт: усі задані критерії (recall_at_k повний,
    source_name_hit True) виконані. `None`, якщо жодного критерію не
    задано (напр. `status="negative"`) — такий пункт не бере участі ні в
    порозі, ні в регресії відносно лінії."""
    recall = row.get("recall_at_k")
    name_hit = row.get("source_name_hit")
    if recall is None and name_hit is None:
        return None
    ok = True
    if recall is not None:
        ok = ok and recall >= 0.999
    if name_hit is not None:
        ok = ok and bool(name_hit)
    return ok


def _run_k(golden_items: list[dict], db_path: str, k: int, *,
           sabotage: Optional[str],
           rerank: bool = False) -> tuple[list[dict], dict, dict, Optional[bool]]:
    """Пункти зі `status="unlabeled"` пропускаються повністю (C1); `negative`
    рахується (search запускається, лишається в per-item звіті), але не
    входить у `aggregate`/`by_slice` (recall_at_k/source_name_hit — `None`,
    `aggregate()` вже виключає `None`). Четвертий елемент — `vector_available`
    цього прогону (D5/D6, знахідка 6): береться з першого виклику search, що
    його повернув — стабільна властивість запуску (модель або є, або нема),
    не по-пунктова."""
    rows = []
    vector_available: Optional[bool] = None
    for item in golden_items:
        if item["status"] == "unlabeled":
            continue
        chunks, vec_avail = _search(db_path, item, k, sabotage=sabotage, rerank=rerank)
        if vector_available is None:
            vector_available = vec_avail
        row = ev_metrics.evaluate_item(item, chunks)
        row["slice"] = item["slice"]
        row["status"] = item["status"]
        row["hit"] = _item_hit(row)
        rows.append(row)

    labeled = [r for r in rows if r["status"] == "labeled"]
    agg = ev_metrics.aggregate(labeled)
    by_slice = {}
    for slice_name in sorted({r["slice"] for r in labeled}):
        by_slice[slice_name] = ev_metrics.aggregate(
            [r for r in labeled if r["slice"] == slice_name])
    return rows, agg, by_slice, vector_available


def _simplify_agg(agg: dict) -> dict:
    """C2 baseline-формат: `{"recall_at_k", "source_name_hit_rate", "n"}`
    замість вкладеного `{"mean","n"}` з `ev_metrics.aggregate`."""
    return {
        "recall_at_k": agg["recall_at_k"]["mean"],
        "source_name_hit_rate": agg["source_name_hit_rate"]["mean"],
        "n": agg["total_items"],
    }


def _fmt_mean(d: dict) -> str:
    if not d or d.get("mean") is None:
        return "  —  "
    return f"{d['mean'] * 100:5.1f}%"


def _git_ignores(path: str) -> Optional[bool]:
    """True — git реально ігнорує `path`, False — реально відстежить,
    None — не вдалось визначити (шлях поза цим репозиторієм, git відсутній).
    Знахідка 3 (review-round-2): filename-конвенція `.local.` сама по собі
    неповна — `.gitignore` покриває лише `evals/*.local.json` напряму, не
    корінь репо і не вкладені каталоги (`evals/runs/…`) — питаємо git, а не
    вгадуємо шаблоном."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "check-ignore", "-q", "--",
             str(Path(path).resolve())],
            capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None  # 128 тощо — поза репо (напр. tmp_path тестів) чи інша помилка


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", required=True, help="Шлях до golden-set (JSONL або legacy JSON)")
    parser.add_argument("--db", default="whisper_history.db", help="Шлях до SQLite БД (default: whisper_history.db)")
    parser.add_argument("--k", default="8,12", help="Список k через кому (default: 8,12)")
    parser.add_argument("--min-recall", default=_DEFAULT_MIN_RECALL,
                         help=f"k=поріг через кому (default: {_DEFAULT_MIN_RECALL})")
    parser.add_argument("--max-drop", type=float, default=_DEFAULT_MAX_DROP,
                         help=f"Макс. падіння mean recall_at_k відносно лінії (default: {_DEFAULT_MAX_DROP})")
    parser.add_argument("--max-item-regressions", type=int, default=_DEFAULT_MAX_ITEM_REGRESSIONS,
                         help=f"Макс. к-сть пунктів hit→miss відносно лінії (default: {_DEFAULT_MAX_ITEM_REGRESSIONS})")
    parser.add_argument("--baseline", default=None, help="Базова лінія (C2) для порівняння і per-item diff")
    parser.add_argument("--write-baseline", default=None, help="Записати поточний прогін як нову базову лінію (C2)")
    parser.add_argument("--rerank", action="store_true",
                        help="Міряти З cross-encoder-реранкером (як RAG-чат при "
                             "RECALL_RERANK_ENABLED=1). Дефолт — без нього; прапорець "
                             "пишеться у провенанс, тож лінія без реранка і прогін з "
                             "ним не порівнюються мовчки")
    parser.add_argument("--sabotage", choices=["shuffle", "reverse"], default=None,
                         help="Детерміновано зламати пул кандидатів перед відсіканням (C6) — для перевірки самого гейта")
    parser.add_argument("--no-snapshot", action="store_true",
                         help="Не робити знімок (VACUUM INTO) — вимагає --yes-live для живої БД. "
                              "На НЕ-живій цілі захисна копія (_scratch_copy) все одно "
                              "застосовується попри цей прапорець (D16) — гейт скаже про це в рантаймі")
    parser.add_argument("--yes-live", action="store_true",
                         help="Дозволити --no-snapshot на живій БД (Config.DATABASE)")
    parser.add_argument("--json-out", default=None,
                         help="Записати повний результат у JSON (шлях мусить відповідати "
                              "конвенції приватності *.local.*, D10)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Друкувати лише підсумковий рядок")
    args = parser.parse_args(argv)

    # D10/знахідка 19: --json-out пише реальні питання і transcription_id архіву —
    # єдина нова поверхня без конвенції приватності. Вимагаємо *.local.* у назві
    # І (знахідка 3) звіряємо з git напряму: сама лише назва не доводить, що
    # шлях справді потрапляє під якесь правило .gitignore (корінь репо і
    # вкладені каталоги на кшталт evals/runs/ під `evals/*.local.json` не
    # підпадають). Якщо перевірити не вдалось (шлях поза репо — тести на
    # tmp_path, чи git відсутній), рішення лишається на конвенції імені.
    if args.json_out:
        name_ok = ".local." in Path(args.json_out).name
        git_ignored = _git_ignores(args.json_out)
        if not name_ok or git_ignored is False:
            print(f"[gate] --json-out {args.json_out!r} не відповідає конвенції приватності "
                  "*.local.* (напр. evals/gate_run.local.json) — пише реальні питання й "
                  "transcription_id архіву в шлях без gitignore-правила.", file=sys.stderr)
            return 2

    if not os.path.exists(args.db):
        print(f"[gate] БД не знайдено: {args.db}", file=sys.stderr)
        return 2

    try:
        ks = _parse_k_list(args.k)
        min_recall = _parse_min_recall(args.min_recall)
    except ValueError as exc:
        print(f"[gate] {exc}", file=sys.stderr)
        return 2

    db = args.db
    if args.no_snapshot:
        if is_live_db(db) and not args.yes_live:
            print(f"[gate] Відмова: {db!r} — бойова БД (Config.DATABASE). "
                  "--no-snapshot без --yes-live заборонено — гейт має ганятись на знімку.",
                  file=sys.stderr)
            return 2
        search_db = db
    elif is_live_db(db):
        search_db = ev_snapshot.auto_snapshot(
            db, protect=_protected_snapshot_paths(args.baseline, args.write_baseline))
    else:
        search_db = db

    try:
        conn = open_readonly(search_db)
    except Exception as exc:  # noqa: BLE001 — довільна причина відкриття (пошкоджений файл тощо) → 2, не крах
        print(f"[gate] не вдалось відкрити {search_db}: {exc}", file=sys.stderr)
        return 2
    try:
        golden_items = golden_io.read_golden(args.golden, conn=conn)
    except golden_io.GoldenSetError as exc:
        print(f"[gate] {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[gate] golden-set {args.golden!r}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if not golden_items:
        print(f"[gate] golden-set {args.golden!r} порожній.", file=sys.stderr)
        return 2

    # D6/ADR-003 (знахідка 7): жоден вимір не тримає WAL-rw зʼєднання проти
    # файлу, який гейт же й звітує як `db_snapshot` — retrieval.search іде
    # проти тимчасової копії. Єдиний легальний обхід — `--no-snapshot
    # --yes-live`, той самий, що вже документований у docstring модуля.
    # D16: обхід легальний ЛИШЕ коли ціль справді жива БД — сама пара
    # прапорців нічого не доводить (`is_live_db` вище перевіряє це тільки
    # щоб відмовити `--no-snapshot` БЕЗ `--yes-live` на живій БД, рядок 295;
    # на будь-якій НЕ-живій цілі, наведеній тими самими прапорцями, — знімку
    # чи довільному файлу — `_scratch_copy` лишається обовʼязковим).
    skip_scratch = args.no_snapshot and args.yes_live and is_live_db(db)
    # Знахідка 15: `--no-snapshot` мовчки не звільняв від захисної копії на
    # НЕ-живій цілі (D16) — довідка тепер каже про це, і рантайм теж мусить.
    if args.no_snapshot and not skip_scratch:
        print(f"[gate] --no-snapshot: захисна копія (_scratch_copy) все одно "
              f"застосовується — {db!r} не жива БД (Config.DATABASE), обхід "
              "легальний лише на живій цілі разом з --yes-live.", file=sys.stderr)
    measure_cm = contextlib.nullcontext(search_db) if skip_scratch else _scratch_copy(search_db)

    rows_by_k: dict[int, dict[str, dict]] = {}
    agg_by_k: dict[int, dict] = {}
    by_slice_by_k: dict[int, dict] = {}
    vector_available: Optional[bool] = None
    try:
        with measure_cm as measure_db:
            for k in ks:
                rows, agg, by_slice, vec_avail = _run_k(
                    golden_items, measure_db, k, sabotage=args.sabotage,
                    rerank=args.rerank)
                rows_by_k[k] = {r["id"]: r for r in rows}
                agg_by_k[k] = agg
                by_slice_by_k[k] = by_slice
                if vector_available is None:
                    vector_available = vec_avail
    except Exception as exc:  # noqa: BLE001 — інфраструктура (модель не піднялась, знімок
        # побитий, диск повний), не вердикт якості (D3/знахідка 4): 2, не 1 і не крах.
        print(f"[gate] збій під час заміру: {exc}", file=sys.stderr)
        return 2

    # D2/знахідка 2: прогін, де на якомусь k нема жодного пункту
    # status="labeled", — помилка даних, НІКОЛИ не PASS (навіть під -q). Саме
    # це видає build_golden без розмітки: усе unlabeled → n=0 на кожному k.
    empty_ks = [k for k in ks if agg_by_k[k]["total_items"] == 0]
    if empty_ks:
        print(f"[gate] на k={','.join(str(k) for k in empty_ks)} немає жодного пункту "
              "status=\"labeled\" — нема на чому рахувати вердикт (помилка даних, не PASS).",
              file=sys.stderr)
        return 2

    # D5 (знахідки 6, 13): провенанс поточного прогону — те, проти чого
    # звіряється базова лінія ПЕРЕД тим, як їй дозволено видати вердикт.
    snap_stat = os.stat(search_db)
    current_provenance = {
        "db_snapshot": {"path": os.path.abspath(search_db), "size": snap_stat.st_size,
                         "mtime": snap_stat.st_mtime},
        "vector_available": vector_available,
        "rerank": bool(args.rerank),
        # Борг Хвилі B (`leftovers.md`, Descoped): `rewrite` гейт не вмикає
        # прапорцем — `retrieval.search` читає гарячий env сам, — але прогін
        # МУСИТЬ сказати, з чим він знятий, інакше дві лінії з різним
        # RAG_QUERY_REWRITE зводяться мовчки. Резолвимо тим самим `env_bool`,
        # що й retrieval.py:718 (ADR-008: одна реалізація на прапорець).
        "rewrite": _settings.env_bool("RAG_QUERY_REWRITE"),
    }

    baseline = None
    if args.baseline:
        try:
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[gate] базова лінія {args.baseline!r}: {exc}", file=sys.stderr)
            return 2

        # Знахідка 6 (review-round-2): провенанс мусить покривати НАБІР, що
        # вимірюється, а не лише БД — інакше лінія, знята над іншим
        # golden-set (інші id/n), приймається як порівнянна лише тому, що
        # знімок і vector_available/rerank збіглись, і mean_drop рахується
        # проти чужих пунктів.
        current_ids = sorted({item_id for k in ks for item_id in rows_by_k[k]})
        base_snap = baseline.get("db_snapshot")
        provenance_ok = (
            isinstance(base_snap, dict)
            and base_snap.get("path") == current_provenance["db_snapshot"]["path"]
            and base_snap.get("size") == current_provenance["db_snapshot"]["size"]
            and base_snap.get("mtime") == current_provenance["db_snapshot"]["mtime"]
            and baseline.get("vector_available") == current_provenance["vector_available"]
            and baseline.get("rerank") == current_provenance["rerank"]
            and sorted((baseline.get("items") or {}).keys()) == current_ids
        )
        if not provenance_ok:
            print(f"[gate] базова лінія {args.baseline!r} знята за іншим провенансом "
                  "(знімок, vector_available/rerank або сам набір пунктів не збігається з "
                  "поточним прогоном) — порівняння відмовлено, вердикт не видається (D5). "
                  "Перезапишіть лінію --write-baseline на цьому наборі.", file=sys.stderr)
            return 2

    # Знахідка 1 (review-round-2), історія 11: дві двері до PASS, що нічого
    # не перевірив. `checked_this_k` фіксує, чи бодай ОДНА змістовна
    # перевірка реально відбулась на цьому k — абсолютний поріг проти
    # реального числа (recall_at_k АБО, за його відсутності,
    # source_name_hit_rate — двері (б)) або звірка з лінією (mean_drop
    # проти реального числа, або хоч один пункт зі спільним base_row —
    # двері (а), де лінія знята на іншому k). Якщо жоден k не пройшов ЖОДНОЇ
    # перевірки — вердикту немає (2), а не мовчазний PASS.
    failures: list[str] = []
    diff_lines: list[str] = []
    unchecked_ks: list[int] = []
    for k in ks:
        checked_this_k = False
        recall_mean = agg_by_k[k]["recall_at_k"]["mean"]
        hit_rate_mean = agg_by_k[k]["source_name_hit_rate"]["mean"]
        min_r = min_recall.get(k)
        if min_r is not None:
            if recall_mean is not None:
                checked_this_k = True
                if recall_mean < min_r:
                    failures.append(f"k={k}: recall_at_k {recall_mean:.3f} < min-recall {min_r:.3f}")
            elif hit_rate_mean is not None:
                # Двері (б): пункти розмічені лише `expected_source_name_contains`
                # (README радить це як стійкішу до дрейфу id альтернативу)
                # лишають recall_at_k.mean = None на кожному пункті — поріг
                # тоді звіряється проти source_name_hit_rate, інакше цей k не
                # перевіряється взагалі і тотальний промах мовчки минається.
                checked_this_k = True
                if hit_rate_mean < min_r:
                    failures.append(
                        f"k={k}: source_name_hit_rate {hit_rate_mean:.3f} < min-recall {min_r:.3f}")

        if baseline is not None:
            base_agg = (baseline.get("aggregate") or {}).get(str(k)) or {}
            base_recall = base_agg.get("recall_at_k")
            if base_recall is not None and recall_mean is not None:
                checked_this_k = True
                drop = base_recall - recall_mean
                if drop > args.max_drop:
                    failures.append(f"k={k}: mean_drop {drop:.3f} > max-drop {args.max_drop:.3f}")

            base_items = baseline.get("items") or {}
            regressions = 0
            for item_id, row in rows_by_k[k].items():
                base_row = (base_items.get(item_id) or {}).get(str(k))
                if base_row is None:
                    continue
                checked_this_k = True
                was_recall, now_recall = base_row.get("recall"), row.get("recall_at_k")
                was_hit, now_hit = base_row.get("hit"), row.get("hit")
                if was_recall != now_recall or was_hit != now_hit:
                    diff_lines.append(f"{item_id} k={k}: {was_recall}→{now_recall}")
                if was_hit is True and now_hit is False:
                    regressions += 1
            if regressions > args.max_item_regressions:
                failures.append(
                    f"k={k}: item_regressions {regressions} > max-item-regressions {args.max_item_regressions}")

        if not checked_this_k:
            unchecked_ks.append(k)

    if unchecked_ks:
        print(f"[gate] на k={','.join(str(k) for k in unchecked_ks)} немає жодної змістовної "
              "перевірки — ні абсолютного порогу (--min-recall) проти обчисленої метрики, ні "
              "порівнюваного запису в базовій лінії для цього k — вердикту немає (помилка "
              "конфігурації, не PASS).", file=sys.stderr)
        return 2

    verdict = "PASS" if not failures else "FAIL"

    # D12/знахідка 15: --write-baseline НЕ пише лінію на вердикті FAIL —
    # регресований прогін інакше мовчки стає новою істиною для порівняння.
    if args.write_baseline and verdict == "PASS":
        all_ids = sorted({item_id for k in ks for item_id in rows_by_k[k]})
        baseline_out = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "db_snapshot": current_provenance["db_snapshot"],
            "vector_available": current_provenance["vector_available"],
            "rerank": current_provenance["rerank"],
            "k": ks,
            "items": {
                item_id: {
                    str(k): {"recall": rows_by_k[k][item_id]["recall_at_k"],
                             "hit": rows_by_k[k][item_id]["hit"]}
                    for k in ks if item_id in rows_by_k[k]
                }
                for item_id in all_ids
            },
            "aggregate": {str(k): _simplify_agg(agg_by_k[k]) for k in ks},
            "by_slice": {
                str(k): {s: _simplify_agg(a) for s, a in by_slice_by_k[k].items()}
                for k in ks
            },
        }
        with open(args.write_baseline, "w", encoding="utf-8") as f:
            json.dump(baseline_out, f, ensure_ascii=False, indent=2, sort_keys=True)
    elif args.write_baseline:
        print(f"[gate] --write-baseline пропущено: вердикт FAIL "
              f"(лінія {args.write_baseline!r} не перезаписана регресованим прогоном).",
              file=sys.stderr)

    if not args.quiet:
        print()
        print(f"[gate] БД (пошук): {search_db}" + (f"  (знімок з {db})" if search_db != db else ""))
        labeled_n = sum(1 for i in golden_items if i["status"] == "labeled")
        print(f"[gate] golden-set: {args.golden} ({len(golden_items)} записів, {labeled_n} labeled)")
        print("-" * 60)
        for k in ks:
            agg = agg_by_k[k]
            print(f"k={k:<3} recall_at_k={_fmt_mean(agg['recall_at_k'])}  "
                  f"source_name_hit_rate={_fmt_mean(agg['source_name_hit_rate'])}  "
                  f"n={agg['total_items']}")
            for slice_name, s_agg in by_slice_by_k[k].items():
                print(f"    {slice_name:<8} recall_at_k={_fmt_mean(s_agg['recall_at_k'])}  n={s_agg['total_items']}")
        if diff_lines:
            print("-" * 60)
            print("Diff vs baseline:")
            for line in diff_lines:
                print(f"  {line}")
        print("-" * 60)
        if args.write_baseline and verdict == "PASS":
            print(f"[gate] базову лінію записано у {args.write_baseline}")
        for f_ in failures:
            print(f"  FAIL: {f_}")

    # Конфігурація — у тому ж рядку, що й вердикт: знахідка 17.09.2026 у тому й
    # була, що прогони без реранкера читали як бойові числа (50.0% проти 78.6%
    # на k=8). Рядок під `-q` єдиний, тож підпис мусить бути саме тут.
    print(f"[gate] {verdict} k={','.join(str(k) for k in ks)}"
          f" [rerank={'on' if current_provenance['rerank'] else 'off'},"
          f" rewrite={'on' if current_provenance['rewrite'] else 'off'}]"
          + (f" — {'; '.join(failures)}" if failures else ""))

    if args.json_out:
        out = {
            "db": os.path.abspath(search_db),
            "golden": args.golden,
            "k": ks,
            "verdict": verdict,
            "failures": failures,
            "aggregate": {str(k): _simplify_agg(agg_by_k[k]) for k in ks},
            "by_slice": {
                str(k): {s: _simplify_agg(a) for s, a in by_slice_by_k[k].items()}
                for k in ks
            },
            "per_item": {str(k): list(rows_by_k[k].values()) for k in ks},
            # Без цього два `--json-out` зводяться через `evals.compare` без
            # жодної згадки, з якою конфігурацією знятий кожен (знахідка
            # 17.09.2026). `compare` читає лише aggregate/per_item — зайвий
            # ключ його не чіпає, але прогін тепер носить свій підпис у файлі.
            "provenance": current_provenance,
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
