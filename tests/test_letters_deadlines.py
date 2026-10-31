from datetime import date
from pathlib import Path

from art15 import letters
from art15.deadlines import add_months, admonition_deadline, request_deadline

DA = Path(__file__).parent / "fixtures" / "da"
GOOGLE = {"name": "Google LLC", "runs": ["Gmail", "YouTube"],
          "required-elements": [{"desc": "Name", "type": "name"}, {"desc": "E-Mail", "type": "email"}]}


def test_access_request_flags_on() -> None:
    body, missing = letters.access_request(DA, GOOGLE, "en", "Ada Rossi", "ada@example.org")
    assert "Article 20(1)" in body  # data portability
    assert "the following as well as any other services" in body and ": Gmail, YouTube]" not in body
    assert body.count("Gmail, YouTube") == 1
    assert "Name: Ada Rossi\nE-Mail: ada@example.org" in body
    assert "<italic>" not in body and "[" not in body and "{" not in body
    assert body.rstrip().endswith("Yours sincerely,\nAda Rossi")
    assert missing == []


def test_access_request_flags_off() -> None:
    record = {"required-elements": [{"desc": "Customer number", "type": "input"}]}
    body, missing = letters.access_request(DA, record, "en", "Ada", "ada@example.org")
    assert "following as well as" not in body
    assert "any other services and companies for which you are the controller as defined by Article 4(7) GDPR." in body
    assert missing == ["Customer number"]


def test_render_free_text_and_unknown_language() -> None:
    body = letters.admonition(DA, "xx", "2026-11-02", "No answer yet.", "Ada")
    assert "on 2026-11-02 I sent you a request according to Article 15 GDPR.\nNo answer yet." in body
    assert letters.render("[a>x][b>y] {k} {free text} {free two}", {"a"}, {"k": "v"}, ["F"]) == "x v F {free two}\n"


def test_subject_has_reference() -> None:
    assert letters.subject("access", "it", "art15-abcd").endswith("[art15-abcd]")
    assert letters.subject("admonition", "xx", "r").startswith("Reminder")


def test_month_end() -> None:
    assert add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)  # leap year
    assert add_months(date(2026, 11, 15), 2) == date(2027, 1, 15)


def test_request_deadline() -> None:
    assert request_deadline(date(2026, 11, 2)) == date(2026, 12, 2)  # a Wednesday
    assert request_deadline(date(2026, 10, 31)) == date(2026, 11, 30)  # Nov 31 doesn't exist
    assert request_deadline(date(2026, 11, 2), extended=True) == date(2027, 2, 2)


def test_weekend_rolls_forward() -> None:
    assert request_deadline(date(2026, 9, 17)) == date(2026, 10, 19)  # Oct 17 is a Saturday
    assert admonition_deadline(date(2026, 11, 1)) == date(2026, 11, 16)  # Nov 15 is a Sunday
