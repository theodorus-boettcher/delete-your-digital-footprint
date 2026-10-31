"""art15 command line. Run it from the repo folder (or set ART15_HOME): data/ and .env live there."""

import argparse
import getpass
import hashlib
import imaplib
import json
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from contextlib import nullcontext
from datetime import date, datetime
from email.message import EmailMessage, Message
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from art15 import audit, db, evals, finder, letters, llm, mail, report
from art15.browser import Browser
from art15.classify import LABELS, Reply, classify
from art15.companies import FREEMAIL, Companies, registrable
from art15.deadlines import admonition_deadline, request_deadline

HOME = Path(os.environ.get("ART15_HOME", Path.cwd()))
DATA = HOME / "data"
DA = DATA / "datenanfragen"
OWN = DATA / "records"  # records found by `art15 find` or written by hand, same format as datenanfragen
REPO = "https://github.com/datenanfragen/data"

ACCOUNT_LIKELY = re.compile(
    r"welcome|verify|verification|confirm|activate|your order|order confirm|receipt|invoice|password|your account|"
    r"sign.?up|registration|booking|subscription|benvenut|conferma|ordine|ricevuta|fattura|willkommen|"
    r"bestätig|bestellung|rechnung|passwort|konto|bienvenue|commande|facture|bienvenido|pedido",
    re.I,
)


# ---------- config ----------


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("ART15_")})
    return {k: v for k, v in env.items() if v}


def save_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{key}=")] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.env = load_env(HOME / ".env")
        self.today: date = args.now or date.today()
        self.con = db.connect(DATA / "art15.db")
        self.ask = llm.make_ask(self.env)
        self._companies: Companies | None = None

    @property
    def companies(self) -> Companies:
        if self._companies is None:
            if not DA.exists():
                sys.exit("No company data yet: run `art15 setup` first.")
            self._companies = Companies(DA, self.env.get("ART15_COUNTRY", ""), OWN)
        return self._companies

    @property
    def me(self) -> str:
        return self.env["ART15_EMAIL"]

    @property
    def name(self) -> str:
        return self.env["ART15_NAME"]

    def imap(self) -> imaplib.IMAP4_SSL:
        return mail.connect(self.env.get("ART15_IMAP_HOST", "imap.gmail.com"), self.me, self.env["ART15_APP_PASSWORD"])

    def lang(self, slug: str) -> str:
        rec = self.companies.records[slug]
        return str(rec.get("request-language") or self.env.get("ART15_LANG", "en"))


def authorities(country: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted((DA / "supervisory-authorities").glob("*.json")):
        rec = json.loads(p.read_text())
        if country in rec.get("relevant-countries", []):
            out.append(rec)
    return out


# ---------- setup ----------


def cmd_setup(ctx: Ctx, args: argparse.Namespace) -> None:
    if (DA / ".git").exists():
        subprocess.run(["git", "-C", str(DA), "pull", "-q", "--ff-only"], check=True)
    else:
        subprocess.run(["git", "clone", "-q", "--depth", "1", REPO, str(DA)], check=True)
    print(f"company records: {len(list((DA / 'companies').glob('*.json')))}")

    path = HOME / ".env"
    asks = {"ART15_NAME": "Your full name", "ART15_EMAIL": "Your email (the inbox to scan)",
            "ART15_COUNTRY": "Your country, two letters (it, de, fr, ...)"}
    for key, prompt in asks.items():
        if key not in ctx.env:
            answer = input(f"{prompt}: ").strip()
            save_env(path, key, answer.lower() if key == "ART15_COUNTRY" else answer)
    if "ART15_APP_PASSWORD" not in load_env(path):
        print("Gmail: Google account > Security > 2-Step Verification > App passwords.")
        save_env(path, "ART15_APP_PASSWORD", getpass.getpass("App password (not shown): ").replace(" ", ""))
    ctx.env = load_env(path)

    auths = authorities(ctx.env["ART15_COUNTRY"])
    if len(auths) > 1 and "ART15_AUTHORITY" not in ctx.env:
        print("Your country has several data protection authorities; set ART15_AUTHORITY in .env to one of:")
        for a in auths:
            print(f"  {a['slug']:<22} {a['name']}")
    elif auths:
        print(f"authority: {auths[0]['name']}")

    try:
        imap = ctx.imap()
        print("IMAP: ok, folders", [mail.folder(imap, f) for f in (r"\All", r"\Sent", r"\Drafts")])
        imap.logout()
    except (imaplib.IMAP4.error, OSError, LookupError) as e:
        print(f"IMAP: failed ({e}). Check the app password and that IMAP is on in Gmail settings.")

    try:
        with Browser():
            print("browser: ok")
    except SystemExit as e:
        print(f"browser: {e}")

    try:
        have = llm.models()
        for key, default in (("ART15_MODEL", "qwen3:8b"), ("ART15_VLM", "qwen2.5vl:3b")):
            m = ctx.env.get(key, default)
            print(f"model {m}: {'ok' if m in have else f'missing, run: ollama pull {m}'}")
    except httpx.HTTPError:
        print("Ollama: not running (start it with `ollama serve`). Without it, only scan and draft work.")


# ---------- scan ----------


def cmd_scan(ctx: Ctx, args: argparse.Namespace) -> None:
    imap = ctx.imap()
    own = registrable(ctx.me.rsplit("@", 1)[1])
    seen: dict[str, dict[str, Any]] = {}
    n = 0
    for msg in mail.headers(imap):
        n += 1
        dom = registrable(mail.sender_domain(msg))
        if not dom or dom == own or dom in FREEMAIL:
            continue
        when = mail.msg_date(msg)
        day = when.date().isoformat() if when else ""
        subj = mail.header(msg, "Subject")
        s = seen.setdefault(dom, {"msgs": 0, "first": day, "last": day, "likely": 0, "subject": subj})
        s["msgs"] += 1
        if day:
            s["first"] = min(s["first"] or day, day)
            s["last"] = max(s["last"], day)
        if ACCOUNT_LIKELY.search(subj) and not s["likely"]:
            s["likely"], s["subject"] = 1, subj
    imap.logout()

    known = {slug for dom in seen for slug in ctx.companies.by_domain.get(dom, [])}
    ctx.con.execute("DELETE FROM senders")
    for dom, s in seen.items():
        ctx.con.execute("INSERT INTO senders VALUES (?,?,?,?,?,?,?)",
                        (dom, ctx.companies.match(dom, known), s["msgs"], s["first"], s["last"], s["likely"], s["subject"]))
    ctx.con.commit()

    top = ctx.con.execute("SELECT * FROM senders ORDER BY msgs DESC LIMIT 50").fetchall()
    print(f"{n} messages, {len(seen)} sender domains. Top 50 domains with a company record: "
          f"{sum(1 for r in top if r['slug'])}/{len(top)}")
    show_senders(ctx.con, args.limit)


def show_senders(con: sqlite3.Connection, limit: int) -> None:
    rows = con.execute("SELECT * FROM senders WHERE slug IS NOT NULL ORDER BY account_likely DESC, msgs DESC LIMIT ?",
                       (limit,)).fetchall()
    print(f"\n{'company':<28} {'domain':<26} {'msgs':>5}  first seen  account?")
    for r in rows:
        print(f"{r['slug']:<28} {r['domain']:<26} {r['msgs']:>5}  {r['first_seen']:<10}  {'yes' if r['account_likely'] else ''}")
    unknown = con.execute("SELECT domain, msgs FROM senders WHERE slug IS NULL AND account_likely = 1 "
                          "ORDER BY msgs DESC LIMIT 15").fetchall()
    if unknown:
        print("\nLikely accounts with no company record (`art15 find` looks for their privacy email):")
        print("  " + ", ".join(f"{r['domain']} ({r['msgs']})" for r in unknown))


# ---------- find ----------


def fetch(url: str) -> str:
    r = httpx.get(url, follow_redirects=True, timeout=20, headers={"User-Agent": "Mozilla/5.0 art15"})
    r.raise_for_status()
    return r.text


def page_reader(browser: Browser | None) -> finder.Read:
    def read(url: str) -> str:
        time.sleep(1)  # one site at a time, one request a second
        return finder.read_page(url, fetch, browser.html if browser else None)
    return read


def need_model() -> None:
    try:
        llm.models()
    except httpx.HTTPError:
        sys.exit("Ollama is off: start it with `~/.local/opt/ollama/bin/ollama serve`, or pass --no-llm.")


def sender_identity(ctx: Ctx, dom: str, ask: llm.Ask) -> tuple[str, str]:
    """Company and website from the newest email of an unknown sender. The email is read here and not kept."""
    try:
        imap = ctx.imap()
        msg = mail.newest_from(imap, dom)
        imap.logout()
    except (imaplib.IMAP4.error, OSError):
        return dom, dom
    return finder.identify(dom, mail.body_text(msg), ask, f"{ctx.name} {ctx.me}") if msg else (dom, dom)


def write_record(dom: str, site: str, company: str, r: finder.Lookup) -> str:
    assert r.found is not None
    slug = re.sub(r"[^a-z0-9]+", "-", dom)
    rec: dict[str, Any] = {"slug": slug, "relevant-countries": ["all"], "name": company, "web": f"https://{site}/",
                           "runs": [dom], "sources": r.pages, "quality": r.how, "comments": [f"art15 find: {r.found.quote}"],
                           "suggested-transport-medium": "webform" if r.found.webform else "email"}
    rec |= {"webform": r.found.webform} if r.found.webform else {"email": r.found.email}
    OWN.mkdir(parents=True, exist_ok=True)
    (OWN / f"{slug}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    return slug


def cmd_find(ctx: Ctx, args: argparse.Namespace) -> None:
    try:
        find_all(ctx, args)
    except httpx.ConnectError:  # web pages handle their own errors, so this is the model
        sys.exit("The model stopped answering. Start Ollama and run this again: finished senders are skipped.")


def find_all(ctx: Ctx, args: argparse.Namespace) -> None:
    tried = set() if args.retry else {r["domain"] for r in ctx.con.execute("SELECT domain FROM lookups")}
    rows = ctx.con.execute("SELECT domain FROM senders WHERE slug IS NULL AND account_likely = 1 ORDER BY msgs DESC")
    todo = [r["domain"] for r in rows if r["domain"] not in tried]
    todo = todo[: args.top] if args.top else todo
    ask = None if args.no_llm else ctx.ask
    if ask:
        need_model()
    known = {r["slug"] for r in ctx.con.execute("SELECT DISTINCT slug FROM senders WHERE slug IS NOT NULL")}
    counts: Counter[str] = Counter()
    by_email = {r["email"].lower(): s for s, r in ctx.companies.records.items() if r.get("email")}
    with Browser() if not args.no_browser else nullcontext(None) as browser:
        read = page_reader(browser)
        for i, dom in enumerate(todo, 1):
            company, site = sender_identity(ctx, dom, ask) if ask else (dom, dom)
            slug = ctx.companies.match(site, known) if site != dom else None
            if slug:
                status, pages, note = "list", 0, f"same company as {slug}, already on the list"
            else:
                r = finder.walk(site, company, read, ask, {dom})
                status, pages, note = r.status, len(r.pages), ""
                if r.found and (slug := by_email.get(r.found.email)):  # one company, one request
                    status, note = "list", f"same address as {slug}"
                elif r.found:
                    slug = write_record(dom, site, company, r)
                    by_email[r.found.email] = slug
                    note = f"{r.found.webform or r.found.email}  ({r.how}) {r.found.url}\n{'':<6}\"{r.found.quote[:160]}\""
            if slug:
                ctx.con.execute("UPDATE senders SET slug = ? WHERE domain = ?", (slug, dom))
            ctx.con.execute("INSERT OR REPLACE INTO lookups VALUES (?,?,?,?)", (dom, status, pages, ctx.today.isoformat()))
            ctx.con.commit()
            counts[status] += 1
            print(f"[{i}/{len(todo)}] {dom:<28} {status:<8} {note}", flush=True)
    print("\n" + ", ".join(f"{k}: {v}" for k, v in counts.most_common()))
    print(f"Records are in {OWN.relative_to(HOME)}/: check each one before `art15 draft`; fix or delete a file to correct it.")


# ---------- draft ----------


def new_ref(con: sqlite3.Connection) -> str:
    while True:
        ref = f"art15-{secrets.token_hex(2)}"
        if not con.execute("SELECT 1 FROM requests WHERE ref = ?", (ref,)).fetchone():
            return ref


def save_letter(ctx: Ctx, ref: str, kind: str, msg: EmailMessage | None, to: str, subject: str, body: str) -> None:
    ctx.con.execute("INSERT INTO letters (ref, kind, to_addr, subject, body, msgid, drafted_at) VALUES (?,?,?,?,?,?,?)",
                    (ref, kind, to, subject, body, msg["Message-ID"] if msg else None, ctx.today.isoformat()))
    ctx.con.commit()


def cmd_draft(ctx: Ctx, args: argparse.Namespace) -> None:
    slugs = list(args.slugs)
    if args.top:
        rows = ctx.con.execute("SELECT DISTINCT slug FROM senders WHERE slug IS NOT NULL AND slug NOT IN "
                               "(SELECT slug FROM requests) ORDER BY account_likely DESC, msgs DESC").fetchall()
        slugs += [r["slug"] for r in rows if r["slug"] not in slugs][: args.top]
    imap = None if args.dry_run else ctx.imap()
    for slug in slugs:
        rec = ctx.companies.records.get(slug)
        if rec is None:
            print(f"{slug}: no such company record")
            continue
        if ctx.con.execute("SELECT 1 FROM requests WHERE slug = ? AND status NOT IN ('cancelled', 'closed', 'bounced')",
                           (slug,)).fetchone():
            print(f"{slug}: already has a request")
            continue
        ref, lang = new_ref(ctx.con), ctx.lang(slug)
        body, missing = letters.access_request(DA, rec, lang, ctx.name, ctx.me)
        subject = letters.subject("access", lang, ref)
        medium = rec.get("suggested-transport-medium", "email")
        notes = []
        if missing:
            notes.append(f"they also ask for: {', '.join(missing)} (add it to the draft if you have it)")
        if rec.get("needs-id-document"):
            notes.append("their record says they want an ID document; art15 never sends one (Art. 12(6))")
        if medium == "webform" or not rec.get("email"):
            path = DATA / "letters" / f"{ref}-{slug}.txt"
            if not args.dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"Subject: {subject}\n\n{body}")
            where = rec.get("webform") or rec.get("address", "").replace("\n", ", ")
            notes.insert(0, f"by hand: {where} (letter text in {path.relative_to(HOME)}); then `art15 mark {ref} sent`")
            status, msg, to = "manual", None, ""
        else:
            to = rec["email"]
            msg = mail.draft(ctx.me, to, subject, body)
            if imap:
                mail.append_draft(imap, msg)
            status = "drafted"
        if not args.dry_run:
            ctx.con.execute("INSERT INTO requests (ref, slug, status, medium, created_at) VALUES (?,?,?,?,?)",
                            (ref, slug, status, "email" if msg else medium, ctx.today.isoformat()))
            save_letter(ctx, ref, "request", msg, to, subject, body)
        print(f"{ref}  {slug:<24} {status:<8} {to}")
        for note in notes:
            print(f"        {note}")
        if args.dry_run and args.show:
            print(body)
    if imap:
        imap.logout()
        print("\nThe drafts are in your Gmail Drafts folder. Read them, then press Send.")


# ---------- sync ----------


def company_domains(ctx: Ctx, slug: str) -> set[str]:
    rec = ctx.companies.records[slug]
    doms = ctx.companies.domains(slug) | {r["domain"] for r in ctx.con.execute("SELECT domain FROM senders WHERE slug = ?", (slug,))}
    if rec.get("email"):
        doms.add(registrable(rec["email"].rsplit("@", 1)[1]))
    return doms - FREEMAIL - {""}


def draft_letter(ctx: Ctx, imap: imaplib.IMAP4 | None, ref: str, kind: str, to: str, subject: str, body: str,
                 in_reply_to: str = "", attachments: dict[str, str] | None = None) -> None:
    msg = mail.draft(ctx.me, to, subject, body, attachments)
    if in_reply_to:
        msg["In-Reply-To"] = msg["References"] = in_reply_to
    if imap:
        mail.append_draft(imap, msg)
    save_letter(ctx, ref, kind, msg, to, subject, body)
    print(f"{ref}: {kind} drafted to {to}")


def request_letter(ctx: Ctx, ref: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = ctx.con.execute("SELECT * FROM letters WHERE ref = ? AND kind = 'request'", (ref,)).fetchone()
    return row


def draft_admonition(ctx: Ctx, imap: imaplib.IMAP4 | None, req: sqlite3.Row, problem: str) -> None:
    ref, lang = req["ref"], ctx.lang(req["slug"])
    request = request_letter(ctx, ref)
    to = request["to_addr"] if request else ""
    body = letters.admonition(DA, lang, req["sent_at"], problem, ctx.name)
    subject = letters.subject("admonition", lang, ref)
    if to:
        draft_letter(ctx, imap, ref, "admonition", to, subject, body)
    else:
        path = DATA / "letters" / f"{ref}-admonition.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Subject: {subject}\n\n{body}")
        save_letter(ctx, ref, "admonition", None, "", subject, body)
        print(f"{ref}: reminder due, send it through their web form today: {path.relative_to(HOME)}")
    db.set_status(ctx.con, ref, "admonished")


def draft_complaint(ctx: Ctx, imap: imaplib.IMAP4 | None, req: sqlite3.Row, adm: sqlite3.Row) -> None:
    auths = authorities(ctx.env.get("ART15_COUNTRY", ""))
    slug = ctx.env.get("ART15_AUTHORITY")
    auth = next((a for a in auths if a["slug"] == slug), None) if slug else (auths[0] if len(auths) == 1 else None)
    if auth is None or not auth.get("email"):
        print(f"{req['ref']}: complaint due, but no authority email (set ART15_COUNTRY / ART15_AUTHORITY)")
        return
    rec = ctx.companies.records[req["slug"]]
    lang = auth.get("complaint-language", "en")
    findings = ctx.con.execute("SELECT item FROM findings WHERE ref = ? AND kind = 'checklist' AND status = 'missing'",
                               (req["ref"],)).fetchall()
    story = (f"I did not receive a complete answer within the one-month period of Article 12(3) GDPR. "
             f"On {adm['sent_at'] or adm['drafted_at']} I sent a reminder giving two more weeks, which also passed")
    if findings:
        story += f". Their answer is missing: {', '.join(f['item'] for f in findings)}"
    story += ". Both letters are attached"
    recipient = f"{rec['name']}\n{rec.get('address', '')}\n{rec.get('email', '')}".strip()
    body = letters.complaint(DA, lang, req["sent_at"], recipient, story, f"by email at {ctx.me}", ctx.name)
    request = request_letter(ctx, req["ref"])
    files = {f"{req['ref']}-request.txt": request["body"] if request else "", f"{req['ref']}-reminder.txt": adm["body"]}
    draft_letter(ctx, imap, req["ref"], "complaint", auth["email"], letters.subject("complaint", lang, req["ref"]), body,
                 attachments=files)
    db.set_status(ctx.con, req["ref"], "complaint_drafted")
    if auth.get("web"):
        print(f"        some authorities prefer their web form: {auth['web']}")


def apply_reply(ctx: Ctx, imap: imaplib.IMAP4 | None, req: sqlite3.Row, msg: Message, r: Reply) -> None:
    ref = req["ref"]
    to_status = {"bounce": "bounced", "acknowledged": "acknowledged", "id_request": "id_requested",
                 "extension": "extended", "redirect_webform": "redirected", "data_delivered": "answered",
                 "refusal": "refused"}.get(r.label)
    if to_status is None:
        if r.pick not in {"other", r.label}:  # the checks overruled the model
            print(f"{ref}: a reply needs you ({r.backend} said {r.pick})")
        return
    fields: dict[str, object] = {}
    if r.label == "extension":
        fields = {"extended": 1, "deadline": request_deadline(date.fromisoformat(req["sent_at"]), True).isoformat()}
    if r.label == "redirect_webform":
        fields = {"note": r.url}
    try:
        db.set_status(ctx.con, ref, to_status, **fields)
    except db.BadTransition as e:
        print(f"{ref}: {r.label} reply, status kept ({e})")
        return
    print(f"{ref}: {r.label}" + (f" ({r.url})" if r.url else ""))
    if r.label == "id_request" and not ctx.con.execute("SELECT 1 FROM letters WHERE ref = ? AND kind = 'idreply'", (ref,)).fetchone():
        draft_letter(ctx, imap, ref, "idreply", parseaddr(mail.header(msg, "From"))[1],
                     letters.subject("idreply", "en", ref), letters.idreply(ctx.me, ctx.name), mail.header(msg, "Message-ID"))
    if r.label == "refusal":
        draft_admonition(ctx, imap, ctx.con.execute("SELECT * FROM requests WHERE ref = ?", (ref,)).fetchone(),
                         f'Your answer ("{r.quote}") does not fulfil my request: Article 15 GDPR gives me the right to '
                         f"this information, and you have to confirm whether you process data about me.")
    if r.label == "data_delivered":
        ctx.con.execute("DELETE FROM findings WHERE ref = ?", (ref,))
        ctx.con.commit()
        print(f"        run: art15 audit {ref}")


def is_bulk(msg: Message) -> bool:
    return bool(mail.header(msg, "List-Unsubscribe")) or mail.header(msg, "Precedence").lower() in {"bulk", "list", "junk"}


def cmd_sync(ctx: Ctx, args: argparse.Namespace) -> None:
    imap = ctx.imap()
    # 1. Which of our drafts did the user send?
    for lt in ctx.con.execute("SELECT * FROM letters WHERE sent_at IS NULL AND to_addr != ''").fetchall():
        for msg in mail.messages(imap, r"\Sent", "SUBJECT", lt["ref"]):
            if " ".join(mail.header(msg, "Subject").split()) == " ".join(lt["subject"].split()):
                sent = (mail.msg_date(msg) or datetime.now()).date().isoformat()
                ctx.con.execute("UPDATE letters SET sent_at = ? WHERE id = ?", (sent, lt["id"]))
                ctx.con.commit()
                if lt["kind"] == "request":
                    db.set_status(ctx.con, lt["ref"], "sent", sent_at=sent,
                                  deadline=request_deadline(date.fromisoformat(sent)).isoformat())
                print(f"{lt['ref']}: {lt['kind']} sent {sent}")
                break

    # 2. Replies from companies with an open request.
    me = ctx.me.lower()
    for req in ctx.con.execute(f"SELECT * FROM requests WHERE sent_at IS NOT NULL AND status IN ({','.join('?' * len(db.OPEN))})",
                               sorted(db.OPEN)).fetchall():
        ref, since = req["ref"], mail.imap_date(date.fromisoformat(req["sent_at"]))
        domains = company_domains(ctx, req["slug"])
        our_ids = {r["msgid"] for r in ctx.con.execute("SELECT msgid FROM letters WHERE ref = ?", (ref,)) if r["msgid"]}
        found: dict[str, Message] = {}
        for crit in [("SUBJECT", ref), *[("FROM", d) for d in sorted(domains)]]:
            for msg in mail.messages(imap, r"\All", "SINCE", since, *crit):
                found.setdefault(mail.header(msg, "Message-ID") or hashlib.sha1(msg.as_bytes()).hexdigest(), msg)
        for mid, msg in sorted(found.items(), key=lambda kv: mail.msg_date(kv[1]) or datetime.min):
            if parseaddr(mail.header(msg, "From"))[1].lower() == me or not mail.is_reply(msg, ref, domains, our_ids):
                continue
            if ctx.con.execute("SELECT 1 FROM messages WHERE message_id = ?", (mid,)).fetchone():
                continue
            if ref not in mail.header(msg, "Subject") and is_bulk(msg):
                continue  # newsletters from the same company
            r = classify(msg, ctx.ask)
            folder = DATA / "mail" / ref
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{len(list(folder.glob('*.eml'))) + 1:03d}.eml"
            path.write_bytes(msg.as_bytes())
            for fname, payload in mail.attachments(msg):
                out = DATA / "exports" / req["slug"] / fname
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(payload)
                print(f"{ref}: saved {out.relative_to(HOME)}")
            ctx.con.execute("INSERT INTO messages (message_id, ref, date, from_addr, subject, label, quote, path, backend) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (mid, ref, (mail.msg_date(msg) or datetime.now()).date().isoformat(), mail.header(msg, "From"),
                             mail.header(msg, "Subject"), r.label, r.quote, str(path.relative_to(HOME)), r.backend))
            ctx.con.commit()
            apply_reply(ctx, imap, ctx.con.execute("SELECT * FROM requests WHERE ref = ?", (ref,)).fetchone(), msg, r)

    deadlines(ctx, imap)
    imap.logout()
    report.build(ctx.con, ctx.companies, DATA / "report", ctx.today)
    print(f"report: {(DATA / 'report' / 'index.html').relative_to(HOME)}")


def deadlines(ctx: Ctx, imap: imaplib.IMAP4 | None) -> None:
    """3. Reminders, follow-ups and complaints that are due today."""
    today = ctx.today.isoformat()
    for req in ctx.con.execute("SELECT * FROM requests").fetchall():
        st, ref = req["status"], req["ref"]
        if st in db.WAITING and req["deadline"] and req["deadline"] < today:
            db.set_status(ctx.con, ref, "overdue")
            draft_admonition(ctx, imap, req, "Unfortunately, I have not received an answer so far. The period of one "
                             "month according to Article 12(3) sentence 1 GDPR is therefore exceeded.")
        elif st == "answered" and req["medium"] != "self-service":
            missing = ctx.con.execute("SELECT kind, item, status FROM findings WHERE ref = ? AND kind IN ('checklist', 'policy') "
                                      "AND status IN ('missing', 'partial')", (ref,)).fetchall()
            audited = ctx.con.execute("SELECT 1 FROM findings WHERE ref = ?", (ref,)).fetchone()
            if audited and not missing:
                db.set_status(ctx.con, ref, "complete")
            elif missing:
                checks = [audit.describe(m["item"]) + (" (only partly)" if m["status"] == "partial" else "")
                          for m in missing if m["kind"] == "checklist"]
                policy = [m["item"].replace("_", " ") for m in missing if m["kind"] == "policy"]
                problem = "Thank you for your answer. It is incomplete."
                if checks:
                    problem += f" Missing: {'; '.join(checks)}."
                if policy:
                    problem += (" Your privacy policy says you collect these kinds of data, but the copy you sent "
                                f"contains none: {', '.join(policy)}.")
                draft_admonition(ctx, imap, req, problem)
        elif st == "admonished":
            adm = ctx.con.execute("SELECT * FROM letters WHERE ref = ? AND kind = 'admonition' ORDER BY id DESC",
                                  (ref,)).fetchone()
            # shortcut: a reminder sent by hand through a web form counts from the day it was written.
            sent = adm and (adm["sent_at"] or (adm["drafted_at"] if not adm["to_addr"] else None))
            if sent and admonition_deadline(date.fromisoformat(sent)).isoformat() < today:
                draft_complaint(ctx, imap, req, adm)


# ---------- audit ----------


def find_request(ctx: Ctx, key: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = ctx.con.execute(
        "SELECT * FROM requests WHERE ref = ? OR slug = ? ORDER BY created_at DESC", (key, key)).fetchone()
    return row


def policy_url(rec: dict[str, Any]) -> str | None:
    for src in rec.get("sources", []):
        if re.search(r"privacy|datenschutz|privacidad|confidentialit|informativa|privacybeleid", src, re.I):
            return str(src)
    return None


def cmd_audit(ctx: Ctx, args: argparse.Namespace) -> None:
    req = find_request(ctx, args.key)
    if req is None:
        if args.key not in ctx.companies.records:
            sys.exit(f"{args.key}: no request and no company record with that slug")
        # A self-service export (Takeout, Meta, Amazon) with no email request behind it.
        ref = new_ref(ctx.con)
        ctx.con.execute("INSERT INTO requests (ref, slug, status, medium, created_at) VALUES (?,?,?,?,?)",
                        (ref, args.key, "answered", "self-service", ctx.today.isoformat()))
        ctx.con.commit()
        req = find_request(ctx, ref)
        assert req is not None
    ref, slug = req["ref"], req["slug"]
    export = Path(args.export) if args.export else DATA / "exports" / slug
    if not export.exists():
        sys.exit(f"no export at {export}")
    try:
        roots = audit.unpack(export, DATA / "unpacked" / slug)
    except RuntimeError as e:  # zipfile: "File ... is encrypted, password required"
        sys.exit(f"{e}\nUnzip it by hand (the password is usually in a second email) and run: art15 audit {ref} --export <folder>")

    vlm = ctx.env.get("ART15_VLM", "qwen2.5vl:3b")

    def ocr(png: bytes) -> str:
        return str(llm.ollama(vlm, "Transcribe all text on this page.", {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}, [png]).get("text", ""))

    ask = None if args.no_llm else ctx.ask
    items = audit.inventory(roots, None if args.no_llm else ocr)
    audit.categorize(items, ask)
    letter = "\n\n".join(mail.body_text(mail.load(HOME / r["path"]))
                         for r in ctx.con.execute("SELECT path FROM messages WHERE ref = ?", (ref,)))
    findings = audit.checklist(items, letter, ask)

    rec = ctx.companies.records[slug]
    url = args.policy or policy_url(rec)
    if ask and url and not args.no_policy:
        try:
            if Path(url).exists():
                html = Path(url).read_text()
            else:
                html = httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": "Mozilla/5.0 art15"}).text
            findings += audit.policy_diff(audit.policy_text(html), url, items, ask)
        except httpx.HTTPError as e:
            print(f"privacy policy not fetched ({e}); pass one with --policy URL")
    elif not url:
        print("no privacy policy URL in the company record; pass one with --policy URL for the policy check")

    for cat, evidence in sorted(audit.categories_found(items).items()):
        findings.append(audit.Finding("inventory", cat, "present", "; ".join(evidence[:8]), f"{len(evidence)} fields"))
    ctx.con.execute("DELETE FROM findings WHERE ref = ?", (ref,))
    ctx.con.executemany("INSERT INTO findings (ref, kind, item, status, quote, file, created_at) VALUES (?,?,?,?,?,?,?)",
                        [(ref, f.kind, f.item, f.status, f.quote, f.file, ctx.today.isoformat()) for f in findings])
    ctx.con.commit()

    kinds = Counter(it.kind for it in items)
    print(f"{ref} {slug}: {len(items)} files ({', '.join(f'{v} {k}' for k, v in kinds.most_common())})")
    for f in findings:
        if f.kind != "inventory":
            print(f"  {f.kind:<9} {f.item:<17} {f.status:<8} {f.quote[:90]}")
    print(f"Art. 15 checklist score: {audit.score(findings):.0%}. Check every 'missing' and 'check' line yourself.")
    report.build(ctx.con, ctx.companies, DATA / "report", ctx.today)
    print(f"report: {(DATA / 'report' / f'{ref}.html').relative_to(HOME)}")


# ---------- eval ----------


def eval_replies(ctx: Ctx, labels: Path) -> None:
    rows = ctx.con.execute("SELECT message_id, from_addr, subject, path FROM messages ORDER BY date").fetchall()
    if not labels.exists():
        evals.write_template(labels, ["message_id", "label", "from", "subject"],
                             [[r["message_id"], "", r["from_addr"], r["subject"]] for r in rows])
        print(f"Wrote {labels} with {len(rows)} replies. Read each one (data/mail/) and fill in `label` with one of: "
              f"{', '.join([*LABELS, 'bounce'])}. Then run this again.")
        return
    truth = evals.read_labels(labels, "message_id", "label")
    results: list[evals.Row] = []
    for r in rows:
        if r["message_id"] not in truth:
            continue
        t0 = time.perf_counter()
        rep = classify(mail.load(HOME / r["path"]), ctx.ask)
        results.append(evals.Row(r["message_id"], truth[r["message_id"]], rep.pick, rep.label, time.perf_counter() - t0))
    head = ["replies", "model right", "sent to you", "right when not sent to you", "s/reply"]
    m = evals.summary(results)
    lines = [[str(int(m["n"])), f"{m['raw_accuracy']:.0%}", f"{m['to_user']:.0%}", f"{m['auto_accuracy']:.0%}", f"{m['seconds']:.2f}"]]
    html = "<h1>Reply labels</h1>" + evals.table(head, lines)
    print(evals.text_table(head, lines))
    wrong = [[x.key[:40], x.truth, x.pick] for x in results if x.pick != x.truth]
    if wrong:
        print(f"\nMistakes\n{evals.text_table(['message', 'truth', 'model'], wrong)}")
        html += "<h2>Mistakes</h2>" + evals.table(["message", "truth", "model"], wrong)
    out = DATA / "report" / "eval.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.page("art15 eval", html))
    print(f"\n{out.relative_to(HOME)}")


def eval_fields(ctx: Ctx, labels: Path) -> None:
    if not labels.exists():
        roots = [p for base in ("unpacked", "exports") for p in sorted((DATA / base).glob("*")) if p.is_dir()]
        names = sorted({f for it in audit.inventory(roots) for f in it.fields})
        evals.write_template(labels, ["field", "category"], [[n, ""] for n in names])
        print(f"Wrote {labels} with {len(names)} field names from your exports. Fill in `category` with one of: "
              f"{', '.join(audit.CATEGORY_TEXT)}. Then run this again.")
        return
    truth = evals.read_labels(labels, "field", "category")
    names = sorted(truth)
    rules = {n: audit.rule_category(n) for n in names}
    covered = [n for n in names if rules[n]]
    head = ["method", "fields labelled", "right", "seconds"]
    lines = [["keyword rules", f"{len(covered)}/{len(names)}", f"{sum(rules[n] == truth[n] for n in covered) / (len(covered) or 1):.0%}", "0"]]
    t0 = time.perf_counter()
    got = audit.model_categories(names, ctx.ask)
    secs = time.perf_counter() - t0
    lines.append(["local model", f"{len(got)}/{len(names)}", f"{sum(got.get(n) == truth[n] for n in names) / len(names):.0%}", f"{secs:.1f}"])
    both = {n: rules[n] or got.get(n) for n in names}
    lines.append(["rules, then local model", f"{sum(1 for v in both.values() if v)}/{len(names)}",
                  f"{sum(both[n] == truth[n] for n in names) / len(names):.0%}", f"{secs:.1f}"])
    print(evals.text_table(head, lines))
    out = DATA / "report" / "eval-fields.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.page("art15 field eval", "<h1>Field categories</h1>" + evals.table(head, lines)))
    print(f"\n{out.relative_to(HOME)}")


def eval_finder(ctx: Ctx, args: argparse.Namespace) -> None:
    """Pretend verified public records are missing and see if the finder recovers their address."""
    recs = [r for _, r in sorted(ctx.companies.records.items())
            if r.get("quality") == "verified" and r.get("email") and r.get("web")
            and r.get("suggested-transport-medium", "email") == "email"]
    ask = None if args.no_llm else ctx.ask
    if ask:
        need_model()
    rows = []
    with Browser() if not args.no_browser else nullcontext(None) as browser:
        read = page_reader(browser)
        for rec in random.Random(15).sample(recs, args.n):  # a fixed sample, so runs compare
            dom = registrable(urlsplit(rec["web"]).hostname or "")
            t0 = time.perf_counter()
            r = finder.walk(dom, rec["name"], read, ask)
            got = r.found.webform or r.found.email if r.found else ""
            outcome = "web form" if r.status == "webform" else r.status if not r.found else evals.finder_outcome(rec["email"], got)
            rows.append([dom, rec["email"], got, outcome, str(len(r.pages)), f"{time.perf_counter() - t0:.1f}"])
            print(f"{dom:<30} {outcome:<20} {got}", flush=True)
    counts = Counter(r[3] for r in rows)
    head = ["outcome", "companies"]
    lines = [[k, str(counts[k])] for k in ("same address", "same company domain", "different address", "web form", "blocked", "nothing")]
    print(f"\n{evals.text_table(head, lines)}\n\n{args.n} tested of {len(recs)} usable records, "
          f"{sum(float(r[5]) for r in rows) / args.n:.1f} s per company")
    mode = ("rules" if args.no_llm else "agent") + ("" if args.no_browser else "-browser")
    out = DATA / "report" / f"eval-finder-{mode}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.page("art15 finder eval", f"<h1>Finder: {mode}</h1>" + evals.table(head, lines)
                               + evals.table(["domain", "verified", "found", "outcome", "pages", "s"], rows)))
    print(f"\n{out.relative_to(HOME)}")


def cmd_eval(ctx: Ctx, args: argparse.Namespace) -> None:
    if args.what == "finder":
        eval_finder(ctx, args)
        return
    default = DATA / ("labels.csv" if args.what == "replies" else "field_labels.csv")
    (eval_replies if args.what == "replies" else eval_fields)(ctx, Path(args.labels) if args.labels else default)


# ---------- report, status, mark ----------


def cmd_report(ctx: Ctx, args: argparse.Namespace) -> None:
    path = report.build(ctx.con, ctx.companies, DATA / "report", ctx.today)
    print(path)


def cmd_status(ctx: Ctx, args: argparse.Namespace) -> None:
    for r in ctx.con.execute("SELECT * FROM requests ORDER BY created_at, slug"):
        print(f"{r['ref']}  {r['slug']:<24} {r['status']:<18} sent {r['sent_at'] or '-':<10}  due {r['deadline'] or '-'}")


def cmd_mark(ctx: Ctx, args: argparse.Namespace) -> None:
    when = (args.date or ctx.today).isoformat()
    fields: dict[str, object] = {}
    if args.status == "sent":
        fields = {"sent_at": when, "deadline": request_deadline(date.fromisoformat(when)).isoformat()}
    db.set_status(ctx.con, args.ref, args.status, **fields)
    print(f"{args.ref}: {args.status}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="art15", description="Get your data back from companies (GDPR Art. 15).")
    p.add_argument("--now", type=date.fromisoformat, help="pretend today is this date (YYYY-MM-DD), for dry runs")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="get company data, check mail login and models").set_defaults(fn=cmd_setup)
    s = sub.add_parser("scan", help="find companies in your mail (headers only)")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("find", help="look up the privacy contact of likely accounts with no company record")
    s.add_argument("--top", type=int, default=0, help="only the first N senders (default: all)")
    s.add_argument("--retry", action="store_true", help="also try senders already tried")
    s.add_argument("--no-llm", action="store_true", help="rules only, no model and no email is read")
    s.add_argument("--no-browser", action="store_true", help="plain downloads only")
    s.set_defaults(fn=cmd_find)
    s = sub.add_parser("draft", help="put access requests in your Drafts folder")
    s.add_argument("slugs", nargs="*")
    s.add_argument("--top", type=int, default=0, help="the N most likely accounts from the last scan")
    s.add_argument("--dry-run", action="store_true", help="print, save nothing")
    s.add_argument("--show", action="store_true", help="with --dry-run: print the letters")
    s.set_defaults(fn=cmd_draft)
    sub.add_parser("sync", help="sent letters, replies, deadlines, reminders").set_defaults(fn=cmd_sync)
    s = sub.add_parser("audit", help="audit what a company sent back")
    s.add_argument("key", help="request ref or company slug")
    s.add_argument("--export", help="a zip or folder (default data/exports/<slug>)")
    s.add_argument("--policy", help="privacy policy URL or saved file")
    s.add_argument("--no-policy", action="store_true")
    s.add_argument("--no-llm", action="store_true", help="rules only, no model")
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("eval", help="measure the local model (replies, fields) or the finder")
    s.add_argument("what", choices=["replies", "fields", "finder"])
    s.add_argument("--n", type=int, default=30, help="finder: how many verified companies to test")
    s.add_argument("--no-llm", action="store_true", help="finder: rules only")
    s.add_argument("--no-browser", action="store_true", help="finder: plain downloads only")
    s.add_argument("--labels", help="the CSV with your labels (default data/labels.csv or data/field_labels.csv)")
    s.set_defaults(fn=cmd_eval)
    sub.add_parser("report", help="write the HTML report").set_defaults(fn=cmd_report)
    sub.add_parser("status", help="list requests").set_defaults(fn=cmd_status)
    s = sub.add_parser("mark", help="set a request's status by hand (e.g. a web form you submitted)")
    s.add_argument("ref")
    s.add_argument("status", choices=sorted({t for ts in db.TRANSITIONS.values() for t in ts}))
    s.add_argument("--date", type=date.fromisoformat)
    s.set_defaults(fn=cmd_mark)
    args = p.parse_args(argv)
    args.fn(Ctx(args), args)


if __name__ == "__main__":
    main()
