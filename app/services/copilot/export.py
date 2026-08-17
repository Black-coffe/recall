"""Co-pilot — експорт сесії + дайджест для реінджесту (Phase 19, Крок 8).

Зібрати все, що йшло паралельно під час дзвінка, в один документ: транскрипт з
таймкодами + інлайн-нотатки ко-пілота (протиріччя / питання / уточнення / факти)
з посиланнями на джерела архіву. Формати ``.md`` (для людей) і ``.json`` (для
машин), узгоджено з наявним bulk-export (Phase 18).

Окремо :func:`notes_digest` — стислий текст лише з підказок ко-пілота, який
реінджеститься назад у RAG (``source_type='copilot'``), щоб аналітика ко-пілота
сама стала пам'яттю для майбутніх дзвінків.

Чисті функції БЕЗ I/O: на вхід — ``timeline`` (з :meth:`CopilotService.get_timeline`)
і ``transcript`` (сегменти+мета або None). Тестується офлайн.
"""
from __future__ import annotations

import json
from typing import Optional

from app.utils.helpers import format_duration


_KIND_MD = {
    "contradiction": ("🔴", "ПРОТИРІЧЧЯ"),
    "question": ("❓", "ПИТАННЯ"),
    "clarification": ("💡", "УТОЧНЕННЯ"),
    "fact": ("📌", "ФАКТ"),
}
_MODE_LBL = {"light": "лайт", "medium": "середній", "hard": "жорсткий"}
_IMP_LBL = {"low": "низька", "medium": "середня", "high": "висока"}


def _ts(seconds) -> str:
    # Канонічний форматер h:mm:ss / m:ss (app/utils/helpers); "--:--" для відсутнього.
    return "--:--" if seconds is None else format_duration(seconds)


def _speaker(raw: Optional[str]) -> str:
    if raw == "self":
        return "Ви"
    if raw == "other":
        return "Інший"
    if raw and raw.startswith("SPEAKER_"):
        try:
            return "Спікер " + str(int(raw.split("_")[1]) + 1)
        except (ValueError, IndexError):
            return raw
    return raw or "?"


def _evidence_str(evidence: list) -> str:
    parts = []
    for e in evidence or []:
        name = e.get("source_name") or ("#" + str(e.get("transcription_id")))
        date = e.get("meeting_date")
        parts.append(f"{name}" + (f" ({date})" if date else ""))
    return ", ".join(parts)


def collect_notes(events: list) -> list:
    """Звести події інсайтів у нотатки. insight_local (база) + злиття з його
    верифікацією (insight_verified з ref_event_id) + окремі sweep-інсайти."""
    verified_by_ref, sweep, locals_ = {}, [], []
    for e in events or []:
        if e.get("kind") == "insight_local":
            locals_.append(e)
        elif e.get("kind") == "insight_verified":
            ref = (e.get("payload") or {}).get("ref_event_id")
            if ref is not None:
                verified_by_ref[ref] = e
            else:  # standalone safety-sweep finding (уже «перевірено»)
                sweep.append(e)
    notes = []
    for e in locals_:
        p = e.get("payload") or {}
        v = verified_by_ref.get(e.get("id"))
        vp = (v.get("payload") or {}) if v else {}
        notes.append({
            "ts": e.get("ts_offset_sec"),
            "kind": p.get("kind", "fact"),
            "text": (vp.get("text") or p.get("text") or "").strip(),
            "source": "api" if v else (e.get("source") or "local"),
            "verdict": vp.get("verdict"),
            "confidence": (v.get("confidence") if v else e.get("confidence")),
            "evidence": p.get("evidence") or [],
        })
    for e in sweep:
        p = e.get("payload") or {}
        notes.append({
            "ts": e.get("ts_offset_sec"), "kind": p.get("kind", "fact"),
            "text": (p.get("text") or "").strip(), "source": "api",
            "verdict": "real", "confidence": e.get("confidence"),
            "evidence": p.get("evidence") or [],
        })
    notes.sort(key=lambda n: (n["ts"] is None, n["ts"] if n["ts"] is not None else 0))
    return notes


def _meta_line(session: dict, agg: dict, notes: list) -> str:
    cfg = session.get("config") or {}
    mode = _MODE_LBL.get(cfg.get("mode") or session.get("mode"), "—")
    imp = _IMP_LBL.get(cfg.get("importance") or session.get("importance"), "—")
    verified = sum(1 for n in notes if n["source"] == "api")
    cost = session.get("cost_estimate") or 0
    return (f"*Режим: {mode} · важливість: {imp} · підказок: {len(notes)} "
            f"(перевірено {verified}) · вартість: ${float(cost):.2f}*")


#: Коментар оператора у вигляді, придатному для тієї ж стрічки, що й нотатки
#: ко-пілота: у них спільна вісь — секунди від початку запису. Тому вони не
#: додаються окремим хвостом, а стають на своє місце в діалозі: у вигрузці
#: видно, ЩО саме говорили, коли оператор вписав своє уточнення.
_CM_LABEL = {
    "correction": "ВИПРАВЛЕННЯ", "decision": "РІШЕННЯ", "note": "НОТАТКА",
    "context": "КОНТЕКСТ", "question": "ПИТАННЯ",
}


def _comment_md(c: dict) -> str:
    lbl = _CM_LABEL.get(c.get("kind"), "НОТАТКА")
    pin = " · закріплено" if c.get("pinned") else ""
    return f"✍ **{lbl}** (оператор{pin}): {(c.get('body') or '').strip()}"


def build_markdown(timeline: dict, transcript: Optional[dict] = None,
                   comments: Optional[list] = None) -> str:
    """Об'єднаний .md: шапка + теми + діалог з інлайн-нотатками + коментарі
    оператора на своїх таймкодах + зведення підказок."""
    session = timeline.get("session") or {}
    events = timeline.get("events") or []
    topics = timeline.get("topics") or []
    agg = timeline.get("aggregates") or {}
    notes = collect_notes(events)
    comments = comments or []
    name = (transcript or {}).get("source_name") or f"Сесія #{session.get('id')}"

    out = [f"# Ко-пілот сесії — {name}", "", _meta_line(session, agg, notes), ""]

    if topics:
        out.append("## Теми")
        for t in topics:
            span = f" ({_ts(t.get('first_ts'))}–{_ts(t.get('last_ts'))})" if t.get("first_ts") is not None else ""
            out.append(f"- {t.get('label') or ('Тема ' + str(t.get('topic_index')))}{span}")
        out.append("")

    # Діалог з інлайн-нотатками (merge сегментів і нотаток за таймкодом).
    segs = (transcript or {}).get("segments") or []
    out.append("## Діалог, нотатки ко-пілота і коментарі оператора")
    cm_items = [("cm", c.get("anchor_time"), c) for c in comments]
    if segs:
        merged = ([("seg", s.get("start"), s) for s in segs]
                  + [("note", n["ts"], n) for n in notes] + cm_items)
        merged.sort(key=lambda x: (x[1] is None, x[1] if x[1] is not None else 0))
        for typ, ts, obj in merged:
            if typ == "seg":
                out.append(f"[{_ts(ts)}] **{_speaker(obj.get('speaker'))}:** {(obj.get('text') or '').strip()}")
            elif typ == "cm":
                out.append("  > " + _comment_md(obj))
            else:
                out.append("  > " + _note_md(obj))
    else:  # немає діалогу — лише нотатки й коментарі
        merged = [("note", n["ts"], n) for n in notes] + cm_items
        merged.sort(key=lambda x: (x[1] is None, x[1] if x[1] is not None else 0))
        for typ, ts, obj in merged:
            out.append(f"[{_ts(ts)}] "
                       + (_comment_md(obj) if typ == "cm" else _note_md(obj)))
    out.append("")

    if comments:
        # Окремим зведенням, попереду підказок: підказки згенерувала модель, а
        # це написала людина — при суперечності правильне друге.
        out.append("## Коментарі оператора")
        for c in comments:
            ts = _ts(c.get("anchor_time")) if c.get("anchor_time") is not None else "—"
            out.append(f"- [{ts}] " + _comment_md(c))
        out.append("")

    if notes:
        out.append("## Зведення підказок")
        for n in notes:
            out.append(f"- [{_ts(n['ts'])}] " + _note_md(n))
        out.append("")
    return "\n".join(out)


def _note_md(n: dict) -> str:
    icon, lbl = _KIND_MD.get(n["kind"], ("•", n["kind"].upper()))
    badge = "перевірено" if n["source"] == "api" else "локально"
    if n.get("verdict") == "refuted":
        badge = "спростовано"
    conf = f" · {round(n['confidence'] * 100)}%" if n.get("confidence") is not None else ""
    ev = _evidence_str(n.get("evidence"))
    ev_str = f" — джерела: {ev}" if ev else ""
    return f"{icon} **{lbl}** ({badge}{conf}): {n['text']}{ev_str}"


def build_json(timeline: dict, transcript: Optional[dict] = None,
               comments: Optional[list] = None) -> dict:
    """Структурований експорт: мета + теми + діалог + нотатки + коментарі
    оператора (для машин/реімпорту)."""
    session = timeline.get("session") or {}
    notes = collect_notes(timeline.get("events") or [])
    segs = (transcript or {}).get("segments") or []
    return {
        "session": {k: session.get(k) for k in (
            "id", "mode", "importance", "api_enabled", "category_id", "status",
            "started_at", "ended_at", "tokens_in", "tokens_out", "cost_estimate",
            "model_local", "model_api", "transcription_id")},
        "transcript_name": (transcript or {}).get("source_name"),
        "topics": timeline.get("topics") or [],
        "aggregates": timeline.get("aggregates") or {},
        "dialog": [{"ts": s.get("start"), "speaker": _speaker(s.get("speaker")),
                    "text": (s.get("text") or "").strip()} for s in segs],
        "notes": notes,
        # Окремим ключем, а не в notes: підказки згенерувала модель, коментарі
        # написала людина. Змішавши їх, споживач втратив би саме ту різницю,
        # заради якої коментарі й важать більше.
        "operator_comments": [
            {"ts": c.get("anchor_time"), "kind": c.get("kind"),
             "pinned": bool(c.get("pinned")), "text": c.get("body"),
             "created_at": c.get("created_at")}
            for c in (comments or [])
        ],
    }


def notes_digest(timeline: dict, transcript: Optional[dict] = None,
                 comments: Optional[list] = None) -> str:
    """Стислий ТЕКСТ лише з підказок ко-пілота — для реінджесту в RAG. Групуємо за
    типом, додаємо джерела; це стає шукабельною пам'яттю для майбутніх дзвінків.

    Коментарі оператора йдуть ПЕРШИМИ і окремим блоком: реінджест кладе цей
    текст у RAG як звичайний запис, і те, що написала людина, має читатись
    раніше за те, що припустила модель."""
    notes = collect_notes(timeline.get("events") or [])
    name = (transcript or {}).get("source_name") or f"Сесія #{(timeline.get('session') or {}).get('id')}"
    topics = [t.get("label") for t in (timeline.get("topics") or []) if t.get("label")]
    lines = [f"Нотатки ко-пілота з дзвінка «{name}».", ""]
    if topics:
        lines.append("Теми: " + ", ".join(topics))
        lines.append("")
    if comments:
        lines.append("Коментарі оператора під час дзвінка:")
        for c in comments:
            ts = _ts(c.get("anchor_time")) if c.get("anchor_time") is not None else "—"
            lbl = _CM_LABEL.get(c.get("kind"), "НОТАТКА").lower()
            lines.append(f"- [{ts}] ({lbl}) {(c.get('body') or '').strip()}")
        lines.append("")
    groups = [("contradiction", "Протиріччя"), ("fact", "Факти"),
              ("question", "Питання"), ("clarification", "Уточнення")]
    for kind, title in groups:
        items = [n for n in notes if n["kind"] == kind]
        if not items:
            continue
        lines.append(f"{title}:")
        for n in items:
            ev = _evidence_str(n.get("evidence"))
            mark = " [перевірено]" if n["source"] == "api" else ""
            lines.append(f"- {n['text']}{mark}" + (f" (джерела: {ev})" if ev else ""))
        lines.append("")
    return "\n".join(lines).strip()
