"""What a company's reply means for the request.

Bounces by rule. The rest by the local model, which must quote the reply word for word and pass plain-rule checks.
"""

import re
from dataclasses import dataclass
from email.message import Message
from email.utils import parseaddr

from art15.audit import verified
from art15.llm import Ask
from art15.mail import attachments, body_text, header

LABEL_TEXT = {
    "acknowledged": "they confirm receipt or say they are working on it",
    "id_request": "they want me to prove who I am (ID document, extra details, log in, confirm by link)",
    "extension": "they extend the deadline (Art. 12(3) allows two more months)",
    "redirect_webform": "they tell me to use a web form, privacy portal or self-service download instead",
    "data_delivered": "they send my data, or a link or password to download it",
    "refusal": "they refuse, say they hold no data, or say the request is not valid",
    "other": "anything else (auto-replies, newsletters, unrelated mail)",
}
LABELS = list(LABEL_TEXT)

PROMPT = f"""You read a company's reply to my data access request under Article 15 GDPR.
Pick the one label that fits best:
{chr(10).join(f"- {k}: {v}" for k, v in LABEL_TEXT.items())}
Copy one sentence from the reply, word for word, that shows the label. If the reply gives a web form or
download link, put that URL in "url".

Subject: {{subject}}
Attachments: {{files}}
Reply:
<<<
{{body}}
>>>"""

SCHEMA = {
    "type": "object",
    "properties": {"label": {"enum": LABELS}, "quote": {"type": "string"}, "url": {"type": "string"}},
    "required": ["label", "quote"],
}
URL = re.compile(r"https?://[^\s<>\"')\]]+")


@dataclass
class Reply:
    label: str
    quote: str = ""
    url: str = ""
    backend: str = ""
    pick: str = ""  # the model's own label, before the checks (for `art15 eval`)


def is_bounce(msg: Message) -> bool:
    name, addr = parseaddr(header(msg, "From"))
    return (
        msg.get_content_type() == "multipart/report"
        or re.match(r"(mailer-daemon|postmaster)@", addr, re.I) is not None
        or name.lower() == "mail delivery subsystem"
    )


def plausible(label: str, body: str, files: list[str]) -> bool:
    """Cheap checks a label must pass whatever the model says."""
    if label == "data_delivered":
        return bool(files) or URL.search(body) is not None
    if label == "redirect_webform":
        return URL.search(body) is not None
    if label == "extension":
        return re.search(r"month|monat|mese|mois|mes\b|extend|verläng|prorog|prolong|\d{1,2}[./]\d{1,2}", body, re.I) is not None
    return True


def classify(msg: Message, ask: Ask) -> Reply:
    if is_bounce(msg):
        return Reply("bounce", backend="rule", pick="bounce")
    body, subject = body_text(msg), header(msg, "Subject")
    files = [name for name, _ in attachments(msg)]
    out = ask(PROMPT.format(subject=subject, files=", ".join(files) or "none", body=body[:8000]), SCHEMA)
    label, quote = str(out.get("label", "other")), str(out.get("quote", ""))
    # Evidence rule: a label the model cannot back with a sentence from the reply is not trusted.
    ok = label in LABELS and verified(quote, body + " " + subject) and plausible(label, body, files)
    return Reply(label if ok else "other", quote, str(out.get("url", "")) if ok else "", backend="local", pick=label)
