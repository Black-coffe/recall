"""Meeting Memory API (Phase 13) — наскрізний граф сутностей + RAG-архів.

Ендпоінти:
- POST /api/memory/backfill                     — масове збагачення історії (job + SSE)
- GET  /api/memory/backfill/status              — статус останнього backfill
- POST /api/memory/transcriptions/<id>/enrich   — збагатити один транскрипт
- GET  /api/memory/entities                     — список сутностей (фільтр type/q/sort)
- GET  /api/memory/entities/<id>                — деталі сутності: мітинги + задачі + aliases
- GET  /api/memory/action-items                 — задачі (фільтр status/owner)
- GET  /api/memory/stats                        — зведена статистика архіву

SSE-канал прогресу backfill: "backfill" (через /api/events/<channel> events_bp).
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date as _date, timedelta as _timedelta

from flask import Blueprint, Response, current_app, jsonify, request

from app import state
from app.core.i18n import localize_sse, request_lang, tr
from app.services import categories as cats
from app.services import enrichment


logger = logging.getLogger(__name__)
memory_bp = Blueprint('memory', __name__)

# Останній статус backfill (для GET status; SSE дає live-прогрес).
_backfill_state: dict = {"running": False, "processed": 0, "total": 0,
                         "done": 0, "skipped": 0, "failed": 0}
_backfill_lock = threading.Lock()


def _get_db():
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


# Нормалізація, розбір фільтра і письменники `category_id` живуть в
# `app/services/categories.py` (спека directions-projects-tags, контракт C2) —
# один модуль на всі 15 місць фільтра у двох процесах. Тут лишаються тонкі
# обгортки: імена вже імпортує сторонній код (`test_categories.py`), а міняти
# їх у цій історії нема за що.
_norm_cat = cats.norm_name
#: (conn, cid) → None. Тепер зануляє і `audio_downloads.category_id` — до цієї
#: спеки id видаленого напрямку лишався висіти на рядках Медіатеки.
_null_category_refs = cats.null_category_refs
#: (conn, src, dst) → None. Раніше повертала к-сть перенесених; тепер id
#: перенесених віддає `cats.merge_categories` — саме вони йдуть у re-embed.
_reassign_category_refs = cats.reassign_category_refs


def _parse_axes(source) -> tuple:
    """C4: (category_id, tags, since, until) із query-параметрів/JSON-тіла.

    `source` — будь-що з `.get(key)` (`request.args` або розпарсений JSON).
    Кидає `ValueError` на кривому значенні — виклики огортають у 400 (той самий
    підхід, що `app/blueprints/research.py._parse_axes`). Раніше `category_id`
    тут мовчки падав назад на «усі напрямки» — тепер, як і решта осей, він
    протікає до читача помилкою (історія 07 закриває цей борг для search/ask/
    entities/action-items)."""
    return (
        cats.parse_filter(source.get('category_id')),
        cats.parse_tags(source.get('tags')),
        cats.parse_date(source.get('since')),
        cats.parse_date(source.get('until')),
    )


def _check_project(raw) -> "str | None":
    """Текст `project` → він сам; неоднозначна назва → `CategoryError`.

    Розвʼязуємо ДО виклику `rag`: у стріма (`ask/stream`) відповідь уже пішла
    зі статусом 200, і сказати «409, уточніть шлях» звідти нема як. Значення
    не підмінюємо — `project` це ще й сутність графа й текстова згадка
    («Ковальчука», бренд), і заміна тексту на шлях вузла зламала б саме ці два
    шари. Тут лише перевірка: назва, що сидить на двох вузлах, зупиняє запит
    переліком шляхів, а не мовчазним вибором одного з них.

    Кілька назв через кому/крапку з комою — той самий розбір, що в
    `scope.scope_filter_ids`: перевіряємо кожну. Нерядковий вхід (число,
    об'єкт) чи список із нерядковими елементами — `CategoryError("bad_name")`
    (мінор 11 р0): раніше `str(5)` мовчки давав `"5"`, і крива форма
    протікала до `scope_filter_ids`, де `list(5)` — 500.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        parts = re.split(r"[,;]", text)
    elif isinstance(raw, (list, tuple)) and all(isinstance(x, str) for x in raw):
        text = list(raw)
        parts = text
    else:
        raise cats.CategoryError("bad_name", tr('knowledge.memory.err.project_bad'))
    with _get_db() as conn:
        for part in parts:
            if str(part).strip():
                cats.resolve_node(conn, str(part).strip())
    return text


def _schedule_reembed(ids, reason: str) -> dict:
    """Поставити змінені записи в точковий re-embed (напрямок і теги їдуть у
    контекстний префікс чанка). Помилка планувальника не валить запит —
    правка вже збережена, re-embed вторинний.

    `reembed` імпортується ВСЕРЕДИНІ функції: він тягне `embeddings`, а той —
    torch (памʼятка `mcp-stdio-no-heavy-models`)."""
    if not ids:
        return {"mode": "none", "count": 0, "stale": 0}
    try:
        from app.services import reembed
        return reembed.schedule_records_reembed(
            ids, db_path=current_app.config["DATABASE"], reason=reason)
    except Exception:  # noqa: BLE001 — зміна вже в БД, re-embed вторинний
        logger.exception("[memory] %s: не вдалось запланувати re-embed для %s записів",
                         reason, len(ids))
        return {"mode": "none", "count": 0, "stale": 0}


def _parse_explain(raw) -> bool:
    """Історія 05 (grep-explainability): query-параметр (`?explain=1`) чи поле
    JSON-body (`{"explain": true}`) → bool. Невідоме значення ('', '0',
    'false', None) читаємо як False, а не як помилку — дефолт скрізь
    компактний `why`, не його відсутність."""
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes")


def _parse_channel(raw) -> tuple:
    """Канал питання для `ask_log` → (channel, error_response|None).

    Порожньо → `ui` (веб-чат нічого не передає). Невідоме значення — 400, а
    НЕ тихий дефолт: канал у логу є вхідним фільтром golden-set, і мовчазне
    «щось незнайоме = ui» зіпсувало б вибірку, яку потім розмічає людина."""
    from app.services import rag
    if raw is None or raw == "":
        return "ui", None
    channel = str(raw).strip().lower()
    if channel not in rag.ASK_CHANNELS:
        return None, (jsonify({"success": False,
                               "error": tr('knowledge.memory.err.channel', channels='/'.join(rag.ASK_CHANNELS))}), 400)
    return channel, None


# ============================================================
# Напрямки / категорії (Phase 14)
# ============================================================

#: Код помилки дерева → HTTP-статус (контракт C4).
_CAT_STATUS = {"not_found": 404, "duplicate": 409, "has_projects": 409,
               "ambiguous": 409, "two_levels": 400, "self_parent": 400,
               "empty": 400, "bad_parent": 400, "bad_name": 400,
               "not_suggested": 400}


def _cat_error(exc: cats.CategoryError):
    """Відповідь на очікувану помилку дерева.

    `error` лишається людським текстом (його показує тост SPA) і дублюється
    машинним `code`. Виняток — `has_projects`: там контракт C4 вимагає саме
    код і перелік проєктів, бо фронт має що показати («спершу перенесіть»).

    `duplicate` (v47): ключі, що були, лишаються на місці, а поруч зʼявляються
    `message` (текст сервісу — він називає напрямок, у якому назва зайнята) і
    `conflict` `{id, path}` — адреса вузла-суперника. Без них 409 на
    «Маркетинг» не відрізнити від 409 на «Маркетинг» у сусідньому напрямку.
    `ambiguous` (v47, 409) влаштований так само, як `has_projects`: `error` —
    машинний код, `message` — текст сервісу, `candidates` — повні шляхи вузлів
    із цією назвою. Порожня відповідь чи «перший збіг» тут заборонені: клієнт
    має отримати ПЕРЕЛІК і передати назад один шлях.

    Фронт не чіпаємо: `settings.js` читає `.message` з історії 09."""
    body = {"success": False, "error": str(exc), "code": exc.code}
    if exc.projects:
        body["projects"] = [{"id": p["id"], "name": p["name"]} for p in exc.projects]
    if exc.code == "has_projects":
        body["error"] = "has_projects"
        body["message"] = str(exc)
    if exc.code == "duplicate":
        body["message"] = str(exc)
        body["conflict"] = exc.conflict
    if exc.code == "ambiguous":
        body["error"] = "ambiguous"
        body["message"] = str(exc)
        body["candidates"] = exc.candidates
    return jsonify(body), _CAT_STATUS.get(exc.code, 400)


@memory_bp.route('/api/memory/categories', methods=['GET'])
def list_categories():
    """Дерево напрямків: напрямок, одразу за ним — його проєкти.

    Конверт той самий, що був; кожен елемент додав `parent_id`, `level`,
    `items`/`items_total`. `count` лишається синонімом `items` — на ньому
    тримаються чинні екрани (Налаштування, Дослідження), а переїзд SPA на
    дерево — історії 09/10."""
    with _get_db() as conn:
        tree = cats.list_tree(conn)
        uncat = conn.execute(
            "SELECT COUNT(*) AS n FROM transcriptions "
            "WHERE category_id IS NULL AND deleted_at IS NULL"
        ).fetchone()["n"]
    for node in tree:
        node["count"] = node["items"]
    return jsonify({"success": True, "categories": tree, "uncategorized": uncat})


def _parse_int_or_none(raw) -> "tuple[int | None, bool]":
    """`raw` → `(значення|None, ok)` — розбір через `int()`, а не через
    перевірку «усі символи цифри» (Мінор 5, ревʼю раунду 3): така перевірка
    каже правду про «²»/«½» (унікодні цифрові символи, які `int()` не зʼїсть)
    — приймала їх і падала 500 нижче за стеком замість 400 тут.

    `None`/порожній рядок → `(None, True)` — значення відсутнє, не помилка.
    Невідʼємне ціле, яке приймає `int()` → `(id, True)`. `bool` НЕ вважається
    цілим (перевірка ДО `int()`, бо `isinstance(True, int)` — так). Решта
    (`"abc"`, `"²"`, `-1`, порожній `[]`) → `(None, False)`."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None, True
    if isinstance(raw, bool):
        return None, False
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, False
    return (val, True) if val >= 0 else (None, False)


def _parse_signed_int_or_none(raw) -> "tuple[int | None, bool]":
    """Як `_parse_int_or_none`, але без обмеження на невідʼємність — для
    `sort_order` (ручне впорядкування вузлів, не id). Звуження до
    невідʼємних (26) зачепило й це поле, хоча JS-відправника з відʼємним
    значенням немає — воно не було на записі; тут відкочено до поведінки
    23 (мінор р0 п.5 / р1 п.4, 28): `-5` знову приймається, `"abc"`/`1.5`/
    `bool` — так само 400, як і в `_parse_int_or_none`."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None, True
    if isinstance(raw, bool):
        return None, False
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, False
    return val, True


def _parse_parent_id(raw) -> "tuple[int | None, tuple | None]":
    """`parent_id` тіла запиту → `(id|None, error_response|None)`.

    Той самий розбір і той самий текст помилки для POST і PATCH (Мажор 7):
    `None`/порожній рядок → без батька (вузол стає напрямком); будь-що інше,
    що не невідʼємне ціле (у т.ч. від'ємне, `bool`, унікодні псевдоцифри) —
    400 замість тихого падіння в «без батька» (або 500 — Мінор 5)."""
    val, ok = _parse_int_or_none(raw)
    if not ok:
        return None, (jsonify({"success": False, "error": tr('knowledge.memory.err.parent_id_bad')}), 400)
    return val, None


@memory_bp.route('/api/memory/categories', methods=['POST'])
def create_category():
    """Створити напрямок або проєкт. Body: {name, color?, icon?, parent_id?}.

    `parent_id` — id напрямку, всередині якого створюємо проєкт. Батьком може
    бути лише напрямок (дерево рівно двох рівнів) — інакше 400."""
    data = request.get_json(silent=True) or {}
    name = " ".join((data.get("name") or "").strip().split())
    if not name:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.name_empty')}), 400
    parent_id, err = _parse_parent_id(data.get("parent_id"))
    if err:
        return err
    color = data.get("color", "#6c757d")
    icon = data.get("icon", "fa-folder")
    try:
        with _get_db() as conn:
            node = cats.create_category(conn, name, color=color, icon=icon,
                                        parent_id=parent_id)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "id": node["id"], "name": node["name"],
                    "color": node["color"], "icon": node["icon"],
                    "parent_id": node["parent_id"], "level": node["level"],
                    "category": node})


@memory_bp.route('/api/memory/categories/<int:cid>', methods=['PATCH'])
def update_category(cid: int):
    """Перейменувати / перефарбувати / перепідпорядкувати вузол.
    Body: {name?, color?, icon?, sort_order?, parent_id?: int|null}.

    `parent_id: null` робить вузол напрямком, число — проєктом усередині
    напрямку. Назва і шлях їдуть у контекстний префікс чанків усього
    піддерева, тож після реальної зміни записи стають у точковий re-embed."""
    data = request.get_json(silent=True) or {}
    kwargs = {}
    if "name" in data and data["name"] is not None:
        if not " ".join(str(data["name"]).strip().split()):
            return jsonify({"success": False, "error": tr('knowledge.memory.err.name_empty')}), 400
        kwargs["name"] = data["name"]
    for key in ("color", "icon"):
        if key in data and data[key] is not None:
            kwargs[key] = data[key]
    if "sort_order" in data:
        sort_order, ok = _parse_signed_int_or_none(data["sort_order"])
        if not ok:
            return jsonify({"success": False, "error": tr('knowledge.memory.err.sort_order_bad')}), 400
        if sort_order is not None:
            kwargs["sort_order"] = sort_order
    if "parent_id" in data:
        parent_id, err = _parse_parent_id(data["parent_id"])
        if err:
            return err
        kwargs["parent_id"] = parent_id
    if not kwargs:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.nothing_to_update')}), 400

    try:
        with _get_db() as conn:
            node, ids = cats.update_category(conn, cid, **kwargs)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "category": node,
                    "reembed": _schedule_reembed(ids, "category_tree_changed")})


@memory_bp.route('/api/memory/categories/<int:cid>', methods=['DELETE'])
def delete_category(cid: int):
    """Видалити вузол. Записи (транскрипти/аудіо/копілот/tg) стають «без
    напрямку» — самі записи НЕ видаляються.

    Напрямок із проєктами не видаляється (409 `has_projects`): тихо підняти
    проєкти в корінь означало б розсипати розкладку архіву без сліду."""
    try:
        with _get_db() as conn:
            moved = [r["id"] for r in conn.execute(
                "SELECT id FROM transcriptions WHERE category_id = ?", (cid,)).fetchall()]
            cats.delete_category(conn, cid)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True,
                    "reembed": _schedule_reembed(moved, "category_deleted")})


@memory_bp.route('/api/memory/categories/<int:cid>/merge', methods=['POST'])
def merge_category(cid: int):
    """Перенести/обʼєднати напрямок. Body: {target_id: int|null, delete_source: bool}.
    Усі записи напрямку cid → target_id (null = «без напрямку»). Якщо
    delete_source=true — після перенесення напрямок cid видаляється.

    Вузол із проєктами не переноситься і не зливається (409 `has_projects`) —
    спершу розберіться з проєктами."""
    data = request.get_json(silent=True) or {}
    dst, ok = _parse_int_or_none(data.get("target_id"))
    if not ok:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.target_id_bad')}), 400
    delete_source = bool(data.get("delete_source"))
    if dst == cid:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.merge_self')}), 400
    try:
        with _get_db() as conn:
            moved = cats.merge_categories(conn, dst, cid)
            if delete_source:
                cats.delete_category(conn, cid)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "moved": len(moved), "deleted": delete_source,
                    "reembed": _schedule_reembed(moved, "category_merged")})


@memory_bp.route('/api/memory/transcriptions/<int:tid>/category', methods=['PATCH'])
def set_transcription_category(tid: int):
    """Призначити напрямок транскрипту. Body: {category_id: int|null}."""
    data = request.get_json(silent=True) or {}
    cid, ok = _parse_int_or_none(data.get("category_id"))
    if not ok:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.category_id_bad')}), 400
    with _get_db() as conn:
        if cid is not None and not conn.execute(
                "SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone():
            return jsonify({"success": False, "error": tr('knowledge.memory.err.category_not_found')}), 404
        if not conn.execute("SELECT 1 FROM transcriptions WHERE id = ?", (tid,)).fetchone():
            return jsonify({"success": False, "error": tr('knowledge.memory.err.transcript_not_found')}), 404
        changed = cats.set_category(conn, [tid], cid)
    return jsonify({"success": True, "category_id": cid,
                    "reembed": _schedule_reembed(changed, "category_changed")})


@memory_bp.route('/api/memory/transcriptions/<int:tid>/suggest-category', methods=['GET'])
def suggest_transcription_category(tid: int):
    """Авто-підказка напрямку через k-NN по розмічених транскриптах (Phase 15C).
    Повертає suggestion=None з reason, якщо підказати не можна (cold-start тощо)."""
    from app.services import categorize
    try:
        res = categorize.suggest_category(current_app.config['DATABASE'], tid)
    except Exception as e:
        logger.error("[memory] suggest-category tx=%s failed: %s", tid, e, exc_info=True)
        return jsonify({"success": False, "error": tr('knowledge.memory.err.suggest_failed')}), 500
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/transcriptions/bulk-category', methods=['POST'])
def bulk_set_category():
    """Масово призначити напрямок. Body: {ids: [..], category_id} АБО
    {source_type: 'youtube'|'file'|..., category_id} (усі без напрямку цього типу)."""
    data = request.get_json(silent=True) or {}
    cid, ok = _parse_int_or_none(data.get("category_id"))
    if not ok:
        # 400 до будь-якого запису (Мінор 6): раніше нецілий/невідомий
        # `category_id` тихо перетворювався на `None` і знімав напрямок УСІМ
        # цілям, а не відмовляв запитом.
        return jsonify({"success": False, "error": tr('knowledge.memory.err.category_id_bad')}), 400
    ids = data.get("ids")
    src = data.get("source_type")
    if ids is not None:
        # `{"ids": "123"}` — рядок теж ітерується (по символах!), тож без
        # перевірки типу він тихо розсипався б на `[1, 2, 3]`.
        if not isinstance(ids, (list, tuple)):
            return jsonify({"success": False, "error": tr('knowledge.memory.err.ids_bad')}), 400
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": tr('knowledge.memory.err.ids_bad')}), 400
    with _get_db() as conn:
        if ids:
            targets = ids
        elif src:
            targets = [r["id"] for r in conn.execute(
                "SELECT id FROM transcriptions WHERE source_type = ? AND category_id IS NULL",
                (src,)).fetchall()]
        else:
            return jsonify({"success": False, "error": tr('knowledge.memory.err.ids_or_source')}), 400
        # `updated` тепер рахує записи, у яких напрямок СПРАВДІ змінився (а не
        # всі, що потрапили під UPDATE): саме цей список іде в re-embed, і
        # розбіжність між «оновлено» і «переембеджено» тільки б плутала.
        changed = cats.set_category(conn, targets, cid)
    return jsonify({"success": True, "updated": len(changed),
                    "reembed": _schedule_reembed(changed, "category_bulk")})


# ============================================================
# Теги (07, контракт C4 «Теги»)
# ============================================================

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _parse_color(raw) -> "tuple[str | None, bool]":
    """`raw` → `(колір|None, ok)`. `None`/відсутній → `(None, True)` — колір
    не заданий, не помилка. Літерал `#RGB`/`#RGBA`/`#RRGGBB`/`#RRGGBBAA`
    (регістр байдужий) → `(raw, True)`. Решта (довільний CSS-текст, який без
    перевірки потрапляв у `style="--tag-color:…"` — Мінор 12 р1) → `(None,
    False)`."""
    if raw is None:
        return None, True
    if isinstance(raw, str) and _COLOR_RE.match(raw.strip()):
        return raw.strip(), True
    return None, False


def _tag_name_from_body(data, *, required: bool) -> "tuple[str | None, tuple | None]":
    """Дістати й перевірити `name` з тіла запиту. `required=False` дозволяє
    відсутність ключа (PATCH без зміни назви); `''`/пробіли й довжина > 40
    (`cats.TAG_NAME_MAX`) — завжди 400, незалежно від `required`."""
    if "name" not in data or data["name"] is None:
        if required:
            return None, (jsonify({"success": False, "error": tr('knowledge.memory.err.tag_name_empty')}), 400)
        return None, None
    name = cats.clean_name(data["name"])
    if not name:
        return None, (jsonify({"success": False, "error": tr('knowledge.memory.err.tag_name_empty')}), 400)
    if len(name) > cats.TAG_NAME_MAX:
        return None, (jsonify({
            "success": False,
            "error": tr('knowledge.memory.err.tag_name_long', max=cats.TAG_NAME_MAX)}), 400)
    return name, None


@memory_bp.route('/api/memory/tags', methods=['GET'])
def list_tags():
    """Словник тегів: {id, name, color, items}, за `name_norm`."""
    with _get_db() as conn:
        return jsonify({"success": True, "tags": cats.list_tags(conn)})


@memory_bp.route('/api/memory/tags', methods=['POST'])
def create_tag():
    """Знайти/створити тег словника. Body: {name, color?}.

    Ідемпотентно за `name_norm` (ADR §B): повторний POST тієї самої назви
    (з іншим регістром/пробілами) повертає наявний тег 200, а не дублює його —
    201 лише на реально новий рядок."""
    data = request.get_json(silent=True) or {}
    name, err = _tag_name_from_body(data, required=True)
    if err:
        return err
    color, ok = _parse_color(data.get("color"))
    if not ok:
        return jsonify({"success": False,
                        "error": tr('knowledge.memory.err.color_bad')}), 400
    with _get_db() as conn:
        existed = conn.execute("SELECT 1 FROM tags WHERE name_norm = ?",
                               (cats.norm_name(name),)).fetchone()
        tag = cats.ensure_tag(conn, name, color=color)
    return jsonify({"success": True, "tag": tag}), (200 if existed else 201)


@memory_bp.route('/api/memory/tags/<int:tag_id>', methods=['PATCH'])
def update_tag_route(tag_id: int):
    """Перейменувати / перефарбувати тег. Body: {name?, color?}."""
    data = request.get_json(silent=True) or {}
    name, err = _tag_name_from_body(data, required=False)
    if err:
        return err
    kwargs = {}
    if name is not None:
        kwargs["name"] = name
    if "color" in data:
        color, ok = _parse_color(data["color"])
        if not ok:
            return jsonify({"success": False,
                            "error": tr('knowledge.memory.err.color_bad')}), 400
        kwargs["color"] = color
    if not kwargs:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.nothing_to_update')}), 400
    try:
        with _get_db() as conn:
            tag, ids = cats.update_tag(conn, tag_id, **kwargs)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "tag": tag,
                    "reembed": _schedule_reembed(ids, "tag_renamed")})


@memory_bp.route('/api/memory/tags/<int:tag_id>', methods=['DELETE'])
def delete_tag_route(tag_id: int):
    """Видалити тег зі словника — каскадом знімається з `record_tags` і
    `tg_chat_tags` (схема без FK, каскад робить `cats.delete_tag`)."""
    try:
        with _get_db() as conn:
            ids = cats.delete_tag(conn, tag_id)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "reembed": _schedule_reembed(ids, "tag_deleted")})


@memory_bp.route('/api/memory/tags/merge', methods=['POST'])
def merge_tags_route():
    """Злити два теги. Body: {keep_id, merge_id}. Записи merge_id переїжджають
    під keep_id, сам merge_id видаляється зі словника."""
    data = request.get_json(silent=True) or {}
    keep_id, keep_ok = _parse_int_or_none(data.get("keep_id"))
    merge_id, merge_ok = _parse_int_or_none(data.get("merge_id"))
    if not keep_ok or not merge_ok or keep_id is None or merge_id is None:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.tag_merge_ids')}), 400
    try:
        with _get_db() as conn:
            ids = cats.merge_tags(conn, keep_id, merge_id)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({"success": True, "moved": len(ids),
                    "reembed": _schedule_reembed(ids, "tags_merged")})


@memory_bp.route('/api/memory/<int:tid>/tags', methods=['PUT'])
def set_record_tags(tid: int):
    """Додати/зняти теги запису. Body: {"add": [names], "remove": [names]} —
    бодай один ключ обов'язковий. Теги з `add`, яких ще нема у словнику,
    створюються з місця вживання (той самий патерн, що напрямок)."""
    data = request.get_json(silent=True) or {}
    if "add" not in data and "remove" not in data:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.tag_add_remove')}), 400
    try:
        with _get_db() as conn:
            if not conn.execute("SELECT 1 FROM transcriptions WHERE id = ?", (tid,)).fetchone():
                return jsonify({"success": False, "error": tr('knowledge.memory.err.transcript_not_found')}), 404
            changed = cats.set_tags(conn, tid, add=data.get("add") or (),
                                    remove=data.get("remove") or ())
            tags = cats.tags_for(conn, [tid]).get(tid, [])
            suggested = cats.suggested_tags(conn, tid)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({
        "success": True, "tags": tags, "suggested_tags": suggested,
        "reembed": _schedule_reembed([tid] if changed else [], "record_tags_changed"),
    })


@memory_bp.route('/api/memory/<int:tid>/tags/suggestions', methods=['POST'])
def resolve_tag_suggestions(tid: int):
    """Прийняти/відхилити підказки Claude. Body: {"accept": [names], "dismiss":
    [names]} — бодай один непорожній список обов'язковий. Прийняте стає
    звичайним тегом (`source='claude'`), обидва списки прибираються з
    `suggested_tags_json`."""
    data = request.get_json(silent=True) or {}
    accept = data.get("accept") or []
    dismiss = data.get("dismiss") or []
    if not accept and not dismiss:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.tag_accept_dismiss')}), 400
    try:
        with _get_db() as conn:
            if not conn.execute("SELECT 1 FROM transcriptions WHERE id = ?", (tid,)).fetchone():
                return jsonify({"success": False, "error": tr('knowledge.memory.err.transcript_not_found')}), 404
            changed = cats.resolve_suggestions(conn, tid, accept=accept, dismiss=dismiss)
            tags = cats.tags_for(conn, [tid]).get(tid, [])
            suggested = cats.suggested_tags(conn, tid)
    except cats.CategoryError as exc:
        return _cat_error(exc)
    return jsonify({
        "success": True, "tags": tags, "suggested_tags": suggested,
        "reembed": _schedule_reembed([tid] if changed else [], "tag_suggestions_resolved"),
    })


# ============================================================
# Збагачення
# ============================================================

@memory_bp.route('/api/memory/transcriptions/<int:tid>/enrich', methods=['POST'])
def enrich_one(tid: int):
    """Збагатити один транскрипт (синхронно). Body: {"force": bool, "model": str}."""
    # Як і в backfill нижче: embed-фаза не потребує Claude — див. коментар там.
    if not enrichment.any_available():
        return jsonify({"success": False,
                        "error": tr('knowledge.memory.err.no_key_no_embeddings')}), 400
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    model = data.get("model")
    try:
        res = enrichment.enrich_transcription(
            current_app.config['DATABASE'], tid, model=model, force=force,
        )
    except Exception as e:
        logger.error("[memory] enrich tx=%s failed: %s", tid, e, exc_info=True)
        return jsonify({"success": False, "error": tr('knowledge.memory.err.enrich_failed')}), 500
    if res.get("status") == "not_found":
        return jsonify({"success": False, "error": tr('knowledge.memory.err.transcript_not_found')}), 404
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/backfill', methods=['POST'])
def start_backfill():
    """Запустити масове збагачення історії у фоні. Body: {force, limit, model}."""
    # T6.5: гейт по any_available(), а не is_available(). Embed-фаза локальна і
    # безкоштовна, а після бампу EMBED_VERSION саме вона стає основною роботою
    # (весь архів на re-embed). Гейт по одному лише Claude-ключу означав би:
    # ключа нема → індексер вічно показує повний архів як роботу, і запустити
    # її з UI неможливо. Інжест уже давно ходить через any_available()
    # (transcription.py/documents.py/copilot.py) — тут був розсинхрон.
    if not enrichment.any_available():
        return jsonify({"success": False,
                        "error": tr('knowledge.memory.err.no_key_no_embeddings')}), 400
    with _backfill_lock:
        if _backfill_state.get("running"):
            return jsonify({"success": False, "error": tr('knowledge.memory.err.backfill_running')}), 409

    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    limit = data.get("limit")
    model = data.get("model")
    effort = data.get("effort", "medium")
    if effort not in ("low", "medium", "high"):
        effort = "medium"
    db_path = current_app.config['DATABASE']

    pending = enrichment.list_unenriched_ids(db_path) if not force else None

    def _job(job):
        with _backfill_lock:
            _backfill_state.update({"running": True, "processed": 0, "total": 0,
                                    "done": 0, "skipped": 0, "failed": 0})

        def _progress(p):
            with _backfill_lock:
                _backfill_state.update(p)
                _backfill_state["running"] = True
            try:
                state.sse_broker.publish("backfill", "progress", p)
            except Exception:
                pass

        try:
            result = enrichment.backfill(
                db_path, model=model, limit=limit, force=force, effort=effort,
                progress_cb=_progress, cancel_cb=job.is_cancelled,
            )
        finally:
            with _backfill_lock:
                _backfill_state["running"] = False
        try:
            state.sse_broker.publish("backfill", "complete", {"status": "completed", **result})
        except Exception:
            pass
        return result

    job = state.job_queue.submit("enrichment_backfill", _job, meta={"kind": "backfill"})
    return jsonify({
        "success": True, "job_id": job.id, "channel": "backfill",
        "pending": len(pending) if pending is not None else None,
    })


@memory_bp.route('/api/memory/backfill/status', methods=['GET'])
def backfill_status():
    with _backfill_lock:
        return jsonify({"success": True, **_backfill_state})


@memory_bp.route('/api/memory/import', methods=['POST'])
def import_archive():
    """Імпорт корпусу meeting_archive. Body: {"path": "<dir>"}.

    Заливає .md-мітинги як транскрипти (idempotent). Збагачення (enrich+embed)
    запускати окремо через POST /api/memory/backfill — це контрольований
    (платний для Claude) крок.
    """
    data = request.get_json(silent=True) or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.import_path')}), 400
    if not os.path.isdir(root):
        return jsonify({"success": False, "error": tr('knowledge.memory.err.dir_not_found', path=root)}), 400
    from app.services import archive_import
    res = archive_import.import_directory(current_app.config['DATABASE'], root)
    return jsonify({"success": True, **res})


# ============================================================
# Пошук (гібрид: вектори + FTS5)
# ============================================================

@memory_bp.route('/api/memory/search', methods=['GET'])
def search():
    """Гібридний семантичний пошук по архіву. Query: q, k (top-k), category_id,
    tags (через кому), since, until (контракт C4 — невалідна вісь → 400)."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.query_empty')}), 400
    try:
        k = min(max(int(request.args.get('k', 8)), 1), 30)
    except ValueError:
        k = 8
    try:
        category_id, tags, since, until = _parse_axes(request.args)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    from app.services import retrieval
    res = retrieval.search(current_app.config['DATABASE'], q, top_k=k,
                           category_id=category_id, tags=tags, since=since, until=until,
                           explain=_parse_explain(request.args.get('explain')))
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/ask', methods=['POST'])
def ask():
    """RAG «Запитай архів»: питання → відповідь з цитатами.

    Body: {question, k, model, category_id, tags, since, until, project, channel}.
    `project` (Трек 2) звужує пошук до записів, де згадано цей проєкт/людину —
    категорії недостатньо, бо «Фонд Робота» покриває більша частина корпусу. `tags`/
    `since`/`until` — контракт C4, невалідна вісь → 400. `channel` (`ui`|`mcp`)
    лише позначає походження питання в `ask_log`."""
    if not enrichment.is_available():
        return jsonify({"success": False, "error": tr('knowledge.memory.err.no_api_key')}), 400
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not q:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.question_empty')}), 400
    channel, err = _parse_channel(data.get("channel"))
    if err:
        return err
    try:
        category_id, tags, since, until = _parse_axes(data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    from app.services import rag
    # Дефолт k живе в rag._DEFAULT_TOP_K (одна точка правди, env RAG_TOP_K).
    try:
        k = min(max(int(data.get("k", rag._DEFAULT_TOP_K)), 1), 20)
    except (ValueError, TypeError):
        k = rag._DEFAULT_TOP_K
    try:
        project = _check_project(data.get("project"))
    except cats.CategoryError as exc:
        return _cat_error(exc)
    try:
        res = rag.answer_question(current_app.config['DATABASE'], q, top_k=k,
                                  model=data.get("model"),
                                  category_id=category_id,
                                  project=project,
                                  explain=_parse_explain(data.get("explain")),
                                  channel=channel,
                                  tags=tags, since=since, until=until)
    except Exception as e:
        logger.error("[memory] ask failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": tr('knowledge.memory.err.rag_failed')}), 500
    return jsonify({"success": True, **res})


# Тексти статусів/помилок, які `answer_question_stream` стрімить українською.
_ASK_SSE_KEYS = (
    'knowledge.memory.err.question_empty',
    'knowledge.memory.err.rag_failed',
    'knowledge.memory.ans.no_results',
)


@memory_bp.route('/api/memory/ask/stream', methods=['POST'])
def ask_stream():
    """RAG зі стрімом відповіді (SSE). Body: {question, k, model, category_id,
    tags, since, until, project}.

    Події: sources (одразу) → delta* (токени) → done | error.
    Усі значення з request читаємо ДО генератора (поза request-контекстом).
    """
    if not enrichment.is_available():
        return jsonify({"success": False, "error": tr('knowledge.memory.err.no_api_key')}), 400
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not q:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.question_empty')}), 400
    channel, err = _parse_channel(data.get("channel"))
    if err:
        return err
    try:
        category_id, tags, since, until = _parse_axes(data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    from app.services import rag
    try:
        k = min(max(int(data.get("k", rag._DEFAULT_TOP_K)), 1), 20)
    except (ValueError, TypeError):
        k = rag._DEFAULT_TOP_K
    model = data.get("model")
    # Трек 2: зріз за проєктом/людиною. Читаємо ТУТ, а не в генераторі — той
    # виконується поза request-контекстом (та сама причина, що й для решти полів).
    try:
        project = _check_project(data.get("project"))
    except cats.CategoryError as exc:
        return _cat_error(exc)
    explain = _parse_explain(data.get("explain"))
    db_path = current_app.config['DATABASE']

    lang = request_lang()

    def generate():
        try:
            yield from localize_sse(
                rag.answer_question_stream(db_path, q, top_k=k, model=model,
                                           category_id=category_id, project=project,
                                           explain=explain, channel=channel,
                                           tags=tags, since=since, until=until),
                lang, _ASK_SSE_KEYS)
        except Exception as e:
            logger.error("[memory] ask_stream generator failed: %s", e, exc_info=True)
            yield rag._sse("error", {"error": tr('knowledge.memory.err.rag_stream_failed', lang=lang)})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@memory_bp.route('/api/memory/ask/<int:ask_id>/rate', methods=['POST'])
def rate_ask(ask_id: int):
    """Оцінка відповіді власником: body {rating: 1|-1, note?} → `ask_log`.

    Це сировина golden-set (A17): раз на тиждень власник розмічає десяток
    питань із логу, 👎 йдуть у набір першими."""
    data = request.get_json(silent=True) or {}
    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        rating = 0
    if rating not in (1, -1):
        return jsonify({"success": False, "error": tr('knowledge.memory.err.rating_bad')}), 400
    from app.services import rag
    if not rag.rate_ask(current_app.config['DATABASE'], ask_id, rating,
                        data.get("note")):
        return jsonify({"success": False, "error": tr('knowledge.memory.err.question_not_found')}), 404
    return jsonify({"success": True, "ask_id": ask_id})


# ============================================================
# Сутності
# ============================================================

@memory_bp.route('/api/memory/entities', methods=['GET'])
def list_entities():
    """Список сутностей. Query: type, q (пошук по імені/alias), sort, limit,
    offset, category_id, tags, since, until (контракт C4 — невалідна вісь → 400)."""
    etype = request.args.get('type')
    q = (request.args.get('q') or '').strip()
    sort = request.args.get('sort', 'mentions')  # mentions | meetings | name | recent
    try:
        limit = min(int(request.args.get('limit', 100)), 500)
        offset = max(int(request.args.get('offset', 0)), 0)
    except ValueError:
        limit, offset = 100, 0

    # Поріг значущості: per-meeting LLM-екстракція плодить разові сутності
    # (≈65-83% згадані лише в 1 мітингу — шум). За замовчуванням ховаємо їх
    # (min_meetings=2). Явний пошук по імені (q) показує і рідкісні (=1).
    default_min = 1 if q else 2
    try:
        min_meetings = max(int(request.args.get('min_meetings', default_min)), 1)
    except (ValueError, TypeError):
        min_meetings = default_min

    try:
        category_id, tag_names, since_val, until_val = _parse_axes(request.args)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    where, params = [], []
    if etype in enrichment._VALID_ENTITY_TYPES:
        where.append("e.type = ?")
        params.append(etype)
    if min_meetings > 1:
        where.append("e.meeting_count >= ?")
        params.append(min_meetings)
    if q:
        where.append(
            "(e.normalized_name LIKE ? OR e.id IN "
            "(SELECT entity_id FROM entity_aliases WHERE normalized_alias LIKE ?))"
        )
        like = f"%{enrichment._normalize(q)}%"
        params.extend([like, like])
    # C4: напрямок (з розкриттям на проєкти) / теги (OR) / період — власний SQL
    # через ті самі `categories.filter_sql`/`tags_sql`/`date_sql`, що й скрізь.
    cat_frag, cat_params = cats.filter_sql('t.category_id', category_id)
    tags_frag, tags_params = cats.tags_sql('t.id', tag_names)
    date_frag, date_params = cats.date_sql(
        'COALESCE(t.meeting_date, t.created_at)', since_val, until_val)
    axes_sql = cat_frag + tags_frag + date_frag
    if axes_sql:
        where.append(
            "e.id IN (SELECT me.entity_id FROM meeting_entities me "
            "JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.deleted_at IS NULL" + axes_sql + ")")
        params.extend(cat_params + tags_params + date_params)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    order = {
        "mentions": "e.mention_count DESC",
        "meetings": "e.meeting_count DESC",
        "name": "e.canonical_name COLLATE NOCASE ASC",
        "recent": "e.updated_at DESC",
    }.get(sort, "e.mention_count DESC")

    with _get_db() as conn:
        rows = conn.execute(
            f"SELECT e.id, e.type, e.canonical_name, e.role, e.mention_count, "
            f"e.meeting_count, e.speaker_id FROM entities e {where_sql} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM entities e {where_sql}", params
        ).fetchone()["n"]

    return jsonify({
        "success": True, "total": total, "limit": limit, "offset": offset,
        "min_meetings": min_meetings,
        "entities": [dict(r) for r in rows],
    })


@memory_bp.route('/api/memory/entities/<int:eid>', methods=['GET'])
def entity_detail(eid: int):
    """Деталі сутності: мітинги (таймлайн), задачі-власник, aliases."""
    with _get_db() as conn:
        ent = conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        if not ent:
            return jsonify({"success": False, "error": tr('knowledge.memory.err.entity_not_found')}), 404

        meetings = conn.execute(
            "SELECT t.id, t.source_name, t.source_type, t.created_at, "
            "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date, "
            "me.mention_count, me.role_in_meeting "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ? AND t.deleted_at IS NULL "
            "ORDER BY meeting_date DESC, t.id DESC",
            (eid,),
        ).fetchall()

        actions = conn.execute(
            "SELECT ai.id, ai.task, ai.due, ai.status, ai.transcription_id, "
            "t.source_name FROM action_items ai "
            "LEFT JOIN transcriptions t ON t.id = ai.transcription_id AND t.deleted_at IS NULL "
            "WHERE ai.owner_entity_id = ? ORDER BY ai.id DESC",
            (eid,),
        ).fetchall()

        aliases = conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (eid,),
        ).fetchall()

    return jsonify({
        "success": True,
        "entity": dict(ent),
        "meetings": [dict(r) for r in meetings],
        "action_items": [dict(r) for r in actions],
        "aliases": [r["alias"] for r in aliases],
    })


# ============================================================
# Action items
# ============================================================

# Дашборд-вікна дедлайну. Взаємно виключні й вичерпні — кожна задача потрапляє
# РІВНО в одне відро, інакше бакети на /tasks брехали б сумою.
#
# NB: свідомо відрізняються від вікон MCP-зводів (commitments._WINDOWS):
#  1. Там `this_week` = понеділок…неділя і тому ПЕРЕТИНАЄТЬСЯ з `overdue`. Для
#     понеділкового зводу це нормально (два окремі списки), для дашборда — ні.
#  2. Календарний тиждень вироджується під кінець: у суботу «цей тиждень» — це
#     два дні, і відро стабільно порожнє. Тому тут `soon` — це СЛИЗЬКЕ вікно
#     «наступні 7 днів від сьогодні», яке однаково корисне будь-якого дня.
_TASK_WINDOWS = ('overdue', 'soon', 'later', 'no_date', 'all')
_SOON_DAYS = 7


def _soon_end_iso(today=None) -> str:
    """ISO-дата кінця вікна «найближчі 7 днів»."""
    return ((today or _date.today()) + _timedelta(days=_SOON_DAYS)).isoformat()


def _task_window_sql(window: str, today=None):
    """(sql_clauses, params) для дашборд-вікна. Порожньо для 'all'."""
    t = (today or _date.today()).isoformat()
    if window == 'no_date':
        return ["ai.due_date IS NULL"], []
    if window == 'overdue':
        return ["ai.due_date IS NOT NULL", "ai.due_date < ?"], [t]
    if window == 'soon':
        return ["ai.due_date >= ?", "ai.due_date <= ?"], [t, _soon_end_iso(today)]
    if window == 'later':
        return ["ai.due_date > ?"], [_soon_end_iso(today)]
    return [], []


@memory_bp.route('/api/memory/action-items', methods=['GET'])
def list_action_items():
    """Задачі. Query: status (open|done|cancelled|stale|all), owner_entity_id,
    owner (canonical або сира назва), category_id, tags (через кому), since,
    until, period (24h|7d|month|year|all), window (overdue|soon|later|no_date|all),
    limit (контракт C4 — невалідна вісь category_id/tags/since/until → 400).

    Додатково повертає ``windows`` (лічильники по відрах у межах решти фільтрів)
    і ``owners`` (фасет власників) — щоб дашборд показував правдиві суми навіть
    коли сам список обрізаний лімітом."""
    status = request.args.get('status', 'open')
    owner = request.args.get('owner_entity_id')
    owner_name = (request.args.get('owner') or '').strip()
    try:
        limit = min(int(request.args.get('limit', 200)), 1000)
    except ValueError:
        limit = 200

    window = (request.args.get('window') or 'all').strip().lower()
    if window not in _TASK_WINDOWS:
        window = 'all'

    try:
        category_id, tag_names, since_val, until_val = _parse_axes(request.args)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Три НЕЗАЛЕЖНІ групи умов, кожна тримає свої clauses разом зі своїми
    # params. Це не педантизм: нижче потрібні три різні комбінації тих самих
    # фільтрів (список / лічильники відер / фасет власників), і зшивати їх з
    # одного плоского списку без прив'язки параметрів до умов неможливо.
    common, common_p = [], []
    if status and status != 'all':
        common.append("ai.status = ?")
        common_p.append(status)
    # T4.6: задачі мітингу, який видалено (soft-delete), не мають спливати на
    # дашборді — незалежно від category-фільтра нижче.
    common.append(
        "ai.transcription_id IN (SELECT id FROM transcriptions WHERE deleted_at IS NULL)")
    # Трек 1: повтори однієї домовленості з різних зустрічей позначені dup_of →
    # за замовчуванням показуємо лише канонічну (include_dups=1 щоб побачити всі).
    if request.args.get('include_dups') not in ('1', 'true', 'yes'):
        common.append("ai.dup_of IS NULL")

    # Фільтр періоду — за датою запису-джерела (meeting_date, інакше created_at),
    # бо користувач мислить «задачі за останні N» у термінах коли був дзвінок,
    # а не коли відпрацював enrichment. Whitelisted модифікатори SQLite.
    period = (request.args.get('period') or 'all').strip().lower()
    _period_mod = {'24h': '-1 day', '7d': '-7 days', 'month': '-1 month', 'year': '-1 year'}
    if period in _period_mod:
        common.append("date(COALESCE(t.meeting_date, t.created_at)) >= date('now', ?)")
        common_p.append(_period_mod[period])

    # C4: напрямок (з розкриттям на проєкти) / теги (OR) / since-until — ті самі
    # осі, що в search/ask/entities, через `categories.filter_sql`/`tags_sql`/
    # `date_sql` на вже приєднаному в `_JOINS` `t` (без окремого підзапиту).
    cat_frag, cat_params = cats.filter_sql('t.category_id', category_id)
    tags_frag, tags_params = cats.tags_sql('t.id', tag_names)
    date_frag, date_params = cats.date_sql(
        'COALESCE(t.meeting_date, t.created_at)', since_val, until_val)
    axes_sql = cat_frag + tags_frag + date_frag
    axes_params = cat_params + tags_params + date_params

    own, own_p = [], []
    if owner:
        own.append("ai.owner_entity_id = ?")
        own_p.append(owner)
    if owner_name:
        # Точний збіг, а не підрядок: значення приходить з чипа фасета (він і
        # зібраний тим самим COALESCE), і підрядок зробив би чип «Андрій»
        # фільтром, що тягне ще й «Андрій Ткаченко».
        #
        # Дві гілки, бо одне й те саме імʼя живе у двох місцях:
        #   1) канонічне імʼя сутності (або сире, коли звʼязку немає) — це чип;
        #   2) аліаси графа — після злиття сутностей (`entity_dedup merge`) усі
        #      інші написання людини переїжджають САМЕ туди. У власника архіву
        #      це 40 варіантів, і без цієї гілки посилання виду
        #      `/tasks?owner=Мельник` давало порожньо при живих даних.
        #
        # Третьої гілки — по сирому `ai.owner_name` — тут свідомо НЕМАЄ, хоча в
        # MCP-фільтрі вона є. На дашборді число на чипі має дорівнювати довжині
        # списку, а сире імʼя цю рівність ламає: задача 2566 звучить як «Юлія»,
        # але звʼязана з сутністю «Юля», тобто живе під ІНШИМ чипом — і список
        # ставав на рядок довшим за чип, який його відкрив. Для вільного тексту
        # є MCP (`list_action_items`), там підрядок і доречний.
        # Порівняння аліасів — по `normalized_alias`, і ключ рахуємо ТІЄЮ Ж
        # функцією, що його й записала (`enrichment._normalize`): робити це в
        # SQL через LOWER() не можна — він не згортає кирилицю, тож
        # регістронезалежність вийшла б фікцією.
        own.append("(COALESCE(e.canonical_name, ai.owner_name) = ? "
                   "OR EXISTS(SELECT 1 FROM entity_aliases ea "
                   "WHERE ea.entity_id = e.id AND ea.normalized_alias = ?))")
        own_p += [owner_name, enrichment._normalize(owner_name)]

    win, win_p = _task_window_sql(window)

    def _sql(*groups):
        # common завжди непорожній (deleted_at IS NULL), тож `WHERE` завжди є
        # ДО того, як сюди дописується `axes_sql` (фрагменти з власним " AND ").
        cl, pr = [], []
        for c, p in groups:
            cl += c
            pr += p
        base = ("WHERE " + " AND ".join(cl)) if cl else ""
        return base + axes_sql, pr + axes_params

    where_sql, params = _sql((common, common_p), (own, own_p), (win, win_p))
    # Лічильники відер — у межах решти фільтрів, але БЕЗ вікна: інакше активне
    # відро завжди показувало б себе як 100%, а решту як нулі.
    count_sql, count_p = _sql((common, common_p), (own, own_p))
    # Фасет власників — фільтр РІВНЯ СТОРІНКИ, тож рахуємо його без вікна і без
    # самого себе: чипи мають показувати «скільки всього задач у цієї людини»,
    # а не «скільки її задач потрапило у відро, яке зараз відкрите».
    facet_sql, facet_p = _sql((common, common_p))

    _JOINS = ("FROM action_items ai "
              "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
              "LEFT JOIN entities e ON e.id = ai.owner_entity_id ")

    # Протерміноване — СВІЖИМ УГОРУ (задача, прострочена вчора, ще жива; торішня
    # — археологія). Решта вікон — найближчий дедлайн першим, недатоване в хвіст.
    # Тай-брейк — дата розмови, потім id: після Волни 5.1 id більше не збігається
    # з хронологією (задачі з переписки записані останніми, а говорять про
    # травень), і на самому id вони витісняли дзвінки з ведра «без дати».
    order_sql = ("ORDER BY ai.due_date DESC, meeting_date DESC, ai.id DESC"
                 if window == 'overdue'
                 else "ORDER BY ai.due_date IS NULL, ai.due_date, meeting_date DESC, ai.id DESC")

    today = _date.today().isoformat()
    soon_end = _soon_end_iso()

    with _get_db() as conn:
        rows = conn.execute(
            f"SELECT ai.id, ai.task, ai.owner_name, ai.owner_entity_id, ai.due, "
            f"ai.due_date, ai.due_precision, ai.stale_at, ai.dup_of, "
            f"ai.status, ai.transcription_id, ai.source, t.source_name, t.category_id, "
            # Волна 5.1: у списку зʼявились задачі з переписки, а source_name у
            # TG — це превʼю самого повідомлення («[TG] Так, звичайно, плануємо…»),
            # тобто підпис, який повторює текст задачі. Картці потрібна адреса:
            # чат і автор репліки.
            f"t.source_type, t.tg_chat_title, t.tg_sender, "
            f"COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date, "
            f"e.canonical_name AS owner_canonical "
            f"{_JOINS}{where_sql} {order_sql} LIMIT ?",
            (*params, limit),
        ).fetchall()

        wc = conn.execute(
            f"SELECT "
            f"SUM(CASE WHEN ai.due_date IS NULL THEN 1 ELSE 0 END) AS no_date, "
            f"SUM(CASE WHEN ai.due_date IS NOT NULL AND ai.due_date < ? THEN 1 ELSE 0 END) AS overdue, "
            f"SUM(CASE WHEN ai.due_date >= ? AND ai.due_date <= ? THEN 1 ELSE 0 END) AS soon, "
            f"SUM(CASE WHEN ai.due_date > ? THEN 1 ELSE 0 END) AS later, "
            f"COUNT(*) AS total "
            f"{_JOINS}{count_sql}",
            (today, today, soon_end, soon_end, *count_p),
        ).fetchone()

        _facet_and = " AND " if facet_sql else "WHERE "
        owners = [dict(r) for r in conn.execute(
            f"SELECT COALESCE(e.canonical_name, ai.owner_name) AS owner, "
            f"MIN(ai.owner_entity_id) AS owner_entity_id, COUNT(*) AS n "
            f"{_JOINS}{facet_sql}{_facet_and}"
            f"COALESCE(e.canonical_name, ai.owner_name) IS NOT NULL "
            f"GROUP BY owner ORDER BY n DESC, owner LIMIT 40",
            facet_p,
        ).fetchall()]

    windows = {k: (wc[k] or 0) for k in ('no_date', 'overdue', 'soon', 'later')}
    windows['all'] = wc['total'] or 0
    return jsonify({
        "success": True,
        "action_items": [dict(r) for r in rows],
        "windows": windows,
        "owners": owners,
        "today": today,
        "soon_end": soon_end,
        "soon_days": _SOON_DAYS,
    })


@memory_bp.route('/api/memory/action-items/<int:aid>', methods=['PATCH'])
def update_action_item(aid: int):
    """Оновити статус задачі. Body: {"status": "open|done|cancelled"}."""
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('open', 'done', 'cancelled'):
        return jsonify({"success": False, "error": tr('knowledge.memory.err.status_bad')}), 400
    # K5: той самий писальний шлях, що й кнопки брифінгу — з подією в журналі.
    from app.services import commitments
    try:
        commitments.apply_action(current_app.config['DATABASE'], aid, status,
                                 source='web', actor='web')
    except LookupError:
        return jsonify({"success": False, "error": tr('knowledge.memory.err.task_not_found')}), 404
    return jsonify({"success": True})


# ============================================================
# Статистика
# ============================================================

@memory_bp.route('/api/memory/stats', methods=['GET'])
def stats():
    from app.services import embeddings
    with _get_db() as conn:
        by_type = {
            r["type"]: r["n"] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM entities GROUP BY type"
            ).fetchall()
        }
        # Значущі = згадані у ≥2 мітингах (хребет графа; решта — разовий шум).
        by_type_sig = {
            r["type"]: r["n"] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM entities WHERE meeting_count >= 2 GROUP BY type"
            ).fetchall()
        }
        actions = {
            r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM action_items GROUP BY status"
            ).fetchall()
        }
        # T6.5: «embedded» рахуємо тим самим предикатом, що й список роботи
        # індексера (enrichment.list_unenriched_ids) — модель І версія. Інакше
        # після бампу EMBED_VERSION на одному екрані стояли б два числа, які
        # суперечать одне одному: «векторизовано 4502/4503» і «до обробки 4503».
        tx = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END) AS enriched, "
            "SUM(CASE WHEN embedded_at IS NOT NULL AND embedding_model = ? "
            "          AND embedding_version = ? THEN 1 ELSE 0 END) AS embedded "
            "FROM transcriptions WHERE deleted_at IS NULL",
            (embeddings.EMBED_MODEL, embeddings.EMBED_VERSION),
        ).fetchone()
        chunks_total = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    return jsonify({
        "success": True,
        "entities": by_type,
        "entities_significant": by_type_sig,
        "action_items": actions,
        "transcriptions": {
            "total": tx["total"] or 0,
            "enriched": tx["enriched"] or 0,
            "embedded": tx["embedded"] or 0,
        },
        "chunks": chunks_total,
        "enrichment_available": enrichment.is_available(),
        "embeddings_available": embeddings.is_available(),
        "embedding_model": embeddings.EMBED_MODEL,
    })
