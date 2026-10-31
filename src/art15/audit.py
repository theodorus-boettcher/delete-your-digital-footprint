"""Audit what a company sent back.

inventory: what is in the export (field names and record counts, never the values)
checklist: what Art. 15 says the answer must contain, checked on the cover letter and documents
policy_diff: data categories the company's privacy policy declares that the export does not contain

Evidence rule: a finding that says something is there carries a quote found word for word in a named file,
or it is dropped. A "missing" finding means the model and a keyword search found no such passage.
"""

import csv
import io
import json
import re
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from bs4 import BeautifulSoup

from art15.llm import Ask, Decide

MAX_PARSE = 200 * 2**20  # bigger files are listed, not parsed
MEDIA = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".mp4", ".mov", ".mp3", ".m4a", ".wav", ".ogg"}

# Category -> field-name keywords (en/de/it/fr/es). Short keywords match whole tokens, long ones prefixes.
CATEGORIES: dict[str, list[str]] = {
    "identity": ["name", "firstname", "lastname", "surname", "birth", "dob", "gender", "age", "nationality",
                 "passport", "nome", "cognome", "nachname", "vorname", "geburt", "nom", "prenom", "nacimiento"],
    "contact": ["email", "mail", "phone", "mobile", "telephone", "address", "street", "zip", "postal", "postcode",
                "city", "telefono", "indirizzo", "adresse", "telefon", "strasse", "direccion"],
    "location": ["lat", "lng", "lon", "latitude", "longitude", "geo", "location", "gps", "coordinate", "place",
                 "posizione", "standort", "ubicacion"],
    "device_ids": ["device", "ip", "useragent", "agent", "browser", "os", "imei", "idfa", "gaid", "advertisingid",
                   "cookie", "mac", "fingerprint", "dispositivo", "gerat"],
    "activity": ["search", "query", "click", "view", "watch", "visit", "history", "session", "login", "event", "activity",
                 "log", "page", "listen", "play", "stream", "like", "follow", "ricerca", "suche", "verlauf"],
    "purchases": ["order", "purchase", "payment", "price", "amount", "transaction", "invoice", "card", "iban",
                  "billing", "cart", "refund", "ordine", "pagamento", "bestellung", "zahlung", "commande"],
    "communications": ["message", "chat", "comment", "conversation", "post", "review", "call", "sms", "inbox",
                       "messaggio", "nachricht"],
    "inferences": ["interest", "segment", "inferred", "affinity", "prediction", "score", "audience", "ad",
                   "ads", "advertis", "marketing", "preference", "topic", "persona", "interesse", "werbung"],
    "third_party": ["partner", "third", "imported", "broker", "provider", "offline", "terze", "dritt"],
    "content": ["photo", "video", "image", "upload", "file", "document", "attachment", "foto", "bild"],
    "account": ["account", "created", "setting", "consent", "subscription", "membership", "plan", "profile",
                "password", "username", "konto", "consenso"],
}

# What each category means, for the models.
CATEGORY_TEXT = {
    "identity": "who you are: name, birth date, gender, nationality, ID numbers",
    "contact": "how to reach you: email, phone, postal address",
    "location": "where you are or were: coordinates, places, location history",
    "device_ids": "your devices and connections: IP address, device or advertising IDs, cookies, browser",
    "activity": "what you did: searches, clicks, views, plays, logins, history",
    "purchases": "what you bought or paid: orders, payments, invoices, cards",
    "communications": "what you wrote or said: messages, chats, comments, reviews, calls",
    "inferences": "what they guess about you: interests, segments, ad audiences, scores, predictions",
    "third_party": "data from other companies: partners, data brokers, imported data",
    "content": "files you uploaded: photos, videos, documents",
    "account": "your account: settings, consents, subscriptions, account dates",
    "other": "none of these, or technical metadata",
}

# Art. 15 checklist: item -> (what the answer must contain, keywords for the fallback search)
CHECKLIST: dict[str, tuple[str, list[str]]] = {
    "purposes": ("the purposes of the processing (Art. 15(1)(a))", ["purpose", "zweck", "finalit", "finalidad"]),
    "categories": ("the categories of personal data concerned (Art. 15(1)(b))", ["categor", "kategorie"]),
    "recipients": ("the recipients to whom the data were disclosed, named individually and not only as "
                   "categories (Art. 15(1)(c), CJEU C-154/21)", ["recipient", "empfänger", "destinatari", "shared with", "disclose"]),
    "retention": ("how long the data are stored, or the criteria (Art. 15(1)(d))",
                  ["retain", "retention", "stored for", "speicherdauer", "aufbewahr", "conservazione", "conserv"]),
    "rights": ("the rights to rectification, erasure, restriction and objection (Art. 15(1)(e))",
               ["rectification", "erasure", "berichtigung", "löschung", "rettifica", "cancellazione", "object"]),
    "complaint": ("the right to lodge a complaint with a supervisory authority (Art. 15(1)(f))",
                  ["supervisory authority", "aufsichtsbehörde", "garante", "complaint", "beschwerde", "reclamo"]),
    "source": ("where data not collected from me came from (Art. 15(1)(g))",
               ["source", "third-party", "third party", "obtained from", "herkunft", "quelle", "origine", "fonte"]),
    "automated": ("whether automated decision-making or profiling is used, and its logic (Art. 15(1)(h))",
                  ["automated", "profiling", "automatisiert", "automatizzat", "profilazione"]),
    "transfers": ("the safeguards for transfers to third countries (Art. 15(2))",
                  ["third countr", "standard contractual", "adequacy", "drittland", "paesi terzi", "transfer"]),
}
STATUSES = ["present", "partial", "missing"]
# Checked from the export itself, not from the letter.
DATA_ITEMS = {"copy": "a copy of my personal data (Art. 15(3))",
              "machine_readable": "the data I provided in a machine-readable format (Art. 20(1))"}


def describe(item: str) -> str:
    return CHECKLIST[item][0] if item in CHECKLIST else DATA_ITEMS.get(item, item.replace("_", " "))


@dataclass
class Item:
    file: str
    kind: str
    fields: list[str] = field(default_factory=list)
    records: int = 0
    text: str = ""
    categories: dict[str, str] = field(default_factory=dict)  # field -> category


@dataclass
class Finding:
    kind: str  # checklist | policy | inventory
    item: str
    status: str  # present | partial | check | missing
    quote: str = ""
    file: str = ""


def norm(text: str) -> str:
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def verified(quote: str, text: str) -> bool:
    q = norm(quote).strip(" .\"'")
    return len(q) >= 8 and q in norm(text)


# ---------- inventory ----------


def unpack(path: Path, dest: Path) -> list[Path]:
    """The folders to inventory: the export itself when it is a folder, plus `dest`, where every zip is
    extracted (zips inside zips too). The export itself is never changed. Password zips raise RuntimeError."""
    shutil.rmtree(dest, ignore_errors=True)
    zips = [path] if path.is_file() else sorted(path.rglob("*.zip"))
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            zf.extractall(dest / z.stem)  # extractall drops absolute paths and ".." parts
    while inner := sorted(dest.rglob("*.zip")):
        for z in inner:
            with zipfile.ZipFile(z) as zf:
                zf.extractall(z.with_suffix(""))
            z.unlink()  # a copy inside dest
    return ([path] if path.is_dir() else []) + ([dest] if zips else [])


def _tokens(name: str) -> list[str]:
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [t for t in re.split(r"[^a-zA-Z0-9äöüß]+", name.lower()) if t]


PERSON_NAME = {"first", "last", "full", "given", "family", "middle", "display", "real", "your", "my", "user", "customer"}


def rule_category(name: str) -> str | None:
    # Only the last part of a path names the value ("orders[].items[].price" -> price).
    tokens = _tokens(re.split(r"[.\[\]]+", name.strip(".[]"))[-1] if name.strip(".[]") else name)
    if "name" in tokens and not set(tokens) - {"name"} <= PERSON_NAME:
        tokens = [t for t in tokens if t != "name"]  # artistName, fileName: the name of something else
    for t in tokens:  # the first word decides: ip_address is a device id, email_address contact data
        for cat, keys in CATEGORIES.items():
            if any(t == k if len(k) <= 3 else t.startswith(k) for k in keys):
                return cat
    return None


def _json_fields(obj: Any, prefix: str, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _json_fields(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        for v in obj[:50]:  # shortcut: the first 50 elements decide the shape of a list
            _json_fields(v, prefix + "[]", out)
    elif prefix:
        out.add(prefix)


def _records(obj: Any) -> int:
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        return sum(len(v) for v in obj.values() if isinstance(v, list)) or 1
    return 1


def inspect(path: Path, rel: str, ocr: Callable[[bytes], str] | None = None) -> Item:
    suffix = path.suffix.lower()
    if suffix in MEDIA:
        return Item(rel, "media", records=1)
    if path.stat().st_size > MAX_PARSE:
        return Item(rel, "too_big")
    raw = path.read_bytes()
    if suffix == ".json":
        try:
            obj = json.loads(raw)
        except ValueError:
            return Item(rel, "other")
        fields: set[str] = set()
        _json_fields(obj, "", fields)
        return Item(rel, "json", sorted(fields), _records(obj))
    if suffix in {".csv", ".tsv"}:
        text = raw.decode("utf-8-sig", errors="replace")
        delim = "\t" if suffix == ".tsv" else (csv.Sniffer().sniff(text[:4096], ",;\t|").delimiter if text.strip() else ",")
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        return Item(rel, "csv", rows[0] if rows else [], max(len(rows) - 1, 0))
    if suffix in {".html", ".htm"}:
        soup = BeautifulSoup(raw, "html.parser")
        heads = sorted({th.get_text(" ", strip=True) for th in soup.find_all("th")} - {""})
        nrows = sum(len(t.find_all("tr")) for t in soup.find_all("table"))
        return Item(rel, "html", heads, nrows, soup.get_text("\n", strip=True))
    if suffix == ".pdf":
        pages = []
        with pymupdf.open(stream=raw, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
            for page in doc:
                text = page.get_text()
                if not text.strip() and ocr is not None:  # a scanned page
                    text = ocr(page.get_pixmap(dpi=150).tobytes("png"))
                pages.append(text)
        return Item(rel, "pdf", records=len(pages), text="\n".join(pages))
    if suffix == ".xml":
        text = raw.decode("utf-8", errors="replace")
        return Item(rel, "xml", sorted(set(re.findall(r"<([A-Za-z_][\w.-]*)", text))), text.count("\n") + 1)
    if suffix in {".txt", ".md", ".eml"}:
        return Item(rel, "text", text=raw.decode("utf-8", errors="replace"))
    return Item(rel, "other")


def inventory(roots: list[Path], ocr: Callable[[bytes], str] | None = None) -> list[Item]:
    return [inspect(p, str(p.relative_to(root)), ocr) for root in roots for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix.lower() != ".zip"]


MAP_PROMPT = """These are field names from a company's export of my personal data. Give each one the data
category it most likely holds:
{cats}
Field names:
{names}"""
FIELD_STATE = "Field names from a company's export of a user's personal data. Names only, no values."
MIN_FIELD_P = 0.5  # a Jev answer below this leaves the field without a category


def categorize(items: list[Item], ask: Ask | None, decide: Decide | None = None) -> None:
    """Fill item.categories: keyword rules first, a model (Jev if given) only for names the rules don't know."""
    unknown: set[str] = set()
    for it in items:
        if it.kind == "media":
            it.categories[it.file] = "content"
        for f in it.fields:
            cat = rule_category(f)
            if cat:
                it.categories[f] = cat
            else:
                unknown.add(f)
    if not unknown or not (ask or decide):
        return
    labels = model_categories(sorted(unknown), ask, decide)
    for it in items:
        for f in it.fields:
            if f not in it.categories and labels.get(f) in CATEGORIES:
                it.categories[f] = labels[f]


def model_categories(names: list[str], ask: Ask | None, decide: Decide | None = None) -> dict[str, str]:
    """Field name -> category from Jev (names only go out) or the local model."""
    if decide is not None:
        questions = {f"f{i}": (f"Which kind of personal data does the field '{n}' hold?", CATEGORY_TEXT)
                     for i, n in enumerate(names)}
        answers = decide(FIELD_STATE, questions)
        return {names[int(k[1:])]: d.choice for k, d in answers.items() if d.p >= MIN_FIELD_P}
    assert ask is not None
    cats = "\n".join(f"- {k}: {v}" for k, v in CATEGORY_TEXT.items())
    labels: dict[str, str] = {}
    for i in range(0, len(names), 150):
        chunk = names[i : i + 150]
        schema = {"type": "object", "properties": {n: {"enum": list(CATEGORY_TEXT)} for n in chunk}, "required": chunk}
        labels |= ask(MAP_PROMPT.format(cats=cats, names="\n".join(chunk)), schema)
    return labels


def categories_found(items: list[Item]) -> dict[str, list[str]]:
    """Category -> evidence ("file: field") from the inventory."""
    out: dict[str, list[str]] = {}
    for it in items:
        for f, cat in it.categories.items():
            out.setdefault(cat, []).append(f"{it.file}: {f}" if f != it.file else it.file)
    return out


# ---------- checklist ----------

CHECK_PROMPT = """This is a company's answer to my data access request under Article 15 GDPR: the cover
letter and the documents that came with it. For each item, say whether the answer gives it:
"present" (given specifically for me), "partial" (only generic, or only categories where names are needed)
or "missing". For present and partial, copy the sentence that shows it word for word and give its file name.

Items:
{items}

{docs}"""


def _documents(items: list[Item], letter: str) -> dict[str, str]:
    docs = {"reply.txt": letter} if letter.strip() else {}
    for it in items:
        if it.text.strip() and it.kind in {"pdf", "text", "html"}:
            docs[it.file] = it.text
    return docs


def _keyword_quote(keys: list[str], docs: dict[str, str]) -> tuple[str, str] | None:
    for name, text in docs.items():
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            low = sentence.lower()
            if any(k in low for k in keys) and len(sentence) < 400:
                return sentence.strip(), name
    return None


def checklist(items: list[Item], letter: str, ask: Ask | None) -> list[Finding]:
    docs = _documents(items, letter)
    claims: dict[str, dict[str, str]] = {}
    if ask and docs:
        text = "\n\n".join(f"=== file: {n}\n{t[:20000]}" for n, t in docs.items())[:60000]
        schema = {"type": "object", "properties": {k: {
            "type": "object",
            "properties": {"status": {"enum": STATUSES}, "quote": {"type": "string"}, "file": {"type": "string"}},
            "required": ["status", "quote", "file"]} for k in CHECKLIST}, "required": list(CHECKLIST)}
        listing = "\n".join(f"- {k}: {desc}" for k, (desc, _) in CHECKLIST.items())
        claims = ask(CHECK_PROMPT.format(items=listing, docs=text), schema)
    out = []
    for key, (_, keys) in CHECKLIST.items():
        c = claims.get(key, {})
        quote = c.get("quote", "")
        # The file the model named, else whichever document holds the quote.
        name = next((n for n in [c.get("file", ""), *docs] if verified(quote, docs.get(n, ""))), None)
        if c.get("status") in {"present", "partial"} and name:
            out.append(Finding("checklist", key, c["status"], quote, name))
        elif hit := _keyword_quote(keys, docs):
            out.append(Finding("checklist", key, "check", *hit))  # a passage exists: the user decides
        else:
            out.append(Finding("checklist", key, "missing", "", f"searched {len(docs)} document{'s' * (len(docs) != 1)}"))
    data = [it for it in items if it.kind in {"json", "csv", "xml", "html", "media"} or (it.kind == "pdf" and it.records)]
    readable = [it for it in items if it.kind in {"json", "csv", "xml"}]
    out.append(Finding("checklist", "copy", "present" if data else "missing", "", ", ".join(it.file for it in data[:5])))
    out.append(Finding("checklist", "machine_readable", "present" if readable else "missing", "",
                       ", ".join(it.file for it in readable[:5])))
    return out


# ---------- policy diff ----------

POLICY_PROMPT = """This is part of a company's privacy policy. Which categories of personal data does it say
the company collects about its users? Use only these categories: {cats}. For each one, copy the sentence
that says so word for word.

{text}"""


def policy_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    return re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n", strip=True))


def policy_diff(policy: str, source: str, items: list[Item], ask: Ask) -> list[Finding]:
    found = categories_found(items)
    schema = {"type": "object", "properties": {"declared": {"type": "array", "items": {
        "type": "object", "properties": {"category": {"enum": list(CATEGORIES)}, "quote": {"type": "string"}},
        "required": ["category", "quote"]}}}, "required": ["declared"]}
    declared: dict[str, str] = {}
    for i in range(0, min(len(policy), 120000), 12000):
        out = ask(POLICY_PROMPT.format(cats=", ".join(CATEGORIES), text=policy[i : i + 12000]), schema)
        for d in out.get("declared", []):
            if d.get("category") in CATEGORIES and d["category"] not in declared and verified(d.get("quote", ""), policy):
                declared[d["category"]] = d["quote"]
    return [Finding("policy", cat, "missing" if cat not in found else "present", quote, source)
            for cat, quote in declared.items()]


def score(findings: list[Finding]) -> float:
    """Share of the Art. 15 checklist answered: present 1, partial or check 0.5."""
    items = [f for f in findings if f.kind == "checklist"]
    points = {"present": 1.0, "partial": 0.5, "check": 0.5}
    return sum(points.get(f.status, 0.0) for f in items) / len(items) if items else 0.0
