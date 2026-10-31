"""The whole loop on a fake mailbox: draft -> sent -> reply -> deadline -> reminder -> complaint."""

import builtins
import re
import sqlite3
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any

import pytest

from art15 import cli, llm, mail

FIX = Path(__file__).parent / "fixtures"


class FakeIMAP:
    def __init__(self, inbox: list[bytes]) -> None:
        self.inbox, self.box = inbox, ""
        self.appended: builtins.list[bytes] = []

    def list(self) -> tuple[str, builtins.list[bytes]]:
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"']

    def select(self, box: str, readonly: bool = False) -> tuple[str, builtins.list[bytes]]:
        self.box = box
        return "OK", [b"1"]

    def uid(self, cmd: str, *args: str) -> tuple[str, builtins.list[Any]]:
        if cmd == "SEARCH":  # every search in All Mail returns the whole inbox; sync must filter
            hits = [str(i + 1).encode() for i in range(len(self.inbox))] if "All Mail" in self.box else []
            return "OK", [b" ".join(hits)]
        return "OK", [(b"x", self.inbox[int(i) - 1]) for i in args[0].split(",")]

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
