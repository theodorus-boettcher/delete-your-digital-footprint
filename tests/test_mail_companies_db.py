import sqlite3
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from art15 import db, evals, mail
from art15.classify import classify, is_bounce
from art15.companies import Companies, registrable
from art15.llm import Ask

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> EmailMessage:
    msg = message_from_bytes((FIX / name).read_bytes(), policy=policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


def test_registrable() -> None:
    assert registrable("mail.google.com") == "google.com"
    assert registrable("em.shop.co.uk") == "shop.co.uk"


def test_match() -> None:
    it = Companies(FIX / "da", "it")
    assert it.match("mail.google.com") == "google"
    assert it.match("youtube.com") == "google"  # by `runs` name
    assert it.match("em.shop.co.uk") == "shop-uk"
    assert it.match("amazon.de") == "amazon-it"  # both list amazon.de; the user's country wins
    assert Companies(FIX / "da", "de").match("amazon.de") == "amazon-de"
    assert it.match("gmail.com") is None  # friends, not companies
    assert it.match("unknown-shop.example") is None
    assert it.match("facebookmail.com") == "facebook"  # a mail-only domain
    assert it.match("burgerking.it") is None  # Burger King Deutschland is another company
    assert it.match("vinted.it") is None
    assert it.match("vinted.it", {"vinted-uab-de"}) == "vinted-uab-de"  # the mailbox already has vinted.de: one request


def test_own_records(tmp_path: Path) -> None:
    (tmp_path / "justeat-it.json").write_text('{"slug": "justeat-it", "name": "justeat.it", "web": "https://justeat.it/", '
                                              '"email": "privacy@justeat.it"}')
    assert Companies(FIX / "da", "it", tmp_path).match("mail.justeat.it") == "justeat-it"


def test_reply_matching() -> None:
    ack = load("reply_ack.eml")
    assert mail.is_reply(ack, "art15-abcd", set(), set())
    assert mail.is_reply(ack, "art15-zzzz", set(), {"<req1@example.org>"})
    assert mail.is_reply(ack, "art15-zzzz", {"shop.co.uk"}, set())
    assert not mail.is_reply(ack, "art15-zzzz", {"other.com"}, set())


def test_bounce_by_rule() -> None:
    def no_model(prompt: str, schema: dict[str, object]) -> dict[str, object]:
        raise AssertionError("bounces never reach the model")

    assert is_bounce(load("bounce.eml"))
    assert classify(load("bounce.eml"), no_model).label == "bounce"
    assert not is_bounce(load("reply_ack.eml"))


def test_classify_needs_a_real_quote() -> None:
    ack = load("reply_ack.eml")
    real = classify(ack, lambda p, s: {"label": "acknowledged", "quote": "We have received your data subject access request"})
    assert real.label == "acknowledged"
    made_up = classify(ack, lambda p, s: {"label": "data_delivered", "quote": "Please find your data attached."})
    assert made_up.label == "other"


def test_draft() -> None:
    msg = mail.draft("me@example.org", "privacy@shop.co.uk", "Subject [art15-abcd]", "Body\n", {"a.txt": "x"})
    assert msg["Message-ID"].endswith("@example.org>")
    assert [p.get_filename() for p in msg.iter_attachments()] == ["a.txt"]


def test_one_send_path() -> None:
    src = Path(mail.__file__).parent
    assert [p.name for p in src.glob("*.py") if "smtplib.SMTP_SSL" in p.read_text()] == ["mail.py"]
    assert (src / "cli.py").read_text().count("ctx.send(") == 1  # send_letters, behind the typed confirmation


def test_transitions() -> None:
    con = db.connect(Path(":memory:"))
    con.execute("INSERT INTO requests (ref, slug, status) VALUES ('r', 's', 'drafted')")
    db.set_status(con, "r", "sent", sent_at="2026-11-02")
    db.set_status(con, "r", "acknowledged")
    with pytest.raises(db.BadTransition):
        db.set_status(con, "r", "drafted")
    db.set_status(con, "r", "extended", extended=1)
    row: sqlite3.Row = con.execute("SELECT * FROM requests").fetchone()
    assert (row["status"], row["sent_at"], row["extended"]) == ("extended", "2026-11-02", 1)
    with pytest.raises(KeyError):
        db.set_status(con, "nope", "sent")


def reply(body: str, attachment: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "privacy@shop.co.uk", "Re: request [art15-abcd]"
    msg.set_content(body)
    if attachment:
        msg.add_attachment(b"PK", maintype="application", subtype="zip", filename="data.zip")
    return msg


def test_classify_checks() -> None:
    def ask(label: str, quote: str) -> Ask:
        return lambda prompt, schema: {"label": label, "quote": quote}

    ack = "We have received your request."
    assert classify(reply(ack), ask("acknowledged", ack)).label == "acknowledged"
    made_up = classify(reply(ack), ask("data_delivered", "Your data is attached."))
    assert (made_up.label, made_up.pick) == ("other", "data_delivered")  # the quote is not in the reply
    assert classify(reply(ack), ask("data_delivered", ack)).label == "other"  # no attachment and no link
    data = "Please find your data attached."
    assert classify(reply(data, attachment=True), ask("data_delivered", data)).label == "data_delivered"


def test_eval_metrics() -> None:
    rows = [evals.Row("1", "acknowledged", "acknowledged", "acknowledged"),
            evals.Row("2", "refusal", "acknowledged", "other"),  # sent to the user
            evals.Row("3", "data_delivered", "data_delivered", "data_delivered"),
            evals.Row("4", "extension", "acknowledged", "acknowledged")]
    m = evals.summary(rows)
    assert (m["raw_accuracy"], m["to_user"]) == (0.5, 0.25) and round(m["auto_accuracy"], 2) == 0.67


def test_reply_text_drops_our_own_letter() -> None:
    gmail = reply("Here is your data.\n\nOn Sat, 31 Oct 2026 at 05:11, theodorus boettcher <\nme@gmail.com> wrote:\n> the purposes of the processing;\n")
    assert mail.reply_text(gmail) == "Here is your data."
    italian = reply("Ecco i dati.\n\nIl giorno sab 31 ott 2026 alle 05:11 Ada <a@b.it> ha scritto:\nthe purposes of the processing;\n")
    assert mail.reply_text(italian) == "Ecco i dati."
    assert mail.reply_text(reply("> quoted only\nNew line.\n")) == "New line."
