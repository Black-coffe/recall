"""Brief cards for Telegram (K5): the message is assembled by code, not by the model.

The owner on 15.10.2026 about the first live K4 brief: «a really huge wall of text that
I don't want to read», `**` visible literally. Hence:

- the text is HTML (`parse_mode=HTML`) with only the tags ``b``, ``i``, ``a``;
- tasks are grouped in blocks by ROOT direction (the ``categories.parent_id`` tree),
  a block has ≤ ``MAX_CARDS`` most urgent ones and a line "N more →" to ``/tasks?category_id=``;
- a card = what · who · due when · from where + a link to the recording with the task highlighted;
- Claude gives only 1-2 "headline" lines as plain text (``clean_headline``).

Privacy: every field is first masked and checked against stop words
(``pipes_privacy``) and only then truncated and escaped — truncation cannot
cut a phone number in half, and escaping does not hide a stop word from the check.

Blocks are separated by an empty line: ``tg_bot.split_text`` cuts exactly there, so no
part of the message breaks a tag.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

from app.core.i18n import server_lang, tr, tr_n
from app.db.connection import get_db_connection
from app.services import pipes_privacy

logger = logging.getLogger(__name__)

MAX_CARDS = 5
TASK_LEN = 160
META_LEN = 60

_TITLE_KEYS = ("weekly_digest", "dropped_commitments", "open_questions")


def _t(key: str, **vars) -> str:
    """Service line of the brief in the language of the setting at assembly time."""
    return tr("telegram.brief." + key, lang=server_lang(), **vars)

#: Digest tasks: bucket → (marker, urgency rank).
_TASK_KINDS = {"overdue": ("🔴", 0), "this_week": ("🟡", 1), "no_date": ("⚪", 2)}


def _dm(iso: Any) -> str:
    s = str(iso or "")
    return f"{s[8:10]}.{s[5:7]}" if len(s) >= 10 else s


# ------------------------------------------------------------- privacy

class _Guard:
    """Field → mask → stop words → truncate → html.escape. Stop words are collected into ``leaked``."""

    def __init__(self, words: Iterable[str]):
        self.words = list(words)
        self.leaked: set[str] = set()

    def __call__(self, value: Any, limit: Optional[int] = None) -> str:
        s = " ".join(str(value).split()) if value is not None else ""
        s = pipes_privacy.mask(s)
        self.leaked.update(pipes_privacy.leaked_words(s, self.words))
        if limit and len(s) > limit:
            s = s[:limit - 1].rstrip() + "…"
        return html.escape(s, quote=False)


def _href(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


# --------------------------------------------------------------- directions

@dataclass
class _Dirs:
    """Where an item comes from and which direction it is in — in one pass over the DB."""
    cats: dict[int, tuple[str, Optional[int]]] = field(default_factory=dict)
    task: dict[int, tuple[Optional[int], Optional[int]]] = field(default_factory=dict)
    rec: dict[int, Optional[int]] = field(default_factory=dict)

    def root(self, cat_id: Optional[int]) -> Optional[int]:
        seen: set[int] = set()
        while cat_id is not None and cat_id in self.cats and cat_id not in seen:
            seen.add(cat_id)
            parent = self.cats[cat_id][1]
            if parent is None or parent not in self.cats:
                return cat_id
            cat_id = parent
        return None

    def name(self, cat_id: Optional[int]) -> str:
        return self.cats[cat_id][0] if cat_id in self.cats else _t("noDir")


def _load_dirs(db_path: str, task_ids: set[int], rec_ids: set[int]) -> _Dirs:
    d = _Dirs()
    with get_db_connection(db_path) as conn:
        d.cats = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT id, name, parent_id FROM categories")}
        if task_ids:
            q = ",".join("?" * len(task_ids))
            d.task = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT ai.id, ai.transcription_id, t.category_id FROM action_items ai "
                f"LEFT JOIN transcriptions t ON t.id = ai.transcription_id WHERE ai.id IN ({q})",
                sorted(task_ids))}
        if rec_ids:
            q = ",".join("?" * len(rec_ids))
            d.rec = {r[0]: r[1] for r in conn.execute(
                f"SELECT id, category_id FROM transcriptions WHERE id IN ({q})", sorted(rec_ids))}
    return d


# ----------------------------------------------------------------- cards

@dataclass
class _Card:
    root: Optional[int]
    leaf: Optional[int]
    sort: tuple
    html: str
    item_id: Optional[int] = None    # task → number and a row of buttons (K5 k5-task-buttons)


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _task_card(it: dict, mark: str, when: str, d: _Dirs, g: _Guard, base: str,
               sort: tuple) -> _Card:
    tid0, cat = d.task.get(_int(it.get("id")), (None, None))
    tid = _int(it.get("transcription_id")) or tid0
    root = d.root(cat)
    text = g(it.get("task"), TASK_LEN) or _t("noText")
    head = (_href(f"{base}/transcript/{tid}?task={_int(it['id'])}", text)
            if tid and _int(it.get("id")) else f"<b>{text}</b>")
    meta = [f"{mark} {when}"]
    if it.get("owner"):
        meta.append(f"👤 {g(it['owner'], META_LEN)}")
    if cat is not None and cat != root:
        meta.append(f"📁 {g(d.name(cat), META_LEN)}")
    src = it.get("chat") or it.get("meeting")
    if src:
        meta.append(f"{'💬' if it.get('chat') else '🎙'} {g(src, META_LEN)}")
    return _Card(root, cat, sort, f"• {head}\n   " + " · ".join(meta),
                 _int(it.get("id")))


def _digest_tasks(res: dict, d: _Dirs, g: _Guard, base: str) -> list[_Card]:
    cards, seen = [], set()
    for kind, (mark, rank) in _TASK_KINDS.items():
        for i, it in enumerate(res.get(kind) or []):
            iid = _int(it.get("id")) if isinstance(it, dict) else None
            if not isinstance(it, dict) or (iid is not None and iid in seen):
                continue
            if iid is not None:
                seen.add(iid)
            due = it.get("due_date")
            if kind == "overdue":
                when = _t("overdue", date=_dm(due))
            elif kind == "this_week":
                when = _t("thisWeek", date=_dm(due))
            else:
                when = _t("noDate")
            # Overdue — oldest deadline first; undated — in order of arrival.
            cards.append(_task_card(it, mark, when, d, g, base,
                                    (rank, str(due or ""), i)))
    return cards


def _dropped(items: list, d: _Dirs, g: _Guard, base: str) -> list[_Card]:
    return [_task_card(it, "🕳", _t("silentSince", date=_dm(it.get("meeting_date"))), d, g, base,
                       (str(it.get("meeting_date") or ""), i))
            for i, it in enumerate(items) if isinstance(it, dict)]


def _questions(items: list, d: _Dirs, g: _Guard, base: str) -> list[_Card]:
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        tid = _int(it.get("transcription_id"))
        cat = d.rec.get(tid) if tid else None
        text = g(it.get("question"), TASK_LEN) or _t("noText")
        head = _href(f"{base}/transcript/{tid}", text) if tid else f"<b>{text}</b>"
        meta = []
        if it.get("silence_h") is not None:
            meta.append(f"🤐 {_t('silentHours', h=round(float(it['silence_h'])))}")
        if it.get("asked_by"):
            meta.append(f"👤 {g(it['asked_by'], META_LEN)}")
        if it.get("chat"):
            meta.append(f"💬 {g(it['chat'], META_LEN)}")
        link = str(it.get("link") or "")
        if link.startswith("https://t.me/"):
            meta.append(_href(link, _t("inTelegram")))
        root = d.root(cat)
        if cat is not None and cat != root:
            meta.append(f"📁 {g(d.name(cat), META_LEN)}")
        out.append(_Card(root, cat, (-float(it.get("silence_h") or 0), i),
                         f"• {head}" + ("\n   " + " · ".join(meta) if meta else "")))
    return out


def _numbered(c: _Card, buttons: Optional[list]) -> str:
    """A task with an id → "N." instead of "•" and its id in ``buttons`` (order = number)."""
    from app.services.brief_actions import MAX_ITEMS
    if buttons is None or c.item_id is None or len(buttons) >= MAX_ITEMS:
        return c.html
    buttons.append(c.item_id)
    return c.html.replace("• ", f"<b>{len(buttons)}.</b> ", 1)


def _blocks(title: str, cards: list[_Card], d: _Dirs, g: _Guard, base: str, *,
            more_link: bool, buttons: Optional[list] = None) -> list[str]:
    """Section → paragraphs: a heading and one block per root direction."""
    if not cards:
        return []
    groups: dict[Optional[int], list[_Card]] = {}
    for c in cards:
        groups.setdefault(c.root, []).append(c)
    order = sorted(groups, key=lambda r: (r is None, -len(groups[r]), d.name(r)))
    out = [f"<b>{title}</b> · {len(cards)}"]
    for root in order:
        group = sorted(groups[root], key=lambda c: c.sort)
        lines = [f"🏷 <b>{g(d.name(root), META_LEN)}</b>"]
        lines += [_numbered(c, buttons) for c in group[:MAX_CARDS]]
        rest = len(group) - MAX_CARDS
        if rest > 0:
            lines.append(_href(f"{base}/tasks?category_id={'none' if root is None else root}",
                               _t("more", n=rest)) if more_link else _t("moreInRecall", n=rest))
        out.append("\n".join(lines))
    return out


def _stale(items: list, g: _Guard, base: str) -> list[str]:
    items = [it for it in items if isinstance(it, dict)]
    if not items:
        return []
    lines = [f"<b>{_t('stale')}</b> · {len(items)}"]
    for it in items[:MAX_CARDS]:
        name = g(it.get("canonical_name"), META_LEN)
        label = _href(f"{base}/entities/{_int(it['id'])}", name) if _int(it.get("id")) else name
        lines.append(f"• {label} · {_t('staleDays', days=it.get('days_silent', '?'))}")
    if len(items) > MAX_CARDS:
        lines.append(_t("moreInRecall", n=len(items) - MAX_CARDS))
    return ["\n".join(lines)]


def _corrections(items: list, g: _Guard, base: str) -> list[str]:
    items = [it for it in items if isinstance(it, dict)]
    if not items:
        return []
    lines = [f"<b>{_t('corrections')}</b> · {len(items)}"]
    for it in items[:MAX_CARDS]:
        name = g(it.get("target_name") or _t("recordWord"), META_LEN)
        tid = _int(it.get("target_id")) if it.get("target_type") == "transcription" else None
        label = _href(f"{base}/transcript/{tid}", name) if tid else f"<b>{name}</b>"
        lines.append(f"• {label}: {g(it.get('body'), TASK_LEN)}")
    if len(items) > MAX_CARDS:
        lines.append(_t("moreInRecall", n=len(items) - MAX_CARDS))
    return ["\n".join(lines)]


# ------------------------------------------------------------------ render

_MD = re.compile(r"(\*\*|__|`+|~~)")
_LEAD = re.compile(r"^\s*(?:#{1,6}\s*|[-*•]\s+|\d+[.)]\s+|>\s*)")


def clean_headline(text: str, *, max_lines: int = 2, max_len: int = 300) -> str:
    """"Headline" from Claude: plain text, ≤2 lines — the model's markup is stripped by code."""
    lines = []
    for line in (text or "").splitlines():
        line = _LEAD.sub("", _MD.sub("", line)).strip().strip("*_").strip()
        if line:
            lines.append(line if len(line) <= max_len else line[:max_len - 1].rstrip() + "…")
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


def link_base() -> str:
    """Base of Recall links in cards: ``RECALL_PUBLIC_URL`` (an address visible from the phone —
    the owner has Tailscale), otherwise ``RECALL_API_URL``. Owner on 15.10.2026: «Links in the new cards
    of the brief lead to the Recall address on this computer, so they will not open from the phone.»"""
    from app.core import settings as _settings
    return (_settings.env("RECALL_PUBLIC_URL").strip() or _settings.env("RECALL_API_URL").strip()
            or "http://127.0.0.1:5050").rstrip("/")


def title_for(tools: Iterable[str]) -> str:
    return " + ".join(_t("title." + t) if t in _TITLE_KEYS else t for t in tools)


def render(title: str, data: dict, *, db_path: str, today: date, base_url: Optional[str] = None,
           headline: Optional[str] = None, missing: Iterable[str] = (), hidden: int = 0,
           words: Optional[Iterable[str]] = None,
           buttons: Optional[list] = None) -> tuple[str, list[str]]:
    """→ (message HTML, stop words that occurred in fields). A non-empty list = do not send.

    ``words`` is the stop list (default ``privacy.stop_words()``; ``PrivacyError``
    propagates out). ``buttons`` (a list, K5): the tasks of the report and "missed" are numbered
    "1.", "2."…, their ids are appended there in number order (→ ``brief_actions.keyboard``),
    and at the bottom a line appears saying the buttons work only with the PC on."""
    from app.services import privacy
    g = _Guard(privacy.stop_words() if words is None else words)
    base = (base_url or link_base()).rstrip("/")
    lists = {(tool, key): v for tool, res in data.items() if isinstance(res, dict)
             for key, v in res.items() if isinstance(v, list)}
    task_ids = {_int(it.get("id")) for (_, key), v in lists.items()
                if key in (*_TASK_KINDS, "dropped") for it in v if isinstance(it, dict)}
    task_ids.discard(None)
    rec_ids = {_int(it.get("transcription_id")) for (_, key), v in lists.items()
               if key == "questions" for it in v if isinstance(it, dict)}
    rec_ids.discard(None)
    d = _load_dirs(db_path, task_ids, rec_ids)

    paras = [f"<b>{g(title)}</b> · {today.strftime('%d.%m')}"]
    if headline:
        paras.append("💡 " + "\n".join(g(line) for line in headline.splitlines()))
    miss = [g(m) for m in missing]
    if miss:
        paras.append("\n".join(miss))
    body: list[str] = []
    for tool, res in data.items():
        if not isinstance(res, dict):
            continue
        if any(k in res for k in _TASK_KINDS):
            body += _blocks(_t("tasks"), _digest_tasks(res, d, g, base), d, g, base,
                            more_link=True, buttons=buttons)
        body += _blocks(_t("dropped"),
                        _dropped(res.get("dropped") or [], d, g, base), d, g, base,
                        more_link=True, buttons=buttons)
        body += _blocks(_t("questions"),
                        _questions(res.get("questions") or [], d, g, base), d, g, base,
                        more_link=False)
        body += _stale(res.get("stale_topics") or [], g, base)
        body += _corrections(res.get("corrections") or [], g, base)
        cov = res.get("coverage")
        if isinstance(cov, dict) and cov.get("total"):
            body.append(f"<i>{_t('coverage', dated=cov.get('dated', 0), total=cov['total'])}</i>")
    paras += body or [_t("nothingNew")]
    if hidden:
        paras.append(g(tr_n("telegram.brief.hidden", hidden, lang=server_lang())))
    if buttons:
        from app.services.brief_actions import footer
        paras.append(f"<i>{html.escape(footer(), quote=False)}</i>")
    return "\n\n".join(paras), sorted(g.leaked)


def __getattr__(name: str):
    if name == "NO_DIR":          # compatibility: the "no direction" name depends on the language
        return _t("noDir")
    raise AttributeError(name)
