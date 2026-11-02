"""The whole loop on a fake mailbox: draft -> sent -> reply -> deadline -> reminder -> complaint."""

import argparse
import builtins
import json
import re
import smtplib
import sqlite3
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from art15 import cli, finder, llm, mail, ticks

FIX = Path(__file__).parent / "fixtures"


class FakeIMAP:
    def __init__(self, inbox: list[bytes]) -> None:
        self.inbox, self.box = inbox, ""
        self.appended: builtins.list[bytes] = []
        self.dropped: builtins.list[str] = []

    def list(self) -> tuple[str, builtins.list[bytes]]:
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"']

    def select(self, box: str, readonly: bool = False) -> tuple[str, builtins.list[bytes]]:
        self.box = box
        return "OK", [b"1"]

    def uid(self, cmd: str, *args: str) -> tuple[str, builtins.list[Any]]:
        if cmd == "STORE":
            self.dropped.append(args[0])
            return "OK", []
        if cmd == "SEARCH" and "Drafts" in self.box:  # drop_draft: one draft by its exact Message-ID
            found = [str(i + 1).encode() for i, d in enumerate(self.appended) if args[-1].strip('"').encode() in d]
            return "OK", [b" ".join(found)]
        if cmd == "SEARCH":  # every search in All Mail returns the whole inbox; sync must filter
            hits = [str(i + 1).encode() for i in range(len(self.inbox))] if "All Mail" in self.box else []
            return "OK", [b" ".join(hits)]
        return "OK", [(b"x", self.inbox[int(i) - 1]) for i in args[0].split(",")]

    def expunge(self) -> tuple[str, builtins.list[bytes]]:
        return "OK", []

    def append(self, box: str, flags: str, when: str, data: bytes) -> tuple[str, builtins.list[bytes]]:
        assert box == '"[Gmail]/Drafts"' and flags == r"(\Draft)"
        self.appended.append(data)
        return "OK", [b""]

    def logout(self) -> None:
        pass


def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox: builtins.list[bytes]) -> FakeIMAP:
    monkeypatch.setattr(cli, "HOME", tmp_path)
    monkeypatch.setattr(cli, "DATA", tmp_path / "data")
    monkeypatch.setattr(cli, "DA", FIX / "da")
    monkeypatch.setattr(cli, "OWN", tmp_path / "data" / "records")
    (tmp_path / ".env").write_text("ART15_NAME=Ada Rossi\nART15_EMAIL=me@example.org\nART15_APP_PASSWORD=x\nART15_COUNTRY=it\n")
    imap = FakeIMAP(inbox)
    monkeypatch.setattr(mail, "connect", lambda *a: imap)
    return imap


def test_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    imap = fake_home(tmp_path, monkeypatch, [(FIX / "reply_ack.eml").read_bytes(), (FIX / "bounce.eml").read_bytes()])
    asked: list[str] = []

    def fake_ask(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        asked.append(prompt)
        return {"label": "acknowledged", "quote": "We have received your data subject access request"}

    monkeypatch.setattr(llm, "make_ask", lambda env: fake_ask)

    cli.main(["--now", "2026-11-02", "draft", "shop-uk", "google"])
    out = capsys.readouterr().out
    assert "they also ask for: Customer number" in out and "takeout.google.com" in out
    assert len(imap.appended) == 1  # google wants its web form: no draft, a letter file instead
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    con.row_factory = sqlite3.Row
    shop = con.execute("SELECT * FROM requests WHERE slug = 'shop-uk'").fetchone()
    google = con.execute("SELECT * FROM requests WHERE slug = 'google'").fetchone()
    assert (shop["status"], google["status"]) == ("drafted", "manual")
    draft = message_from_bytes(imap.appended[0], policy=policy.default)
    assert draft["To"] == "privacy@shop.co.uk" and shop["ref"] in draft["Subject"]

    ref = shop["ref"]
    cli.main(["mark", ref, "sent", "--date", "2026-11-02"])  # stands in for finding it in Sent
    cli.main(["--now", "2026-11-03", "sync"])
    assert con.execute("SELECT status FROM requests WHERE ref = ?", (ref,)).fetchone()[0] == "acknowledged"
    labels = [r[0] for r in con.execute("SELECT label FROM messages")]
    assert labels == ["acknowledged"]  # the bounce came from googlemail.com, not from the shop: ignored
    assert len(asked) == 1

    cli.main(["--now", "2026-12-02", "sync"])  # deadline day: nothing yet
    assert len(imap.appended) == 1 and len(asked) == 1  # the reply is not classified twice
    cli.main(["--now", "2026-12-03", "sync"])
    assert con.execute("SELECT status FROM requests WHERE ref = ?", (ref,)).fetchone()[0] == "admonished"
    reminder = message_from_bytes(imap.appended[1], policy=policy.default)
    assert "on 2026-11-02 I sent you a request" in reminder.get_body().get_content()  # type: ignore[union-attr]

    con.execute("UPDATE letters SET sent_at = '2026-12-03' WHERE kind = 'admonition'")
    con.commit()
    cli.main(["--now", "2026-12-17", "sync"])  # two weeks run to Dec 17
    assert len(imap.appended) == 2
    cli.main(["--now", "2026-12-18", "sync"])
    complaint = message_from_bytes(imap.appended[2], policy=policy.default)
    assert complaint["To"] == "protocollo@pec.gpdp.it"
    assert [p.get_filename() for p in complaint.iter_attachments()] == [f"{ref}-request.txt", f"{ref}-reminder.txt"]
    assert con.execute("SELECT status FROM requests WHERE ref = ?", (ref,)).fetchone()[0] == "complaint_drafted"
    assert (tmp_path / "data" / "report" / "index.html").exists()


def test_followup_after_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    imap = fake_home(tmp_path, monkeypatch, [])
    cli.main(["--now", "2026-11-02", "draft", "shop-uk"])
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    ref = con.execute("SELECT ref FROM requests").fetchone()[0]
    cli.main(["mark", ref, "sent", "--date", "2026-11-02"])
    cli.main(["mark", ref, "answered"])
    con.executemany("INSERT INTO findings (ref, kind, item, status) VALUES (?,?,?,?)",
                    [(ref, "checklist", "recipients", "partial"), (ref, "checklist", "purposes", "present"),
                     (ref, "checklist", "retention", "missing"), (ref, "policy", "location", "missing")])
    con.commit()
    cli.main(["--now", "2026-11-20", "sync"])
    text = message_from_bytes(imap.appended[-1], policy=policy.default).get_body().get_content()  # type: ignore[union-attr]
    assert "Missing: the recipients to whom" in text and "C-154/21) (only partly); how long the data are stored" in text
    assert "contains none: location." in text and "purposes" not in text
    assert con.execute("SELECT status FROM requests").fetchone()[0] == "admonished"


def test_sync_and_eval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_home(tmp_path, monkeypatch, [(FIX / "reply_ack.eml").read_bytes()])

    def fake_ask(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if "label" in schema["properties"]:
            return {"label": "acknowledged", "quote": "We have received your data subject access request"}
        return {n: "inferences" for n in schema["required"]}

    monkeypatch.setattr(llm, "make_ask", lambda env: fake_ask)
    cli.main(["--now", "2026-11-02", "draft", "shop-uk"])
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    ref = con.execute("SELECT ref FROM requests").fetchone()[0]
    cli.main(["mark", ref, "sent", "--date", "2026-11-02"])
    cli.main(["--now", "2026-11-03", "sync"])
    assert con.execute("SELECT label, backend FROM messages").fetchone() == ("acknowledged", "local")

    cli.main(["eval", "replies"])  # writes the label template
    labels = tmp_path / "data" / "labels.csv"
    labels.write_text(labels.read_text().replace(",,", ",acknowledged,", 1))
    capsys.readouterr()
    cli.main(["eval", "replies"])
    out = capsys.readouterr().out
    assert "100%" in out and "Mistakes" not in out
    assert (tmp_path / "data" / "report" / "eval.html").exists()

    (tmp_path / "data" / "field_labels.csv").write_text("field,category\nprofile.firstName,identity\nzzq,inferences\n")
    cli.main(["eval", "fields"])
    out = capsys.readouterr().out
    assert re.search(r"keyword rules\s+1/2\s+100%", out) and re.search(r"rules, then local model\s+2/2\s+100%", out)


def test_find_keeps_no_email_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "Your order 7781 ships to Via Segreta 9"
    mail_from = (f"From: Shop <news@mailer-shop.it>\r\nSubject: Order\r\nMessage-ID: <1@x>\r\n\r\n{secret}\r\n"
                 "Shop Italia S.r.l. www.shop-italia.it\r\n").encode()
    fake_home(tmp_path, monkeypatch, [mail_from])
    policy_page = "<p>Write to privacy@shop-italia.it with any request.</p>" + "<p>" + "Words about the shop. " * 100 + "</p>"
    prompts: list[str] = []

    def fake_ask(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        prompts.append(prompt)
        return {"company": "Shop Italia S.r.l.", "website": "www.shop-italia.it"}

    monkeypatch.setattr(llm, "make_ask", lambda env: fake_ask)
    monkeypatch.setattr(llm, "models", lambda: ["qwen3:8b"])
    monkeypatch.setattr(cli, "fetch", lambda url: policy_page)
    monkeypatch.setattr("time.sleep", lambda s: None)
    cli.main(["status"])  # creates the database
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    con.execute("INSERT INTO senders VALUES ('mailer-shop.it', NULL, 3, '', '', 1, 'Order')")
    con.commit()
    capsys.readouterr()

    cli.main(["find", "--no-browser"])
    out = capsys.readouterr().out
    assert secret in prompts[0] and "7781" not in out  # the local model read it; it went no further
    record = (tmp_path / "data" / "records" / "mailer-shop-it.json").read_text()
    assert "7781" not in record and "Segreta" not in record and "privacy@shop-italia.it" in record
    assert con.execute("SELECT slug FROM senders").fetchone() == ("mailer-shop-it",)
    assert con.execute("SELECT status, pages FROM lookups").fetchone() == ("email", 1)

    con.execute("INSERT INTO senders VALUES ('shop-italia.it', NULL, 2, '', '', 1, 'Order')")  # the same company again
    con.commit()
    capsys.readouterr()
    cli.main(["find", "--no-browser"])
    assert "same address as mailer-shop-it" in capsys.readouterr().out
    assert not (tmp_path / "data" / "records" / "shop-italia-it.json").exists()

    cli.main(["find", "--no-browser"])  # a second run skips what was tried
    assert "email" not in capsys.readouterr().out.split("Records")[0]
    cli.main(["draft", "mailer-shop-it", "--dry-run"])
    assert "privacy@shop-italia.it" in capsys.readouterr().out


def test_a_bounced_request_gets_no_reminder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    imap = fake_home(tmp_path, monkeypatch, [])
    cli.main(["--now", "2026-11-02", "draft", "shop-uk"])
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    ref = con.execute("SELECT ref FROM requests").fetchone()[0]
    cli.main(["mark", ref, "sent", "--date", "2026-11-02"])
    imap.inbox.append((f"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\nSubject: Delivery Status Notification (Failure)\r\n"
                       f"Message-ID: <b@x>\r\n\r\nAddress not found.\r\nSubject: Request [{ref}]\r\n").encode())
    drafts = len(imap.appended)
    cli.main(["--now", "2026-11-03", "sync"])
    assert con.execute("SELECT status FROM requests").fetchone() == ("bounced",)
    cli.main(["--now", "2026-12-20", "sync"])  # past the deadline
    assert len(imap.appended) == drafts


class FakeScreen:
    def __init__(self, keys: str) -> None:
        self.keys = [ord(k) for k in keys]
        self.lines: list[str] = []

    def getmaxyx(self) -> tuple[int, int]:
        return 24, 100

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
        self.lines.append(text)

    def erase(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def getch(self) -> int:
        return self.keys.pop(0)


def senders_with_records(tmp_path: Path, emails: dict[str, str]) -> sqlite3.Connection:
    """One own record and one likely-account sender per domain."""
    (tmp_path / "data" / "records").mkdir(parents=True, exist_ok=True)
    cli.main(["status"])  # creates the database
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    con.row_factory = sqlite3.Row
    for i, (dom, email) in enumerate(emails.items()):
        slug = dom.replace(".", "-")
        rec = {"slug": slug, "name": slug, "web": f"https://{dom}/", "runs": [dom], "quality": "found", "email": email}
        (tmp_path / "data" / "records" / f"{slug}.json").write_text(json.dumps(rec))
        con.execute("INSERT INTO senders VALUES (?,?,?,?,?,?,?)", (dom, slug, 10 - i, "", "", 1, "Welcome"))
    con.commit()
    return con


def choose(con: sqlite3.Connection, *slugs: str) -> None:
    con.executemany("INSERT OR REPLACE INTO choices VALUES (?, 1)", [(s,) for s in slugs])
    con.commit()


def fake_send(monkeypatch: pytest.MonkeyPatch, answer: str = "") -> list[EmailMessage]:
    sent: list[EmailMessage] = []
    monkeypatch.setattr(mail, "send", lambda host, user, password, msg: sent.append(msg))
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    return sent


def test_review_starts_risky_ones_unticked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_home(tmp_path, monkeypatch, [])
    con = senders_with_records(tmp_path, {"safe.it": "privacy@safe.it", "plain.it": "info@plain.it",
                                          "amazonaws.com": "privacyofficer@marketo.com", "wind.it": "dpo@pec.wind.it"})
    rows = cli.review_rows(cli.Ctx(argparse.Namespace(now=None)))
    assert {r.slug: (r.ticked, r.note) for r in rows} == {
        "safe-it": (True, "privacy address"), "plain-it": (False, "! general address"),
        "amazonaws-com": (False, "! address at another company"), "wind-it": (False, "! certified mail, may refuse Gmail")}

    assert ticks.tick(FakeScreen("nj q"), rows) is False  # q leaves; the caller saves nothing
    monkeypatch.setattr(ticks, "run", lambda rows: ticks.tick(FakeScreen("nj \n"), rows))  # none, down, tick the second, save
    cli.main(["review"])
    saved = {r["slug"]: r["chosen"] for r in con.execute("SELECT * FROM choices")}
    assert saved == {"safe-it": 0, "plain-it": 1, "amazonaws-com": 0, "wind-it": 0}
    assert "1 of 4 chosen" in capsys.readouterr().out
    # The second review shows what was chosen, not the defaults again.
    assert {r.slug: r.ticked for r in cli.review_rows(cli.Ctx(argparse.Namespace(now=None)))}["plain-it"] is True


def test_send_needs_the_typed_number(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_home(tmp_path, monkeypatch, [])
    con = senders_with_records(tmp_path, {"safe.it": "privacy@safe.it"})
    choose(con, "safe-it", "shop-uk", "google")  # google takes a web form only
    for wrong in ("", "yes", "send 3", "send"):
        sent = fake_send(monkeypatch, wrong)
        cli.main(["--now", "2026-11-02", "send"])
        assert not sent and not con.execute("SELECT 1 FROM requests").fetchone()

    sent = fake_send(monkeypatch, "send 2")
    cli.main(["--now", "2026-11-02", "send"])
    assert sorted(m["To"] for m in sent) == ["privacy@safe.it", "privacy@shop.co.uk"]
    rows = {r["slug"]: r for r in con.execute("SELECT * FROM requests")}
    assert rows["safe-it"]["status"] == "sent" and rows["safe-it"]["deadline"] == "2026-12-02"
    assert rows["google"]["status"] == "manual"
    assert con.execute("SELECT COUNT(*) FROM letters WHERE sent_at IS NULL AND to_addr != ''").fetchone()[0] == 0
    ours = {r["msgid"] for r in con.execute("SELECT msgid FROM letters")}
    assert {m["Message-ID"] for m in sent} <= ours  # sync matches replies on these

    capsys.readouterr()
    cli.main(["--now", "2026-11-03", "send"])  # nothing goes twice
    assert len(sent) == 2 and "Nothing to send" in capsys.readouterr().out
    cli.main(["forms"])
    assert "takeout.google.com" in capsys.readouterr().out


def test_send_takes_letters_out_of_drafts_and_never_a_complaint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    imap = fake_home(tmp_path, monkeypatch, [])
    cli.main(["--now", "2026-11-02", "draft", "shop-uk"])
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    ref = con.execute("SELECT ref FROM requests").fetchone()[0]
    con.execute("INSERT INTO letters (ref, kind, to_addr, subject, body) VALUES (?, 'complaint', 'x@pec.gpdp.it', 's', 'b')", (ref,))
    con.commit()
    sent = fake_send(monkeypatch, "send 1")
    cli.main(["--now", "2026-11-02", "send"])
    assert [m["To"] for m in sent] == ["privacy@shop.co.uk"] and imap.dropped == ["1"]

    # A reminder drafted before the company answered must not go out after it did.
    con.execute("INSERT INTO letters (ref, kind, to_addr, subject, body, msgid) VALUES (?, 'admonition', 'p@shop.co.uk', 's', 'b', '<m@x>')", (ref,))
    con.execute("UPDATE requests SET status = 'answered'")
    con.commit()
    sent.clear()
    cli.main(["--now", "2026-11-20", "send"])
    assert not sent
    con.execute("UPDATE requests SET status = 'admonished'")  # still waiting: this one goes
    con.commit()
    sent = fake_send(monkeypatch, "send 1")
    cli.main(["--now", "2026-11-20", "send"])
    assert [m["To"] for m in sent] == ["p@shop.co.uk"]
    assert con.execute("SELECT sent_at FROM letters WHERE kind = 'complaint'").fetchone()[0] is None


def test_send_stops_on_bounces_and_on_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    imap = fake_home(tmp_path, monkeypatch, [])
    con = senders_with_records(tmp_path, {"a.it": "privacy@a.it", "b.it": "privacy@b.it", "c.it": "privacy@c.it"})
    choose(con, "a-it", "b-it", "c-it")
    sent = fake_send(monkeypatch, "send 3")
    monkeypatch.setattr(cli, "CHECK_EVERY", 1)
    monkeypatch.setattr(cli, "MAX_BOUNCES", 1)

    def bounce(host: str, user: str, password: str, msg: EmailMessage) -> None:
        sent.append(msg)
        imap.inbox.append(f"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\nSubject: Failure\r\n\r\n{msg['Subject']}\r\n".encode())

    monkeypatch.setattr(mail, "send", bounce)
    with pytest.raises(SystemExit, match="1 came back"):
        cli.main(["--now", "2026-11-02", "send"])
    assert len(sent) == 1

    def refused(host: str, user: str, password: str, msg: EmailMessage) -> None:
        raise smtplib.SMTPServerDisconnected("gone")

    monkeypatch.setattr(cli, "MAX_BOUNCES", 99)
    monkeypatch.setattr("builtins.input", lambda prompt: "send 2")
    monkeypatch.setattr(mail, "send", refused)
    with pytest.raises(SystemExit, match="Stopped after 0 of 2: gone"):
        cli.main(["--now", "2026-11-02", "send"])
    assert con.execute("SELECT COUNT(*) FROM requests WHERE status = 'drafted'").fetchone()[0] == 2  # both still ready


def test_start_skips_a_scan_that_is_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home(tmp_path, monkeypatch, [])
    calls: list[str] = []
    monkeypatch.setattr(cli, "cmd_setup", lambda ctx, args: calls.append("setup"))
    monkeypatch.setattr(cli, "cmd_scan", lambda ctx, args: calls.append("scan"))
    monkeypatch.setattr(cli, "cmd_find", lambda ctx, args: calls.append("find"))
    cli.main(["start"])
    assert calls == ["setup", "scan", "find"]
    senders_with_records(tmp_path, {"a.it": "privacy@a.it"})
    calls.clear()
    cli.main(["start"])
    assert calls == ["setup", "find"]


def test_show_reads_a_letter_without_saving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_home(tmp_path, monkeypatch, [])
    cli.main(["show", "shop-uk"])
    assert "Ada Rossi" in capsys.readouterr().out
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    assert not con.execute("SELECT 1 FROM requests").fetchone()


def test_find_keeps_web_forms_and_outside_firms_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home(tmp_path, monkeypatch, [])
    found = {"form-a.it": finder.Found("", "https://form-a.it/p", "q", "https://portal.onetrust.com/a"),
             "form-b.it": finder.Found("", "https://form-b.it/p", "q", "https://portal.onetrust.com/b"),
             "client-a.it": finder.Found("dpo@outside-firm.eu", "https://client-a.it/p", "q"),
             "client-b.it": finder.Found("dpo@outside-firm.eu", "https://client-b.it/p", "q")}

    def walk(site: str, company: str, read: Any, ask: Any, also: Any = None) -> finder.Lookup:
        f = found[site]
        return finder.Lookup("webform" if f.webform else "email", f, [f.url])

    monkeypatch.setattr(finder, "walk", walk)
    cli.main(["status"])
    con = sqlite3.connect(tmp_path / "data" / "art15.db")
    con.executemany("INSERT INTO senders VALUES (?, NULL, 3, '', '', 1, 'Order')", [(d,) for d in found])
    con.commit()
    cli.main(["find", "--no-llm", "--no-browser"])
    assert sorted(r[0] for r in con.execute("SELECT slug FROM senders")) == ["client-a-it", "client-b-it", "form-a-it", "form-b-it"]
    assert json.loads((tmp_path / "data" / "records" / "form-b-it.json").read_text())["webform"].endswith("/b")
