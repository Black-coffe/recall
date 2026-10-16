"""MIME -> text parser."""
import base64
from email.message import EmailMessage

from app.services import mail_parse
from app.services.mail_parse import parse_message, strip_quotes


def _raw(body_bytes, charset, cte="8bit", ctype="text/plain", extra=""):
    head = (
        "From: =?utf-8?B?" + base64.b64encode("Олена Тест".encode()).decode() + "?= <olena@example.com>\r\n"
        "To: bob@example.com\r\n"
        "Subject: =?utf-8?B?" + base64.b64encode("Привіт світ".encode()).decode() + "?=\r\n"
        "Message-ID: <m1@example.com>\r\n"
        "Date: Tue, 06 Jan 2026 12:00:00 +0000\r\n"
        f"Content-Type: {ctype}; charset={charset}\r\n"
        f"Content-Transfer-Encoding: {cte}\r\n{extra}\r\n"
    )
    return head.encode() + body_bytes


def test_rfc2047_and_charsets():
    for charset in ("koi8-r", "windows-1251", "utf-8"):
        p = parse_message(_raw("Добрий день".encode(charset), charset))
        assert p.subject == "Привіт світ"
        assert "Олена Тест" in p.sender
        assert p.body == "Добрий день"


def test_qp_and_base64_bodies():
    import quopri
    qp = quopri.encodestring("Добрий день".encode("utf-8"))
    assert parse_message(_raw(qp, "utf-8", "quoted-printable")).body == "Добрий день"
    b64 = base64.b64encode("Добрий день".encode("utf-8"))
    assert parse_message(_raw(b64, "utf-8", "base64")).body == "Добрий день"


def test_thread_key_and_synthetic_id():
    m = EmailMessage()
    m["Message-ID"] = "<c@x>"
    m["In-Reply-To"] = "<b@x>"
    m["References"] = "<a@x> <b@x>"
    m.set_content("hi")
    assert parse_message(m.as_bytes()).thread_key == "<a@x>"
    del m["References"]
    assert parse_message(m.as_bytes()).thread_key == "<b@x>"
    del m["In-Reply-To"]
    assert parse_message(m.as_bytes()).thread_key == "<c@x>"
    n = EmailMessage()
    n["Subject"] = "s"
    n.set_content("hi")
    a, b = parse_message(n.as_bytes()), parse_message(n.as_bytes())
    assert a.message_id == b.message_id
    assert a.message_id.endswith("@recall.local>")
    assert a.thread_key == a.message_id


def test_meeting_date_and_broken_date():
    p = parse_message(_raw(b"x", "utf-8"))
    assert p.date is not None and p.date.tzinfo is not None
    assert p.meeting_date == p.date.astimezone().date().isoformat()
    n = EmailMessage()
    n["Date"] = "garbage"
    n.set_content("x")
    q = parse_message(n.as_bytes())
    assert q.date is None and q.meeting_date is None
    r = EmailMessage()
    r.set_content("x")
    assert parse_message(r.as_bytes()).meeting_date is None


def test_strip_quotes_variants():
    assert strip_quotes("Ok\n> old\n> older") == "Ok"
    assert strip_quotes("Ok\n\nOn Mon, 5 Jan 2026 at 10:00, Bob <b@example.com> wrote:\n> x") == "Ok"
    assert strip_quotes("Ok\n\nOn Mon, 5 Jan 2026 at 10:00 Bob\n<b@example.com> wrote:\n> x") == "Ok"
    assert strip_quotes("Добре\n\nпн, 5 січ. 2026 р. о 10:00 Іван <i@example.com> пише:\n> x") == "Добре"
    assert strip_quotes("Добре\n\n05.01.2026 10:00, Иван писал(а):\nстарое") == "Добре"
    assert strip_quotes("Добре\n\n5 січня 2026 Іван <i@example.com> написав(ла):\nстаре") == "Добре"
    assert strip_quotes("> only\n> quote") == ""
    assert strip_quotes("Own line\n> quote") == "Own line"


def test_strip_quotes_outlook_and_gmail_ru():
    # Outlook: a From:/Sent:/To:/Subject: header block, with or without the separator rule.
    outlook = ("Дякую\n\n________________________________\nFrom: Ivan <ivan@x.com>\n"
               "Sent: Friday, September 4, 2026 10:00\nTo: me@x.com\nSubject: Re: тема\n\nQUOTE_OUTLOOK old")
    assert strip_quotes(outlook) == "Дякую"
    assert strip_quotes("Ok\n\n-----Original Message-----\nFrom: Ivan\nSent: x\n\nold") == "Ok"
    assert strip_quotes("Ok\n\n-----Исходное сообщение-----\nОт: Иван\nОтправлено: x\n\nold") == "Ok"
    # A lone "From:" line in the owner's own text is not a header.
    assert strip_quotes("From: the airport I wrote nothing\nmore") == "From: the airport I wrote nothing\nmore"
    # Russian attribution with "пишет".
    assert strip_quotes("Добре\n\n04.09.2026 10:00, Иван пишет:\nстарое") == "Добре"
    # Gmail ru attribution line without a verb (date + address + colon).
    assert strip_quotes("Добре\n\nср, 2 сент. 2026 г. в 11:00, Ivan <ivan@x.com>:\n> старое") == "Добре"
    assert strip_quotes("Добре\n\nср, 2 вер. 2026 р. о 11:00 Ivan <ivan@x.com> пише:\n> старе") == "Добре"


def test_html_quote_blocks_dropped():
    html = ('<div>Hello<br>there</div><div class="gmail_quote"><div>old <div>nested</div></div>'
            '</div><blockquote>q</blockquote><div>tail</div>')
    text = mail_parse.html_to_text(html)
    assert "Hello" in text and "tail" in text
    assert "old" not in text and "nested" not in text and "q" not in text.replace("tail", "")


def test_html_only_message():
    m = EmailMessage()
    m.set_content("<html><body><p>Привіт</p><blockquote>quoted</blockquote></body></html>", subtype="html")
    assert parse_message(m.as_bytes()).body == "Привіт"


def test_masking_body_and_subject():
    m = EmailMessage()
    m["Subject"] = "password: Zx9qLm2Vb8"
    m.set_content("Ось доступ\npassword: Zx9qLm2Vb8\nконец")
    p = parse_message(m.as_bytes())
    assert "[🔒 секрет]" in p.body and "Zx9qLm2Vb8" not in p.body
    assert "[🔒 секрет]" in p.subject
    assert "Zx9qLm2Vb8" not in mail_parse.mask("token: Zx9qLm2Vb8")


def test_attachments_and_nested_message():
    m = EmailMessage()
    m.set_content("body")
    m.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="Звіт.pdf")
    inner = EmailMessage()
    inner["Subject"] = "inner"
    inner.set_content("inner body")
    m.add_attachment(inner)
    m.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="logo.png", disposition="inline",
                     cid="<c1@x>")
    p = parse_message(m.as_bytes())
    by = {a.content_type: a for a in p.attachments}
    assert by["application/pdf"].filename == "Звіт.pdf" and by["application/pdf"].inline is False
    assert by["image/png"].inline is True
    assert "message/rfc822" in by


def test_forward_content_is_kept():
    fwd = ("FYI, see below.\n\n---------- Forwarded message ---------\nFrom: Ivan <ivan@x.com>\n"
           "Date: Mon, 5 Oct 2026 at 10:00\nSubject: Ціна\nTo: me@x.com\n\n"
           "The price is 12000 EUR, payment by 30 Nov.")
    out = strip_quotes(fwd)
    assert out.startswith("FYI, see below.")
    assert "The price is 12000 EUR, payment by 30 Nov." in out
    # A forward with no own text keeps the forwarded content, not only the separator.
    bare = strip_quotes("---------- Пересылаемое сообщение ---------\nОт: Иван <i@x.com>\n"
                        "Дата: пн, 5 окт. 2026 г.\nТема: Цена\n\nЦена 12000 EUR.")
    assert "Цена 12000 EUR." in bare
    apple = strip_quotes("Глянь\n\nBegin forwarded message:\n\nFrom: Ivan <ivan@x.com>\n"
                         "Subject: Ціна\nDate: 5 October 2026\n\nЦіна 12000 EUR.")
    assert "Ціна 12000 EUR." in apple
    # A reply's quote below its own text is still cut.
    assert strip_quotes("Ok\n\nOn Mon, 5 Jan 2026 at 10:00, Bob <b@example.com> wrote:\n> x") == "Ok"


def test_html_gmail_forward_is_kept():
    html = ('<div dir="ltr">FYI</div><br><div class="gmail_quote"><div dir="ltr" class="gmail_attr">'
            '---------- Forwarded message ---------<br>From: Ivan &lt;ivan@x.com&gt;<br>'
            'Subject: Ціна<br></div><br><div>Ціна 12000 EUR, оплата до 30 листопада.</div></div>')
    text = mail_parse.html_to_text(html)
    assert "FYI" in text and "Ціна 12000 EUR, оплата до 30 листопада." in text
    reply = '<div>Ok</div><div class="gmail_quote"><div class="gmail_attr">On Mon Bob wrote:</div>old</div>'
    assert mail_parse.html_to_text(reply) == "Ok"


def test_masking_mail_phrasings():
    cases = {
        "app password: abcd efgh ijkl mnop": ["abcd", "efgh", "ijkl", "mnop"],
        "Пароль до кабінету: Qwerty!2345zz": ["Qwerty!2345zz"],
        "Пароль от кабинета: Zx9qLm2Vb8": ["Zx9qLm2Vb8"],
        "Password - Zx9qLm2Vb8": ["Zx9qLm2Vb8"],
        "Authorization: Bearer " + "a1b2c3d4" * 4: ["a1b2c3d4"],
        "curl -H 'Bearer " + "f0e1d2c3" * 4 + "'": ["f0e1d2c3"],
        "pass: hunter2": ["hunter2"],
        "Пароль: hunter2": ["hunter2"],
    }
    for text, secrets in cases.items():
        out = mail_parse.mask(text)
        assert "[🔒 секрет]" in out, text
        for s in secrets:
            assert s not in out, (text, out)
    # Ordinary prose stays readable; masking is idempotent.
    assert mail_parse.mask("passport: AB123") == "passport: AB123"
    assert mail_parse.mask("Hello world - fine") == "Hello world - fine"
    once = mail_parse.mask("Login: bob\nPassword: s3cr3t\nThanks")
    assert once == "Login: bob\nPassword: [🔒 секрет]\nThanks"
    assert mail_parse.mask(once) == once


def test_masking_round4_layouts():
    cases = {
        "Password:\nQwerty123": ["Qwerty123"],
        "Пароль:\n\n  NextLine77\nдалі текст": ["NextLine77"],
        "логин admin, пароль S3cr3tPass": ["S3cr3tPass"],
        "мой пароль Qw3rty!Zx": ["Qw3rty!Zx"],
        "The password is Tr0ub4dor&3.": ["Tr0ub4dor&3"],
        "Пароль приложения abcd efgh ijkl mnop": ["abcd", "efgh", "ijkl", "mnop"],
        "PIN-код: 4821": ["4821"],
        "Код подтверждения: 554433": ["554433"],
        "Your verification code is 123 456": ["123 456"],
        "Ваш новий пароль — Kyiv2026!": ["Kyiv2026!"],
    }
    for text, secrets in cases.items():
        out = mail_parse.mask(text)
        assert "[🔒 секрет]" in out, text
        for s in secrets:
            assert s not in out, (text, out)
        assert mail_parse.mask(out) == out, text
    # Prose around a label word stays readable.
    assert mail_parse.mask("Пароль я скину завтра") == "Пароль я скину завтра"
    assert "далі текст" in mail_parse.mask("Пароль:\nNextLine77\nдалі текст")
    # A bare "code" is a business identifier, not a one-time code: it stays readable.
    for text in ("Код ЄДРПОУ: 12345678", "Рахунок 2026, код ЄДРПОУ 40123456, МФО 300346",
                 "Код товару 123456", "error code 4040", "ИНН/Код 1234567890"):
        assert mail_parse.mask(text) == text, text


def test_masking_html_layouts():
    for html, secret in (("<p>Пароль:</p><p>NextLine77</p>", "NextLine77"),
                         ("<table><tr><td>Пароль</td><td>TblPass99</td></tr></table>", "TblPass99")):
        text = mail_parse.html_to_text(html)
        out = mail_parse.mask(text)
        assert secret not in out and "[🔒 секрет]" in out, (html, out)
    assert mail_parse.html_to_text("<table><tr><td>A</td><td>B</td></tr></table>") == "A B"
