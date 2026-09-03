"""Шар коментарів (Comments Layer) — дані, індекс, граф, живий дзвінок.

Коментар — це речення, яке власник написав ПРО запис, а не сказав У записі.
Він уточнює, виправляє або акцентує те, що в сирій стенограмі сказано погано,
неповно чи просто неправильно. Тому в пошуку він важить БІЛЬШЕ за транскрипт,
а не нарівні з ним.

П'ять речей, які цей модуль тримає:

1. **CRUD із поліморфною ціллю.** Коментувати можна не лише транскрипт: файл
   Медіатеки без тексту, задачу, сутність, сесію запису. Реєстр допустимих
   цілей (`TARGETS`) живе тут, а не в БД — так само, як `action_items.source`
   і `meeting_entities.source` валідуються в коді. Ціль адресується числом
   або рядком (сесія запису — `rec_<hex>`), і це вирішує саме реєстр.

2. **Власний індекс.** `comment_chunks` + `comment_chunks_fts` (міграція v37)
   — дзеркало `chunks`/`chunks_fts` за формою, окреме за таблицею. Чому не
   спільна таблиця — довга причина записана в самій міграції; коротка: у
   `chunks.transcription_id` NOT NULL, а коментар на нетранскрибованому файлі
   транскрипту не має.

3. **Вага.** `kind` → множник у ранжуванні (Волна 2) і ознака «підшивати
   навіть без збігу». `weight` — ручний override; NULL означає «взяти з kind».

4. **Похідні.** Локальні звʼязки з графом (`link_entities`, безкоштовно, на
   інжесті) і розбір через Claude на вимогу (`analyze` — задачі й сутності,
   ніколи не автоматично).

5. **Живий дзвінок.** Коментар пишеться на сесію запису, поки транскрипту ще
   немає, і переїжджає на нього після транскрибування (`retarget`) зі
   збереженням `anchor_time`.

Деградація. Без моделі ембедингів (`embeddings.is_available()` = False)
коментар усе одно створюється, лягає у FTS (тригери) і показується на картці —
просто без вектора, до наступного `reindex`. Мовчазної втрати немає:
`embedded_at` лишається NULL, і бекфіл бачить роботу.

CLI (офлайн, ідемпотентні):

    python -m app.services.comments reindex [--force] [--dry-run] [--limit N]
    python -m app.services.comments relink [--dry-run]
    python -m app.services.comments analyze <id> [--force]     # платно
    python -m app.services.comments stats
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

from app.db.connection import get_db_connection
from app.services import embeddings


logger = logging.getLogger(__name__)


# ============================================================
# Реєстр цілей і типів
# ============================================================

#: Що можна коментувати: target_type → (таблиця, колонка id, вид ключа).
#: Валідація тут, а не FK у БД: FK на одну таблицю неможливий для
#: поліморфної цілі, а тримати вісім nullable-колонок замість пари
#: (type, id) — гірше в усьому, крім формальної цілісності.
#:
#: Вид ключа ("int" | "str") — не дрібниця, а те, чим адресується картка.
#: Сесія запису має рядковий id (`rec_<hex16>`), і саме вона потрібна тоді,
#: коли коментар найцінніший: під час дзвінка транскрипту ще не існує. Рядкові
#: цілі живуть у `target_key`, числові — у `target_id` (міграція v39).
TARGETS: dict[str, tuple[Optional[str], Optional[str], str]] = {
    "transcription": ("transcriptions", "id", "int"),
    "audio_download": ("audio_downloads", "id", "int"),
    "action_item": ("action_items", "id", "int"),
    "entity": ("entities", "id", "int"),
    "speaker": ("speakers", "id", "int"),
    "category": ("categories", "id", "int"),
    "tg_thread": ("tg_threads", "id", "int"),
    # Сесія запису: рядковий ключ, власної таблиці немає (живе в манифесті на
    # диску), тож існування не перевіряємо — інакше коментар під час дзвінка
    # створити було б неможливо. Після транскрибування коментарі сесії
    # перецепляються на транскрипт (`retarget`, Волна 4).
    "recording_session": (None, None, "str"),
}


def key_kind(target_type: str) -> str:
    """Яким ключем адресується цей тип цілі: 'int' або 'str'."""
    entry = TARGETS.get(target_type)
    return entry[2] if entry else "int"


#: Що лежить у `target_id`, коли картка адресується рядком. Колонка створена
#: у v37 як `NOT NULL`, а знімати обмеження заради цього довелось би
#: перебудовою таблиці, на яку посилається FK з `comment_chunks` — надто
#: багато ризику заради косметики. Нуль безпечний: AUTOINCREMENT у SQLite
#: починає з 1, тож id=0 не належить жодному рядку жодної таблиці.
STR_TARGET_ID = 0


def _norm_target(target_type: str, target_id) -> tuple[int, Optional[str]]:
    """(target_id, target_key) під вид ключа цього типу цілі."""
    if key_kind(target_type) == "str":
        key = str(target_id).strip()
        if not key:
            raise CommentError("порожній ключ цілі")
        return STR_TARGET_ID, key
    try:
        return int(target_id), None
    except (TypeError, ValueError):
        raise CommentError(f"ціль {target_type} адресується числом, отримано {target_id!r}")


def _target_clause(target_type: str, target_id) -> tuple[str, list]:
    """WHERE-фрагмент «це та сама картка» — один перемикач на весь модуль."""
    tid, tkey = _norm_target(target_type, target_id)
    if tkey is not None:
        return " AND target_type = ? AND target_key = ?", [target_type, tkey]
    return " AND target_type = ? AND target_id = ?", [target_type, tid]

#: kind → вага в ранжуванні. Не декоративний список: саме він вирішує, чи
#: коментар переважить сирий транскрипт при рівній релевантності.
KIND_WEIGHTS: dict[str, float] = {
    "correction": 1.0,   # виправляє сказане в записі
    "decision": 0.8,     # підсумок / ухвалене рішення
    "note": 0.6,         # звичайна замітка (дефолт)
    "context": 0.5,      # передісторія, тло
    "question": 0.4,     # відкрите питання до себе
}
DEFAULT_KIND = "note"

#: Типи, які підшиваються до видачі, навіть якщо самі не збіглися із запитом
#: (Волна 2). Виправлення критичне саме там, де спільних слів із питанням
#: немає: «Іван більше не в проєкті» не збігається з «хто відповідає за X».
ATTACH_KINDS = frozenset({"correction"})

#: Джерела створення — для провенансу у видачі й експорті.
SOURCES = frozenset({"ui", "live", "mcp", "import"})

MAX_BODY_CHARS = 20000


class CommentError(ValueError):
    """Некоректний вхід (невідома ціль/тип, порожнє тіло, немає такої картки)."""


def kind_weight(kind: Optional[str], weight: Optional[float] = None) -> float:
    """Ефективна вага коментаря: ручний override, інакше — за типом."""
    if weight is not None:
        return max(0.0, min(1.0, float(weight)))
    return KIND_WEIGHTS.get((kind or DEFAULT_KIND), KIND_WEIGHTS[DEFAULT_KIND])


# ============================================================
# Валідація
# ============================================================

def _validate(target_type: str, kind: Optional[str], source: Optional[str],
              body: str) -> tuple[str, str]:
    if target_type not in TARGETS:
        raise CommentError(
            f"невідомий тип цілі: {target_type!r} "
            f"(відомі: {', '.join(sorted(TARGETS))})")
    k = (kind or DEFAULT_KIND).strip().lower()
    if k not in KIND_WEIGHTS:
        raise CommentError(
            f"невідомий тип коментаря: {kind!r} "
            f"(відомі: {', '.join(sorted(KIND_WEIGHTS))})")
    s = (source or "ui").strip().lower()
    if s not in SOURCES:
        raise CommentError(f"невідоме джерело: {source!r}")
    if not (body or "").strip():
        raise CommentError("порожній коментар")
    if len(body) > MAX_BODY_CHARS:
        raise CommentError(f"коментар довший за {MAX_BODY_CHARS} символів")
    return k, s


def target_exists(db_path: str, target_type: str, target_id: int) -> bool:
    """Чи існує картка, до якої чіпляють коментар.

    Перевірка потрібна, бо ціль поліморфна і FK її не тримає: без неї
    коментар на видалену/неіснуючу картку тихо осів би в БД, знаходився б
    пошуком і не мав би де показатись. Для типів без окремої таблиці
    (`recording_session`) перевірити нічого — вважаємо, що існує.
    """
    entry = TARGETS.get(target_type)
    table, col = (entry[0], entry[1]) if entry else (None, None)
    if not table:
        return True
    with get_db_connection(db_path) as conn:
        exists = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone())
        if not exists:
            return False
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {col} = ?", (target_id,)).fetchone()
    return bool(row)


# ============================================================
# CRUD
# ============================================================

_ROW_FIELDS = (
    "id, target_type, target_id, target_key, body, kind, weight, pinned, anchor_time, "
    "anchor_chunk_id, author, source, lang, parent_id, created_at, updated_at, "
    "embedded_at, embedding_model, embedding_version, analyzed_at, analyzed_model"
)


def _to_dict(row) -> dict:
    d = dict(row)
    d["pinned"] = bool(d.get("pinned"))
    # Один вихідний контракт для обох видів ключа: споживач (фронт, MCP,
    # експорт) не має знати, у якій колонці лежить адреса картки.
    key = d.get("target_key")
    d["target_ref"] = key if key is not None else d.get("target_id")
    d["effective_weight"] = kind_weight(d.get("kind"), d.get("weight"))
    d["indexed"] = bool(d.get("embedded_at"))
    # Розбір платний і запускається кнопкою, тож інтерфейс мусить розрізняти
    # «ще не розбирали» і «розібрали, задач не знайшлось» — інакше власник
    # платить за той самий коментар удруге.
    d["analyzed"] = bool(d.get("analyzed_at"))
    return d


def create(db_path: str, target_type: str, target_id, body: str,
           kind: str = DEFAULT_KIND, *, pinned: bool = False,
           weight: Optional[float] = None, anchor_time: Optional[float] = None,
           anchor_chunk_id: Optional[int] = None, author: Optional[str] = None,
           source: str = "ui", parent_id: Optional[int] = None,
           check_target: bool = True) -> dict:
    """Створити коментар. Індексація НЕ виконується тут — див. `index_comment`
    (її ставить у чергу блюпринт, щоб POST не чекав на GPU).

    `target_id` — число або рядок залежно від типу цілі (див. `TARGETS`)."""
    k, s = _validate(target_type, kind, source, body)
    tid, tkey = _norm_target(target_type, target_id)
    if check_target and tkey is None and not target_exists(db_path, target_type, tid):
        raise CommentError(f"немає картки {target_type}#{target_id}")
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO comments (target_type, target_id, target_key, body, kind, weight, "
            "pinned, anchor_time, anchor_chunk_id, author, source, parent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_type, tid, tkey, body.strip(), k, weight,
             1 if pinned else 0, anchor_time, anchor_chunk_id, author, s, parent_id),
        )
        cid = cur.lastrowid
        conn.commit()
        row = conn.execute(
            f"SELECT {_ROW_FIELDS} FROM comments WHERE id = ?", (cid,)).fetchone()
    logger.info("[comments] +%s#%s %s (%s)", target_type, target_id, cid, k)
    return _to_dict(row)


def get(db_path: str, comment_id: int) -> Optional[dict]:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_ROW_FIELDS} FROM comments "
            "WHERE id = ? AND deleted_at IS NULL", (comment_id,)).fetchone()
    return _to_dict(row) if row else None


def update(db_path: str, comment_id: int, *, body: Optional[str] = None,
           kind: Optional[str] = None, pinned: Optional[bool] = None,
           weight: Optional[float] = None,
           anchor_time: Optional[float] = None) -> Optional[dict]:
    """Правка коментаря. Зміна ТІЛА скидає `embedded_at` — інакше в індексі
    назавжди лишився б вектор старої редакції, а картка показувала б нову
    (тиха розбіжність між тим, що видно, і тим, що шукається)."""
    cur = get(db_path, comment_id)
    if not cur:
        return None
    sets, params = [], []
    body_changed = False
    if body is not None:
        _validate(cur["target_type"], kind or cur["kind"], "ui", body)
        body_changed = body.strip() != (cur["body"] or "").strip()
        sets.append("body = ?"); params.append(body.strip())
    if kind is not None:
        k = kind.strip().lower()
        if k not in KIND_WEIGHTS:
            raise CommentError(f"невідомий тип коментаря: {kind!r}")
        sets.append("kind = ?"); params.append(k)
    if pinned is not None:
        sets.append("pinned = ?"); params.append(1 if pinned else 0)
    if weight is not None:
        sets.append("weight = ?"); params.append(max(0.0, min(1.0, float(weight))))
    if anchor_time is not None:
        sets.append("anchor_time = ?"); params.append(anchor_time)
    if not sets:
        return cur
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if body_changed:
        sets.append("embedded_at = NULL")
    with get_db_connection(db_path) as conn:
        conn.execute(f"UPDATE comments SET {', '.join(sets)} WHERE id = ?",
                     params + [comment_id])
        if body_changed:
            # Старі чанки прибираємо одразу, а не при переіндексації: інакше
            # у вікні між правкою і reindex пошук віддавав би текст, якого
            # на картці вже немає.
            conn.execute("DELETE FROM comment_chunks WHERE comment_id = ?", (comment_id,))
        conn.commit()
    return get(db_path, comment_id)


def delete(db_path: str, comment_id: int) -> bool:
    """Soft-delete (як `transcriptions`/`audio_downloads`) + зняття з індексу.

    Чанки стираємо ФІЗИЧНО, хоча сам рядок лишається: soft-delete існує заради
    «Скасувати» в UI, а не заради того, щоб видалений коментар ще тиждень
    впливав на відповіді RAG. Відновлення поверне його в чергу індексації.
    """
    # Якір читаємо ДО видалення: після нього `get` уже не побачить рядок, і
    # перерахувати граф не буде від чого.
    existing = get(db_path, comment_id)
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE comments SET deleted_at = CURRENT_TIMESTAMP, embedded_at = NULL, "
            "analyzed_at = NULL WHERE id = ? AND deleted_at IS NULL", (comment_id,))
        conn.execute("DELETE FROM comment_chunks WHERE comment_id = ?", (comment_id,))
        # Задачі, витягнуті Claude саме з ЦЬОГО коментаря, йдуть разом із ним.
        # Інакше вони лишались би «open» у дашборді й у понеділковому зводі
        # назавжди, а джерело було б невидиме: власник видалив коментар як
        # помилковий, а три задачі з нього продовжують вимагати уваги, і
        # дізнатись, звідки вони, вже нізвідки. Знімаємо і `analyzed_at`, щоб
        # після відновлення коментар можна було розібрати наново.
        conn.execute("DELETE FROM action_items WHERE comment_id = ?", (comment_id,))
        conn.commit()
        ok = cur.rowcount > 0
    if ok and existing:
        _refresh_graph(db_path, existing)
    return ok


def restore(db_path: str, comment_id: int) -> bool:
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE comments SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
            (comment_id,))
        conn.commit()
        ok = cur.rowcount > 0
    if ok:
        _refresh_graph(db_path, get(db_path, comment_id))
    return ok


def _refresh_graph(db_path: str, comment: Optional[dict]) -> None:
    """Перерахувати звʼязки графа для картки цього коментаря.

    Тут, а не лише в блюпринті: видалений коментар, чиї згадки лишились у
    графі, — це факт, якого більше ніхто не стверджує, і знайти таке потім
    неможливо. Гарантія має триматись на рівні сервісу, щоб її не треба було
    повторювати в кожного виклику (CLI, майбутній MCP, тести).
    """
    if not comment:
        return
    try:
        tid = resolve_transcription(db_path, comment["target_type"],
                                    comment.get("target_ref", comment.get("target_id")))
        if tid is not None:
            link_entities(db_path, tid)
    except Exception:
        # Граф — надбудова над коментарем: сам коментар уже видалено/повернуто
        # коректно, а розбіжність лікує `relink`. Ковтаємо з логом.
        logger.warning("[comments] граф після зміни #%s не оновлено",
                       comment.get("id"), exc_info=True)


def list_for(db_path: str, target_type: str, target_id,
             include_deleted: bool = False) -> list[dict]:
    """Коментарі однієї картки. Порядок — закріплені зверху, далі за часом:
    на картці спершу треба бачити те, що власник свідомо підняв.

    Для сесії запису (рядковий ключ) сортуємо всередині закріплених за
    `anchor_time`, коли він є: під час дзвінка коментарі природно читаються
    за ходом розмови, а не за моментом натискання Enter."""
    where, params = _target_clause(target_type, target_id)
    sql = f"SELECT {_ROW_FIELDS} FROM comments WHERE 1=1{where}"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    sql += " ORDER BY pinned DESC, COALESCE(anchor_time, 1e18), created_at ASC"
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_to_dict(r) for r in rows]


def counts_for(db_path: str, target_type: str,
               target_ids: list) -> dict:
    """Лічильники для СПИСКУ карток одним запитом.

    Окрема функція, а не корельований підзапит у кожному списковому SELECT:
    списків уже чотири (Бібліотека, Медіатека, задачі, сутності), і
    підзапит довелось би дублювати в кожному, кожен раз ризикуючи розійтись
    у визначенні «живого» коментаря.

    Returns {target: {"n": скільки всього, "corrections": скільки виправлень}}.
    """
    raw = list(target_ids or [])
    if not raw:
        return {}
    is_str = key_kind(target_type) == "str"
    col = "target_key" if is_str else "target_id"
    try:
        ids = [str(i) for i in raw] if is_str else [int(i) for i in raw]
    except (TypeError, ValueError):
        raise CommentError(f"ціль {target_type} адресується числом")
    out: dict = {}
    # Ріжемо на пачки: SQLite має ліміт на кількість параметрів (999 у
    # стандартній збірці), а «вибрати всі N за фільтром» у Бібліотеці
    # піднімає до 3000 id.
    CHUNK = 800
    with get_db_connection(db_path) as conn:
        for i in range(0, len(ids), CHUNK):
            part = ids[i:i + CHUNK]
            ph = ",".join("?" * len(part))
            # Окремо рахуємо ВИПРАВЛЕННЯ: бейдж на картці має третій стан
            # («у записі щось спростовано»), і без цього числа фронт не може
            # його показати — а це найважливіше, що картка про себе каже.
            rows = conn.execute(
                f"SELECT {col} AS ref, COUNT(*) AS n, "
                f"SUM(CASE WHEN kind = 'correction' THEN 1 ELSE 0 END) AS fixes "
                f"FROM comments WHERE target_type = ? AND deleted_at IS NULL "
                f"AND {col} IN ({ph}) GROUP BY {col}",
                [target_type] + part).fetchall()
            for r in rows:
                key = r["ref"] if is_str else int(r["ref"])
                out[key] = {"n": int(r["n"]), "corrections": int(r["fixes"] or 0)}
    return out


def _feed_where(kind, target_type, since, search) -> tuple[str, list]:
    sql = " WHERE c.deleted_at IS NULL"
    params: list = []
    if kind:
        sql += " AND c.kind = ?"; params.append(kind)
    if target_type:
        sql += " AND c.target_type = ?"; params.append(target_type)
    if since:
        sql += " AND c.created_at >= ?"; params.append(since)
    if search:
        # LIKE, а не FTS: сторінка гортає ВЛАСНІ записи, яких сотні, і
        # підрядок тут чесніший за токенізацію — власник шукає фразу, яку сам
        # писав, часто з середини слова. Порівняння в Python не завернеш, тож
        # покладаємось на LIKE з COLLATE NOCASE — для латиниці працює, для
        # кирилиці лишається регістрозалежним (SQLite LOWER кирилицю не
        # згортає — той самий обмежувач, що і в решті проєкту).
        sql += " AND c.body LIKE ?"; params.append(f"%{search}%")
    return sql, params


def list_recent(db_path: str, limit: int = 50, kind: Optional[str] = None,
                target_type: Optional[str] = None,
                since: Optional[str] = None, search: Optional[str] = None,
                offset: int = 0) -> dict:
    """Стрічка коментарів для оглядової сторінки: рядки + total + фасети.

    Повертає `dict`, а не список: сторінці потрібен і знаменник (скільки всього
    під фільтром), і розкладка за типами — інакше «5 виправлень» читається як
    повна картина, хоча це 5 із 200 (та сама пастка, через яку у зводі зʼявився
    `coverage`).

    `target_name` збирається одним LEFT JOIN на транскрипти й картки Медіатеки —
    решта типів цілей показуються як «тип #id»: тягнути сюди ще пʼять таблиць
    заради рідких випадків дорожче, ніж воно того варте.
    """
    where, params = _feed_where(kind, target_type, since, search)
    sel = ", ".join("c." + f for f in _ROW_FIELDS.split(", "))
    sql = (f"SELECT {sel}, t.source_name AS tx_name, a.title AS audio_title "
           f"FROM comments c "
           f"LEFT JOIN transcriptions t ON c.target_type = 'transcription' "
           f"AND t.id = c.target_id "
           f"LEFT JOIN audio_downloads a ON c.target_type = 'audio_download' "
           f"AND a.id = c.target_id"
           # id як тай-брейкер обовʼязковий: `created_at` має роздільність в
           # одну секунду, а живі коментарі пишуться чергою по кілька за раз —
           # без нього рівні за часом поверталися б у порядку rowid, тобто
           # ЗВОРОТНО до заявленого «найновіші зверху».
           f"{where} ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?")
    lim = max(1, min(int(limit), 500))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params + [lim, max(0, int(offset))]).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM comments c{where}", params).fetchone()[0]
        # Фасети рахуємо БЕЗ фільтра за типом — інакше, вибравши «виправлення»,
        # користувач бачив би лічильники «виправлення 5, решта 0» і не міг би
        # оцінити, куди перемикатись.
        fw, fp = _feed_where(None, target_type, since, search)
        by_kind = dict(conn.execute(
            f"SELECT c.kind, COUNT(*) FROM comments c{fw} GROUP BY c.kind", fp).fetchall())
    out = []
    for r in rows:
        d = _to_dict(r)
        name = d.pop("tx_name", None) or d.pop("audio_title", None)
        d.pop("tx_name", None); d.pop("audio_title", None)
        d["target_name"] = name or f"{d['target_type']} #{d['target_ref']}"
        out.append(d)
    return {"comments": out, "total": int(total), "by_kind": by_kind,
            "limit": lim, "offset": max(0, int(offset))}


def retarget(db_path: str, from_type: str, from_id,
             to_type: str, to_id) -> int:
    """Перецепити коментарі з однієї картки на іншу.

    Потрібно рівно для одного сценарію (Волна 4), але сценарій обовʼязковий:
    під час дзвінка коментар пишеться до СЕСІЇ ЗАПИСУ (транскрипту ще немає),
    а після finalize має жити на транскрипті — інакше все, що власник
    надиктував по ходу дзвінка, лишилось би висіти на id, якого не видно
    з жодного екрана.
    """
    if to_type not in TARGETS or from_type not in TARGETS:
        raise CommentError(f"невідомий тип цілі: {to_type!r}")
    new_id, new_key = _norm_target(to_type, to_id)
    where, params = _target_clause(from_type, from_id)
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE comments SET target_type = ?, target_id = ?, target_key = ?, "
            f"updated_at = CURRENT_TIMESTAMP WHERE 1=1{where}",
            [to_type, new_id, new_key] + params)
        conn.commit()
        n = cur.rowcount
    if n:
        logger.info("[comments] %s#%s → %s#%s: перецеплено %d",
                    from_type, from_id, to_type, to_id, n)
        # Нова ціль може мати граф (транскрипт) — старий якір уже не діє.
        _refresh_graph(db_path, {"id": None, "target_type": to_type,
                                 "target_id": to_id})
    return n


# ============================================================
# Індексер
# ============================================================

def _build_comment_chunks(body: str) -> list[dict]:
    """Порізати тіло коментаря на чанки.

    Переважна більшість коментарів — один-два абзаци, тобто один чанк, і
    ділити там нічого. Довгі (вставлена цитата листа, розгорнутий розбір)
    ріжемо тим самим текстовим чанкером, що й транскрипти
    (`embeddings._window_text`) — щоб довгий коментар не втрачав хвіст і
    щоб форма чанка збігалася з формою чанків транскрипту, які він
    конкурує витіснити.
    """
    text = (body or "").strip()
    if not text:
        return []
    parts = embeddings._window_text(text)
    return [{"chunk_index": i, "text": p} for i, p in enumerate(parts) if p.strip()]


def index_comment(db_path: str, comment_id: int, force: bool = False) -> dict:
    """Порізати + закодувати + зберегти коментар. Ідемпотентно через
    `embedded_at` + модель + версію (та сама умова, що й у транскриптів)."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, body, embedded_at, embedding_model, embedding_version, "
            "deleted_at FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if not row:
        return {"status": "not_found", "comment_id": comment_id}
    if row["deleted_at"]:
        return {"status": "skipped", "comment_id": comment_id, "reason": "deleted"}

    already = (bool(row["embedded_at"])
               and row["embedding_model"] == embeddings.EMBED_MODEL
               and row["embedding_version"] == embeddings.EMBED_VERSION)
    if already and not force:
        return {"status": "skipped", "comment_id": comment_id,
                "reason": "already_embedded"}

    chunks = _build_comment_chunks(row["body"])
    if not chunks:
        return {"status": "empty", "comment_id": comment_id}

    # Без моделі коментар лишається у FTS (тригери наповнюють
    # comment_chunks_fts при INSERT), але БЕЗ вектора і БЕЗ позначки
    # embedded_at — тобто бекфіл потім знайде його як роботу. Це і є
    # деградація без тихої втрати: лексичний пошук працює одразу.
    available = embeddings.is_available()
    vecs = embeddings.embed_texts([c["text"] for c in chunks]) if available else None

    with get_db_connection(db_path) as conn:
        conn.execute("DELETE FROM comment_chunks WHERE comment_id = ?", (comment_id,))
        for i, ch in enumerate(chunks):
            blob = (vecs[i].astype(np.float32).tobytes()) if available else None
            conn.execute(
                "INSERT INTO comment_chunks (comment_id, chunk_index, text, "
                "embedding, token_estimate) VALUES (?, ?, ?, ?, ?)",
                (comment_id, ch["chunk_index"], ch["text"], blob, len(ch["text"]) // 4))
        if available:
            conn.execute(
                "UPDATE comments SET embedded_at = CURRENT_TIMESTAMP, "
                "embedding_model = ?, embedding_version = ? WHERE id = ?",
                (embeddings.EMBED_MODEL, embeddings.EMBED_VERSION, comment_id))
        conn.commit()

    status = "embedded" if available else "fts_only"
    logger.info("[comments] #%s проіндексовано: %d чанк(ів), %s",
                comment_id, len(chunks), status)
    return {"status": status, "comment_id": comment_id, "chunks": len(chunks)}


def pending_ids(db_path: str, limit: Optional[int] = None,
                force: bool = False) -> list[int]:
    """Коментарі, які треба (пере)індексувати.

    Умова ОБОВʼЯЗКОВО дзеркалить skip-логіку `index_comment` — інакше бекфіл
    вічно віддавав би рядки, які індексер мовчки пропускає, і робота б не
    закінчувалась (та сама пастка, що описана в `embeddings` для
    `list_unenriched_ids`).
    """
    sql = "SELECT id FROM comments WHERE deleted_at IS NULL"
    params: list = []
    if not force:
        sql += (" AND (embedded_at IS NULL OR embedding_model IS NOT ? "
                "OR embedding_version IS NOT ?)")
        params += [embeddings.EMBED_MODEL, embeddings.EMBED_VERSION]
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"; params.append(int(limit))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [int(r["id"]) for r in rows]


def reindex(db_path: str, force: bool = False, limit: Optional[int] = None,
            dry_run: bool = False) -> dict:
    """Бекфіл: проіндексувати все, що чекає. Ідемпотентний."""
    ids = pending_ids(db_path, limit=limit, force=force)
    if dry_run:
        return {"pending": len(ids), "ids": ids[:50], "dry_run": True}
    stats = {"pending": len(ids), "embedded": 0, "fts_only": 0,
             "skipped": 0, "empty": 0}
    for cid in ids:
        res = index_comment(db_path, cid, force=force)
        stats[res["status"]] = stats.get(res["status"], 0) + 1
    return stats


def stats(db_path: str) -> dict:
    with get_db_connection(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE deleted_at IS NULL").fetchone()[0]
        indexed = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE deleted_at IS NULL "
            "AND embedded_at IS NOT NULL").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM comment_chunks").fetchone()[0]
        by_kind = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM comments WHERE deleted_at IS NULL "
            "GROUP BY kind").fetchall())
        by_target = dict(conn.execute(
            "SELECT target_type, COUNT(*) FROM comments WHERE deleted_at IS NULL "
            "GROUP BY target_type").fetchall())
    return {"total": total, "indexed": indexed, "pending": total - indexed,
            "chunks": chunks, "by_kind": by_kind, "by_target": by_target}


# ============================================================
# Звʼязки з графом (Волна 3, шар А — локальний, безкоштовний)
# ============================================================

#: Провенанс звʼязку в `meeting_entities.source`. Мітка не формальна: згадка в
#: коментарі означає «названо в репліці ПРО зустріч», а не «названо НА
#: зустрічі». Це різні твердження, і зріз за людиною, який їх змішає, збреше
#: рівно так, як уже одного разу збрехав звід (memory/derived-claims-need-second-source).
ENTITY_SOURCE = "comment"


def resolve_transcription(db_path: str, target_type: str,
                          target_id: int) -> Optional[int]:
    """До якого транскрипту прикріпити похідні коментаря (граф, задачі).

    Граф і задачі в Recall транскрипт-центричні: `meeting_entities` і
    `action_items` обидва тримають `transcription_id NOT NULL`. Тому коментар
    дає похідні лише там, де транскрипт можна назвати:

    - `transcription` — сам себе;
    - `audio_download` — його транскрипт, ЯКЩО він уже є (файл без тексту —
      головний зріз Медіатеки, і це нормальний стан, а не помилка);
    - `action_item` — транскрипт, на якому висить задача;
    - `recording_session` — через картку Медіатеки, яку створив finalize
      (`audio_downloads.recording_session_id`), і далі її транскрипт. Поки
      дзвінок іде, ланцюг обривається на першому кроці — і це правильно:
      транскрипту ще фізично немає.

    Решта типів (сутність, спікер, напрямок, нитка) якоря не мають —
    повертаємо None, і шар А для них просто не працює. Прив'язати їх силоміць
    до «якогось» транскрипту було б гірше за відсутність звʼязку: граф
    отримав би факт, якого ніхто не стверджував.
    """
    if target_type == "transcription":
        return int(target_id)
    with get_db_connection(db_path) as conn:
        if target_type == "audio_download":
            row = conn.execute(
                "SELECT t.id FROM transcriptions t "
                "JOIN audio_downloads a ON a.id = ? "
                "WHERE t.file_path = a.file_path AND t.deleted_at IS NULL "
                "ORDER BY t.id DESC LIMIT 1", (int(target_id),)).fetchone()
            return int(row["id"]) if row else None
        if target_type == "action_item":
            row = conn.execute(
                "SELECT transcription_id FROM action_items WHERE id = ?",
                (int(target_id),)).fetchone()
            return int(row["transcription_id"]) if row else None
        if target_type == "recording_session":
            row = conn.execute(
                "SELECT t.id FROM transcriptions t "
                "JOIN audio_downloads a ON a.recording_session_id = ? "
                "WHERE t.file_path = a.file_path AND t.deleted_at IS NULL "
                "ORDER BY t.id DESC LIMIT 1", (str(target_id),)).fetchone()
            return int(row["id"]) if row else None
    return None


def link_entities(db_path: str, transcription_id: int) -> dict:
    """Перерахувати звʼязки графа, породжені коментарями цього транскрипту.

    Рахуємо ВСІ живі коментарі запису за раз, а не один щойно доданий. Причина
    в ключі: `meeting_entities` має PRIMARY KEY (transcription_id, entity_id) і
    жодного поля під коментар — тож «прибрати звʼязки цього коментаря»
    неможливо, не зачепивши сусідні. Повний перерахунок по запису робить
    операцію ідемпотентною за побудовою і однаково правильною після створення,
    правки й видалення коментаря.

    Чужі звʼязки (Claude-картка `source IS NULL`, TG `source='thread_match'`)
    не чіпаються — стирається лише власний `source='comment'`.

    Локально і безкоштовно: використовує той самий словник назв і те саме
    правило «написано як власна назва», що й інжест Telegram
    (`tg_entities.find_exact_mentions`). Спільне правило тут обовʼязкове —
    розійшовшись, два шари почали б давати різні звʼязки з однакового тексту.

    Свідомо точний матчер, а не `find_mentions`: замір історії 04 бачив лише
    точні збіги, і `TG_ENTITIES_MORPH_ENABLED` — прапорець інжесту TG, він не
    має тихо міняти те, що пише коментарний шар (вимкнути прапорець і зняти
    вже записані морфо-звʼязки коментарів тут нічим).
    """
    from app.services import tg_entities

    names = tg_entities.names_for(db_path)
    if not names:
        return {"transcription_id": transcription_id, "status": "no_entities"}

    with get_db_connection(db_path) as conn:
        bodies = [r["body"] for r in conn.execute(
            "SELECT body FROM comments WHERE deleted_at IS NULL "
            "AND target_type = 'transcription' AND target_id = ?",
            (transcription_id,)).fetchall()]

        hits: set[int] = set()
        for body in bodies:
            hits |= tg_entities.find_exact_mentions(body, names)

        conn.execute(
            "DELETE FROM meeting_entities WHERE source = ? AND transcription_id = ?",
            (ENTITY_SOURCE, transcription_id))
        written = 0
        for eid in hits:
            # OR IGNORE, а не REPLACE: якщо сутність уже привʼязана до цього
            # запису Claude-карткою, її звʼязок — сильніше твердження («звучало
            # на зустрічі»), і перетирати його провенансом коментаря не можна.
            cur = conn.execute(
                "INSERT OR IGNORE INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, source) "
                "VALUES (?, ?, 1, ?)", (transcription_id, eid, ENTITY_SOURCE))
            written += cur.rowcount
        conn.commit()

    return {"transcription_id": transcription_id, "status": "ok",
            "comments": len(bodies), "mentions": len(hits), "written": written}


def link_entities_for_comment(db_path: str, comment_id: int) -> dict:
    """Зручна обгортка для шляху інжесту: знайти якір коментаря і перерахувати.

    Свідомо мовчазна на коментарях без якоря — це не збій, а нормальний стан
    (замітка на сутності або на файлі без транскрипта)."""
    c = get(db_path, comment_id)
    if not c:
        return {"comment_id": comment_id, "status": "not_found"}
    tid = resolve_transcription(db_path, c["target_type"], c["target_ref"])
    if tid is None:
        return {"comment_id": comment_id, "status": "no_anchor"}
    res = link_entities(db_path, tid)
    res["comment_id"] = comment_id
    return res


def relink_all(db_path: str, dry_run: bool = False) -> dict:
    """Прохід по всіх транскриптах, що мають коментарі. Ідемпотентний."""
    with get_db_connection(db_path) as conn:
        tids = [int(r["target_id"]) for r in conn.execute(
            "SELECT DISTINCT target_id FROM comments WHERE deleted_at IS NULL "
            "AND target_type = 'transcription' ORDER BY target_id").fetchall()]
    if dry_run:
        return {"transcriptions": len(tids), "dry_run": True}
    written = 0
    for tid in tids:
        written += link_entities(db_path, tid).get("written", 0)
    return {"transcriptions": len(tids), "written": written}


# ============================================================
# Розбір коментаря через Claude (Волна 3, шар Б — платний, на вимогу)
# ============================================================

ACTION_SOURCE = "comment"


def _persist_analysis(db_path: str, comment_id: int, transcription_id: int,
                      parsed: dict) -> dict:
    """Записати результат розбору: сутності, звʼязки графа, задачі."""
    from app.services import enrichment, tg_entities

    entity_ids: set[int] = set()
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        for etype, key in (("person", "people"), ("project", "projects"),
                           ("org", "orgs")):
            for item in (parsed.get(key) or []):
                if not isinstance(item, dict):
                    continue
                eid = enrichment._upsert_entity(
                    c, etype, item.get("name") or "",
                    role=item.get("role"), aliases=item.get("aliases") or [])
                if eid:
                    entity_ids.add(eid)
        conn.commit()

    # Локальний перерахунок ПЕРЕД вставкою знайдених Claude: він стирає всі
    # `source='comment'` звʼязки запису, тож зворотний порядок з'їв би те, що
    # ми щойно записали.
    link_entities(db_path, transcription_id)

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        for eid in entity_ids:
            c.execute(
                "INSERT OR IGNORE INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, source) "
                "VALUES (?, ?, 1, ?)", (transcription_id, eid, ENTITY_SOURCE))

        # Свої задачі переписуємо цілком — саме заради цього у v38 зʼявився
        # comment_id. Без нього повторний розбір або стирав би задачі сусідніх
        # коментарів того ж запису, або плодив дублі.
        c.execute("DELETE FROM action_items WHERE comment_id = ?", (comment_id,))
        written = 0
        for it in (parsed.get("action_items") or []):
            if not isinstance(it, dict):
                continue
            task = (it.get("task") or "").strip()
            if not task:
                continue
            c.execute(
                "INSERT INTO action_items (transcription_id, task, owner_name, "
                "due, due_date, status, source, comment_id) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                (transcription_id, task, it.get("owner"), it.get("due"),
                 it.get("due_date"), ACTION_SOURCE, comment_id))
            written += 1

        c.execute(
            "UPDATE comments SET analyzed_at = CURRENT_TIMESTAMP, analyzed_model = ? "
            "WHERE id = ?", (parsed.get("model") or "", comment_id))
        conn.commit()

    if entity_ids:
        with get_db_connection(db_path) as conn:
            enrichment._recompute_entity_aggregates(conn.cursor(), entity_ids)
            conn.commit()
        # Нові сутності мають почати ловитись локальним шаром одразу, а не
        # через 5 хвилин, коли протухне кеш назв.
        tg_entities.reset_names_cache()

    return {"entities": len(entity_ids), "action_items": written}


def analyze(db_path: str, comment_id: int, model: Optional[str] = None,
            force: bool = False, effort: str = "low") -> dict:
    """Витягти з коментаря задачі й сутності через Claude.

    ЗАПУСКАЄТЬСЯ КНОПКОЮ, НЕ АВТОМАТИЧНО. Автоматичний розбір кожного
    коментаря — це платний виклик на кожну замітку, включно з «ага», і рішення
    власника (24.07.2026) прямо тримає Claude на тому, що справді того варте.
    Локальний шар (`link_entities`) працює сам і безкоштовно.

    Без якоря (`resolve_transcription` → None) виходимо ДО виклику моделі:
    задачу нема куди покласти (`action_items.transcription_id` NOT NULL), тож
    платити за витяг, який нікуди не запишеться, немає сенсу.
    """
    from app.services import enrichment, text_polishing

    c = get(db_path, comment_id)
    if not c:
        return {"status": "not_found", "comment_id": comment_id}
    if c.get("analyzed_at") and not force:
        return {"status": "skipped", "comment_id": comment_id,
                "reason": "already_analyzed"}
    if not enrichment.is_available():
        return {"status": "unavailable", "comment_id": comment_id,
                "reason": "no_api_key"}

    tid = resolve_transcription(db_path, c["target_type"], c["target_ref"])
    if tid is None:
        return {"status": "no_anchor", "comment_id": comment_id,
                "reason": f"{c['target_type']}#{c['target_id']} без транскрипта"}

    try:
        parsed = text_polishing.extract_comment_items(
            c["body"], comment_date=str(c["created_at"])[:10],
            model=model, effort=effort)
    except Exception as exc:
        # Мʼяка деградація, як у card-фазі збагачення: коментар лишається на
        # картці й у пошуку, розбір можна повторити. Логуємо — мовчазний збій
        # тут читався б як «Claude нічого не знайшов».
        logger.warning("[comments] розбір #%s не вдався: %s", comment_id, exc)
        return {"status": "retry_needed", "comment_id": comment_id,
                "error": str(exc)}

    counts = _persist_analysis(db_path, comment_id, tid, parsed)
    logger.info("[comments] #%s розібрано: сутностей=%d, задач=%d (in=%d out=%d)",
                comment_id, counts["entities"], counts["action_items"],
                parsed.get("input_tokens", 0), parsed.get("output_tokens", 0))
    return {
        "status": "analyzed", "comment_id": comment_id,
        "transcription_id": tid, "model": parsed.get("model", ""),
        "counts": counts,
        "input_tokens": parsed.get("input_tokens", 0),
        "output_tokens": parsed.get("output_tokens", 0),
        "cache_read_tokens": parsed.get("cache_read_tokens", 0),
    }


# ============================================================
# CLI
# ============================================================

def _default_db() -> str:
    return os.environ.get("DATABASE", os.path.join(os.getcwd(), "whisper_history.db"))


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(prog="python -m app.services.comments")
    p.add_argument("command", choices=["reindex", "relink", "analyze", "stats"])
    p.add_argument("id", nargs="?", type=int, default=None,
                   help="id коментаря (для analyze)")
    p.add_argument("--db", default=_default_db())
    p.add_argument("--force", action="store_true",
                   help="переіндексувати/перерозібрати навіть уже оброблені")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="лише показати, скільки роботи, нічого не писати")
    a = p.parse_args(argv)

    if a.command == "stats":
        res = stats(a.db)
    elif a.command == "relink":
        res = relink_all(a.db, dry_run=a.dry_run)
    elif a.command == "analyze":
        if a.id is None:
            p.error("analyze потребує id коментаря (розбір платний — "
                    "масового прогону тут навмисно немає)")
        res = analyze(a.db, a.id, force=a.force)
    else:
        res = reindex(a.db, force=a.force, limit=a.limit, dry_run=a.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
