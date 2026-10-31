"""What a company's reply means for the request.

Bounces by rule. The rest by a model: the local one must quote the reply word for word; Jev (--jev) gives
no quote, so its label counts only above a probability threshold. Both pass the same plain-rule checks.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import Message
from email.utils import parseaddr

from art15.audit import verified
from art15.llm import Ask, Decide
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
INSTRUCTIONS = "This is a company's reply to my data access request under Article 15 GDPR. What does it say?"

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
    confidence: float | None = None  # Jev only
    backend: str = ""
    pick: str = ""  # the model's own label, before the checks (for `art15 eval`)


def is_bounce(msg: Message) -> bool:
    name, addr = parseaddr(header(msg, "From"))
    return (
        msg.get_content_type() == "multipart/report"
        or re.match(r"(mailer-daemon|postmaster)@", addr, re.I) is not None
        or name.lower() == "mail delivery subsystem"
    )


def redact(text: str, private: Iterable[str] = ()) -> str:
    """What goes to Jev: no passwords, tokens in links, email addresses, long numbers or the user's own name."""
    text = re.sub(r"(?im)^.*\b(password|passwort|kennwort|codice|mot de passe|contraseña|code|pin)\b.*$", "[password line]", text)
    text = URL.sub(lambda m: f"[link: {re.sub(r'^https?://', '', m.group(0)).split('/')[0]}]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "[email]", text)
    text = re.sub(r"\d{6,}", "[number]", text)
    for word in sorted({w for p in private for w in [p, *p.split()] if len(w) >= 3}, key=len, reverse=True):
        text = re.sub(re.escape(word), "[me]", text, flags=re.I)
    return text


def plausible(label: str, body: str, files: list[str]) -> bool:
    """Cheap checks a label must pass whatever the model says."""
    if label == "data_delivered":
        return bool(files) or URL.search(body) is not None
    if label == "redirect_webform":
        return URL.search(body) is not None
    if label == "extension":
        return re.search(r"month|monat|mese|mois|mes\b|extend|verläng|prorog|prolong|\d{1,2}[./]\d{1,2}", body, re.I) is not None
    return True


def classify(msg: Message, ask: Ask, decide: Decide | None = None, threshold: float = 0.8,
             private: Iterable[str] = ()) -> Reply:
    if is_bounce(msg):
        return Reply("bounce", backend="rule", pick="bounce")
    body, subject = body_text(msg), header(msg, "Subject")
    files = [name for name, _ in attachments(msg)]
    link = next(iter(URL.findall(body)), "")
    if decide is not None:
        # Attachment names can carry the user's name: only their types go out.
        kinds = ", ".join(sorted({f.rsplit(".", 1)[-1].lower() for f in files})) or "none"
        state = redact(f"Subject: {subject}\nAttachments: {len(files)} ({kinds})\n\n{body[:8000]}", private)
        d = decide(state, {"label": (INSTRUCTIONS, LABEL_TEXT)})["label"]
        ok = d.choice in LABELS and d.p >= threshold and plausible(d.choice, body, files)
        return Reply(d.choice if ok else "other", url=link if ok and d.choice == "redirect_webform" else "",
                     confidence=d.p, backend="jev", pick=d.choice)
    out = ask(PROMPT.format(subject=subject, files=", ".join(files) or "none", body=body[:8000]), SCHEMA)
    label, quote = str(out.get("label", "other")), str(out.get("quote", ""))
    # Evidence rule: a label the model cannot back with a sentence from the reply is not trusted.
    ok = label in LABELS and verified(quote, body + " " + subject) and plausible(label, body, files)
    return Reply(label if ok else "other", quote, str(out.get("url", "")) if ok else "", backend="local", pick=label)
