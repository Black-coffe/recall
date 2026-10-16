"""Mail store: migration v52, accounts, labels, keyring (gmail-ingest-01)."""
from __future__ import annotations

import logging
import sqlite3

import keyring
import pytest

from app.db.migrations import init_database
from app.services import mail_accounts as ma

PW = "zq-distinct-pass-4417"
ADDR = "Ivan.Test@Example.com "


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "t.db")
    init_database(p)
    return p


def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _walk(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _walk(v)
    else:
        yield o


def test_v52_schema_and_rerun(db):
    conn = sqlite3.connect(db)
    tx = {r[1] for r in conn.execute("PRAGMA table_info(transcriptions)")}
    for c in ("mail_account_id", "mail_message_id", "mail_thread_key", "mail_in_reply_to", "mail_from",
              "mail_to", "mail_subject", "mail_folder", "mail_enrich"):
        assert c in tx
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"mail_accounts", "mail_labels", "mail_attachments", "idx_tx_mail_msg"} <= names
    assert conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] >= 52
    conn.execute("DELETE FROM schema_versions WHERE version = 52")
    conn.commit()
    conn.close()
    init_database(db)  # no-op on the migrated schema


def test_normalize_address():
    assert ma.normalize_address("  A@B.com ") == "a@b.com"
    for bad in ("nobody", "a@@b", "a@b@c", ""):
        with pytest.raises(ValueError):
            ma.normalize_address(bad)


def test_add_account_keyring_and_no_leak(db, caplog):
    caplog.set_level(logging.DEBUG)
    acc = ma.add_account(db, ADDR, PW)
    assert acc["address"] == "ivan.test@example.com" and acc["configured"] is True
    assert keyring.get_password("Recall Gmail", "ivan.test@example.com") == PW
    dump = "\n".join(sqlite3.connect(db).iterdump())
    assert PW not in dump
    assert PW not in caplog.text
    assert PW not in list(map(str, _walk(ma.list_accounts(db))))
    assert PW not in list(map(str, _walk(ma.status(db))))


def test_add_existing_replaces_password_keeps_labels(db):
    acc = ma.add_account(db, ADDR, PW)
    ma.upsert_folders(db, acc["id"], [{"folder": "INBOX", "display_name": "Інвестори"}])
    again = ma.add_account(db, "ivan.test@example.com", "new-pass-9")
    assert again["id"] == acc["id"]
    assert ma.get_password("ivan.test@example.com") == "new-pass-9"
    assert len(ma.list_labels(db, acc["id"])) == 1


def test_add_account_insert_failure_removes_keyring(tmp_path):
    p = str(tmp_path / "empty.db")  # no mail_accounts table: insert fails
    sqlite3.connect(p).close()
    with pytest.raises(sqlite3.Error):
        ma.add_account(p, "x@example.com", PW)
    assert keyring.get_password("Recall Gmail", "x@example.com") is None


def test_delete_account(db):
    acc = ma.add_account(db, "a@example.com", PW)
    ma.upsert_folders(db, acc["id"], [{"folder": "INBOX", "display_name": "Вхідні"}])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transcriptions (source_name, source_type, mail_account_id, mail_message_id, mail_folder) "
                 "VALUES ('m', 'email', ?, '<m1>', 'INBOX')", (acc["id"],))
    conn.commit()
    conn.close()
    assert ma.delete_account(db, acc["id"]) is True
    assert ma.list_accounts(db) == [] and ma.list_labels(db) == []
    assert keyring.get_password("Recall Gmail", "a@example.com") is None
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0] == 1
    # a missing keyring entry is swallowed
    acc2 = ma.add_account(db, "b@example.com", PW)
    keyring.delete_password("Recall Gmail", "b@example.com")
    assert ma.delete_account(db, acc2["id"]) is True
    assert ma.delete_account(db, 9999) is False


def test_folders_and_labels(db):
    acc = ma.add_account(db, "a@example.com", PW)
    labels = ma.upsert_folders(db, acc["id"], [{"folder": "Інвестори", "display_name": "Інвестори"},
                                               {"folder": "[Gmail]/Spam", "display_name": "Spam", "system": True}])
    assert all(lb["enabled"] == 0 and lb["enrich"] == 0 for lb in labels)
    lid = labels[0]["id"]
    upd = ma.update_label(db, lid, enabled=True, category_id=3)
    assert upd["enabled"] == 1 and upd["category_id"] == 3 and upd["enrich"] == 0
    assert ma.update_label(db, lid, enrich=True)["category_id"] == 3
    ma.upsert_folders(db, acc["id"], [{"folder": "Інвестори", "display_name": "Інвестори 2"}])
    kept = ma.list_labels(db, acc["id"])[0]
    assert kept["enabled"] == 1 and kept["category_id"] == 3 and kept["enrich"] == 1
    en = ma.enabled_labels(db)
    assert [lb["id"] for lb in en] == [lid] and en[0]["address"] == "a@example.com"


def test_status_counts_records(db):
    acc = ma.add_account(db, "a@example.com", PW)
    ma.upsert_folders(db, acc["id"], [{"folder": "INBOX", "display_name": "Вхідні"}])
    conn = sqlite3.connect(db)
    for mid, deleted in (("<1>", None), ("<2>", None), ("<3>", 123)):
        conn.execute("INSERT INTO transcriptions (source_name, source_type, mail_account_id, mail_message_id, mail_folder, "
                     "deleted_at) VALUES ('m', 'email', ?, ?, 'INBOX', ?)", (acc["id"], mid, deleted))
    conn.commit()
    conn.close()
    st = ma.status(db)
    assert st["accounts"][0]["labels"][0]["records"] == 2
    assert st["accounts"][0]["configured"] is True
