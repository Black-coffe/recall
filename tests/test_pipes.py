"""K4: scheduled pipes — cron, the pipe file, render, history, catch-up."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.migrations import init_database
from app.services import pipes
from app.services.pipes import Cron, PipeError, PipeScheduler, parse_pipe, run_pipe


@pytest.fixture(autouse=True)
def _isolated_privacy(tmp_path, monkeypatch):
    """The owner's real private.txt does not get into the tests."""
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(tmp_path / "private.txt"))
    monkeypatch.delenv("RECALL_PRIVATE_CATEGORIES", raising=False)


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "pipes.db")
    init_database(path)
    return path


def _pipe(**over) -> pipes.Pipe:
    meta = {"schedule": "0 9 * * 1", "tools": "weekly_digest", "channel": "telegram",
            "budget_usd": "0.10", "skip_if_empty": "false"}
    meta.update(over)
    head = "\n".join(f"{k}: {v}" for k, v in meta.items())
    return parse_pipe(f"---\n{head}\n---\nПерекажи звід коротко.", "звід")


DATA_FULL = {"this_week": [{"task": "Надіслати Олені договір", "owner": "Андрій",
                            "due_date": "2026-10-16"}],
             "overdue": [], "coverage": {"total": 3}}
DATA_EMPTY = {"questions": [], "skipped_group": 0, "me": {"name": "Андрій"}}


@pytest.fixture
def fake_tools(monkeypatch):
    calls = {}

    def make(result):
        def tool(db_path, now):
            calls["now"] = now
            if isinstance(result, Exception):
                raise result
            return result
        return tool

    def set_tool(name, result):
        monkeypatch.setitem(pipes.TOOLS, name, make(result))
    return set_tool


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ------------------------------------------------------------------ cron

class TestCron:
    def test_monday_9_next_and_prev_around_week_edge(self):
        c = Cron.parse("0 9 * * 1")
        sun = datetime(2026, 10, 18, 23, 59)            # Sunday
        assert c.next(sun) == datetime(2026, 10, 19, 9, 0)
        assert c.prev(sun) == datetime(2026, 10, 12, 9, 0)
        mon_9 = datetime(2026, 10, 19, 9, 0)
        assert c.prev(mon_9) == mon_9                    # the moment ≤ now inclusive
        assert c.next(mon_9) == datetime(2026, 10, 26, 9, 0)

    def test_weekdays_skip_weekend(self):
        c = Cron.parse("0 9 * * 1-5")
        fri_10 = datetime(2026, 10, 16, 10, 0)
        assert c.next(fri_10) == datetime(2026, 10, 19, 9, 0)
        sun = datetime(2026, 10, 18, 12, 0)
        assert c.prev(sun) == datetime(2026, 10, 16, 9, 0)
        mon_8 = datetime(2026, 10, 19, 8, 59)
        assert c.prev(mon_8) == datetime(2026, 10, 16, 9, 0)

    def test_sunday_as_0_and_7_and_lists(self):
        assert Cron.parse("30 7 * * 0").weekdays == Cron.parse("30 7 * * 7").weekdays == {0}
        c = Cron.parse("0,30 8-9 * * *")
        assert c.next(datetime(2026, 10, 15, 8, 10)) == datetime(2026, 10, 15, 8, 30)

    @pytest.mark.parametrize("bad", ["0 9 * *", "0 25 * * 1", "0 9 * * пн", "x 9 * * 1",
                                     "0 9 5-1 * *"])
    def test_invalid_rejected(self, bad):
        with pytest.raises(PipeError):
            Cron.parse(bad)


# ----------------------------------------------------------- pipe file

class TestPipeFile:
    def test_parses_frontmatter_and_body(self):
        p = _pipe(skip_if_empty="true")
        assert p.tools == ["weekly_digest"] and p.skip_if_empty is True
        assert p.budget_usd == pytest.approx(0.10)
        assert p.prompt == "Перекажи звід коротко."

    def test_tool_outside_registry_rejected(self):
        with pytest.raises(PipeError, match="outside the registry"):
            _pipe(tools="weekly_digest, ask_archive")

    def test_invalid_file_skipped_with_reason_in_log(self, tmp_path, caplog):
        (tmp_path / "добра.md").write_text(
            "---\nschedule: 0 9 * * 1\ntools: open_questions\nchannel: telegram\n---\nПромпт.",
            encoding="utf-8")
        (tmp_path / "зламана.md").write_text(
            "---\nschedule: 0 99 * * 1\ntools: open_questions\nchannel: telegram\n---\nПромпт.",
            encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="app.services.pipes"):
            loaded = pipes.load_pipes(tmp_path)
        assert [p.name for p in loaded] == ["добра"]
        assert "зламана.md" in caplog.text and "hour" in caplog.text

    def test_shipped_pipe_files_load(self):
        names = {p.name: p for p in pipes.load_pipes()}
        assert set(names) == {"weekly-digest", "dropped-commitments", "open-questions", "mcp-actions",
                              "task-proposals"}
        assert names["open-questions"].skip_if_empty is True
        assert names["open-questions"].schedule.expr == "0 9 * * 1-5"
        assert names["weekly-digest"].schedule.expr == "0 9 * * 1"


# --------------------------------------------------------------- render

class _FakeClient:
    def __init__(self, text="Звід: надіслати Олені договір.", exc=None):
        self.text, self.exc, self.calls = text, exc, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return SimpleNamespace(content=[SimpleNamespace(text=self.text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                     cache_read_input_tokens=0))


class TestRunPipe:
    def test_no_key_sends_cards_without_headline(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        sent = []
        res = run_pipe(_pipe(), db_path=db, deliver=sent.append)
        assert res["status"] == "ok" and res["rendered_by"] == "plain"
        assert "💡" not in sent[0]
        assert "Надіслати Олені договір" in sent[0]
        row = pipes.list_runs(db)[0]
        assert row["rendered_by"] == "plain" and "ANTHROPIC_API_KEY" in row["error"]

    def test_budget_exceeded_does_not_call_claude(self, db, fake_tools, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        client = _FakeClient()
        monkeypatch.setattr(pipes, "_get_client", lambda: client)
        fake_tools("weekly_digest", DATA_FULL)
        sent = []
        res = run_pipe(_pipe(budget_usd="0.0001"), db_path=db, deliver=sent.append)
        assert client.calls == []
        assert res["rendered_by"] == "plain" and "budget" in res["error"]
        assert "💡" not in sent[0] and "Надіслати Олені договір" in sent[0]

    def test_claude_exception_goes_plain(self, db, fake_tools, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(pipes, "_get_client", lambda: _FakeClient(exc=TimeoutError("довго")))
        fake_tools("weekly_digest", DATA_FULL)
        sent = []
        res = run_pipe(_pipe(), db_path=db, deliver=sent.append)
        assert res["status"] == "ok" and res["rendered_by"] == "plain"
        assert "TimeoutError" in res["error"]

    def test_claude_success_records_cost_from_usage(self, db, fake_tools, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        client = _FakeClient()
        monkeypatch.setattr(pipes, "_get_client", lambda: client)
        fake_tools("weekly_digest", DATA_FULL)
        sent = []
        res = run_pipe(_pipe(), db_path=db, deliver=sent.append)
        assert len(sent) == 1 and "💡 Звід: надіслати Олені договір." in sent[0]
        assert res["rendered_by"] == "claude"
        from app.services.pricing import estimate_cost
        expected = estimate_cost(pipes._model(), 1000, 200)
        assert pipes.list_runs(db)[0]["cost_usd"] == pytest.approx(expected)
        assert "Надіслати Олені договір" in client.calls[0]["messages"][0]["content"]

    def test_skip_if_empty(self, db, fake_tools, no_key):
        fake_tools("open_questions", DATA_EMPTY)
        sent = []
        res = run_pipe(_pipe(tools="open_questions", skip_if_empty="true"),
                       db_path=db, deliver=sent.append)
        assert res["status"] == "skipped" and sent == []
        assert pipes.list_runs(db)[0]["status"] == "skipped"

    def test_empty_without_skip_still_sends(self, db, fake_tools, no_key):
        fake_tools("open_questions", DATA_EMPTY)
        sent = []
        run_pipe(_pipe(tools="open_questions"), db_path=db, deliver=sent.append)
        assert len(sent) == 1

    def test_data_function_error_recorded(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", RuntimeError("БД зайнята"))
        res = run_pipe(_pipe(), db_path=db, deliver=lambda t: None)
        assert res["status"] == "error" and "БД зайнята" in res["error"]
        assert pipes.list_runs(db)[0]["status"] == "error"

    def test_deliver_error_recorded(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)

        def boom(text):
            raise ConnectionError("Telegram недоступний")
        res = run_pipe(_pipe(), db_path=db, deliver=boom)
        assert res["status"] == "error" and "Telegram недоступний" in res["error"]


# ---------------------------------------------------------- scheduler

class TestScheduler:
    def _sched(self, db, now, sent):
        return PipeScheduler([_pipe()], db_path=db, deliver=sent.append,
                             clock=lambda: now)

    def test_catchup_runs_once_even_if_several_missed(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        sent = []
        # The last run was 3 weeks ago — three Mondays were missed.
        run_pipe(_pipe(), db_path=db, deliver=lambda t: None,
                 now=datetime(2026, 9, 28, 9, 0), trigger="schedule",
                 scheduled_for=datetime(2026, 9, 28, 9, 0))
        s = self._sched(db, datetime(2026, 10, 21, 12, 0), sent)
        first = s.step("catchup")
        assert [r["trigger"] for r in first] == ["catchup"]
        assert s.step("schedule") == [] and s.step("catchup") == []
        runs = pipes.list_runs(db)
        assert runs[0]["scheduled_for"] == "2026-10-19 09:00:00"
        assert len(sent) == 1

    def test_nothing_when_already_ran(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        run_pipe(_pipe(), db_path=db, deliver=lambda t: None,
                 now=datetime(2026, 10, 19, 9, 0), scheduled_for=datetime(2026, 10, 19, 9, 0),
                 trigger="schedule")
        sent = []
        assert self._sched(db, datetime(2026, 10, 20, 9, 0), sent).step("catchup") == []
        assert sent == []

    def test_due_moment_runs_on_schedule(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        now = {"t": datetime(2026, 10, 19, 8, 59)}
        run_pipe(_pipe(), db_path=db, deliver=lambda t: None,
                 now=datetime(2026, 10, 12, 9, 0), scheduled_for=datetime(2026, 10, 12, 9, 0),
                 trigger="schedule")
        sent = []
        s = PipeScheduler([_pipe()], db_path=db, deliver=sent.append, clock=lambda: now["t"])
        assert s.step() == []
        now["t"] = datetime(2026, 10, 19, 9, 0, 20)
        assert [r["status"] for r in s.step()] == ["ok"]

    def test_scheduler_survives_failing_pipe(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", RuntimeError("зламалось"))
        s = self._sched(db, datetime(2026, 10, 19, 9, 1), [])
        s.tick = 0.01
        s.start()
        try:
            s._thread.join(0.2)
            assert s._thread.is_alive()
        finally:
            s.stop()
        assert pipes.list_runs(db)[0]["status"] == "error"


def _app_py():
    """app.py is shadowed by the app/ package — we load it by file (like test_endpoints_smoke)."""
    import importlib.util
    import sys
    mod = sys.modules.get("whisper_app_module")
    if mod is None:
        root = Path(__file__).resolve().parents[1]
        if not (root / "whisper_history.db").exists():
            pytest.skip("no live DB — not importing app.py")
        spec = importlib.util.spec_from_file_location("whisper_app_module", root / "app.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["whisper_app_module"] = mod
        spec.loader.exec_module(mod)
    return mod


def test_app_import_does_not_start_scheduler():
    extensions = _app_py().app.extensions
    assert "pipe_scheduler" not in extensions and "brief_buttons" not in extensions


def test_disabled_flag_logs_one_line(monkeypatch, caplog):
    app_module = _app_py()
    monkeypatch.setenv("PIPES_ENABLED", "0")
    with caplog.at_level(logging.INFO):
        app_module._maybe_start_pipes()
    lines = [r for r in caplog.records if "Pipes" in r.getMessage()]
    assert len(lines) == 1 and "PIPES_ENABLED=0" in lines[0].getMessage()
    assert "pipe_scheduler" not in app_module.app.extensions


# ------------------------------------------------- failure → message to the bot

class TestFailureNotice:
    def test_data_failure_sends_short_notice_without_data(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", RuntimeError("БД зайнята: Олена Петренко"))
        sent = []
        res = run_pipe(_pipe(), db_path=db, deliver=sent.append)
        assert res["status"] == "error"
        assert sent == ["⚠️ Труба «звід» впала: RuntimeError. Деталі — у list_pipe_runs."]

    def test_deliver_failure_no_recursion(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        calls = []

        def boom(text):
            calls.append(text)
            raise ConnectionError("Telegram недоступний")
        res = run_pipe(_pipe(), db_path=db, deliver=boom)
        assert res["status"] == "error" and len(calls) == 1

    def test_notice_failure_swallowed(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", RuntimeError("зламалось"))

        def boom(text):
            raise ConnectionError("нема мережі")
        res = run_pipe(_pipe(), db_path=db, deliver=boom)
        assert res["status"] == "error" and "зламалось" in res["error"]

    def test_bot_not_configured_recorded_as_error(self, db, fake_tools, no_key, monkeypatch):
        from app.services import tg_bot
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_BOT_CHAT_ID", "")
        fake_tools("weekly_digest", DATA_FULL)
        res = run_pipe(_pipe(), db_path=db, deliver=tg_bot.send)
        assert res["status"] == "error" and "not configured" in pipes.list_runs(db)[0]["error"]


def test_mcp_list_pipe_runs(db, fake_tools, no_key, monkeypatch):
    import mcp_server
    fake_tools("weekly_digest", RuntimeError("зламалось"))
    run_pipe(_pipe(), db_path=db, deliver=lambda t: None)
    monkeypatch.setattr(mcp_server, "DB_PATH", db)
    fn = getattr(mcp_server.list_pipe_runs, "fn", mcp_server.list_pipe_runs)
    runs = fn(limit=5)["runs"]
    assert runs[0]["pipe"] == "звід" and runs[0]["status"] == "error"
    assert {"status", "error", "cost_usd"} <= set(runs[0])


# ------------------------------------- retry after a failure (story 05, owner 15.10.2026)

class TestRetry:
    """«If the brief is not sent on Monday at 9:00, Recall will not try again,
    and the digest for that week will be lost.»"""

    def _sched(self, db, clock, sent, **kw):
        return PipeScheduler([_pipe()], db_path=db, deliver=sent.append,
                             clock=lambda: clock["t"], retry_minutes=15, retry_max=3, **kw)

    def test_failed_delivery_retried_after_interval(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        clock = {"t": datetime(2026, 10, 19, 9, 0)}
        sent, fail = [], {"on": True}

        def deliver(text):
            if fail["on"]:
                raise ConnectionError("Telegram недоступний")
            sent.append(text)
        s = PipeScheduler([_pipe()], db_path=db, deliver=deliver, clock=lambda: clock["t"],
                          retry_minutes=15, retry_max=3)
        assert [r["status"] for r in s.step()] == ["error"]
        fail["on"] = False
        clock["t"] = datetime(2026, 10, 19, 9, 10)
        assert s.step() == []                                  # too early
        clock["t"] = datetime(2026, 10, 19, 9, 16)
        res = s.step()
        assert [(r["trigger"], r["status"]) for r in res] == [("retry", "ok")]
        assert len(sent) == 1
        clock["t"] = datetime(2026, 10, 19, 10, 0)
        assert s.step() == []                                  # delivered — the moment is closed

    def test_gives_up_after_max_and_notifies_first_and_last(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", RuntimeError("БД зайнята"))
        clock = {"t": datetime(2026, 10, 19, 9, 0)}
        sent = []
        s = self._sched(db, clock, sent)
        for minute in (0, 16, 32, 48, 59):
            clock["t"] = datetime(2026, 10, 19, 9, minute)
            s.step()
        runs = pipes.list_runs(db)
        assert [r["status"] for r in runs] == ["error"] * 3
        assert [r["trigger"] for r in reversed(runs)] == ["schedule", "retry", "retry"]
        assert len(sent) == 2
        assert "Повторю через 15 хв (спроба 1 з 3)" in sent[0]
        assert "Спроби вичерпано (3)" in sent[1]
        assert "БД зайнята" not in "".join(sent)

    def test_dead_running_row_does_not_eat_catchup(self, db, fake_tools, no_key):
        import sqlite3
        fake_tools("weekly_digest", DATA_FULL)
        with sqlite3.connect(db) as conn:                      # the process died in the middle of a run
            conn.execute("INSERT INTO pipe_runs (pipe, trigger, scheduled_for, started_at, status) "
                         "VALUES ('звід', 'schedule', '2026-10-19 09:00:00', "
                         "'2026-10-19 09:00:00', 'running')")
        sent = []
        clock = {"t": datetime(2026, 10, 19, 9, 10)}
        s = self._sched(db, clock, sent)
        assert s.step("catchup") == []                         # may still go
        clock["t"] = datetime(2026, 10, 19, 11, 0)
        assert [r["status"] for r in s.step("catchup")] == ["ok"] and len(sent) == 1

    def test_manual_run_does_not_close_moment(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        run_pipe(_pipe(), db_path=db, deliver=lambda t: None,
                 now=datetime(2026, 10, 19, 9, 30))           # pipes run in the console
        sent = []
        clock = {"t": datetime(2026, 10, 19, 12, 0)}
        assert [r["trigger"] for r in self._sched(db, clock, sent).step("catchup")] == ["catchup"]
        assert len(sent) == 1

    def test_blocked_is_not_retried(self, db, fake_tools, no_key, tmp_path):
        (tmp_path / "private.txt").write_bytes(b"\xff\xfe\xfa")
        fake_tools("weekly_digest", DATA_FULL)
        clock = {"t": datetime(2026, 10, 19, 9, 0)}
        s = self._sched(db, clock, [])
        assert [r["status"] for r in s.step()] == ["blocked"]
        clock["t"] = datetime(2026, 10, 19, 10, 0)
        assert s.step() == []


# ------------------------- "could not collect" ≠ "empty" (story 07, owner 15.10.2026)

NO_OWNER = {"me": None, "questions": [], "skipped_group": 0,
            "reason": "невідомо, хто власник архіву: слухач вимкнений"}


class TestNotCollected:
    """«The morning summary of questions can stay silent every workday if Recall does not know who owns the
    Telegram, and this silence cannot be told from "no questions".»"""

    def _q(self):
        return _pipe(tools="open_questions", skip_if_empty="true")

    def test_unknown_owner_is_not_skipped(self, db, fake_tools, no_key):
        fake_tools("open_questions", NO_OWNER)
        sent = []
        res = run_pipe(self._q(), db_path=db, deliver=sent.append)
        assert res["status"] == "ok" and len(sent) == 1
        assert "⚠️ Питання з Telegram не зібрано: невідомо, хто власник" in sent[0]
        assert "not collected: open_questions" in pipes.list_runs(db)[0]["error"]

    def test_real_empty_still_silent(self, db, fake_tools, no_key):
        fake_tools("open_questions", DATA_EMPTY)
        sent = []
        assert run_pipe(self._q(), db_path=db, deliver=sent.append)["status"] == "skipped"
        assert sent == []

    def test_reason_line_added_by_code_with_claude(self, db, fake_tools, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(pipes, "_get_client", lambda: _FakeClient(text="Питань нема."))
        fake_tools("open_questions", NO_OWNER)
        sent = []
        run_pipe(self._q(), db_path=db, deliver=sent.append)
        assert "⚠️ Питання з Telegram не зібрано" in sent[0]
        assert "💡 Питань нема." in sent[0]


# --------------------- story mcp-telegram-automation-03: lock, cooldown, mcp, event

def _slow_tool(entered, gate):
    def tool(db_path, now):
        entered.set()
        gate.wait(5)
        return DATA_FULL
    return tool


class TestConcurrency:
    def test_concurrent_same_pipe_one_runs_other_busy(self, db, no_key, monkeypatch):
        import threading
        gate, entered = threading.Event(), threading.Event()
        monkeypatch.setitem(pipes.TOOLS, "weekly_digest", _slow_tool(entered, gate))
        sent, results = [], []
        t = threading.Thread(target=lambda: results.append(
            run_pipe(_pipe(), db_path=db, deliver=sent.append, trigger="schedule")))
        t.start()
        try:
            assert entered.wait(5)
            busy = run_pipe(_pipe(), db_path=db, deliver=sent.append, trigger="mcp")
        finally:
            gate.set()
            t.join(5)
        assert busy == {"id": None, "pipe": "звід", "trigger": "mcp", "status": "busy",
                        "rendered_by": None, "cost_usd": 0.0, "error": "busy", "chars": 0}
        assert results[0]["status"] == "ok"
        assert len(pipes.list_runs(db)) == 1 and len(sent) == 1

    def test_different_pipes_run_concurrently(self, db, fake_tools, no_key, monkeypatch):
        import threading
        gate, entered = threading.Event(), threading.Event()
        monkeypatch.setitem(pipes.TOOLS, "weekly_digest", _slow_tool(entered, gate))
        fake_tools("open_questions", {"questions": [{"q": "Коли?"}], "me": {"name": "Андрій"}})
        other = parse_pipe("---\nschedule: 0 9 * * *\ntools: open_questions\n"
                           "channel: telegram\n---\nКоротко.", "питання")
        t = threading.Thread(target=lambda: run_pipe(_pipe(), db_path=db,
                                                     deliver=lambda x: None))
        t.start()
        try:
            assert entered.wait(5)
            res = run_pipe(other, db_path=db, deliver=lambda x: None)
        finally:
            gate.set()
            t.join(5)
        assert res["status"] == "ok" and res["id"] is not None

    def test_scheduler_busy_leaves_moment_open(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        sent = []
        clock = {"t": datetime(2026, 10, 19, 9, 0)}
        s = PipeScheduler([_pipe()], db_path=db, deliver=sent.append,
                          clock=lambda: clock["t"], retry_minutes=15, retry_max=3)
        lock = pipes._pipe_lock("звід")
        lock.acquire()
        try:
            assert s.step() == []
        finally:
            lock.release()
        assert pipes.list_runs(db) == [] and sent == []
        clock["t"] = datetime(2026, 10, 19, 9, 1)
        assert [(r["trigger"], r["status"]) for r in s.step()] == [("schedule", "ok")]


class TestMcpTrigger:
    def test_mcp_run_does_not_close_moment(self, db, fake_tools, no_key):
        fake_tools("weekly_digest", DATA_FULL)
        run_pipe(_pipe(), db_path=db, deliver=lambda t: None, trigger="mcp",
                 now=datetime(2026, 10, 19, 9, 30))
        sent = []
        s = PipeScheduler([_pipe()], db_path=db, deliver=sent.append,
                          clock=lambda: datetime(2026, 10, 19, 12, 0),
                          retry_minutes=15, retry_max=3)
        assert [r["trigger"] for r in s.step("catchup")] == ["catchup"]
        assert len(sent) == 1


class TestCooldown:
    NOW = datetime(2026, 10, 19, 12, 0)

    def _row(self, db, status, started, finished=None):
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO pipe_runs (pipe, trigger, started_at, finished_at, status) "
                         "VALUES ('звід', 'mcp', ?, ?, ?)", (started, finished, status))

    def test_empty_table_is_free(self, db):
        assert pipes.cooldown_remaining(db, "звід", now=self.NOW) == 0

    @pytest.mark.parametrize("status", ["ok", "skipped", "blocked"])
    def test_recent_run_blocks(self, db, status):
        self._row(db, status, "2026-10-19 11:56:00", "2026-10-19 11:57:00")
        assert pipes.cooldown_remaining(db, "звід", now=self.NOW) == 7 * 60

    def test_error_does_not_block(self, db):
        self._row(db, "error", "2026-10-19 11:58:00", "2026-10-19 11:59:00")
        assert pipes.cooldown_remaining(db, "звід", now=self.NOW) == 0

    def test_old_run_is_free(self, db):
        self._row(db, "ok", "2026-10-19 11:45:00", "2026-10-19 11:49:00")
        assert pipes.cooldown_remaining(db, "звід", now=self.NOW) == 0

    def test_running_younger_than_stale_blocks(self, db):
        self._row(db, "running", "2026-10-19 11:45:00")
        assert pipes.cooldown_remaining(db, "звід", now=self.NOW) == 15 * 60

    def test_stale_running_is_free(self, db):
        self._row(db, "running", "2026-10-19 11:00:00")
        assert pipes.cooldown_remaining(db, "звід", now=self.NOW) == 0

    def test_other_pipe_does_not_block(self, db):
        self._row(db, "ok", "2026-10-19 11:59:00", "2026-10-19 11:59:00")
        assert pipes.cooldown_remaining(db, "інша", now=self.NOW) == 0


class TestRunEvent:
    @pytest.fixture
    def events(self, monkeypatch):
        got = []
        monkeypatch.setattr(pipes.sse_broker.broker, "publish",
                            lambda ch, ev, data: got.append((ch, ev, data)))
        return got

    def _assert_event(self, events, res, status):
        assert len(events) == 1
        ch, ev, data = events[0]
        assert (ch, ev) == ("pipes", "pipe_run")
        assert data == {"pipe": "звід", "run_id": res["id"], "status": status,
                        "trigger": res["trigger"], "rendered_by": res["rendered_by"],
                        "cost_usd": res["cost_usd"]}

    def test_ok_event_without_text(self, db, fake_tools, no_key, events):
        fake_tools("weekly_digest", DATA_FULL)
        res = run_pipe(_pipe(), db_path=db, deliver=lambda t: None, trigger="mcp")
        self._assert_event(events, res, "ok")
        assert "Олені" not in repr(events)

    def test_skipped_event(self, db, fake_tools, no_key, events):
        fake_tools("weekly_digest", {"this_week": [], "overdue": []})
        res = run_pipe(_pipe(skip_if_empty="true"), db_path=db, deliver=lambda t: None)
        self._assert_event(events, res, "skipped")

    def test_blocked_event(self, db, fake_tools, no_key, events, tmp_path):
        (tmp_path / "private.txt").write_bytes(b"\xff\xfe\xfa")
        fake_tools("weekly_digest", DATA_FULL)
        res = run_pipe(_pipe(), db_path=db, deliver=lambda t: None)
        self._assert_event(events, res, "blocked")

    def test_error_event(self, db, fake_tools, no_key, events):
        fake_tools("weekly_digest", RuntimeError("boom"))
        res = run_pipe(_pipe(), db_path=db, deliver=lambda t: None)
        self._assert_event(events, res, "error")

    def test_busy_publishes_nothing(self, db, fake_tools, no_key, events):
        fake_tools("weekly_digest", DATA_FULL)
        lock = pipes._pipe_lock("звід")
        lock.acquire()
        try:
            assert run_pipe(_pipe(), db_path=db, deliver=lambda t: None)["status"] == "busy"
        finally:
            lock.release()
        assert events == []

    def test_broker_failure_keeps_status(self, db, fake_tools, no_key, monkeypatch):
        def boom(*a):
            raise RuntimeError("broker down")
        monkeypatch.setattr(pipes.sse_broker.broker, "publish", boom)
        fake_tools("weekly_digest", DATA_FULL)
        res = run_pipe(_pipe(), db_path=db, deliver=lambda t: None)
        assert res["status"] == "ok"
        assert pipes.list_runs(db)[0]["status"] == "ok"
