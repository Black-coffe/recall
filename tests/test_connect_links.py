"""One-time connect links: lifetime, single use, failure budget."""
import pytest

from app.services import connect_links as cl


@pytest.fixture
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(cl, "_clock", lambda: t[0])
    return t


def test_issue_and_peek_does_not_consume(clock):
    link = cl.issue("mail", subject="a@example.com", requested_by="mcp")
    assert link["expires_in"] == 600 and link["requested_by"] == "mcp"
    assert cl.peek("mail", link["token"]) is not None
    assert cl.peek("mail", link["token"]) is not None
    assert cl.peek("anthropic_key", link["token"]) is None


def test_expiry(clock):
    link = cl.issue("anthropic_key")
    clock[0] += 599
    assert cl.peek("anthropic_key", link["token"]) is not None
    clock[0] += 1
    assert cl.peek("anthropic_key", link["token"]) is None


def test_consume_is_single_use(clock):
    link = cl.issue("mail", subject="a@example.com")
    assert cl.consume("mail", link["token"]) is not None
    assert cl.consume("mail", link["token"]) is None
    assert cl.peek("mail", link["token"]) is None


def test_three_failures_kill(clock):
    link = cl.issue("mail", subject="a@example.com")
    assert cl.record_failure("mail", link["token"]) == 2
    assert cl.record_failure("mail", link["token"]) == 1
    assert cl.record_failure("mail", link["token"]) == 0
    assert cl.peek("mail", link["token"]) is None


def test_reissue_kills_first(clock):
    a = cl.issue("mail", subject="a@example.com")
    b = cl.issue("mail", subject="a@example.com")
    assert cl.peek("mail", a["token"]) is None
    assert cl.peek("mail", b["token"]) is not None
    cl.issue("mail", subject="other@example.com")
    assert cl.peek("mail", b["token"]) is not None


def test_unknown_kind_and_url(clock):
    with pytest.raises(ValueError):
        cl.issue("nope")
    assert cl.url_for("mail", "tok", 5050) == "http://127.0.0.1:5050/connect/mail/tok"
