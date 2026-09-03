"""Читання і валідація golden-set для evals/gate.py (eval-gate, історія 01).

Формат — контракт C1 плану (docs/specs/eval-gate/plan.md): JSONL, один
запис на рядок (сучасний формат), або legacy `{"items": [...]}` / голий
список (стара схема `evals/run_eval.py`, читається без конвертації файлу —
лише в памʼяті). Це ЛИШЕ читання і нормалізація — побудова набору (мінінг
логу, розмітка) — окрема історія (`evals/build_golden.py`, історія 02).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional


class GoldenSetError(ValueError):
    """Невалідний golden-set — `evals.gate` ловить і повертає exit 2. Єдиний
    тип помилки для `read_jsonl` (D13/D3) — файл не знайдено, нечитний, рядок
    не є валідним JSON-обʼєктом. Кожен CLI ловить цей ОДИН тип замість трьох."""


_VALID_SLICES = ("calls", "tg", "docs")
_VALID_STATUS = ("labeled", "unlabeled", "negative")

# Та сама мапа, що C8 (мінінг логу) буде використовувати для нових
# записів — тут лише для інференсу `slice` у legacy-записах.
_SOURCE_TYPE_TO_SLICE = {"telegram": "tg", "document": "docs"}


def _slice_from_source_type(source_type: Optional[str]) -> str:
    if not source_type:
        return "calls"
    return _SOURCE_TYPE_TO_SLICE.get(source_type, "calls")


def _where(line_no: Optional[int]) -> str:
    return f" (рядок {line_no})" if line_no else ""


def _normalize(item: dict, *, source: str, default_status: str,
               line_no: Optional[int] = None) -> dict:
    item_id = item.get("id")
    if not item_id:
        raise GoldenSetError(f"golden-set: запис без 'id'{_where(line_no)}")
    if not item.get("question"):
        raise GoldenSetError(f"golden-set {item_id!r}: без 'question'{_where(line_no)}")

    slice_ = item.get("slice") or "calls"
    if slice_ not in _VALID_SLICES:
        raise GoldenSetError(
            f"golden-set {item_id!r}: невалідний slice {slice_!r}{_where(line_no)}")

    status = item.get("status") or default_status
    if status not in _VALID_STATUS:
        raise GoldenSetError(
            f"golden-set {item_id!r}: невалідний status {status!r}{_where(line_no)}")

    return {
        "id": item_id,
        "question": item["question"],
        "slice": slice_,
        "category_id": item.get("category_id"),
        "expected_transcription_ids": item.get("expected_transcription_ids") or [],
        "expected_source_name_contains": item.get("expected_source_name_contains") or [],
        "expected_facts": item.get("expected_facts") or [],
        "notes": item.get("notes", ""),
        "source": item.get("source") or source,
        "status": status,
        "candidates": item.get("candidates") or [],
    }


def _lookup_source_type(conn: Optional[sqlite3.Connection],
                         transcription_ids: list) -> Optional[str]:
    if conn is None or not transcription_ids:
        return None
    try:
        row = conn.execute(
            "SELECT source_type FROM transcriptions WHERE id = ?",
            (transcription_ids[0],),
        ).fetchone()
    except sqlite3.Error as exc:
        # Не ковтаємо мовчки (CLAUDE.md: no bare except without a log; finding
        # 18) — побитий знімок/відсутня таблиця стають видимими, а не тихим
        # дефолтом slice="calls".
        print(f"[golden_io] не вдалось визначити source_type для "
              f"{transcription_ids[0]}: {exc}", file=sys.stderr)
        return None
    return row[0] if row is not None else None


def read_jsonl(path: str) -> list[dict]:
    """Спільний зчитувач JSONL для ВСІХ eval-CLI (D13, знахідка 21) — один
    JSON-обʼєкт на непорожній рядок, БЕЗ golden-set-специфічної нормалізації
    (`id`/`slice`/`status`/... лишаються шаром `read_golden`/`_normalize` для
    golden-set і власною схемою кожного CLI для інших наборів, напр.
    tasks-golden). Контракт фіксований: `read_jsonl(path) -> list[dict]`.

    Кидає ЄДИНИЙ тип помилки — `GoldenSetError` (D3): файл не знайдено,
    нечитний, або рядок не є валідним JSON-обʼєктом (з номером рядка) —
    кожен CLI ловить один `except GoldenSetError` і мапить у exit 2."""
    p = Path(path)
    if not p.exists():
        raise GoldenSetError(f"golden-set {path}: файл не знайдено")
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldenSetError(f"golden-set {path}: не вдалось прочитати: {exc}") from exc

    items = []
    for line_no, raw in enumerate(raw_text.splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GoldenSetError(
                f"golden-set {path}: невалідний JSON у рядку {line_no}: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise GoldenSetError(
                f"golden-set {path}: рядок {line_no} має бути обʼєктом")
        items.append(obj)
    return items


def _read_jsonl(path: Path) -> list[dict]:
    """Golden-set-специфічний шар над `read_jsonl` — нормалізує кожен сирий
    обʼєкт (`id`/`slice`/`status`/...). Позиція у списку (1-based) — не
    буквальний номер рядка файлу (порожні рядки випадають до нормалізації),
    але помилки самого JSON (`read_jsonl`) уже несуть точний номер рядка."""
    raw_items = read_jsonl(str(path))
    return [_normalize(obj, source="manual", default_status="labeled", line_no=i)
            for i, obj in enumerate(raw_items, start=1)]


def _read_legacy(path: Path, conn: Optional[sqlite3.Connection]) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw_items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw_items, list):
        raise GoldenSetError(
            f"golden-set {path}: очікується список або {{'items': [...]}}")

    out = []
    for item in raw_items:
        item = dict(item)
        if not item.get("slice"):
            expected_ids = item.get("expected_transcription_ids") or []
            source_type = _lookup_source_type(conn, expected_ids)
            item["slice"] = _slice_from_source_type(source_type)
        item.setdefault("status", "labeled")
        out.append(_normalize(item, source="legacy", default_status="labeled"))
    return out


def read_golden(path: str, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Прочитати golden-set — JSONL (C1, `.jsonl`) або legacy JSON (усе інше).

    `conn` — опційне read-only зʼєднання зі знімком; використовується лише
    для інференсу `slice` у legacy-записах без явного поля (за `source_type`
    першого очікуваного транскрипту) — без нього такі записи падають у
    `"calls"`. JSONL-записи без `slice` теж падають у `"calls"` (C1 не
    інферить його з даних для нового формату — має проставляти будівник).
    """
    p = Path(path)
    if p.suffix == ".jsonl":
        return _read_jsonl(p)
    return _read_legacy(p, conn)
