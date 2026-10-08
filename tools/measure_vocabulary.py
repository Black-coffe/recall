#!/usr/bin/env python3
"""Замір словника імен і змішаних мов (K1, історії 02–03).

Висновків інструмент не робить: він готує матеріал, який власник переглядає й
судить, а підсумок рахує історія 03. Підкоманди:

    candidates  БД (mode=ro) → measure/fragments.md (+ fragments.json):
                ≥20 іменних фрагментів (в тексті стоїть аліас-помилка імені
                зі словника), ≥10 контрольних без імен, ≥10 вікон ≤60 с зі
                змішаною uk/ru мовою. Вибірка детермінована (SEED).
    run         ті самі фрагменти → варіанти через той самий
                ``transcribe_with_progress(decode_options=…)``, що й прод:
                імена/контроль — baseline / hotwords / initial_prompt,
                змішана — baseline / multilingual (вікно 30 с) / multilingual +
                ``chunk_length=10``; швидкість — ті самі три варіанти словника
                на 3 повних записах 10–30 хв. Виходи й час → measure/raw/*.json.
                Відмовляє, якщо в Recall іде запис.
    ab          raw/mixed.json → measure/ab.md (сліпі пари «А/Б») +
                measure/ab-key.json (ключ, до вердикту не відкривати).
    score       розмітка власника + raw → measure/report.md і рішення щодо
                дефолтів (історія 03).

Фрагмент запису довше 10 хв декодується так само, як декодувався б увесь запис:
batched-шляхом (менеджер обирає його за тривалістю файлу, тож тривалість
підставляється тривалістю джерела). Оригінали не чіпаються: ffmpeg ріже кліп
у тимчасову теку.

    .venv/Scripts/python.exe tools/measure_vocabulary.py candidates
    .venv/Scripts/python.exe tools/measure_vocabulary.py run
    .venv/Scripts/python.exe tools/measure_vocabulary.py ab
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import vocabulary  # noqa: E402 — легкий модуль, без torch
from app.utils.proc import NO_WINDOW  # noqa: E402

MEASURE_DIR = ROOT / "docs" / "specs" / "k1-name-vocabulary" / "measure"
SEED = 20261008
NAME_TARGET = 30          # з запасом: власник викреслить частину, лишитись має ≥20
CONTROL_TARGET = 12
MIXED_TARGET = 10
SPEED_RECORDINGS = 3
SPEED_MIN_S, SPEED_MAX_S = 10 * 60, 30 * 60
#: Повтори заміру швидкості: один прогін давав розкид +7…+16 % на тих самих записах.
SPEED_REPS = 3
#: Фрагменти беруться лише для перших імен словника: 40 імен = 174 токени
#: (замір плану на токенайзері large-v3-turbo) — гарантовано вміщаються в 223.
VOCAB_HEAD = 40
PER_ENTITY = 2
PAD_S = 1.0
MIXED_WINDOW_S = 60.0
CONTROL_MIN_S, CONTROL_MAX_S, CONTROL_MIN_WORDS = 4.0, 20.0, 8
BATCHED_FROM_S = 600.0    # поріг менеджера: довші файли йдуть batched-шляхом
DEFAULT_API = "http://127.0.0.1:5050"
DEFAULT_MODEL = "large-v3-turbo"

NAME_VARIANTS = ("baseline", "hotwords", "initial_prompt")
MIXED_VARIANTS = ("baseline", "ml30", "ml10")

_RU = re.compile(r"[ыэъё]")
_UK = re.compile(r"[іїєґ]")
_LATIN = re.compile(r"[A-Za-z]")
_REVIEWED = re.compile(r"^\s*-\s*\[[xX]\]\s*([NC]\d+)\b")


# ============================================================
# Дані (лише читання)
# ============================================================

def connect_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_records(conn) -> list[dict]:
    """Аудіо-записи з файлом на диску і сегментами (файл, запис; YouTube ні)."""
    out = []
    for r in conn.execute(
        "SELECT id, source_type, source_name, title, file_path, language, segments "
        "FROM transcriptions WHERE source_type IN ('file','recording') "
        "AND model_used IS NOT NULL AND segments IS NOT NULL AND deleted_at IS NULL ORDER BY id"
    ):
        if not r["file_path"] or not os.path.isfile(r["file_path"]):
            continue
        try:
            segs = json.loads(r["segments"]) or []
        except ValueError:
            continue
        if not segs:
            continue
        out.append({
            "tid": r["id"], "file": r["file_path"], "language": r["language"] or "uk",
            "name": r["title"] or r["source_name"], "segments": segs,
            "duration": float(segs[-1].get("end") or 0.0),
        })
    return out


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def looks_like_mangling(alias: str, canonical: str) -> bool:
    """Аліас схожий на спотворене імʼя, а не на інше слово, приліплене до сутності.

    Відстань Левенштейна між фонетичними ключами (``vocabulary.phonetic_keys``) —
    не більше половини ключа імені: «Акмеец» (brgts) при Acmecorp (brgds) —
    так; «штучний інтелект» при Anthropic, «Спікер 7» при «Руслан» — ні."""
    a_keys = vocabulary.phonetic_keys("".join(vocabulary._words(alias)))
    c_keys = set(vocabulary.phonetic_keys("".join(vocabulary._words(canonical))))
    for w in vocabulary._words(canonical):
        c_keys |= {k for k in vocabulary.phonetic_keys(w) if len(k) >= 2}
    return any(_levenshtein(a, c) <= max(1, len(c) // 2) for a in a_keys for c in c_keys)


def error_alias_targets(conn, names: list[str]) -> list[dict]:
    """Для імен словника (у порядку рангу) — їхні аліаси-помилки і звʼязані записи.

    Помилка — за ``vocabulary.is_error_alias``: відмінок і переклад («Адаму» при
    «Адам», «Гугл» при Google) помилкою не є. Для заміру лишаються лише аліаси,
    схожі на спотворене імʼя (:func:`looks_like_mangling`): на аліасі-сміттю
    («AdWords» при Google) словник нічого не може виправити."""
    by_name: dict[str, dict] = {}
    for name in names:
        by_name.setdefault(name.casefold(), {"canonical": name, "aliases": [], "tids": set()})
    marks = ",".join("?" * len(vocabulary.ENTITY_TYPES))
    rows = conn.execute(
        f"SELECT id, canonical_name FROM entities WHERE type IN ({marks})", vocabulary.ENTITY_TYPES)
    eids: dict[int, dict] = {}
    for eid, canonical in rows:
        target = by_name.get((canonical or "").strip().casefold())
        if target is not None:
            eids[eid] = target
    for eid, alias in conn.execute("SELECT entity_id, alias FROM entity_aliases"):
        target = eids.get(eid)
        if target and alias and vocabulary.is_error_alias(alias, target["canonical"]) \
                and looks_like_mangling(alias, target["canonical"]) and alias not in target["aliases"]:
            target["aliases"].append(alias)
    for eid, tid in conn.execute("SELECT entity_id, transcription_id FROM meeting_entities"):
        if eid in eids:
            eids[eid]["tids"].add(tid)
    return [by_name[n.casefold()] for n in names if by_name[n.casefold()]["aliases"]]


def _contains(text: str, alias: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(alias.casefold()) + r"(?!\w)", text.casefold()) is not None


def find_name_fragments(records: list[dict], targets: list[dict], rng: random.Random,
                        target_n: int = NAME_TARGET) -> list[dict]:
    """По черзі за рангом імен — до ``PER_ENTITY`` фрагментів на імʼя, різні аліаси першими."""
    by_tid = {r["tid"]: r for r in records}
    pools = []
    for t in targets:
        hits_by_alias: dict[str, list] = defaultdict(list)
        for tid in sorted(t["tids"]):
            rec = by_tid.get(tid)
            if rec is None:
                continue
            for seg in rec["segments"]:
                text = seg.get("text") or ""
                for alias in t["aliases"]:
                    if _contains(text, alias):
                        hits_by_alias[alias].append((rec, seg))
        picks = []
        for alias in sorted(hits_by_alias):
            rec, seg = rng.choice(hits_by_alias[alias])
            picks.append((alias, rec, seg))
        rng.shuffle(picks)
        if picks:
            pools.append((t["canonical"], picks[:PER_ENTITY]))
    out: list[dict] = []
    seen: set[tuple] = set()
    for depth in range(PER_ENTITY):
        for canonical, picks in pools:
            if depth >= len(picks) or len(out) >= target_n:
                continue
            alias, rec, seg = picks[depth]
            key = (rec["tid"], seg.get("start"))
            if key in seen:
                continue
            seen.add(key)
            out.append(_fragment(f"N{len(out) + 1:02d}", "name", rec, seg["start"], seg["end"],
                                 seg.get("text", ""), alias=alias, canonical=canonical))
    return out


def proper_words(conn) -> set[str]:
    """Слова імен і аліасів, написані з великої (імена власні), ≥3 літер, casefold."""
    words: set[str] = set()
    marks = ",".join("?" * len(vocabulary.ENTITY_TYPES))
    for (text,) in conn.execute(
        f"SELECT canonical_name FROM entities WHERE type IN ({marks}) UNION ALL "
        f"SELECT a.alias FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
        f"WHERE e.type IN ({marks})", vocabulary.ENTITY_TYPES * 2,
    ):
        for w in vocabulary._words(text or ""):
            if len(w) >= 3 and w[0].isupper():
                words.add(w.casefold())
    return words


def find_control_fragments(records: list[dict], proper: set[str], rng: random.Random,
                           target_n: int = CONTROL_TARGET) -> list[dict]:
    """Сегменти без імен: ні латиниці, ні слова з імен/аліасів графа; по одному на запис."""
    order = list(records)
    rng.shuffle(order)
    out = []
    for rec in order:
        ok = []
        for seg in rec["segments"]:
            text = (seg.get("text") or "").strip()
            dur = float(seg["end"]) - float(seg["start"])
            words = vocabulary._words(text)
            if not (CONTROL_MIN_S <= dur <= CONTROL_MAX_S) or len(words) < CONTROL_MIN_WORDS:
                continue
            if _LATIN.search(text) or any(w.casefold() in proper for w in words):
                continue
            ok.append(seg)
        if ok:
            seg = rng.choice(ok)
            out.append(_fragment(f"C{len(out) + 1:02d}", "control", rec, seg["start"], seg["end"],
                                 seg.get("text", "")))
        if len(out) >= target_n:
            break
    return out


def lang_of(text: str) -> str | None:
    t = text.casefold()
    ru, uk = bool(_RU.search(t)), bool(_UK.search(t))
    return "ru" if ru and not uk else "uk" if uk and not ru else None


def mixed_window(segments: list[dict], window_s: float = MIXED_WINDOW_S) -> dict | None:
    """Найкраще вікно ≤ ``window_s``: у ньому є і ы/э/ъ/ё, і і/ї/є/ґ; ранг —
    кількість перемикань мови між сегментами (швидкий діалог вище)."""
    best = None
    for i in range(len(segments)):
        start = float(segments[i]["start"])
        j, texts = i, []
        while j < len(segments) and float(segments[j]["end"]) - start <= window_s:
            texts.append(segments[j].get("text") or "")
            j += 1
        if not texts:
            continue
        joined = " ".join(texts).casefold()
        if not (_RU.search(joined) and _UK.search(joined)):
            continue
        labels = [lab for lab in (lang_of(t) for t in texts) if lab]
        switches = sum(1 for a, b in zip(labels, labels[1:]) if a != b)
        if best is None or switches > best["switches"]:
            best = {"start": start, "end": float(segments[j - 1]["end"]),
                    "text": " ".join(t.strip() for t in texts), "switches": switches}
    return best


def find_mixed(records: list[dict], target_n: int = MIXED_TARGET) -> list[dict]:
    scored = []
    for rec in records:
        w = mixed_window(rec["segments"])
        if w:
            scored.append((w["switches"], rec, w))
    scored.sort(key=lambda x: (-x[0], x[1]["tid"]))
    out = []
    for switches, rec, w in scored[:target_n]:
        out.append(_fragment(f"M{len(out) + 1:02d}", "mixed", rec, w["start"], w["end"], w["text"],
                             switches=switches))
    return out


def pick_speed_records(records: list[dict], rng: random.Random, n: int = SPEED_RECORDINGS) -> list[dict]:
    pool = [r for r in records if SPEED_MIN_S < r["duration"] <= SPEED_MAX_S]
    picks = rng.sample(pool, min(n, len(pool)))
    return [{"id": f"S{i + 1}", "kind": "speed", "tid": r["tid"], "file": r["file"],
             "language": r["language"], "duration": r["duration"], "name": r["name"]}
            for i, r in enumerate(sorted(picks, key=lambda r: r["tid"]))]


def _fragment(fid, kind, rec, start, end, text, **extra) -> dict:
    return {"id": fid, "kind": kind, "tid": rec["tid"], "file": rec["file"], "name": rec["name"],
            "language": rec["language"], "rec_duration": rec["duration"],
            "start": round(float(start), 2), "end": round(float(end), 2),
            "text": (text or "").strip(), **extra}


def build_candidates(db_path: str, seed: int = SEED) -> dict:
    rng = random.Random(seed)
    conn = connect_ro(db_path)
    try:
        records = load_records(conn)
        vocab = vocabulary.build_hard_names(db_path)
        targets = error_alias_targets(conn, vocab[:VOCAB_HEAD])
        proper = proper_words(conn)
    finally:
        conn.close()
    return {
        "seed": seed, "vocabulary": vocab,
        "names": find_name_fragments(records, targets, rng),
        "control": find_control_fragments(records, proper, rng),
        "mixed": find_mixed(records),
        "speed": pick_speed_records(records, rng),
    }


# ============================================================
# fragments.md і перегляд власника
# ============================================================

def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def render_fragments_md(c: dict) -> str:
    lines = [
        "# K1 · Фрагменти заміру",
        "",
        "Як переглядати (~5 хв): у **іменних** рядках зніміть галочку (`[x]` → `[ ]`) або закресліть рядок",
        "(`~~…~~`), якщо аліас насправді не помилка (сказали інше імʼя, це не та людина, текст правильний).",
        "У **контрольних** — якщо в сегменті все ж є імʼя. Змішану мову переглядати не треба: її судять пари `ab.md`.",
        "Час — від початку запису; файл запису вказано в `fragments.json`.",
        "",
        f"## Іменні ({len(c['names'])})",
        "",
    ]
    for f in c["names"]:
        lines.append(f"- [x] {f['id']} · #{f['tid']} {f['name']} · {_ts(f['start'])}–{_ts(f['end'])} · "
                     f"«{f['alias']}» → {f['canonical']} · {f['text']}")
    lines += ["", f"## Контрольні ({len(c['control'])})", ""]
    for f in c["control"]:
        lines.append(f"- [x] {f['id']} · #{f['tid']} {f['name']} · {_ts(f['start'])}–{_ts(f['end'])} · {f['text']}")
    lines += ["", f"## Змішана мова ({len(c['mixed'])}) — для A/B, не переглядаються", ""]
    for f in c["mixed"]:
        lines.append(f"- {f['id']} · #{f['tid']} {f['name']} · {_ts(f['start'])}–{_ts(f['end'])} · "
                     f"перемикань мови: {f['switches']}")
    lines += ["", f"## Швидкість ({len(c['speed'])} повні записи)", ""]
    for s in c["speed"]:
        lines.append(f"- {s['id']} · #{s['tid']} {s['name']} · {_ts(s['duration'])}")
    return "\n".join(lines) + "\n"


def reviewed_ids(md_text: str) -> set[str]:
    """Id іменних і контрольних рядків, які власник лишив (`[x]`, не закреслені)."""
    keep = set()
    for line in md_text.splitlines():
        m = _REVIEWED.match(line)
        if m and "~~" not in line:
            keep.add(m.group(1))
    return keep


# ============================================================
# Прогін
# ============================================================

def recording_active(api: str, timeout: float = 5.0) -> bool | None:
    """True/False — чи йде запис у Recall; None — API недоступний (Recall не запущено)."""
    try:
        with urllib.request.urlopen(f"{api}/api/recordings/active", timeout=timeout) as resp:
            return bool(json.load(resp).get("active"))
    except (OSError, ValueError):
        return None


def refuse_if_recording(api: str) -> None:
    state = recording_active(api)
    if state:
        raise SystemExit("Іде запис у Recall — GPU-прогін не запускаю. Повторіть після зупинки запису.")
    if state is None:
        print(f"[measure] API {api} недоступний — вважаю, що запису немає (Recall не запущено).")


def cut_clip(src: str, start: float, end: float, dst: str) -> None:
    start = max(0.0, start - PAD_S)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{end + PAD_S - start:.2f}",
         "-i", src, "-ac", "1", "-ar", "16000", dst],
        check=True, capture_output=True, creationflags=NO_WINDOW)


@contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update({k: v for k, v in values.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def decode_options(variant: str, db_path: str) -> dict:
    """Опції варіанта тим самим ``vocabulary.decode_options_for``, що й прод."""
    if variant == "baseline":
        return {}
    if variant in ("hotwords", "initial_prompt"):
        with _env(**{vocabulary.ENV_MODE: variant, vocabulary.ENV_MULTILINGUAL: "0"}):
            return vocabulary.decode_options_for("recording", db_path)
    with _env(**{vocabulary.ENV_MODE: "off", vocabulary.ENV_MULTILINGUAL: "1"}):
        opts = vocabulary.decode_options_for("recording", db_path)
    if variant == "ml10":
        opts["chunk_length"] = 10
    return opts


def transcribe(wm, path: str, model: str, language: str, opts: dict, source_duration: float | None):
    """Один прогін через прод-шлях. ``source_duration`` > 600 с → batched, як для всього запису."""
    real = wm.__class__._get_duration_ffprobe
    if source_duration is not None:
        wm._get_duration_ffprobe = lambda _p: source_duration
    try:
        t0 = time.perf_counter()
        result = wm.transcribe_with_progress(path, model_name=model, language=language,
                                             decode_options=opts)
        seconds = time.perf_counter() - t0
    finally:
        wm._get_duration_ffprobe = real.__get__(wm)
    if "error" in result:
        raise RuntimeError(result["error"])
    return {"text": (result.get("text") or "").strip(), "seconds": round(seconds, 3),
            "language": result.get("language")}


def run_set(wm, fragments: list[dict], variants: tuple, db_path: str, model: str, api: str,
            tmp: str, force_batched: bool) -> dict:
    refuse_if_recording(api)
    out = {"model": model, "variants": {}, "decode_options": {}}
    opts = {v: decode_options(v, db_path) for v in variants}
    for v, o in opts.items():
        out["decode_options"][v] = {k: (len(val) if isinstance(val, list) else val) for k, val in o.items()}
    clips = {}
    for f in fragments:
        clip = os.path.join(tmp, f"{f['id']}.wav")
        cut_clip(f["file"], f["start"], f["end"], clip)
        clips[f["id"]] = clip
    for v in variants:
        out["variants"][v] = {}
        for f in fragments:
            src_dur = f["rec_duration"] if (force_batched or f["rec_duration"] > BATCHED_FROM_S) else None
            if force_batched:
                src_dur = max(src_dur or 0.0, BATCHED_FROM_S + 1)
            # multilingual діє лише без явної мови (4.8.1) — варіанти ml* ідуть на «auto».
            lang = "auto" if opts[v].get("multilingual") else f["language"]
            out["variants"][v][f["id"]] = transcribe(wm, clips[f["id"]], model, lang, opts[v], src_dur)
        print(f"[measure] {v}: {len(fragments)} фрагментів")
    return out


def run_speed(wm, records: list[dict], db_path: str, model: str, api: str, reps: int = SPEED_REPS) -> dict:
    """Ті самі три варіанти на цілих записах, ``reps`` повторів із круговим порядком
    (перший прогін не завжди baseline), у ``seconds`` — медіана повторів."""
    out = {"model": model, "reps": reps, "variants": {v: {} for v in NAME_VARIANTS}}
    for rec in records:
        runs: dict[str, list] = {v: [] for v in NAME_VARIANTS}
        for r in range(reps):
            refuse_if_recording(api)
            order = NAME_VARIANTS[r % len(NAME_VARIANTS):] + NAME_VARIANTS[:r % len(NAME_VARIANTS)]
            for v in order:
                runs[v].append(transcribe(wm, rec["file"], model, rec["language"], decode_options(v, db_path), None))
        for v in NAME_VARIANTS:
            secs = sorted(x["seconds"] for x in runs[v])
            out["variants"][v][rec["id"]] = {**runs[v][0], "seconds": secs[len(secs) // 2],
                                             "runs": [x["seconds"] for x in runs[v]], "duration": rec["duration"]}
        print(f"[measure] speed {rec['id']}: {[out['variants'][v][rec['id']]['runs'] for v in NAME_VARIANTS]}")
    return out


def cmd_run(args) -> int:
    cands = json.loads((MEASURE_DIR / "fragments.json").read_text(encoding="utf-8"))
    refuse_if_recording(args.api)
    from whisper_manager_new import ModernWhisperManager  # важкий імпорт лише тут

    wm = ModernWhisperManager()
    if wm.load_model(args.model) is None:
        raise SystemExit(f"модель {args.model} не завантажилась")
    raw = MEASURE_DIR / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    sets = args.sets.split(",")
    with tempfile.TemporaryDirectory(prefix="k1_measure_") as tmp:
        for name in sets:
            if name in ("names", "control"):
                data = run_set(wm, cands[name], NAME_VARIANTS, args.db, args.model, args.api, tmp, False)
            elif name == "mixed":
                data = run_set(wm, cands["mixed"], MIXED_VARIANTS, args.db, args.model, args.api, tmp, True)
            elif name == "speed":
                data = run_speed(wm, cands["speed"], args.db, args.model, args.api, args.reps)
            else:
                raise SystemExit(f"невідомий набір: {name}")
            (raw / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


# ============================================================
# Сліпі пари A/B
# ============================================================

def _norm(text: str) -> str:
    return " ".join(text.split())


def build_ab(mixed: list[dict], raw: dict, seed: int = SEED) -> tuple[str, dict]:
    """По парі «baseline проти варіанта» на кожен фрагмент і кожне вікно (ml30, ml10).

    Однакові тексти — нічия без показу власнику. Порядок пар і сторони А/Б
    перемішані; який варіант де — лише в ключі."""
    rng = random.Random(seed)
    pairs, ties = [], []
    for f in mixed:
        base = raw["variants"]["baseline"][f["id"]]["text"]
        for v in ("ml30", "ml10"):
            other = raw["variants"][v][f["id"]]["text"]
            if _norm(base) == _norm(other):
                ties.append({"fragment": f["id"], "variant": v})
                continue
            sides = [("baseline", base), (v, other)]
            rng.shuffle(sides)
            pairs.append({"fragment": f, "variant": v, "sides": sides})
    rng.shuffle(pairs)
    lines = [
        "# K1 · Змішана мова — сліпі пари",
        "",
        "Правильним вважається дослівний текст мовою того, хто говорить (російська — російською,",
        "англійська — англійською). Послухайте фрагмент і впишіть після «Вердикт:» `А`, `Б` або `=`.",
        f"Пари, де обидва тексти однакові, сюди не потрапили (нічиїх: {len(ties)}).",
        "",
    ]
    key = {"seed": seed, "pairs": {}, "auto_ties": ties}
    for n, p in enumerate(pairs, 1):
        f = p["fragment"]
        (la, ta), (lb, tb) = p["sides"]
        key["pairs"][str(n)] = {"fragment": f["id"], "variant": p["variant"], "A": la, "B": lb}
        lines += [
            f"## Пара {n} · #{f['tid']} {f['name']} · {_ts(f['start'])}–{_ts(f['end'])}",
            "", f"**А:** {ta}", "", f"**Б:** {tb}", "", "Вердикт: ", "",
        ]
    return "\n".join(lines), key


def cmd_ab(args) -> int:
    cands = json.loads((MEASURE_DIR / "fragments.json").read_text(encoding="utf-8"))
    raw = json.loads((MEASURE_DIR / "raw" / "mixed.json").read_text(encoding="utf-8"))
    md, key = build_ab(cands["mixed"], raw)
    (MEASURE_DIR / "ab.md").write_text(md, encoding="utf-8")
    (MEASURE_DIR / "ab-key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[measure] ab.md: {len(key['pairs'])} пар, нічиїх {len(key['auto_ties'])}")
    return 0


# ============================================================
# Підсумок (історія 03)
# ============================================================

SLOWDOWN_LIMIT = 0.10
#: Більшість із 10 пар на кожне вікно multilingual.
AB_MAJORITY = 6
_VERDICT = re.compile(r"^## Пара (\d+) ·.*?^Вердикт:[ \t]*(\S*)", re.S | re.M)


def target_word(alias: str, canonical: str) -> str:
    """Слово канону, яке спотворив аліас: найближче за фонетичним ключем.

    «Мельнюк» при «Василь Мельник» → «Мельник», «Rebuild» при «Upbuild Ukraine» →
    «Upbuild»: інакше «Василь»/«Ukraine» зараховували б влучання і baseline."""
    words = [w for w in vocabulary._words(canonical) if len(w) >= 3] or vocabulary._words(canonical)
    a_keys = vocabulary.phonetic_keys("".join(vocabulary._words(alias))) or {alias.casefold()}

    def dist(w):
        return min(_levenshtein(a, c) for a in a_keys for c in (vocabulary.phonetic_keys(w) or {w.casefold()}))
    return min(words, key=dist)


def strict_hit(text: str, word: str) -> bool:
    """Слово канону стоїть у тексті в будь-якому відмінку: основа після casefold."""
    w0 = word.casefold()
    for w in vocabulary._words(text):
        w = w.casefold()
        if (w[:vocabulary.STEM_LEN] == w0[:vocabulary.STEM_LEN]) if len(w0) >= vocabulary.STEM_LEN else w == w0:
            return True
    return False


def tolerant_hit(text: str, word: str) -> bool:
    """Те саме, але переклад/транслітерація теж влучання («Клода» при Claude)."""
    return any(vocabulary.same_name(w, word) for w in vocabulary._words(text))


def name_present(text: str, name: str) -> bool:
    words = [w for w in vocabulary._words(name) if len(w) >= 3]
    return bool(words) and all(strict_hit(text, w) for w in words)


def score_names(fragments: list[dict], raw: dict, keep: set[str]) -> dict:
    kept = [f for f in fragments if f["id"] in keep]
    out = {}
    for v, outputs in raw["variants"].items():
        per = {}
        for f in kept:
            text = outputs[f["id"]]["text"]
            word = target_word(f["alias"], f["canonical"])
            per[f["id"]] = {"word": word, "strict": strict_hit(text, word), "tolerant": tolerant_hit(text, word)}
        out[v] = {"n": len(kept), "per": per,
                  "strict": sum(p["strict"] for p in per.values()),
                  "tolerant": sum(p["tolerant"] for p in per.values())}
    return out


def score_control(fragments: list[dict], raw: dict, keep: set[str], vocab: list[str]) -> dict:
    """Розбіжності з baseline і «зайві» імена словника, яких у baseline не було."""
    kept = [f for f in fragments if f["id"] in keep]
    base = raw["variants"]["baseline"]
    out = {}
    for v, outputs in raw["variants"].items():
        if v == "baseline":
            continue
        diverged, extra = [], []
        for f in kept:
            b, t = base[f["id"]]["text"], outputs[f["id"]]["text"]
            if _norm(b) != _norm(t):
                diverged.append({"fragment": f["id"], "baseline": b, "variant": t})
            for name in vocab:
                if name_present(t, name) and not name_present(b, name):
                    extra.append({"fragment": f["id"], "name": name})
        out[v] = {"n": len(kept), "diverged": diverged, "extra": extra}
    return out


def score_speed(raw: dict) -> dict:
    base = raw["variants"]["baseline"]
    out = {"_reps": raw.get("reps", 1)}
    for v, outputs in raw["variants"].items():
        per = {sid: round(o["seconds"] / base[sid]["seconds"] - 1, 4) for sid, o in outputs.items()}
        total = sum(o["seconds"] for o in outputs.values()) / sum(base[s]["seconds"] for s in outputs) - 1
        out[v] = {"per": per, "total": round(total, 4)}
    return out


def parse_verdicts(md: str) -> dict[str, str]:
    return {n: v.upper().replace("A", "А").replace("B", "Б") for n, v in _VERDICT.findall(md)}


def score_ab(verdicts: dict[str, str], key: dict) -> dict:
    out = {v: {"wins": 0, "losses": 0, "ties": 0, "missing": 0} for v in ("ml30", "ml10")}
    for n, pair in key["pairs"].items():
        s = out[pair["variant"]]
        verdict = verdicts.get(n, "")
        if verdict == "=":
            s["ties"] += 1
        elif verdict in ("А", "Б"):
            chosen = pair["A"] if verdict == "А" else pair["B"]
            s["wins" if chosen == pair["variant"] else "losses"] += 1
        else:
            s["missing"] += 1
    for t in key.get("auto_ties", []):
        out[t["variant"]]["ties"] += 1
    return out


def decide(names: dict, control: dict, speed: dict, ab: dict, owner_vocab: str | None = None,
           owner_note: str = "") -> dict:
    """Правила плану: словник — частка зросла, сповільнення ≤10 %, контроль без
    зайвих імен; multilingual — перемога у більшості з 10 пар свого вікна."""
    base = names["baseline"]["strict"]
    vocab = {}
    for v in ("hotwords", "initial_prompt"):
        reasons = []
        if names[v]["strict"] <= base:
            reasons.append(f"частка не зросла ({names[v]['strict']} проти {base})")
        if speed[v]["total"] > SLOWDOWN_LIMIT:
            reasons.append(f"сповільнення {speed[v]['total']:+.1%} > {SLOWDOWN_LIMIT:.0%}")
        if control[v]["extra"]:
            reasons.append(f"зайвих імен на контролі: {len(control[v]['extra'])}")
        vocab[v] = reasons
    passed = [v for v, r in vocab.items() if not r]
    mode = max(passed, key=lambda v: names[v]["strict"]) if passed else "off"
    ml = {v: ab[v]["wins"] >= AB_MAJORITY and not ab[v]["missing"] for v in ("ml30", "ml10")}
    if ml["ml10"] and (not ml["ml30"] or ab["ml10"]["wins"] > ab["ml30"]["wins"]):
        window = "ml10"
    elif ml["ml30"]:
        window = "ml30"
    else:
        window = None
    decision = {"vocab_mode": mode, "vocab_rule": mode, "vocab_reasons": vocab,
                "multilingual": window is not None, "chunk_length": 10 if window == "ml10" else None,
                "ab_accepted": ml}
    if owner_vocab and owner_vocab != mode:
        # Слово власника переважає правило, але правило лишається у звіті поруч.
        decision.update(vocab_mode=owner_vocab, owner_note=owner_note)
    return decision


def _pct(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n:.0%})" if n else "—"


def render_report(names, control, speed, ab, decision, vocab_size) -> str:
    n = names["baseline"]["n"]
    L = ["# K1 · Підсумок заміру", "",
         "Числа порахував `tools/measure_vocabulary.py score` з розмітки власника (`fragments.md`, `ab.md`)",
         "і сирих виходів `raw/*.json`. Ранжування словника під фрагменти не підганялось.", "",
         "## Рішення", ""]
    mode, rule = decision["vocab_mode"], decision["vocab_rule"]
    if mode != rule:
        L.append(f"- **RECALL_VOCAB_MODE = {mode}** — рішення власника всупереч правилу (за правилом: {rule}). "
                 f"{decision.get('owner_note', '')}".rstrip())
    else:
        L.append(f"- **RECALL_VOCAB_MODE = {mode}**"
                 + ("" if mode != "off" else " — жоден варіант словника не пройшов усі три умови."))
    win = "вікно 10 с (`chunk_length=10`)" if decision["chunk_length"] else "вікно 30 с"
    L.append(f"- **WHISPER_MULTILINGUAL = {'1' if decision['multilingual'] else '0'}**"
             + (f", {win}" if decision["multilingual"] else " — жодне вікно не виграло більшість пар."))
    L += ["", f"## Імена ({n} фрагментів після перегляду, словник {vocab_size} імен)", "",
          "| Варіант | Влучання (основа, casefold) | З перекладами («Клода» = Claude) |", "|---|---|---|"]
    for v in NAME_VARIANTS:
        L.append(f"| {v} | {_pct(names[v]['strict'], n)} | {_pct(names[v]['tolerant'], n)} |")
    L += ["", "По фрагментах (✓ — слово канону є у виході, строго):", "",
          "| Фрагмент | Слово | " + " | ".join(NAME_VARIANTS) + " |", "|---|---|" + "---|" * len(NAME_VARIANTS)]
    for fid, p in names["baseline"]["per"].items():
        cells = ["✓" if names[v]["per"][fid]["strict"] else ("≈" if names[v]["per"][fid]["tolerant"] else "·")
                 for v in NAME_VARIANTS]
        L.append(f"| {fid} | {p['word']} | " + " | ".join(cells) + " |")
    L += ["", "`≈` — у виході переклад або транслітерація того самого імені.", "",
          f"## Контроль ({control['hotwords']['n']} фрагментів без імен)", ""]
    for v in ("hotwords", "initial_prompt"):
        c = control[v]
        L.append(f"### {v}: розбіжностей з baseline {len(c['diverged'])}, зайвих імен {len(c['extra'])}")
        L.append("")
        for e in c["extra"]:
            L.append(f"- ⚠ зайве імʼя «{e['name']}» у {e['fragment']}")
        for d in c["diverged"]:
            L += [f"- {d['fragment']}", f"  - baseline: {d['baseline']}", f"  - {v}: {d['variant']}"]
        L.append("")
    L += ["## Швидкість (3 повні записи 12–27 хв)", "",
          "| Варіант | " + " | ".join(speed["baseline"]["per"]) + " | Разом |",
          "|---|" + "---|" * (len(speed["baseline"]["per"]) + 1)]
    for v in NAME_VARIANTS:
        L.append(f"| {v} | " + " | ".join(f"{x:+.1%}" for x in speed[v]["per"].values())
                 + f" | **{speed[v]['total']:+.1%}** |")
    reps = speed.get("_reps", 1)
    L += ["", f"Модель прогріта до замірів; {reps} повтори на варіант із круговим порядком варіантів, у таблиці —",
          "медіана. Один прогін давав розкид +7…+16 % на тих самих записах, тому рішення за ним не приймалось.", "",
          "## Змішані мови, сліпі пари", "", "| Вікно | Перемог | Поразок | Нічиїх | Прийнято (≥6 з 10) |", "|---|---|---|---|---|"]
    for v, label in (("ml30", "30 с"), ("ml10", "10 с")):
        a = ab[v]
        L.append(f"| {label} | {a['wins']} | {a['losses']} | {a['ties']} | {'так' if decision['ab_accepted'][v] else 'ні'} |")
    L += ["", "## Чому так", ""]
    for v, reasons in decision["vocab_reasons"].items():
        L.append(f"- {v}: " + ("пройшов усі умови." if not reasons else "; ".join(reasons) + "."))
    return "\n".join(L) + "\n"


def cmd_score(args) -> int:
    cands = json.loads((MEASURE_DIR / "fragments.json").read_text(encoding="utf-8"))
    raw = {name: json.loads((MEASURE_DIR / "raw" / f"{name}.json").read_text(encoding="utf-8"))
           for name in ("names", "control", "speed")}
    keep = reviewed_ids((MEASURE_DIR / "fragments.md").read_text(encoding="utf-8"))
    vocab = cands["vocabulary"][:VOCAB_HEAD]
    names = score_names(cands["names"], raw["names"], keep)
    control = score_control(cands["control"], raw["control"], keep, vocab)
    speed = score_speed(raw["speed"])
    key = json.loads((MEASURE_DIR / "ab-key.json").read_text(encoding="utf-8"))
    ab = score_ab(parse_verdicts((MEASURE_DIR / "ab.md").read_text(encoding="utf-8")), key)
    decision = decide(names, control, speed, ab, args.owner_vocab, args.owner_note)
    (MEASURE_DIR / "report.md").write_text(render_report(names, control, speed, ab, decision, len(vocab)),
                                           encoding="utf-8")
    print(json.dumps(decision, ensure_ascii=False))
    return 0


def cmd_candidates(args) -> int:
    c = build_candidates(args.db, args.seed)
    MEASURE_DIR.mkdir(parents=True, exist_ok=True)
    (MEASURE_DIR / "fragments.json").write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    (MEASURE_DIR / "fragments.md").write_text(render_fragments_md(c), encoding="utf-8")
    print(f"[measure] іменних {len(c['names'])}, контрольних {len(c['control'])}, "
          f"змішаних {len(c['mixed'])}, швидкість {len(c['speed'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Замір словника імен і змішаних мов (K1).")
    parser.add_argument("--db", default=str(ROOT / "whisper_history.db"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("candidates")
    p.add_argument("--seed", type=int, default=SEED)
    p = sub.add_parser("run")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--api", default=DEFAULT_API)
    p.add_argument("--sets", default="names,control,mixed,speed")
    p.add_argument("--reps", type=int, default=SPEED_REPS)
    sub.add_parser("ab")
    p = sub.add_parser("score")
    p.add_argument("--owner-vocab", choices=("off",) + NAME_VARIANTS[1:],
                   help="рішення власника щодо словника, якщо воно розходиться з правилом")
    p.add_argument("--owner-note", default="", help="дослівна причина рішення власника")
    args = parser.parse_args(argv)
    return {"candidates": cmd_candidates, "run": cmd_run, "ab": cmd_ab, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
