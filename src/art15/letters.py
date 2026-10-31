"""Render datenanfragen letter templates (`templates/<lang>/*.txt`, CC0).

Syntax: `{variable}` and `[flag> text ]` (text kept only when the flag is on). Placeholders with
spaces, like `{Description of the problem, ...}`, are free text: filled in order from `free`,
or left for the user to fill in the draft.
"""

import re
from pathlib import Path
from typing import Any

FLAG = re.compile(r"\[(\w+)>(.*?)\]", re.S)
VAR = re.compile(r"\{([^{}]+)\}")

SUBJECTS = {
    "access": {
        "en": "Request for access to personal data according to Art. 15 GDPR",
        "de": "Anfrage auf Auskunft gemäß Art. 15 DSGVO",
        "it": "Richiesta di accesso ai dati personali ai sensi dell'art. 15 GDPR",
        "fr": "Demande d'accès aux données personnelles conformément à l'article 15 du RGPD",
        "es": "Solicitud de acceso a datos personales conforme al artículo 15 del RGPD",
        "nl": "Verzoek om inzage in persoonsgegevens op grond van artikel 15 AVG",
    },
    "admonition": {
        "en": "Reminder: my request according to Art. 15 GDPR",
        "de": "Erinnerung: meine Anfrage gemäß Art. 15 DSGVO",
        "it": "Sollecito: la mia richiesta ai sensi dell'art. 15 GDPR",
    },
    "complaint": {
        "en": "Complaint according to Art. 77 GDPR",
        "de": "Beschwerde gemäß Art. 77 DSGVO",
        "it": "Reclamo ai sensi dell'art. 77 GDPR",
    },
    "idreply": {"en": "Re: identity check for my request according to Art. 15 GDPR"},
}

# shortcut: English only, these letters have no datenanfragen template; translate when a user needs it.
IDREPLY = """To Whom It May Concern:

you asked me to verify my identity before answering my request according to Article 15 GDPR.

My request was sent from {email}, the email address registered with my account with you. Under Article 12(6) GDPR you may ask for additional information only where you have reasonable doubts about my identity, and under Article 12(2) GDPR you may not refuse to act on my request unless you demonstrate that you are not in a position to identify me. Please tell me what those doubts are. I will not send a copy of an identity document, as this would be more data than is necessary to identify me (Article 5(1)(c) GDPR).

The one-month period of Article 12(3) GDPR continues to run.

Yours sincerely,
"""


def subject(kind: str, lang: str, ref: str) -> str:
    texts = SUBJECTS[kind]
    return f"{texts.get(lang, texts['en'])} [{ref}]"


def template(root: Path, lang: str, name: str) -> str:
    path = root / "templates" / lang / f"{name}.txt"
    if not path.exists():
        path = root / "templates" / "en" / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def render(text: str, flags: set[str], variables: dict[str, str], free: list[str] | None = None) -> str:
    text = FLAG.sub(lambda m: m.group(2) if m.group(1) in flags else "", text)
    queue = list(free or [])

    def fill(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in variables:
            return variables[key]
        if " " in key and queue:
            return queue.pop(0)
        return m.group(0)

    text = VAR.sub(fill, text)
    text = re.sub(r"</?italic>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def id_data(record: dict[str, Any], name: str, email: str) -> tuple[str, list[str]]:
    """The identification lines (name + account email) and the record's other required fields we leave out."""
    labels = {"name": "Name", "email": "Email"}
    missing = []
    for el in record.get("required-elements", []):
        if el.get("type") in labels:
            labels[el["type"]] = el["desc"]
        elif not el.get("optional"):
            missing.append(el["desc"])
    return f"{labels['name']}: {name}\n{labels['email']}: {email}\n", missing


def access_request(root: Path, record: dict[str, Any], lang: str, name: str, email: str) -> tuple[str, list[str]]:
    ident, missing = id_data(record, name, email)
    runs = record.get("runs", [])
    flags = {"data_portability", "has_fields"} | ({"runs"} if runs else set())
    body = render(template(root, lang, "access-default"), flags, {"runs_list": ", ".join(runs), "id_data": ident})
    return body + name + "\n", missing


def admonition(root: Path, lang: str, request_date: str, problem: str, name: str) -> str:
    text = template(root, lang, "admonition")
    return render(text, set(), {"request_date": request_date, "request_article": "15"}, [problem]) + name + "\n"


def complaint(root: Path, lang: str, request_date: str, recipient: str, story: str, contact: str, name: str) -> str:
    text = template(root, lang, "complaint")
    variables = {"request_date": request_date, "request_article": "15", "request_recipient_address": recipient}
    return render(text, set(), variables, [story, contact]) + name + "\n"


def idreply(email: str, name: str) -> str:
    return IDREPLY.format(email=email) + name + "\n"
