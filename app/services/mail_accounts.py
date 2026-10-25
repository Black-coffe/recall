"""Gmail accounts and labels (gmail-ingest-01).

An account row holds only the address. The app password lives in the OS credential
store through ``keyring`` (service ``KEYRING_SERVICE``, username = normalised address)
and never reaches the DB, .env, logs or any structure this module returns.
No flask/torch imports.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import keyring
import keyring.errors

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "Recall Gmail"


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()

_ACCOUNT_COLS = "id, address, created_at, last_poll_at, last_error_code"
_LABEL_COLS = ("id, account_id, folder, display_name, enabled, category_id, enrich, system, "
               "uidvalidity, last_uid, synced_since, last_sync_at, last_error_code")
_SYNC_FIELDS = {"uidvalidity", "last_uid", "synced_since", "last_sync_at", "last_error_code"}


@contextmanager
def _db(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def normalize_address(s: str) -> str:
    """Lowercase and strip; a value without exactly one ``@`` raises ValueError."""
    addr = (s or "").strip().lower()
    if addr.count("@") != 1:
        raise ValueError("invalid mail address")
    local, domain = addr.split("@")
    if not local or not domain:
        raise ValueError("invalid mail address")
    return addr


def get_password(address: str) -> Optional[str]:
    """The only reader of the stored app password."""
    return keyring.get_password(KEYRING_SERVICE, normalize_address(address))


def has_password(address: str) -> bool:
    return bool(get_password(address))


def _account_row(conn: sqlite3.Connection, account_id: int) -> Optional[dict]:
    row = conn.execute(f"SELECT {_ACCOUNT_COLS} FROM mail_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["configured"] = has_password(d["address"])
    return d


def add_account(db_path: str, address: str, app_password: str) -> dict:
    """Store the password in the keyring and insert the row; an existing address gets a new password."""
    addr = normalize_address(address)
    if not app_password:
        raise ValueError("empty app password")
    previous = keyring.get_password(KEYRING_SERVICE, addr)
    keyring.set_password(KEYRING_SERVICE, addr, app_password)
    try:
        with _db(db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO mail_accounts (address) VALUES (?)", (addr,))
            row = conn.execute("SELECT id FROM mail_accounts WHERE address = ?", (addr,)).fetchone()
            account = _account_row(conn, row["id"])
    except Exception:
        # Put the keyring back to what it held before this call.
        try:
            if previous is None:
                keyring.delete_password(KEYRING_SERVICE, addr)
            else:
                keyring.set_password(KEYRING_SERVICE, addr, previous)
        except keyring.errors.PasswordDeleteError:
            logger.debug("keyring entry already gone during add_account rollback")
        raise
    logger.info("Mail account added: id=%s", account["id"])
    return account


def connect_account(db_path: str, address: str, password: str) -> dict:
    """Connect sequence: IMAP LIST first, keyring and DB second.

    Returns ``{account, labels}``. Nothing is stored when the login or LIST fails.
    Raises ``ValueError("INVALID_ADDRESS")`` / ``ValueError("EMPTY_PASSWORD")``;
    ``mail_imap.MailError`` propagates with its ``.code``.
    """
    from app.services import mail_imap
    try:
        addr = normalize_address(address)
    except ValueError:
        raise ValueError("INVALID_ADDRESS") from None
    # Google shows app passwords in groups separated by spaces.
    pwd = "".join(str(password or "").split())
    if not pwd:
        raise ValueError("EMPTY_PASSWORD")
    with mail_imap.connect(addr, pwd) as client:
        folders = client.list_folders()
    account = add_account(db_path, addr, pwd)
    labels = upsert_folders(db_path, account["id"], folders)
    return {"account": account, "labels": labels}


def delete_account(db_path: str, account_id: int) -> bool:
    """Remove the account row, its labels and the keyring entry. Archived records stay."""
    with _db(db_path) as conn:
        row = conn.execute("SELECT address FROM mail_accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            return False
        addr = row["address"]
        conn.execute("DELETE FROM mail_labels WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM mail_accounts WHERE id = ?", (account_id,))
    try:
        keyring.delete_password(KEYRING_SERVICE, addr)
    except keyring.errors.PasswordDeleteError:
        logger.debug("no keyring entry to delete for account id=%s", account_id)
    return True


def disconnect_account(db_path: str, account_id: int) -> Optional[list[int]]:
    """Soft disconnect: drop the keyring entry and disable every label of the account.

    Cursors, directions and archived records stay, so a reconnect (``add_account`` reuses the
    row by address) resumes where it stopped instead of loading the window again. Returns the
    ids of the labels it disabled (a reconnect does not turn them back on; the caller names them
    so the owner can), or None when the account does not exist.
    """
    with _db(db_path) as conn:
        row = conn.execute("SELECT address FROM mail_accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            return None
        addr = row["address"]
        disabled = [r["id"] for r in conn.execute(
            "SELECT id FROM mail_labels WHERE account_id = ? AND enabled = 1 ORDER BY id", (account_id,))]
        conn.execute("UPDATE mail_labels SET enabled = 0 WHERE account_id = ?", (account_id,))
    try:
        keyring.delete_password(KEYRING_SERVICE, addr)
    except keyring.errors.PasswordDeleteError:
        logger.debug("no keyring entry to delete for account id=%s", account_id)
    logger.info("Mail account disconnected: id=%s, labels disabled=%s", account_id, len(disabled))
    return disabled


def list_accounts(db_path: str) -> list[dict]:
    with _db(db_path) as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM mail_accounts ORDER BY id")]
        return [_account_row(conn, i) for i in ids]


def upsert_folders(db_path: str, account_id: int, folders: list[dict]) -> list[dict]:
    """Insert new folders (disabled, no Claude card); existing rows keep their settings."""
    with _db(db_path) as conn:
        for f in folders:
            folder = f["folder"]
            conn.execute(
                "INSERT INTO mail_labels (account_id, folder, display_name, enabled, enrich, system) "
                "VALUES (?, ?, ?, 0, 0, ?) "
                "ON CONFLICT(account_id, folder) DO UPDATE SET "
                "display_name = excluded.display_name, system = excluded.system",
                (account_id, folder, f.get("display_name") or folder, 1 if f.get("system") else 0))
    return list_labels(db_path, account_id)


def list_labels(db_path: str, account_id: Optional[int] = None) -> list[dict]:
    sql = f"SELECT {_LABEL_COLS} FROM mail_labels"
    params: tuple = ()
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params = (account_id,)
    with _db(db_path) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]


def update_label(db_path: str, label_id: int, *, enabled: Any = UNSET, category_id: Any = UNSET,
                 enrich: Any = UNSET) -> dict:
    """Change only the passed fields."""
    sets, params = [], []
    if enabled is not UNSET:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if category_id is not UNSET:
        sets.append("category_id = ?")
        params.append(category_id)
    if enrich is not UNSET:
        sets.append("enrich = ?")
        params.append(1 if enrich else 0)
    with _db(db_path) as conn:
        if sets:
            conn.execute(f"UPDATE mail_labels SET {', '.join(sets)} WHERE id = ?", (*params, label_id))
        row = conn.execute(f"SELECT {_LABEL_COLS} FROM mail_labels WHERE id = ?", (label_id,)).fetchone()
    if row is None:
        raise KeyError(label_id)
    return dict(row)


def enabled_labels(db_path: str, account_id: Optional[int] = None) -> list[dict]:
    sql = ("SELECT l.id, l.account_id, l.folder, l.display_name, l.enabled, l.category_id, l.enrich, "
           "l.system, l.uidvalidity, l.last_uid, l.synced_since, l.last_sync_at, l.last_error_code, "
           "a.address FROM mail_labels l JOIN mail_accounts a ON a.id = l.account_id WHERE l.enabled = 1")
    params: tuple = ()
    if account_id is not None:
        sql += " AND l.account_id = ?"
        params = (account_id,)
    with _db(db_path) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY l.id", params)]


def record_label_sync(db_path: str, label_id: int, **fields: Any) -> None:
    bad = set(fields) - _SYNC_FIELDS
    if bad:
        raise ValueError(f"unknown label sync fields: {sorted(bad)}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _db(db_path) as conn:
        conn.execute(f"UPDATE mail_labels SET {sets} WHERE id = ?", (*fields.values(), label_id))


def record_account_poll(db_path: str, account_id: int, *, error_code: Optional[str] = None) -> None:
    with _db(db_path) as conn:
        conn.execute("UPDATE mail_accounts SET last_poll_at = CURRENT_TIMESTAMP, last_error_code = ? "
                     "WHERE id = ?", (error_code, account_id))


def status(db_path: str) -> dict:
    out = []
    with _db(db_path) as conn:
        counts = {(r["a"], r["f"]): r["n"] for r in conn.execute(
            "SELECT mail_account_id a, mail_folder f, COUNT(*) n FROM transcriptions "
            "WHERE mail_account_id IS NOT NULL AND deleted_at IS NULL GROUP BY 1, 2")}
        labels = [dict(r) for r in conn.execute(f"SELECT {_LABEL_COLS} FROM mail_labels ORDER BY id")]
        for acc in conn.execute(f"SELECT {_ACCOUNT_COLS} FROM mail_accounts ORDER BY id").fetchall():
            a = dict(acc)
            out.append({
                "id": a["id"], "address": a["address"], "configured": has_password(a["address"]),
                "last_poll_at": a["last_poll_at"], "last_error_code": a["last_error_code"],
                "labels": [{
                    "id": lb["id"], "folder": lb["folder"], "display_name": lb["display_name"],
                    "enabled": lb["enabled"], "system": lb["system"], "category_id": lb["category_id"],
                    "enrich": lb["enrich"], "last_sync_at": lb["last_sync_at"],
                    "synced_since": lb["synced_since"], "last_uid": lb["last_uid"],
                    "last_error_code": lb["last_error_code"],
                    "records": counts.get((a["id"], lb["folder"]), 0),
                } for lb in labels if lb["account_id"] == a["id"]],
            })
    return {"accounts": out}
