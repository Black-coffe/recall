"""Guard: every surface that enumerates task statuses agrees with `commitments.STATUSES` (ADR-056).

A ninth status must not appear on one surface only. The surfaces are read from source text
or from the module that owns them; nothing runs over HTTP and no database is opened.

1. commitments.py: the decision transitions stay inside STATUSES, and every status is named
   somewhere in the module outside the STATUSES tuple itself (a SQL write, a transition or an
   event). A value that only sits in the tuple fails here.
2. memory.py: the `status=` filter checks `commitments.STATUSES`; the PATCH body accepts
   open|done|cancelled, and the rest is excluded by name below. A new value must be decided
   on here, not silently left out.
3. mcp_server.py: `_LIST_TASK_STATUSES` is STATUSES plus `all`.
4. static/js/recall/views/tasks.js: the chips are STATUSES plus `all`; APPROVE_FROM and
   REJECT_FROM mirror the decision transitions.
5. pipes_cards.py: the proposal card names `expired`, its only status line, and the catalog
   key it renders exists. The card has no per-status label list, so a new status does not
   fail this surface (see the story CONCERNS).

The decision actions (approve, reject) must be named by memory.py, brief_actions.py and mcp_server.py.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.services import commitments

ROOT = Path(__file__).resolve().parent.parent
STATUSES = tuple(commitments.STATUSES)
STATUS_SET = set(STATUSES)

# Statuses a PATCH body may not set: proposal states move only through approve/reject and the
# daily pass, and `stale`/`legacy` only through the machine. Written here so that a new value
# has to be decided on explicitly.
_PATCH_BODY_EXCLUDED = {"proposed", "stale", "rejected", "expired", "legacy"}


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _module_tuple(rel: str, name: str) -> tuple:
    """Literal value of a module-level `name = (...)` assignment, read from the AST."""
    for node in ast.parse(_read(rel)).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{rel}: no module-level assignment to {name}")


def _quoted(word: str, text: str) -> bool:
    return re.search(rf"""["']{re.escape(word)}["']""", text) is not None


def test_status_tuple_is_unique_and_nonempty():
    assert len(STATUSES) == len(STATUS_SET) == 8


def test_decision_transitions_stay_inside_statuses():
    assert set(commitments._DECISION_FROM) == set(commitments.DECISION_ACTIONS)
    assert set(commitments._DECISION_TO) == set(commitments.DECISION_ACTIONS)
    named = {s for sources in commitments._DECISION_FROM.values() for s in sources}
    named |= set(commitments._DECISION_TO.values())
    assert named <= STATUS_SET, f"transition names unknown status: {named - STATUS_SET}"


def test_every_status_is_named_by_the_machine_outside_the_tuple():
    lines = [ln for ln in _read("app/services/commitments.py").splitlines()
             if not ln.startswith("STATUSES = ")]
    body = "\n".join(lines)
    missing = [s for s in STATUSES if not _quoted(s, body)]
    assert not missing, f"commitments.py never names these statuses outside STATUSES: {missing}"


def test_memory_status_filter_reads_the_enum():
    text = _read("app/blueprints/memory.py")
    assert "status not in commitments.STATUSES" in text


def test_memory_patch_accepts_the_non_proposal_statuses_only():
    text = _read("app/blueprints/memory.py")
    m = re.search(r"def update_action_item\(.*?(?=\n@memory_bp)", text, re.S)
    assert m is not None, "update_action_item not found in memory.py"
    accepted = re.search(r"status not in \(([^)]*)\)", m.group(0))
    assert accepted is not None, "PATCH status check not found"
    patch_set = set(re.findall(r"""["'](\w+)["']""", accepted.group(1)))
    assert patch_set <= STATUS_SET, f"PATCH accepts unknown status: {patch_set - STATUS_SET}"
    assert STATUS_SET - patch_set == _PATCH_BODY_EXCLUDED


def test_memory_patch_names_both_decisions_and_edit():
    actions = _module_tuple("app/blueprints/memory.py", "_PATCH_ACTIONS")
    assert set(commitments.DECISION_ACTIONS) <= set(actions)
    assert "edit" in actions


def test_mcp_list_action_items_enum_is_statuses_plus_all():
    enum = _module_tuple("mcp_server.py", "_LIST_TASK_STATUSES")
    assert len(enum) == len(set(enum))
    assert set(enum) == STATUS_SET | {"all"}


def test_spa_chips_are_statuses_plus_all():
    js = _read("static/js/recall/views/tasks.js")
    block = re.search(r"const STATUSES = \[(.*?)\];", js, re.S)
    assert block is not None, "chip list not found in tasks.js"
    chips = re.findall(r"k: '(\w+)'", block.group(1))
    assert len(chips) == len(set(chips))
    assert set(chips) == STATUS_SET | {"all"}


def test_spa_decision_gates_mirror_the_transitions():
    js = _read("static/js/recall/views/tasks.js")
    for const, action in (("APPROVE_FROM", "approve"), ("REJECT_FROM", "reject")):
        m = re.search(rf"const {const} = \[(.*?)\];", js, re.S)
        assert m is not None, f"{const} not found in tasks.js"
        assert set(re.findall(r"'(\w+)'", m.group(1))) == set(commitments._DECISION_FROM[action])


def test_pipes_cards_names_the_expired_line_and_its_catalog_key():
    assert "proposals.expired" in _read("app/services/pipes_cards.py")
    assert '"telegram.brief.proposals.expired"' in _read("app/core/i18n_catalog/telegram.py")


def test_decision_actions_are_the_literals_the_surfaces_accept():
    assert commitments.DECISION_ACTIONS == ("approve", "reject")
    for rel in ("app/blueprints/memory.py", "app/services/brief_actions.py", "mcp_server.py"):
        text = _read(rel)
        for action in commitments.DECISION_ACTIONS:
            assert _quoted(action, text), f"{rel} does not name the decision {action!r}"
