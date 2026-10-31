# art15

Get your data back from the companies that hold it.

Under Article 15 of the GDPR anyone in the EU can ask a company for a copy of all the personal data it holds
on them, plus why it uses it, who it shares it with and how long it keeps it. The company has one month to
answer. In practice most answers are incomplete: noyb found that only 16.5% of access requests were answered
properly, and academic studies put full compliance between 15% and 53%.

Writing one request is easy (datarequests.org has a good free generator). The hard part is everything after:
finding every company that has your data, keeping track of 30 deadlines, reading the replies, checking whether
the answer is complete, and chasing the ones that are not. art15 does that loop for you, on your laptop:

```
your mailbox (headers only)
  -> scan     which companies write to you, and which of them likely hold an account
  -> draft    an Art. 15 request per company, put in your Drafts folder (you press Send)
  -> sync     sees what you sent, reads the replies, follows the one-month deadline
              acknowledged / ID request / extension / web form / data delivered / refusal / bounce
  -> audit    what came back: field inventory, Art. 15 checklist, privacy policy vs export
  -> chase    drafts the follow-up, the reminder and, after that, the complaint to your data protection authority
```

## What makes it different

- **It audits the answer.** For each export it lists what is inside (field names and record counts, never the
  values), maps the fields to data categories, checks the cover letter against the eleven things Art. 15
  requires (purposes, recipients by name per CJEU C-154/21, retention, source, automated decisions, transfer
  safeguards, a machine-readable copy, ...) and compares the export with the company's own privacy policy:
  "the policy says it collects your location; the export has no location data".
- **Every claim has evidence.** A finding that says something is there carries a sentence copied word for word
  from a named file, or it is dropped. A small local model cannot make up a compliant answer.
- **No cloud AI.** The model runs on your own machine (Ollama), and your mail is never sent to any AI service. The
  scan reads only From, Date and Subject. art15 connects to your mailbox, the datenanfragen data and company websites,
  nothing else. Nothing is ever sent by the tool: it writes drafts, you read them and press Send.
- **It never sends ID documents.** When a company asks for one, art15 drafts a reply citing Art. 12(6): the
  request came from the email address registered with the account.

## Setup

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), a Gmail account with an
[app password](https://myaccount.google.com/apppasswords) (2-step verification on, IMAP on) and, for reading
replies and auditing, [Ollama](https://ollama.com) with `qwen3:8b` (and `qwen2.5vl:3b` for scanned PDFs).
An 8 GB GPU is enough.

```bash
uv sync
uv run art15 setup          # company data, your name/email/country, checks mail login and models
uv run art15 scan           # which companies are in your mail
uv run art15 find           # privacy emails of likely accounts with no record, from their privacy pages
uv run art15 draft --top 30 # requests for the 30 most likely accounts, into Gmail Drafts
uv run art15 sync           # run daily (by hand or from a timer)
uv run art15 audit google --export ~/Downloads/takeout.zip
uv run art15 report         # data/report/index.html
```

`art15 eval replies` and `art15 eval fields` measure the local model on replies and field names you label by hand:
accuracy, how many replies go to you, and speed.

`art15 status` lists every request, `art15 mark <ref> sent` records a request you sent through a web form,
`--now 2026-12-03` pretends today is another day (for dry runs).

## Limits

- Companies come from the [datenanfragen.de](https://www.datarequests.org) database (about 3,000 records,
  mostly German and EU companies). For likely accounts without a record, `art15 find` reads the company's privacy page and keeps a privacy or DPO
  address with the sentence it came from (in `data/records/`, one file per company: check them, fix or delete).
- Some companies only take requests through a web form (Google, Meta): art15 gives you the link and the letter
  text, and tracks the deadline once you mark it sent.
- Reminders and the ID reply are written in English; the request and complaint templates exist in 12 languages.
- Deadlines skip weekends but not public holidays.
- Use it only for your own mailbox and your own data. Requests made in someone else's name are a known attack
  on GDPR processes, which is why art15 only writes from the account it reads.

## Credits

Company records, supervisory authorities and letter templates: [datenanfragen/data](https://github.com/datenanfragen/data)
(CC0) by Datenanfragen.de e.V.

This tool helps you use your rights under the GDPR. It is not legal advice.
