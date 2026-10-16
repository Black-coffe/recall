"""RFC 822 bytes -> ParsedEmail (stdlib + screen_secrets). No DB, no network."""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional

from app.services import screen_secrets

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes
    inline: bool = False


@dataclass
class ParsedEmail:
    message_id: str
    in_reply_to: Optional[str]
    references: list = field(default_factory=list)
    thread_key: str = ""
    subject: str = ""
    sender: str = ""
    to: str = ""
    date: Optional[datetime] = None
    meeting_date: Optional[str] = None
    body: str = ""
    attachments: list = field(default_factory=list)


_LABEL = (r"(?:пароль|пароля|паролі|паролем|password|passwd|passphrase|pass|pwd|pin|пін|пин|"
          r"token|токен|secret|секрет|api[-_ ]?key|apikey|access[-_ ]?key|secret[-_ ]?key|ключ|"
          r"client[-_ ]?secret|authorization)")
# "<label> [up to 3 words]: value" in mail prose: "Пароль до кабінету: X", "Password - X",
# "PIN-code: X", "app password: abcd efgh ijkl mnop". The whole rest of the line is the value:
# a Google app password has spaces, and an error goes toward hiding.
_MAIL_LABEL = re.compile(
    r"(?im)^(?P<pre>[^\n]*?\b" + _LABEL + r"\b(?:[ \t-]+[\w'’]+){0,3}?(?:[ \t]*[:=]|[ \t]+[-–—]))"
    r"[ \t]*(?P<val>\S[^\n]*?)[ \t]*$")
# A label alone at the end of its line: the value sits on the next non-empty line
# ("Password:\nX", HTML "<p>Пароль:</p><p>X</p>").
_LABEL_EOL = re.compile(
    r"(?i)^[^\n]{0,60}?\b" + _LABEL + r"\b(?:[ \t-]+[\w'’]+){0,3}?[ \t]*(?P<sep>[:=\-–—])?[ \t]*$")
# A label word inside prose: "логин admin, пароль X", "The password is X", "<td>Пароль</td><td>X</td>".
_LABEL_WORD = re.compile(r"(?i)\b" + _LABEL + r"\b")
_APP_PASSWORD = re.compile(r"^[a-z]{4}(?:[ \t]+[a-z]{4}){3}\b")
_TOKEN = re.compile(r"[^\s,;]+")
# One-time / confirmation codes: "Код подтверждения: 554433", "verification code 123 456".
# Only a code word with a confirmation qualifier counts; a bare "code" is a business id
# (tax id, product code, error code) and stays readable.
_CODE_KIND = (
    r"(?:(?:verification|confirmation|one[- ]?time|security|auth\w*|login|sign[- ]?in|2fa|sms|"
    r"одноразов\w*|перевірочн\w*|проверочн\w*|смс)[ \t-]+(?:код\w*|code)"
    r"|(?:код|code)[ \t]+(?:підтвердж\w*|подтвержд\w*|верифікац\w*|верификац\w*|перевірки|проверки|"
    r"доступу|доступа|входу|входа|авторизац\w*|для[ \t]+входу|для[ \t]+входа|з[ \t]+смс|из[ \t]+смс|"
    r"from[ \t]+sms)"
    r"|otp)")
_CODE = re.compile(
    r"(?i)\b(?P<pre>" + _CODE_KIND + r"\b(?:[ \t]+[\w'’]+){0,3}?[ \t]*[:=–—-]?[ \t]*)"
    r"(?P<val>\d{4,8}|\d{3}[ -]\d{3})\b")
# A token carries a digit or base64 symbols; a plain word ("Basic information") is prose.
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)[ \t]+(?=[A-Za-z._~\-]*[0-9+/=])[A-Za-z0-9._~+/=\-]{8,}")


def _looks_secret(tok: str) -> bool:
    """A password-shaped token: a digit, a symbol, or an inner capital ("Qw3rty!Zx", "TblPass99")."""
    if len(tok) < 4:
        return False
    return bool(re.search(r"\d|[!@#$%^&*_+=?~|<>/\\]", tok) or re.search(r"[^\W\d_][A-ZА-ЯІЇЄҐ]", tok))


def _mask_inline(seg: str) -> str:
    out, pos = [], 0
    for m in _LABEL_WORD.finditer(seg):
        if m.start() < pos:
            continue
        rest = seg[m.end():]
        toks = list(_TOKEN.finditer(rest))[:4]
        hit = None
        for i, t in enumerate(toks):
            tail = rest[t.start():]
            if _APP_PASSWORD.match(tail):
                hit = (t.start(), t.start() + _APP_PASSWORD.match(tail).end())
                break
            word = t.group(0).rstrip(".)!?\"'»")
            if word.strip(":=-–—") and _looks_secret(word):
                hit = (t.start(), t.start() + len(word))
                break
        if hit is None:
            continue
        a, b = m.end() + hit[0], m.end() + hit[1]
        out.append(seg[pos:a] + screen_secrets.MASK)
        pos = b
    out.append(seg[pos:])
    return "".join(out)


def _mask_next_line(text: str) -> str:
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        m = _LABEL_EOL.match(ln)
        if not m or screen_secrets.MASK in ln:
            continue
        j = i + 1
        while j < len(lines) and j <= i + 3 and not lines[j].strip():
            j += 1
        if j >= len(lines) or j > i + 3:
            continue
        nxt = lines[j].strip()
        if screen_secrets.MASK in nxt or len(nxt) > 80 or len(nxt.split()) > 4:
            continue
        if m.group("sep") or _looks_secret(nxt) or _APP_PASSWORD.match(nxt):
            lines[j] = lines[j][:len(lines[j]) - len(lines[j].lstrip())] + screen_secrets.MASK
    return "\n".join(lines)


def _mask_labels(text: str) -> str:
    def _sub(m):
        if screen_secrets.MASK in m.group("val"):
            return m.group(0)
        return m.group("pre") + " " + screen_secrets.MASK
    text = _MAIL_LABEL.sub(_sub, text)
    text = _mask_next_line(text)
    text = "\n".join(screen_secrets.MASK.join(_mask_inline(seg) for seg in ln.split(screen_secrets.MASK))
                     for ln in text.split("\n"))
    text = _CODE.sub(lambda m: m.group("pre") + screen_secrets.MASK, text)
    return _BEARER.sub(lambda m: m.group(1) + " " + screen_secrets.MASK, text)


def mask(text: Optional[str]) -> str:
    """Mask passwords and tokens (mail phrasings + screen_secrets) with the shared marker."""
    if not text:
        return text or ""
    text = _mask_labels(text)
    return screen_secrets.mask_text(text, screen_secrets.scan_text(text)) or ""


# --- HTML -> text ---------------------------------------------------------

# A forward is content, not a quote: everything from its marker on is kept.
_FORWARD = re.compile(
    r"^\s*(?:-{2,}\s*(?:Forwarded message|Пересылаемое сообщение|Пересланное сообщение|"
    r"Пересылаемое письмо|Переслане повідомлення|Пересланий лист)\s*-{2,}"
    r"|(?:Begin forwarded message|Начало переадресованного сообщения|"
    r"Початок пересланого повідомлення)\s*:)\s*$", re.I | re.M)

_BLOCK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}
_CELL = {"td", "th"}  # cells of one row stay apart on one line: "Пароль TblPass99"


class _Html2Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_tag: Optional[str] = None
        self._skip_depth = 0
        self._ignore = 0  # inside script/style
        self._cap: list[str] = []  # text of the skipped quote block, kept when it is a forward

    def _sink(self) -> list:
        return self._cap if self._skip_tag else self.out

    def handle_starttag(self, tag, attrs):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth += 1
            if tag in _BLOCK:
                self._cap.append("\n")
            elif tag in _CELL:
                self._cap.append(" ")
            return
        if tag in ("script", "style"):
            self._ignore += 1
            return
        classes = (dict(attrs).get("class") or "").split()
        if tag == "blockquote" or (tag == "div" and "gmail_quote" in classes):
            self._skip_tag, self._skip_depth, self._cap = tag, 1, []
            return
        if tag in _BLOCK:
            self.out.append("\n")
        elif tag in _CELL:
            self.out.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK:
            self._sink().append("\n")

    def handle_endtag(self, tag):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
                    captured = "".join(self._cap)
                    self._cap = []
                    # A forward is content, not a quote (Gmail wraps it in gmail_quote too).
                    if _FORWARD.search(captured.replace("\xa0", " ").strip()[:400]):
                        self.out.extend(["\n", captured, "\n"])
                    return
            if tag in _BLOCK:
                self._cap.append("\n")
            return
        if tag in ("script", "style"):
            self._ignore = max(0, self._ignore - 1)
            return
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._ignore:
            self._sink().append(data)


def html_to_text(html: str) -> str:
    p = _Html2Text()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001
        logger.debug("[mail] html parse failed, using the partial text")
    text = "".join(p.out).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --- quote stripping ------------------------------------------------------

_ON_WROTE = re.compile(r"^\s*On\b.+\bwrote:\s*$", re.I)
_ON_START = re.compile(r"^\s*On\b", re.I)
_WROTE_END = re.compile(r"\bwrote:\s*$", re.I)
_CYR_ATTR = re.compile(
    r"^\s*.+\s(пише|пишет|писал|писала|писал\(а\)|написав|написала|написав\(ла\)|написал|написал\(а\))"
    r"\s*:\s*$", re.I)
# Gmail ru/uk attribution without a verb: "ср, 2 сент. 2026 г. в 11:00, Ivan <ivan@x.com>:"
_ADDR_COLON = re.compile(r"<[^<>\s@]+@[^<>\s]+>\s*:\s*$")
# Outlook-style reply header: "From:" followed within a few lines by Sent:/Date:/Subject:/To:
_OUTLOOK_FROM = re.compile(r"^\s*>?\s*(From|Від|От|Von)\s*:\s*\S", re.I)
_OUTLOOK_NEXT = re.compile(
    r"^\s*>?\s*(Sent|Date|To|Cc|Subject|Надіслано|Відправлено|Отправлено|Кому|Тема|Дата|Копія|Копия)\s*:", re.I)
_OUTLOOK_SEP = re.compile(
    r"^\s*-{3,}\s*(Original Message|Исходное сообщение|Вихідне повідомлення|Оригінальне повідомлення)"
    r"\s*-{3,}\s*$", re.I)
_RULE = re.compile(r"^\s*[-_]{3,}\s*$")


def _is_attribution(line: str) -> bool:
    if _ON_WROTE.match(line):
        return True
    if _ADDR_COLON.search(line) and re.search(r"\d", line):
        return True
    return bool(_CYR_ATTR.match(line) and re.search(r"[\d@<]", line))


def _is_outlook_header(lines: list, i: int) -> bool:
    if not _OUTLOOK_FROM.match(lines[i]):
        return False
    return any(_OUTLOOK_NEXT.match(ln) for ln in lines[i + 1:i + 4])


def strip_quotes(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    kept: list[str] = []
    for i, ln in enumerate(lines):
        if _FORWARD.match(ln):
            kept.extend(lines[i:])
            break
        if _is_attribution(ln) or _OUTLOOK_SEP.match(ln):
            break
        if _is_outlook_header(lines, i):
            # Drop the separator rule Outlook puts above its header.
            while kept and (_RULE.match(kept[-1]) or not kept[-1].strip()):
                kept.pop()
            break
        if _WROTE_END.search(ln) and i > 0 and _ON_START.match(lines[i - 1]):
            if kept and kept[-1] is lines[i - 1]:
                kept.pop()
            break
        if ln.lstrip().startswith(">"):
            continue
        kept.append(ln)
    out = "\n".join(kept).strip()
    if out:
        return out
    # Only quote: keep the first non-quote lines rather than an empty body.
    own = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith(">")]
    return "\n".join(own).strip()


# --- message parsing ------------------------------------------------------

_ID_RE = re.compile(r"<[^<>\s]+>")


def _hdr(msg, name: str) -> str:
    try:
        return str(msg.get(name) or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("[mail] undecodable header %s", name)
        return ""


def _ids(value: str) -> list[str]:
    return _ID_RE.findall(value or "")


def _local_day(msg) -> tuple[Optional[datetime], Optional[str]]:
    raw = _hdr(msg, "Date")
    if not raw:
        return None, None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        logger.debug("[mail] broken Date header %r", raw)
        return None, None
    if dt is None:
        return None, None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt, dt.date().isoformat()


def _text_of(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError, KeyError):
        payload = part.get_payload(decode=True) or b""
        try:
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")


def _body_text(msg) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = _text_of(plain)
        if text.strip():
            return text
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        return html_to_text(_text_of(html))
    return ""


def _attachments(msg) -> list[Attachment]:
    out = []
    try:
        parts = list(msg.iter_attachments())
    except Exception:  # noqa: BLE001
        logger.debug("[mail] could not iterate attachments")
        return out
    for part in parts:
        try:
            ctype = part.get_content_type()
            if ctype == "message/rfc822":
                inner = part.get_payload()
                data = inner[0].as_bytes() if inner else b""
            else:
                data = part.get_payload(decode=True) or b""
            name = part.get_filename() or ""
            disp = (part.get_content_disposition() or "").lower()
            inline = ctype.startswith("image/") and (bool(part.get("Content-ID")) or disp == "inline")
            out.append(Attachment(filename=str(name), content_type=ctype, data=data, inline=inline))
        except Exception:  # noqa: BLE001
            logger.debug("[mail] skipped an unreadable attachment part")
    return out


def parse_message(raw: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    mid_ids = _ids(_hdr(msg, "Message-ID"))
    message_id = mid_ids[0] if mid_ids else f"<{hashlib.sha1(raw).hexdigest()}@recall.local>"
    irt = _ids(_hdr(msg, "In-Reply-To"))
    in_reply_to = irt[0] if irt else None
    refs = _ids(_hdr(msg, "References"))
    thread_key = refs[0] if refs else (in_reply_to or message_id)
    date, day = _local_day(msg)
    try:
        body = mask(strip_quotes(_body_text(msg)))
    except Exception:  # noqa: BLE001
        logger.warning("[mail] body extraction failed")
        body = ""
    return ParsedEmail(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=refs,
        thread_key=thread_key,
        subject=mask(_hdr(msg, "Subject")),
        sender=_hdr(msg, "From"),
        to=_hdr(msg, "To"),
        date=date,
        meeting_date=day,
        body=body,
        attachments=_attachments(msg),
    )
