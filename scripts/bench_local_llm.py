"""Бенч локального LLM-диспетчера ко-пілота (Phase 19, Крок 0 — gate здійсненності).

Прогоняє N реалістичних диспетчер-викликів (профіль напрямку + вікно діалогу +
мок-чанки архіву → структуроване JSON-рішення) і друкує:
  - латентність p50/p95 (wall) на одне рішення;
  - швидкість генерації (tok/s) і обробки промпта (tok/s);
  - розмір вхідного промпта в токенах (реальний токенайзер моделі);
  - частку валідного JSON.

Це відповідає на головне питання Кроку 0: чи видає Qwen-14B структурований JSON
на 3090 з прийнятною латентністю — і (якщо ганяти ПІД ЧАС запису) чи влазить
поряд з whisper.

Запуск:
    .venv/Scripts/python.exe scripts/bench_local_llm.py
    .venv/Scripts/python.exe scripts/bench_local_llm.py --n 30 --model qwen2.5:14b-instruct-q5_K_M

Бенч GPU-контеншену (під-крок 0.4):
    1) у браузері почати запис (whisper medium активний);
    2) паралельно запустити цей скрипт;
    3) дивитись `nvidia-smi` (VRAM) і чи не застрягають live-сегменти у віджеті.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

# Windows charmap gotcha: дефолтна консоль (cp1252) падає на кирилиці у print().
# Форсуємо UTF-8 вивід, щоб скрипт не крешився UnicodeEncodeError (див. memory/gotchas).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Проект на sys.path, щоб імпортнути сервіс при запуску з кореня чи зі scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import local_llm  # noqa: E402


# --- Реалістичний диспетчер-промпт (фонд-кейс із протиріччям бюджету) -----------

DISPATCHER_SYSTEM = (
    "Ти — локальний диспетчер живого ко-пілота дзвінка. Слухаєш діалог у реальному "
    "часі й вирішуєш, що варто підказати оператору. Відповідай ВИКЛЮЧНО валідним "
    "JSON за схемою. Шукай: протиріччя з архівом (contradiction), доречні питання "
    "(question), уточнення (clarification), важливі факти з архіву (fact). Якщо "
    "нічого вартого — порожній масив candidate_insights. escalate=true лише коли "
    "впевненість висока або тема критична."
)

DIRECTION_PROFILE = (
    "Напрямок: ФОНД. Контекст: інвестиційні проєкти, бюджети, терміни, домовленості "
    "з командою та партнерами. Оператору важливі розбіжності у цифрах і термінах "
    "порівняно з тим, що вже зафіксовано в архіві."
)

DIALOGUE_WINDOW = (
    "[Олег]: Слухай, по проєкту «Оріон» давай фіналізуємо бюджет на другий квартал.\n"
    "[Марина]: Так, я закладаю шістдесят тисяч доларів на розробку і маркетинг разом.\n"
    "[Олег]: Шістдесят? Окей, тоді з цієї суми йдемо до партнерів.\n"
    "[Марина]: Плюс орієнтовно запуск переносимо на середину липня, не на червень."
)

# Мок-чанки, які «знайшов» RAG із архіву (id як у реальній таблиці chunks).
ARCHIVE_CHUNKS = (
    "[123] Мітинг «Планування Q2 / Оріон» (2026-04-18), спікер: Марина, ~12:30\n"
    "Домовились: бюджет проєкту «Оріон» на другий квартал — сорок тисяч доларів "
    "(розробка 30k + маркетинг 10k). Запуск — кінець червня.\n\n"
    "[456] Документ «Оріон_план.xlsx» (2026-04-20), лист 1\n"
    "Кошторис Q2: разом 40 000 USD. Дата релізу: 28 червня 2026.\n\n"
    "[789] Мітинг «Синхрон з партнерами» (2026-05-05), спікер: Олег, ~03:10\n"
    "Партнери очікують реліз «Оріон» у червні; бюджет узгоджено на рівні 40k."
)

DISPATCHER_SCHEMA = {
    "type": "object",
    "properties": {
        "topic_label": {"type": "string"},
        "needs_retrieval": {"type": "boolean"},
        "retrieval_query": {"type": "string"},
        "candidate_insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["contradiction", "question", "clarification", "fact"],
                    },
                    "text": {"type": "string"},
                    "evidence_chunk_ids": {"type": "array", "items": {"type": "integer"}},
                    "confidence": {"type": "number"},
                    "escalate": {"type": "boolean"},
                },
                "required": ["kind", "text", "confidence", "escalate"],
            },
        },
    },
    "required": ["topic_label", "needs_retrieval", "candidate_insights"],
}


def build_prompt() -> str:
    return (
        f"ПРОФІЛЬ НАПРЯМКУ:\n{DIRECTION_PROFILE}\n\n"
        f"ОСТАННЄ ВІКНО ДІАЛОГУ:\n{DIALOGUE_WINDOW}\n\n"
        f"ЗНАЙДЕНО В АРХІВІ (можливі докази):\n{ARCHIVE_CHUNKS}\n\n"
        "Проаналізуй вікно проти архіву і поверни рішення за схемою."
    )


# --- Метрики --------------------------------------------------------------------

def _ns_to_s(ns) -> float:
    return (ns or 0) / 1e9


def pct(arr: list[float], p: float) -> float:
    return float(np.percentile(arr, p)) if arr else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Бенч локального LLM-диспетчера ко-пілота")
    ap.add_argument("--n", type=int, default=20, help="к-сть викликів (default 20)")
    ap.add_argument("--model", default=None, help="override LOCAL_LLM_MODEL")
    ap.add_argument("--url", default=None, help="override LOCAL_LLM_URL")
    ap.add_argument("--max-tokens", type=int, default=512, help="num_predict (default 512)")
    args = ap.parse_args()

    if args.model:
        local_llm.LOCAL_LLM_MODEL = args.model
    if args.url:
        local_llm.LOCAL_LLM_URL = args.url.rstrip("/")

    print("=" * 72)
    print("  Бенч локального LLM-диспетчера ко-пілота (Phase 19, Крок 0)")
    print("=" * 72)
    print(f"  URL:   {local_llm.LOCAL_LLM_URL}")
    print(f"  Model: {local_llm.LOCAL_LLM_MODEL}")
    print(f"  num_ctx={local_llm.LOCAL_LLM_NUM_CTX}  max_tokens={args.max_tokens}  n={args.n}")
    print("-" * 72)

    ok, reason = local_llm.availability(force=True)
    if not ok:
        print(f"  ✗ Недоступно: {reason}")
        print()
        print("  Як підготувати:")
        print("    1) Встановити Ollama: https://ollama.com/download")
        print("    2) ollama serve            (зазвичай стартує сам як служба)")
        print(f"    3) ollama pull {local_llm.LOCAL_LLM_MODEL}")
        return 1
    print(f"  ✓ Доступно: {reason}")
    print(f"  Завантажені моделі: {', '.join(local_llm.list_models()) or '—'}")
    print("-" * 72)

    # Прогрів (перше завантаження у VRAM може зайняти десятки секунд).
    print("  Прогрів моделі (завантаження у VRAM)…", flush=True)
    t0 = time.perf_counter()
    if not local_llm.warmup(timeout=180.0):
        print("  ✗ Прогрів не вдався — див. лог вище.")
        return 1
    print(f"  ✓ Прогріто за {time.perf_counter() - t0:.1f} с")
    print("-" * 72)

    prompt = build_prompt()
    wall, gen_tps, prompt_tps, out_tokens, in_tokens = [], [], [], [], []
    valid_json = 0
    found_contradiction = 0
    sample_out = None

    print(f"  Прогон {args.n} викликів…", flush=True)
    for i in range(args.n):
        t = time.perf_counter()
        try:
            res = local_llm.generate_json(
                prompt, schema=DISPATCHER_SCHEMA, system=DISPATCHER_SYSTEM,
                max_tokens=args.max_tokens, temperature=0.0, retries=0,
            )
            elapsed = time.perf_counter() - t
            valid_json += 1
            data, raw = res["data"], res["raw"]
        except local_llm.LocalLLMError as e:
            elapsed = time.perf_counter() - t
            print(f"    [{i + 1:>2}/{args.n}] ✗ {e}")
            wall.append(elapsed)
            continue

        wall.append(elapsed)
        ec, ed = raw.get("eval_count", 0), _ns_to_s(raw.get("eval_duration"))
        pc, pd = raw.get("prompt_eval_count", 0), _ns_to_s(raw.get("prompt_eval_duration"))
        if ed > 0:
            gen_tps.append(ec / ed)
        if pd > 0:
            prompt_tps.append(pc / pd)
        out_tokens.append(ec)
        in_tokens.append(pc)

        # Чи зловив протиріччя бюджету (40k vs 60k) — якісний сигнал, не лише швидкість.
        kinds = [ins.get("kind") for ins in (data.get("candidate_insights") or [])]
        if "contradiction" in kinds:
            found_contradiction += 1
        if sample_out is None:
            sample_out = data
        print(f"    [{i + 1:>2}/{args.n}] {elapsed:5.2f} с · {ec:>3} вих.ток · "
              f"{(ec / ed if ed else 0):5.1f} tok/s · інсайтів: {len(kinds)}", flush=True)

    print("-" * 72)
    print("  РЕЗУЛЬТАТИ")
    print(f"    Валідний JSON:        {valid_json}/{args.n}")
    print(f"    Зловив протиріччя:    {found_contradiction}/{valid_json or 1} (якісний sanity-check)")
    print(f"    Латентність wall:     p50={pct(wall, 50):.2f}с  p95={pct(wall, 95):.2f}с  "
          f"min={min(wall):.2f}  max={max(wall):.2f}" if wall else "    Латентність: —")
    if gen_tps:
        print(f"    Генерація:            {np.mean(gen_tps):.1f} tok/s (середнє)")
    if prompt_tps:
        print(f"    Обробка промпта:      {np.mean(prompt_tps):.0f} tok/s (середнє)")
    if in_tokens:
        print(f"    Вхідний промпт:       ~{int(np.mean(in_tokens))} токенів (реальний токенайзер)")
    if out_tokens:
        print(f"    Вихід:                ~{int(np.mean(out_tokens))} токенів/рішення (середнє)")
    print("-" * 72)

    # Орієнтир Кроку 0: p95 < 5с і 100% валідний JSON → gate пройдено.
    p95 = pct(wall, 95)
    gate_ok = valid_json == args.n and p95 < 5.0
    print(f"  GATE (p95<5с і 100% JSON): {'✓ ПРОЙДЕНО' if gate_ok else '✗ переглянути модель/квант'}")
    if not gate_ok:
        print("    Якщо p95 завеликий або JSON ламається — спробуйте легшу модель")
        print("    (qwen2.5:7b-instruct) або менший квант, і повторіть бенч.")
    print()
    print("  Нагадування: для бенчу GPU-контеншену повторіть ЦЕЙ запуск під час")
    print("  активного запису (whisper medium) і слідкуйте за `nvidia-smi` + чи не")
    print("  застрягають live-сегменти у віджеті запису.")

    if sample_out is not None:
        print("-" * 72)
        print("  Приклад рішення (1-й валідний):")
        print(json.dumps(sample_out, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
