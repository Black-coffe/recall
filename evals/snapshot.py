"""VACUUM INTO знімок БД для evals/gate.py (eval-gate, історія 01).

Джерело відкривається СТРОГО `mode=ro` (SQLite фізично забороняє запис,
не лише конвенція коду) — гейт ганяється на copy-БД і не сміє мутувати
бойовий файл (ADR-003). Live-db-гард (`is_live_db`/`open_readonly`) лишається
в `evals/graph_links.py` — цей модуль лише вміє зробити знімок, рішення
"чи знімати" приймає `evals/gate.py`.
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
    """URI для `sqlite3.connect(..., uri=True)` у режимі `mode=ro`."""
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def make_snapshot(src: str, dst: str) -> str:
    """Скопіювати `src` у `dst` через `VACUUM INTO` з read-only зʼєднання.

    `src` відкривається лише `mode=ro` — SQLite не дозволить запис навіть
    якщо щось спробує, тож джерело гарантовано лишається байт-у-байт
    незмінним (хеш/mtime). `dst` створює сам SQLite (`VACUUM INTO` вимагає,
    щоб файл ще не існував) — якщо старий знімок з такою назвою лишився,
    прибираємо його перед викликом.
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
    """`evals/snapshots/<db-basename>-<YYYYMMDD-HHMMSS>.db` (план, C5)."""
    basename = Path(src).stem
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(_SNAPSHOT_DIR / f"{basename}-{stamp}.db")


def _meta_path(snapshot_path: str) -> str:
    return snapshot_path + ".meta.json"


def _src_identity(src: str) -> dict:
    st = os.stat(src)
    return {"src": str(Path(src).resolve()), "src_size": st.st_size, "src_mtime": st.st_mtime}


def _existing_snapshot_for(src: str, basename: str) -> Optional[str]:
    """Знімок з тим самим basename, чиє джерело (розмір+mtime) збігається з
    поточним `src` (D8) — джерело не змінилось, новий знімок не потрібен."""
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
            # Не ковтаємо мовчки (CLAUDE.md) — побитий метафайл лише
            # пропускається (нижче створиться новий знімок), не крашить гейт.
            print(f"[snapshot] пошкоджений метафайл {meta_file}, пропускаю: {exc}",
                  file=sys.stderr)
            continue
        if (meta.get("src") == identity["src"]
                and meta.get("src_size") == identity["src_size"]
                and meta.get("src_mtime") == identity["src_mtime"]):
            return snap_path
    return None


def _norm(path: str) -> str:
    """Нормалізація для порівняння шляхів знімків (case/`.`/`..`) — той самий
    файл мусить збігатися незалежно від того, як його записали (`os.path.abspath`
    у `gate.py` чи `_SNAPSHOT_DIR / ...` тут)."""
    return os.path.normcase(os.path.abspath(path))


def _remove_snapshot_files(db_stem: str) -> None:
    """Прибрати знімок цілком: `.db` + сайдкари `-wal`/`-shm` + `.meta.json`
    (знахідки 13/14 — прибирання лише `.db` лишає сирітські файли, які
    жоден наступний прогін уже не претендує почистити)."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_stem + suffix)
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError as exc:
                print(f"[snapshot] не вдалось прибрати {candidate}: {exc}", file=sys.stderr)
    meta_file = Path(_meta_path(db_stem))
    if meta_file.exists():
        try:
            meta_file.unlink()
        except OSError as exc:
            print(f"[snapshot] не вдалось прибрати метафайл {meta_file}: {exc}",
                  file=sys.stderr)


def _prune_other_snapshots(basename: str, keep: str,
                            protect: Iterable[str] = ()) -> None:
    """Прибрати всі знімки з тим самим basename, крім `keep` (D8 — знімок не
    плодиться безкінечно; `whisper_history.db` важить 315 МБ) і крім
    `protect` (D20/знахідка 5): шляхи знімків, названих ЖИВОЮ базовою лінією
    (`db_snapshot.path` файлу, який ще не перезаписано `--write-baseline`).
    D8 і D5 інакше знищують одне одного — прунер видаляв саме той знімок,
    без якого лінія стає невідтворюваною назавжди."""
    keep_norm = {_norm(keep)} | {_norm(p) for p in protect}
    for db_file in _SNAPSHOT_DIR.glob(f"{basename}-*.db"):
        if _norm(str(db_file)) in keep_norm:
            continue
        _remove_snapshot_files(str(db_file))

    # Сироти від попередніх багів чи перерваних прогонів (знахідка 14) —
    # `-wal`/`-shm` без `.db` поруч (напр. `.db` уже прибраний раніше, коли
    # прунер ще не чистив сайдкари). Не чіпаємо ті, чий `.db` під захистом.
    for suffix in ("-wal", "-shm"):
        for sidecar in _SNAPSHOT_DIR.glob(f"{basename}-*.db{suffix}"):
            db_stem = str(sidecar)[: -len(suffix)]
            if _norm(db_stem) in keep_norm or Path(db_stem).exists():
                continue
            try:
                sidecar.unlink()
            except OSError as exc:
                print(f"[snapshot] не вдалось прибрати сирітський сайдкар {sidecar}: {exc}",
                      file=sys.stderr)


def auto_snapshot(src: str, protect: Iterable[str] = ()) -> str:
    """Знімок `src` за дефолтним шляхом (C5) — те, що `gate.py` робить
    автоматично, коли `--db` вказує на живу БД (без `--no-snapshot`).

    D8: якщо джерело не змінилось із часу останнього знімка з тим самим
    basename (розмір+mtime), той знімок перевикористовується замість нового
    315-МБ файлу; інакше створюється новий, а старі з тим самим basename
    прибираються (максимум один живий знімок на джерело) — КРІМ шляхів
    у `protect` (D20): знімки, названі живими базовими лініями, `gate.py`
    передає сюди, щоб прунер не знищив артефакт, без якого лінію вже не
    відтворити."""
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
