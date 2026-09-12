"""config-registry-profiles S2: тест `app.core.logger.JsonFormatter` напряму,
без імпорту `app.py` (той самий мотив, що в `tests/test_auth_gate.py` —
`app.py` на імпорт кличе `load_dotenv()`, що дописує реальні секрети в
`os.environ` на решту pytest-сесії).

Перевіряє критерії приймання S2:
  - кожен відформатований рядок парситься `json.loads`;
  - обов'язкові поля: ts, level, logger, msg, request_id;
  - request_id береться з LogRecord, якщо проставлений (app.py:_RequestIdLogFilter
    у реальному сетапі), інакше '-' — так само, як у text-форматі.

Запуск:
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
        parsed = json.loads(line)  # не має кинути ValueError
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
        """Поза HTTP-запитом (фоновий потік, boot-код) `_RequestIdLogFilter`
        реального сетапу теж пише '-' — JsonFormatter лишається сумісним."""
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
        """S4 знахідка 16: `formatTime` з явним datefmt губив мілісекунди
        проти text-режиму (`%(asctime)s` без datefmt додає ",%03d" сам).
        Дописуємо їх так само явно."""
        record = _make_record()
        record.msecs = 42.7
        parsed = json.loads(JsonFormatter().format(record))
        assert parsed["ts"].endswith(",042")


class TestJsonFormatterImportSafety:
    """config-registry-fix S4, критерій 1: `from app.core.logger import
    JsonFormatter` не сміє оживляти `setup_logger("whisper_ui")` — раніше це
    був модульний рівень (`app_logger = setup_logger("whisper_ui")`), що
    додавало StreamHandler (і, при RECALL_LOG_TO_FILE=1, ще й другий
    RotatingFileHandler на спільний `whisper_ui.log`, за який уже конкурує
    хендлер app.py — WinError 32) щоразу, як хтось імпортував цей модуль
    заради самого лише форматера."""

    def test_import_does_not_create_handlers(self):
        import app.core.logger as logger_module

        named = logging.getLogger("whisper_ui")
        named.handlers.clear()
        root_before = len(logging.root.handlers)
        try:
            importlib.reload(logger_module)
            assert named.handlers == [], (
                "перезавантаження модуля додало хендлер(и) на логер 'whisper_ui'"
            )
            assert len(logging.root.handlers) == root_before
        finally:
            named.handlers.clear()

    def test_import_does_not_create_handlers_with_log_to_file_enabled(self, monkeypatch):
        """Той самий сценарій, що і фінальний фікс, зі знахідки 5: раніше
        саме `RECALL_LOG_TO_FILE=1` вмикав другий `RotatingFileHandler` на
        тому самому файлі, що й app.py."""
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
    """config-registry-fix-r3-02, N2: замінює `test_json_format_end_to_end_via_env`.

    Старий тест відтворював гейт-логіку app.py у власному тілі (`if
    _settings.env(...) == 'json': formatter = JsonFormatter()`) і тому не
    міг почервоніти від поламки самого `app.py` — лише від поламки своєї ж
    копії умови. Наскрізне покриття (реальний дочірній процес,
    `RECALL_LOG_FORMAT=json`, кожен рядок stream-виводу — валідний JSON)
    тепер живе в `tests/test_headless_boot.py::test_headless_boot_json_logs`
    (`slow`, справжній `app.py`). Тут лишається статичний гард на текст —
    за зразком `tests/test_boot_робота.py:99-103` — що гейт справді
    використовує `JsonFormatter`, а не текстовий форматер, коли
    `RECALL_LOG_FORMAT=json`."""
    idx = APP_PY_SOURCE.index("_settings.env('RECALL_LOG_FORMAT')")
    snippet = APP_PY_SOURCE[idx: idx + 250]
    assert "JsonFormatter()" in snippet, (
        "гейт RECALL_LOG_FORMAT=json у app.py більше не веде до JsonFormatter()"
    )
