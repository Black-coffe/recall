"""Bench of the copilot's local LLM dispatcher (Phase 19, Step 0 - feasibility gate).

Runs N realistic dispatcher calls (direction profile + dialogue window +
mock archive chunks -> a structured JSON decision) and prints:
  - latency p50/p95 (wall) per decision;
  - generation speed (tok/s) and prompt processing speed (tok/s);
  - input prompt size in tokens (the model's real tokenizer);
  - share of valid JSON.

This answers the main question of Step 0: does Qwen-14B produce structured JSON
on a 3090 with acceptable latency - and (if run DURING recording) does it fit
alongside whisper.

Run:
    .venv/Scripts/python.exe scripts/bench_local_llm.py
    .venv/Scripts/python.exe scripts/bench_local_llm.py --n 30 --model qwen2.5:14b-instruct-q5_K_M

GPU-contention bench (sub-step 0.4):
    1) start a recording in the browser (whisper medium active);
    2) run this script in parallel;
    3) watch `nvidia-smi` (VRAM) and whether live segments get stuck in the widget.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

# Windows charmap gotcha: the default console (cp1252) fails on Cyrillic in print().
# Force UTF-8 output so the script does not crash with UnicodeEncodeError (see memory/gotchas).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Project on sys.path so the service imports when run from the root or from scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import local_llm  # noqa: E402


# --- Realistic dispatcher prompt (a fund case with a budget contradiction) -----------

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

# Mock chunks that RAG "found" in the archive (ids as in the real chunks table).
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


# --- Metrics --------------------------------------------------------------------

def _ns_to_s(ns) -> float:
    return (ns or 0) / 1e9


def pct(arr: list[float], p: float) -> float:
    return float(np.percentile(arr, p)) if arr else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Bench of the copilot's local LLM dispatcher")
    ap.add_argument("--n", type=int, default=20, help="number of calls (default 20)")
    ap.add_argument("--model", default=None, help="override LOCAL_LLM_MODEL")
    ap.add_argument("--url", default=None, help="override LOCAL_LLM_URL")
    ap.add_argument("--max-tokens", type=int, default=512, help="num_predict (default 512)")
    args = ap.parse_args()

    if args.model:
        local_llm.LOCAL_LLM_MODEL = args.model
    if args.url:
        local_llm.LOCAL_LLM_URL = args.url.rstrip("/")

    print("=" * 72)
    print("  Bench of the copilot's local LLM dispatcher (Phase 19, Step 0)")
    print("=" * 72)
    print(f"  URL:   {local_llm.LOCAL_LLM_URL}")
    print(f"  Model: {local_llm.LOCAL_LLM_MODEL}")
    print(f"  num_ctx={local_llm.LOCAL_LLM_NUM_CTX}  max_tokens={args.max_tokens}  n={args.n}")
    print("-" * 72)

    ok, reason = local_llm.availability(force=True)
    if not ok:
        print(f"  [x] Unavailable: {reason}")
        print()
        print("  How to prepare:")
        print("    1) Install Ollama: https://ollama.com/download")
        print("    2) ollama serve            (usually starts itself as a service)")
        print(f"    3) ollama pull {local_llm.LOCAL_LLM_MODEL}")
        return 1
    print(f"  [ok] Available: {reason}")
    print(f"  Loaded models: {', '.join(local_llm.list_models()) or '-'}")
    print("-" * 72)

    # Warm-up (the first load into VRAM can take tens of seconds).
    print("  Model warm-up (loading into VRAM)...", flush=True)
    t0 = time.perf_counter()
    if not local_llm.warmup(timeout=180.0):
        print("  [x] Warm-up failed - see the log above.")
        return 1
    print(f"  [ok] Warmed up in {time.perf_counter() - t0:.1f} s")
    print("-" * 72)

    prompt = build_prompt()
    wall, gen_tps, prompt_tps, out_tokens, in_tokens = [], [], [], [], []
    valid_json = 0
    found_contradiction = 0
    sample_out = None

    print(f"  Running {args.n} calls...", flush=True)
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
            print(f"    [{i + 1:>2}/{args.n}] [x] {e}")
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

        # Did it catch the budget contradiction (40k vs 60k) - a quality signal, not just speed.
        kinds = [ins.get("kind") for ins in (data.get("candidate_insights") or [])]
        if "contradiction" in kinds:
            found_contradiction += 1
        if sample_out is None:
            sample_out = data
        print(f"    [{i + 1:>2}/{args.n}] {elapsed:5.2f} s | {ec:>3} out.tok | "
              f"{(ec / ed if ed else 0):5.1f} tok/s | insights: {len(kinds)}", flush=True)

    print("-" * 72)
    print("  RESULTS")
    print(f"    Valid JSON:           {valid_json}/{args.n}")
    print(f"    Caught contradiction: {found_contradiction}/{valid_json or 1} (quality sanity-check)")
    print(f"    Wall latency:         p50={pct(wall, 50):.2f}s  p95={pct(wall, 95):.2f}s  "
          f"min={min(wall):.2f}  max={max(wall):.2f}" if wall else "    Latency: -")
    if gen_tps:
        print(f"    Generation:           {np.mean(gen_tps):.1f} tok/s (average)")
    if prompt_tps:
        print(f"    Prompt processing:    {np.mean(prompt_tps):.0f} tok/s (average)")
    if in_tokens:
        print(f"    Input prompt:         ~{int(np.mean(in_tokens))} tokens (real tokenizer)")
    if out_tokens:
        print(f"    Output:               ~{int(np.mean(out_tokens))} tokens/decision (average)")
    print("-" * 72)

    # Step 0 target: p95 < 5s and 100% valid JSON -> gate passed.
    p95 = pct(wall, 95)
    gate_ok = valid_json == args.n and p95 < 5.0
    print(f"  GATE (p95<5s and 100% JSON): {'[ok] PASSED' if gate_ok else '[x] reconsider the model/quant'}")
    if not gate_ok:
        print("    If p95 is too big or the JSON breaks - try a lighter model")
        print("    (qwen2.5:7b-instruct) or a smaller quant, and repeat the bench.")
    print()
    print("  Reminder: for the GPU-contention bench repeat THIS run during")
    print("  an active recording (whisper medium) and watch `nvidia-smi` and whether")
    print("  live segments get stuck in the recording widget.")

    if sample_out is not None:
        print("-" * 72)
        print("  Example decision (1st valid):")
        print(json.dumps(sample_out, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
