"""Recording SPA guards: the REC badge poll and the recovered-recordings banner in shell.js.

Static checks on the source, like test_copilot_opt_in.py. The recovered endpoint clears its log
on read, so the SPA must read it with ack=0 and send ack=1 only after the banner is rendered.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL_JS = ROOT / "static" / "js" / "recall" / "shell.js"
RECOVERED = "/api/recordings/recovered"


def _src():
    return SHELL_JS.read_text(encoding="utf-8")


def test_recovered_is_never_read_without_ack_flag():
    # Comments may name the endpoint; only code counts as a read.
    src = re.sub(r"(^|\s)//[^\n]*", r"\1", _src(), flags=re.M)
    reads = [m.start() for m in re.finditer(re.escape(RECOVERED), src)]
    assert reads, "shell.js does not read the recovered endpoint"
    for pos in reads:
        assert re.match(re.escape(RECOVERED) + r"\?ack=[01]\b", src[pos:]), \
            f"recovered read without an ack flag at offset {pos}"
    assert RECOVERED + "?ack=0" in src


def test_ack_request_follows_the_banner_render():
    src = _src()
    fn = src.index("async function checkRecovered")
    render_def = src.index("function renderRecoveredBanner")
    ack = src.index(RECOVERED + "?ack=1")
    render_call = src.index("renderRecoveredBanner(", fn)
    assert fn < ack < render_def, "ack=1 must be sent from checkRecovered"
    assert render_call < ack, "ack=1 precedes the banner render in source order"


def test_badge_poll_has_hidden_tab_guard():
    src = _src()
    m = re.search(r"async function pollRecBadge\(\) \{(.*?)\n    \}", src, re.S)
    assert m, "pollRecBadge not found"
    assert "if (document.hidden) return;" in m.group(1)
    assert "setInterval(pollRecBadge" in src
    start = src.index("function startRecBadge")
    assert "visibilitychange" in src[start:src.index("async function pollRecBadge")]


# ---- record view (wave2-recording-calls-06): honest interruption, pause/resume resync ----

RECORD_JS = ROOT / "static" / "js" / "recall" / "views" / "record.js"
SW_JS = ROOT / "static" / "sw.js"


def _record():
    return RECORD_JS.read_text(encoding="utf-8")


def _fn_body(src, head):
    start = src.index(head)
    end = src.index("\n    }\n", start)
    return src[start:end]


def test_resync_reads_state_and_stops_on_a_gone_session():
    body = _fn_body(_record(), "async function resyncState(")
    assert "'/state'" in body
    assert "markInterrupted(" in body
    for status in ("'crashed'", "'discarded'", "'finalized'"):
        assert status in body, status
    mark = _fn_body(_record(), "function markInterrupted(")
    assert "haltS(S)" in mark, "an interrupted session must stop its SSE reader and timer"


def test_reconnecting_and_permanent_fail_read_state():
    src = _record()
    conn = _fn_body(src, "function connectSSE(")
    for cb in ("onReconnecting:", "onPermanentFail:"):
        part = conn[conn.index(cb):]
        part = part[:part.index("},")]
        assert "resyncState(" in part, f"{cb} does not re-read /state"
    assert "if (S.interrupted) return;" in conn


def test_pause_resume_catch_reads_state():
    body = _fn_body(_record(), "async function pauseResume(")
    catch = body[body.index("catch (err)"):]
    assert "resyncState(" in catch
    assert "err.status === 404" in catch and "markInterrupted(" in catch


def test_sw_version_bumped():
    assert "const VERSION = 'recall-v97';" in SW_JS.read_text(encoding="utf-8")


# ---- wave4-client-calls-07: consent line, "What did I miss", the started plate ----

def test_record_view_posts_live_missed_and_shows_consent():
    src = _record()
    assert "'/api/copilot/live-missed'" in src
    assert "'capture.rec.consent'" in src
    assert "consent_reminder" in _fn_body(src, "function applyStateFlags(")


def test_missed_button_is_gated_on_started_by():
    src = _record()
    paint = _fn_body(src, "function paintMissed(")
    assert "st.startedBy" in paint and "OWNER_STARTERS" in paint
    assert "started_by" in _fn_body(src, "function applyStateFlags(")
    ask = _fn_body(src, "async function askMissed(")
    assert "'AGENT_SESSION'" in ask and "missedOff = true" in ask


def test_started_plate_reads_consent_reminder():
    src = _src()
    start = src.index("function showCallPrompt(")
    body = src[start:src.index("\n    }\n", start)]
    assert "consent_reminder" in body and "'core.call.consent'" in body
