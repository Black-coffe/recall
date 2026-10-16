"""config-registry-profiles S2: a test of `app.core.logger.JsonFormatter` directly,
without importing `app.py` (the same motive as in `tests/test_auth_gate.py` —
`app.py` calls `load_dotenv()` on import, which appends real secrets to
`os.environ` for the rest of the pytest session).

Checks the S2 acceptance criteria:
  - every formatted line parses with `json.loads`;
  - required fields: ts, level, logger, msg, request_id;
  - request_id is taken from the LogRecord if set (app.py:_RequestIdLogFilter
    in the real setup), otherwise '-' — the same as in the text format.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_json_logs.py -v
"""
import importlib
import json
import logging
from pathlib import Path

from app.core.logger import JsonFormatter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_PY_SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")


def _make_record(msg="hello world", level=logging.INFO, request_id=None, exc_info=None):
    record = logging.LogRecord(
        name="whisper_ui.test",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    if request_id is not None:
        record.request_id = request_id
    return record


class TestJsonFormatter:
    def test_output_is_valid_json(self):
        record = _make_record()
        line = JsonFormatter().format(record)
        parsed = json.loads(line)  # must not raise ValueError
        assert isinstance(parsed, dict)

    def test_required_fields_present(self):
        record = _make_record(msg="boot ok", request_id="abc123def456")
        parsed = json.loads(JsonFormatter().format(record))
        assert set(("ts", "level", "logger", "msg", "request_id")) <= set(parsed.keys())
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "whisper_ui.test"
        assert parsed["msg"] == "boot ok"
        assert parsed["request_id"] == "abc123def456"

    def test_request_id_defaults_to_dash_outside_request_context(self):
        """Outside an HTTP request (background thread, boot code) the `_RequestIdLogFilter`
        of the real setup also writes '-' — JsonFormatter stays compatible."""
        record = _make_record(msg="background task")
        parsed = json.loads(JsonFormatter().format(record))
        assert parsed["request_id"] == "-"

    def test_message_with_percent_args_is_expanded(self):
        record = logging.LogRecord(
            name="whisper_ui.test",
            level=logging.WARNING,
            pathname="test.py",
            lineno=1,
            msg="retry %s of %d",
            args=("job-1", 3),
            exc_info=None,
        )
        parsed = json.loads(JsonFormatter().format(record))
        assert parsed["msg"] == "retry job-1 of 3"
        assert parsed["level"] == "WARNING"

    def test_exc_info_included_when_present(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="whisper_ui.test",
                level=logging.ERROR,
                pathname="test.py",
                lineno=1,
                msg="failed",
                args=(),
                exc_info=sys.exc_info(),
            )
        parsed = json.loads(JsonFormatter().format(record))
        assert "exc_info" in parsed
        assert "ValueError" in parsed["exc_info"]
        assert "boom" in parsed["exc_info"]

    def test_ts_carries_milliseconds(self):
        """S4 finding 16: `formatTime` with an explicit datefmt lost milliseconds
        versus text mode (`%(asctime)s` without datefmt adds ",%03d" itself).
        We append them just as explicitly."""
        record = _make_record()
        record.msecs = 42.7
        parsed = json.loads(JsonFormatter().format(record))
        assert parsed["ts"].endswith(",042")


class TestJsonFormatterImportSafety:
    """config-registry-fix S4, criterion 1: `from app.core.logger import
    JsonFormatter` must not revive `setup_logger("whisper_ui")` — it used to be
    module level (`app_logger = setup_logger("whisper_ui")`), which
    added a StreamHandler (and, with RECALL_LOG_TO_FILE=1, also a second
    RotatingFileHandler on the shared `whisper_ui.log`, which the app.py
    handler already competes for — WinError 32) every time someone imported this module
    just for the formatter."""

    def test_import_does_not_create_handlers(self):
        import app.core.logger as logger_module

        named = logging.getLogger("whisper_ui")
        named.handlers.clear()
        root_before = len(logging.root.handlers)
        try:
            importlib.reload(logger_module)
            assert named.handlers == [], (
                "module reload added handler(s) to the 'whisper_ui' logger"
            )
            assert len(logging.root.handlers) == root_before
        finally:
            named.handlers.clear()

    def test_import_does_not_create_handlers_with_log_to_file_enabled(self, monkeypatch):
        """The same scenario as the final fix, from finding 5: it used to be
        `RECALL_LOG_TO_FILE=1` that turned on a second `RotatingFileHandler` on
        the same file as app.py."""
        monkeypatch.setenv("RECALL_LOG_TO_FILE", "1")
        import app.core.logger as logger_module

        named = logging.getLogger("whisper_ui")
        named.handlers.clear()
        try:
            importlib.reload(logger_module)
            assert named.handlers == []
        finally:
            named.handlers.clear()


def test_json_format_gate_uses_json_formatter_in_app_py():
    """config-registry-fix-r3-02, N2: replaces `test_json_format_end_to_end_via_env`.

    The old test reproduced the app.py gate logic in its own body (`if
    _settings.env(...) == 'json': formatter = JsonFormatter()`) and therefore could not
    go red from a breakage of `app.py` itself — only from a breakage of its own
    copy of the condition. The end-to-end coverage (a real child process,
    `RECALL_LOG_FORMAT=json`, every stream output line is valid JSON)
    now lives in `tests/test_headless_boot.py::test_headless_boot_json_logs`
    (`slow`, the real `app.py`). What stays here is a static text guard —
    modelled on `tests/test_boot_робота.py:99-103` — that the gate really
    uses `JsonFormatter`, and not the text formatter, when
    `RECALL_LOG_FORMAT=json`."""
    idx = APP_PY_SOURCE.index("_settings.env('RECALL_LOG_FORMAT')")
    snippet = APP_PY_SOURCE[idx: idx + 250]
    assert "JsonFormatter()" in snippet, (
        "the RECALL_LOG_FORMAT=json gate in app.py no longer leads to JsonFormatter()"
    )
