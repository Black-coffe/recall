"""Таксономія архіву: дерево напрямків (напрямок → проєкти) + словник тегів.

Спека `directions-projects-tags`, історія 01 (трасер), контракт **C2**;
рішення — ADR-023 (§A дерево, §B теги, §E межі).

**Навіщо цей модуль.** Напрямок фільтрується у 15 місцях двох процесів (Flask,
stdio-MCP), і після появи другого рівня «напрямок = він сам + його проєкти»
кожне з них мусило б писати той самий підзапит руками. Тут — єдине джерело
цього SQL (`filter_sql`), єдиний нормалізатор імені (`norm_name`) і всі
письменники `category_id`/`record_tags`, які повертають **id змінених записів**
— саме їх викликач передає в `reembed.schedule_records_reembed`, бо напрямок і
теги їдуть у контекстний префікс чанка.

**Що модулю заборонено.** Тягнути `torch`, `anthropic` чи `flask` — його
імпортує `mcp_server.py` (stdio), а там важкий імпорт на рівні модуля вішає
сервер назавжди (памʼятка `mcp-stdio-no-heavy-models`). Стеля — stdlib плюс
`app.db.connection` (зʼєднання відкриває викликач, тож поки що й він не
потрібен). `reembed` сюди теж не імпортується: планування re-embed —
робота викликача, який знає свій `db_path`.

**Транзакції.** Функції-письменники роблять `conn.commit()` самі — блупринти й
CLI працюють із короткими зʼєднаннями `get_db_connection(db_path)`. Письменники
на шляху карти чатів (`create_category`, `set_category`, `apply_chat_tags`,
`ensure_tag`) мають ключ `commit=False`: тоді транзакцію завершує викликач —
`scope.apply_chat_categories` робить `commit()` у реальному проході і
`rollback()` у `--dry-run` (один писальний шлях, історія 26). Жоден із них не
кличе `executescript` — той комітить неявно і зламав би відкат.

**Регістр і кирилиця.** Ключ унікальності — `norm_name` (NFKC → strip → стиск
пробілів → casefold), рахується в Python, а не SQLite `LOWER()`: той згортає
лише ASCII, тож «Фонд» і «фонд» проходили як різні (памʼятка
`plan-dictated-sql-ignored-own-memory`, міграція v20).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date as _date

logger = logging.getLogger(__name__)

#: Сентинел «поле не передали» — відрізняє «не чіпати» від «поставити NULL»
#: (той самий патерн, що `record_meta._Unset`).
UNSET = object()

#: Таблиці, які тримають `category_id` без FK (цілісність — тут, в app-коді).
#: `transcriptions` перша: саме її id повертаються на re-embed.
_CATEGORY_REF_TABLES = ("transcriptions", "audio_downloads",
                        "tg_monitored_chats", "copilot_sessions")

#: Дозволені джерела тега на записі (ADR §B): ручний, підказка Claude,
#: успадкований від TG-чату. `source` потрібен для відвʼязування — зняття тега
#: з чату прибирає лише свої рядки, ручний лишається.
TAG_SOURCES = ("manual", "claude", "chat")

#: Стеля довжини назви тега (ADR §B) — перевіряє HTTP-шар, тут лише константа.
TAG_NAME_MAX = 40

#: Роздільник повного шляху вузла («Напрямок → Проєкт»). Той самий символ уже
#: показують арбітр `scope._category_paths`, контекстний префікс чанка і UI —
#: тут він стає ще й ВХОДОМ (`resolve_node`), бо з v47 гола назва проєкту
#: більше не адресує вузол однозначно. Повний шлях — КАНОНІЧНА текстова назва
#: вузла: його віддає `path` кожного елемента `list_tree`, його ж чекають назад.
PATH_SEP = "→"


class CategoryError(ValueError):
    """Очікувана помилка роботи з деревом/словником.

    `.code` — машинний код для HTTP-шару: `not_found` (404 — вузол із URL),
    `bad_parent`, `two_levels`, `self_parent`, `empty`, `bad_name` (400 — криве
    тіло запиту; `bad_name` — порожня/задовга назва тега або `PATH_SEP` у назві
    вузла), `duplicate` (409), `has_projects` (409).
    `.projects` заповнюється лише для `has_projects` — перелік вузлів, які
    заважають видалити/злити напрямок (їх треба спершу перенести руками).
    `.conflict` — лише для `duplicate`: `{"id", "path"}` вузла, що вже зайняв
    назву. З v47 назва унікальна лише серед братів, тож «зайнято» без адреси
    («у якому напрямку») клієнт показати не може.
    `.candidates` — лише для `ambiguous` (409): повні шляхи вузлів, на яких
    сидить та сама гола назва. Без переліку «неоднозначно» — це глухий кут:
    ні людина, ні агент не знають, ЩО передати замість назви.
    """

    def __init__(self, code: str, message: str = "", *, projects: list | None = None,
                 conflict: dict | None = None, candidates: list | None = None):
        super().__init__(message or code)
        self.code = code
        self.projects = projects or []
        self.conflict = conflict or {}
        self.candidates = candidates or []


# ============================================================
# Нормалізація та розбір фільтрів
# ============================================================

def norm_name(name: str) -> str:
    """Єдиний ключ унікальності імені (і напрямку, і тега).

    NFKC → strip → стиск повторних пробілів → casefold. NFKC зводить
    сумісні форми (напр. повноширинні латинські літери) до канонічних, щоб
    візуально однакові назви не жили двома рядками; casefold згортає регістр
    для будь-якого алфавіту. Для 29 наявних напрямків результат збігається зі
    старим `memory._norm_cat`, тому `name_norm` міграція v45 НЕ перераховує.
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    return " ".join(text.strip().split()).casefold()


def clean_name(name: str) -> str:
    """Назва як її збережемо: strip + стиск пробілів, регістр користувача."""
    return " ".join(unicodedata.normalize("NFKC", str(name or "")).strip().split())


def parse_filter(raw) -> "int | str | None":
    """Значення query-параметра `category_id` → значення для `filter_sql`.

    `''`/`None`/`'all'` → `None` (усі напрямки); `'none'` → `'none'` (записи без
    напрямку); цифри → `int` (id будь-якого вузла — напрямку або проєкту).
    Будь-що інше — `ValueError`: тихе «не зрозумів → показую все» означало б,
    що фільтр мовчки протікає (і метрика `filter_leak` брехала б).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"category_id: очікую id, 'none' або 'all' (отримав {raw!r})")
    if isinstance(raw, int):
        return int(raw)
    s = str(raw).strip().lower()
    if not s or s == "all":
        return None
    if s == "none":
        return "none"
    # `isdecimal`, а не `isdigit`: «²»/«½» проходять `isdigit()`, але `int()` на
    # них падає сирим «invalid literal for int()» — внутрішнім текстом замість
    # нашого. `isdecimal()` пропускає рівно те, що `int()` уміє прочитати.
    if s.isdecimal():
        return int(s)
    raise ValueError(f"category_id: очікую id, 'none' або 'all' (отримав {raw!r})")


def filter_sql(column: str, value) -> tuple[str, list]:
    """Фрагмент WHERE для фільтра за напрямком. Завжди починається з `" AND "`.

    `column` — будь-який SQL-вираз із колонкою напрямку (`t.category_id`,
    `cs.category_id`, корельований `COALESCE(...)` Медіатеки — усе одно).

    - `None` → `("", [])` — фільтра немає;
    - `'none'` → `IS NULL` — записи без напрямку;
    - `int` → `IN (SELECT id FROM categories WHERE id = ? OR parent_id = ?)` —
      **це і є вся семантика двох рівнів**: id напрямку розкривається у нього
      самого та його проєкти, id проєкту (листка) дає лише його. Дерево рівно
      двох рівнів (ADR §E), тому рекурсивний CTE не потрібен.
    """
    if value is None:
        return "", []
    if isinstance(value, str):
        value = parse_filter(value)
        if value is None:
            return "", []
    if value == "none":
        return f" AND {column} IS NULL", []
    cid = int(value)
    return (f" AND {column} IN (SELECT id FROM categories "
            f"WHERE id = ? OR parent_id = ?)", [cid, cid])


def parse_tags(raw) -> list[str]:
    """`'AI, інвестори'` або `['AI', 'інвестори']` → список назв тегів.

    Порожні відкидаються, дублі згортаються за `norm_name` (перше написання
    виграє). Порядок збережено — він іде в повідомлення про помилку й у UI.
    """
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = clean_name(item)
        if not name:
            continue
        key = norm_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def tags_sql(tid_column: str, names) -> tuple[str, list]:
    """Фрагмент WHERE для фільтра за тегами (OR — «будь-який із»).

    `tid_column` — вираз із id транскрипту (теги кріпляться лише до
    `transcriptions`). Невідома назва просто нічого не матчить — фільтр
    звужує видачу до порожньої, а не «ігнорується» (інакше показували б чуже).
    """
    names = list(names or [])
    if not names:
        return "", []
    keys = [norm_name(n) for n in names]
    ph = ",".join("?" * len(keys))
    return (f" AND {tid_column} IN (SELECT rt.transcription_id FROM record_tags rt "
            f"JOIN tags tg ON tg.id = rt.tag_id WHERE tg.name_norm IN ({ph}))", keys)


def parse_date(raw) -> "str | None":
    """`'YYYY-MM-DD'` → та сама строка; порожнє → None; інше → `ValueError`.

    Текст помилки — український і в тому самому стилі, що `parse_filter`:
    його дослівно бачить і людина в тості SPA, і агент у полі `error`
    MCP-тулза. Сире `Invalid isoformat string: 'not-a-date'` від
    `date.fromisoformat` не казало ні мовою користувача, ні що саме очікується
    — тому розбір `since`/`until` у всьому Recall іде лише через цю функцію
    (жоден блупринт і жоден тулз не парсить дату сам)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        _date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"Невалідна дата «{s}»: очікується YYYY-MM-DD") from None
    return s


def date_sql(date_expr: str, since=None, until=None) -> tuple[str, list]:
    """Фрагмент WHERE для періоду. Обидві межі **включно**, гранулярність — день.

    `date_expr` задає викликач, бо «дата запису» у кожній таблиці своя
    (`COALESCE(t.meeting_date, t.created_at)` для транскриптів,
    `a.created_at` для аудіо). Жоден шар Recall не парсить «за тиждень» —
    абсолютні ISO-дати приходять ззовні (рішення A11 + ADR §D).
    """
    sql, params = "", []
    s = parse_date(since)
    u = parse_date(until)
    if s:
        sql += f" AND date({date_expr}) >= ?"
        params.append(s)
    if u:
        sql += f" AND date({date_expr}) <= ?"
        params.append(u)
    return sql, params


# ============================================================
# Читання дерева
# ============================================================

def _node(conn, cid: int) -> "dict | None":
    """Рядок дерева як dict + `path` — канонічна текстова назва вузла (v47).

    `path` тут, а не окремим викликом, бо з v47 гола назва вузол більше не
    адресує: усе, що віддає вузол назовні (HTTP, MCP, повідомлення про дубль),
    мусить нести адресу, інакше клієнт не має що показати і що передати назад.
    """
    row = conn.execute(
        "SELECT id, name, slug, color, icon, sort_order, parent_id, name_norm "
        "FROM categories WHERE id = ?", (int(cid),)).fetchone()
    if row is None:
        return None
    path = row[1]
    if row[6] is not None:
        parent = conn.execute("SELECT name FROM categories WHERE id = ?",
                              (row[6],)).fetchone()
        if parent:
            path = f"{parent[0]} {PATH_SEP} {row[1]}"
    return {"id": row[0], "name": row[1], "slug": row[2], "color": row[3],
            "icon": row[4], "sort_order": row[5], "parent_id": row[6],
            "level": "project" if row[6] is not None else "direction",
            "path": path}


def get_category(conn, cid: int) -> dict:
    node = _node(conn, cid)
    if node is None:
        raise CategoryError("not_found", f"Напрямок {cid} не знайдено")
    return node


def child_ids(conn, cid: int) -> list[int]:
    """id проєктів усередині напрямку (порожньо для проєкта)."""
    return [r[0] for r in conn.execute(
        "SELECT id FROM categories WHERE parent_id = ? "
        "ORDER BY sort_order, name_norm, id", (int(cid),)).fetchall()]


def subtree_ids(conn, category_id: int) -> list[int]:
    """`[сам вузол, *його проєкти]`. Неіснуючий id → `[]`.

    Той самий розкрив, що й у `filter_sql`, але списком — для лічильників,
    письменників і тестів, яким потрібні id, а не фрагмент SQL.
    """
    cid = int(category_id)
    if _node(conn, cid) is None:
        return []
    return [cid, *child_ids(conn, cid)]


def node_path(conn, cid: int) -> str:
    """Повна адреса вузла: `"Напрямок → Проєкт"` або `"Напрямок"`.

    Та сама форма, що її показує арбітр і приймає `resolve_node` — одне
    написання шляху на весь проєкт, щоб «зайнято», «знайди» і «неоднозначно»
    говорили однією мовою."""
    node = _node(conn, cid)
    return node["path"] if node else ""


def name_keys(name, name_norm=None) -> set:
    """Ключі, за якими рядок таблиці впізнається по імені.

    Два, а не один: `name_norm` старих рядків порахований бекфілом v20
    (`strip().casefold()`, без NFKC і без стиску пробілів), тож для них
    `norm_name(name)` і збережений `name_norm` можуть розійтись. Порівнюємо з
    обома — інакше легасі-напрямок просто не знайшовся б за власною назвою.

    Публічний (v47): той самий набір ключів потрібен карті чатів
    (`scope._find_direction`/`_find_child`), і другий його примірник уже
    розходився з цим — один хелпер на весь проєкт."""
    return {norm_name(name), (name_norm or "")} - {""}


def split_path(text) -> list[str]:
    """Текст → частини канонічного шляху, нормалізовані `norm_name`.

    «Фонд Робота → Акмекорп» → `['фонд робота', 'акмекорп']`; гола назва →
    `['маркетинг']`; порожня частина або **три і більше** частин → `[]` (такий
    текст не адресує нічого: третього рівня в дереві немає). Пробіли навколо
    `PATH_SEP` необовʼязкові.

    Єдиний парсер «A → B» на весь проєкт (v47): `_match_nodes`, карта чатів і
    Claude-арбітр читають шлях ним самим, інакше «Фріланс→Маркетинг» і
    «Фріланс → Маркетинг» розʼїжджаються рівно в тому місці, де їх порівнюють.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [norm_name(p) for p in raw.split(PATH_SEP)]
    if len(parts) > 2 or not all(parts):
        return []
    return parts


def _match_nodes(conn, text) -> list[int]:
    """Текст → id усіх вузлів, які він описує (порожньо, якщо жодного).

    Дві форми входу: повний шлях `"Напрямок → Проєкт"` (роздільник `PATH_SEP`,
    пробіли навколо необовʼязкові) — рівно той вузол; гола назва — усі вузли з
    цим `norm_name` (напрямок і однойменні проєкти різних напрямків).
    Публічний вхід — `resolve_node`: вибір «одного з кількох» не робить ніхто.
    """
    parts = split_path(text)
    if not parts:
        return []
    rows = conn.execute("SELECT id, name, name_norm, parent_id FROM categories").fetchall()

    if len(parts) == 2:
        pkey, ckey = parts
        parents = {r[0] for r in rows if r[3] is None and pkey in name_keys(r[1], r[2])}
        return sorted(r[0] for r in rows
                      if r[3] in parents and ckey in name_keys(r[1], r[2]))

    return sorted(r[0] for r in rows if parts[0] in name_keys(r[1], r[2]))


def resolve_node(conn, text) -> "int | None":
    """Текст → id РІВНО одного вузла дерева; `None`, якщо такого немає.

    Єдиний резолвер тексту в вузол на весь проєкт (HTTP, MCP, зріз, карта
    чатів, арбітр). Канонічна форма — повний шлях `"Напрямок → Проєкт"`; гола
    назва лишається дозволеним входом і працює, поки вона одна.

    **Старшинство (v47, хвиля 7):** спершу — точний канонічний шлях, далі гола
    назва. Канонічний шлях напрямку — це його ГОЛА назва, тож «Маркетинг» при
    напрямку «Маркетинг» поруч із чужим проєктом «Фріланс → Маркетинг» дає
    напрямок, а не `ambiguous`. Без цього правила напрямок-тезка був би
    недосяжний текстом назавжди: його ж власний `path` із `list_tree`
    повертався б тим самим `ambiguous`, і `candidates` зациклювались на собі.
    Інваріант: `resolve_node(node["path"]) == node["id"]` для КОЖНОГО вузла, і
    кожен рядок `.candidates` резолвиться назад у свій вузол.

    Кілька збігів — `CategoryError('ambiguous')` з `.candidates` (повні шляхи).
    Виняток, а не «перший збіг» і не тихе обʼєднання піддерев: обидва мовчки
    підмінюють те, що спитали (рішення власника 22.09.2026). Вектори й FTS
    тезок уже розрізняють за побудовою — шлях стоїть у контекстному префіксі
    чанка, — тож «обʼєднати два Маркетинги» означало б змішати саме те, що
    префікс розводив.
    """
    ids = _match_nodes(conn, text)
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    if len(split_path(text)) == 1:
        # Гола назва = канонічний шлях НАПРЯМКУ (двочастинний текст уже й так
        # розібраний як шлях у `_match_nodes`).
        ph = ",".join("?" * len(ids))
        tops = [r[0] for r in conn.execute(
            f"SELECT id FROM categories WHERE parent_id IS NULL AND id IN ({ph})",
            ids).fetchall()]
        if len(tops) == 1:
            return tops[0]
    paths = [node_path(conn, i) for i in ids]
    raise CategoryError(
        "ambiguous",
        f"«{str(text).strip()}» є в кількох напрямках — уточніть повний шлях: "
        + ", ".join(f"«{p}»" for p in paths),
        candidates=paths)


def list_tree(conn) -> list[dict]:
    """Плаский список у порядку дерева: напрямок, одразу за ним — його проєкти.

    `items` — власні живі записи вузла, `items_total` — разом із проєктами
    (для напрямку) або те саме `items` (для проєкта). Сортування —
    `(sort_order, name_norm)` на кожному рівні: саме його бачить користувач у
    селектах, тож воно має бути стабільним і однаковим скрізь.

    `path` (v47) — канонічна назва вузла: «Напрямок → Проєкт» або «Напрямок».
    Її бачить і людина в селекті, і агент у `list_categories`, і саме її вони
    передають назад, коли гола назва сидить на двох вузлах.
    """
    rows = conn.execute(
        "SELECT c.id, c.name, c.slug, c.color, c.icon, c.sort_order, c.parent_id, "
        "       COALESCE(c.name_norm, c.name) AS nn, "
        "       (SELECT COUNT(*) FROM transcriptions t "
        "         WHERE t.category_id = c.id AND t.deleted_at IS NULL) AS items "
        "FROM categories c"
    ).fetchall()

    nodes = {}
    for r in rows:
        nodes[r[0]] = {"id": r[0], "name": r[1], "slug": r[2], "color": r[3],
                       "icon": r[4], "sort_order": r[5] or 0, "parent_id": r[6],
                       "level": "project" if r[6] is not None else "direction",
                       "items": r[8], "items_total": r[8], "path": r[1],
                       "_key": (r[5] or 0, r[7] or "")}

    # Вузол, чий parent зник (розсинхрон), показуємо як напрямок — інакше він
    # випав би з дерева і став невидимим в UI назавжди.
    for node in nodes.values():
        if node["parent_id"] is not None and node["parent_id"] not in nodes:
            logger.warning("[categories] вузол %s посилається на неіснуючий parent %s — "
                           "показую як напрямок", node["id"], node["parent_id"])
            node["parent_id"] = None
            node["level"] = "direction"

    children: dict = {}
    for node in nodes.values():
        if node["parent_id"] is not None:
            children.setdefault(node["parent_id"], []).append(node)

    out: list[dict] = []
    for parent in sorted((n for n in nodes.values() if n["parent_id"] is None),
                         key=lambda n: (n["_key"], n["id"])):
        kids = sorted(children.get(parent["id"], []), key=lambda n: (n["_key"], n["id"]))
        parent["items_total"] = parent["items"] + sum(k["items"] for k in kids)
        out.append(parent)
        for kid in kids:
            kid["path"] = f"{parent['name']} {PATH_SEP} {kid['name']}"
        out.extend(kids)
    for node in out:
        node.pop("_key", None)
    return out


# ============================================================
# Письменники дерева
# ============================================================

def _assert_parent(conn, cid: "int | None", parent_id: "int | None") -> None:
    """Перевірка «рівно два рівні» (ADR §E) — вона ж уся бізнес-логіка дерева."""
    if parent_id is None:
        return
    parent_id = int(parent_id)
    if cid is not None and parent_id == int(cid):
        raise CategoryError("self_parent", "Напрямок не може бути власним батьком")
    parent = _node(conn, parent_id)
    if parent is None:
        # Не `not_found`: 404 лишається за вузлом з URL, а неіснуючий батько —
        # це криве тіло запиту (400), інакше клієнт не розрізнить два випадки.
        raise CategoryError("bad_parent", f"Напрямок {parent_id} не знайдено")
    if parent["parent_id"] is not None:
        raise CategoryError("two_levels",
                            f"«{parent['name']}» — уже проєкт; третього рівня немає")


def _assert_plain_name(name: str) -> None:
    """`PATH_SEP` у назві вузла заборонений (v47, хвиля 7) — `bad_name` ДО запису.

    Повний шлях — канонічна текстова адреса вузла, і `split_path` рахує в ньому
    рівно дві частини. Вузол на імʼя «Фонд → Клієнти» ламає цю арифметику з
    обох боків: сам він не резолвиться за власною назвою, а канонічний шлях
    його дитини («Фонд → Клієнти → Акмекорп») стає тричастинним і теж не
    резолвиться — після чого зріз мовчки розширюється на весь архів.
    `_cat_error` у `memory.py` мапить `bad_name` у 400."""
    if PATH_SEP in str(name or ""):
        raise CategoryError(
            "bad_name",
            f"Назва вузла не може містити «{PATH_SEP}» — це роздільник повного шляху")


def _assert_free_sibling_name(conn, name: str, parent_id: "int | None",
                              exclude_id: "int | None" = None) -> str:
    """Перевірка унікальності назви **серед братів** (v47, ADR-023 §E).

    Брати — це напрямки між собою (`parent_id IS NULL`) або проєкти одного
    батька; напрямок і чужий проєкт можуть збігатись. Перевірка йде в Python
    по `norm_name` і **до** запису — саме тому клієнт бачить 409 з адресою
    («у якому напрямку зайнято»), а не голий `IntegrityError` від індексу
    `idx_categories_parent_name`. Індекс лишається останнім рубежем (гонка
    двох запитів), а не основним контролем."""
    key = norm_name(name)
    if not key:
        raise CategoryError("empty", "Порожня назва")
    pid = int(parent_id) if parent_id is not None else None
    rows = conn.execute(
        "SELECT id, name, name_norm FROM categories WHERE parent_id IS ?", (pid,)).fetchall()
    for row in rows:
        if exclude_id is not None and row[0] == int(exclude_id):
            continue
        if key not in name_keys(row[1], row[2]):
            continue
        conflict = {"id": row[0], "path": node_path(conn, row[0])}
        if pid is None:
            raise CategoryError("duplicate", f"Напрямок «{row[1]}» уже існує",
                                conflict=conflict)
        parent = _node(conn, pid)
        raise CategoryError(
            "duplicate",
            f"Проєкт «{row[1]}» уже є в напрямку «{parent['name'] if parent else pid}»",
            conflict=conflict)
    return key


def _slugify(name: str) -> str:
    """Назва → ключ для `slug`: `norm_name` + усе, що не літера/цифра, на «-»."""
    return re.sub(r"[^\w]+", "-", norm_name(name), flags=re.UNICODE).strip("-")


def _free_slug(conn, name: str, parent_id: "int | None") -> str:
    """Унікальний `slug` — єдина унікальність, що лишилась глобальною (v47).

    Назва більше не унікальна по таблиці, тож два «Маркетинги» в різних
    напрямках мусять якось розійтись у `slug`: перший бере `маркетинг`,
    другий — `<slug батька>-маркетинг`, далі `-2`, `-3`. Детерміновано і
    ніколи не порожньо: порожній slug у двох вузлах — це та сама колізія,
    лише тихіша (NULL-и в UNIQUE-індексі не конфліктують)."""
    taken = {r[0] for r in conn.execute(
        "SELECT slug FROM categories WHERE slug IS NOT NULL").fetchall()}
    base = _slugify(name) or "category"
    if base not in taken:
        return base
    if parent_id is not None:
        prow = conn.execute("SELECT slug, name FROM categories WHERE id = ?",
                            (int(parent_id),)).fetchone()
        if prow:
            parent_slug = prow[0] or _slugify(prow[1])
            if parent_slug:
                base = f"{parent_slug}-{base}"
                if base not in taken:
                    return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def create_category(conn, name, *, color=None, icon=None, parent_id=None,
                    commit: bool = True) -> dict:
    """Створити напрямок (`parent_id=None`) або проєкт усередині напрямку.

    Батько перевіряється ПЕРШИМ: без нього незрозуміло, серед кого шукати
    дубль, і повідомлення «уже є в напрямку «X»» не мало б що назвати."""
    clean = clean_name(name)
    _assert_plain_name(clean)
    _assert_parent(conn, None, parent_id)
    key = _assert_free_sibling_name(conn, clean, parent_id)
    order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO categories (name, name_norm, slug, color, icon, sort_order, parent_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (clean, key, _free_slug(conn, clean, parent_id), color, icon, order,
         int(parent_id) if parent_id is not None else None))
    if commit:
        conn.commit()
    return get_category(conn, cur.lastrowid)


def update_category(conn, cid: int, *, name=UNSET, color=UNSET, icon=UNSET,
                    parent_id=UNSET, sort_order=UNSET) -> tuple[dict, list[int]]:
    """Перейменувати / перефарбувати / перепідпорядкувати вузол.

    Повертає `(вузол, id записів)`, де id — **усе піддерево** вузла, якщо
    змінилась назва або батько: і те, й інше входить у контекстний префікс
    чанка («Напрямок → Проєкт»), тож префікс застарів і в записів проєктів
    теж. Зміна лише кольору/іконки/порядку re-embed не потребує — повертаємо
    порожній список.

    `parent_id=None` робить вузол напрямком; напрямок, у якого є проєкти,
    проєктом стати не може (`two_levels`) — інакше зʼявився б третій рівень.
    `sort_order` — понад контракт C2, бо його вже вміє чинний PATCH.
    """
    current = get_category(conn, cid)
    cid = int(cid)
    fields, params = [], []
    prefix_changed = False

    # Батько — першим: з v47 назва перевіряється серед братів, а хто брати,
    # знає лише НОВИЙ батько. Переїзд «Маркетинга» під напрямок, де такий
    # проєкт уже є, — теж `duplicate`, хоч назву ніхто не міняв.
    new_parent = current["parent_id"]
    reparenting = False
    if parent_id is not UNSET:
        new_parent = int(parent_id) if parent_id is not None else None
        if new_parent is not None:
            _assert_parent(conn, cid, new_parent)
            kids = child_ids(conn, cid)
            if kids:
                raise CategoryError(
                    "two_levels",
                    f"«{current['name']}» має проєкти ({len(kids)}) — спершу перенесіть їх",
                    projects=[_node(conn, k) for k in kids])
        reparenting = new_parent != current["parent_id"]
        if reparenting:
            prefix_changed = True
        fields.append("parent_id = ?")
        params.append(new_parent)

    renaming = name is not UNSET and name is not None
    if renaming or reparenting:
        clean = clean_name(name) if renaming else current["name"]
        _assert_plain_name(clean)
        key = _assert_free_sibling_name(conn, clean, new_parent, exclude_id=cid)
        if renaming:
            if clean != current["name"]:
                prefix_changed = True
            fields += ["name = ?", "name_norm = ?"]
            params += [clean, key]

    for key_name, value in (("color", color), ("icon", icon)):
        if value is not UNSET and value is not None:
            fields.append(f"{key_name} = ?")
            params.append(str(value).strip())

    if sort_order is not UNSET and sort_order is not None:
        fields.append("sort_order = ?")
        params.append(int(sort_order))

    if not fields:
        raise CategoryError("empty", "Нічого оновлювати")

    conn.execute(f"UPDATE categories SET {', '.join(fields)} WHERE id = ?",
                 [*params, cid])
    conn.commit()

    ids: list[int] = []
    if prefix_changed:
        scope = subtree_ids(conn, cid)
        ph = ",".join("?" * len(scope))
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM transcriptions WHERE category_id IN ({ph}) "
            "AND deleted_at IS NULL ORDER BY id", scope).fetchall()]
    return get_category(conn, cid), ids


def _assert_no_projects(conn, cid: int, action: str) -> None:
    kids = child_ids(conn, cid)
    if kids:
        nodes = [_node(conn, k) for k in kids]
        raise CategoryError(
            "has_projects",
            f"{action}: спершу перенесіть проєкти ({', '.join(n['name'] for n in nodes)})",
            projects=nodes)


def delete_category(conn, cid: int) -> None:
    """Видалити вузол. Записи не видаляються — їхній `category_id` зануляється.

    Вузол із проєктами не видаляється і не «підіймає» їх у корінь мовчки
    (`has_projects`): тихий підйом розсипав би розкладку архіву без сліду.
    """
    cid = int(cid)
    get_category(conn, cid)
    _assert_no_projects(conn, cid, "Видалення напрямку")
    null_category_refs(conn, cid)
    conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
    conn.commit()


def merge_categories(conn, keep_id, merge_id) -> list[int]:
    """Перенести всі посилання з `merge_id` у `keep_id` (`None` = «без напрямку»).

    Сам вузол `merge_id` лишається — його видаляє викликач, якщо просив
    (`delete_source`). Повертає id транскриптів, що переїхали: у них змінився
    напрямок, отже застарів префікс чанків.
    """
    merge_id = int(merge_id)
    get_category(conn, merge_id)
    if keep_id is not None:
        keep_id = int(keep_id)
        get_category(conn, keep_id)
    _assert_no_projects(conn, merge_id, "Обʼєднання напрямку")
    moved = [r[0] for r in conn.execute(
        "SELECT id FROM transcriptions WHERE category_id = ? ORDER BY id",
        (merge_id,)).fetchall()]
    reassign_category_refs(conn, merge_id, keep_id)
    conn.commit()
    return moved


def set_category(conn, ids, cid, *, commit: bool = True) -> list[int]:
    """Поставити напрямок записам. Повертає лише ті id, де значення **змінилось**.

    Саме цей список іде в `reembed.schedule_records_reembed`: переембеджувати
    запис, якому поставили той самий напрямок, — це GPU-час у нікуди.
    """
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return []
    cid = int(cid) if cid is not None else None
    ph = ",".join("?" * len(ids))
    changed = [r[0] for r in conn.execute(
        f"SELECT id FROM transcriptions WHERE id IN ({ph}) AND category_id IS NOT ? "
        "ORDER BY id", [*ids, cid]).fetchall()]
    if changed:
        ph2 = ",".join("?" * len(changed))
        conn.execute(f"UPDATE transcriptions SET category_id = ? WHERE id IN ({ph2})",
                     [cid, *changed])
        if commit:
            conn.commit()
    return changed


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (table,)).fetchone())


def null_category_refs(conn, cid: int) -> None:
    """Занулити `category_id` у ВСІХ чотирьох таблицях, що на нього посилаються.

    `audio_downloads` тут зʼявився разом зі спекою: до неї видалення напрямку
    лишало його id висіти на рядках Медіатеки (розвідка, дірка v21).
    """
    for table in _CATEGORY_REF_TABLES:
        if not _table_exists(conn, table):
            continue
        conn.execute(f"UPDATE {table} SET category_id = NULL WHERE category_id = ?",
                     (int(cid),))


def reassign_category_refs(conn, old_id: int, new_id) -> None:
    """Перенести посилання old_id → new_id (`None` = «без напрямку») у 4 таблицях."""
    dst = int(new_id) if new_id is not None else None
    for table in _CATEGORY_REF_TABLES:
        if not _table_exists(conn, table):
            continue
        conn.execute(f"UPDATE {table} SET category_id = ? WHERE category_id = ?",
                     (dst, int(old_id)))


# ============================================================
# Теги
# ============================================================

def _check_tag_name(clean: str) -> None:
    """Єдиний гард назви тега — порожня або довша за `TAG_NAME_MAX` (ADR §B).

    Один код (`bad_name`) для обох випадків: HTTP-шар мапить його в 400 і
    показує людський текст. Викликається з `ensure_tag` (а через неї —
    `set_tags(add)`, `resolve_suggestions(accept)`, `apply_chat_tags`), тож
    жоден письменник тега не обходить перевірку."""
    if not clean:
        raise CategoryError("bad_name", "Порожня назва тега")
    if len(clean) > TAG_NAME_MAX:
        raise CategoryError("bad_name", f"Назва тега довша за {TAG_NAME_MAX} символів")


def _check_raw_tag_names(raw) -> None:
    """Гард сирого списку `add`/`accept` — ДО `parse_tags`.

    `parse_tags` тихо відкидає порожні елементи (правильно для фільтрів), але
    у списку на запис порожній рядок — навмисна крива назва, тож ловимо її тут
    і кидаємо `bad_name` до першого запису (усе-або-нічого)."""
    if raw is None:
        return
    items = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    for item in items:
        if not clean_name(item):
            raise CategoryError("bad_name", "Порожня назва тега")


def ensure_tag(conn, name, color=None, *, commit: bool = True) -> dict:
    """Знайти тег за `norm_name` або створити. Ідемпотентно (ADR §B)."""
    clean = clean_name(name)
    _check_tag_name(clean)
    key = norm_name(clean)
    row = conn.execute("SELECT id, name, color FROM tags WHERE name_norm = ?",
                       (key,)).fetchone()
    if row:
        return {"id": row[0], "name": row[1], "color": row[2]}
    cur = conn.execute("INSERT INTO tags (name, name_norm, color) VALUES (?, ?, ?)",
                       (clean, key, color))
    if commit:
        conn.commit()
    return {"id": cur.lastrowid, "name": clean, "color": color}


def list_tags(conn) -> list[dict]:
    """Словник тегів із лічильником записів, за `name_norm`."""
    rows = conn.execute(
        "SELECT tg.id, tg.name, tg.color, "
        "  (SELECT COUNT(*) FROM record_tags rt JOIN transcriptions t "
        "     ON t.id = rt.transcription_id "
        "    WHERE rt.tag_id = tg.id AND t.deleted_at IS NULL) AS items "
        "FROM tags tg ORDER BY tg.name_norm").fetchall()
    return [{"id": r[0], "name": r[1], "color": r[2], "items": r[3]} for r in rows]


def _tag_or_404(conn, tag_id: int) -> dict:
    row = conn.execute("SELECT id, name, color FROM tags WHERE id = ?",
                       (int(tag_id),)).fetchone()
    if row is None:
        raise CategoryError("not_found", f"Тег {tag_id} не знайдено")
    return {"id": row[0], "name": row[1], "color": row[2]}


def update_tag(conn, tag_id: int, *, name=UNSET, color=UNSET) -> tuple[dict, list[int]]:
    """Перейменувати / перефарбувати тег. Повертає `(тег, id живих записів)`.

    Записи йдуть на re-embed лише коли змінився **точний** збережений рядок
    `name` (вона, не `name_norm`, стоїть у контекстному префіксі чанка) —
    «ai» → «AI» міняє префікс і йде на re-embed, «AI» → «AI» ні; колір/іконка
    префікс не чіпають взагалі. Валідація назви — той самий `_check_tag_name`,
    що й у `ensure_tag`/`set_tags`, тож жоден шлях перейменування не обходить
    ліміт довжини."""
    tag = _tag_or_404(conn, tag_id)
    fields, params = [], []
    name_changed = False
    if name is not UNSET and name is not None:
        clean = clean_name(name)
        _check_tag_name(clean)
        key = norm_name(clean)
        clash = conn.execute("SELECT name FROM tags WHERE name_norm = ? AND id != ?",
                             (key, tag["id"])).fetchone()
        if clash:
            raise CategoryError("duplicate", f"Тег «{clash[0]}» уже існує")
        name_changed = clean != tag["name"]
        fields += ["name = ?", "name_norm = ?"]
        params += [clean, key]
    if color is not UNSET:
        fields.append("color = ?")
        params.append(color)
    if not fields:
        raise CategoryError("empty", "Нічого оновлювати")
    conn.execute(f"UPDATE tags SET {', '.join(fields)} WHERE id = ?",
                 [*params, tag["id"]])
    conn.commit()

    ids: list[int] = []
    if name_changed:
        ids = [r[0] for r in conn.execute(
            "SELECT t.id FROM transcriptions t "
            "JOIN record_tags rt ON rt.transcription_id = t.id "
            "WHERE rt.tag_id = ? AND t.deleted_at IS NULL ORDER BY t.id",
            (tag["id"],)).fetchall()]
    return _tag_or_404(conn, tag["id"]), ids


def delete_tag(conn, tag_id: int) -> list[int]:
    """Видалити тег з каскадом у `record_tags`/`tg_chat_tags` (FK у схемі немає).

    Повертає id записів, які цей тег втратили — їхній префікс змінився.
    """
    tag = _tag_or_404(conn, tag_id)
    ids = [r[0] for r in conn.execute(
        "SELECT transcription_id FROM record_tags WHERE tag_id = ? "
        "ORDER BY transcription_id", (tag["id"],)).fetchall()]
    conn.execute("DELETE FROM record_tags WHERE tag_id = ?", (tag["id"],))
    conn.execute("DELETE FROM tg_chat_tags WHERE tag_id = ?", (tag["id"],))
    conn.execute("DELETE FROM tags WHERE id = ?", (tag["id"],))
    conn.commit()
    return ids


def merge_tags(conn, keep_id: int, merge_id: int) -> list[int]:
    """Злити теги: посилання merge_id → keep_id, сам merge_id видаляється.

    Повертає **усі** записи, що мали `merge_id` — включно з тими, у кого вже
    був і `keep_id`: у них назва тега, що зникає, все одно випадає з
    контекстного префіксу (був список із двох імен, лишилось одне)."""
    keep = _tag_or_404(conn, keep_id)
    merge = _tag_or_404(conn, merge_id)
    if keep["id"] == merge["id"]:
        raise CategoryError("self_parent", "Не можна злити тег із самим собою")
    moved = [r[0] for r in conn.execute(
        "SELECT transcription_id FROM record_tags WHERE tag_id = ? "
        "ORDER BY transcription_id", (merge["id"],)).fetchall()]
    conn.execute("INSERT OR IGNORE INTO record_tags (transcription_id, tag_id, source) "
                 "SELECT transcription_id, ?, source FROM record_tags WHERE tag_id = ?",
                 (keep["id"], merge["id"]))
    conn.execute("INSERT OR IGNORE INTO tg_chat_tags (chat_id, tag_id) "
                 "SELECT chat_id, ? FROM tg_chat_tags WHERE tag_id = ?",
                 (keep["id"], merge["id"]))
    conn.execute("DELETE FROM record_tags WHERE tag_id = ?", (merge["id"],))
    conn.execute("DELETE FROM tg_chat_tags WHERE tag_id = ?", (merge["id"],))
    conn.execute("DELETE FROM tags WHERE id = ?", (merge["id"],))
    conn.commit()
    return moved


def _record_tag_ids(conn, tid: int) -> set:
    return {r[0] for r in conn.execute(
        "SELECT tag_id FROM record_tags WHERE transcription_id = ?", (int(tid),)).fetchall()}


def set_tags(conn, tid: int, *, add=(), remove=(), source: str = "manual") -> bool:
    """Додати/зняти теги запису. `True` — якщо набір справді змінився.

    Відсутні теги в `add` створюються (словник поповнюється з місця вживання —
    так само, як напрямок створюється з будь-якого селекта). `source` пишеться
    лише на нові рядки: ручне підтвердження вже наявного тега не переписує
    його походження.
    """
    tid = int(tid)
    if source not in TAG_SOURCES:
        raise CategoryError("empty", f"Невідоме джерело тега: {source}")
    before = _record_tag_ids(conn, tid)

    _check_raw_tag_names(add)
    add_names = parse_tags(add)
    # Уся `add` перевіряється ДО першого INSERT — інакше крива назва в
    # середині списку лишила б попередні вже записаними (напівзастосований 400).
    for name in add_names:
        _check_tag_name(name)
    for name in add_names:
        tag = ensure_tag(conn, name)
        conn.execute("INSERT OR IGNORE INTO record_tags (transcription_id, tag_id, source) "
                     "VALUES (?, ?, ?)", (tid, tag["id"], source))
    remove_names = parse_tags(remove)
    if remove_names:
        keys = [norm_name(n) for n in remove_names]
        ph = ",".join("?" * len(keys))
        conn.execute(
            f"DELETE FROM record_tags WHERE transcription_id = ? AND tag_id IN "
            f"(SELECT id FROM tags WHERE name_norm IN ({ph}))", [tid, *keys])

    changed = _record_tag_ids(conn, tid) != before
    if changed:
        conn.commit()
    return changed


def tags_for(conn, ids) -> dict:
    """Батч для серіалізаторів: `{transcription_id: [{id, name, color}, …]}`."""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT rt.transcription_id, tg.id, tg.name, tg.color FROM record_tags rt "
        f"JOIN tags tg ON tg.id = rt.tag_id WHERE rt.transcription_id IN ({ph}) "
        f"ORDER BY tg.name_norm", ids).fetchall()
    out: dict = {tid: [] for tid in ids}
    for r in rows:
        out[r[0]].append({"id": r[1], "name": r[2], "color": r[3]})
    return out


def suggested_tags(conn, tid: int) -> list[str]:
    """Назви, які запропонував Claude і людина ще не підтвердила."""
    row = conn.execute("SELECT suggested_tags_json FROM transcriptions WHERE id = ?",
                       (int(tid),)).fetchone()
    if row is None or not row[0]:
        return []
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError):
        logger.warning("[categories] tx=%s: suggested_tags_json не JSON — ігнорую", tid)
        return []
    if not isinstance(data, list):
        return []
    return [clean_name(x) for x in data if clean_name(x)]


def set_suggested_tags(conn, tid: int, names) -> None:
    """Перезаписати підказки (шлях збагачення, історія 06)."""
    names = parse_tags(names)
    conn.execute("UPDATE transcriptions SET suggested_tags_json = ? WHERE id = ?",
                 (json.dumps(names, ensure_ascii=False) if names else None, int(tid)))
    conn.commit()


def resolve_suggestions(conn, tid: int, *, accept=(), dismiss=()) -> bool:
    """Прийняти / відхилити підказки. `True` — якщо щось змінилось.

    Прийняте стає звичайним тегом із `source='claude'` (фільтри не знають про
    статус підказки взагалі — вони читають тільки `record_tags`), обидва
    списки прибираються з `suggested_tags_json`. `accept` назви, якої нема в
    `suggested_tags_json` (за `norm_name`) — `CategoryError(code="not_suggested")`,
    нічого не записується; `dismiss` невідомої назви — no-op (ADR §B: `accept`
    не мусить уміти «створи що завгодно»)."""
    tid = int(tid)
    _check_raw_tag_names(accept)
    accepted = parse_tags(accept)
    dropped = parse_tags(dismiss)
    if accepted:
        pending_keys = {norm_name(n) for n in suggested_tags(conn, tid)}
        unknown = [n for n in accepted if norm_name(n) not in pending_keys]
        if unknown:
            raise CategoryError(
                "not_suggested",
                f"Не запропоновано: {', '.join(unknown)}")
    changed = False
    if accepted:
        changed = set_tags(conn, tid, add=accepted, source="claude") or changed
    handled = {norm_name(n) for n in accepted} | {norm_name(n) for n in dropped}
    if handled:
        pending = suggested_tags(conn, tid)
        rest = [n for n in pending if norm_name(n) not in handled]
        if rest != pending:
            set_suggested_tags(conn, tid, rest)
            changed = True
    return changed


def _chat_record_ids(conn, chat_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM transcriptions WHERE source_type = 'telegram' AND tg_chat_id = ? "
        "AND deleted_at IS NULL ORDER BY id", (int(chat_id),)).fetchall()]


def apply_chat_tags(conn, chat_id: int, names, *, commit: bool = True) -> list[int]:
    """Замінити набір тегів TG-чату — **ретроактивно** на всі його записи.

    Додані теги лягають на історію чату з `source='chat'`; відвʼязані
    знімаються лише там, де стоять від цього чату (`source='chat'`) — ручний
    тег на конкретному повідомленні лишається (ADR §B, правило «один слот —
    один власник»). Повертає id записів, чий набір тегів змінився.
    """
    chat_id = int(chat_id)
    target = {ensure_tag(conn, n, commit=commit)["id"] for n in parse_tags(names)}
    current = {r[0] for r in conn.execute(
        "SELECT tag_id FROM tg_chat_tags WHERE chat_id = ?", (chat_id,)).fetchall()}
    records = _chat_record_ids(conn, chat_id)
    before = {tid: _record_tag_ids(conn, tid) for tid in records}

    removed = current - target
    if removed and records:
        ph_t = ",".join("?" * len(removed))
        ph_r = ",".join("?" * len(records))
        conn.execute(
            f"DELETE FROM record_tags WHERE source = 'chat' AND tag_id IN ({ph_t}) "
            f"AND transcription_id IN ({ph_r})", [*removed, *records])
    for tag_id in target:
        for tid in records:
            conn.execute("INSERT OR IGNORE INTO record_tags "
                         "(transcription_id, tag_id, source) VALUES (?, ?, 'chat')",
                         (tid, tag_id))

    conn.execute("DELETE FROM tg_chat_tags WHERE chat_id = ?", (chat_id,))
    for tag_id in target:
        conn.execute("INSERT OR IGNORE INTO tg_chat_tags (chat_id, tag_id) VALUES (?, ?)",
                     (chat_id, tag_id))
    if commit:
        conn.commit()

    return [tid for tid in records if _record_tag_ids(conn, tid) != before[tid]]


def chat_tags(conn, chat_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT tg.id, tg.name, tg.color FROM tg_chat_tags ct "
        "JOIN tags tg ON tg.id = ct.tag_id WHERE ct.chat_id = ? ORDER BY tg.name_norm",
        (int(chat_id),)).fetchall()
    return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]


def inherit_chat_tags(conn, tid: int, chat_id: int) -> None:
    """Інжест TG: копія тегів чату на свіжий запис (`source='chat'`)."""
    conn.execute(
        "INSERT OR IGNORE INTO record_tags (transcription_id, tag_id, source) "
        "SELECT ?, tag_id, 'chat' FROM tg_chat_tags WHERE chat_id = ?",
        (int(tid), int(chat_id)))
    conn.commit()
