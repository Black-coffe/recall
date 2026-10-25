"""/record: the live copilot is opt-in per call (copilot-opt-in).

`collectCopilot` is cut out of views/record.js and run through node against a fake mount:
without the checkbox the start payload gets no `copilot`, so the server starts none.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "static" / "js" / "recall" / "views" / "record.js"
CAPTURE = ROOT / "static" / "js" / "recall" / "i18n" / "capture.js"

_RUN = r"""
const src = process.argv[1];
const fields = JSON.parse(process.argv[2]);
const ctx = { mount: { querySelector: (id) => fields[id] === undefined ? null : fields[id] } };
const _cpAvail = { enabled: true };
const collectCopilot = new Function('_cpAvail', src + '\nreturn collectCopilot;')(_cpAvail);
console.log(JSON.stringify(collectCopilot(ctx)));
"""


def _collect(fields: dict):
    src = RECORD.read_text(encoding="utf-8")
    m = re.search(r"\n    function collectCopilot\(ctx\) \{.*?\n    \}\n", src, re.S)
    assert m, "collectCopilot not found in record.js"
    node = shutil.which("node")
    assert node, "node is required"
    out = subprocess.run([node, "-e", _RUN, m.group(0), json.dumps(fields)], capture_output=True,
                         text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(out.stdout)


PANEL = {"#rcCpMode": {"value": "hard"}, "#rcCpImportance": {"value": "high"},
         "#rcCpVector": {"value": "7"}, "#rcCpScope": {"value": ""},
         "#rcCpLocalOnly": {"checked": False}, "#rcCpBudget": {"value": ""}}


def test_unchecked_sends_no_copilot():
    assert _collect({**PANEL, "#rcCpOn": {"checked": False}}) is None


def test_checked_sends_the_panel_config():
    cp = _collect({**PANEL, "#rcCpOn": {"checked": True}})
    assert cp == {"mode": "hard", "importance": "high", "category_id": "7"}


def test_checkbox_renders_unchecked_and_fields_start_disabled():
    src = RECORD.read_text(encoding="utf-8")
    assert re.search(r'<input type="checkbox" id="rcCpOn">', src)
    assert '<fieldset id="rcCpFields" disabled' in src


def test_label_in_both_languages():
    assert CAPTURE.read_text(encoding="utf-8").count('"capture.rec.cp.enable":') == 2
