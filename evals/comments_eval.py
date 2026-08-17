"""Замір впливу шару коментарів на видачу (Волна 2.5).

ЩО САМЕ ТУТ МІРЯЄТЬСЯ І ЧОМУ НЕ recall@k.

Звичайний recall@k відповідає на питання «чи знайшли потрібний запис». Шар
коментарів ставить інше питання: «чи не зіпсував буст те, що працювало». Це
різні виміри, і для другого golden-set із очікуваними id не потрібен — потрібен
ДИФ між двома прогонами того самого запиту:

  A. `include_comments=False` — видача, якою вона була до шару;
  B. `include_comments=True`  — видача з коментарями і бустом.

Три числа з цього дифу:

  * **drift** — скільки записів з A випало з B. Це і є «витіснив щось корисне».
  * **comment_slots** — скільки слотів у B зайняли самі коментарі.
  * **rank_shift** — на скільки позицій зсунулись ті, хто вцілів.

ДВА k ОБОВʼЯЗКОВІ. На k=8 і k=12 вердикт уже одного разу перевернувся
(memory/eval-verdict-flips-with-k): вузьке вікно карає будь-яке додавання, бо
слотів мало, широке — прощає. Один k дає впевнену відповідь, яка не витримує
перевірки другим.

ІНВАРІАНТ ПОРОЖНЬОГО АРХІВУ. Поки коментарів немає, A і B мусять збігатися
ПОБАЙТОВО. Якщо ні — шар щось міняє в кожному запиті ще до того, як у ньому
зʼявився бодай один коментар, і це не «невеликий вплив», а баг. Режим
`--invariant` перевіряє саме це і повертає ненульовий код при розбіжності.

РЕЖИМ --seed. Без коментарів у БД міряти зсув нема на чому. `--seed N` садить
на КОПІЮ бази синтетичні коментарі, зроблені з тексту РЕАЛЬНИХ чанків, які
видача вже повертає (тобто максимально «правдоподібні» — вони гарантовано
релевантні запиту й тому дають ВЕРХНЮ оцінку зсуву, гіршу за реальність).
Бойову БД скрипт не чіпає ніколи: `--db` має вказувати на копію.

Використання:

    # 1. Інваріант на бойовій БД (безпечно, лише читання):
    python -m evals.comments_eval --golden evals/golden_set.local.json --invariant

    # 2. Зсув на копії з посадженими коментарями:
    python -m evals.comments_eval --golden evals/golden_set.local.json \\
        --db copy.db --seed 2 --json-out evals/comments_drift.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: Обидва k міряються завжди — див. модульний докстрінг.
K_VALUES = (8, 12)


def _load_questions(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", data) if isinstance(data, dict) else data
    return [{"id": it.get("id") or f"q{i}", "question": it["question"],
             "category_id": it.get("category_id")}
            for i, it in enumerate(items) if it.get("question")]


def _key(ch: dict):
    """Ідентичність елемента видачі для дифу.

    Для чанка архіву — його id; для коментаря — окремий простір, щоб
    відʼємні id не змішувались із додатними (та сама причина, що в retrieval).
    """
    if ch.get("source_type") == "comment":
        return ("comment", ch.get("comment_id"))
    return ("chunk", ch.get("chunk_id"))


def _run_pair(db_path: str, item: dict, k: int) -> dict:
    """Один запит, два прогони, диф між ними."""
    from app.services import retrieval

    base = retrieval.search(db_path, item["question"], top_k=k,
                            category_id=item.get("category_id"),
                            include_comments=False)["chunks"]
    with_cm = retrieval.search(db_path, item["question"], top_k=k,
                               category_id=item.get("category_id"),
                               include_comments=True)["chunks"]

    base_keys = [_key(c) for c in base]
    new_keys = [_key(c) for c in with_cm]
    base_pos = {kk: i for i, kk in enumerate(base_keys)}
    new_pos = {kk: i for i, kk in enumerate(new_keys)}

    dropped = [kk for kk in base_keys if kk not in new_pos]
    comment_slots = [kk for kk in new_keys if kk[0] == "comment"]
    # Зсув рангу рахуємо лише по тих, хто лишився в обох — інакше «зник»
    # порахувався б і як drift, і як гігантський зсув.
    shifts = [abs(new_pos[kk] - base_pos[kk]) for kk in base_keys if kk in new_pos]

    return {
        "id": item["id"], "k": k,
        "identical": base_keys == new_keys,
        "drift": len(dropped),
        "comment_slots": len(comment_slots),
        "max_rank_shift": max(shifts) if shifts else 0,
        "mean_rank_shift": round(sum(shifts) / len(shifts), 2) if shifts else 0.0,
        "dropped": [list(x) for x in dropped],
        "top1_changed": bool(base_keys[:1] != new_keys[:1]),
    }


def _aggregate(rows: list[dict], k: int) -> dict:
    at_k = [r for r in rows if r["k"] == k]
    n = len(at_k) or 1
    return {
        "k": k, "items": len(at_k),
        "identical": sum(1 for r in at_k if r["identical"]),
        "drift_total": sum(r["drift"] for r in at_k),
        "drift_mean": round(sum(r["drift"] for r in at_k) / n, 2),
        "comment_slots_total": sum(r["comment_slots"] for r in at_k),
        "top1_changed": sum(1 for r in at_k if r["top1_changed"]),
        "max_rank_shift": max((r["max_rank_shift"] for r in at_k), default=0),
    }


# ------------------------------------------------------------------ seeding

def seed_comments(db_path: str, questions: list[dict], per_question: int) -> dict:
    """Посадити синтетичні коментарі на КОПІЮ бази.

    Джерело тексту — реальні чанки, які видача вже повертає на ці ж питання.
    Це навмисно найгірший випадок: такі коментарі гарантовано релевантні, тож
    зсув, який вони дадуть, — верхня межа, а не типова величина.
    """
    from app.services import comments, retrieval

    made = 0
    for item in questions:
        chunks = retrieval.search(db_path, item["question"], top_k=per_question,
                                  category_id=item.get("category_id"),
                                  include_comments=False)["chunks"]
        for ch in chunks[:per_question]:
            tid = ch.get("transcription_id")
            if not tid:
                continue
            body = (ch.get("text") or "")[:400].strip()
            if not body:
                continue
            c = comments.create(db_path, "transcription", tid,
                                f"Уточнення власника: {body}", kind="correction",
                                source="import", check_target=False)
            comments.index_comment(db_path, c["id"])
            made += 1
    return {"seeded": made}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--golden", required=True)
    p.add_argument("--db", default="whisper_history.db")
    p.add_argument("--invariant", action="store_true",
                   help="вимагати, щоб A і B збігались (архів без коментарів)")
    p.add_argument("--seed", type=int, default=0,
                   help="посадити N коментарів на питання ПЕРЕД заміром (лише копія БД!)")
    p.add_argument("--json-out", default=None)
    a = p.parse_args(argv)

    questions = _load_questions(a.golden)
    if not questions:
        print("golden-set порожній", file=sys.stderr)
        return 2

    if a.seed:
        if os.path.abspath(a.db) == os.path.abspath(
                os.path.join(_PROJECT_ROOT, "whisper_history.db")):
            print("ВІДМОВА: --seed пише в БД. Вкажіть КОПІЮ через --db.",
                  file=sys.stderr)
            return 2
        print(f"seed: {seed_comments(a.db, questions, a.seed)}")

    rows = [_run_pair(a.db, item, k) for k in K_VALUES for item in questions]
    aggs = [_aggregate(rows, k) for k in K_VALUES]

    print(f"\n{'k':>3} {'питань':>7} {'ідентично':>10} {'drift':>7} "
          f"{'drift/зпт':>10} {'слотів CM':>10} {'top1≠':>6} {'max зсув':>9}")
    for g in aggs:
        print(f"{g['k']:>3} {g['items']:>7} {g['identical']:>10} {g['drift_total']:>7} "
              f"{g['drift_mean']:>10} {g['comment_slots_total']:>10} "
              f"{g['top1_changed']:>6} {g['max_rank_shift']:>9}")

    changed = [r for r in rows if not r["identical"]]
    if changed:
        print(f"\nЗмінені запити ({len(changed)}):")
        for r in sorted(changed, key=lambda x: -x["drift"])[:12]:
            print(f"  k={r['k']:>2} {r['id']:<28} drift={r['drift']} "
                  f"слотів CM={r['comment_slots']} max зсув={r['max_rank_shift']}")

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump({"aggregates": aggs, "per_item": rows}, f,
                      ensure_ascii=False, indent=2)
        print(f"\n→ {a.json_out}")

    if a.invariant:
        if changed:
            print(f"\n❌ ІНВАРІАНТ ПОРУШЕНО: {len(changed)} запитів змінились, "
                  f"хоча коментарів в архіві не має бути.", file=sys.stderr)
            return 1
        print("\n✅ Інваріант тримається: без коментарів видача незмінна.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
