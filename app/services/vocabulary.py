"""Словник «важких» імен із графа сутностей для faster-whisper (K1).

Whisper калічить імена людей, проєктів і організацій, які часто звучать у
дзвінках власника («Акмеец» замість Acmecorp). Граф сутностей уже знає ці
імена, а його аліаси — ще й те, як саме Whisper їх писав. Звідси словник,
який сам збирається з графа без ручного ведення:

- кандидати — сутності типів ``person``/``project``/``org`` (``topic`` ні);
- **аліас-помилка** — аліас сутності, який трапляється в тексті її
  аудіо-транскрипцій (файл, запис, голосові TG; YouTube ні) і не є тим самим
  імʼям ні за основою (перші 4 літери після ``casefold()`` у Python — SQLite
  ``LOWER`` кирилицю не згортає), ні за фонетичним ключем поперек мов.
  Відмінки («Адаму» при «Адам»), переклади й транслітерації uk/ru/en («Гугл»
  при Google, «Телеграм» при Telegram) помилками не вважаються;
- ранг — кількість зустрічей × кількість аліасів-помилок × буст (латиниця,
  складене імʼя), нічия — за зустрічами; сутності без помилок ідуть хвостом;
- імʼя, яке в аудіо-текстах частіше стоїть з малої літери («фонд», «парк»),
  — загальновживане слово, відсіюється; складене — якщо таке хоч одне його слово.

Список кешується в процесі з TTL і перебудовується без рестарту. Модуль лише
ЧИТАЄ граф (``mode=ro``) і не імпортує torch/модель: токени рахує
``whisper_manager_new`` токенайзером завантаженої моделі й обрізає список
за межею імені в 223 токени.

Прапори (реєстр ``app/core/settings.py``, група «Транскрипція»). Дефолти —
за підсумком заміру K1 (``docs/specs/k1-name-vocabulary/measure/report.md``):
- ``RECALL_VOCAB_MODE`` = off | hotwords | initial_prompt, дефолт ``hotwords``
  (імена 15 % → 35 %, +11 % часу — рішення власника понад поріг 10 %);
- ``WHISPER_MULTILINGUAL`` — визначення мови на кожному вікні до 30 с
  (не на фразу) на повній транскрипції, файлах і TG, дефолт ``1`` (сліпі пари:
  7 з 10); жива транскрипція його не отримує, бо вже визначає мову на кожному
  своєму шматку. Діє лише коли мову не обрано («Авто-визначення»): явна мова
  («uk») — закон, це вирішує ``whisper_manager_new`` (4.8.1).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)

ENV_MODE = "RECALL_VOCAB_MODE"
ENV_MULTILINGUAL = "WHISPER_MULTILINGUAL"
DEFAULT_MODE = "hotwords"
DEFAULT_MULTILINGUAL = "1"
MODES = ("off", "hotwords", "initial_prompt")
SOURCE_KINDS = ("file", "recording", "telegram", "live", "youtube")

ENTITY_TYPES = ("person", "project", "org")
#: Кандидатів більше, ніж влізе: обрізає за токенами менеджер (≈50 імен у 223 токени).
VOCAB_LIMIT = 60
CACHE_TTL_S = 3600.0
STEM_LEN = 4
MIN_NAME_LEN = 3

_WORD_RE = re.compile(r"[^\W\d_]+")
_LATIN_RE = re.compile(r"[A-Za-z]")

_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = threading.Lock()


def mode() -> str:
    """Режим словника з env; невідоме значення → ``off`` з попередженням."""
    raw = (os.environ.get(ENV_MODE, DEFAULT_MODE) or "off").strip().lower()
    if raw not in MODES:
        logger.warning("%s=%r невідомий, словник вимкнено (допустимі: %s)",
                       ENV_MODE, raw, ", ".join(MODES))
        return "off"
    return raw


def multilingual_enabled() -> bool:
    raw = (os.environ.get(ENV_MULTILINGUAL, DEFAULT_MULTILINGUAL) or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _stems(name: str) -> set[str]:
    return {w.casefold()[:STEM_LEN] for w in _words(name)}


# uk + ru кирилиця → латиниця. «г» → g, бо так пишуть англійські імена (Гугл,
# Гітхаб); англійське h нижче теж зводиться до g, тож «Гарвард» = Harvard.
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "є": "ye", "ж": "zh",
    "з": "z", "и": "i", "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "yu", "я": "ya", "ё": "yo", "ы": "y",
    "э": "e", "ъ": "",
}
# Англійська орфографія → один звук (порядок важливий: диграфи до одиночних літер).
_PHONETIC_RULES = (
    ("tion", "shn"), ("sh", "S"), ("ch", "C"), ("zh", "Z"), ("kh", "h"), ("ph", "f"),
    ("th", "t"), ("ck", "k"), ("qu", "kv"), ("x", "ks"), ("c", "k"), ("q", "k"),
    ("w", "v"), ("h", "g"),
)
_NON_LATIN_RE = re.compile(r"[^a-zA-Z]")
_VOWELS_RE = re.compile(r"[aeiouy]")
_REPEAT_RE = re.compile(r"(.)\1+")
#: Скільки приголосних може дописати закінчення або суфікс
#: («Клод» → «Клодом»: kld → kldm; «Гугл» → «гугловський»: gl → glvsk).
_ENDING_SLACK = 3
#: Префіксне порівняння окремих слів — лише з ключем від 3 приголосних:
#: коротший (gl, dm) збігся б префіксом із чим завгодно.
_PREFIX_MIN_KEY = 3


def phonetic_keys(text: str) -> set[str]:
    """Фонетичні ключі поперек мов і письма: кирилиця → латиниця, англійські
    диграфи → один звук, голосні відкинуто, повтори злито.

    «Гугл» і Google → gl, «Телеграм» і Telegram → tlgrm, «Олексій» і «Алексей» → lks;
    «Акмеец» (brgts) і Acmecorp (brgds) — різні. Голосних ключ не розрізняє навмисно:
    саме ними відрізняються переклади uk/ru/en одного імені. Латинське j дає два
    ключі: «дж» (Jason → Джейсон) і «й» (Julia → Юлія)."""
    w = "".join(_CYR_TO_LAT.get(ch, ch) for ch in text.casefold())
    w = _NON_LATIN_RE.sub("", w)
    for src, dst in _PHONETIC_RULES:
        w = w.replace(src, dst)
    variants = {w.replace("j", "dZ"), w.replace("j", "y")} if "j" in w else {w}
    return {_REPEAT_RE.sub(r"\1", _VOWELS_RE.sub("", v)) for v in variants} - {""}


def _prefix_match(alias_keys: set[str], canon_keys: set[str]) -> bool:
    return any(a.startswith(c) and len(a) - len(c) <= _ENDING_SLACK
               for a in alias_keys for c in canon_keys)


def same_name(alias: str, canonical: str) -> bool:
    """Аліас — те саме імʼя, а не помилка Whisper: відмінок («Адаму» при «Адам»),
    переклад чи транслітерація («Гугл» при Google, «Роботес фонд» при Робота Fund)."""
    if _stems(alias) & _stems(canonical):
        return True
    a_words, c_words = _words(alias), _words(canonical)
    if _prefix_match(phonetic_keys("".join(a_words)), phonetic_keys("".join(c_words))):
        return True
    # Окремі слова: «гугл диск» при Google, «Julia Bondarenko» при «Юлія».
    a_keys = [phonetic_keys(w) for w in a_words]
    for cw in c_words:
        c_keys = phonetic_keys(cw)
        long_keys = {k for k in c_keys if len(k) >= _PREFIX_MIN_KEY}
        for ak in a_keys:
            if ak & c_keys or (long_keys and _prefix_match(ak, long_keys)):
                return True
    return False


def is_error_alias(alias: str, canonical: str) -> bool:
    """Аліас-помилка: не те саме імʼя ні за основою, ні за фонетичним ключем.

    «Акмеец» при Acmecorp — помилка; «Адаму» при «Адам», «Гугл» при Google,
    «Телеграм» при Telegram і «Фонд Робота» при «Робота Fund» — ні."""
    if not _stems(alias) or not _stems(canonical):
        return False
    return not same_name(alias, canonical)


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def build_hard_names(db_path: str, limit: int = VOCAB_LIMIT) -> list[str]:
    """Ранжований список «важких» імен без кешу (див. docstring модуля)."""
    conn = _connect_ro(db_path)
    try:
        types_sql = ",".join("?" * len(ENTITY_TYPES))
        entities = conn.execute(
            f"SELECT e.id, e.canonical_name, COUNT(DISTINCT me.transcription_id) "
            f"FROM entities e LEFT JOIN meeting_entities me ON me.entity_id = e.id "
            f"WHERE e.type IN ({types_sql}) GROUP BY e.id",
            ENTITY_TYPES,
        ).fetchall()
        aliases: dict[int, list[str]] = defaultdict(list)
        for eid, alias in conn.execute(
            f"SELECT a.entity_id, a.alias FROM entity_aliases a "
            f"JOIN entities e ON e.id = a.entity_id WHERE e.type IN ({types_sql})",
            ENTITY_TYPES,
        ):
            if alias:
                aliases[eid].append(alias)
        # Аудіо-транскрипції: текст, який написав саме Whisper (YouTube — чужі люди).
        audio_text: dict[int, str] = {}
        for tid, text in conn.execute(
            "SELECT id, transcript_text FROM transcriptions "
            "WHERE model_used IS NOT NULL AND source_type IN ('file','recording','telegram') "
            "AND transcript_text IS NOT NULL AND transcript_text != ''"
        ):
            audio_text[tid] = text
        linked: dict[int, list[int]] = defaultdict(list)
        for eid, tid in conn.execute("SELECT entity_id, transcription_id FROM meeting_entities"):
            if tid in audio_text:
                linked[eid].append(tid)
    finally:
        conn.close()

    # Регістр слова в аудіо-текстах: скільки разів воно стоїть з малої / з великої.
    raw_counts: Counter[str] = Counter()
    for text in audio_text.values():
        raw_counts.update(_words(text))
    lower_n: Counter[str] = Counter()
    upper_n: Counter[str] = Counter()
    for word, n in raw_counts.items():
        (lower_n if word[0].islower() else upper_n)[word.casefold()] += n

    def is_common(name: str) -> bool:
        # Складене імʼя із загальновживаним словом («Промінвест (хаб)», «RAG-система
        # Андрія») — опис, а не назва; Whisper такі слова й так пише правильно.
        return any(lower_n[w] > upper_n[w] for w in (x.casefold() for x in _words(name)))

    folded_text: dict[int, str] = {}

    def occurs(alias: str, tids: list[int]) -> bool:
        needle = alias.casefold()
        pattern = re.compile(r"(?<!\w)" + re.escape(needle) + r"(?!\w)")
        for tid in tids:
            ft = folded_text.get(tid)
            if ft is None:
                ft = folded_text[tid] = audio_text[tid].casefold()
            # Підрядок — швидкий відсів; регекс із lookbehind лише підтверджує межі слова.
            if needle in ft and pattern.search(ft):
                return True
        return False

    ranked = []
    for eid, canonical, meetings in entities:
        name = (canonical or "").strip()
        if len(name) < MIN_NAME_LEN or is_common(name):
            continue
        tids = linked.get(eid, [])
        errors = 0
        if tids:
            seen: set[str] = set()
            for alias in aliases.get(eid, []):
                key = alias.casefold()
                if key in seen or not is_error_alias(alias, name):
                    continue
                seen.add(key)
                if occurs(alias, tids):
                    errors += 1
        boost = 1 + (1 if _LATIN_RE.search(name) else 0) + (1 if len(_words(name)) > 1 else 0)
        ranked.append((meetings * errors * boost, meetings * boost, name))

    ranked.sort(key=lambda r: (-r[0], -r[1], r[2]))
    out: list[str] = []
    seen_names: set[str] = set()
    for _, _, name in ranked:
        key = name.casefold()
        if key in seen_names:
            continue  # двійники одного імені поперек типів (project + org)
        seen_names.add(key)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def hard_names(db_path: str, limit: int = VOCAB_LIMIT) -> list[str]:
    """Кешований :func:`build_hard_names`: у межах TTL у БД не ходить."""
    key = os.path.abspath(db_path)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return list(hit[1][:limit])
        try:
            names = build_hard_names(db_path, limit=VOCAB_LIMIT)
        except Exception:  # noqa: BLE001 — словник не має права зупинити транскрипцію
            # Граф недоступний (нова БД без міграцій, замок) — транскрипція йде без
            # словника; порожній список теж кешується, щоб не довбати БД на кожен тик живої.
            logger.warning("словник імен не зібрано з %s", db_path, exc_info=True)
            names = []
        _cache[key] = (now + CACHE_TTL_S, names)
        logger.info("словник імен перебудовано: %d імен", len(names))
        return list(names[:limit])


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def decode_options_for(source_kind: str, db_path: str | None) -> dict:
    """Опції декодування для ``transcribe_with_progress`` на шляху ``source_kind``.

    YouTube і вимкнені прапори → ``{}`` (виклик faster-whisper як до K1)."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"невідомий source_kind: {source_kind!r}")
    if source_kind == "youtube":
        return {}
    opts: dict = {}
    vocab_mode = mode()
    if vocab_mode != "off" and db_path:
        names = hard_names(db_path)
        if names:
            opts[vocab_mode] = names
    if multilingual_enabled() and source_kind != "live":
        opts["multilingual"] = True
    return opts


def check_error_pairs(path: str) -> int:
    """Гейт карти дефекту ``translation-counted-as-asr-error``.

    ``path`` — JSON-список ``{"alias", "canonical"}`` пар, які артефакт (список
    фрагментів заміру, звіт словника) подає як помилки Whisper. Exit 1, якщо
    серед них є переклад/транслітерація того самого імені («Гугл» при Google);
    файл не того формату — гейт не застосовний, exit 0."""
    import json

    try:
        with open(path, encoding="utf-8") as fh:
            pairs = json.load(fh)
    except (OSError, ValueError):
        print(f"check-error-pairs: {path} — не JSON-список пар, гейт не застосовний")
        return 0
    if not isinstance(pairs, list):
        print(f"check-error-pairs: {path} — не список, гейт не застосовний")
        return 0
    bad = [p for p in pairs
           if isinstance(p, dict) and p.get("alias") and p.get("canonical")
           and same_name(str(p["alias"]), str(p["canonical"]))]
    for p in bad:
        print(f"переклад, не помилка: «{p['alias']}» = «{p['canonical']}»")
    print(f"check-error-pairs: {len(bad)} з {len(pairs)} пар — переклади того самого імені")
    return 1 if bad else 0


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.services.vocabulary",
                                     description="Словник «важких» імен (K1).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check-error-pairs", help="гейт карти дефекту: переклад ≠ помилка")
    p_check.add_argument("path")
    p_show = sub.add_parser("show", help="зібраний словник (БД лише читається)")
    p_show.add_argument("--db", default="whisper_history.db")
    args = parser.parse_args(argv)
    if args.cmd == "check-error-pairs":
        return check_error_pairs(args.path)
    for name in build_hard_names(args.db):
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
