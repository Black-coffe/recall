"""Task status machine (task-approval-01, ADR-056): decisions, show, expiry, queue, guards,
the Monday `stuck` bucket and the `proposals` counter, `unlegacy`."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.core.error_codes import ERROR_CODES
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import commitments as cm

TODAY = date(2026, 10, 27)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "status.db")
    init_database(path)
    with get_db_connection(path) as conn:
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, meeting_date, "
                     "created_at) VALUES (1, 'recording', 'Зустріч', '2026-10-20', "
                     "'2026-10-20 10:00:00')")
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, meeting_date, "
                     "created_at) VALUES (2, 'recording', 'Стара', '2026-01-10', "
                     "'2026-01-10 10:00:00')")
        conn.commit()
    return path


def _add(db: str, task: str = "Надіслати договір", *, status: str = "proposed", tid: int = 1,
         **cols) -> int:
    fields = {"transcription_id": tid, "task": task, "status": status, **cols}
    with get_db_connection(db) as conn:
        cur = conn.execute(
            f"INSERT INTO action_items ({', '.join(fields)}) "
            f"VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))
        conn.commit()
        return cur.lastrowid


def _row(db: str, aid: int) -> dict:
    with get_db_connection(db) as conn:
        return dict(conn.execute("SELECT * FROM action_items WHERE id = ?", (aid,)).fetchone())


def _events(db: str) -> list[dict]:
    with get_db_connection(db) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM action_item_events ORDER BY id")]


# ---------------------------------------------------------------- decisions

@pytest.mark.parametrize("start", ["proposed", "expired", "rejected", "legacy"])
def test_approve_moves_to_open_with_decided_at(db: str, start: str):
    aid = _add(db, status=start)
    res = cm.apply_action(db, aid, "approve", source="web")
    row = _row(db, aid)
    assert res["status"] == "open" == row["status"] and row["decided_at"]
    [ev] = _events(db)
    assert (ev["action"], ev["source"], ev["old_status"], ev["new_status"]) == (
        "approve", "web", start, "open")


@pytest.mark.parametrize("start", ["open", "done"])
def test_approve_from_open_or_done_is_invalid(db: str, start: str):
    aid = _add(db, status=start)
    with pytest.raises(ValueError):
        cm.apply_action(db, aid, "approve", source="web")
    assert _row(db, aid)["status"] == start and _events(db) == []


@pytest.mark.parametrize("start,ok", [("proposed", True), ("expired", True), ("open", False),
                                      ("legacy", False), ("rejected", False), ("done", False)])
def test_reject_only_from_proposed_or_expired(db: str, start: str, ok: bool):
    aid = _add(db, status=start)
    if ok:
        cm.apply_action(db, aid, "reject", source="bot")
        row = _row(db, aid)
        assert row["status"] == "rejected" and row["decided_at"]
        assert _events(db)[0]["source"] == "bot"
    else:
        with pytest.raises(ValueError):
            cm.apply_action(db, aid, "reject", source="bot")
        assert _row(db, aid)["status"] == start


def test_undo_approve_restores_status_and_decided_at(db: str):
    aid = _add(db)
    ev = cm.apply_action(db, aid, "approve", source="bot")
    cm.undo_action(db, ev["event_id"], source="bot")
    row = _row(db, aid)
    assert row["status"] == "proposed" and row["decided_at"] is None


def test_edit_changes_text_and_date_and_undo_restores(db: str):
    aid = _add(db, due_date="2026-11-01", due_precision="week")
    ev = cm.apply_action(db, aid, "edit", source="mcp", text="Надіслати договір юристу",
                         due="2026-11-05")
    row = _row(db, aid)
    assert (row["task"], row["due_date"], row["due_precision"], row["status"]) == (
        "Надіслати договір юристу", "2026-11-05", "day", "proposed")
    [e] = _events(db)
    assert e["source"] == "mcp" and e["action"] == "edit"
    assert json.loads(e["payload_json"]) == {"task": "Надіслати договір",
                                             "due_date": "2026-11-01"}
    cm.undo_action(db, ev["event_id"], source="mcp")
    row = _row(db, aid)
    assert (row["task"], row["due_date"], row["due_precision"]) == (
        "Надіслати договір", "2026-11-01", "week")


@pytest.mark.parametrize("action", ["open", "postpone:7"])
@pytest.mark.parametrize("start", ["proposed", "expired", "rejected", "legacy"])
def test_open_or_postpone_on_undecided_is_invalid(db: str, start: str, action: str):
    # Only `approve` makes an undecided row live: a plain open would skip decided_at
    # and the approve event, so the owner's Yes would never be recorded.
    aid = _add(db, status=start)
    with pytest.raises(ValueError):
        cm.apply_action(db, aid, action, source="mcp", dedupe=True)
    row = _row(db, aid)
    assert row["status"] == start and row["decided_at"] is None and _events(db) == []


@pytest.mark.parametrize("kw", [{}, {"due": "next friday"}, {"text": "   "}])
def test_edit_without_valid_change_is_invalid(db: str, kw: dict):
    aid = _add(db)
    with pytest.raises(ValueError):
        cm.apply_action(db, aid, "edit", source="web", **kw)
    assert _events(db) == []


def test_failing_event_write_leaves_row_unchanged(db: str):
    aid = _add(db)
    with get_db_connection(db) as conn:
        conn.execute("CREATE TRIGGER fail_ev BEFORE INSERT ON action_item_events "
                     "BEGIN SELECT RAISE(ABORT, 'boom'); END")
        conn.commit()
    with pytest.raises(sqlite3.Error):
        cm.apply_action(db, aid, "approve", source="web")
    row = _row(db, aid)
    assert row["status"] == "proposed" and row["decided_at"] is None


def test_invalid_transition_error_code_is_409():
    assert ERROR_CODES["INVALID_TRANSITION"] == 409


# ---------------------------------------------------------------- show and expiry

def test_mark_shown_sets_once_with_one_event(db: str):
    aid = _add(db)
    other = _add(db, "Інше", status="open")
    now = datetime(2026, 10, 27, 9, 0, 0)
    assert cm.mark_shown(db, [aid, other], source="bot", now=now) == [aid]
    assert _row(db, aid)["first_shown_at"] == "2026-10-27T09:00:00"
    assert _row(db, other)["first_shown_at"] is None
    assert cm.mark_shown(db, [aid], source="web", now=now + timedelta(days=1)) == []
    evs = _events(db)
    assert [(e["item_id"], e["action"], e["source"]) for e in evs] == [(aid, "show", "bot")]
    assert _row(db, aid)["first_shown_at"] == "2026-10-27T09:00:00"


def test_expire_proposals_only_shown_window_passed(db: str):
    now = datetime(2026, 10, 27, 9, 0, 0)
    old = _add(db, "Старе", first_shown_at="2026-10-24T09:00:00")
    fresh = _add(db, "Свіже", first_shown_at="2026-10-25T09:00:00")
    never = _add(db, "Не показане")
    approved = _add(db, "Затверджене", status="open", first_shown_at="2026-10-01T09:00:00",
                    decided_at="2026-10-02T09:00:00")
    dry = cm.expire_proposals(db, now=now, dry_run=True)
    assert dry["expired"] == 1 and dry["ids"] == [old] and _events(db) == []
    assert _row(db, old)["status"] == "proposed"
    res = cm.expire_proposals(db, now=now)
    assert res["ids"] == [old]
    assert _row(db, old)["status"] == "expired"
    for aid, st in ((fresh, "proposed"), (never, "proposed"), (approved, "open")):
        assert _row(db, aid)["status"] == st
    [ev] = _events(db)
    assert (ev["item_id"], ev["action"], ev["source"]) == (old, "expire", "system")


# ---------------------------------------------------------------- queue

def test_proposal_queue_order_dups_limit_exclude(db: str):
    late = _add(db, "Пізня", due_date="2026-11-20", importance=3)
    soon = _add(db, "Рання", due_date="2026-10-30", importance=1)
    low = _add(db, "Без дати низька", importance=1)
    high = _add(db, "Без дати висока", importance=3)
    mid = _add(db, "Без дати середня", importance=2)
    dup = _add(db, "Повтор", importance=3, dup_of=high)
    _add(db, "Відкрита", status="open", due_date="2026-10-28")
    ids = [r["id"] for r in cm.proposal_queue(db)]
    assert ids == [soon, late, high, mid, low] and dup not in ids
    assert [r["id"] for r in cm.proposal_queue(db, limit=2)] == [soon, late]
    assert [r["id"] for r in cm.proposal_queue(db, exclude_ids=[soon, high])] == [late, mid, low]
    row = cm.proposal_queue(db, limit=1)[0]
    assert {"for_whom", "confidence", "importance", "first_shown_at", "owner",
            "meeting"} <= set(row)


# ---------------------------------------------------------------- guards

def test_mark_stale_and_sweep_leave_decided_rows(db: str):
    approved = _add(db, "Затверджена давно", status="open", tid=2,
                    decided_at="2026-10-01T10:00:00")
    plain = _add(db, "Розкопана стара", status="open", tid=2)
    cm.mark_stale(db, today=TODAY)
    assert _row(db, approved)["status"] == "open"
    assert _row(db, plain)["status"] == "stale"
    with get_db_connection(db) as conn:
        conn.execute("UPDATE action_items SET status = 'open' WHERE id = ?", (plain,))
        conn.commit()
    res = cm.sweep_dropped(db, days=400, min_age_days=1, today=TODAY, sources=["all"])
    assert res["candidates"] == 1           # the plain row only; the approved one is never judged


def test_dedup_marks_repeated_proposal_but_never_a_decided_row(db: str):
    approved = _add(db, "Підготувати звіт для інвесторів фонду", status="open",
                    owner_name="Андрій", decided_at="2026-10-21T10:00:00", tid=2)
    repeat = _add(db, "Підготувати звіт для інвесторів фонду", owner_name="Андрій")
    cm.dedup(db)
    assert _row(db, approved)["dup_of"] is None
    assert _row(db, repeat)["dup_of"] == approved


def test_dedup_marks_repeated_proposals_among_themselves(db: str):
    a = _add(db, "Зателефонувати бухгалтеру щодо звіту", tid=2)
    b = _add(db, "Зателефонувати бухгалтеру щодо звіту")
    cm.dedup(db)
    assert _row(db, a)["dup_of"] == b and _row(db, b)["dup_of"] is None


# ---------------------------------------------------------------- readers

@pytest.mark.parametrize("status", list(cm.STATUSES) + ["all"])
def test_list_commitments_accepts_every_status(db: str, status: str):
    for st in cm.STATUSES:
        _add(db, f"Задача {st}", status=st)
    rows = cm.list_commitments(db, window="all", status=status)
    want = set(cm.STATUSES) if status == "all" else {status}
    assert {r["status"] for r in rows} == want
    assert cm.count_commitments(db, window="all", status=status) == len(rows)


def test_default_open_excludes_proposals_and_coverage_counts_approved(db: str):
    for st in ("proposed", "expired", "rejected", "legacy"):
        _add(db, f"Задача {st}", status=st)
    live = _add(db, "Жива", status="open", decided_at="2026-10-26T10:00:00")
    assert [r["id"] for r in cm.list_commitments(db, window="all")] == [live]
    assert cm.commitments_coverage(db)["total"] == 1


# ---------------------------------------------------------------- digest

def test_weekly_digest_stuck_and_proposals(db: str):
    old = _add(db, "Давно затверджена", status="open", decided_at="2026-10-10T10:00:00")
    overdue = _add(db, "Прострочена", status="open", decided_at="2026-10-26T10:00:00",
                   due_date="2026-10-25", due_precision="day")
    _add(db, "Свіжа", status="open", decided_at="2026-10-20T10:00:00")
    _add(db, "Пропозиція", due_date="2026-10-01")
    dig = cm.weekly_digest(db, today=TODAY)
    assert [r["id"] for r in dig["stuck"]] == [overdue, old]
    assert overdue in [r["id"] for r in dig["overdue"]]
    assert set(dig["stuck"][0]) == set(dig["overdue"][0])
    assert dig["proposals"] == {"shown": 0, "pressed": 0, "review_days": 0, "active": 3,
                                "window_days": 7}


def test_proposals_counters_from_events(db: str):
    a, b, c = _add(db, "А"), _add(db, "Б"), _add(db, "В")
    cm.mark_shown(db, [a, b, c], source="bot", now=datetime(2026, 10, 25, 9))
    cm.apply_action(db, a, "approve", source="bot")
    cm.apply_action(db, b, "reject", source="bot")
    with get_db_connection(db) as conn:     # spread the presses over two days
        conn.execute("UPDATE action_item_events SET at = '2026-10-26T10:00:00' "
                     "WHERE action = 'reject'")
        conn.execute("UPDATE action_item_events SET at = '2026-10-27T10:00:00' "
                     "WHERE action = 'approve'")
        conn.commit()
    p = cm.weekly_digest(db, today=TODAY)["proposals"]
    assert p == {"shown": 3, "pressed": 2, "review_days": 2, "active": 1, "window_days": 7}


# ---------------------------------------------------------------- unlegacy

def test_unlegacy_dry_run_then_restore(db: str):
    ids = {st: _add(db, f"Стара {st}", status="legacy", pre_legacy_status=st)
           for st in ("open", "stale", "done", "cancelled")}
    assert cm.unlegacy(db, dry_run=True) == 4
    assert {_row(db, i)["status"] for i in ids.values()} == {"legacy"}
    assert cm.unlegacy(db) == 4
    for st, i in ids.items():
        row = _row(db, i)
        assert (row["status"], row["pre_legacy_status"]) == (st, None)
    assert cm.unlegacy(db, dry_run=True) == 0


def test_unlegacy_cli_accepts_db_after_subcommand(db: str, capsys):
    _add(db, "Стара", status="legacy", pre_legacy_status="open")
    assert cm.main(["unlegacy", "--dry-run", "--db", db]) == 0
    assert capsys.readouterr().out.strip() == "1"
    assert _row(db, 1)["status"] == "legacy"
