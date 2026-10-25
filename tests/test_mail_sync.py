"""mail_sync: label sync, dedup, attachments, errors, poller (fake IMAP, fake keyring, temp DB)."""
import logging
import sqlite3
from datetime import date
from email.message import EmailMessage

import pytest

from app.db.migrations import init_database
from app.services import enrichment, mail_accounts, mail_imap, mail_sync

TODAY = date(2026, 10, 16)


def make_raw(mid, subject="Звіт за тиждень", body="Привіт, команда. Звіт готовий.", attachments=(),
             day="Tue, 13 Oct 2026 10:00:00 +0300"):
    m = EmailMessage()
    m["Message-ID"] = mid
    m["Subject"] = subject
    m["From"] = "Олена <olena@example.com>"
    m["To"] = "me@example.com"
    m["Date"] = day
    m.set_content(body)
    for name, data in attachments:
        m.add_attachment(data, subtype="plain", filename=name)
    return m.as_bytes()


class FakeImap:
    def __init__(self, folders, uidvalidity=1):
        self.folders = folders          # folder -> {uid: raw}
        self.uidvalidity = uidvalidity
        self.calls = []
        self.current = None

    def examine(self, folder):
        if folder not in self.folders:
            raise mail_imap.MailError(mail_imap.FOLDER_MISSING, "nope")
        self.current = folder
        return {"uidvalidity": self.uidvalidity, "exists": len(self.folders[folder])}

    def search_since(self, since, before=None):
        self.calls.append(("since", since, before))
        return sorted(self.folders[self.current])

    def search_after_uid(self, uid):
        self.calls.append(("after", uid))
        return sorted(u for u in self.folders[self.current] if u > uid)

    def fetch_raw(self, uids, batch=50):
        for u in uids:
            yield u, self.folders[self.current][u]

    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    init_database(db)
    from config import Config
    monkeypatch.setattr(Config, "DOCUMENTS_FOLDER", str(tmp_path / "documents"), raising=False)
    monkeypatch.setattr(mail_sync, "_submit_embed", lambda *a, **k: "stub")
    monkeypatch.setattr(mail_sync.document_ingest, "submit_enrichment", lambda *a, **k: "stub")
    acc = mail_accounts.add_account(db, "me@example.com", "s3cret-пароль")
    mail_accounts.upsert_folders(db, acc["id"], [{"folder": "INBOX"}, {"folder": "Work"}])
    labels = {lb["folder"]: lb for lb in mail_accounts.list_labels(db, acc["id"])}
    fake = FakeImap({"INBOX": {1: make_raw("<a@x>"), 2: make_raw("<b@x>", subject="")}, "Work": {}})
    connects = []

    def connect(address, password, **kw):
        connects.append((address, password))
        return fake

    monkeypatch.setattr(mail_imap, "connect", connect)
    return {"db": db, "acc": acc["id"], "labels": labels, "fake": fake, "connects": connects}


def rows(db, sql="SELECT * FROM transcriptions WHERE source_type='email' ORDER BY id"):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql)]
    finally:
        c.close()


def test_no_enabled_label_no_connection(env):
    assert mail_sync.sync_all(env["db"], today=TODAY) == []
    assert env["connects"] == []


def test_first_sync_then_incremental(env):
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True)
    mail_sync.sync_all(env["db"], today=TODAY)
    assert env["fake"].calls == [("since", date(2025, 10, 16), None)]
    lb = mail_accounts.list_labels(env["db"], env["acc"])[0]
    assert (lb["uidvalidity"], lb["last_uid"], lb["synced_since"]) == (1, 2, "2025-10-16")
    assert lb["last_sync_at"] and lb["last_error_code"] is None
    env["fake"].folders["INBOX"][3] = make_raw("<c@x>")
    env["fake"].calls.clear()
    mail_sync.sync_all(env["db"], today=TODAY)
    assert env["fake"].calls == [("after", 2)]
    assert len(rows(env["db"])) == 3


def test_row_shape(env):
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True, category_id=None, enrich=True)
    mail_sync.sync_all(env["db"], today=TODAY)
    r1, r2 = rows(env["db"])
    assert r1["source_type"] == "email" and r1["title"] == "Звіт за тиждень"
    assert r1["source_name"] == "Звіт за тиждень" and r1["meeting_date"] == "2026-10-13"
    assert r1["mail_folder"] == "INBOX" and r1["mail_enrich"] == 1 and r1["mail_account_id"] == env["acc"]
    assert r2["title"] is None and "olena@example.com" in r2["source_name"]


def test_uidvalidity_change_no_duplicates(env):
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True)
    mail_sync.sync_all(env["db"], today=TODAY)
    env["fake"].uidvalidity = 7
    env["fake"].calls.clear()
    mail_sync.sync_all(env["db"], today=TODAY)
    assert env["fake"].calls == [("since", date(2025, 10, 16), None)]
    assert len(rows(env["db"])) == 2
    assert mail_accounts.list_labels(env["db"], env["acc"])[0]["uidvalidity"] == 7


def test_load_deeper_gap_only_and_idempotent(env):
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True)
    mail_sync.sync_all(env["db"], today=TODAY)
    env["fake"].calls.clear()
    mail_sync.sync_account(env["db"], env["acc"], months=24, today=TODAY)
    assert env["fake"].calls == [("since", date(2024, 10, 16), date(2025, 10, 16))]
    assert mail_accounts.list_labels(env["db"], env["acc"])[0]["synced_since"] == "2024-10-16"
    env["fake"].calls.clear()
    mail_sync.sync_account(env["db"], env["acc"], months=24, today=TODAY)
    assert env["fake"].calls == []
    assert len(rows(env["db"])) == 2


def test_two_labels_one_row(env):
    for f in ("INBOX", "Work"):
        mail_accounts.update_label(env["db"], env["labels"][f]["id"], enabled=True)
    env["fake"].folders["Work"] = {5: make_raw("<a@x>")}
    mail_sync.sync_all(env["db"], today=TODAY)
    rs = rows(env["db"])
    assert len(rs) == 2
    assert [r["mail_folder"] for r in rs if r["mail_message_id"] == "<a@x>"] == ["INBOX"]


def test_integrity_race_is_duplicate(env, monkeypatch):
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True)
    monkeypatch.setattr(mail_sync, "_find_email", lambda *a: None)
    mail_sync.sync_all(env["db"], today=TODAY)
    mail_sync.sync_all(env["db"], months=24, today=TODAY)
    assert len(rows(env["db"])) == 2


def test_attachments(env, monkeypatch):
    lid = env["labels"]["INBOX"]["id"]
    mail_accounts.update_label(env["db"], lid, enabled=True)
    monkeypatch.setenv("MAIL_ATTACH_MAX_MB", "1")
    big = "x" * (1024 * 1024 + 10)
    env["fake"].folders["INBOX"] = {1: make_raw("<z@x>", attachments=[
        ("звіт.txt", "Квартальний звіт: пароль: hunter22secret"), ("big.txt", big)])}
    mail_sync.sync_all(env["db"], today=TODAY)
    docs = rows(env["db"], "SELECT * FROM transcriptions WHERE source_type='document'")
    assert len(docs) == 1 and docs[0]["original_filename"] == "звіт.txt"
    assert docs[0]["mail_enrich"] == 0 and "hunter22secret" not in docs[0]["transcript_text"]
    links = rows(env["db"], "SELECT * FROM mail_attachments")
    assert len(links) == 1 and links[0]["filename"] == "звіт.txt"
    assert "documents" in docs[0]["file_path"] and "mail" in docs[0]["file_path"]
    # The original on disk is masked too (get_original_file serves it verbatim).
    with open(docs[0]["file_path"], encoding="utf-8") as f:
        stored = f.read()
    assert "hunter22secret" not in stored and "Квартальний звіт" in stored


def test_text_attachment_masked_on_disk_tokens_and_cp1251(env):
    lid = env["labels"]["INBOX"]["id"]
    mail_accounts.update_label(env["db"], lid, enabled=True)
    creds = ("api_key = sk-ant-api03-AAAAbbbbCCCCddddEEEEffffGGGGhhhh\n"
             "password: S3cr3tPass!77\ntoken ghp_abcdefABCDEF0123456789abcdefABCDEF0123\n")
    m = EmailMessage()
    m["Message-ID"] = "<creds@x>"
    m["Subject"] = "доступи"
    m["From"] = "Олена <olena@example.com>"
    m["Date"] = "Tue, 13 Oct 2026 10:00:00 +0300"
    m.set_content("див. вкладення")
    m.add_attachment(creds.encode("utf-8"), maintype="text", subtype="plain", filename="creds.txt")
    m.add_attachment("пароль: hunter22secret\nрядок".encode("cp1251"), maintype="application",
                     subtype="octet-stream", filename="notes.md")
    env["fake"].folders["INBOX"] = {1: m.as_bytes()}
    mail_sync.sync_all(env["db"], today=TODAY)
    docs = rows(env["db"], "SELECT * FROM transcriptions WHERE source_type='document' ORDER BY id")
    assert [d["original_filename"] for d in docs] == ["creds.txt", "notes.md"]
    for d in docs:
        with open(d["file_path"], encoding="utf-8") as f:
            stored = f.read()
        for secret in ("sk-ant-api03", "S3cr3tPass!77", "ghp_abcdef", "hunter22secret"):
            assert secret not in stored, d["original_filename"]
            assert secret not in d["transcript_text"], d["original_filename"]
    with open(docs[1]["file_path"], encoding="utf-8") as f:
        assert "рядок" in f.read()


def _docx_bytes(text):
    import io

    import docx
    d = docx.Document()
    for line in text.splitlines():
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_binary_attachment_original_kept_text_masked(env):
    lid = env["labels"]["INBOX"]["id"]
    mail_accounts.update_label(env["db"], lid, enabled=True)
    m = EmailMessage()
    m["Message-ID"] = "<bin@x>"
    m["Subject"] = "доступи"
    m["From"] = "Олена <olena@example.com>"
    m["Date"] = "Tue, 13 Oct 2026 10:00:00 +0300"
    m.set_content("див. вкладення")
    docx_ct = ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
    m.add_attachment(_docx_bytes("Доступ до кабінету\nПароль до кабінету: Qwerty!2345zz"),
                     maintype=docx_ct[0], subtype=docx_ct[1], filename="access.docx")
    m.add_attachment(_docx_bytes("Звіт за жовтень\nвиручка росте"),
                     maintype=docx_ct[0], subtype=docx_ct[1], filename="report.docx")
    env["fake"].folders["INBOX"] = {1: m.as_bytes()}
    mail_sync.sync_all(env["db"], today=TODAY)
    docs = {d["original_filename"]: d for d in
            rows(env["db"], "SELECT * FROM transcriptions WHERE source_type='document'")}
    secret_doc, clean_doc = docs["access.docx"], docs["report.docx"]
    assert "Qwerty!2345zz" not in secret_doc["transcript_text"]
    assert "[🔒 секрет]" in secret_doc["transcript_text"]
    # Binary originals stay on disk as is (same as manual documents, owner-accepted).
    import os
    assert secret_doc["file_path"] and os.path.exists(secret_doc["file_path"])
    assert clean_doc["file_path"] and os.path.exists(clean_doc["file_path"])
    assert len(rows(env["db"], "SELECT * FROM mail_attachments")) == 2


def test_enrich_off_means_no_claude(env, monkeypatch):
    calls = []
    monkeypatch.setattr(mail_sync.document_ingest, "submit_enrichment", lambda *a, **k: calls.append(a))
    lid = env["labels"]["INBOX"]["id"]
    mail_accounts.update_label(env["db"], lid, enabled=True)
    mail_sync.sync_all(env["db"], today=TODAY)
    assert calls == []
    mail_accounts.update_label(env["db"], lid, enrich=True)
    env["fake"].folders["INBOX"][9] = make_raw("<n@x>")
    mail_sync.sync_all(env["db"], today=TODAY)
    assert len(calls) == 1


def test_errors_and_isolation(env, caplog):
    for f in ("INBOX", "Work"):
        mail_accounts.update_label(env["db"], env["labels"][f]["id"], enabled=True)
    del env["fake"].folders["Work"]
    mail_sync.sync_all(env["db"], today=TODAY)
    lbs = {lb["folder"]: lb for lb in mail_accounts.list_labels(env["db"], env["acc"])}
    assert lbs["Work"]["last_error_code"] == "FOLDER_MISSING" and lbs["INBOX"]["last_error_code"] is None
    assert len(rows(env["db"])) == 2
    env["fake"].folders["Work"] = {}
    mail_sync.sync_all(env["db"], today=TODAY)
    assert mail_accounts.list_labels(env["db"], env["acc"])[1]["last_error_code"] is None


def test_auth_failure_no_password_in_logs(env, monkeypatch, caplog):
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True)

    def bad(address, password, **kw):
        raise mail_imap.MailError(mail_imap.AUTH_FAILED, "login rejected", password)

    monkeypatch.setattr(mail_imap, "connect", bad)
    with caplog.at_level(logging.DEBUG):
        mail_sync.sync_all(env["db"], today=TODAY)
    assert "s3cret-пароль" not in caplog.text
    assert mail_accounts.list_accounts(env["db"])[0]["last_error_code"] == "AUTH_FAILED"


def test_no_password(env):
    import keyring
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True)
    keyring.delete_password(mail_accounts.KEYRING_SERVICE, "me@example.com")
    mail_sync.sync_all(env["db"], today=TODAY)
    assert mail_accounts.list_accounts(env["db"])[0]["last_error_code"] == "NO_PASSWORD"
    assert env["connects"] == []


def test_poller_iteration_and_trigger(env):
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True)
    p = mail_sync.MailPoller(env["db"])
    p.trigger(env["acc"])
    p.run_once()
    assert len(rows(env["db"])) == 2
    p.run_once()
    assert len(env["connects"]) == 2


def test_poller_thread_stops_fast(env, caplog):
    p = mail_sync.MailPoller(env["db"])
    with caplog.at_level("INFO", logger=mail_sync.logger.name):
        p.start()
    assert p.is_alive()
    assert any(r.getMessage().startswith("Mail poller started (every ") for r in caplog.records)
    p.stop()
    assert not p.is_alive()


# ---- enrichment guard ----

def _mk(db, source_type, mail_enrich):
    c = sqlite3.connect(db)
    cur = c.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, mail_enrich) "
                    "VALUES (?, 'n', 'текст', ?)", (source_type, mail_enrich))
    c.commit()
    tid = cur.lastrowid
    c.close()
    return tid


def test_enrichment_guard(env, monkeypatch):
    db = env["db"]
    off, on, plain = _mk(db, "email", 0), _mk(db, "email", 1), _mk(db, "document", None)
    c = sqlite3.connect(db)
    c.execute("UPDATE transcriptions SET embedded_at = CURRENT_TIMESTAMP, embedding_model = ?, embedding_version = ?",
              (enrichment.embeddings.EMBED_MODEL, enrichment.embeddings.EMBED_VERSION))
    c.commit()
    c.close()
    ids = enrichment.list_unenriched_ids(db)
    assert off not in ids and on in ids and plain in ids
    assert enrichment._enrich_card(db, off)["reason"] == "mail_enrich_off"
    monkeypatch.setattr(enrichment.text_polishing, "is_available", lambda: False)
    assert enrichment._enrich_card(db, plain).get("reason") != "mail_enrich_off"


def test_failed_store_is_retried_next_poll(env, monkeypatch):
    import sqlite3
    lid = env["labels"]["INBOX"]["id"]
    mail_accounts.update_label(env["db"], lid, enabled=True)
    mail_sync.sync_all(env["db"], today=TODAY)
    env["fake"].folders["INBOX"][9] = make_raw("<m9@x>", subject="m9")
    real, calls = mail_sync._insert_email, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(mail_sync, "_insert_email", flaky)
    mail_sync.sync_all(env["db"], today=TODAY)
    assert not [r for r in rows(env["db"]) if r["mail_message_id"] == "<m9@x>"]
    mail_sync.sync_all(env["db"], today=TODAY)
    assert [r for r in rows(env["db"]) if r["mail_message_id"] == "<m9@x>"]


def test_prose_basic_word_not_masked_original_kept(env):
    import os
    lid = env["labels"]["INBOX"]["id"]
    mail_accounts.update_label(env["db"], lid, enabled=True)
    m = EmailMessage()
    m["Message-ID"] = "<fp@x>"
    m["Subject"] = "план"
    m["From"] = "a@example.com"
    m["Date"] = "Tue, 13 Oct 2026 10:00:00 +0300"
    m.set_content("див. вкладення")
    ct = ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
    m.add_attachment(_docx_bytes("Project plan\nBasic information about the project is below."),
                     maintype=ct[0], subtype=ct[1], filename="plan.docx")
    env["fake"].folders["INBOX"] = {1: m.as_bytes()}
    mail_sync.sync_all(env["db"], today=TODAY)
    d = rows(env["db"], "SELECT * FROM transcriptions WHERE source_type='document'")[0]
    assert "Basic information about the project" in d["transcript_text"]
    assert d["file_path"] and os.path.exists(d["file_path"])


def test_first_months_cap_limits_first_sync_only(env, monkeypatch):
    monkeypatch.setenv("MAIL_BACKFILL_MONTHS", "24")
    mail_accounts.update_label(env["db"], env["labels"]["INBOX"]["id"], enabled=True)
    p = mail_sync.MailPoller(env["db"])
    p.trigger(env["acc"], first_months_cap=12)
    p.trigger(env["acc"], None, 12)
    assert [r[2] for r in p._requests] == [12, 12]
    p.run_once()
    assert env["fake"].calls[0] == ("since", mail_sync._months_ago(date.today(), 12), None)
    env["fake"].calls.clear()
    mail_sync.sync_all(env["db"], today=TODAY, first_months_cap=12)
    assert env["fake"].calls == [("after", 2)]
