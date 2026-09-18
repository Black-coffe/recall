"""Смок-тести `evals/compare.py` (Хвиля B, історія 06) — синтетичні JSON у
формі `evals.gate --json-out`. Сам гейт тут НЕ запускається: JSON будуються
руками, як задокументовано у acceptance criteria цієї історії.
"""
import json

import pytest

from evals import compare


# ============================================================
# хелпери
# ============================================================

def _golden_jsonl(tmp_path, items, name="golden.jsonl"):
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return str(path)


def _row(item_id, recall, hit, *, slice_="calls", status="labeled"):
    return {
        "id": item_id,
        "recall_at_k": recall,
        "source_name_hit": None,
        "has_citation": None,
        "citations_valid": None,
        "slice": slice_,
        "status": status,
        "hit": hit,
    }


def _gate_result(tmp_path, *, name, golden, k_list, per_item_by_k, db="snap.db"):
    aggregate = {}
    for k in k_list:
        rows = per_item_by_k.get(k, [])
        recalls = [r["recall_at_k"] for r in rows if r.get("recall_at_k") is not None]
        aggregate[str(k)] = {
            "recall_at_k": (sum(recalls) / len(recalls)) if recalls else None,
            "source_name_hit_rate": None,
            "n": len(rows),
        }
    out = {
        "db": db,
        "golden": golden,
        "k": k_list,
        "verdict": "PASS",
        "failures": [],
        "aggregate": aggregate,
        "by_slice": {str(k): {} for k in k_list},
        "per_item": {str(k): per_item_by_k.get(k, []) for k in k_list},
    }
    path = tmp_path / name
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# ============================================================
# тести
# ============================================================

def test_compare_deltas_and_both_diff_directions(tmp_path, capsys):
    golden = _golden_jsonl(tmp_path, [
        {"id": "q1", "question": "Коли зустріч з Андрієм щодо бюджету?", "slice": "calls",
         "status": "labeled", "expected_transcription_ids": [1]},
        {"id": "q2", "question": "Хто відповідальний за звіт?", "slice": "calls",
         "status": "labeled", "expected_transcription_ids": [2]},
        {"id": "q3", "question": "Яка дата дедлайну проєкту?", "slice": "calls",
         "status": "labeled", "expected_transcription_ids": [3]},
    ])
    per_item_a = {
        8: [_row("q1", 1.0, True), _row("q2", 0.0, False), _row("q3", 1.0, True)],
        12: [_row("q1", 1.0, True), _row("q2", 0.0, False), _row("q3", 1.0, True)],
    }
    per_item_b = {
        8: [_row("q1", 0.0, False), _row("q2", 1.0, True), _row("q3", 1.0, True)],
        12: [_row("q1", 0.0, False), _row("q2", 1.0, True), _row("q3", 1.0, True)],
    }
    a = _gate_result(tmp_path, name="a.json", golden=golden, k_list=[8, 12], per_item_by_k=per_item_a)
    b = _gate_result(tmp_path, name="b.json", golden=golden, k_list=[8, 12], per_item_by_k=per_item_b)

    rc = compare.main([a, b, "--label-a", "e5", "--label-b", "qwen3"])
    assert rc == 0
    out = capsys.readouterr().out

    # Обидва напрямки diff, з id і питанням ≤80 символів.
    assert "hit→miss" in out and "miss→hit" in out
    assert "q1" in out and "Андрієм" in out  # hit→miss
    assert "q2" in out and "звіт" in out  # miss→hit
    # Дельта по обох k (recall 66.7% в обох напрямах, симетрично 0 сумарно
    # на середньому, але кожен k має власний рядок таблиці).
    assert "e5" in out and "qwen3" in out
    assert "k=8" in out.replace(" ", "") or "8" in out
    assert "k=12" in out.replace(" ", "") or "12" in out


def test_compare_different_item_sets_warns_and_compares_intersection(tmp_path, capsys):
    golden_a = _golden_jsonl(tmp_path, [
        {"id": "q1", "question": "Питання перше", "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [1]},
        {"id": "q_only_a", "question": "Питання лише в A", "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [2]},
    ], name="golden_a.jsonl")
    golden_b = _golden_jsonl(tmp_path, [
        {"id": "q1", "question": "Питання перше", "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [1]},
        {"id": "q_only_b", "question": "Питання лише в B", "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [3]},
    ], name="golden_b.jsonl")

    per_item_a = {8: [_row("q1", 1.0, True), _row("q_only_a", 1.0, True)]}
    per_item_b = {8: [_row("q1", 1.0, True), _row("q_only_b", 0.0, False)]}
    a = _gate_result(tmp_path, name="a.json", golden=golden_a, k_list=[8], per_item_by_k=per_item_a)
    b = _gate_result(tmp_path, name="b.json", golden=golden_b, k_list=[8], per_item_by_k=per_item_b)

    rc = compare.main([a, b])
    assert rc == 0
    out = capsys.readouterr().out
    assert "набори різняться" in out
    assert "q_only_a" in out and "не порівняно" in out
    assert "q_only_b" in out
    assert "спільних пунктів: 1" in out


def test_compare_no_common_k_exits_2_without_traceback(tmp_path, capsys):
    golden = _golden_jsonl(tmp_path, [
        {"id": "q1", "question": "Питання", "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [1]},
    ])
    a = _gate_result(tmp_path, name="a.json", golden=golden, k_list=[8],
                      per_item_by_k={8: [_row("q1", 1.0, True)]})
    b = _gate_result(tmp_path, name="b.json", golden=golden, k_list=[12],
                      per_item_by_k={12: [_row("q1", 1.0, True)]})

    rc = compare.main([a, b])
    assert rc == 2
    err = capsys.readouterr().err
    assert "спільного k" in err
    assert "Traceback" not in err


def test_compare_unreadable_file_exits_2(tmp_path, capsys):
    golden = _golden_jsonl(tmp_path, [
        {"id": "q1", "question": "Питання", "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [1]},
    ])
    a = _gate_result(tmp_path, name="a.json", golden=golden, k_list=[8],
                      per_item_by_k={8: [_row("q1", 1.0, True)]})
    missing = str(tmp_path / "does_not_exist.json")

    rc = compare.main([a, missing])
    assert rc == 2
    err = capsys.readouterr().err
    assert "не вдалось прочитати" in err
    assert "Traceback" not in err


def test_compare_json_out_writes_structured_diff(tmp_path):
    golden = _golden_jsonl(tmp_path, [
        {"id": "q1", "question": "Питання перше", "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [1]},
        {"id": "q2", "question": "Питання друге", "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [2]},
    ])
    per_item_a = {8: [_row("q1", 1.0, True), _row("q2", 1.0, True)]}
    per_item_b = {8: [_row("q1", 0.0, False), _row("q2", 1.0, True)]}
    a = _gate_result(tmp_path, name="a.json", golden=golden, k_list=[8], per_item_by_k=per_item_a)
    b = _gate_result(tmp_path, name="b.json", golden=golden, k_list=[8], per_item_by_k=per_item_b)
    json_out = tmp_path / "compare_out.local.json"

    rc = compare.main([a, b, "--json-out", str(json_out)])
    assert rc == 0

    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["common_k"] == [8]
    assert data["common_count"] == 2
    assert data["table"]["8"]["a"] == pytest.approx(1.0)
    assert data["table"]["8"]["b"] == pytest.approx(0.5)
    assert data["table"]["8"]["delta"] == pytest.approx(-0.5)
    hit_to_miss_ids = [e["id"] for e in data["diff"]["8"]["hit_to_miss"]]
    assert hit_to_miss_ids == ["q1"]
    assert data["diff"]["8"]["miss_to_hit"] == []
    assert data["only_in_a"] == []
    assert data["only_in_b"] == []


def test_compare_question_truncated_to_80_chars(tmp_path, capsys):
    long_question = "Дуже довге питання про архів дзвінків " * 5  # > 80 символів
    golden = _golden_jsonl(tmp_path, [
        {"id": "q1", "question": long_question, "slice": "calls", "status": "labeled",
         "expected_transcription_ids": [1]},
    ])
    per_item_a = {8: [_row("q1", 1.0, True)]}
    per_item_b = {8: [_row("q1", 0.0, False)]}
    a = _gate_result(tmp_path, name="a.json", golden=golden, k_list=[8], per_item_by_k=per_item_a)
    b = _gate_result(tmp_path, name="b.json", golden=golden, k_list=[8], per_item_by_k=per_item_b)
    json_out = tmp_path / "out.local.json"

    rc = compare.main([a, b, "--json-out", str(json_out)])
    assert rc == 0
    data = json.loads(json_out.read_text(encoding="utf-8"))
    shown = data["diff"]["8"]["hit_to_miss"][0]["question"]
    assert len(shown) <= 80
    assert shown.endswith("…")
