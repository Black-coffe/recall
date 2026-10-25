"""app.core.env_file: the single locked .env writer."""
import os
import threading

from app.core import env_file


def test_set_writes_quoted_and_updates_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("RC_T_KEY", raising=False)
    p = tmp_path / ".env"
    env_file.set_env_var("RC_T_KEY", "abc", path=p)
    assert "RC_T_KEY='abc'" in p.read_text(encoding="utf-8")
    assert os.environ["RC_T_KEY"] == "abc"
    monkeypatch.delenv("RC_T_KEY", raising=False)


def test_unset_removes_line(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    env_file.set_env_var("RC_T_KEY", "abc", path=p)
    assert env_file.unset_env_var("RC_T_KEY", path=p) is True
    assert "RC_T_KEY" not in p.read_text(encoding="utf-8")
    assert "RC_T_KEY" not in os.environ


def test_concurrent_writes_lose_nothing(tmp_path, monkeypatch):
    p = tmp_path / ".env"

    def work(prefix):
        for i in range(50):
            env_file.set_env_var(f"RC_T_{prefix}{i}", str(i), path=p)

    threads = [threading.Thread(target=work, args=(x,)) for x in "AB"]
    [t.start() for t in threads]
    [t.join() for t in threads]
    text = p.read_text(encoding="utf-8")
    for x in "AB":
        for i in range(50):
            assert f"RC_T_{x}{i}='{i}'" in text
            os.environ.pop(f"RC_T_{x}{i}", None)


def test_cyrillic_roundtrip(tmp_path):
    from dotenv import dotenv_values
    p = tmp_path / ".env"
    env_file.set_env_var("RC_T_CYR", "Привіт, світ", path=p)
    assert dotenv_values(p, encoding="utf-8")["RC_T_CYR"] == "Привіт, світ"
    env_file.unset_env_var("RC_T_CYR", path=p)


def test_writers_go_through_env_file():
    import inspect
    import config
    from app.blueprints import settings_api
    from app.services import settings_secrets
    assert "env_file" in inspect.getsource(settings_api._set_env_var)
    assert "env_file" in inspect.getsource(settings_secrets.save_anthropic_key)
    assert "env_file" in inspect.getsource(config._persist_secret_key)
    for mod in (settings_api, settings_secrets):
        assert "from dotenv import set_key" not in inspect.getsource(mod)


def test_cli_entry_points_load_env_by_explicit_path(monkeypatch):
    """pipes/tg_bot CLIs read the project's own .env, never a parent tree's (find_dotenv walk)."""
    import inspect

    import dotenv
    import pytest

    from app.services import pipes, tg_bot

    calls = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(SystemExit):
        pipes._cli([])  # argparse rejects the missing subcommand right after the .env load
    assert calls == [((env_file.ENV_PATH,), {})]
    for mod in (pipes, tg_bot):
        assert "load_dotenv()" not in inspect.getsource(mod)
