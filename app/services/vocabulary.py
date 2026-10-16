"""A dictionary of "hard" names from the entity graph for faster-whisper (K1).

Whisper mangles the names of people, projects and organizations that sound often in
the owner's calls («Акмеец» instead of Acmecorp). The entity graph already knows these
names, and its aliases also know how exactly Whisper wrote them. Hence a dictionary
that assembles itself from the graph without manual upkeep:

- candidates — entities of types ``person``/``project``/``org`` (not ``topic``);
- **an error alias** — an alias of an entity that occurs in the text of its
  audio transcripts (file, recording, TG voice messages; not YouTube) and is not the same
  name either by stem (the first 4 letters after ``casefold()`` in Python — SQLite
  ``LOWER`` does not fold Cyrillic) or by a cross-language phonetic key.
  Cases («Адаму» for «Адам»), translations and transliterations uk/ru/en («Гугл»
  for Google, «Телеграм» for Telegram) are not considered errors;
- rank — number of meetings × number of error aliases × boost (Latin script,
  compound name), a tie is broken by meetings; entities without errors go at the tail;
- a name that stands lowercase more often in the audio texts («фонд», «парк»)
  is a common word and is filtered out; a compound one — if even one of its words is such.

The list is cached in the process with a TTL and rebuilt without a restart. The module only
READS the graph (``mode=ro``) and does not import torch/the model: tokens are counted
by ``whisper_manager_new`` with the tokenizer of the loaded model, and it trims the list
at a name boundary to 223 tokens.

Flags (registry ``app/core/settings.py``, group "Transcription"). Defaults —
by the outcome of the K1 measurement (``docs/specs/k1-name-vocabulary/measure/report.md``):
- ``RECALL_VOCAB_MODE`` = off | hotwords | initial_prompt, default ``hotwords``
  (names 15 % → 35 %, +11 % time — the owner's decision above the 10 % threshold);
- ``WHISPER_MULTILINGUAL`` — language detection on every window up to 30 s
  (not per phrase) on full transcription, files and TG, default ``1`` (blind pairs:
  7 of 10); live transcription does not get it, because it already detects the language on each
  of its chunks. Works only when no language is chosen ("Auto-detect"): an explicit language
  («uk») is law, this is decided by ``whisper_manager_new`` (4.8.1).
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
#: There are more candidates than fit: the manager trims by tokens (≈50 names in 223 tokens).
VOCAB_LIMIT = 60
CACHE_TTL_S = 3600.0
STEM_LEN = 4
MIN_NAME_LEN = 3

_WORD_RE = re.compile(r"[^\W\d_]+")
_LATIN_RE = re.compile(r"[A-Za-z]")

_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = threading.Lock()


def mode() -> str:
    """The dictionary mode from env; an unknown value → ``off`` with a warning."""
    raw = (os.environ.get(ENV_MODE, DEFAULT_MODE) or "off").strip().lower()
    if raw not in MODES:
        logger.warning("%s=%r is unknown, vocabulary disabled (allowed: %s)",
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


# uk + ru Cyrillic → Latin. «г» → g, because English names are written that way («Гугл»,
# «Гітхаб»); English h below also reduces to g, so «Гарвард» = Harvard.
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "є": "ye", "ж": "zh",
    "з": "z", "и": "i", "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "yu", "я": "ya", "ё": "yo", "ы": "y",
    "э": "e", "ъ": "",
}
# English orthography → one sound (order matters: digraphs before single letters).
_PHONETIC_RULES = (
    ("tion", "shn"), ("sh", "S"), ("ch", "C"), ("zh", "Z"), ("kh", "h"), ("ph", "f"),
    ("th", "t"), ("ck", "k"), ("qu", "kv"), ("x", "ks"), ("c", "k"), ("q", "k"),
    ("w", "v"), ("h", "g"),
)
_NON_LATIN_RE = re.compile(r"[^a-zA-Z]")
_VOWELS_RE = re.compile(r"[aeiouy]")
_REPEAT_RE = re.compile(r"(.)\1+")
#: How many consonants an ending or a suffix can append
#: («Клод» → «Клодом»: kld → kldm; «Гугл» → «гугловський»: gl → glvsk).
_ENDING_SLACK = 3
#: Prefix comparison of individual words — only with a key of 3 consonants or more:
#: a shorter one (gl, dm) would match anything by prefix.
_PREFIX_MIN_KEY = 3


def phonetic_keys(text: str) -> set[str]:
    """Phonetic keys across languages and scripts: Cyrillic → Latin, English
    digraphs → one sound, vowels dropped, repeats merged.

    «Гугл» and Google → gl, «Телеграм» and Telegram → tlgrm, «Олексій» and «Алексей» → lks;
    «Акмеец» (brgts) and Acmecorp (brgds) differ. The key deliberately does not distinguish vowels:
    it is exactly by them that uk/ru/en translations of one name differ. Latin j gives two
    keys: «дж» («Jason → Джейсон») and «й» («Julia → Юлія»)."""
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
    """An alias is the same name, not a Whisper error: a case («Адаму» for «Адам»),
    a translation or transliteration («Гугл» for Google, «Роботес фонд» for Робота Fund)."""
    if _stems(alias) & _stems(canonical):
        return True
    a_words, c_words = _words(alias), _words(canonical)
    if _prefix_match(phonetic_keys("".join(a_words)), phonetic_keys("".join(c_words))):
        return True
    # Individual words: «гугл диск» for Google, «Julia Bondarenko» for «Юлія».
    a_keys = [phonetic_keys(w) for w in a_words]
    for cw in c_words:
        c_keys = phonetic_keys(cw)
        long_keys = {k for k in c_keys if len(k) >= _PREFIX_MIN_KEY}
        for ak in a_keys:
            if ak & c_keys or (long_keys and _prefix_match(ak, long_keys)):
                return True
    return False


def is_error_alias(alias: str, canonical: str) -> bool:
    """An error alias: not the same name by either stem or phonetic key.

    «Акмеец» for Acmecorp is an error; «Адаму» for «Адам», «Гугл» for Google,
    «Телеграм» for Telegram and «Фонд Робота» for «Робота Fund» are not."""
    if not _stems(alias) or not _stems(canonical):
        return False
    return not same_name(alias, canonical)


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def build_hard_names(db_path: str, limit: int = VOCAB_LIMIT) -> list[str]:
    """A ranked list of "hard" names without a cache (see the module docstring)."""
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
        # Audio transcripts: the text written by Whisper itself (YouTube is other people).
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

    # Word case in the audio texts: how many times it stands lowercase / capitalized.
    raw_counts: Counter[str] = Counter()
    for text in audio_text.values():
        raw_counts.update(_words(text))
    lower_n: Counter[str] = Counter()
    upper_n: Counter[str] = Counter()
    for word, n in raw_counts.items():
        (lower_n if word[0].islower() else upper_n)[word.casefold()] += n

    def is_common(name: str) -> bool:
        # A compound name with a common word («Промінвест (хаб)») is a description,
        # not a title («RAG-система Андрія»); Whisper writes such words correctly anyway.
        return any(lower_n[w] > upper_n[w] for w in (x.casefold() for x in _words(name)))

    folded_text: dict[int, str] = {}

    def occurs(alias: str, tids: list[int]) -> bool:
        needle = alias.casefold()
        pattern = re.compile(r"(?<!\w)" + re.escape(needle) + r"(?!\w)")
        for tid in tids:
            ft = folded_text.get(tid)
            if ft is None:
                ft = folded_text[tid] = audio_text[tid].casefold()
            # A substring is a fast filter; the regex with lookbehind only confirms word boundaries.
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
            continue  # twins of one name across types (project + org)
        seen_names.add(key)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def hard_names(db_path: str, limit: int = VOCAB_LIMIT) -> list[str]:
    """Cached :func:`build_hard_names`: within the TTL it does not touch the DB."""
    key = os.path.abspath(db_path)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return list(hit[1][:limit])
        try:
            names = build_hard_names(db_path, limit=VOCAB_LIMIT)
        except Exception:  # noqa: BLE001 — the vocabulary must not be allowed to stop transcription
            # The graph is unavailable (a new DB without migrations, a lock) — transcription goes on without
            # the vocabulary; an empty list is cached too, so as not to hammer the DB on every live tick.
            logger.warning("name vocabulary not built from %s", db_path, exc_info=True)
            names = []
        _cache[key] = (now + CACHE_TTL_S, names)
        logger.info("name vocabulary rebuilt: %d names", len(names))
        return list(names[:limit])


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def decode_options_for(source_kind: str, db_path: str | None) -> dict:
    """Decoding options for ``transcribe_with_progress`` on the ``source_kind`` path.

    YouTube and disabled flags → ``{}`` (the faster-whisper call as before K1)."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source_kind: {source_kind!r}")
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
    """The gate of the defect card ``translation-counted-as-asr-error``.

    ``path`` — a JSON list of ``{"alias", "canonical"}`` pairs that an artifact (the list of
    measurement fragments, the vocabulary report) presents as Whisper errors. Exit 1 if
    among them there is a translation/transliteration of the same name («Гугл» for Google);
    a file of the wrong format — the gate is not applicable, exit 0."""
    import json

    try:
        with open(path, encoding="utf-8") as fh:
            pairs = json.load(fh)
    except (OSError, ValueError):
        print(f"check-error-pairs: {path} — not a JSON list of pairs, the gate is not applicable")
        return 0
    if not isinstance(pairs, list):
        print(f"check-error-pairs: {path} — not a list, the gate is not applicable")
        return 0
    bad = [p for p in pairs
           if isinstance(p, dict) and p.get("alias") and p.get("canonical")
           and same_name(str(p["alias"]), str(p["canonical"]))]
    for p in bad:
        print(f"translation, not an error: «{p['alias']}» = «{p['canonical']}»")
    print(f"check-error-pairs: {len(bad)} of {len(pairs)} pairs — translations of the same name")
    return 1 if bad else 0


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.services.vocabulary",
                                     description="A dictionary of \"hard\" names (K1).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check-error-pairs", help="defect-card gate: translation ≠ error")
    p_check.add_argument("path")
    p_show = sub.add_parser("show", help="the assembled dictionary (the DB is only read)")
    p_show.add_argument("--db", default="whisper_history.db")
    args = parser.parse_args(argv)
    if args.cmd == "check-error-pairs":
        return check_error_pairs(args.path)
    for name in build_hard_names(args.db):
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
