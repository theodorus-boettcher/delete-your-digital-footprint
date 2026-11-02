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
  -> find     the privacy contact of companies that are on no public list, from their own website
  -> review   you tick who gets a request; anything risky starts unticked
  -> send     sends the requests after you type the number to confirm
  -> sync     reads the replies, follows the one-month deadline
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
  scan reads only From, Date and Subject. `find` reads the newest email of a sender it can't place, on your machine, and
  keeps none of the text. art15 connects to your mailbox, the datenanfragen data and company websites, nothing else.
- **Nothing leaves without you.** `art15 send` shows every recipient and sends only after you type `send 12` for 12
  letters. There is no flag or schedule to skip that. Prefer pressing Send yourself? `art15 draft` puts the letters in Gmail
  Drafts instead. Complaints to an authority are never sent by the tool.
- **It never sends ID documents.** When a company asks for one, art15 drafts a reply citing Art. 12(6): the
  request came from the email address registered with the account.

## Setup

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), a Gmail account with an
[app password](https://myaccount.google.com/apppasswords) (2-step verification on, IMAP on) and, for reading
replies and auditing, [Ollama](https://ollama.com) with `qwen3:8b` (and `qwen2.5vl:3b` for scanned PDFs).
An 8 GB GPU is enough.

```bash
git clone https://github.com/theodorus-boettcher/delete-your-digital-footprint
cd delete-your-digital-footprint
uv sync
uv run playwright install chromium   # a background browser for pages that need JavaScript
uv run art15 start          # your details, then finds your companies and their contacts. Safe to stop and rerun
uv run art15 review         # tick who gets a request
uv run art15 send           # shows who, asks you to confirm, then sends about one every 30 seconds
uv run art15 forms          # companies that only take a web form: the link and the text to paste
uv run art15 sync           # run daily: replies, bounces, deadlines. Reminders then go with `art15 send`
uv run art15 audit google --export ~/Downloads/takeout.zip
uv run art15 report         # data/report/index.html
```

`art15 start` runs `setup`, `scan` and `find`, which also work on their own. `art15 show <company>` prints a letter,
`art15 status` lists every request, `art15 mark <ref> sent` records a request you sent through a web form, and
`--now 2026-12-03` pretends today is another day (for dry runs).

`art15 eval replies` and `art15 eval fields` measure the local model on replies and field names you label by hand:
accuracy, how many replies go to you, and speed.

## How well the search works

`art15 eval finder` hides 30 verified companies from the public list and looks for them from scratch, using only their
websites. A contact counts as found when it is the same address, another address at the same company, or a privacy form.

| Method | Contact found |
|---|---|
| Rules only | 15 of 30 |
| Rules, local model and browser | 23 of 30 |

Results move a little between runs because websites change, and 30 is a small sample. It finds no address on some sites,
and a few sites block it, which art15 never tries to get around. Check every address in `art15 review`.

## Limits

- Companies come from the [datenanfragen.de](https://www.datarequests.org) database (about 3,000 records,
  mostly German and EU companies). For likely accounts without a record, `art15 find` walks the company's own site and
  keeps a privacy address, a privacy form or, failing those, its general address, with the sentence it came from
  (in `data/records/`, one file per company: check them, fix or delete). It uses no search engine, only reads pages, and
  never logs in or fills a form.
- Some companies only take requests through a web form (Google, Meta): art15 gives you the link and the letter
  text, and tracks the deadline once you mark it sent.
- `art15 send` is new. The rest of the loop has run on a real Gmail account, but `send` has only run in tests so far, so
  send one letter to an address of your own first.
- Sending goes through your own Gmail account, so Gmail's daily limits apply. Near-identical letters can still look like
  bulk mail: start with a small batch.
- A complaint to an authority stays a Gmail draft, for you to read and file. Many authorities want their own form, a
  signature or certified mail.
- Reminders and the ID reply are written in English; the request and complaint templates exist in 12 languages.
- Deadlines skip weekends but not public holidays.
- Use it only for your own mailbox and your own data. Requests made in someone else's name are a known attack
  on GDPR processes, which is why art15 only writes from the account it reads.

## Credits

Company records, supervisory authorities and letter templates: [datenanfragen/data](https://github.com/datenanfragen/data)
(CC0) by Datenanfragen.de e.V.

The code is under the [MIT license](LICENSE).

This tool helps you use your rights under the GDPR. It is not legal advice.
