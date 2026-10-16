"""Co-pilot — session export + digest for re-ingest (Phase 19, Step 8).

Gather everything that ran in parallel during the call into one document: the transcript with
timecodes + inline co-pilot notes (contradictions / questions / clarifications / facts)
with links to archive sources. Formats ``.md`` (for humans) and ``.json`` (for
machines), consistent with the existing bulk-export (Phase 18).

Separately :func:`notes_digest` — a compact text of only the co-pilot hints, which is
re-ingested back into RAG (``source_type='copilot'``), so that the co-pilot's analytics
itself becomes memory for future calls.

Pure functions WITHOUT I/O: the input is ``timeline`` (from :meth:`CopilotService.get_timeline`)
and ``transcript`` (segments+meta or None). Tested offline.
"""
from __future__ import annotations

import json
from typing import Optional

from app.core.i18n import tr
from app.utils.helpers import format_duration


#: Insight kind → icon; the label comes from the catalog (svc_llm.cp_export.kind.*).
_KIND_ICON = {"contradiction": "🔴", "question": "❓", "clarification": "💡", "fact": "📌"}
_MODES = ("light", "medium", "hard")
_IMPS = ("low", "medium", "high")


def _ts(seconds) -> str:
    # Canonical h:mm:ss / m:ss formatter (app/utils/helpers); "--:--" for a missing value.
    return "--:--" if seconds is None else format_duration(seconds)


def _speaker(raw: Optional[str]) -> str:
    if raw == "self":
        return tr("svc_llm.cp_export.you")
    if raw == "other":
        return tr("svc_llm.cp_export.other")
    if raw and raw.startswith("SPEAKER_"):
        try:
            return tr("svc_llm.cp_export.speaker_n", n=int(raw.split("_")[1]) + 1)
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
    """Merge insight events into notes. insight_local (base) + merge with its
    verification (insight_verified with ref_event_id) + standalone sweep insights."""
    verified_by_ref, sweep, locals_ = {}, [], []
    for e in events or []:
        if e.get("kind") == "insight_local":
            locals_.append(e)
        elif e.get("kind") == "insight_verified":
            ref = (e.get("payload") or {}).get("ref_event_id")
            if ref is not None:
                verified_by_ref[ref] = e
            else:  # standalone safety-sweep finding (already "verified")
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
    mode = cfg.get("mode") or session.get("mode")
    mode = tr(f"svc_llm.cp_export.mode.{mode}") if mode in _MODES else "—"
    imp = cfg.get("importance") or session.get("importance")
    imp = tr(f"svc_llm.cp_export.imp.{imp}") if imp in _IMPS else "—"
    verified = sum(1 for n in notes if n["source"] == "api")
    cost = session.get("cost_estimate") or 0
    return tr("svc_llm.cp_export.meta", mode=mode, imp=imp, notes=len(notes),
              verified=verified, cost=f"{float(cost):.2f}")


#: An operator comment in a form suitable for the same stream as the co-pilot
#: notes: they share an axis — seconds from the start of the recording. So they are not
#: appended as a separate tail, but take their place in the dialogue: the export
#: shows WHAT was being said when the operator typed their clarification.
#: The .md labels come from the catalog (svc_media.export.kind.*); this uk table serves only
#: `notes_digest`, whose text is re-ingested into RAG (archive data, kept in the source language).
_CM_LABEL = {
    "correction": "ВИПРАВЛЕННЯ", "decision": "РІШЕННЯ", "note": "НОТАТКА",
    "context": "КОНТЕКСТ", "question": "ПИТАННЯ",
}


def _comment_md(c: dict) -> str:
    kind = c.get("kind") if c.get("kind") in _CM_LABEL else "note"
    pin = tr("svc_llm.cp_export.pinned") if c.get("pinned") else ""
    return tr("svc_llm.cp_export.comment", label=tr(f"svc_media.export.kind.{kind}"),
              pin=pin, body=(c.get("body") or "").strip())


def build_markdown(timeline: dict, transcript: Optional[dict] = None,
                   comments: Optional[list] = None) -> str:
    """Combined .md: header + topics + dialogue with inline notes + operator
    comments at their timecodes + hint summary."""
    session = timeline.get("session") or {}
    events = timeline.get("events") or []
    topics = timeline.get("topics") or []
    agg = timeline.get("aggregates") or {}
    notes = collect_notes(events)
    comments = comments or []
    name = ((transcript or {}).get("source_name")
            or tr("svc_llm.cp_export.session_n", id=session.get("id")))

    out = [f"# {tr('svc_llm.cp_export.title', name=name)}", "",
           _meta_line(session, agg, notes), ""]

    if topics:
        out.append(f"## {tr('svc_llm.cp_export.topics')}")
        for t in topics:
            span = f" ({_ts(t.get('first_ts'))}–{_ts(t.get('last_ts'))})" if t.get("first_ts") is not None else ""
            label = t.get("label") or tr("svc_llm.cp_export.topic_n", n=t.get("topic_index"))
            out.append(f"- {label}{span}")
        out.append("")

    # Dialogue with inline notes (merge of segments and notes by timecode).
    segs = (transcript or {}).get("segments") or []
    out.append(f"## {tr('svc_llm.cp_export.dialog')}")
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
    else:  # no dialogue — only notes and comments
        merged = [("note", n["ts"], n) for n in notes] + cm_items
        merged.sort(key=lambda x: (x[1] is None, x[1] if x[1] is not None else 0))
        for typ, ts, obj in merged:
            out.append(f"[{_ts(ts)}] "
                       + (_comment_md(obj) if typ == "cm" else _note_md(obj)))
    out.append("")

    if comments:
        # As a separate summary, ahead of the hints: the hints were generated by the model, and
        # this was written by a human — in case of a contradiction the latter is right.
        out.append(f"## {tr('svc_llm.cp_export.comments')}")
        for c in comments:
            ts = _ts(c.get("anchor_time")) if c.get("anchor_time") is not None else "—"
            out.append(f"- [{ts}] " + _comment_md(c))
        out.append("")

    if notes:
        out.append(f"## {tr('svc_llm.cp_export.summary')}")
        for n in notes:
            out.append(f"- [{_ts(n['ts'])}] " + _note_md(n))
        out.append("")
    return "\n".join(out)


def _note_md(n: dict) -> str:
    icon = _KIND_ICON.get(n["kind"], "•")
    lbl = (tr(f"svc_llm.cp_export.kind.{n['kind']}") if n["kind"] in _KIND_ICON
           else n["kind"].upper())
    badge = tr("svc_llm.cp_export.badge.verified" if n["source"] == "api"
               else "svc_llm.cp_export.badge.local")
    if n.get("verdict") == "refuted":
        badge = tr("svc_llm.cp_export.badge.refuted")
    conf = f" · {round(n['confidence'] * 100)}%" if n.get("confidence") is not None else ""
    ev = _evidence_str(n.get("evidence"))
    ev_str = tr("svc_llm.cp_export.sources", sources=ev) if ev else ""
    return f"{icon} **{lbl}** ({badge}{conf}): {n['text']}{ev_str}"


def build_json(timeline: dict, transcript: Optional[dict] = None,
               comments: Optional[list] = None) -> dict:
    """Structured export: meta + topics + dialogue + notes + operator
    comments (for machines/re-import)."""
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
        # As a separate key, not in notes: the hints were generated by the model, the comments
        # were written by a human. Mixing them, the consumer would lose exactly the difference
        # for which the comments matter more.
        "operator_comments": [
            {"ts": c.get("anchor_time"), "kind": c.get("kind"),
             "pinned": bool(c.get("pinned")), "text": c.get("body"),
             "created_at": c.get("created_at")}
            for c in (comments or [])
        ],
    }


def notes_digest(timeline: dict, transcript: Optional[dict] = None,
                 comments: Optional[list] = None) -> str:
    """Compact TEXT of only the co-pilot hints — for re-ingest into RAG. Grouped by
    kind, with sources added; this becomes searchable memory for future calls.

    Operator comments go FIRST and as a separate block: re-ingest puts this
    text into RAG as a regular record, and what a human wrote should be read
    before what the model assumed."""
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
