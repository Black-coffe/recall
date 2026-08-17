"""Імпорт корпусу meeting_archive у Whisper (Phase 13D).

Бере markdown-файли мітингів зі старого проєкту meeting_archive (структура
`MM_YYYY/YYYY-MM-DD_*.md` з YAML-frontmatter від /indexer) і заливає їх як
транскрипти Whisper. Після імпорту вони стають у чергу на авто-enrich (Claude
card) + embed (вектори) — як і нативні транскрипції.

Тіло markdown (вже структуроване Claude-веб при їх workflow) кладеться у
transcript_text. meeting_date парситься з імені файлу. Frontmatter (summary/tags)
прибирається — наш пайплайн генерує власні структуровані дані.

Ідемпотентність: source_url = абсолютний шлях .md; повторний імпорт пропускає
вже залиті файли.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from app.db.connection import get_db_connection


logger = logging.getLogger(__name__)

_DIR_RE = re.compile(r"^\d{2}_\d{4}$")          # 01_2026, 12_2025
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")    # у назві файлу
SOURCE_TYPE = "meeting_archive"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Відділити YAML-frontmatter (--- ... ---) від тіла. Повертає (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:]).lstrip("\n")
            return _parse_frontmatter(fm), body
    return {}, text


def _parse_frontmatter(fm: str) -> dict:
    """Легкий парс summary + tags (без повного YAML)."""
    meta: dict = {}
    for line in fm.split("\n"):
        if line.startswith("summary:"):
            meta["summary"] = line[len("summary:"):].strip()
        elif line.startswith("tags:"):
            raw = line[len("tags:"):].strip().strip("[]")
            meta["tags"] = [t.strip() for t in raw.split(",") if t.strip()]
    return meta


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def parse_markdown_file(path: str) -> dict:
    """Розпарсити один .md → {title, meeting_date, body, meta}."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    meta, body = _split_frontmatter(text)
    fname = os.path.basename(path)
    m = _DATE_RE.search(fname)
    meeting_date = m.group(1) if m else None
    title = _title_from_body(body, os.path.splitext(fname)[0])
    return {"title": title, "meeting_date": meeting_date, "body": body, "meta": meta,
            "filename": fname}


def find_meeting_files(root: str) -> list[str]:
    """Усі .md у директоріях формату MM_YYYY (як scope /indexer)."""
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    for entry in sorted(os.listdir(root)):
        sub = os.path.join(root, entry)
        if os.path.isdir(sub) and _DIR_RE.match(entry):
            for fn in sorted(os.listdir(sub)):
                if fn.lower().endswith(".md"):
                    found.append(os.path.join(sub, fn))
    return found


def import_directory(db_path: str, root: str) -> dict:
    """Залити всі мітинг-файли з root. Idempotent (skip за source_url=абс.шлях).

    Returns {"found", "imported", "skipped", "ids": [...]}.
    """
    files = find_meeting_files(root)
    imported_ids: list[int] = []
    skipped = 0

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        for path in files:
            abspath = os.path.abspath(path)
            # T4.6: soft-deleted не рахується дублем — дозволяємо повторний
            # import того самого файлу, якщо попередній запис видалено.
            exists = c.execute(
                "SELECT id FROM transcriptions WHERE source_type = ? AND source_url = ? "
                "AND deleted_at IS NULL",
                (SOURCE_TYPE, abspath),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            try:
                parsed = parse_markdown_file(path)
            except Exception as e:
                logger.warning("[import] не вдалось розпарсити %s: %s", path, e)
                skipped += 1
                continue
            if not parsed["body"].strip():
                skipped += 1
                continue

            created_at = (parsed["meeting_date"] + " 00:00:00") if parsed["meeting_date"] else None
            cur = c.execute(
                "INSERT INTO transcriptions (source_type, source_name, source_url, "
                "transcript_text, language, meeting_date, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                (SOURCE_TYPE, parsed["title"], abspath, parsed["body"], "uk",
                 parsed["meeting_date"], created_at),
            )
            imported_ids.append(cur.lastrowid)
        conn.commit()

    logger.info("[import] root=%s: found=%d imported=%d skipped=%d",
                root, len(files), len(imported_ids), skipped)
    return {"found": len(files), "imported": len(imported_ids),
            "skipped": skipped, "ids": imported_ids}
