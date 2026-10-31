"""SQLite schema and the request status rules. Only this module changes a request's status."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS senders (
    domain TEXT PRIMARY KEY, slug TEXT, msgs INTEGER, first_seen TEXT, last_seen TEXT,
    account_likely INTEGER, example_subject TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    ref TEXT PRIMARY KEY, slug TEXT NOT NULL, status TEXT NOT NULL, medium TEXT,
    sent_at TEXT, deadline TEXT, extended INTEGER DEFAULT 0, note TEXT, created_at TEXT
);
-- every letter we wrote (request, idreply, admonition, complaint); sent_at comes from the Sent folder
CREATE TABLE IF NOT EXISTS letters (
    id INTEGER PRIMARY KEY, ref TEXT NOT NULL, kind TEXT NOT NULL, to_addr TEXT, subject TEXT,
    body TEXT, msgid TEXT, drafted_at TEXT, sent_at TEXT
);
-- replies from the company
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY, ref TEXT NOT NULL, date TEXT, from_addr TEXT, subject TEXT,
    label TEXT, quote TEXT, path TEXT, confidence REAL, backend TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY, ref TEXT NOT NULL, kind TEXT, item TEXT, status TEXT,
    quote TEXT, file TEXT, created_at TEXT
);
"""

# What a request can move to. `answered` waits for an audit: complete, or a follow-up (admonition).
TRANSITIONS: dict[str, set[str]] = {
    "manual": {"sent", "cancelled"},
    "drafted": {"sent", "cancelled"},
    "sent": {"acknowledged", "id_requested", "extended", "redirected", "answered", "refused", "bounced", "overdue"},
    "acknowledged": {"id_requested", "extended", "redirected", "answered", "refused", "overdue"},
    "id_requested": {"acknowledged", "extended", "answered", "refused", "overdue"},
    "extended": {"id_requested", "answered", "refused", "overdue"},
    "redirected": {"sent", "cancelled"},
    "bounced": {"drafted", "cancelled"},
    "answered": {"complete", "admonished"},
    "refused": {"admonished"},
    "overdue": {"answered", "admonished"},
    "admonished": {"answered", "complaint_drafted"},
    "complaint_drafted": {"answered", "closed"},
    "complete": {"closed"},
}
OPEN = {"sent", "acknowledged", "id_requested", "extended", "overdue", "admonished", "complaint_drafted"}
WAITING = {"sent", "acknowledged", "id_requested", "extended"}  # the one-month clock is running


class BadTransition(ValueError):
    pass


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def set_status(con: sqlite3.Connection, ref: str, new: str, **fields: object) -> None:
    row = con.execute("SELECT status FROM requests WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        raise KeyError(ref)
    if new != row["status"] and new not in TRANSITIONS.get(row["status"], set()):
        raise BadTransition(f"{ref}: {row['status']} -> {new}")
    sets = ", ".join(f"{k} = ?" for k in ["status", *fields])
    con.execute(f"UPDATE requests SET {sets} WHERE ref = ?", (new, *fields.values(), ref))
    con.commit()
