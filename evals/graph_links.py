"""Re-measurement of TG graph links on a DB SNAPSHOT (entity-graph-tg, story 04).

**Why.** Story 03 added a morph matcher (`app/services/tg_entities.py`:
`SOURCE_MORPH`, `TG_ENTITIES_MORPH_ENABLED`, off by default); the decision
"whether to enable it" must be made on numbers, not impressions. `evals/run_eval.py`
measures RAG (recall@k) and does not see the graph; this is a separate module, not an extension of the
RAG eval (`evals/metrics.py`/`run_eval.py` are not touched here).

**Read-only, on a snapshot only.** The connection is in `file:...?mode=ro` mode.
An attempt to open a file that matches `Config.DATABASE` (the live DB) without
`--yes-live` is refused: the snapshot is made by the owner via `scripts/backup_db.ps1`
(`VACUUM INTO`), not by this module (Non-goals: there is deliberately NO VACUUM here).

**Two slices, no automatic gate.** We count exact-only (how it lives now
by default) and exact+morph (the morph branch "as if" - enabled only for
the duration of the count, nothing is written to the DB) over the SAME TG
messages of the snapshot, and return the per-entity delta. As in `evals/README.md`
(154-159): no "X% drop = fail", just two runs + a manual diff.

**Second layer of evidence** (lesson `derived-claims-need-second-source`): next to the
link count, the number of DISTINCT messages and DISTINCT chats in which the
entity occurred, so that growth from a single chat/single entity
(like «Україні» - 25% of the growth in the 14.08 measurement) is visible rather than hidden behind
the total number.

Examples:
    python -m evals.graph_links --db path\\to\\snapshot.db
    python -m evals.graph_links --db path\\to\\snapshot.db --json-out evals/graph_links_run1.json
    python -m evals.graph_links --db path\\to\\snapshot.db --top-n 30
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Allows running both as `python evals/graph_links.py` and as
# `python -m evals.graph_links` without installing the package (the same trick as
# in evals/run_eval.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.services import entity_dedup  # noqa: E402
from app.services import tg_entities  # noqa: E402

#: The environment variable by which `tg_entities._morph_enabled()`/`find_mentions`
#: decide whether to run the morph branch. A story 03 contract, not our own name.
_MORPH_ENV = "TG_ENTITIES_MORPH_ENABLED"


# ============================================================
# Read-only access to the snapshot
# ============================================================

def _ro_uri(db_path: str) -> str:
    """URI for `sqlite3.connect(..., uri=True)` in `mode=ro`: the file
    is opened read-only at the SQLite level, not merely "we do not write"."""
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def _live_db_path() -> Optional[str]:
    """Absolute path of the live DB (`Config.DATABASE`). A separate function:
    tests replace it without touching the real `config.py`/file system."""
    try:
        from config import Config
    except Exception:  # noqa: BLE001 - config may be unavailable in CI/venv without .env
        return None
    return str((Config.BASE_DIR / Config.DATABASE).resolve())


def is_live_db(db_path: str) -> bool:
    """Whether `db_path` matches the live DB by absolute path."""
    live = _live_db_path()
    if live is None:
        return False
    return str(Path(db_path).resolve()) == live


def open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def _scratch_copy(db_path: str) -> Iterator[str]:
    """A temporary copy of the snapshot for functions that do NOT support `mode=ro`:
    `tg_entities.load_names` and `entity_dedup.find_alias_cross_type_collisions`
    open their own connection via `get_db_connection`, which sets
    `PRAGMA journal_mode=WAL`; on a snapshot in `delete` mode this flips the file's
    journal_mode and leaves `-wal`/`-shm` next to it. The copy takes this
    side effect upon itself: the snapshot we measure stays byte-for-byte
    unchanged."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copyfile(db_path, tmp_path)
    try:
        yield tmp_path
    finally:
        for suffix in ("", "-wal", "-shm"):
            candidate = tmp_path + suffix
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    pass


# ============================================================
# Existing state of the graph (as recorded in the snapshot)
# ============================================================

def existing_graph_state(conn: sqlite3.Connection) -> dict:
    """Graph links already present in the snapshot, broken down by `source`
    (NULL = inherited from Claude enrichment, `thread_match`/`thread_morph`
    - this module/`tg_entities.py`)."""
    rows = conn.execute(
        "SELECT COALESCE(source, 'NULL') AS src, COUNT(*) AS n, "
        "COUNT(DISTINCT entity_id) AS entities FROM meeting_entities "
        "GROUP BY 1").fetchall()
    total_entities = conn.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM meeting_entities").fetchone()[0]
    return {
        "links_by_source": {r["src"]: r["n"] for r in rows},
        "entities_by_source": {r["src"]: r["entities"] for r in rows},
        "entities_with_any_link": total_entities,
    }


def _tg_rows(conn: sqlite3.Connection) -> list[tuple[int, Optional[int], str]]:
    rows = conn.execute(
        "SELECT id, tg_chat_id, transcript_text FROM transcriptions "
        "WHERE source_type = 'telegram' AND deleted_at IS NULL").fetchall()
    return [(r["id"], r["tg_chat_id"], r["transcript_text"] or "") for r in rows]


def _entity_meta(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute("SELECT id, type, canonical_name FROM entities").fetchall()
    return {r["id"]: {"type": r["type"], "name": r["canonical_name"]} for r in rows}


def _collision_entity_ids(db_path: str) -> set[int]:
    """Entities involved in a cross-type alias collision
    (`entity_dedup.find_alias_cross_type_collisions`): a caveat flag
    in the per-entity diff: growth of the morph branch on such an entity is worth a double
    check by eye before treating it as evidence."""
    ids: set[int] = set()
    for group in entity_dedup.find_alias_cross_type_collisions(db_path):
        if "keep" in group:
            ids.add(group["keep"]["id"])
            ids.update(m["id"] for m in group.get("merge", []))
        for m in group.get("members", []):
            ids.add(m["id"])
    return ids


# ============================================================
# "As if" simulation (exact-only / exact+morph): nothing is written
# ============================================================

def _simulate(rows: list[tuple[int, Optional[int], str]], names: dict[str, int],
              *, morph: bool) -> dict[int, dict]:
    """entity_id -> {"links": number of messages with a mention, "records":
    set(transcription_id), "chats": set(tg_chat_id)}.

    `find_mentions` decides whether to run the morph branch by the environment variable
    `TG_ENTITIES_MORPH_ENABLED` (a story 03 contract); we temporarily set
    it for the duration of counting one slice and restore it afterwards. Nothing is
    written to the DB: we count over the snapshot text, `meeting_entities` is not touched.
    """
    prev = os.environ.get(_MORPH_ENV)
    os.environ[_MORPH_ENV] = "1" if morph else "0"
    try:
        out: dict[int, dict] = {}
        for tid, chat_id, text in rows:
            for eid in tg_entities.find_mentions(text, names):
                slot = out.setdefault(eid, {"links": 0, "records": set(), "chats": set()})
                slot["links"] += 1
                slot["records"].add(tid)
                if chat_id is not None:
                    slot["chats"].add(chat_id)
        return out
    finally:
        if prev is None:
            os.environ.pop(_MORPH_ENV, None)
        else:
            os.environ[_MORPH_ENV] = prev


def per_entity_diff(exact: dict[int, dict], morph: dict[int, dict],
                     entity_meta: dict[int, dict], collisions: set[int]) -> list[dict]:
    """A row per entity: `+N` morph-branch links over exact-only, plus
    the second layer of evidence (distinct records/chats). Sorting is stable
    (`-delta, -morph_links, entity_id`) so that the `--json-out` of two runs
    can be diffed directly."""
    empty = {"links": 0, "records": set(), "chats": set()}
    out = []
    for eid in sorted(set(exact) | set(morph)):
        ex = exact.get(eid, empty)
        mo = morph.get(eid, empty)
        meta = entity_meta.get(eid, {"type": "?", "name": f"#{eid}"})
        out.append({
            "entity_id": eid,
            "type": meta["type"],
            "name": meta["name"],
            "exact_links": ex["links"],
            "morph_links": mo["links"],
            "delta": mo["links"] - ex["links"],
            "distinct_records": len(mo["records"] | ex["records"]),
            "distinct_chats": len(mo["chats"] | ex["chats"]),
            "cross_type_collision": eid in collisions,
        })
    out.sort(key=lambda r: (-r["delta"], -r["morph_links"], r["entity_id"]))
    return out


def build_report(conn: sqlite3.Connection, db_path: str) -> dict:
    """`top_n` is deliberately absent here: the JSON artifact carries the full per-entity diff
    (`report["diff"]`) regardless of how many rows are printed to the
    terminal; `--top-n` affects only `_print_report`."""
    existing = existing_graph_state(conn)
    entity_meta = _entity_meta(conn)
    tg_rows = _tg_rows(conn)

    with _scratch_copy(db_path) as scratch:
        names = tg_entities.load_names(scratch)
        exact = _simulate(tg_rows, names, morph=False)
        morph = _simulate(tg_rows, names, morph=True)
        collisions = _collision_entity_ids(scratch)
    diff = per_entity_diff(exact, morph, entity_meta, collisions)

    exact_links = sum(v["links"] for v in exact.values())
    morph_links = sum(v["links"] for v in morph.values())

    return {
        "db": os.path.abspath(db_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "existing_graph": existing,
        "simulation": {
            "messages_total": len(tg_rows),
            "exact_only": {"total_links": exact_links, "entities_with_links": len(exact)},
            "exact_plus_morph": {"total_links": morph_links, "entities_with_links": len(morph)},
            "delta": {"links": morph_links - exact_links, "entities": len(morph) - len(exact)},
        },
        "diff": diff,
    }


def _print_report(report: dict, *, top_n: int) -> None:
    print()
    print(f"[graph_links] DB: {report['db']}")
    print("-" * 60)
    eg = report["existing_graph"]
    print("Existing links by source (in the snapshot):")
    for src, n in sorted(eg["links_by_source"].items()):
        print(f"  {src:<14} links={n:<6} entities={eg['entities_by_source'].get(src, 0)}")
    print(f"  entities_with_any_link (all sources together): {eg['entities_with_any_link']}")
    print("-" * 60)
    sim = report["simulation"]
    print(f"\"As if\" simulation ({sim['messages_total']} TG messages):")
    print(f"  exact-only:      links={sim['exact_only']['total_links']:<6} "
          f"entities={sim['exact_only']['entities_with_links']}")
    print(f"  exact+morph:     links={sim['exact_plus_morph']['total_links']:<6} "
          f"entities={sim['exact_plus_morph']['entities_with_links']}")
    print(f"  delta:           links={sim['delta']['links']:+d}  entities={sim['delta']['entities']:+d}")
    print("-" * 60)
    shown = [r for r in report["diff"][:top_n] if r["delta"] != 0]
    print(f"TOP-{len(shown)} by growth (morph branch):")
    for r in shown:
        flag = " ⚠ cross-type collision" if r["cross_type_collision"] else ""
        print(f"  [{r['type']}] {r['name']}: +{r['delta']} "
              f"(exact={r['exact_links']} morph={r['morph_links']}, "
              f"records={r['distinct_records']}, chats={r['distinct_chats']}){flag}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True,
                         help="Path to the DB SNAPSHOT (VACUUM INTO - scripts/backup_db.ps1), read-only")
    parser.add_argument("--json-out", default=None, help="Write the full report to a JSON file")
    parser.add_argument("--top-n", type=int, default=20,
                         help="How many entities to show in TOP-by-growth (default: 20)")
    parser.add_argument("--yes-live", action="store_true",
                         help="Allow opening the file that matches Config.DATABASE "
                              "(forbidden by default - this tooling is for a snapshot)")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[graph_links] DB not found: {args.db}", file=sys.stderr)
        return 2

    if is_live_db(args.db) and not args.yes_live:
        print(f"[graph_links] Refused: {args.db!r} is the live DB (Config.DATABASE). "
              "This tooling works on a SNAPSHOT (VACUUM INTO - scripts/backup_db.ps1), "
              "not on a live file. If this is a deliberate choice, add --yes-live.",
              file=sys.stderr)
        return 2

    conn = open_readonly(args.db)
    try:
        report = build_report(conn, args.db)
    finally:
        conn.close()

    _print_report(report, top_n=args.top_n)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"\n[graph_links] Full result written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
