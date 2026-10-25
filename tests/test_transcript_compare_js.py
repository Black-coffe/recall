"""static/js/recall/compare.js: pure alignment and word diff, driven through node."""
import json
import shutil
import subprocess
from pathlib import Path

COMPARE = Path(__file__).resolve().parents[1] / "static" / "js" / "recall" / "compare.js"

_RUN = r"""
const c = require(process.argv[1]);
const a = [
  {start: 0, end: 4, text: "Зустріч у вівторок, о десятій."},
  {start: 4, end: 8, text: "Це зайва фраза."},
  {start: 20, end: 24, text: "Дякую всім."},
];
const b = [
  {start: 0.5, end: 3.9, text: "Зустріч у четвер, о десятій."},
  {start: 12, end: 15, text: "Фраза тільки тут."},
  {start: 20, end: 24, text: "Дякую всім."},
];
const rows = c.align(a, b);
const d = c.diffWords(a[0].text, b[0].text);
console.log(JSON.stringify({rows: rows, d: d}));
"""


def _run():
    node = shutil.which("node")
    assert node, "node is required to test compare.js"
    out = subprocess.run([node, "-e", _RUN, str(COMPARE)], capture_output=True, text=True,
                         encoding="utf-8", timeout=30, check=True)
    return json.loads(out.stdout)


def test_overlapping_phrases_pair_and_orphans_get_null_cells():
    rows = _run()["rows"]
    assert len(rows) == 4
    assert rows[0]["a"]["start"] == 0 and rows[0]["b"]["start"] == 0.5
    assert rows[1]["a"]["text"] == "Це зайва фраза." and rows[1]["b"] is None
    assert rows[2]["a"] is None and rows[2]["b"]["text"] == "Фраза тільки тут."
    assert rows[3]["a"]["text"] == rows[3]["b"]["text"] == "Дякую всім."


def test_only_differing_words_are_marked():
    d = _run()["d"]
    assert [w["text"] for w in d["a"] if w["diff"]] == ["вівторок,"]
    assert [w["text"] for w in d["b"] if w["diff"]] == ["четвер,"]
