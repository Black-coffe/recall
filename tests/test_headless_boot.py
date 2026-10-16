"""Roadmap criterion S3 (config-registry-profiles-04) as a subprocess test.

`RECALL_PROFILE=headless FORCE_CPU=1 python app.py` must come up without
`pyaudiowpatch`/torch-GPU and serve `/api/ready` 200 with `ready=true`. The test
launches a REAL child `app.py` process (not `test_client()`, like
`test_endpoints_smoke.py`) — only this way is the boot sequence itself verified
(module-level code, `__main__`, `app.run()`), not blueprints in isolation.

Marker `slow`: the child process imports torch/faster-whisper and brings up a
real HTTP server — tens of seconds, not part of `-m "not slow"`.

Isolation of the live DB: the child process `cwd` is a temporary directory, and
`config.py::Config.DATABASE` is a relative path (`'whisper_history.db'`,
`'test_whisper_history.db'` under `APP_ENV=testing`), so the sqlite file
is created in tmp_path, never in the project root. The env `DATABASE=<tmp>` itself
from this story's acceptance is NOT read by any code (checked by grep —
`config.py` holds a hardcode, and `app/services/comments.py` reads the same
env for an unrelated feature) — this is a documented gap between the wording
of the criterion and the code, worked around via `cwd`+`APP_ENV=testing`, without edits to app.py
(a Non-goal of the story — not to patch the app).

Telegram gate (config-registry-fix-02): `config.py::TELEGRAM_SESSION` is built
via `str(BASE_DIR / settings.env('TELEGRAM_SESSION'))` (`config.py:269`) —
the registry gives the name, `BASE_DIR` gives an ABSOLUTE path, so `cwd=tmp_path` does not
close this gate — on the owner's machine with real `TELEGRAM_API_ID`/`HASH` in `.env`
and telethon installed the child process would bring up the live Telegram listener
(a separate subprocess, a live TG MTProto connection, writes to `telegram_media/` and
fights for `telegram.session`/port 5051 with the owner's live listener). So
`_spawn_headless` explicitly passes `TELEGRAM_SESSION=<tmp_path>/telegram` — the `.session`
file is not there and never will be. `_maybe_launch_telegram_listener` (`app.py:1156`)
checks `TELEGRAM_ENABLED` (keys+telethon) FIRST: no keys — the `disabled`
branch; keys present (as on the owner's machine) — the session file is checked next, and
its absence gives the `absent` branch. In both cases no Telegram subprocess
appears. Both tests assert `checks.telegram in ("disabled", "absent")`
— explicit proof, not hope. Cleanup (`_terminate`) does not rely on
the child's `atexit` (on Windows `terminate()`/`TerminateProcess` does not run it) —
it terminates/kills the whole process tree of the child PID via psutil
by PID (never by image name — `memory/never-taskkill-by-image-name.md`), and
the test explicitly checks that no pid from this tree survived the cleanup.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_PY = PROJECT_ROOT / "app.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_pyaudiowpatch_stub(tmp_path: Path) -> Path:
    """PYTHONPATH stub: `import pyaudiowpatch` raises ImportError.

    Independent of the child's `sys.modules` (it is a separate process) —
    proves that headless boot really does NOT try to import
    pyaudiowpatch, rather than simply "we do not have it installed".
    """
    stub_root = tmp_path / "pyaudiowpatch_stub"
    pkg = stub_root / "pyaudiowpatch"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "raise ImportError("
        "'pyaudiowpatch stub: headless boot must not import it')\n",
        encoding="utf-8",
    )
    return stub_root


def _get_json(port: int, path: str, timeout: float = 2.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        data = json.loads(body) if body else {}
        return resp.status, data
    finally:
        conn.close()


def _spawn_headless(tmp_path: Path, port: int, extra_env: dict | None = None) -> subprocess.Popen:
    stub_root = _make_pyaudiowpatch_stub(tmp_path)
    env = dict(os.environ)
    env.update({
        "RECALL_PROFILE": "headless",
        "FORCE_CPU": "1",
        "FLASK_PORT": str(port),
        "FLASK_HOST": "127.0.0.1",
        "FLASK_DEBUG": "false",
        "APP_ENV": "testing",
        "AUTO_CLEANUP_ENABLED": "false",
        "PYTHONPATH": str(stub_root) + os.pathsep + env.get("PYTHONPATH", ""),
        # Telegram gate (Wave 1, config-registry-fix-02): TELEGRAM_SESSION is
        # an absolute default in config.py, cwd=tmp_path does not close it. The file
        # <tmp_path>/telegram.session deliberately does not exist → _maybe_launch_telegram_
        # listener falls into 'absent' regardless of TELEGRAM_API_ID/HASH in the
        # owner's real .env — no Telegram subprocess starts.
        "TELEGRAM_SESSION": str(tmp_path / "telegram"),
    })
    # Explicitly do NOT trust localhost by default (headless fail-closed) —
    # the criterion does not need RECALL_API_KEY, because /api/ready is in the exempt list.
    env.pop("RECALL_API_KEY", None)
    env.pop("RECALL_LOCAL_TRUSTED", None)
    if extra_env:
        env.update(extra_env)
    log_path = tmp_path / "headless_boot.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP_PY)],
        cwd=str(tmp_path),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    proc._recall_log_file = log_file  # type: ignore[attr-defined]
    proc._recall_log_path = log_path  # type: ignore[attr-defined]
    return proc


def _terminate(proc: subprocess.Popen) -> list[int]:
    """Guaranteed cleanup of the child PID's process tree.

    Does not rely on the child's `atexit`: on Windows `terminate()`
    does `TerminateProcess`, which does NOT run `atexit` hooks (that is why
    `_stop_listener` in `app.py` does not help — we do not let the listener
    start at all, a gate on the env side). Terminates/kills the process ITSELF and all its
    descendants by PID via psutil (never by image name —
    `memory/never-taskkill-by-image-name.md`), with a hard `kill()` fallback
    after the `terminate()` timeout. Returns the tree's pids at the moment of the call —
    the caller checks explicitly that none of them survived the cleanup, rather than just
    trusting `proc.wait()`.
    """
    try:
        parent = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        parent = None
    tree = list(parent.children(recursive=True)) if parent is not None else []
    if parent is not None:
        tree.append(parent)
    pids = [p.pid for p in tree]

    for p in tree:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(tree, timeout=15)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=15)

    try:
        proc.wait(timeout=5)
    except Exception:
        pass

    log_file = getattr(proc, "_recall_log_file", None)
    if log_file is not None:
        log_file.close()

    return pids


def _assert_process_tree_gone(pids: list[int]) -> None:
    """Explicit cleanup check: no pid from the tree may be alive."""
    survivors = [pid for pid in pids if psutil.pid_exists(pid)]
    assert survivors == [], (
        f"processes survived the cleanup (pid not by image name): {survivors}"
    )


def _boot_log(proc: subprocess.Popen) -> str:
    log_path = getattr(proc, "_recall_log_path", None)
    if log_path is None or not log_path.exists():
        return "(no log)"
    return log_path.read_text(encoding="utf-8", errors="replace")[-4000:]


def _full_boot_log(proc: subprocess.Popen) -> str:
    """Full (untruncated) content of the boot log.

    `_boot_log` deliberately cuts to the last 4000 characters (a size for
    the diagnostic message in `pytest.fail`/assert). Tests that
    check SPECIFIC logger lines (e.g. the two `_warm_up` phrases or
    the validity of every JSON line) cannot rely on this tail —
    during long polling the Werkzeug access logs push the needed lines
    out of the 4000-character window.
    """
    log_path = getattr(proc, "_recall_log_path", None)
    if log_path is None or not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def test_headless_boot_ready_with_preload_disabled(tmp_path):
    """Roadmap criterion: headless + FORCE_CPU + no warm-up -> ready=true quickly."""
    port = _free_port()
    proc = _spawn_headless(tmp_path, port, extra_env={"RECALL_PRELOAD_WHISPER": "0"})
    try:
        deadline = time.time() + 120
        status, data = None, None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"child process exited prematurely (code {proc.returncode}):\n"
                    f"{_boot_log(proc)}"
                )
            try:
                status, data = _get_json(port, "/api/ready")
                if status == 200:
                    break
            except (ConnectionRefusedError, OSError):
                pass
            time.sleep(0.5)

        assert status == 200, f"/api/ready did not return 200 within 120s:\n{_boot_log(proc)}"
        assert data["ready"] is True
        assert data["profile"] == "headless"
        assert data["checks"]["whisper"]["preload"] == "disabled"
        # Telegram gate (config-registry-fix-02): TELEGRAM_SESSION points into
        # tmp_path without a .session file → the listener could not start regardless
        # of TELEGRAM_API_ID/HASH in this machine's real .env.
        assert data["checks"]["telegram"] in ("disabled", "absent"), (
            f"Telegram listener started in the test: {data['checks']['telegram']}"
        )

        # `/` is disabled in headless (the shell is not brought up).
        root_status, root_data = _get_json(port, "/")
        assert root_status == 404
        assert root_data.get("error") == "headless"
    finally:
        _assert_process_tree_gone(_terminate(proc))


def _preload_cache_status() -> tuple[bool, str]:
    """(is_cached, skip_reason) for `DEFAULT_PRELOAD_MODEL` in the local HF cache.

    Without a bare `except Exception` (finding N1): `ImportError`
    (no `whisper_manager_new`/`faster-whisper`) now surfaces as a test
    collection error, and is not quietly swallowed into the same `False` as "the model is not
    warmed up" — previously `skipif` was indistinguishable from a real
    import failure.
    """
    from whisper_manager_new import _hf_cache_base, _hf_models_map, _repo_cached
    from app.services.whisper_preload import DEFAULT_PRELOAD_MODEL

    repo = _hf_models_map().get(DEFAULT_PRELOAD_MODEL)
    if not repo:
        return False, (
            f"faster-whisper does not know a repo for model {DEFAULT_PRELOAD_MODEL!r} "
            "(check the installed faster-whisper version)"
        )
    cache_base = _hf_cache_base()
    cached = _repo_cached(repo)
    reason = (
        f"warm-up model {DEFAULT_PRELOAD_MODEL!r} (repo {repo!r}) is absent from "
        f"the HF cache {cache_base} of this machine — do not hit the network in a test"
    )
    return cached, reason


_PRELOAD_CACHED, _PRELOAD_SKIP_REASON = _preload_cache_status()


@pytest.mark.skipif(not _PRELOAD_CACHED, reason=_PRELOAD_SKIP_REASON)
def test_headless_boot_ready_waits_for_preload(tmp_path):
    """Without RECALL_PRELOAD_WHISPER=0: first 503 (the model is still loading), then 200.

    Proof that the process really went through the "model is loading" state does NOT rely
    on an HTTP polling race (the 503 window may be shorter than one poll — so
    this story's Non-goals forbid making `saw_not_ready` mandatory).
    Instead `_boot_log` is checked: both INFO lines of `_warm_up`
    (`whisper_preload.py:57-63`) must be in the child process log, and
    there must be no WARNING about a failed warm-up — this is proof through the product itself, not
    through the test's timing.
    """
    from app.services.whisper_preload import DEFAULT_PRELOAD_MODEL

    port = _free_port()
    proc = _spawn_headless(tmp_path, port)
    try:
        deadline = time.time() + 180
        preload_sequence: list[str] = []
        status, data = None, None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"child process exited prematurely (code {proc.returncode}):\n"
                    f"{_boot_log(proc)}"
                )
            try:
                status, data = _get_json(port, "/api/ready")
            except (ConnectionRefusedError, OSError):
                # (a) polling starts before HTTP comes up: connection refused -> retry,
                # step <= 50ms.
                time.sleep(0.05)
                continue
            if status == 200 and data.get("ready") is True:
                preload_sequence.append(
                    data.get("checks", {}).get("whisper", {}).get("preload")
                )
                break
            if status == 503:
                preload = data.get("checks", {}).get("whisper", {}).get("preload")
                preload_sequence.append(preload)
                # (b) every 503 carries preload=='loading' exactly: 'pending' is no longer
                # among the allowed, 'disabled'/'failed' here are a failure (робота
                # is not blocked by these states at all -- system.py:172-181
                # lets 200 through on both 'disabled' and 'loading' -- the only state that
                # holds 503 while whisper_manager exists and warm-up is enabled).
                assert preload == "loading", (
                    f"unexpected warm-up state in a 503 response "
                    f"(must be 'loading'): {data}"
                )
            time.sleep(0.05)

        assert status == 200, f"/api/ready did not become 200 within 180s:\n{_boot_log(proc)}"
        assert data["ready"] is True
        # (c) the final response.
        assert data["checks"]["whisper"]["preload"] == "ready"
        assert data["checks"]["whisper"]["model"] == DEFAULT_PRELOAD_MODEL
        # Telegram gate (config-registry-fix-02) — see the comment in the first test.
        assert data["checks"]["telegram"] in ("disabled", "absent"), (
            f"Telegram listener started in the test: {data['checks']['telegram']}"
        )

        # (e) if 'loading' was observed at least once — the final 'ready' comes
        # strictly after it in the response sequence.
        if "loading" in preload_sequence:
            ready_idx = len(preload_sequence) - 1
            assert preload_sequence[ready_idx] == "ready"
            last_loading_idx = max(
                i for i, v in enumerate(preload_sequence) if v == "loading"
            )
            assert last_loading_idx < ready_idx, (
                f"'ready' does not come strictly after 'loading': {preload_sequence}"
            )

        # (d) proof through the product itself, not through a polling race: both lines of
        # `_warm_up` are present in the boot log, and there is no warm-up failure.
        full_log = _full_boot_log(proc)
        loading_line = f"Whisper preload: loading model {DEFAULT_PRELOAD_MODEL} in the background..."
        ready_line_prefix = f"Whisper preload: model {DEFAULT_PRELOAD_MODEL} ready in"
        failed_line = f"Whisper preload: failed to warm up {DEFAULT_PRELOAD_MODEL}"
        assert loading_line in full_log, (
            f"boot log does not contain the warm-up start line {loading_line!r}:\n{full_log[-4000:]}"
        )
        assert ready_line_prefix in full_log, (
            f"boot log does not contain the warm-up completion line {ready_line_prefix!r}:\n"
            f"{full_log[-4000:]}"
        )
        assert failed_line not in full_log, (
            f"warm-up crashed with an exception, although success was expected:\n{full_log[-4000:]}"
        )
    finally:
        _assert_process_tree_gone(_terminate(proc))


def test_headless_boot_json_logs(tmp_path):
    """Criterion N2 (config-registry-fix-r3-02): end-to-end coverage of the
    `RECALL_LOG_FORMAT=json` gate (`app.py:239-245`) by a REAL child process —
    unlike the old `test_json_format_end_to_end_via_env`
    (`tests/test_json_logs.py`), which reproduced the same condition in the body of
    the test and so could not turn red from a breakage of `app.py` itself.
    `RECALL_PRELOAD_WHISPER=0` — the test does not depend on the model being in the
    HF cache and stays fast."""
    port = _free_port()
    proc = _spawn_headless(
        tmp_path, port,
        extra_env={
            "RECALL_PRELOAD_WHISPER": "0",
            "RECALL_LOG_FORMAT": "json",
            # Suppresses third-party `warnings.warn(...)` (e.g. ctranslate2's
            # deprecated pkg_resources), which are written directly to stderr
            # bypassing logging and so will NEVER be JSON regardless of
            # RECALL_LOG_FORMAT — otherwise they would spoil the "every
            # non-empty line" check with noise unrelated to the app.py gate.
            "PYTHONWARNINGS": "ignore",
        },
    )
    try:
        deadline = time.time() + 120
        status, data = None, None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"child process exited prematurely (code {proc.returncode}):\n"
                    f"{_boot_log(proc)}"
                )
            try:
                status, data = _get_json(port, "/api/ready")
                if status == 200:
                    break
            except (ConnectionRefusedError, OSError):
                pass
            time.sleep(0.05)

        assert status == 200, f"/api/ready did not return 200 within 120s:\n{_boot_log(proc)}"
        assert data["ready"] is True

        full_log = _full_boot_log(proc)
        lines = [ln for ln in full_log.splitlines() if ln.strip()]
        assert lines, "boot log is empty — nothing to check"
        # The Flask CLI prints these two lines via click.echo (flask/cli.py
        # show_server_banner, an unconditional call from flask/app.py:607) DIRECTLY to
        # stdout, bypassing the logging module — they do not pass and cannot
        # pass through any app.py formatter regardless of RECALL_LOG_FORMAT.
        # The only exception to the "every line is JSON" check, because no breakage of
        # the app.py:239-245 gate could fix or break this.
        _FLASK_CLI_BANNER_PREFIXES = (" * Serving Flask app", " * Debug mode:")
        for line in lines:
            if line.startswith(_FLASK_CLI_BANNER_PREFIXES):
                continue
            # A breakage of the app.py:239-245 gate (e.g. the formatter stays text
            # despite RECALL_LOG_FORMAT=json) -> the line does not parse as JSON.
            parsed = json.loads(line)
            assert {"ts", "level", "logger", "msg", "request_id"} <= set(parsed.keys()), (
                f"log line without the required fields: {line}"
            )
    finally:
        _assert_process_tree_gone(_terminate(proc))
