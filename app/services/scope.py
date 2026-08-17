"""Скоуп архіву (Трек 2): напрямки за чатами + автопозначення + проєктний зріз.

**Проблема** (гриль 24.07.2026, підтверджено запитами до БД): скоуп пошуку був
одношаровим — категорія, — і не сужував нічого. Усі 21 моніторених Telegram-чатів
успадковували ОДНУ категорію «Робота», через що вона зібрала 78%
категоризованих фрагментів; ще більша частина архіву не мали категорії взагалі й при
увімкненому скоупі випадали з пошуку. Копілот під час дзвінка одночасно
змішував напрямки і сліпнув до частини архіву.

**Рішення — два шари:**

1. **Грубий (цей модуль, `apply_chat_categories`)**: назва чату вже містить
   напрямок («Nova Dance & Робота», «Ділова англійська & Робота»), тож
   розмітка детермінована і безкоштовна — не ручна робота, а таблиця
   відповідності чат→напрямок. Інджест TG уже успадковує `category_id` з
   `tg_monitored_chats` (`app/blueprints/telegram.py:_resolve_category`), тому
   після проходу нові повідомлення їдуть у правильний напрямок самі.
2. **Тонкий (`resolve_scope`, використовується в `retrieval.search`)**: зріз за
   сутністю-проєктом та учасниками через `meeting_entities` — many-to-many, бо
   одна зустріч чесно буває про три проєкти, і категорія 1:1 це втрачає.

**Карта чат→напрямок НЕ зашита в код**: вона містить назви чатів і людей, а
репозиторій публічний. Модуль читає її з локального JSON (за замовчуванням
`data/chat_categories.local.json`, у .gitignore) — формат у `load_mapping`.

CLI:
    python -m app.services.scope apply-chats --dry-run
    python -m app.services.scope label-rest --dry-run   # k-NN для не-TG записів
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

DEFAULT_MAPPING_PATH = "data/chat_categories.local.json"

# Мінімальна впевненість, щоб проставити напрямок без людини. 0.6 підібрано на
# реальних 216 записах: при 0.5 крізь поріг просочувались явні помилки
# («відео про штукатурку» → «Фиби» з 0.526), при 0.6 вони йдуть на Claude-арбітраж.
KNN_MIN_CONFIDENCE = 0.6

# Щоб не сипати однаковим попередженням на кожен запис (216 однакових рядків
# у логу — це не діагностика, а шум).
_claude_warned = False


def _norm_name(name: str) -> str:
    """Ключ унікальності категорій — той самий, що у міграції v20 (casefold)."""
    return unicodedata.normalize("NFKC", (name or "").strip()).casefold()


def _slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", (name or "").strip().lower())
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return re.sub(r"[\s_]+", "-", s).strip("-") or "cat"


# ============================================================
# Шар 1 — напрямки за Telegram-чатами
# ============================================================

def load_mapping(path: str = DEFAULT_MAPPING_PATH) -> list[dict]:
    """Карта чат→напрямок з локального JSON.

    Формат: {"chats": [{"chat_id": -100…, "title": "…", "category": "NOVA"}, …]}
    `title` — лише для читабельності діффу, зіставлення йде за `chat_id`.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    chats = data.get("chats") if isinstance(data, dict) else data
    if not isinstance(chats, list):
        raise ValueError(f"{path}: очікую {{'chats': [...]}} або список")
    out = []
    for row in chats:
        if row.get("chat_id") is None or not (row.get("category") or "").strip():
            raise ValueError(f"{path}: рядок без chat_id/category: {row}")
        out.append({"chat_id": int(row["chat_id"]), "title": row.get("title") or "",
                    "category": row["category"].strip()})
    return out


def _ensure_categories(conn, names: list[str], *, dry_run: bool) -> dict[str, Optional[int]]:
    """Назва напрямку → id. Відсутні створює (idempotent за name_norm)."""
    existing = {}
    for r in conn.execute("SELECT id, name, name_norm FROM categories"):
        existing[r["name_norm"] or _norm_name(r["name"])] = r["id"]
    order_row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) AS m FROM categories").fetchone()
    next_order = (order_row["m"] or 0) + 1

    resolved: dict[str, Optional[int]] = {}
    for name in dict.fromkeys(names):
        key = _norm_name(name)
        if key in existing:
            resolved[name] = existing[key]
            continue
        if dry_run:
            resolved[name] = None            # ще не існує — покажемо як «створити»
            continue
        cur = conn.execute(
            "INSERT INTO categories (name, slug, sort_order, name_norm) VALUES (?, ?, ?, ?)",
            (name, _slugify(name), next_order, key))
        next_order += 1
        existing[key] = cur.lastrowid
        resolved[name] = cur.lastrowid
    return resolved


def apply_chat_categories(db_path: str, *, mapping_path: str = DEFAULT_MAPPING_PATH,
                          dry_run: bool = False) -> dict:
    """Розкласти Telegram-чати по напрямках: створити відсутні категорії,
    прив'язати чат у `tg_monitored_chats` і перерозмітити його історію.

    Ідемпотентно: повторний прохід нічого не змінює. Записи інших джерел
    (дзвінки/файли) НЕ чіпає — у них своя розмітка (`label_uncategorized`).
    """
    mapping = load_mapping(mapping_path)
    stats = {"chats": len(mapping), "categories_created": [], "chats_bound": 0,
             "chats_inserted": 0, "records_recategorized": 0, "per_chat": []}

    with get_db_connection(db_path) as conn:
        before = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM categories")}
        cat_ids = _ensure_categories(conn, [m["category"] for m in mapping], dry_run=dry_run)
        stats["categories_created"] = sorted(
            {m["category"] for m in mapping if _norm_name(m["category"])
             not in {_norm_name(n) for n in before.values()}})

        for m in mapping:
            cid, cat_id = m["chat_id"], cat_ids.get(m["category"])
            row = conn.execute(
                "SELECT enabled, category_id FROM tg_monitored_chats WHERE chat_id = ?",
                (cid,)).fetchone()
            n_wrong = conn.execute(
                "SELECT COUNT(*) AS n FROM transcriptions WHERE source_type = 'telegram' "
                "AND tg_chat_id = ? AND deleted_at IS NULL "
                "AND (category_id IS NULL OR category_id <> COALESCE(?, -1))",
                (cid, cat_id)).fetchone()["n"]
            stats["per_chat"].append({
                "title": m["title"], "category": m["category"],
                "monitored": bool(row), "records_to_move": n_wrong})
            if dry_run:
                # Превʼю має рахувати те саме, що зробить реальний прохід:
                # інакше «0 привʼязок» у dry-run виглядає як «нічого не зміниться».
                stats["records_recategorized"] += n_wrong
                if row is None:
                    stats["chats_inserted"] += 1
                elif row["category_id"] != cat_id:
                    stats["chats_bound"] += 1
                continue

            if row is None:
                # Чат не моніториться (історія є, слухання вимкнене) — заводимо
                # рядок з enabled=0, щоб майбутнє вмикання успадкувало напрямок,
                # а не повернуло записи в «без категорії».
                conn.execute(
                    "INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                    "category_id) VALUES (?, ?, 'group', 0, ?)", (cid, m["title"], cat_id))
                stats["chats_inserted"] += 1
            elif row["category_id"] != cat_id:
                conn.execute("UPDATE tg_monitored_chats SET category_id = ? WHERE chat_id = ?",
                             (cat_id, cid))
                stats["chats_bound"] += 1

            cur = conn.execute(
                "UPDATE transcriptions SET category_id = ? WHERE source_type = 'telegram' "
                "AND tg_chat_id = ? AND deleted_at IS NULL "
                "AND (category_id IS NULL OR category_id <> ?)", (cat_id, cid, cat_id))
            stats["records_recategorized"] += cur.rowcount
        if not dry_run:
            conn.commit()
    logger.info("apply_chat_categories: chats=%(chats)s bound=%(chats_bound)s "
                "inserted=%(chats_inserted)s records=%(records_recategorized)s", stats)
    return stats


# Токени, надто загальні для розпізнавання напрямку за назвою запису: вони є
# майже в кожній назві («фонд», «робота») і дали б хибні влучання.
_TITLE_STOPWORDS = {"фонд", "робота", "team", "чат", "запис", "call", "дзвінок"}


def _title_matchers(conn) -> list[tuple[int, str, "re.Pattern"]]:
    """Регекси для впізнавання напрямку прямо в назві запису.

    Назви записів у власника вже містять напрямок («NOVA я та Микола по
    маркетингу»), і це детермінований сигнал, сильніший за k-NN.
    """
    out = []
    for r in conn.execute("SELECT id, name FROM categories"):
        name = (r["name"] or "").strip()
        tokens = [t for t in re.split(r"[\s/|,]+", name) if len(t) >= 3
                  and t.lower() not in _TITLE_STOPWORDS]
        if not tokens:
            continue
        alts = [re.escape(name)] + [re.escape(t) for t in tokens]
        out.append((r["id"], name,
                    re.compile(r"(?<![\w])(" + "|".join(alts) + r")(?![\w])", re.IGNORECASE)))
    return out


def _prior_correct(candidates: list[dict], sizes: dict[int, int]) -> list[dict]:
    """Прибрати перекос k-NN у бік найбільшої категорії.

    Сусідів у «Робота» більше просто тому, що записів там у рази більше,
    тож голосування за сирими скорами стягує туди все підряд (перевірено: дзвінок
    з NOVA у назві отримував «Фонд» з 0.625).

    Показник навмисно мʼякий (0.25, не 0.5): при корені перекос перевертався в
    інший бік — крихітні категорії (записів) починали вигравати чужі теми
    (відео про штукатурку падало у «Фиби»). Задача коригування — прибрати
    домінування гіганта, а не зробити переможцем найменшого.
    """
    total = sum(sizes.values()) or 1
    rescored = []
    for c in candidates:
        share = (sizes.get(c["category_id"], 1) or 1) / total
        rescored.append({**c, "score": c["score"] / (share ** 0.25)})
    s = sum(c["score"] for c in rescored) or 1.0
    for c in rescored:
        c["score"] = round(c["score"] / s, 3)
    return sorted(rescored, key=lambda c: -c["score"])


def _ask_claude_category(title: str, excerpt: str, cats: list[tuple[int, str]]) -> Optional[int]:
    """Останній рубіж для записів, де ні назва, ні k-NN не дали впевненості.

    Дешева модель (Haiku) бачить назву, початок тексту і список напрямків —
    для різнорідного хвоста (ютуб-відео, документи, разові дзвінки) це точніше
    за голосування сусідів у перекошеному корпусі. Повертає None, якщо модель
    не впевнена: «не розмічено» краще за «розмічено не туди».
    """
    from app.services import models, text_polishing

    listing = "\n".join(f"{cid}. {name}" for cid, name in cats)
    prompt = (
        "Ось перелік напрямків архіву:\n" + listing +
        f"\n\nЗапис — назва: «{title}»\nПочаток тексту:\n{excerpt[:1500]}\n\n"
        "До якого напрямку він належить? Відповідай ЛИШЕ числом-id зі списку, "
        "або словом NONE, якщо жоден не підходить чи ти не впевнений."
    )
    try:
        client = text_polishing._get_client()
        resp = client.messages.create(
            model=models.HAIKU_4_5, max_tokens=8,
            messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as exc:
        global _claude_warned
        if not _claude_warned:
            logger.warning("scope: Claude-класифікація недоступна (%s) — решта лишається людині", exc)
            _claude_warned = True
        return None
    m = re.search(r"\d+", raw)
    if not m:
        return None
    cid = int(m.group())
    return cid if cid in {c for c, _ in cats} else None


def label_uncategorized(db_path: str, *, min_confidence: float = KNN_MIN_CONFIDENCE,
                        limit: int = 500, dry_run: bool = False, use_claude: bool = False,
                        source_types: Optional[tuple] = None) -> dict:
    """k-NN-автопозначення НЕ-Telegram записів без напрямку.

    Переюзає `categorize.suggest_category` (ті самі ембеддинги, що й пошук) —
    нової моделі не вантажимо. Проставляє лише впевнені підказки; решта
    лишається людині, бо тихо покладена не в той напрямок зустріч гірша за
    непокладену: вона зникає зі скоупу, у якому її шукатимуть.
    """
    from app.services import categorize

    stats = {"scanned": 0, "labeled": 0, "by_title": 0, "by_knn": 0, "by_claude": 0,
             "low_confidence": 0, "no_neighbors": 0, "samples": []}
    types = source_types or ("recording", "file", "meeting_archive", "youtube", "document")
    placeholders = ",".join("?" * len(types))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, source_name, source_type FROM transcriptions "
            f"WHERE category_id IS NULL AND deleted_at IS NULL "
            f"AND source_type IN ({placeholders}) AND embedded_at IS NOT NULL "
            f"ORDER BY id DESC LIMIT ?", (*types, int(limit))).fetchall()
        matchers = _title_matchers(conn)
        sizes = {r["category_id"]: r["n"] for r in conn.execute(
            "SELECT category_id, COUNT(*) AS n FROM transcriptions "
            "WHERE category_id IS NOT NULL AND deleted_at IS NULL GROUP BY category_id")}

    updates: list[tuple] = []
    for r in rows:
        stats["scanned"] += 1
        title = r["source_name"] or ""
        hit = next(((cid, name) for cid, name, rx in matchers if rx.search(title)), None)
        if hit:
            cat_id, cat_name, conf, method = hit[0], hit[1], 0.95, "title"
        else:
            res = categorize.suggest_category(db_path, r["id"])
            if not res.get("suggestion"):
                stats["no_neighbors"] += 1
                continue
            ranked = _prior_correct(res.get("candidates") or [], sizes)
            if not ranked:
                stats["no_neighbors"] += 1
                continue
            cat_id, cat_name = ranked[0]["category_id"], ranked[0]["name"]
            conf, method = ranked[0]["score"], "knn"
        if conf < min_confidence and use_claude:
            with get_db_connection(db_path) as conn:
                cats = [(c["id"], c["name"]) for c in
                        conn.execute("SELECT id, name FROM categories ORDER BY id")]
                row = conn.execute(
                    "SELECT COALESCE(summary_json, substr(COALESCE(polished_text, "
                    "transcript_text), 1, 1500)) AS body FROM transcriptions WHERE id = ?",
                    (r["id"],)).fetchone()
            picked = _ask_claude_category(title, (row["body"] if row else "") or "", cats)
            if picked:
                cat_id = picked
                cat_name = dict(cats).get(picked, "")
                conf, method = 0.9, "claude"
                stats["by_claude"] += 1
        if conf < min_confidence:
            stats["low_confidence"] += 1
            continue
        updates.append((cat_id, r["id"]))
        if method == "knn":
            stats["by_knn"] += 1
        elif method == "title":
            stats["by_title"] += 1
        if len(stats["samples"]) < 25:
            stats["samples"].append({
                "id": r["id"], "source": r["source_type"], "name": title[:58],
                "category": cat_name, "confidence": conf, "method": method})
    if updates and not dry_run:
        with get_db_connection(db_path) as conn:
            conn.executemany("UPDATE transcriptions SET category_id = ? WHERE id = ?", updates)
            conn.commit()
    stats["labeled"] = len(updates)
    logger.info("label_uncategorized: scanned=%(scanned)s labeled=%(labeled)s "
                "low_conf=%(low_confidence)s", stats)
    return stats


# ============================================================
# Шар 2 — проєктний/учасницький зріз
# ============================================================

def resolve_scope(db_path: str, names: list[str] | str,
                  types: tuple = ("project", "org", "person")) -> list[int]:
    """Назви проєктів/людей → id сутностей (канонічні імена + аліаси).

    Приймає рядок або список. Порівняння регістронезалежне за нормалізованим
    ім'ям — тим самим ключем, що використовує enrichment при дедупі.
    """
    if isinstance(names, str):
        names = [n.strip() for n in re.split(r"[,;]", names) if n.strip()]
    if not names:
        return []
    keys = [n.strip().lower() for n in names if n and n.strip()]
    ph_types = ",".join("?" * len(types))
    ph_keys = ",".join("?" * len(keys))
    sql = (
        f"SELECT DISTINCT e.id FROM entities e WHERE e.type IN ({ph_types}) AND ("
        f"  LOWER(e.normalized_name) IN ({ph_keys}) OR LOWER(e.canonical_name) IN ({ph_keys}) "
        f"  OR e.id IN (SELECT a.entity_id FROM entity_aliases a "
        f"              WHERE LOWER(a.normalized_alias) IN ({ph_keys})))"
    )
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, (*types, *keys, *keys, *keys)).fetchall()
    return [r["id"] for r in rows]


def scope_transcription_ids(db_path: str, entity_ids: list[int], *,
                            min_salience: float = 0.0) -> list[int]:
    """Записи, у яких згадані задані сутності (лише граф, без текстового шару)."""
    if not entity_ids:
        return []
    ph = ",".join("?" * len(entity_ids))
    sql = (f"SELECT DISTINCT me.transcription_id FROM meeting_entities me "
           f"JOIN transcriptions t ON t.id = me.transcription_id AND t.deleted_at IS NULL "
           f"WHERE me.entity_id IN ({ph})")
    params: list = list(entity_ids)
    if min_salience:
        sql += " AND COALESCE(me.salience, 0) >= ?"
        params.append(min_salience)
    with get_db_connection(db_path) as conn:
        return [r["transcription_id"] for r in conn.execute(sql, params).fetchall()]


def _name_variants(db_path: str, names: list[str], entity_ids: list[int]) -> list[str]:
    """Назви для текстового пошуку: те, що спитали + канонічні імена та аліаси."""
    out = {n.strip() for n in names if n and n.strip()}
    if entity_ids:
        ph = ",".join("?" * len(entity_ids))
        with get_db_connection(db_path) as conn:
            for r in conn.execute(
                    f"SELECT canonical_name FROM entities WHERE id IN ({ph})", entity_ids):
                out.add(r["canonical_name"])
            for r in conn.execute(
                    f"SELECT alias FROM entity_aliases WHERE entity_id IN ({ph})", entity_ids):
                out.add(r["alias"])
    return [n for n in out if len(n) >= 3]


def scope_filter_ids(db_path: str, project: list[str] | str) -> list[int]:
    """Повний зріз за проєктом/людиною: **граф ∪ текстова згадка**.

    Чому не лише граф: `meeting_entities` існує тільки для збагачених записів
    (зараз це ~помітна частина архіву), тож зріз «лише за сутностями» мовчки викидав решту —
    та сама сліпота, що була в категорій, лише по іншій осі. Перевірено на
    golden-set: очікуване джерело кейса (документ «Сенсети, страница Базовая
    инфраструктура») не має жодного лінка в графі і зникало при звуженні.

    Тому додаємо другий шлях: FTS5-згадка назви (або аліаса) у тексті чанків +
    збіг у назві запису. Це ширше за граф, але чесно ширше — і не ховає записи,
    які просто не пройшли enrichment.
    """
    names = [n.strip() for n in (re.split(r"[,;]", project) if isinstance(project, str)
                                 else list(project)) if str(n).strip()]
    if not names:
        return []
    entity_ids = resolve_scope(db_path, names)
    tids = set(scope_transcription_ids(db_path, entity_ids))

    # Назва може збігатись із НАПРЯМКОМ («Datalink», «Ділова англійська», «HoReCa»):
    # тоді зріз має включати весь напрямок. Без цього латинська назва давала
    # порожньо там, де в графі/тексті лежить кирилиця («Сенсети»).
    # Порівнюємо за name_norm (casefold з Python), а НЕ через SQLite LOWER():
    # вбудований LOWER згортає лише ASCII, тож «Юридичне» не збігалося саме з
    # собою — та сама пастка, через яку в міграції v20 і зʼявився name_norm.
    with get_db_connection(db_path) as conn:
        ph = ",".join("?" * len(names))
        keys = [_norm_name(n) for n in names]
        rows = conn.execute(
            f"SELECT t.id AS tid FROM transcriptions t JOIN categories c ON c.id = t.category_id "
            f"WHERE t.deleted_at IS NULL AND COALESCE(c.name_norm, c.name) IN ({ph})",
            keys).fetchall()
        tids.update(r["tid"] for r in rows)

    variants = _name_variants(db_path, names, entity_ids)
    if variants:
        fts_q = " OR ".join(f'"{v}"' for v in variants)
        like_sql = " OR ".join(["t.source_name LIKE ?"] * len(variants))
        with get_db_connection(db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT DISTINCT ch.transcription_id AS tid FROM chunks_fts f "
                    "JOIN chunks ch ON ch.id = f.rowid "
                    "JOIN transcriptions t ON t.id = ch.transcription_id "
                    "WHERE f.chunks_fts MATCH ? AND t.deleted_at IS NULL", (fts_q,)).fetchall()
                tids.update(r["tid"] for r in rows)
            except Exception as exc:      # екзотичні токени ламають FTS-синтаксис
                logger.debug("scope: FTS-шар пропущено (%s)", exc)
            rows = conn.execute(
                f"SELECT id AS tid FROM transcriptions t WHERE ({like_sql}) "
                f"AND t.deleted_at IS NULL", [f"%{v}%" for v in variants]).fetchall()
            tids.update(r["tid"] for r in rows)
    return sorted(_expand_to_threads(db_path, tids))


def _expand_to_threads(db_path: str, tids: set[int]) -> set[int]:
    """Розширити зріз до ЦІЛИХ ниток розмови (Волна 4.5.1a).

    Назва проєкту в переписці звучить один раз, а рішення по ньому — у сусідніх
    репліках, де назви вже немає («а скільки там?», «ок, беремо»). Тому зріз,
    зібраний по згадках, віддавав окремі репліки замість розмов: на живих даних
    Ковальчука згадана 23 рази всередині фонд-чатів, і саме ці 23 однорядковики
    були всім, що бачив `project=`.

    Нитка — природна межа розширення: вона вже обмежена сплеском і темою, тож
    це не розповзання на весь чат (розповзання по чату було б поверненням до
    того, від чого волна й лікує: чат ≠ тема)."""
    if not tids:
        return tids
    ids = list(tids)
    out = set(tids)
    with get_db_connection(db_path) as conn:
        for i in range(0, len(ids), 500):     # SQLite обмежує число параметрів
            batch = ids[i:i + 500]
            ph = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT DISTINCT s.id AS tid FROM transcriptions t "
                f"JOIN transcriptions s ON s.tg_thread_id = t.tg_thread_id "
                f"WHERE t.id IN ({ph}) AND t.tg_thread_id IS NOT NULL "
                f"AND s.deleted_at IS NULL", batch).fetchall()
            out.update(r["tid"] for r in rows)
    return out


# ============================================================
# CLI
# ============================================================

def _print(res: dict) -> int:
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Optional[list] = None) -> int:
    # CLI стартує поза app.py, який зазвичай і вантажить .env — без цього
    # Claude-арбітраж «не бачить» ключ, що лежить у файлі поруч.
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except ImportError:
        pass

    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(prog="scope", description="Напрямки та зрізи архіву (Трек 2).")
    p.add_argument("--db", default=default_db)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Нічого не писати в БД")

    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("apply-chats", parents=[common],
                       help="Розкласти TG-чати по напрямках за локальною картою")
    a.add_argument("--file", default=DEFAULT_MAPPING_PATH)
    l = sub.add_parser("label-rest", parents=[common],
                       help="k-NN-автопозначення не-TG записів без напрямку")
    l.add_argument("--min-confidence", type=float, default=KNN_MIN_CONFIDENCE)
    l.add_argument("--limit", type=int, default=500)
    l.add_argument("--claude", action="store_true",
                   help="Арбітраж Haiku для записів, де назва і k-NN не дали впевненості")
    s = sub.add_parser("scope", help="Показати id сутностей і кількість записів у зрізі")
    s.add_argument("names", help="Назви через кому: «Acmecorp,Ковальчука»")

    args = p.parse_args(argv)
    if args.command == "apply-chats":
        return _print(apply_chat_categories(args.db, mapping_path=args.file,
                                            dry_run=args.dry_run))
    if args.command == "label-rest":
        return _print(label_uncategorized(args.db, min_confidence=args.min_confidence,
                                          limit=args.limit, dry_run=args.dry_run,
                                          use_claude=args.claude))
    ids = resolve_scope(args.db, args.names)
    return _print({"entity_ids": ids, "transcriptions": len(scope_transcription_ids(args.db, ids))})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
