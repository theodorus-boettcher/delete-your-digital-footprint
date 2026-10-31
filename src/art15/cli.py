"""art15 command line. Run it from the repo folder (or set ART15_HOME): data/ and .env live there."""

import argparse
import getpass
import hashlib
import imaplib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime
from email.message import EmailMessage, Message
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx

from art15 import audit, db, evals, finder, letters, llm, mail, report
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
        self.ask = llm.make_ask(self.env, args.allow_cloud)
        self.decide: llm.Decide | None = None
        if args.jev:
            if "ART15_JEV_KEY" not in self.env:
                sys.exit("--jev needs ART15_JEV_KEY in .env (an OpenRouter or TypeSafe key).")
            self.decide = llm.make_decide(self.env)
        self.threshold = float(self.env.get("ART15_JEV_THRESHOLD", "0.8"))
        self._companies: Companies | None = None

    @property
    def private(self) -> list[str]:
        """Your name and address, masked in anything sent to Jev."""
        return [v for k in ("ART15_NAME", "ART15_EMAIL") if (v := self.env.get(k))]

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

    if "ART15_JEV_KEY" in ctx.env:
        try:
            d = llm.make_decide(ctx.env)("This is a test message.", {"t": ("Is this a test?", {"yes": "it is a test", "no": "it is not"})})["t"]
            print(f"Jev: ok ({ctx.env.get('ART15_JEV_MODEL', llm.JEV_MODEL)} answered {d.choice}, p={d.p:.2f})")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            print(f"Jev: failed ({e}). Check ART15_JEV_KEY / ART15_JEV_URL.")

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


def cmd_find(ctx: Ctx, args: argparse.Namespace) -> None:
    rows = ctx.con.execute("SELECT domain FROM senders WHERE slug IS NULL AND account_likely = 1 "
                           "ORDER BY msgs DESC LIMIT ?", (args.top,)).fetchall()
    OWN.mkdir(parents=True, exist_ok=True)
    for r in rows:
        dom = r["domain"]
        found = finder.find(dom, fetch)
        if found is None:
            print(f"{dom:<26} not found (no privacy address on its privacy pages, or the site blocks scripts)")
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", dom)
        rec = {"slug": slug, "relevant-countries": ["all"], "name": dom, "web": f"https://{dom}/", "email": found.email,
               "sources": [found.url], "suggested-transport-medium": "email", "quality": "found",
               "comments": [f"art15 find: {found.quote}"]}
        (OWN / f"{slug}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        ctx.con.execute("UPDATE senders SET slug = ? WHERE domain = ?", (slug, dom))
        ctx.con.commit()
        print(f"{dom:<26} {found.email:<34} {found.url}\n{'':<27}\"{found.quote}\"")
    print(f"\nRecords are in {OWN.relative_to(HOME)}/: check each address before `art15 draft`; fix or delete a file to correct it.")


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
            print(f"{ref}: a reply needs you ({r.backend} said {r.pick}"
                  + (f", p={r.confidence:.2f})" if r.confidence is not None else ")"))
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
            r = classify(msg, ctx.ask, ctx.decide, ctx.threshold, ctx.private)
            folder = DATA / "mail" / ref
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{len(list(folder.glob('*.eml'))) + 1:03d}.eml"
            path.write_bytes(msg.as_bytes())
            for fname, payload in mail.attachments(msg):
                out = DATA / "exports" / req["slug"] / fname
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(payload)
                print(f"{ref}: saved {out.relative_to(HOME)}")
            ctx.con.execute("INSERT INTO messages (message_id, ref, date, from_addr, subject, label, quote, path, confidence, "
                            "backend) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (mid, ref, (mail.msg_date(msg) or datetime.now()).date().isoformat(), mail.header(msg, "From"),
                             mail.header(msg, "Subject"), r.label, r.quote, str(path.relative_to(HOME)), r.confidence, r.backend))
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
    audit.categorize(items, ask, None if args.no_llm else ctx.decide)
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
              f"{', '.join([*LABELS, 'bounce'])}. Then run this again (add --jev to compare with Jev).")
        return
    truth = evals.read_labels(labels, "message_id", "label")
    backends: dict[str, llm.Decide | None] = {"local": None} | ({"jev": ctx.decide} if ctx.decide else {})
    results: dict[str, list[evals.Row]] = {b: [] for b in backends}
    for r in rows:
        if r["message_id"] not in truth:
            continue
        msg = mail.load(HOME / r["path"])
        for b, decide in backends.items():
            t0 = time.perf_counter()
            rep = classify(msg, ctx.ask, decide, ctx.threshold, ctx.private)
            results[b].append(evals.Row(r["message_id"], truth[r["message_id"]], rep.pick, rep.label, rep.confidence,
                                        time.perf_counter() - t0))
    head = ["backend", "replies", "model right", "sent to you", "right when not sent to you", "s/reply"]
    lines = []
    for b, rs in results.items():
        m = evals.summary(rs)
        lines.append([b, str(int(m["n"])), f"{m['raw_accuracy']:.0%}", f"{m['to_user']:.0%}", f"{m['auto_accuracy']:.0%}",
                      f"{m['seconds']:.2f}"])
    html = "<h1>Reply labels: local model vs Jev</h1>" + evals.table(head, lines)
    print(evals.text_table(head, lines))
    if "jev" in results:
        cal = [[b, str(n), f"{acc:.0%}"] for b, n, acc in evals.calibration(results["jev"])]
        thr = [[f"{t:.1f}", f"{kept:.0%}", f"{acc:.0%}"] for t, kept, acc in evals.thresholds(results["jev"])]
        cost = f"Jev usage: {llm.usage.get('input_tokens', 0)} input tokens, ${llm.usage.get('cost', 0):.4f}"
        print(f"\nJev calibration\n{evals.text_table(['probability', 'replies', 'right'], cal)}")
        print(f"\nJev threshold\n{evals.text_table(['threshold', 'labelled for you', 'right'], thr)}\n{cost}")
        html += ("<h2>Is Jev's probability honest?</h2>" + evals.table(["probability", "replies", "right"], cal)
                 + "<h2>Threshold</h2>" + evals.table(["threshold", "labelled automatically", "right"], thr) + f"<p>{cost}</p>")
    by_key = {b: {x.key: x for x in rs} for b, rs in results.items()}
    wrong = [[k[:40], row.truth, *(by_key[b][k].pick for b in results)]
             for k, row in by_key["local"].items() if any(by_key[b][k].pick != row.truth for b in results)]
    if wrong:
        print(f"\nMistakes\n{evals.text_table(['message', 'truth', *results], wrong)}")
        html += "<h2>Mistakes</h2>" + evals.table(["message", "truth", *results], wrong)
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
              f"{', '.join(audit.CATEGORY_TEXT)}. Then run this again (add --jev to compare with Jev).")
        return
    truth = evals.read_labels(labels, "field", "category")
    names = sorted(truth)
    rules = {n: audit.rule_category(n) for n in names}
    covered = [n for n in names if rules[n]]
    head = ["method", "fields labelled", "right", "seconds"]
    lines = [["keyword rules", f"{len(covered)}/{len(names)}", f"{sum(rules[n] == truth[n] for n in covered) / (len(covered) or 1):.0%}", "0"]]
    backends: dict[str, llm.Decide | None] = {"local model": None} | ({"jev": ctx.decide} if ctx.decide else {})
    for b, decide in backends.items():
        t0 = time.perf_counter()
        got = audit.model_categories(names, ctx.ask, decide)
        secs = time.perf_counter() - t0
        lines.append([b, f"{len(got)}/{len(names)}", f"{sum(got.get(n) == truth[n] for n in names) / len(names):.0%}", f"{secs:.1f}"])
        both = {n: rules[n] or got.get(n) for n in names}
        lines.append([f"rules, then {b}", f"{sum(1 for v in both.values() if v)}/{len(names)}",
                      f"{sum(both[n] == truth[n] for n in names) / len(names):.0%}", f"{secs:.1f}"])
    print(evals.text_table(head, lines))
    out = DATA / "report" / "eval-fields.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.page("art15 field eval", "<h1>Field categories</h1>" + evals.table(head, lines)))
    print(f"\n{out.relative_to(HOME)}")


def cmd_eval(ctx: Ctx, args: argparse.Namespace) -> None:
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
    p.add_argument("--allow-cloud", action="store_true", help="use the cloud model from .env instead of the local one")
    p.add_argument("--jev", action="store_true",
                   help="use Jev (cloud) to label replies (redacted) and export fields (names only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="get company data, check mail login and models").set_defaults(fn=cmd_setup)
    s = sub.add_parser("scan", help="find companies in your mail (headers only)")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("find", help="look up the privacy email of likely accounts with no company record")
    s.add_argument("--top", type=int, default=30)
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
    s = sub.add_parser("eval", help="local model vs Jev on replies or field names you labelled by hand")
    s.add_argument("what", choices=["replies", "fields"])
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
