"""Archive scope (Track 2): directions by chats + auto-labelling + project slice.

**The problem** (grill 24.07.2026, confirmed by DB queries): the search scope was
single-layer - a category - and narrowed nothing. All 21 monitored Telegram chats
inherited ONE category, «Фонд Робота», so it collected 78% of the
categorised fragments; another more than half of the archive had no category at all and
dropped out of the search when the scope was on. The copilot during a call
mixed directions and went blind to part of the archive at once.

**The solution - two layers:**

1. **Coarse (this module, `apply_chat_categories`)**: the chat title already contains
   a direction («Nova Dance & Робота», «Ділова англійська & Робота»), so
   the labelling is deterministic and free - not manual work but a chat->direction
   correspondence table. TG ingest already inherits `category_id` from
   `tg_monitored_chats` (`app/blueprints/telegram.py:_resolve_category`), so
   after the pass new messages go into the right direction on their own.
2. **Fine (`resolve_scope`, used in `retrieval.search`)**: a slice by
   an entity-project and participants through `meeting_entities` - many-to-many, because
   one meeting honestly can be about three projects, and a 1:1 category loses that.

**The chat->direction map is NOT baked into the code**: it contains chat and people names, and
the repository is public. The module reads it from a local JSON (by default
`data/chat_categories.local.json`, in .gitignore) - the format is in `load_mapping`.

CLI:
    python -m app.services.scope apply-chats --dry-run
    python -m app.services.scope label-rest --dry-run   # k-NN for non-TG records
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from app.db.connection import get_db_connection
from app.services import categories

logger = logging.getLogger(__name__)

DEFAULT_MAPPING_PATH = "data/chat_categories.local.json"

# Minimum confidence to set a direction without a human. 0.6 was picked on
# real records: at 0.5 obvious errors leaked through the threshold
# («відео про штукатурку» -> «Фиби» with 0.526), at 0.6 they go to the Claude arbiter.
KNN_MIN_CONFIDENCE = 0.6

# So as not to spray the same warning on every record (216 identical lines
# in the log are noise, not diagnostics).
_claude_warned = False


# The name normaliser and the path parser live in one place - `categories`
# (`norm_name`/`name_keys`/`split_path`). Own copies here had already diverged from
# it (v47: collapsing repeated spaces existed only in `categories`).


# ============================================================
# Layer 1 - directions by Telegram chats
# ============================================================

def load_mapping(path: str = DEFAULT_MAPPING_PATH) -> list[dict]:
    """Chat->direction map from a local JSON.

    Format: {"chats": [{"chat_id": -100..., "title": "...", "category": "NOVA",
             "parent"?: "Фонд Робота", "tags"?: ["AI"]}, ...]}
    `title` is only for a readable diff, matching goes by `chat_id`.
    `parent` (contract C7, optional) - the direction under which `category` lives as
    a project; without it `category` stays a direction (as before). `tags`
    (optional) - tags that the whole chat history will inherit via `apply_chat_tags`.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    chats = data.get("chats") if isinstance(data, dict) else data
    if not isinstance(chats, list):
        raise ValueError(f"{path}: expected {{'chats': [...]}} or a list")
    out = []
    for row in chats:
        if row.get("chat_id") is None or not (row.get("category") or "").strip():
            raise ValueError(f"{path}: row without chat_id/category: {row}")
        parent = (row.get("parent") or "").strip() or None
        tags = [str(t).strip() for t in (row.get("tags") or []) if str(t).strip()]
        out.append({"chat_id": int(row["chat_id"]), "title": row.get("title") or "",
                    "category": row["category"].strip(), "parent": parent, "tags": tags})
    return out


def _find_direction(conn, name: str) -> Optional[dict]:
    """A direction (top-level node) with this name, or None.

    Looks EXACTLY among directions, not through `resolve_node`: among themselves they are
    unique even after v47, so a same-named project of a foreign direction must not
    make a chat-map row ambiguous."""
    key = categories.norm_name(name)
    for r in conn.execute("SELECT id, name, name_norm FROM categories "
                          "WHERE parent_id IS NULL").fetchall():
        if key in categories.name_keys(r["name"], r["name_norm"]):
            return categories.get_category(conn, r["id"])
    return None


def _find_child(conn, parent_id: int, name: str) -> Optional[dict]:
    """A project with this name EXACTLY under this parent, or None."""
    key = categories.norm_name(name)
    for r in conn.execute("SELECT id, name, name_norm FROM categories WHERE parent_id = ?",
                          (int(parent_id),)).fetchall():
        if key in categories.name_keys(r["name"], r["name_norm"]):
            return {"id": r["id"], "parent_id": int(parent_id)}
    return None


def _row_key(row: dict) -> tuple[str, str]:
    """Key of a chat-map row - a **pair** `(norm(parent), norm(category))`.

    A bare `category` cannot be the key since v47: two rows
    `{"Маркетинг", parent: "Фонд Робота"}` and `{"Маркетинг", parent:
    "Фріланс"}` are TWO different nodes, and a shared cache by name would merge them into
    one and bind both chats to the first (the second one's history would silently go to a
    foreign direction)."""
    return (categories.norm_name(row.get("parent") or ""),
            categories.norm_name(row["category"]))


def _target_path(row: dict) -> str:
    """Where a map row aims: "Parent -> Category" or a bare name."""
    parent = (row.get("parent") or "").strip()
    return f"{parent} {categories.PATH_SEP} {row['category']}" if parent else row["category"]


def _create_node(conn, created: list, name: str,
                 parent: Optional[tuple[int, str]] = None) -> int:
    """Create a node in the pass transaction and record the creation in `created`.

    `categories_created` is a log of the ACTUAL creations of this pass, not a
    separately derived plan: `--dry-run` runs the same code and rolls back,
    so the preview cannot diverge from the real run (story 26 - three rounds
    in a row a separate plan branch diverged, first on the `namesake` note, then on the
    row order). `namesake` - paths of nodes that already carry this name
    elsewhere (including ones created earlier in the same pass): not an error (namesakes
    are allowed, ADR §E), but exactly what a person must see BEFORE a run on a
    live archive."""
    parent_id, parent_name = parent if parent else (None, None)
    key = categories.norm_name(name)
    namesake = [categories.node_path(conn, r["id"]) for r in conn.execute(
        "SELECT id, name, name_norm FROM categories").fetchall()
        if key in categories.name_keys(r["name"], r["name_norm"])]
    node_id = categories.create_category(conn, name, parent_id=parent_id,
                                         commit=False)["id"]
    created.append({
        "path": f"{parent_name} {categories.PATH_SEP} {name}" if parent_name else name,
        "namesake": namesake,
        "note": ("namesake exists: " + ", ".join(f"«{p}»" for p in namesake)
                 if namesake else None)})
    return node_id


def _ensure_categories(conn, mapping: list[dict], *, ambiguous: dict, created: list
                       ) -> dict[tuple[str, str], Optional[int]]:
    """Pair `(parent, category)` -> node id (contract **C7**).

    Writes into the caller's transaction (`commit=False`) - it is ended by
    `apply_chat_categories`: `commit()` or, in `--dry-run`, `rollback()`.

    The resolve order is **deterministic and does not depend on the order of map rows**:

    1. first all rows with `parent`: the direction is created if needed,
       `category` is looked up **among the children of exactly this parent** and created
       there if missing. Such a row never reparents a foreign node and
       is never `ambiguous` - it addresses the node by the pair;
    2. then bare rows - via `categories.resolve_node` over the tree that already
       includes the creations of step 1. So `[{Маркетинг, parent: Фонд}, {Маркетинг}]`
       in any order gives the same result: the bare row sees
       "Фонд -> Маркетинг" as the only match. No match - a direction is created;
       several (and no same-named direction) - the row is skipped (`None` + an entry in
       `ambiguous[_row_key] = {category, candidates}`), because "the first match" here
       would mean assigning the whole chat history to a randomly chosen direction.

    The key of the resolve, the cache and `ambiguous` is the PAIR (`_row_key`), not the bare
    `category`: two rows with one name under different parents are two nodes,
    and a row that resolved cleanly by the pair is not rejected because of a bare namesake.
    """
    resolved: dict[tuple[str, str], Optional[int]] = {}
    parent_ids: dict[str, int] = {}

    # 1) Rows with parent.
    for m in mapping:
        if not m.get("parent"):
            continue
        key = _row_key(m)
        if key in resolved:
            continue
        pkey, name, parent_name = key[0], m["category"], m["parent"]
        if pkey not in parent_ids:
            node = _find_direction(conn, parent_name)
            parent_ids[pkey] = (node["id"] if node else
                                _create_node(conn, created, parent_name))
        pid = parent_ids[pkey]
        child = _find_child(conn, pid, name)
        resolved[key] = (child["id"] if child else
                         _create_node(conn, created, name,
                                      (pid, categories.get_category(conn, pid)["name"])))

    # 2) Bare rows - over the tree that already contains the creations of step 1.
    for m in mapping:
        if m.get("parent"):
            continue
        key = _row_key(m)
        if key in resolved:
            continue
        name = m["category"]
        try:
            match = categories.resolve_node(conn, name)
        except categories.CategoryError as exc:
            if exc.code != "ambiguous":
                raise
            ambiguous[key] = {"category": name, "candidates": list(exc.candidates)}
            resolved[key] = None
            continue
        resolved[key] = match if match is not None else _create_node(conn, created, name)
    return resolved


def apply_chat_categories(db_path: str, *, mapping_path: str = DEFAULT_MAPPING_PATH,
                          dry_run: bool = False) -> dict:
    """Distribute Telegram chats over directions/projects and tags (contract **C7**):
    create the missing taxonomy, bind the chat in `tg_monitored_chats`,
    re-label its history via `categories.set_category` and apply
    the chat tags via `categories.apply_chat_tags`. After `commit` - a targeted
    re-embed of all records whose prefix changed.

    **One write path (story 26).** `dry_run` has NO branch of its own: the pass
    runs exactly the same code on the same connection (writers with
    `commit=False`) and ends with `rollback()` instead of `commit()`, without
    scheduling a re-embed. Everything the preview prints is what the pass did, so the
    `stats` of dry-run and real run are the same (except `reembed_scheduled`).

    Idempotent: a repeated pass changes nothing (and does not call re-embed).
    Records of other sources (calls/files) are NOT touched - they have their own labelling
    (`label_uncategorized`).
    """
    mapping = load_mapping(mapping_path)
    stats = {"chats": len(mapping), "categories_created": [], "chats_bound": 0,
             "chats_inserted": 0, "records_recategorized": 0, "tags_applied": 0,
             "ambiguous": [], "per_chat": []}
    changed_ids: set[int] = set()
    ambiguous: dict[tuple[str, str], dict] = {}

    with get_db_connection(db_path) as conn:
        created: list[dict] = []
        cat_ids = _ensure_categories(conn, mapping, ambiguous=ambiguous, created=created)
        stats["ambiguous"] = [ambiguous[k] for k in sorted(ambiguous)]
        stats["categories_created"] = created

        for m in mapping:
            key = _row_key(m)
            cid, cat_id, tags = m["chat_id"], cat_ids.get(key), m.get("tags") or []
            row = conn.execute(
                "SELECT enabled, category_id FROM tg_monitored_chats WHERE chat_id = ?",
                (cid,)).fetchone()
            # The note names the TARGET PATH, not the bare name: after v47 «Акмекорп»
            # in the preview is indistinguishable from «Фонд Робота → Акмекорп», and those are two
            # different nodes and two different places the chat history would go to.
            target = (categories.node_path(conn, cat_id) if cat_id
                      else _target_path(m))
            row_stat = {
                "title": m["title"], "category": m["category"], "parent": m.get("parent"),
                "tags": tags, "monitored": bool(row), "records_to_move": 0,
                "note": f"target: «{target}»"}
            stats["per_chat"].append(row_stat)
            if key in ambiguous:
                # Neither in dry-run nor in a real pass: otherwise `cat_id=None`
                # would strip from the chat a direction that a person set by hand.
                row_stat["note"] = (
                    "ambiguous, specify parent: "
                    + ", ".join(f"«{p}»" for p in ambiguous[key]["candidates"]))
                continue

            if row is None:
                # The chat is not monitored (history exists, listening is off) - we create
                # a row with enabled=0 so that enabling it later inherits the direction
                # instead of returning the records to "no category".
                conn.execute(
                    "INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                    "category_id) VALUES (?, ?, 'group', 0, ?)", (cid, m["title"], cat_id))
                stats["chats_inserted"] += 1
            elif row["category_id"] != cat_id:
                conn.execute("UPDATE tg_monitored_chats SET category_id = ? WHERE chat_id = ?",
                             (cat_id, cid))
                stats["chats_bound"] += 1

            record_ids = [r[0] for r in conn.execute(
                "SELECT id FROM transcriptions WHERE source_type = 'telegram' "
                "AND tg_chat_id = ? AND deleted_at IS NULL", (cid,)).fetchall()]
            changed_cat = categories.set_category(conn, record_ids, cat_id, commit=False)
            changed_ids.update(changed_cat)
            row_stat["records_to_move"] = len(changed_cat)
            stats["records_recategorized"] += len(changed_cat)

            changed_tags = categories.apply_chat_tags(conn, cid, tags, commit=False)
            changed_ids.update(changed_tags)
            stats["tags_applied"] += len(changed_tags)
        # The only dry-run fork is the end of the transaction (and the re-embed below).
        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    if not dry_run and changed_ids:
        from app.services import reembed
        reembed.schedule_records_reembed(sorted(changed_ids), db_path=db_path,
                                         reason="apply-chats")
    stats["reembed_scheduled"] = len(changed_ids) if not dry_run else 0
    logger.info("apply_chat_categories: chats=%(chats)s bound=%(chats_bound)s "
                "inserted=%(chats_inserted)s records=%(records_recategorized)s "
                "tags=%(tags_applied)s", stats)
    return stats


# Tokens too generic for recognising a direction by a record title: they are in
# almost every title («фонд», «робота») and would give false hits.
_TITLE_STOPWORDS = {"фонд", "робота", "team", "чат", "запис", "call", "дзвінок"}


def _title_matchers(conn) -> list[tuple[int, str, "re.Pattern"]]:
    """Regexes for recognising a direction right in a record title.

    The owner's record titles already contain the direction («NOVA я та Микола по
    маркетингу»), and this is a deterministic signal, stronger than k-NN.

    v47: a name that sits on more than one node («Маркетинг» under two
    directions) does not reach the matcher at all. A deterministic signal here is a
    name that points to EXACTLY one node; with two candidates the choice would be
    a coin flip, and a coin flip is never stronger than k-NN.
    """
    rows = conn.execute("SELECT id, name FROM categories").fetchall()
    seen: dict[str, int] = defaultdict(int)
    for r in rows:
        seen[categories.norm_name(r["name"])] += 1

    out = []
    for r in rows:
        name = (r["name"] or "").strip()
        if seen[categories.norm_name(name)] > 1:
            continue
        tokens = [t for t in re.split(r"[\s/|,]+", name) if len(t) >= 3
                  and t.lower() not in _TITLE_STOPWORDS]
        if not tokens:
            continue
        alts = [re.escape(name)] + [re.escape(t) for t in tokens]
        out.append((r["id"], name,
                    re.compile(r"(?<![\w])(" + "|".join(alts) + r")(?![\w])", re.IGNORECASE)))
    return out


def _prior_correct(candidates: list[dict], sizes: dict[int, int]) -> list[dict]:
    """Remove the k-NN skew toward the largest category.

    «Фонд Робота» has more neighbours simply because it has several times more records,
    so voting by raw scores pulls everything there (verified: a call
    with NOVA in the title got «Фонд» with 0.625).

    The exponent is deliberately soft (0.25, not 0.5): with a square root the skew flipped to
    the other side - tiny categories (5 records) started winning foreign topics
    (a video about plastering fell into «Фиби»). The task of the correction is to remove the
    dominance of the giant, not to make the smallest one the winner.
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


def _category_paths(conn) -> list[tuple[int, str]]:
    """All tree nodes as `(id, "Direction -> Project"|"Direction")` (contract C7).

    A candidate project is allowed on a par with a direction - the claim «Акмекорп» is more precise
    than the coarse «Фонд Робота», if the archive is already laid out into projects.
    """
    out = []
    by_id = {}
    for node in categories.list_tree(conn):
        by_id[node["id"]] = node
        if node["level"] == "project":
            parent = by_id.get(node["parent_id"])
            label = f"{parent['name']} → {node['name']}" if parent else node["name"]
        else:
            label = node["name"]
        out.append((node["id"], label))
    return out


def _map_claude_answer(raw: str, cats: list[tuple[int, str]]
                       ) -> tuple[Optional[int], str, list]:
    """Arbiter answer -> `(node id | None, reason, candidate paths)`.

    Three forms, in decreasing order of unambiguity: a numeric id from the list, a full
    path «Direction -> Project», a bare name. A bare name is accepted ONLY if it
    points to exactly one node: after v47 «Маркетинг» exists in two directions, and
    "the first match" here is a silently assigned foreign direction (the reason is
    `ambiguous`, it is also a counter in stats, and the paths go into the pass output).
    `NONE`/unclear -> `unknown`.

    The id form only when the WHOLE answer is a number. Earlier the first digit
    anywhere in the text was taken, so a name with a number («Проєкт 2026») was read as id 2026:
    among the 29 current names there are no digits, but the first such node would silently break
    the arbiter. The path is parsed by `categories.split_path` - the same parser as in
    the resolver, so «Фріланс→Маркетинг» and «Фріланс → Маркетинг» are the same."""
    answer = str(raw or "").strip()
    ids = {cid for cid, _ in cats}
    try:
        cid = int(answer)
    except ValueError:
        pass
    else:
        return (cid, "id", []) if cid in ids else (None, "unknown", [])

    key = categories.split_path(answer)
    if not key:
        return None, "unknown", []
    by_path = [cid for cid, label in cats if categories.split_path(label) == key]
    if len(by_path) == 1:
        return by_path[0], "path", []
    leaves = [(cid, label) for cid, label in cats
              if categories.norm_name(label.split(categories.PATH_SEP)[-1])
              == categories.norm_name(answer)]
    if len(leaves) == 1:
        return leaves[0][0], "name", []
    if leaves:
        return None, "ambiguous", [label for _, label in leaves]
    return None, "unknown", []


def _ask_claude_category(title: str, excerpt: str, cats: list[tuple[int, str]],
                         stats: Optional[dict] = None) -> Optional[int]:
    """The last line of defence for records where neither the title nor k-NN gave confidence.

    A cheap model (Haiku) sees the title, the start of the text and the list of directions/
    projects (`_category_paths`, as a "Direction -> Project" path) - for a heterogeneous
    tail (YouTube videos, documents, one-off calls) this is more accurate than neighbour
    voting in a skewed corpus. Returns None if the model is not sure:
    "not labelled" is better than "labelled wrongly".

    The model is asked to answer with an id, but readily answers with a name - so the
    answer is mapped by `_map_claude_answer` (id -> path -> bare name with a SINGLE
    match). An ambiguous bare name assigns nothing and is counted in
    `stats['claude_ambiguous']` if `stats` was passed.
    """
    from app.services import models, text_polishing

    listing = "\n".join(f"{cid}. {name}" for cid, name in cats)
    prompt = (
        "Ось перелік напрямків/проєктів архіву:\n" + listing +
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
            logger.warning("scope: Claude classification unavailable (%s) - the rest is left to a human", exc)
            _claude_warned = True
        return None
    cid, reason, candidates = _map_claude_answer(raw, cats)
    if reason == "ambiguous":
        logger.info("scope: the arbiter named «%s» - such a name exists in several directions (%s), "
                    "leaving unassigned", raw, ", ".join(candidates))
        if stats is not None:
            stats["claude_ambiguous"] = stats.get("claude_ambiguous", 0) + 1
            seen = stats.setdefault("claude_ambiguous_paths", [])
            for label in candidates:
                if label not in seen:
                    seen.append(label)
    return cid


def label_uncategorized(db_path: str, *, min_confidence: float = KNN_MIN_CONFIDENCE,
                        limit: int = 500, dry_run: bool = False, use_claude: bool = False,
                        source_types: Optional[tuple] = None) -> dict:
    """k-NN auto-labelling of NON-Telegram records without a direction.

    Reuses `categorize.suggest_category` (the same embeddings as the search) -
    we load no new model. Sets only confident suggestions; the rest
    is left to a human, because a meeting quietly put in the wrong direction is worse than
    an unplaced one: it vanishes from the scope in which it will be looked for.
    """
    from app.services import categorize

    stats = {"scanned": 0, "labeled": 0, "by_title": 0, "by_knn": 0, "by_claude": 0,
             "low_confidence": 0, "no_neighbors": 0, "claude_ambiguous": 0,
             "claude_ambiguous_paths": [], "samples": []}
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
                cats = _category_paths(conn)
                row = conn.execute(
                    "SELECT COALESCE(summary_json, substr(COALESCE(polished_text, "
                    "transcript_text), 1, 1500)) AS body FROM transcriptions WHERE id = ?",
                    (r["id"],)).fetchone()
            picked = _ask_claude_category(title, (row["body"] if row else "") or "", cats,
                                          stats)
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
    changed_ids: set[int] = set()
    if updates and not dry_run:
        by_cat: dict[int, list[int]] = defaultdict(list)
        for cat_id, tid in updates:
            by_cat[cat_id].append(tid)
        with get_db_connection(db_path) as conn:
            for cat_id, ids in by_cat.items():
                changed_ids.update(categories.set_category(conn, ids, cat_id))

    if not dry_run and changed_ids:
        from app.services import reembed
        reembed.schedule_records_reembed(sorted(changed_ids), db_path=db_path,
                                         reason="label-rest")
    stats["reembed_scheduled"] = len(changed_ids) if not dry_run else 0
    stats["labeled"] = len(updates)
    logger.info("label_uncategorized: scanned=%(scanned)s labeled=%(labeled)s "
                "low_conf=%(low_confidence)s", stats)
    return stats


# ============================================================
# Layer 2 - project/participant slice
# ============================================================

def resolve_scope(db_path: str, names: list[str] | str,
                  types: tuple = ("project", "org", "person")) -> list[int]:
    """Project/person names -> entity ids (canonical names + aliases).

    Accepts a string or a list. The comparison is case-insensitive by the normalised
    name - the same key that enrichment uses for dedup.
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
    """Records that mention the given entities (graph only, no text layer)."""
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
    """Names for the text search: what was asked + canonical names and aliases."""
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
    """Full slice by a project/person: **graph ∪ text mention**.

    Why not graph only: `meeting_entities` exists only for enriched records
    (currently ~a noticeable share of the archive), so a slice "by entities only" silently dropped the rest -
    the same blindness that categories had, just on another axis. Verified on the
    golden set: the expected source of a case (the document «Сенсети, страница Базовая
    инфраструктура») has no link in the graph and vanished on narrowing.

    So we add a second path: an FTS5 mention of the name (or alias) in the chunk text +
    a match in the record title. This is wider than the graph, but honestly wider - and it does not hide records
    that simply did not go through enrichment.
    """
    names = [n.strip() for n in (re.split(r"[,;]", project) if isinstance(project, str)
                                 else list(project)) if str(n).strip()]
    if not names:
        return []
    entity_ids = resolve_scope(db_path, names)
    tids = set(scope_transcription_ids(db_path, entity_ids))

    # A name may coincide with a tree node - a DIRECTION («Datalink», «Ділова
    # English», «HoReCa») or a project («Акмекорп»). For a direction the slice must
    # include the WHOLE SUBTREE (its projects too), otherwise the Latin name of the
    # direction gave empty where the graph/text holds Cyrillic
    # («Сенсети»), and projects inside a direction dropped out of the slice altogether.
    # `filter_sql` is the same source of two-level semantics as in the
    # UI/MCP filters (contract C2): a direction id expands to itself and its
    # projects, a project (leaf) id gives only itself.
    # We compare by name_norm (casefold from Python), NOT through SQLite LOWER():
    # the built-in LOWER folds only ASCII, so «Юридичне» did not match
    # itself - the same trap that gave rise to name_norm in migration v20.
    # v47: a name may sit on SEVERAL nodes («Маркетинг» in two directions).
    # Then `resolve_node` raises `CategoryError('ambiguous')` with a list of paths,
    # and the slice does NOT catch this exception: merging the subtrees of namesakes would mean silently
    # answering a different question (the chunk prefix separates them), while taking the
    # first one would attribute the answer to a foreign direction. The asker clarifies the full path
    # «Direction -> Project» - `resolve_node` parses it exactly.
    with get_db_connection(db_path) as conn:
        node_ids = [nid for nid in (categories.resolve_node(conn, n) for n in names)
                    if nid is not None]
        for node_id in dict.fromkeys(node_ids):
            frag, params = categories.filter_sql("t.category_id", node_id)
            rows = conn.execute(
                f"SELECT t.id AS tid FROM transcriptions t WHERE t.deleted_at IS NULL{frag}",
                params).fetchall()
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
            except Exception as exc:      # exotic tokens break the FTS syntax
                logger.debug("scope: FTS layer skipped (%s)", exc)
            rows = conn.execute(
                f"SELECT id AS tid FROM transcriptions t WHERE ({like_sql}) "
                f"AND t.deleted_at IS NULL", [f"%{v}%" for v in variants]).fetchall()
            tids.update(r["tid"] for r in rows)
    return sorted(_expand_to_threads(db_path, tids))


def _expand_to_threads(db_path: str, tids: set[int]) -> set[int]:
    """Expand the slice to WHOLE conversation threads (Wave 4.5.1a).

    A project name in chat is spoken once, while the decision about it is in neighbouring
    replies where the name is gone («а скільки там?», «ок, беремо»). So a slice
    assembled by mentions returned separate replies instead of conversations: on live data
    «Ковальчука» was mentioned 23 times inside fund chats, and exactly these 23 one-liners
    were all that `project=` saw.

    A thread is a natural boundary of expansion: it is already limited by a burst and a topic, so
    this is not sprawl across the whole chat (sprawl across the chat would be a return to
    what the wave is curing: chat != topic)."""
    if not tids:
        return tids
    ids = list(tids)
    out = set(tids)
    with get_db_connection(db_path) as conn:
        for i in range(0, len(ids), 500):     # SQLite limits the number of parameters
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
    # The CLI starts outside app.py, which usually loads .env - without this the
    # Claude arbiter "does not see" a key lying in the file next to it.
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except ImportError:
        pass

    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(prog="scope", description="Archive directions and slices (Track 2).")
    p.add_argument("--db", default=default_db)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Write nothing to the DB")

    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("apply-chats", parents=[common],
                       help="Distribute TG chats over directions by the local map")
    a.add_argument("--file", default=DEFAULT_MAPPING_PATH)
    l = sub.add_parser("label-rest", parents=[common],
                       help="k-NN auto-labelling of non-TG records without a direction")
    l.add_argument("--min-confidence", type=float, default=KNN_MIN_CONFIDENCE)
    l.add_argument("--limit", type=int, default=500)
    l.add_argument("--claude", action="store_true",
                   help="Haiku arbitration for records where title and k-NN gave no confidence")
    s = sub.add_parser("scope", help="Show entity ids and the number of records in the slice")
    s.add_argument("names", help="Names separated by commas: «Acmecorp,Ковальчука»")

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
