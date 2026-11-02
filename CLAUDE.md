# art15

An agent that gets the user's personal data back from companies with GDPR Art. 15 access requests:
it finds the companies in their mail (headers only), drafts the requests, tracks the deadlines, reads
the replies, audits what came back (Art. 15 checklist + privacy policy vs export) and drafts the
reminders and complaints. Portfolio project (FDE/agent lane), a new build on open data.

- Plan: `~/.claude/plans/shimmering-knitting-muffin.md` (local agent, search first). First plan, phases 0-4:
  `~/.claude/plans/peppy-inventing-valley.md`.
- Company records, authorities and letter templates: `data/datenanfragen/` (clone of datenanfragen/data, CC0),
  made by `art15 setup`. Tests use the small copy in `tests/fixtures/da/`.
- Local model: Ollama v0.40.5 in `~/.local/opt/ollama` (no sudo; start with
  `~/.local/opt/ollama/bin/ollama serve`). Models `qwen3:8b` (text) and `qwen2.5vl:3b` (scanned pages).
- `uv run pytest` and `uv run mypy src tests` (strict) must pass.

Rules that are easy to miss:
- One send path: `art15 send`, the only SMTP use. It sends after the user types `send N`, about one letter per 30 s, and
  stops on bounces. Never add a `--yes`, a schedule or any way around the typed confirmation. It never sends a complaint
  (`SENDABLE` in `cli.py` is an allowlist) or an attachment. `art15 draft` still puts letters in Gmail Drafts for sending by hand.
- Never attach or send ID documents. An ID request gets the Art. 12(6) reply draft.
- Act only for the inbox owner: requests go from the account's own address only.
- The scan reads headers only. Bodies are read only for replies from companies with an open request, and by
  `art15 find`: the newest email of an unknown sender, for the local model. Its text is never stored or printed.
- `art15 find`: no search engine. GET only: it never logs in, fills a form or gets around a block.
- Network: IMAP, the datenanfragen pull and company websites. All models run on this machine (Ollama on
  127.0.0.1). No cloud model, ever: never add a cloud path or a key for one.
- The model sees field names and counts from exports, never values.
- Evidence rule: a finding that says something is present needs a quote found word for word in a named file.
- `.env` holds the Gmail app password: git-ignored, never print it.
- Commits: no task ID, never Co-Authored-By or "Generated with Claude" lines. Author email is the user's
  personal gmail (set locally in this repo).
- No em dashes in text meant for other people (README, posts). The README keeps its "not legal advice" note.
