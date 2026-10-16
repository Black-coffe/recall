"""Read-only IMAP client (stdlib imaplib only).

The wrapper exposes a fixed set of read methods. There is no passthrough to the
underlying connection, so nothing here can STORE, EXPUNGE, APPEND, COPY, MOVE,
CREATE, DELETE, RENAME or SUBSCRIBE. Mailboxes are opened with EXAMINE (readonly)
and messages are fetched with BODY.PEEK[] so the \\Seen flag is never touched.
"""
from __future__ import annotations

import base64
import datetime as _dt
import imaplib
import logging
import re
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)

GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993

AUTH_FAILED = "AUTH_FAILED"
CONNECT_FAILED = "CONNECT_FAILED"
FOLDER_MISSING = "FOLDER_MISSING"
PROTOCOL = "PROTOCOL"

_SYSTEM_FLAGS = {"\\all", "\\trash", "\\junk", "\\drafts", "\\important", "\\flagged"}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def scrub(text, *secrets) -> str:
    """Remove every secret value from text (plain replace, so Cyrillic neighbours are covered)."""
    out = str(text) if text is not None else ""
    for s in sorted({x for x in secrets if x}, key=len, reverse=True):
        out = out.replace(s, "***")
    return out


class MailError(Exception):
    """Mail wire error. `code` is one of AUTH_FAILED, CONNECT_FAILED, FOLDER_MISSING, PROTOCOL."""

    def __init__(self, code: str, message: str = "", *secrets):
        self.code = code
        self.message = scrub(message, *secrets)
        super().__init__(f"{code}: {self.message}" if self.message else code)


def encode_mutf7(s: str) -> str:
    """Modified UTF-7 (RFC 3501): UTF-16BE base64, ',' for '/', no padding."""
    out: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            out.append("&" + b64 + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7e:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def decode_mutf7(s: str) -> str:
    def repl(m):
        chunk = m.group(1)
        if not chunk:
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64).decode("utf-16-be")

    try:
        return re.sub(r"&([^-]*)-", repl, s)
    except (ValueError, UnicodeDecodeError):
        logger.debug("[mail] undecodable mUTF-7 folder name, keeping raw")
        return s


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL|\S+)\s+(?P<name>.+)$', re.S)


def _parse_list_line(line) -> Optional[dict]:
    if isinstance(line, tuple):  # literal: (b'(...) "/" {n}', b'name')
        m = _LIST_RE.match(line[0].split(b" {")[0] + b" X")
        if not m:
            return None
        name = line[1].decode("ascii", "replace")
    else:
        m = _LIST_RE.match(line)
        if not m:
            return None
        name = m.group("name").decode("ascii", "replace").strip()
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
    flags = m.group("flags").decode("ascii", "replace").split()
    return {"folder": name, "flags": flags}


def _quote(name: str) -> str:
    return '"' + encode_mutf7(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(d) -> str:
    """dd-Mon-yyyy with English month names whatever the locale."""
    if isinstance(d, _dt.datetime):
        d = d.date()
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


class ReadOnlyImap:
    """Fixed read-only surface over an imaplib connection."""

    def __init__(self, conn, secrets: tuple = ()):
        self._conn = conn
        self._secrets = secrets

    def _err(self, code: str, exc) -> MailError:
        return MailError(code, f"{type(exc).__name__}: {exc}", *self._secrets)

    def list_folders(self) -> list[dict]:
        try:
            typ, data = self._conn.list()
        except (imaplib.IMAP4.error, OSError) as exc:
            raise self._err(PROTOCOL, exc) from None
        if typ != "OK":
            raise MailError(PROTOCOL, "LIST failed")
        out = []
        for line in data or []:
            item = _parse_list_line(line)
            if not item:
                continue
            flags = item["flags"]
            low = {f.lower() for f in flags}
            if "\\noselect" in low:
                continue
            decoded = decode_mutf7(item["folder"])
            out.append({
                "folder": decoded,
                "display_name": decoded,
                "flags": flags,
                "system": bool(low & _SYSTEM_FLAGS),
            })
        return out

    def examine(self, folder: str) -> dict:
        try:
            typ, data = self._conn.select(_quote(folder), readonly=True)
        except imaplib.IMAP4.error as exc:
            raise MailError(FOLDER_MISSING, str(exc), *self._secrets) from None
        except OSError as exc:
            raise self._err(PROTOCOL, exc) from None
        if typ != "OK":
            raise MailError(FOLDER_MISSING, f"folder not found: {folder}")
        try:
            exists = int(data[0])
            _t, uv = self._conn.response("UIDVALIDITY")
            uidvalidity = int(uv[0])
        except (TypeError, ValueError, IndexError):
            raise MailError(PROTOCOL, "bad EXAMINE response") from None
        return {"uidvalidity": uidvalidity, "exists": exists}

    def _uid_search(self, *criteria) -> list[int]:
        try:
            typ, data = self._conn.uid("SEARCH", *criteria)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise self._err(PROTOCOL, exc) from None
        if typ != "OK":
            raise MailError(PROTOCOL, "SEARCH failed")
        raw = data[0] if data else b""
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", "replace")
        return sorted(int(x) for x in (raw or "").split() if x.isdigit())

    def search_since(self, since, before=None) -> list[int]:
        crit = ["SINCE", _imap_date(since)]
        if before is not None:
            crit += ["BEFORE", _imap_date(before)]
        return self._uid_search(*crit)

    def search_after_uid(self, uid: int) -> list[int]:
        found = self._uid_search("UID", f"{int(uid) + 1}:*")
        # "N+1:*" always returns the last message even when it is <= uid
        return [u for u in found if u > int(uid)]

    def fetch_raw(self, uids, batch: int = 50) -> Iterator[tuple[int, bytes]]:
        uids = [int(u) for u in uids]
        step = max(1, int(batch))
        for i in range(0, len(uids), step):
            chunk = uids[i:i + step]
            try:
                typ, data = self._conn.uid("FETCH", ",".join(map(str, chunk)), "(BODY.PEEK[])")
            except (imaplib.IMAP4.error, OSError) as exc:
                raise self._err(PROTOCOL, exc) from None
            if typ != "OK":
                raise MailError(PROTOCOL, "FETCH failed")
            for item in data or []:
                if not isinstance(item, tuple):
                    continue
                m = re.search(rb"UID\s+(\d+)", item[0])
                if m:
                    yield int(m.group(1)), item[1]

    def close(self) -> None:
        try:
            self._conn.logout()
        except (imaplib.IMAP4.error, OSError) as exc:
            logger.debug("[mail] logout failed (ignored): %s", type(exc).__name__)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def connect(address: str, password: str, *, host: str = GMAIL_HOST, port: int = GMAIL_PORT,
            timeout: int = 30, factory: Optional[Callable] = None) -> ReadOnlyImap:
    factory = factory or (lambda h, p, t: imaplib.IMAP4_SSL(h, p, timeout=t))
    try:
        conn = factory(host, port, timeout)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise MailError(CONNECT_FAILED, type(exc).__name__, password) from None
    try:
        conn.debug = 0
    except AttributeError:
        logger.debug("[mail] connection object has no debug attribute")
    try:
        conn.login(address, password)
    except imaplib.IMAP4.error:
        # The raw server text may echo the credentials: drop it entirely.
        _safe_logout(conn)
        raise MailError(AUTH_FAILED, "login rejected") from None
    except OSError as exc:
        raise MailError(CONNECT_FAILED, type(exc).__name__, password) from None
    return ReadOnlyImap(conn, (password,))


def _safe_logout(conn) -> None:
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        logger.debug("[mail] logout after failed login ignored")
