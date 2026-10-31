"""Static HTML: one page with every request, one page per audit. Opens from disk, nothing loads from the web."""

import sqlite3
from datetime import date
from html import escape
from pathlib import Path

from art15.audit import CATEGORIES, CHECKLIST, Finding, score
from art15.companies import Companies

CSS = """
:root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --fg:#f2f2f7; --muted:#98989d; --line:#2c2c2e;
  --ok:#3fb950; --warn:#d29922; --bad:#f85149; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; max-width:1000px; margin:0 auto; padding:24px 16px; }
h1 { font-size:22px; } h2 { font-size:17px; margin-top:28px; } .muted { color:var(--muted); }
table { border-collapse:collapse; width:100%; } th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-weight:600; color:var(--muted); font-size:13px; } a { color:inherit; }
.present, .complete { color:var(--ok); } .partial, .check, .acknowledged, .extended, .answered { color:var(--warn); }
.missing, .overdue, .refused, .bounced, .admonished, .complaint_drafted { color:var(--bad); }
q { font-style:italic; } .wrap { overflow-x:auto; }
"""

LABELS = {
    "purposes": "Purposes", "categories": "Data categories", "recipients": "Recipients, by name",
    "retention": "How long it is kept", "rights": "Your other rights", "complaint": "Right to complain",
    "source": "Where the data came from", "automated": "Automated decisions", "transfers": "Transfers outside the EU",
    "copy": "A copy of the data", "machine_readable": "Machine-readable copy",
}


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            f"initial-scale=1'><title>{escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>")


def findings_of(con: sqlite3.Connection, ref: str) -> list[Finding]:
    return [Finding(r["kind"], r["item"], r["status"], r["quote"] or "", r["file"] or "")
            for r in con.execute("SELECT * FROM findings WHERE ref = ? ORDER BY id", (ref,))]


def audit_page(name: str, ref: str, fs: list[Finding]) -> str:
    rows = "".join(
        f"<tr><td>{escape(LABELS.get(f.item, f.item))}</td><td class={f.status}>{f.status}</td>"
        f"<td>{'<q>' + escape(f.quote) + '</q>' if f.quote else ''} <span class=muted>{escape(f.file)}</span></td></tr>"
        for f in fs if f.kind == "checklist")
    policy = "".join(
        f"<tr><td>{escape(f.item.replace('_', ' '))}</td><td class={f.status}>{'in export' if f.status == 'present' else 'not in export'}</td>"
        f"<td><q>{escape(f.quote)}</q></td></tr>" for f in fs if f.kind == "policy")
    inv = "".join(
        f"<tr><td>{escape(f.item.replace('_', ' '))}</td><td>{escape(f.file)}</td><td class=muted>{escape(f.quote)}</td></tr>"
        for f in fs if f.kind == "inventory")
    body = (f"<p><a href=index.html>All requests</a></p><h1>{escape(name)}</h1>"
            f"<p>Art. 15 checklist score: <b>{score(fs):.0%}</b>. <span class=muted>{escape(ref)}. "
            f"'check' means a matching passage exists and you decide; every 'missing' is worth checking by hand.</span></p>"
            f"<h2>What the answer must contain</h2><div class=wrap><table><tr><th>Item</th><th>Status</th><th>Evidence</th></tr>{rows}</table></div>")
    if policy:
        body += (f"<h2>Privacy policy vs export</h2><p class=muted>Kinds of data their privacy policy says they collect.</p>"
                 f"<div class=wrap><table><tr><th>Data</th><th>Export</th><th>Policy says</th></tr>{policy}</table></div>")
    if inv:
        body += (f"<h2>What the export contains</h2><p class=muted>Field names only, never values.</p>"
                 f"<div class=wrap><table><tr><th>Category</th><th>Fields</th><th>Examples</th></tr>{inv}</table></div>")
    return page(f"art15: {name}", body)


def build(con: sqlite3.Connection, companies: Companies, out: Path, today: date) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for r in con.execute("SELECT * FROM requests ORDER BY created_at, slug"):
        name = companies.records.get(r["slug"], {}).get("name", r["slug"])
        fs = findings_of(con, r["ref"])
        days = (date.fromisoformat(r["deadline"]) - today).days if r["deadline"] else None
        link = escape(name)
        if fs:
            (out / f"{r['ref']}.html").write_text(audit_page(name, r["ref"], fs))
            link = f"<a href='{r['ref']}.html'>{escape(name)}</a>"
        last = con.execute("SELECT label FROM messages WHERE ref = ? ORDER BY date DESC, rowid DESC",
                           (r["ref"],)).fetchone()
        reply = "" if last is None else last["label"].replace("_", " ")
        rows.append(f"<tr><td>{link}</td><td class={r['status']}>{r['status'].replace('_', ' ')}</td><td>{reply}</td>"
                    f"<td>{r['sent_at'] or ''}</td><td>{r['deadline'] or ''}</td>"
                    f"<td>{'' if days is None or r['status'] not in {'sent', 'acknowledged', 'id_requested', 'extended'} else days}</td>"
                    f"<td>{f'{score(fs):.0%}' if fs else ''}</td></tr>")
    body = (f"<h1>My data requests</h1><p class=muted>GDPR Art. 15 access requests, as of {today}. "
            f"{len(rows)} requests. Not legal advice.</p><div class=wrap><table><tr><th>Company</th><th>Status</th><th>Last reply</th>"
            f"<th>Sent</th><th>Deadline</th><th>Days left</th><th>Checklist</th></tr>{''.join(rows)}</table></div>"
            f"<p class=muted>Categories checked: {', '.join(c.replace('_', ' ') for c in CATEGORIES)}. "
            f"Checklist items: {len(CHECKLIST) + 2}.</p>")
    path = out / "index.html"
    path.write_text(page("art15 requests", body))
    return path
