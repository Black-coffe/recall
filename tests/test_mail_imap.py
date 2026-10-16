"""Read-only IMAP wrapper: command recording against a fake connection."""
import datetime as dt
import imaplib
import logging
import re
from pathlib import Path

import pytest

from app.services import mail_imap
from app.services.mail_imap import MailError, ReadOnlyImap

FORBIDDEN = {"STORE", "EXPUNGE", "APPEND", "COPY", "MOVE", "CREATE", "DELETE", "RENAME", "SUBSCRIBE"}


class FakeConn:
    def __init__(self, login_error=None):
        self.cmds = []
        self.selects = []
        self.debug = 5
        self.login_error = login_error

    def login(self, user, pw):
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        self.cmds.append(("LOGIN",))
        return "OK", [b"ok"]

    def list(self):
        self.cmds.append(("LIST",))
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
            b'(\\HasNoChildren) "/" "&BAYEPQQyBDUEQQRCBD4EQAQ4-"',
        ]

    def select(self, mailbox, readonly=False):
        self.cmds.append(("SELECT", mailbox))
        self.selects.append(readonly)
        if "Missing" in mailbox:
            return "NO", [b"nope"]
        return "OK", [b"42"]

    def response(self, code):
        self.cmds.append(("RESPONSE", code))
        return code, [b"777"]

    def uid(self, command, *args):
        self.cmds.append(("UID", command.upper()) + args)
        if command.upper() == "SEARCH":
            return "OK", [b"3 1 2"]
        if command.upper() == "FETCH":
            ids = args[0].split(",")
            data = []
            for u in ids:
                data.append((f"{u} (UID {u} BODY[] {{5}}".encode(), b"raw" + u.encode()))
                data.append(b")")
            return "OK", data
        return "NO", []

    def logout(self):
        self.cmds.append(("LOGOUT",))
        return "BYE", []


def _client():
    c = FakeConn()
    return c, ReadOnlyImap(c)


def test_every_method_is_read_only():
    c, ro = _client()
    ro.list_folders()
    ro.examine("INBOX")
    ro.search_since(dt.date(2026, 1, 5), before=dt.date(2026, 2, 1))
    ro.search_after_uid(10)
    list(ro.fetch_raw(range(1, 121), batch=50))
    ro.close()
    for cmd in c.cmds:
        assert cmd[0] not in FORBIDDEN
        if cmd[0] == "UID":
            assert cmd[1] not in FORBIDDEN
    assert c.selects and all(c.selects)
    fetches = [cmd for cmd in c.cmds if cmd[:2] == ("UID", "FETCH")]
    assert len(fetches) == 3
    assert all("BODY.PEEK" in cmd[3] for cmd in fetches)


def test_no_passthrough_surface():
    public = {n for n in dir(ReadOnlyImap) if not n.startswith("_")}
    assert public == {"list_folders", "examine", "search_since", "search_after_uid", "fetch_raw",
                      "close"}
    assert not hasattr(ReadOnlyImap, "__getattr__")


def test_modules_do_not_import_smtplib():
    root = Path(mail_imap.__file__).parent
    for name in ("mail_imap.py", "mail_parse.py"):
        assert not re.search(r"^\s*(import|from)\s+smtplib", (root / name).read_text(encoding="utf-8"), re.M)


def test_connect_sets_debug_zero_and_returns_wrapper():
    c = FakeConn()
    ro = mail_imap.connect("a@example.com", "pw", factory=lambda h, p, t: c)
    assert c.debug == 0
    assert isinstance(ro, ReadOnlyImap)


def test_auth_failure_hides_password(caplog):
    pw = "секретpass1"
    c = FakeConn(login_error=f"[AUTHENTICATIONFAILED] bad {pw} here")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MailError) as ei:
            mail_imap.connect("a@example.com", pw, factory=lambda h, p, t: c)
    assert ei.value.code == "AUTH_FAILED"
    assert pw not in str(ei.value)
    assert all(pw not in r.getMessage() for r in caplog.records)


def test_scrub_with_cyrillic_neighbour():
    assert mail_imap.scrub("пароль:abc123слово", "abc123") == "пароль:***слово"


def test_list_folders_flags_and_mutf7():
    _, ro = _client()
    folders = ro.list_folders()
    names = [f["folder"] for f in folders]
    assert "[Gmail]" not in names
    assert "Інвестори" in names
    allmail = next(f for f in folders if f["folder"] == "[Gmail]/All Mail")
    assert allmail["system"] is True
    assert next(f for f in folders if f["folder"] == "INBOX")["system"] is False


def test_mutf7_roundtrip():
    assert mail_imap.encode_mutf7("Інвестори") == "&BAYEPQQyBDUEQQRCBD4EQAQ4-"
    assert mail_imap.decode_mutf7("&BAYEPQQyBDUEQQRCBD4EQAQ4-") == "Інвестори"
    assert mail_imap.decode_mutf7(mail_imap.encode_mutf7("A&B/ї")) == "A&B/ї"


def test_examine_and_missing():
    _, ro = _client()
    assert ro.examine("INBOX") == {"uidvalidity": 777, "exists": 42}
    with pytest.raises(MailError) as ei:
        ro.examine("Missing")
    assert ei.value.code == "FOLDER_MISSING"


def test_search_since_uses_english_months():
    c, ro = _client()
    assert ro.search_since(dt.date(2026, 3, 5), before=dt.date(2026, 12, 1)) == [1, 2, 3]
    assert c.cmds[-1] == ("UID", "SEARCH", "SINCE", "05-Mar-2026", "BEFORE", "01-Dec-2026")


def test_fetch_raw_batches():
    c, ro = _client()
    out = list(ro.fetch_raw([1, 2, 3], batch=2))
    assert out == [(1, b"raw1"), (2, b"raw2"), (3, b"raw3")]
    assert len([x for x in c.cmds if x[:2] == ("UID", "FETCH")]) == 2
