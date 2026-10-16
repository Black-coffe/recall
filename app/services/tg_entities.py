"""Telegram in the entity graph (Wave 4.5.3).

**The problem.** Telegram is the vast majority of the archive and almost absent from the graph: some records. Because of this the project slice, `list_stale_topics` and the Monday digest are blind
to the bulk of the archive — they see only calls and documents.

**We cure it without a model.** The graph already knows entities from calls. Extracting
entities from TG anew with a local 7B would mean breeding duplicates («Акме»,
«Acmecorp», «акмекорп») in a graph where dedup is unsolved anyway. So TG is not
enriched, it is **linked to what is already known**: we look in the text for mentions
of canonical names and aliases.

**Why a naive match does not work.** Enrichment wrote ordinary
words into the aliases, and on live data this produced junk:

    [project] «Стратегія розвитку Волинської області» ← alias «документ»
    [project] «Договір для Миколи/Адама»              ← alias «договір»
    [person]  «Том»                                   ← alias «Тому» (144 matches
                                                        on the word «тому»)

**Frequency does not save us either.** Measured over the whole archive: among the names occurring
in more than a share of the records there is a mix of junk («документ» 5.5%, «модель» 7.5%) and
key people («адам» 7.0%, «андрій» 6.4%). A frequency threshold would cut out exactly
what the graph is needed for.

**What works is evidence from the place of use.** A mention counts only if in
the message itself it is written as a proper name: with a capital letter and not at the
start of a sentence (a capital after a period proves nothing). «документ» in a
sentence is written lowercase, «Адам» — capitalized. Measured: the junk disappears from the top
completely, and «Ковальчука» — the same case the wave was made for — becomes
visible with 43 mentions.

The price is lower recall (a share of the messages versus 40% naive), but a link in the graph
lives long and spoils slices forever, so precision matters more than recall here.

**We put the link on the MESSAGE with the mention, not on the whole thread.** The temptation
was the opposite — "the name sounds once, and the decision is in the neighboring replies" — but
the expansion to the thread is ALREADY done in 4.5.1a (`scope._expand_to_threads`), and it
works from `tg_thread_id`, not from the graph. Duplicating it here would spoil
the statistics for the sake of something that exists anyway: measured, threads pull many entities
each (median 3, p90 19), and a "every message × every entity of the
thread" link would give 43 482 rows against 10 441 existing. Then `entities.meeting_count`
would say "Адам is mentioned in records", and `list_stale_topics` would consider
every entity of a thread mentioned on the day of the last message — i.e. it would make
fresh what had not been talked about for months.

The two mechanisms add up: the graph answers "where EXACTLY it is mentioned", the thread —
"what was said around it".

**Linking lives on ingest, not only in the CLI.** A pass over the whole archive is
a one-off operation, and while it was the only way, every new message
stayed outside the graph until the next manual run. Now `link_message`
is called from the same job that sets the embeddings (`_submit_embed_only`), right
after the split into threads. The full pass remains — it is needed after changes
in the graph (entity merges/splits add spellings by which old
messages are now found).

CLI:
    python -m app.services.tg_entities link --dry-run
    python -m app.services.tg_entities link --chat -1001234567890
    python -m app.services.tg_entities stats
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Optional

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

#: Provenance of the links this module sets (meeting_entities.source).
SOURCE = "thread_match"

#: Provenance of the morphological matcher links (story 03) — a SEPARATE value
#: from `SOURCE`: an exact word form and a mention by the word stem alone are evidence of
#: different reliability, and `entity_dedup.py`/`comments.py` read exactly `SOURCE`,
#: so morph links must not substitute its value.
SOURCE_MORPH = "thread_morph"

#: Switch of the morph matcher. Default "0" — off: the merge of this story by itself does not
#: change the system's behavior until the owner deliberately turns the flag on
#: (after working through the blockers of story 02). Read by a function, not a one-off
#: module constant (like `rag.py:33`), so the flag can be toggled between
#: calls (tests, a repeated CLI run) without restarting the process.
def _morph_enabled() -> bool:
    return os.environ.get("TG_ENTITIES_MORPH_ENABLED", "0").strip() in ("1", "true", "True")

#: 'topic' is deliberately out of play: 2153 free-text entities («планування»,
#: «бюджет») would catch half the archive and mean nothing. Graph slices
#: are built on people, projects and organizations.
LINKABLE_TYPES = ("person", "project", "org")

#: Shorter names catch junk («Ок», «БХ») and are of no use.
MIN_NAME_LEN = 4

#: How many words a name may contain (we do not search for longer ones — that is no longer a name).
MAX_NAME_WORDS = 6

_WORD = re.compile(r"\w+", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?…\n]\s*$")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", (text or "").strip()).casefold()


def _key(text: str) -> str:
    """Search key: words separated by a single space, without punctuation and case."""
    return " ".join(_WORD.findall(_norm(text)))


#: Case suffixes (Ukr./Rus.) that we fold before comparing stems —
#: our own dependency-free solution (pymorphy/snowball/nltk are forbidden by the story
#: plan, see Non-goals): not morphology but a crude stripping of endings,
#: enough for «Юлією»/«Тетяни»/«Промприладом» to meet the base form
#: («Юлія»/«Тетяна»/«Промприлад»). The longest are checked first, so as not to
#: strip only part of a longer ending as a shorter one.
_CASE_SUFFIXES = tuple(sorted({
    "ями", "ами", "еві", "єві", "ові",
    "ою", "ею", "єю", "ом", "ем", "єм", "ів", "им", "ім", "ий", "ій",
    "а", "я", "у", "ю", "і", "и", "е", "о", "й",
}, key=len, reverse=True))

#: How many letters must remain after stripping a suffix — a shorter remainder
#: matches by accident more often than on substance (e.g. «ти» → «т»).
_STEM_MIN_LEN = 3


def _stem(word: str) -> str:
    """A crude word stem for morphological matching (expects already
    case-folded/NFKC text, like `lowered`/the keys of `names`)."""
    for suf in _CASE_SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= _STEM_MIN_LEN:
            return word[:-len(suf)]
    return word


def _build_stems(names: dict[str, int]) -> dict[str, set[int]]:
    """Stem → set of entity_id among SINGLE-WORD names/aliases. Multi-word
    keys (`names` contains them too) are not touched by morphology — the inflection of
    phrases is beyond plain suffix folding."""
    stems: dict[str, set[int]] = {}
    for key, eid in names.items():
        if " " in key:
            continue
        stems.setdefault(_stem(key), set()).add(eid)
    return stems


def load_names(db_path: str) -> dict[str, int]:
    """Canonical names + aliases → entity_id. The canonical name has priority:
    the same string may be an alias of another entity, and then the exact name
    is truer than a spelling variant."""
    names: dict[str, int] = {}
    ph = ",".join("?" * len(LINKABLE_TYPES))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, canonical_name AS name FROM entities WHERE type IN ({ph})",
            LINKABLE_TYPES).fetchall()
        alias_rows = conn.execute(
            f"SELECT a.entity_id AS id, a.alias AS name FROM entity_aliases a "
            f"JOIN entities e ON e.id = a.entity_id WHERE e.type IN ({ph})",
            LINKABLE_TYPES).fetchall()
    for r in list(rows) + list(alias_rows):
        key = _key(r["name"])
        if len(key) >= MIN_NAME_LEN and len(key.split()) <= MAX_NAME_WORDS:
            names.setdefault(key, r["id"])
    return names


def _mentions_core(text: Optional[str], names: dict[str, int],
                    stems: dict[str, set[int]]) -> dict[int, str]:
    """entity_id → provenance (`SOURCE`/`SOURCE_MORPH`). An empty `stems` means
    the morph branch does not run at all (the flag is off, see
    `_find_mentions_detailed`/`stems_for`).

    The requirement of a capital letter not at the start of a sentence is the only thing that separates «Адам»
    from «документ» and «Том» from «тому» (see the module docstring). This is not
    a heuristic to save effort: without it the top of mentions in the live archive consists
    of junk, and the graph becomes worse than it was.

    Three guards of the morph branch (story 03): (1) the exact word form at a position
    decides everything — the morph branch does not run at that position, even if by
    stem the word points to ANOTHER entity; (2) a stem must point to exactly
    one entity — «Русланою» with existing «Руслан» and «Руслана» is ambiguous
    and rejected (one mention cannot be credited to two); (3) the same
    `written_as_proper_noun` as for exact matches — ALL-CAPS is not evidence.
    """
    result: dict[int, str] = {}
    if not text:
        return result
    tokens = [(m.group(0), m.start()) for m in _WORD.finditer(text)]
    if not tokens:
        return result
    lowered = [t[0].casefold() for t in tokens]

    exact_word_positions: set[int] = set()
    for size in range(1, MAX_NAME_WORDS + 1):
        for i in range(len(tokens) - size + 1):
            entity_id = names.get(" ".join(lowered[i:i + size]))
            if entity_id is None:
                continue
            # Guard 1 works at the TOKEN level, not the match: all words consumed
            # by this exact match (not only the first) are closed to the morph branch
            # — otherwise a word inside a multi-word name stays
            # open and the morph branch could credit it to ANOTHER entity
            # (one spelling cannot be credited to two).
            exact_word_positions.update(range(i, i + size))
            if entity_id in result:
                continue
            word, pos = tokens[i]
            if written_as_proper_noun(text, word, pos):
                result[entity_id] = SOURCE

    if stems:
        for i, (word, pos) in enumerate(tokens):
            if i in exact_word_positions:
                continue  # Guard 1: the exact word form at the position has already decided
            candidates = stems.get(_stem(lowered[i]))
            if not candidates or len(candidates) != 1:
                continue  # Guard 2: a stem must point to exactly one entity
            entity_id = next(iter(candidates))
            if entity_id in result:
                continue
            if written_as_proper_noun(text, word, pos):  # Guard 3: not an all-caps header
                result[entity_id] = SOURCE_MORPH
    return result


def _find_mentions_detailed(text: Optional[str], names: dict[str, int]) -> dict[int, str]:
    """`_mentions_core` that computes the stems itself (for plain calls without
    db_path — CLI/`find_mentions`/tests). Stems are computed only when the flag is
    on: when it is off, the morph branch is guaranteed not to run."""
    stems = _build_stems(names) if _morph_enabled() else {}
    return _mentions_core(text, names, stems)


def find_mentions(text: Optional[str], names: dict[str, int]) -> set[int]:
    """entity_ids mentioned in the text AS PROPER NAMES (exactly or, with the flag
    on, by word stem — see `_mentions_core`)."""
    return set(_find_mentions_detailed(text, names))


def find_exact_mentions(text: Optional[str], names: dict[str, int]) -> set[int]:
    """`find_mentions` that NEVER runs the morph branch — regardless of
    `TG_ENTITIES_MORPH_ENABLED`. For consumers whose measurement sees only the exact
    matcher (`comments.link_entities`): turning the flag on for TG ingest must not
    silently change what another subsystem writes."""
    return set(_mentions_core(text, names, {}))


def is_shouting(text: str, pos: int) -> bool:
    """Whether the line around the position is typed entirely in CAPITAL LETTERS.

    In a contract header («ЩОДО ТЕКСТІВ СТАТЕЙ У РОЗДІЛ WHAT WE DO») all words are
    capitalized in a row, so a capital letter there does not distinguish a proper name
    from a common word — and the whole module rests on that distinction.
    """
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start:end if end != -1 else len(text)]
    letters = [ch for ch in line if ch.isalpha()]
    if len(letters) < 8:                       # too short to draw a conclusion
        return False
    return sum(1 for ch in letters if ch.isupper()) / len(letters) >= 0.8


def written_as_proper_noun(text: str, word: str, pos: int) -> bool:
    """Whether the word is written as a proper name at this place.

    One definition for two layers: `find_mentions` sets links by it, and
    the spelling audit (`entity_dedup.find_junk_aliases`) counts by it the evidence
    "it is a name after all". Once they diverge, they start contradicting each other:
    the audit advised removing a spelling that is producing links right now.
    """
    if not word[:1].isupper():
        return False
    before = text[:pos]
    # A capital letter at the start of a line or sentence proves nothing:
    # «Документ надіслано» is not a project name.
    if not before.strip() or _SENTENCE_END.search(before):
        return False
    return not is_shouting(text, pos)


#: How many seconds we keep the names dictionary between messages. The cache is needed because
#: on ingest `load_names` is 6200 rows for EVERY message, while the graph does not change in
#: seconds. The term is deliberately short: a new entity from a call or a merge
#: should start being caught on their own, without restarting the app.
_NAMES_TTL = float(os.environ.get("TG_ENTITIES_NAMES_TTL", "300"))

_names_lock = threading.Lock()
#: (timestamp, names, stems, the flag state under which the stems were built).
#: Stems are computed together with the names and live in the same cache/TTL —
#: reset_names_cache() drops both in one move. The fourth element exists
#: because the flag can be toggled BETWEEN warm-up and cache read (tests,
#: turning it on in a live process without a restart): a warm-up under OFF leaves the slot
#: `{}` empty, and without this check it would be served under ON too — the morph
#: pass then deletes its rows and has nothing to rewrite them with.
_names_cache: dict[str, tuple[float, dict[str, int], dict[str, set[int]], bool]] = {}


def names_for(db_path: str, *, force: bool = False) -> dict[str, int]:
    """The names dictionary with a short cache (see `_NAMES_TTL`)."""
    now = time.monotonic()
    morph_now = _morph_enabled()
    with _names_lock:
        hit = _names_cache.get(db_path)
        if hit and not force and (now - hit[0]) < _NAMES_TTL and hit[3] == morph_now:
            return hit[1]
    names = load_names(db_path)          # outside the lock: reading the DB can be long
    # Stems are computed only with the flag on — a disabled matcher
    # pays nothing for them, even on a cache warm-up.
    stems = _build_stems(names) if morph_now else {}
    with _names_lock:
        _names_cache[db_path] = (now, names, stems, morph_now)
    return names


def stems_for(db_path: str, *, force: bool = False) -> dict[str, set[int]]:
    """Word stems for the morph matcher — the same cache as `names_for`
    (warms/refreshes it if needed)."""
    names_for(db_path, force=force)
    with _names_lock:
        return _names_cache[db_path][2]


def reset_names_cache() -> None:
    """Forget the cache of names and stems. Needed by tests and after bulk changes in the graph."""
    with _names_lock:
        _names_cache.clear()


def link_message(db_path: str, transcription_id: int) -> dict:
    """Link ONE message to the graph — an ingest step.

    Idempotent: this record's own links are rewritten, foreign ones (from
    Claude enrichment, `source IS NULL`) are not touched. Called after a
    message edit the same as after the first appearance: the text changed —
    mentions could have appeared or disappeared.

    The thread is NOT needed here (unlike the full pass): a mention is proven by
    the text of the message itself, not by which conversation it was assigned to. If
    the split into threads did not work, the record still gets into the graph.
    """
    names = names_for(db_path)
    if not names:
        return {"transcription_id": transcription_id, "status": "no_entities"}

    morph_on = _morph_enabled()
    stems = stems_for(db_path) if morph_on else {}

    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, transcript_text FROM transcriptions "
            "WHERE id = ? AND source_type = 'telegram' AND deleted_at IS NULL",
            (transcription_id,)).fetchone()
        if row is None:
            return {"transcription_id": transcription_id, "status": "not_found"}

        hits = _mentions_core(row["transcript_text"], names, stems)
        conn.execute("DELETE FROM meeting_entities WHERE source = ? AND transcription_id = ?",
                     (SOURCE, transcription_id))
        if morph_on:
            # The morph pass touches only its own rows — the exact SOURCE is removed
            # by the separate DELETE above and does not concern the morph branch.
            conn.execute("DELETE FROM meeting_entities WHERE source = ? AND transcription_id = ?",
                         (SOURCE_MORPH, transcription_id))
        written = 0
        for eid, src in hits.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, source) "
                "VALUES (?, ?, 1, ?)", (transcription_id, eid, src))
            written += cur.rowcount
        conn.commit()
    return {"transcription_id": transcription_id, "status": "ok",
            "mentions": len(hits), "written": written}


def link_threads(db_path: str, *, chat_id: Optional[int] = None,
                 dry_run: bool = True) -> dict:
    """Link Telegram threads to the existing graph entities.

    Idempotent: links with our provenance are rewritten, foreign ones (from
    Claude enrichment, source IS NULL) are not touched — they are more reliable."""
    names = load_names(db_path)
    if not names:
        return {"error": "there are no entities in the graph to link"}

    morph_on = _morph_enabled()
    stems = _build_stems(names) if morph_on else {}

    where = ["source_type = 'telegram'", "deleted_at IS NULL", "tg_thread_id IS NOT NULL"]
    params: list = []
    if chat_id is not None:
        where.append("tg_chat_id = ?")
        params.append(chat_id)

    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, tg_thread_id, transcript_text FROM transcriptions "
            f"WHERE {' AND '.join(where)}", params).fetchall()

        # A mention is where it is written. Expansion to the thread lives separately
        # (scope._expand_to_threads) and must not be duplicated in the graph.
        per_message: dict[int, dict[int, str]] = {}
        threads_seen: set[int] = set()
        threads_touched: set[int] = set()
        for r in rows:
            threads_seen.add(r["tg_thread_id"])
            hits = _mentions_core(r["transcript_text"], names, stems)
            if hits:
                per_message[r["id"]] = hits
                threads_touched.add(r["tg_thread_id"])

        result = {"threads_total": len(threads_seen),
                  "threads_with_mentions": len(threads_touched),
                  "messages_total": len(rows), "messages_linked": len(per_message),
                  "entities": len({e for s in per_message.values() for e in s}),
                  "links": sum(len(s) for s in per_message.values())}
        if dry_run:
            return {"dry_run": True, **result}

        # We remove our own links before the rewrite; foreign ones stay. The exact and
        # morph provenances are removed by separate DELETEs — the morph pass touches only
        # its own rows; when the flag is off we do not even touch SOURCE_MORPH.
        own_sources = (SOURCE, SOURCE_MORPH) if morph_on else (SOURCE,)
        tids = [r["id"] for r in rows]
        for i in range(0, len(tids), 500):
            batch = tids[i:i + 500]
            ph = ",".join("?" * len(batch))
            for src in own_sources:
                conn.execute(
                    f"DELETE FROM meeting_entities WHERE source = ? AND transcription_id IN "
                    f"({ph})", [src, *batch])

        written = 0
        for tid, entity_map in per_message.items():
            for eid, src in entity_map.items():
                # INSERT OR IGNORE: if Claude has already set the link —
                # we keep its version (it has salience and a role).
                cur = conn.execute(
                    "INSERT OR IGNORE INTO meeting_entities "
                    "(transcription_id, entity_id, mention_count, source) "
                    "VALUES (?, ?, 1, ?)", (tid, eid, src))
                written += cur.rowcount
        conn.commit()

    return {"dry_run": False, **result, "written": written}


def stats(db_path: str) -> dict:
    with get_db_connection(db_path) as conn:
        total_tg = conn.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE source_type='telegram' "
            "AND deleted_at IS NULL").fetchone()[0]
        linked_tg = conn.execute(
            "SELECT COUNT(DISTINCT me.transcription_id) FROM meeting_entities me "
            "JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.source_type='telegram'").fetchone()[0]
        by_source = dict(conn.execute(
            "SELECT COALESCE(source, 'claude'), COUNT(*) FROM meeting_entities "
            "GROUP BY 1").fetchall())
        top = conn.execute(
            "SELECT e.type, e.canonical_name, COUNT(DISTINCT me.transcription_id) n "
            "FROM meeting_entities me JOIN entities e ON e.id = me.entity_id "
            "WHERE me.source = ? GROUP BY e.id ORDER BY n DESC LIMIT 10",
            (SOURCE,)).fetchall()
    return {"telegram_total": total_tg, "telegram_linked": linked_tg,
            "links_by_source": by_source,
            "top_entities": [{"type": r["type"], "name": r["canonical_name"],
                              "records": r["n"]} for r in top]}


def _print(res: dict) -> int:
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Optional[list] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except ImportError:
        pass

    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(prog="tg_entities",
                                description="Telegram in the entity graph (Wave 4.5.3).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)
    l = sub.add_parser("link", help="Link threads to the existing entities")
    l.add_argument("--dry-run", action="store_true")
    l.add_argument("--chat", type=int, default=None)
    sub.add_parser("stats", help="Graph coverage")

    args = p.parse_args(argv)
    if args.command == "link":
        return _print(link_threads(args.db, chat_id=args.chat, dry_run=args.dry_run))
    return _print(stats(args.db))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
