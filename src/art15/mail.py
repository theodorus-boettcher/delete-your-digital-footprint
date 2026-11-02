"""Mail: read headers, find sent letters and replies, put drafts in the Drafts folder.

IMAP for all of that. SMTP only in `send`, which only `art15 send` calls, after the user typed the confirmation.
"""

import email
import imaplib
import re
import smtplib
import time
from collections.abc import Iterator
from datetime import date, datetime
from email import policy
from email.message import EmailMessage, Message
from email.utils import make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

from art15.companies import registrable

# Where a mail client starts quoting. The date line can wrap, so these span lines.
QUOTED = re.compile(r"^(?:On .{5,200}?wrote:|Il .{5,200}?ha scritto:|Am .{5,200}?schrieb.{0,120}?:|Le .{5,200}?a écrit\s*:|El .{5,200}?escribió:"
                    r"|-{2,}\s*(?:Original Message|Messaggio originale|Ursprüngliche Nachricht)\s*-{2,})\s*$", re.M | re.S)
LIST_LINE = re.compile(r"\((?P<flags>[^)]*)\) (?:\"[^\"]*\"|NIL) (?P<name>.+)")


def connect(host: str, user: str, password: str) -> imaplib.IMAP4_SSL:
    # The UID list of a 200k-message mailbox is one line longer than imaplib's 1 MB default.
    setattr(imaplib, "_MAXLINE", 20_000_000)
    imap = imaplib.IMAP4_SSL(host)
    imap.login(user, password)
    return imap


def folder(imap: imaplib.IMAP4, flag: str) -> str:
    r"""The mailbox with a special-use flag (\Sent, \Drafts, \All); names differ per language."""
    _, lines = imap.list()
    for raw in lines:
        m = LIST_LINE.match(raw.decode() if isinstance(raw, bytes) else str(raw))
        if m and flag in m["flags"].split():
            return m["name"]
    raise LookupError(f"no {flag} folder")


def _search(imap: imaplib.IMAP4, mailbox: str, *criteria: str) -> list[bytes]:
    imap.select(mailbox, readonly=True)
    _, data = imap.uid("SEARCH", *criteria)
    return data[0].split() if data and data[0] else []


def _fetch(imap: imaplib.IMAP4, uids: list[bytes], part: str, batch: int = 500) -> Iterator[Message]:
    for i in range(0, len(uids), batch):
        _, data = imap.uid("FETCH", b",".join(uids[i : i + batch]).decode(), f"({part})")
        for item in data:
            if isinstance(item, tuple):
                yield email.message_from_bytes(item[1], policy=policy.default)


def headers(imap: imaplib.IMAP4) -> Iterator[Message]:
    """From, Date and Subject of every message in All Mail. Bodies are never fetched."""
    box = folder(imap, r"\All")
    yield from _fetch(imap, _search(imap, box, "ALL"), "BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)]")


def messages(imap: imaplib.IMAP4, flag: str, *criteria: str) -> list[Message]:
    box = folder(imap, flag)
    return list(_fetch(imap, _search(imap, box, *criteria), "BODY.PEEK[]", batch=50))


def newest_from(imap: imaplib.IMAP4, domain: str) -> Message | None:
    """The newest email from a domain, with its body: `art15 find` reads its footer and keeps none of it."""
    uids = _search(imap, folder(imap, r"\All"), "FROM", domain)
    msg = next(_fetch(imap, uids[-1:], "BODY.PEEK[]"), None)
    return msg if msg is not None and registrable(sender_domain(msg)) == domain else None


def append_draft(imap: imaplib.IMAP4, msg: EmailMessage) -> None:
    typ, data = imap.append(folder(imap, r"\Drafts"), r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    if typ != "OK":
        raise RuntimeError(f"draft not saved: {data!r}")


def drop_draft(imap: imaplib.IMAP4, msgid: str) -> None:
    """Delete our draft with this exact Message-ID, so a letter `art15 send` sends can't also go out by hand."""
    imap.select(folder(imap, r"\Drafts"))
    _, data = imap.uid("SEARCH", "HEADER", "Message-ID", f'"{msgid}"')
    for uid in data[0].split() if data and data[0] else []:
        imap.uid("STORE", uid.decode(), "+FLAGS", r"(\Deleted)")
    imap.expunge()


def send(host: str, user: str, password: str, msg: EmailMessage) -> None:
    # A new connection per letter: they go out 30 s apart, too slow to keep one open for a long run.
    with smtplib.SMTP_SSL(host, 465, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def draft(sender: str, to: str, subject: str, body: str, attachments: dict[str, str] | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    msg.set_content(body)
    for name, text in (attachments or {}).items():
        msg.add_attachment(text, filename=name)
    return msg


def load(path: Path) -> Message:
    return email.message_from_bytes(path.read_bytes(), policy=policy.default)


def imap_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")  # IMAP wants English month names; %b is English in the C locale


def header(msg: Message, name: str) -> str:
    try:
        return str(msg.get(name, "") or "")
    except (ValueError, IndexError):  # malformed headers in old mail
        return ""


def sender_domain(msg: Message) -> str:
    addr = parseaddr(header(msg, "From"))[1]
    return addr.rsplit("@", 1)[1].lower() if "@" in addr else ""


def msg_date(msg: Message) -> datetime | None:
    try:
        return parsedate_to_datetime(header(msg, "Date"))
    except (TypeError, ValueError):
        return None


def is_reply(msg: Message, ref: str, domains: set[str], our_ids: set[str]) -> bool:
    """A message belongs to a request by its reference in the subject, by threading headers, or by
    coming from one of the company's domains (Gmail may rewrite our Message-IDs, so all three)."""
    if ref in header(msg, "Subject"):
        return True
    thread = set(re.findall(r"<[^>]+>", header(msg, "In-Reply-To") + header(msg, "References")))
    return bool(thread & our_ids) or registrable(sender_domain(msg)) in domains


def body_text(msg: Message) -> str:
    if not isinstance(msg, EmailMessage):
        return ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = str(part.get_content())
    if part.get_content_subtype() == "html":
        text = BeautifulSoup(text, "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def reply_text(msg: Message) -> str:
    """What the sender wrote, without the quoted letter below it: our own words are not their answer."""
    text = body_text(msg)
    if m := QUOTED.search(text):
        text = text[: m.start()]
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(">")).strip()


def attachments(msg: Message) -> list[tuple[str, bytes]]:
    if not isinstance(msg, EmailMessage):
        return []
    out = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            out.append((Path(part.get_filename() or "attachment.bin").name, payload))
    return out
