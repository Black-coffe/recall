"""Archive taxonomy: the direction tree (direction -> projects) plus the tag dictionary.

Spec `directions-projects-tags`, story 01 (tracer), contract **C2**;
decisions in ADR-023 (§A tree, §B tags, §E limits).

**Why this module exists.** A direction is filtered in 15 places across two
processes (Flask, stdio MCP), and once the second level appeared ("direction =
itself + its projects") each of them would have had to write the same subquery
by hand. This is the single source of that SQL (`filter_sql`), the single name
normaliser (`norm_name`) and all the `category_id`/`record_tags` writers, which
return **the ids of the changed records** - the caller passes exactly those to
`reembed.schedule_records_reembed`, because direction and tags go into the
chunk context prefix.

**What the module must not do.** Pull in `torch`, `anthropic` or `flask` -
`mcp_server.py` (stdio) imports it, and a heavy module-level import there hangs
the server forever (memory note `mcp-stdio-no-heavy-models`). The ceiling is
stdlib plus `app.db.connection` (the caller opens the connection, so for now it
is not needed either). `reembed` is not imported here either: scheduling a
re-embed is the job of the caller, who knows its `db_path`.

**Transactions.** The writer functions call `conn.commit()` themselves - blueprints
and the CLI work with short `get_db_connection(db_path)` connections. The writers
on the chat-map path (`create_category`, `set_category`, `apply_chat_tags`,
`ensure_tag`) take a `commit=False` key: then the caller ends the transaction -
`scope.apply_chat_categories` does `commit()` in a real run and
`rollback()` in `--dry-run` (one write path, story 26). None of them
calls `executescript` - it commits implicitly and would break the rollback.

**Case and Cyrillic.** The uniqueness key is `norm_name` (NFKC -> strip -> collapse
spaces -> casefold), computed in Python, not SQLite `LOWER()`: the latter folds
only ASCII, so "Фонд" and "фонд" passed as different (memory note
`plan-dictated-sql-ignored-own-memory`, migration v20).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date as _date

logger = logging.getLogger(__name__)


def _t(key: str, **vars) -> str:
    """Catalog text for user-facing errors. Imported lazily: `app.core.i18n` pulls in
    flask, which the stdio MCP process must not load at module level."""
    from app.core.i18n import tr
    return tr(key, **vars)

#: Sentinel "field not passed" - tells "leave alone" from "set NULL"
#: (the same pattern as `record_meta._Unset`).
UNSET = object()

#: Tables that hold `category_id` without an FK (integrity lives here, in app code).
#: `transcriptions` comes first: its ids are the ones returned for re-embed.
_CATEGORY_REF_TABLES = ("transcriptions", "audio_downloads",
                        "tg_monitored_chats", "copilot_sessions")

#: Allowed tag sources on a record (ADR §B): manual, Claude suggestion,
#: inherited from a TG chat. `source` is needed for detaching - removing a tag
#: from a chat only drops its own rows, the manual one stays.
TAG_SOURCES = ("manual", "claude", "chat", "mcp")

#: Tag name length ceiling (ADR §B) - checked by the HTTP layer, only the constant lives here.
TAG_NAME_MAX = 40

#: Separator of a node's full path ("Direction -> Project"). The same symbol is already
#: shown by the arbiter `scope._category_paths`, the chunk context prefix and the UI -
#: here it also becomes an INPUT (`resolve_node`), because since v47 a bare project name
#: no longer addresses a node unambiguously. The full path is the CANONICAL text name
#: of a node: it is what `path` of every `list_tree` item returns and what is expected back.
PATH_SEP = "→"


class CategoryError(ValueError):
    """Expected error of working with the tree/dictionary.

    `.code` - machine code for the HTTP layer: `not_found` (404 - the node from the URL),
    `bad_parent`, `two_levels`, `self_parent`, `empty`, `bad_name` (400 - a malformed
    request body; `bad_name` - an empty/too long tag name or `PATH_SEP` in a node
    name), `duplicate` (409), `has_projects` (409).
    `.projects` is filled only for `has_projects` - the list of nodes that
    prevent deleting/merging a direction (they must be moved by hand first).
    `.conflict` - only for `duplicate`: `{"id", "path"}` of the node that already took
    the name. Since v47 a name is unique only among siblings, so a client cannot show
    "taken" without an address ("in which direction").
    `.candidates` - only for `ambiguous` (409): full paths of the nodes that
    carry the same bare name. Without the list "ambiguous" is a dead end:
    neither a person nor an agent knows WHAT to pass instead of the name.
    """

    def __init__(self, code: str, message: str = "", *, projects: list | None = None,
                 conflict: dict | None = None, candidates: list | None = None):
        super().__init__(message or code)
        self.code = code
        self.projects = projects or []
        self.conflict = conflict or {}
        self.candidates = candidates or []


# ============================================================
# Normalisation and filter parsing
# ============================================================

def norm_name(name: str) -> str:
    """The single uniqueness key of a name (both direction and tag).

    NFKC -> strip -> collapse repeated spaces -> casefold. NFKC maps
    compatibility forms (e.g. full-width Latin letters) to canonical ones so that
    visually identical names do not live as two rows; casefold folds case
    for any alphabet. For the 29 existing directions the result matches the
    old `memory._norm_cat`, so migration v45 does NOT recompute `name_norm`.
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    return " ".join(text.strip().split()).casefold()


def clean_name(name: str) -> str:
    """Name as we will store it: strip + collapse spaces, the user's case."""
    return " ".join(unicodedata.normalize("NFKC", str(name or "")).strip().split())


def parse_filter(raw) -> "int | str | None":
    """Value of the `category_id` query parameter -> value for `filter_sql`.

    `''`/`None`/`'all'` -> `None` (all directions); `'none'` -> `'none'` (records without a
    direction); digits -> `int` (id of any node - a direction or a project).
    Anything else is `ValueError`: a silent "did not understand -> show everything" would mean
    the filter silently leaks (and the `filter_leak` metric would lie).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(_t("svc_taxonomy.err.bad_category_id", raw=repr(raw)))
    if isinstance(raw, int):
        return int(raw)
    s = str(raw).strip().lower()
    if not s or s == "all":
        return None
    if s == "none":
        return "none"
    # `isdecimal`, not `isdigit`: "²"/"½" pass `isdigit()`, but `int()` on
    # them fails with a raw "invalid literal for int()" - an internal text instead of
    # ours. `isdecimal()` lets through exactly what `int()` can read.
    if s.isdecimal():
        return int(s)
    raise ValueError(_t("svc_taxonomy.err.bad_category_id", raw=repr(raw)))


def filter_sql(column: str, value) -> tuple[str, list]:
    """WHERE fragment for the direction filter. Always starts with `" AND "`.

    `column` - any SQL expression with the direction column (`t.category_id`,
    `cs.category_id`, the correlated `COALESCE(...)` of the Media library - all the same).

    - `None` -> `("", [])` - no filter;
    - `'none'` -> `IS NULL` - records without a direction;
    - `int` -> `IN (SELECT id FROM categories WHERE id = ? OR parent_id = ?)` -
      **this is the whole semantics of the two levels**: a direction id expands to itself
      and its projects, a project (leaf) id gives only itself. The tree has exactly
      two levels (ADR §E), so a recursive CTE is not needed.
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
    """`'AI, investors'` or `['AI', 'investors']` -> list of tag names.

    Empty ones are dropped, duplicates collapse by `norm_name` (the first spelling
    wins). Order is kept - it goes into the error message and the UI.
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
    """WHERE fragment for the tag filter (OR - "any of").

    `tid_column` - expression with the transcript id (tags attach only to
    `transcriptions`). An unknown name simply matches nothing - the filter
    narrows the result to empty instead of being "ignored" (otherwise someone else's data would show).
    """
    names = list(names or [])
    if not names:
        return "", []
    keys = [norm_name(n) for n in names]
    ph = ",".join("?" * len(keys))
    return (f" AND {tid_column} IN (SELECT rt.transcription_id FROM record_tags rt "
            f"JOIN tags tg ON tg.id = rt.tag_id WHERE tg.name_norm IN ({ph}))", keys)


def parse_date(raw) -> "str | None":
    """`'YYYY-MM-DD'` -> the same string; empty -> None; anything else -> `ValueError`.

    The error text is user-facing and in the same style as `parse_filter`:
    it is seen verbatim both by a person in an SPA toast and by an agent in the `error` field
    of an MCP tool. The raw `Invalid isoformat string: 'not-a-date'` from
    `date.fromisoformat` told neither in the user's language nor what exactly is expected
    - so parsing `since`/`until` across Recall goes only through this function
    (no blueprint and no tool parses a date itself)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        _date.fromisoformat(s)
    except ValueError:
        raise ValueError(_t("svc_taxonomy.err.bad_date", s=s)) from None
    return s


def date_sql(date_expr: str, since=None, until=None) -> tuple[str, list]:
    """WHERE fragment for a period. Both bounds are **inclusive**, granularity is a day.

    `date_expr` is set by the caller, because the "record date" differs in each table
    (`COALESCE(t.meeting_date, t.created_at)` for transcripts,
    `a.created_at` for audio). No Recall layer parses "last week" -
    absolute ISO dates come from outside (decision A11 + ADR §D).
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
# Reading the tree
# ============================================================

def _node(conn, cid: int) -> "dict | None":
    """A tree row as a dict + `path` - the canonical text name of the node (v47).

    `path` is here, not a separate call, because since v47 a bare name no longer
    addresses a node: everything that hands a node outward (HTTP, MCP, the duplicate message)
    must carry the address, otherwise the client has nothing to show or pass back.
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
        raise CategoryError("not_found", _t("svc_taxonomy.err.direction_not_found", cid=cid))
    return node


def child_ids(conn, cid: int) -> list[int]:
    """Ids of the projects inside a direction (empty for a project)."""
    return [r[0] for r in conn.execute(
        "SELECT id FROM categories WHERE parent_id = ? "
        "ORDER BY sort_order, name_norm, id", (int(cid),)).fetchall()]


def subtree_ids(conn, category_id: int) -> list[int]:
    """`[the node itself, *its projects]`. A nonexistent id -> `[]`.

    The same expansion as in `filter_sql`, but as a list - for counters,
    writers and tests that need ids, not a SQL fragment.
    """
    cid = int(category_id)
    if _node(conn, cid) is None:
        return []
    return [cid, *child_ids(conn, cid)]


def node_path(conn, cid: int) -> str:
    """Full address of a node: `"Direction -> Project"` or `"Direction"`.

    The same form that the arbiter shows and `resolve_node` accepts - one
    spelling of a path across the whole project, so that "taken", "find" and "ambiguous"
    speak the same language."""
    node = _node(conn, cid)
    return node["path"] if node else ""


def name_keys(name, name_norm=None) -> set:
    """Keys by which a table row is recognised by name.

    Two, not one: `name_norm` of old rows was computed by the v20 backfill
    (`strip().casefold()`, no NFKC and no space collapsing), so for them
    `norm_name(name)` and the stored `name_norm` may diverge. We compare with
    both - otherwise a legacy direction would not be found by its own name.

    Public (v47): the chat map needs the same set of keys
    (`scope._find_direction`/`_find_child`), and a second copy of it had already
    diverged from this one - one helper for the whole project."""
    return {norm_name(name), (name_norm or "")} - {""}


def split_path(text) -> list[str]:
    """Text -> parts of the canonical path, normalised by `norm_name`.

    "Фонд Робота → Акмекорп" -> `['фонд робота', 'акмекорп']`; a bare name ->
    `['маркетинг']`; an empty part or **three and more** parts -> `[]` (such
    text addresses nothing: there is no third level in the tree). Spaces around
    `PATH_SEP` are optional.

    The single "A -> B" parser for the whole project (v47): `_match_nodes`, the chat map and
    the Claude arbiter read a path with it, otherwise "Фріланс→Маркетинг" and
    "Фріланс → Маркетинг" diverge exactly where they are compared.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [norm_name(p) for p in raw.split(PATH_SEP)]
    if len(parts) > 2 or not all(parts):
        return []
    return parts


def _match_nodes(conn, text) -> list[int]:
    """Text -> ids of all nodes it describes (empty if none).

    Two input forms: the full path `"Direction -> Project"` (separator `PATH_SEP`,
    spaces around optional) - exactly that node; a bare name - all nodes with
    this `norm_name` (a direction and same-named projects of different directions).
    The public entry is `resolve_node`: nobody makes the choice "one of several".
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
    """Text -> id of EXACTLY one tree node; `None` if there is none.

    The single text-to-node resolver for the whole project (HTTP, MCP, slice, chat
    map, arbiter). The canonical form is the full path `"Direction -> Project"`; a bare
    name stays an allowed input and works while it is unique.

    **Precedence (v47, wave 7):** first the exact canonical path, then the bare
    name. The canonical path of a direction is its BARE name, so "Маркетинг" with a
    direction "Маркетинг" next to a foreign project "Фріланс → Маркетинг" gives the
    direction, not `ambiguous`. Without this rule a same-named direction would be
    unreachable by text forever: its own `path` from `list_tree`
    would return the same `ambiguous`, and `candidates` would loop onto themselves.
    Invariant: `resolve_node(node["path"]) == node["id"]` for EVERY node, and
    every row of `.candidates` resolves back to its own node.

    Several matches - `CategoryError('ambiguous')` with `.candidates` (full paths).
    An exception, not "the first match" and not a silent merge of subtrees: both silently
    substitute what was asked (owner's decision 22.09.2026). Vectors and FTS
    already tell same-named nodes apart by construction - the path sits in the chunk
    context prefix - so "merging two Маркетинги" would mix exactly what the
    prefix separated.
    """
    ids = _match_nodes(conn, text)
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    if len(split_path(text)) == 1:
        # A bare name = the canonical path of a DIRECTION (a two-part text is already
        # parsed as a path in `_match_nodes`).
        ph = ",".join("?" * len(ids))
        tops = [r[0] for r in conn.execute(
            f"SELECT id FROM categories WHERE parent_id IS NULL AND id IN ({ph})",
            ids).fetchall()]
        if len(tops) == 1:
            return tops[0]
    paths = [node_path(conn, i) for i in ids]
    raise CategoryError(
        "ambiguous",
        _t("svc_taxonomy.err.ambiguous", text=str(text).strip(),
           paths=", ".join(f"«{p}»" for p in paths)),
        candidates=paths)


def list_tree(conn) -> list[dict]:
    """Flat list in tree order: a direction, right after it - its projects.

    `items` - the node's own live records, `items_total` - together with projects
    (for a direction) or the same `items` (for a project). Sorting is
    `(sort_order, name_norm)` on each level: it is what the user sees in
    selects, so it must be stable and the same everywhere.

    `path` (v47) - the canonical node name: "Direction -> Project" or "Direction".
    It is seen by a person in the select, by an agent in `list_categories`, and it is exactly what they
    pass back when a bare name sits on two nodes.
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

    # A node whose parent has vanished (desync) is shown as a direction - otherwise it
    # would drop out of the tree and become invisible in the UI forever.
    for node in nodes.values():
        if node["parent_id"] is not None and node["parent_id"] not in nodes:
            logger.warning("[categories] node %s references nonexistent parent %s - "
                           "showing as a direction", node["id"], node["parent_id"])
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
# Tree writers
# ============================================================

def _assert_parent(conn, cid: "int | None", parent_id: "int | None") -> None:
    """Check "exactly two levels" (ADR §E) - this is the entire business logic of the tree."""
    if parent_id is None:
        return
    parent_id = int(parent_id)
    if cid is not None and parent_id == int(cid):
        raise CategoryError("self_parent", _t("svc_taxonomy.err.self_parent"))
    parent = _node(conn, parent_id)
    if parent is None:
        # Not `not_found`: 404 stays for the node from the URL, while a nonexistent parent is
        # a malformed request body (400), otherwise the client cannot tell the two cases apart.
        raise CategoryError("bad_parent", _t("svc_taxonomy.err.direction_not_found", cid=parent_id))
    if parent["parent_id"] is not None:
        raise CategoryError("two_levels",
                            _t("svc_taxonomy.err.parent_is_project", name=parent['name']))


def _assert_plain_name(name: str) -> None:
    """`PATH_SEP` in a node name is forbidden (v47, wave 7) - `bad_name` BEFORE writing.

    The full path is the canonical text address of a node, and `split_path` counts exactly
    two parts in it. A node named "Фонд → Клієнти" breaks this arithmetic from
    both sides: it does not resolve by its own name, and the canonical path of
    its child ("Фонд → Клієнти → Акмекорп") becomes three-part and does not
    resolve either - after which a slice silently widens to the whole archive.
    `_cat_error` in `memory.py` maps `bad_name` to 400."""
    if PATH_SEP in str(name or ""):
        raise CategoryError(
            "bad_name",
            _t("svc_taxonomy.err.sep_in_name", sep=PATH_SEP))


def _assert_free_sibling_name(conn, name: str, parent_id: "int | None",
                              exclude_id: "int | None" = None) -> str:
    """Check name uniqueness **among siblings** (v47, ADR-023 §E).

    Siblings are directions among themselves (`parent_id IS NULL`) or projects of one
    parent; a direction and a foreign project may coincide. The check runs in Python
    by `norm_name` and **before** writing - which is why the client sees 409 with an address
    ("in which direction it is taken"), not a bare `IntegrityError` from the index
    `idx_categories_parent_name`. The index stays the last line of defence (a race
    of two requests), not the main control."""
    key = norm_name(name)
    if not key:
        raise CategoryError("empty", _t("svc_taxonomy.err.empty_name"))
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
            raise CategoryError("duplicate", _t("svc_taxonomy.err.dup_direction", name=row[1]),
                                conflict=conflict)
        parent = _node(conn, pid)
        raise CategoryError(
            "duplicate",
            _t("svc_taxonomy.err.dup_project", name=row[1],
               parent=parent['name'] if parent else pid),
            conflict=conflict)
    return key


def _slugify(name: str) -> str:
    """Name -> key for `slug`: `norm_name` + everything that is not a letter/digit becomes "-"."""
    return re.sub(r"[^\w]+", "-", norm_name(name), flags=re.UNICODE).strip("-")


def _free_slug(conn, name: str, parent_id: "int | None") -> str:
    """Unique `slug` - the only uniqueness that stayed global (v47).

    A name is no longer unique across the table, so two "Маркетинги" in different
    directions have to diverge somehow in `slug`: the first takes `маркетинг`,
    the second - `<parent slug>-маркетинг`, then `-2`, `-3`. Deterministic and
    never empty: an empty slug on two nodes is the same collision,
    just quieter (NULLs in a UNIQUE index do not conflict)."""
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
                    commit: bool = True, created_source: "str | None" = None) -> dict:
    """Create a direction (`parent_id=None`) or a project inside a direction.

    The parent is checked FIRST: without it it is unclear among whom to look for a
    duplicate, and the message "already exists in direction «X»" would have nothing to name.
    `created_source`: NULL for the owner/UI, `'mcp'` for an agent (provenance, ADR-042)."""
    clean = clean_name(name)
    _assert_plain_name(clean)
    _assert_parent(conn, None, parent_id)
    key = _assert_free_sibling_name(conn, clean, parent_id)
    order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO categories (name, name_norm, slug, color, icon, sort_order, parent_id, "
        "created_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (clean, key, _free_slug(conn, clean, parent_id), color, icon, order,
         int(parent_id) if parent_id is not None else None, created_source))
    if commit:
        conn.commit()
    return get_category(conn, cur.lastrowid)


def update_category(conn, cid: int, *, name=UNSET, color=UNSET, icon=UNSET,
                    parent_id=UNSET, sort_order=UNSET) -> tuple[dict, list[int]]:
    """Rename / recolour / reparent a node.

    Returns `(node, record ids)`, where the ids are **the whole subtree** of the node if
    the name or parent changed: both go into the chunk context prefix
    ("Direction -> Project"), so the prefix is stale for the project records
    too. A change of only colour/icon/order needs no re-embed - we return
    an empty list.

    `parent_id=None` makes the node a direction; a direction that has projects
    cannot become a project (`two_levels`) - otherwise a third level would appear.
    `sort_order` is beyond contract C2, because the current PATCH already supports it.
    """
    current = get_category(conn, cid)
    cid = int(cid)
    fields, params = [], []
    prefix_changed = False

    # Parent first: since v47 the name is checked among siblings, and who the siblings
    # are is known only to the NEW parent. Moving "Маркетинг" under a direction where such
    # a project already exists is also `duplicate`, even though nobody changed the name.
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
                    _t("svc_taxonomy.err.has_projects_n", name=current['name'], n=len(kids)),
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
        raise CategoryError("empty", _t("svc_taxonomy.err.nothing_to_update"))

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
            _t("svc_taxonomy.err.move_projects_first", action=action,
               names=', '.join(n['name'] for n in nodes)),
            projects=nodes)


def delete_category(conn, cid: int) -> None:
    """Delete a node. Records are not deleted - their `category_id` is set to NULL.

    A node with projects is not deleted and does not silently "lift" them to the root
    (`has_projects`): a silent lift would scatter the archive layout without a trace.
    """
    cid = int(cid)
    get_category(conn, cid)
    _assert_no_projects(conn, cid, _t("svc_taxonomy.action.delete_direction"))
    null_category_refs(conn, cid)
    conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
    conn.commit()


def merge_categories(conn, keep_id, merge_id) -> list[int]:
    """Move all references from `merge_id` to `keep_id` (`None` = "no direction").

    The `merge_id` node itself stays - the caller deletes it if asked
    (`delete_source`). Returns the ids of transcripts that moved: their
    direction changed, hence the chunk prefix is stale.
    """
    merge_id = int(merge_id)
    get_category(conn, merge_id)
    if keep_id is not None:
        keep_id = int(keep_id)
        get_category(conn, keep_id)
    _assert_no_projects(conn, merge_id, _t("svc_taxonomy.action.merge_direction"))
    moved = [r[0] for r in conn.execute(
        "SELECT id FROM transcriptions WHERE category_id = ? ORDER BY id",
        (merge_id,)).fetchall()]
    reassign_category_refs(conn, merge_id, keep_id)
    conn.commit()
    return moved


def set_category(conn, ids, cid, *, commit: bool = True) -> list[int]:
    """Set a direction on records. Returns only the ids where the value **changed**.

    Exactly this list goes into `reembed.schedule_records_reembed`: re-embedding
    a record that got the same direction is GPU time wasted.
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
    """Null `category_id` in ALL four tables that reference it.

    `audio_downloads` appeared here together with the spec: before it, deleting a direction
    left its id hanging on Media library rows (recon, hole v21).
    """
    for table in _CATEGORY_REF_TABLES:
        if not _table_exists(conn, table):
            continue
        conn.execute(f"UPDATE {table} SET category_id = NULL WHERE category_id = ?",
                     (int(cid),))


def reassign_category_refs(conn, old_id: int, new_id) -> None:
    """Move references old_id -> new_id (`None` = "no direction") in 4 tables."""
    dst = int(new_id) if new_id is not None else None
    for table in _CATEGORY_REF_TABLES:
        if not _table_exists(conn, table):
            continue
        conn.execute(f"UPDATE {table} SET category_id = ? WHERE category_id = ?",
                     (dst, int(old_id)))


# ============================================================
# Tags
# ============================================================

def _check_tag_name(clean: str) -> None:
    """The single tag-name guard - empty or longer than `TAG_NAME_MAX` (ADR §B).

    One code (`bad_name`) for both cases: the HTTP layer maps it to 400 and
    shows a human text. Called from `ensure_tag` (and through it -
    `set_tags(add)`, `resolve_suggestions(accept)`, `apply_chat_tags`), so
    no tag writer bypasses the check."""
    if not clean:
        raise CategoryError("bad_name", _t("svc_taxonomy.err.empty_tag_name"))
    if len(clean) > TAG_NAME_MAX:
        raise CategoryError("bad_name", _t("svc_taxonomy.err.tag_too_long", max=TAG_NAME_MAX))


def _check_raw_tag_names(raw) -> None:
    """Guard of the raw `add`/`accept` list - BEFORE `parse_tags`.

    `parse_tags` quietly drops empty items (right for filters), but
    in a list to write an empty string is a deliberately bad name, so we catch it here
    and raise `bad_name` before the first write (all or nothing)."""
    if raw is None:
        return
    items = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    for item in items:
        if not clean_name(item):
            raise CategoryError("bad_name", _t("svc_taxonomy.err.empty_tag_name"))


def ensure_tag(conn, name, color=None, *, commit: bool = True) -> dict:
    """Find a tag by `norm_name` or create it. Idempotent (ADR §B)."""
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
    """Tag dictionary with a record counter, by `name_norm`."""
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
        raise CategoryError("not_found", _t("svc_taxonomy.err.tag_not_found", tag_id=tag_id))
    return {"id": row[0], "name": row[1], "color": row[2]}


def update_tag(conn, tag_id: int, *, name=UNSET, color=UNSET) -> tuple[dict, list[int]]:
    """Rename / recolour a tag. Returns `(tag, ids of live records)`.

    Records go to re-embed only when the **exact** stored `name` string changed
    (it, not `name_norm`, sits in the chunk context prefix) -
    "ai" -> "AI" changes the prefix and goes to re-embed, "AI" -> "AI" does not; colour/icon
    do not touch the prefix at all. Name validation is the same `_check_tag_name`
    as in `ensure_tag`/`set_tags`, so no rename path bypasses
    the length limit."""
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
            raise CategoryError("duplicate", _t("svc_taxonomy.err.dup_tag", name=clash[0]))
        name_changed = clean != tag["name"]
        fields += ["name = ?", "name_norm = ?"]
        params += [clean, key]
    if color is not UNSET:
        fields.append("color = ?")
        params.append(color)
    if not fields:
        raise CategoryError("empty", _t("svc_taxonomy.err.nothing_to_update"))
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
    """Delete a tag with cascade into `record_tags`/`tg_chat_tags` (no FK in the schema).

    Returns the ids of records that lost this tag - their prefix changed.
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
    """Merge tags: references merge_id -> keep_id, merge_id itself is deleted.

    Returns **all** records that had `merge_id` - including those that already
    had `keep_id`: for them the name of the disappearing tag still drops out of the
    context prefix (it was a list of two names, one is left)."""
    keep = _tag_or_404(conn, keep_id)
    merge = _tag_or_404(conn, merge_id)
    if keep["id"] == merge["id"]:
        raise CategoryError("self_parent", _t("svc_taxonomy.err.merge_tag_self"))
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
    """Add/remove tags of a record. `True` - if the set really changed.

    Missing tags in `add` are created (the dictionary is filled from the place of use -
    the same way a direction is created from any select). `source` is written
    only on new rows: a manual confirmation of an existing tag does not overwrite
    its origin.
    """
    tid = int(tid)
    if source not in TAG_SOURCES:
        raise CategoryError("empty", _t("svc_taxonomy.err.unknown_tag_source", source=source))
    before = _record_tag_ids(conn, tid)

    _check_raw_tag_names(add)
    add_names = parse_tags(add)
    # The whole `add` is checked BEFORE the first INSERT - otherwise a bad name in
    # the middle of the list would leave the previous ones already written (a half-applied 400).
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
    """Batch for serializers: `{transcription_id: [{id, name, color}, ...]}`."""
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
    """Names suggested by Claude that a person has not confirmed yet."""
    row = conn.execute("SELECT suggested_tags_json FROM transcriptions WHERE id = ?",
                       (int(tid),)).fetchone()
    if row is None or not row[0]:
        return []
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError):
        logger.warning("[categories] tx=%s: suggested_tags_json is not JSON - ignoring", tid)
        return []
    if not isinstance(data, list):
        return []
    return [clean_name(x) for x in data if clean_name(x)]


def set_suggested_tags(conn, tid: int, names) -> None:
    """Overwrite the suggestions (enrichment path, story 06)."""
    names = parse_tags(names)
    conn.execute("UPDATE transcriptions SET suggested_tags_json = ? WHERE id = ?",
                 (json.dumps(names, ensure_ascii=False) if names else None, int(tid)))
    conn.commit()


def resolve_suggestions(conn, tid: int, *, accept=(), dismiss=()) -> bool:
    """Accept / dismiss suggestions. `True` - if anything changed.

    An accepted one becomes a regular tag with `source='claude'` (filters know nothing about
    the suggestion status - they read only `record_tags`), both
    lists are removed from `suggested_tags_json`. `accept` of a name that is not in
    `suggested_tags_json` (by `norm_name`) - `CategoryError(code="not_suggested")`,
    nothing is written; `dismiss` of an unknown name is a no-op (ADR §B: `accept`
    does not have to be able to "create anything")."""
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
                _t("svc_taxonomy.err.not_suggested", names=', '.join(unknown)))
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
    """Replace the tag set of a TG chat - **retroactively** over all its records.

    Added tags lie on the chat history with `source='chat'`; detached ones
    are removed only where they stand from this chat (`source='chat'`) - a manual
    tag on a specific message stays (ADR §B, the rule "one slot -
    one owner"). Returns the ids of records whose tag set changed.
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
    """TG ingest: copy the chat tags onto a fresh record (`source='chat'`)."""
    conn.execute(
        "INSERT OR IGNORE INTO record_tags (transcription_id, tag_id, source) "
        "SELECT ?, tag_id, 'chat' FROM tg_chat_tags WHERE chat_id = ?",
        (int(tid), int(chat_id)))
    conn.commit()
