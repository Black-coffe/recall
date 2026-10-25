"""Own title and description of a record (`transcriptions.title` / `.description`).

Contract C2 of the spec `editable-title-description`. Here: value normalisation,
a human-readable record name (`display_name`) and a point update of the fields; no
HTTP and no heavy dependency.

The module is imported both from blueprints and from the stdio MCP, so it has **only
stdlib + `app.db.connection`**: `import torch` (or anything that pulls torch)
hangs the stdio server forever (memory note `mcp-stdio-no-heavy-models`).

`source_name` is provenance (file name, YouTube title, preview of a
TG message): it is NOT overwritten here or in PATCH. An empty title
means `NULL` in `title`, not a copy of `source_name`.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

TITLE_MAX = 200
DESCRIPTION_MAX = 4000


class _Unset:
    """Sentinel "the field was not passed", to tell it from an explicit `None`."""

    def __repr__(self) -> str:  # pragma: no cover - only for logs/errors
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


def _normalize(value: Any, limit: int, kind: str) -> str | None:
    if value is None or isinstance(value, _Unset):
        return None
    if not isinstance(value, str):
        raise ValueError(_tr(f"svc_embed.meta.{kind}.not_text"))
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise ValueError(_tr(f"svc_embed.meta.{kind}.too_long", limit=limit))
    return cleaned


def _tr(key: str, **vars) -> str:
    # Lazy import: keeps the module free of Flask at import time (stdio MCP).
    from app.core.i18n import tr
    return tr(key, **vars)


def normalize_title(value: Any) -> str | None:
    """`"  Зустріч  "` -> `"Зустріч"`; empty/`None` -> `None`; >200 -> `ValueError`.

    A non-string (`123`, `[]`, `{}`) is also a `ValueError` (-> 400 in PATCH): silently
    turning it into `NULL` would erase the title on a malformed request."""
    return _normalize(value, TITLE_MAX, "title")


def normalize_description(value: Any) -> str | None:
    """The same for the description (ceiling 4000). Line breaks inside are kept."""
    return _normalize(value, DESCRIPTION_MAX, "description")


def display_name(row: Mapping[str, Any]) -> str:
    """How to name a record for a person: `title` -> `source_name` -> `Record #id`."""
    for key in ("title", "source_name"):
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        row_id = row["id"]
    except (KeyError, IndexError, TypeError):
        row_id = None
    return _tr("svc_embed.meta.record_fallback", id=row_id)


def update_meta(conn, transcription_id: int, *, title=UNSET, description=UNSET,
                meta_source=UNSET):
    """Update only the passed fields of a record. `None` if the record is missing or deleted.

    ``meta_source`` (`'mcp'` for an agent, `None` for the owner) is written together with a
    real change of the title/description, never on its own.

    Returns ``{"id", "title", "description", "source_name", "display_name",
    "changed"}``. ``changed`` is False if the new values match the old ones
    (then no UPDATE is executed at all, and `transcriptions_au` does not poke FTS).
    Values are normalised right here, so a `ValueError` from `normalize_*` reaches
    the caller.
    """
    row = conn.execute(
        "SELECT id, title, description, source_name FROM transcriptions "
        "WHERE id = ? AND deleted_at IS NULL",
        (transcription_id,),
    ).fetchone()
    if row is None:
        return None

    current = {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "source_name": row["source_name"],
    }

    updates: dict[str, Any] = {}
    if not isinstance(title, _Unset):
        updates["title"] = normalize_title(title)
    if not isinstance(description, _Unset):
        updates["description"] = normalize_description(description)

    changed = any(current[field] != value for field, value in updates.items())
    if changed:
        writes = dict(updates)
        if not isinstance(meta_source, _Unset):
            writes["meta_source"] = meta_source
        assignments = ", ".join(f"{field} = ?" for field in writes)
        conn.execute(
            f"UPDATE transcriptions SET {assignments} WHERE id = ?",
            [*writes.values(), transcription_id],
        )
        conn.commit()
        current.update(updates)

    result = dict(current)
    result["display_name"] = display_name(current)
    result["changed"] = changed
    return result


def after_meta_update(transcription_id: int, *, changed: bool, db_path: str) -> None:
    """Hook "the record meta changed": the title and description go into the chunk
    context prefix, so after a real edit the chunks of this record must be re-embedded.

    `changed=False` (PATCH with the same values) does nothing; otherwise
    every repeated form save would run the GPU for nothing.

    `db_path` is a mandatory keyword: the re-embed must target exactly the DB in which
    the edit was made (in PATCH, `current_app.config["DATABASE"]`). There is deliberately
    no "live DB" default here: with it any call on a temporary DB
    would rewrite the chunks of the live archive.

    `reembed` is imported INSIDE the function: it pulls `embeddings`, and that pulls
    torch, and a top-level import would hang the stdio MCP
    (memory note `mcp-stdio-no-heavy-models`).
    """
    if not changed:
        logger.debug("after_meta_update: id=%s unchanged; re-embed not needed",
                     transcription_id)
        return
    try:
        from app.services import reembed
        outcome = reembed.schedule_record_reembed(transcription_id, db_path=db_path)
    except Exception:  # noqa: BLE001 - the meta edit is already saved, re-embed is secondary
        logger.exception("after_meta_update: id=%s - failed to schedule re-embed",
                         transcription_id)
        return
    logger.info("after_meta_update: id=%s re-embed → %s", transcription_id, outcome)
