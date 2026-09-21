"""Власна назва й опис запису (`transcriptions.title` / `.description`).

Контракт C2 спеки `editable-title-description`. Тут — нормалізація значень,
читабельне імʼя запису (`display_name`) і точкове оновлення полів; жодного
HTTP і жодної важкої залежності.

Модуль імпортується і з блупринтів, і зі stdio-MCP, тому в ньому **лише
stdlib + `app.db.connection`**: `import torch` (чи будь-що, що тягне торч)
підвішує stdio-сервер назавжди — памʼятка `mcp-stdio-no-heavy-models`.

`source_name` — провенанс (імʼя файлу, заголовок YouTube, превʼю
TG-повідомлення): він НЕ перезаписується ні тут, ні в PATCH. Порожня назва
означає `NULL` у `title`, а не копію `source_name`.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

TITLE_MAX = 200
DESCRIPTION_MAX = 4000


class _Unset:
    """Сентинел «поле не передали» — щоб відрізнити його від явного `None`."""

    def __repr__(self) -> str:  # pragma: no cover - лише для логів/помилок
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


def _normalize(value: Any, limit: int, label: str) -> str | None:
    if value is None or isinstance(value, _Unset):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} має бути текстом або порожнім значенням")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise ValueError(f"{label} задовга: максимум {limit} символів")
    return cleaned


def normalize_title(value: Any) -> str | None:
    """`"  Зустріч  "` → `"Зустріч"`; порожнє/`None` → `None`; >200 → `ValueError`.

    Не-рядок (`123`, `[]`, `{}`) — теж `ValueError` (→ 400 у PATCH): мовчки
    перетворити його на `NULL` означало б стерти назву на кривому запиті."""
    return _normalize(value, TITLE_MAX, "Назва")


def normalize_description(value: Any) -> str | None:
    """Те саме для опису (стеля 4000). Переноси рядків усередині зберігаються."""
    return _normalize(value, DESCRIPTION_MAX, "Опис")


def display_name(row: Mapping[str, Any]) -> str:
    """Як запис називати людині: `title` → `source_name` → `Запис #id`."""
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
    return f"Запис #{row_id}"


def update_meta(conn, transcription_id: int, *, title=UNSET, description=UNSET):
    """Оновити лише передані поля запису. `None` — якщо запису нема або видалений.

    Повертає ``{"id", "title", "description", "source_name", "display_name",
    "changed"}``. ``changed`` — False, якщо нові значення збігаються зі старими
    (тоді UPDATE не виконується взагалі, і `transcriptions_au` не смикає FTS).
    Значення нормалізуються тут же, тож `ValueError` з `normalize_*` долітає
    до викликача.
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
        assignments = ", ".join(f"{field} = ?" for field in updates)
        conn.execute(
            f"UPDATE transcriptions SET {assignments} WHERE id = ?",
            [*updates.values(), transcription_id],
        )
        conn.commit()
        current.update(updates)

    result = dict(current)
    result["display_name"] = display_name(current)
    result["changed"] = changed
    return result


def after_meta_update(transcription_id: int, *, changed: bool, db_path: str) -> None:
    """Хук «мета запису змінилась»: назва й опис ідуть у контекстний префікс
    чанків, тож після реальної правки чанки цього запису треба переембедити.

    `changed=False` (PATCH тими самими значеннями) не робить нічого — інакше
    кожне повторне збереження форми ганяло б GPU намарно.

    `db_path` — обовʼязковий keyword: re-embed мусить цілити саме в ту БД, якою
    правку й зробили (у PATCH — `current_app.config["DATABASE"]`). Дефолту
    «бойова БД» тут нема свідомо — з ним будь-який виклик на тимчасовій БД
    переписував би чанки бойового архіву.

    `reembed` імпортується ВСЕРЕДИНІ функції: він тягне `embeddings`, а той —
    torch, і модуль верхнього рівня підвісив би stdio-MCP
    (памʼятка `mcp-stdio-no-heavy-models`).
    """
    if not changed:
        logger.debug("after_meta_update: id=%s без змін — re-embed не потрібен",
                     transcription_id)
        return
    try:
        from app.services import reembed
        outcome = reembed.schedule_record_reembed(transcription_id, db_path=db_path)
    except Exception:  # noqa: BLE001 — правка мети вже збережена, re-embed вторинний
        logger.exception("after_meta_update: id=%s — не вдалось запланувати re-embed",
                         transcription_id)
        return
    logger.info("after_meta_update: id=%s re-embed → %s", transcription_id, outcome)
