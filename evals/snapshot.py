"""VACUUM INTO snapshot of the DB for evals/gate.py (eval-gate, story 01).

The source is opened STRICTLY with `mode=ro` (SQLite physically forbids writes,
not just a code convention): the gate runs on a copy DB and must not mutate
the live file (ADR-003). The live-db guard (`is_live_db`/`open_readonly`) stays
in `evals/graph_links.py`; this module only knows how to take a snapshot, the decision
of "whether to snapshot" is made by `evals/gate.py`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT_DIR = _PROJECT_ROOT / "evals" / "snapshots"


def _ro_uri(db_path: str) -> str:
    """URI for `sqlite3.connect(..., uri=True)` in `mode=ro`."""
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def make_snapshot(src: str, dst: str) -> str:
    """Copy `src` to `dst` via `VACUUM INTO` from a read-only connection.

    `src` is opened only with `mode=ro`: SQLite will not allow a write even
    if something tries, so the source is guaranteed to stay byte-for-byte
    unchanged (hash/mtime). `dst` is created by SQLite itself (`VACUUM INTO` requires
    the file not to exist yet): if an old snapshot with that name is left over,
    it is removed before the call.
    """
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        dst_path.unlink()
    conn = sqlite3.connect(_ro_uri(src), uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(dst_path),))
    finally:
        conn.close()
    return str(dst_path)


def default_snapshot_path(src: str) -> str:
    """`evals/snapshots/<db-basename>-<YYYYMMDD-HHMMSS>.db` (plan, C5)."""
    basename = Path(src).stem
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(_SNAPSHOT_DIR / f"{basename}-{stamp}.db")


def _meta_path(snapshot_path: str) -> str:
    return snapshot_path + ".meta.json"


def _src_identity(src: str) -> dict:
    st = os.stat(src)
    return {"src": str(Path(src).resolve()), "src_size": st.st_size, "src_mtime": st.st_mtime}


def _existing_snapshot_for(src: str, basename: str) -> Optional[str]:
    """A snapshot with the same basename whose source (size+mtime) matches
    the current `src` (D8): the source has not changed, no new snapshot is needed."""
    if not _SNAPSHOT_DIR.exists():
        return None
    identity = _src_identity(src)
    for meta_file in sorted(_SNAPSHOT_DIR.glob(f"{basename}-*.db.meta.json")):
        snap_path = str(meta_file)[: -len(".meta.json")]
        if not os.path.exists(snap_path):
            continue
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            # Do not swallow silently (CLAUDE.md): a broken metafile is only
            # skipped (a new snapshot is created below), it does not crash the gate.
            print(f"[snapshot] corrupted metafile {meta_file}, skipping: {exc}",
                  file=sys.stderr)
            continue
        if (meta.get("src") == identity["src"]
                and meta.get("src_size") == identity["src_size"]
                and meta.get("src_mtime") == identity["src_mtime"]):
            return snap_path
    return None


def _norm(path: str) -> str:
    """Normalization for comparing snapshot paths (case/`.`/`..`): the same
    file must match regardless of how it was written (`os.path.abspath`
    in `gate.py` or `_SNAPSHOT_DIR / ...` here)."""
    return os.path.normcase(os.path.abspath(path))


def _remove_snapshot_files(db_stem: str) -> None:
    """Remove a snapshot entirely: `.db` + `-wal`/`-shm` sidecars + `.meta.json`
    (findings 13/14: removing only `.db` leaves orphan files that
    no later run will ever claim to clean up)."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_stem + suffix)
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError as exc:
                print(f"[snapshot] failed to remove {candidate}: {exc}", file=sys.stderr)
    meta_file = Path(_meta_path(db_stem))
    if meta_file.exists():
        try:
            meta_file.unlink()
        except OSError as exc:
            print(f"[snapshot] failed to remove metafile {meta_file}: {exc}",
                  file=sys.stderr)


def _prune_other_snapshots(basename: str, keep: str,
                            protect: Iterable[str] = ()) -> None:
    """Remove all snapshots with the same basename except `keep` (D8: a snapshot must not
    multiply endlessly; `whisper_history.db` weighs 315 MB) and except
    `protect` (D20/finding 5): paths of snapshots named by a LIVE baseline
    (the `db_snapshot.path` of a file not yet overwritten by `--write-baseline`).
    Otherwise D8 and D5 destroy each other: the pruner deleted exactly the snapshot
    without which the baseline becomes unreproducible forever."""
    keep_norm = {_norm(keep)} | {_norm(p) for p in protect}
    for db_file in _SNAPSHOT_DIR.glob(f"{basename}-*.db"):
        if _norm(str(db_file)) in keep_norm:
            continue
        _remove_snapshot_files(str(db_file))

    # Orphans from earlier bugs or interrupted runs (finding 14):
    # `-wal`/`-shm` without a `.db` next to them (e.g. the `.db` was already removed earlier, when
    # the pruner did not yet clean sidecars). Do not touch those whose `.db` is protected.
    for suffix in ("-wal", "-shm"):
        for sidecar in _SNAPSHOT_DIR.glob(f"{basename}-*.db{suffix}"):
            db_stem = str(sidecar)[: -len(suffix)]
            if _norm(db_stem) in keep_norm or Path(db_stem).exists():
                continue
            try:
                sidecar.unlink()
            except OSError as exc:
                print(f"[snapshot] failed to remove orphan sidecar {sidecar}: {exc}",
                      file=sys.stderr)


def auto_snapshot(src: str, protect: Iterable[str] = ()) -> str:
    """Snapshot of `src` at the default path (C5): what `gate.py` does
    automatically when `--db` points to a live DB (without `--no-snapshot`).

    D8: if the source has not changed since the last snapshot with the same
    basename (size+mtime), that snapshot is reused instead of a new
    315 MB file; otherwise a new one is created and old ones with the same basename
    are removed (at most one live snapshot per source), EXCEPT paths
    in `protect` (D20): snapshots named by live baselines, which `gate.py`
    passes here so the pruner does not destroy the artifact without which the baseline can
    no longer be reproduced."""
    basename = Path(src).stem
    existing = _existing_snapshot_for(src, basename)
    if existing:
        return existing

    dst = default_snapshot_path(src)
    make_snapshot(src, dst)
    with open(_meta_path(dst), "w", encoding="utf-8") as f:
        json.dump(_src_identity(src), f)
    _prune_other_snapshots(basename, dst, protect=protect)
    return dst
