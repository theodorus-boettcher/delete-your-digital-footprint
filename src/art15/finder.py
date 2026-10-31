"""A company's privacy contact from its own website, for senders with no datenanfragen record.

Rules first, at no cost. The local model helps only where rules can't: it names the company from an email
footer and picks the next page. Code chooses what it sees and checks what it says. No search engine, and
nothing but GET requests: it reads the company's own pages and never logs in or fills a form.
"""

import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import pymupdf
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from art15.audit import policy_text, verified
from art15.companies import FREEMAIL, registrable
from art15.llm import Ask

Read = Callable[[str], str]

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # sitemaps

MAX_PAGES = 8  # pages read per company
MAX_TRIES = 30  # with the pages that failed
MAX_LINKS = 15  # links shown to the model
MAX_TEXT = 8000  # characters of a page shown to the model
FOOTER = 1500  # characters at the end of an email
MAX_SITEMAP = 5  # privacy pages taken from a sitemap
THIN = 1500  # a page with less visible text than this probably needs JavaScript

PRIVACY_PAGE = re.compile(r"privacy|datenschutz|privacidad|confidentialit|informativa|privacybeleid|gdpr|rgpd|dsgvo", re.I)
MORE_PAGES = re.compile(r"contact|kontakt|contatt|impressum|imprint|legal|legali|help|support|aiuto|hilfe|data.?request|rights|diritti|rechte", re.I)
PRIVACY_BOX = re.compile(r"privacy|dpo|rpd|datenschutz|gdpr|rgpd|dsgvo|data.?protection|dataprotect|privacidad|protezione|dsb", re.I)
# Where privacy pages usually live, when no link leads there. Most of these 404 at no cost.
GUESSES = ("/privacy", "/privacy-policy", "/privacy-notice", "/privacy-statement", "/our-privacy-policy", "/legal/privacy",
           "/legal/privacy-policy", "/legal/privacy-notice", "/legal-documents/privacy-notice", "/policies/privacy-policy",
           "/pages/privacy-policy", "/en/privacy", "/en/privacy-policy", "/it/privacy", "/datenschutz", "/datenschutzerklaerung",
           "/informativa-privacy", "/politique-de-confidentialite", "/politica-de-privacidad")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
# An address for another law's users (Hinge: "If you are located in Brazil ... dpobrazil@hinge.co") is not ours,
# nor one for authorities (Hinge: "Article 11 of the DSA ... Point of Contact for Member State authorities").
OTHER_LAW = re.compile(r"brazil|brasil|lgpd|california|ccpa|canad|australia|japan|korea|china|india|\bdsa\b|digital services act", re.I)
# Sample text in privacy portal pages ("Generitech Privacy, 123 Main Street"), not anyone's address.
PLACEHOLDER = re.compile(r"generitech|@example\.|@(?:your)?(?:company|domain)\.", re.I)
PEC = re.compile(r"@(?:[\w-]+\.)*(?:pec|legalmail)\.", re.I)  # Italian certified mail: refuses ordinary email
# Boxes for something else, whatever the page around them says.
WRONG_BOX = re.compile(r"security|abuse|sales|sponsor|marketing|press|jobs|career|billing|no-?reply|donotreply|partner|advert|assicurazioni|insurance", re.I)
PORTAL = re.compile(r"onetrust|trustarc|privacy-?portal|transcend|securiti|datagrail|ketch|^privacy\.", re.I)
WEBFORM = re.compile(r"\bform\b|webform|formular|modulo|portal|request|richiesta|anfrage|onetrust|trustarc", re.I)
BOT_WALL = re.compile(r"just a moment|verify you are human|are you human|attention required|access denied|captcha", re.I)
# Policies name these as third parties. Their addresses and pages are about them, not about the company we look for.
PLATFORMS = {"google.com", "facebook.com", "meta.com", "microsoft.com", "apple.com", "linkedin.com", "twitter.com", "x.com",
             "youtube.com", "instagram.com", "amazon.com", "adobe.com", "cloudflare.com", "tiktok.com"}
# What to keep from a long page: contact details first, then the sections about rights and who is responsible.
KEY = re.compile(r"contact|e-?mail|rights|request|controller|titolare|dpo\b|data protection officer|responsabile|representative|"
                 r"rappresentante|art(?:icle|\.)? ?27|diritti|richiest|kontakt|verantwortlich|betroffen", re.I)

IDENTIFY = """An email came from the domain {domain}. This is the end of the email:
<<<
{footer}
>>>
Which company sent it (its legal name if given) and what is its main website?
Copy both exactly as written above. Leave a field empty if it is not there."""
IDENTIFY_SCHEMA = {"type": "object", "properties": {"company": {"type": "string"}, "website": {"type": "string"}},
                   "required": ["company", "website"]}

PROMPT = """You help a person in the EU send a GDPR access request to {company}.
Below is text from {url} and numbered links from that page.
- If the text gives an email address for privacy or data requests that applies to people in the EU (a data protection
  officer, a privacy team, an EU representative under Article 27, or a contact named for privacy questions), put it in
  "address" and copy the sentence with it word for word in "quote".
- If it points to a web form or privacy portal for data requests, put that link's number in "webform_link".
- Otherwise put in "next_link" the number of the link most likely to lead to one.
Use 0 for no link. Never guess or build an address. Leave "address" empty for addresses meant for sales, sponsorship,
security reports, press, jobs, authorities or another company.

TEXT:
{text}

LINKS:
{links}"""
SCHEMA = {"type": "object", "properties": {"address": {"type": "string"}, "quote": {"type": "string"},
                                           "webform_link": {"type": "integer"}, "next_link": {"type": "integer"}},
          "required": ["address", "quote", "webform_link", "next_link"]}


PURPOSE = """Someone wants to send {company} a GDPR request for a copy of their personal data.
A web page of the company says:
<<<
{around}
>>>
What is the address {address} for, according to this text?
- privacy: privacy or personal data questions and requests of {company} (privacy team, data protection officer, EU representative)
- general: {company}'s general contact, given as the way to reach it about this text
- other: anything else (sales, sponsorship, security reports, legal notices, press, jobs, or a different company)"""
PURPOSE_SCHEMA = {"type": "object", "properties": {"use": {"enum": ["privacy", "general", "other"]}}, "required": ["use"]}

# One bad answer costs a page, not the run. A model that is down is not in here: that must stop the run.
BAD_ANSWER = (httpx.HTTPStatusError, httpx.TimeoutException, ValueError, KeyError)


class Refused(Exception):
    """The site said no (403, a bot wall). We move on; we don't get around it."""


@dataclass
class Found:
    email: str
    url: str
    quote: str  # the line on the page with the address: the evidence
    webform: str = ""


@dataclass
class Lookup:
    status: str  # email, webform, blocked or nothing
    found: Found | None = None
    pages: list[str] = field(default_factory=list)
    how: str = "found"  # the record's quality: "found" by rules, "agent" with the model


def _host(url: str) -> str:
    return (urlsplit(url if "//" in url else "//" + url).hostname or "").lower()


def read_page(url: str, get: Read, browse: Read | None = None) -> str:
    """A plain download first, the browser only when that shows too little. A refusal stays a refusal."""
    html = ""
    try:
        html = get(url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403, 429):
            raise Refused(url) from e
        return ""  # a page that isn't there: no browser will change that
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        pass
    text = policy_text(html)
    hidden = "email-protection" in html  # Cloudflare hides addresses until a browser runs the page
    if (len(text) >= THIN and not (hidden and browse)) or url.endswith(".xml"):
        return html
    if BOT_WALL.search(text):
        raise Refused(url)
    rendered = browse(url) if browse else ""
    text = policy_text(rendered)
    if len(text) < THIN and BOT_WALL.search(text):
        raise Refused(url)
    return rendered or html


def pdf_page(data: bytes) -> str:
    """A PDF policy as a page: some companies publish nothing else."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
            return f"<pre>{escape(chr(10).join(str(p.get_text()) for p in doc))}</pre>"
    except (RuntimeError, ValueError):
        return ""


def sitemap_pages(site: str, read: Read) -> list[str]:
    """Privacy pages from the site's own page list, for when no link leads to them."""
    found: list[str] = []
    todo = [f"https://{site}/sitemap.xml", f"https://www.{site}/sitemap.xml", f"https://{site}/sitemap_index.xml"]
    for n, url in enumerate(todo):  # a sitemap can list further sitemaps, which join this list
        if n >= 6:
            break
        try:
            xml = read(url)
        except Refused:
            continue
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.I):
            if loc.endswith(".xml"):
                todo.append(loc)
            elif PRIVACY_PAGE.search(urlsplit(loc).path) and loc not in found:
                found.append(loc)
    return sorted(found, key=len)[:MAX_SITEMAP]


def _mailable(address: str) -> bool:
    return not PLACEHOLDER.search(address) and not PEC.search(address)


def _hits(text: str, url: str) -> list[Found]:
    text = re.sub(r"\s*[\[(]at[\])]\s*", "@", text, flags=re.I)
    out = []
    for m in EMAIL.finditer(text):
        quote = re.sub(r"<[^>]+>|\s+", " ", text[max(0, m.start() - 120): m.end() + 60]).strip()
        if PRIVACY_BOX.search(m.group().split("@")[0]) and not OTHER_LAW.search(quote) and _mailable(m.group()):
            out.append(Found(m.group().lower().rstrip("."), url, quote))
    return out


def privacy_email(html: str, url: str, domain: str) -> Found | None:
    """A privacy/DPO address on the page; the company's own domain first (a policy also names Google's)."""
    mailtos = [str(a["href"])[7:].split("?")[0] for a in BeautifulSoup(html, "html.parser").find_all("a", href=True)
               if str(a["href"]).lower().startswith("mailto:")]
    # Some sites have it only in page data (beehiiv: JSON-LD "contactType": "privacy").
    hits = _hits(policy_text(html) + "\n" + "\n".join(mailtos), url) or _hits(html, url)
    hits = [h for h in hits if domain in PLATFORMS or registrable(h.email.split("@")[1]) not in PLATFORMS]
    own = domain.split(".")[0]
    hits.sort(key=lambda f: own not in registrable(f.email.split("@")[1]))
    return hits[0] if hits else None


def candidate_links(html: str, base: str, seen: set[str], domains: set[str]) -> list[tuple[str, str]]:
    """(url, text) of the links worth a look, privacy pages first. No same-page anchors or visited pages; only the
    company's own sites, or a privacy portal another site runs for it."""
    links: dict[str, str] = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        try:
            url = urljoin(base, str(a["href"])).split("#")[0]
            host = _host(url)
        except ValueError:  # a broken href
            continue
        text = a.get_text(" ", strip=True)[:80]
        label = f"{url} {text}"
        site = registrable(host)
        allowed = site in domains or (PORTAL.search(host) is not None and site not in PLATFORMS)
        if (url.startswith("http") and url not in seen and url != base.split("#")[0] and url not in links and allowed
                and (PRIVACY_PAGE.search(label) or MORE_PAGES.search(label))):
            links[url] = text
    ranked = sorted(links.items(), key=lambda kv: PRIVACY_PAGE.search(f"{kv[0]} {kv[1]}") is None)
    return ranked[:MAX_LINKS]


def relevant_parts(text: str, limit: int = MAX_TEXT) -> str:
    """The lines of a long page that matter, each with its neighbours, in page order."""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    order = [i for i, ln in enumerate(lines) if "@" in ln] + [i for i, ln in enumerate(lines) if KEY.search(ln)]
    keep: set[int] = set()
    size = 0
    for i in order:
        for j in range(max(0, i - 1), min(len(lines), i + 2)):
            if j not in keep and size + len(lines[j]) < limit:
                keep.add(j)
                size += len(lines[j]) + 1
    return "\n".join(lines[i] for i in sorted(keep)) or text[:limit]


def _pick(n: Any, links: list[tuple[str, str]]) -> str:
    """Links are numbered, so the model can only choose from the list; it can't make one up."""
    return links[n - 1][0] if isinstance(n, int) and 1 <= n <= len(links) else ""


def _proven(address: str, quote: str, text: str, site: str, url: str) -> bool:
    """The evidence rule: the address sits inside a sentence found word for word on the page. A box without a
    privacy word in it counts only on a privacy page: on a homepage or contact page it is just some address."""
    box, domain = address.split("@")[0], registrable(address.split("@")[-1])
    return (EMAIL.fullmatch(address) is not None and address in quote.lower() and verified(quote, text)
            and not OTHER_LAW.search(quote) and _mailable(address) and domain not in FREEMAIL
            and not WRONG_BOX.search(box) and (PRIVACY_BOX.search(box) is not None or PRIVACY_PAGE.search(url) is not None)
            and (site in PLATFORMS or domain not in PLATFORMS))


def _own(address: str, site: str, domains: set[str]) -> bool:
    domain = registrable(address.split("@")[-1])
    return domain in domains or site.split(".")[0] in domain


def _purpose(company: str, address: str, around: str, ask: Ask) -> str:
    """A second, narrow question: finding an address and judging it are different jobs."""
    try:
        return str(ask(PURPOSE.format(company=company, address=address, around=around), PURPOSE_SCHEMA).get("use", "other"))
    except BAD_ANSWER:
        return "other"


def _context(text: str, address: str, fallback: str) -> str:
    i = text.lower().find(address)
    return text[max(0, i - 400): i + 200] if i >= 0 else fallback


def _consult(company: str, url: str, html: str, links: list[tuple[str, str]], site: str, domains: set[str], ask: Ask) -> tuple[Found | None, str]:
    """The model's view of one page: a checked contact, or the link to try next."""
    text = policy_text(html)
    listing = "\n".join(f"{i}. {u} ({t})" for i, (u, t) in enumerate(links, 1)) or "(none)"
    try:
        out = ask(PROMPT.format(company=company, url=url, text=relevant_parts(text), links=listing), SCHEMA)
    except BAD_ANSWER:
        return None, ""
    written, quote = EMAIL.search(str(out.get("address", ""))), str(out.get("quote", ""))
    address = written.group().lower() if written else ""  # the model may add words around it ("Email us: ...")
    # The company's own address on its own privacy page is enough. An address elsewhere may be a third party's.
    if address and _proven(address, quote, text, site, url) and (
            _own(address, site, domains) or _purpose(company, address, _context(text, address, quote), ask) != "other"):
        return Found(address, url, quote), ""
    form, follow = _pick(out.get("webform_link"), links), _pick(out.get("next_link"), links)
    label = f"{form} {dict(links).get(form, '')}"
    # A general contact form is not a way to make a data request.
    if form and WEBFORM.search(label) and (PRIVACY_PAGE.search(label) or PORTAL.search(_host(form))):
        return Found("", url, dict(links)[form] or form, webform=form), ""
    return None, follow or form


def walk(site: str, company: str, read: Read, ask: Ask | None, also: set[str] | None = None) -> Lookup:
    """Rules over the whole walk first, so the model can only add to what they find; then the walk again with
    the model, on the pages already read. `also`: the company's other domains."""
    pages: dict[str, str | None] = {}

    def once(url: str) -> str:
        if url not in pages:
            try:
                pages[url] = read(url)
            except Refused:
                pages[url] = None
        html = pages[url]
        if html is None:
            raise Refused(url)
        return html

    domains = {site, *(also or set())}
    rules = _walk(site, company, once, None, ask, domains)
    if rules.found or ask is None or rules.status == "blocked":
        return rules
    return _walk(site, company, once, ask, ask, domains)


def _walk(site: str, company: str, read: Read, ask: Ask | None, judge: Ask | None, domains: set[str]) -> Lookup:
    """Read the company's site until a contact turns up or the pages run out. `ask` steers, `judge` checks
    rule hits at other domains: a policy also lists third parties (Marketo on Amazon's page)."""
    home = f"https://{site}/"
    # Some sites only work with www. (real-solution.de: a bad certificate without it).
    queue, guesses = [home], [f"https://www.{site}/", *(urljoin(home, p) for p in GUESSES)]
    mapped = False
    seen: set[str] = set()
    pages: list[str] = []
    refused = False
    for _ in range(MAX_TRIES):
        if not queue and not mapped:
            mapped = True
            queue = sitemap_pages(site, read)
        queue = queue or guesses
        if not queue or len(pages) >= MAX_PAGES:
            break
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if PORTAL.search(_host(url)) and WEBFORM.search(url):  # a privacy portal's request form: that is the contact
            return Lookup("webform", Found("", url, url, webform=url), [*pages, url])
        try:
            html = read(url)
        except Refused:
            refused = True
            continue
        if not html:
            continue
        pages.append(url)
        found = privacy_email(html, url, site)
        if found and (_own(found.email, site, domains) or judge is None
                      or _purpose(company, found.email, _context(policy_text(html), found.email, found.quote), judge) != "other"):
            return Lookup("email", found, pages)
        links = candidate_links(html, url, seen, domains)
        follow = ""
        if ask is not None:
            found, follow = _consult(company, url, html, links, site, domains, ask)
            if found:
                return Lookup("webform" if found.webform else "email", found, pages, "agent")
        ranked = [u for u, _ in links if u != follow and u not in queue]
        queue = ([follow] if follow else []) + ranked + queue
    return Lookup("blocked" if refused and not pages else "nothing", pages=pages)


def identify(domain: str, body: str, ask: Ask, me: str = "") -> tuple[str, str]:
    """(company, website) from the end of an email, checked word for word; the sender domain when that fails.
    `me`: the user's name and address, which must never come back as the company."""
    footer = body[-FOOTER:]
    try:
        out = ask(IDENTIFY.format(domain=domain, footer=footer), IDENTIFY_SCHEMA)
    except BAD_ANSWER:
        return domain, domain
    company, website = str(out.get("company", "")).strip(), str(out.get("website", "")).strip()
    low = footer.lower()
    host = registrable(_host(website)) if website and website.lower() in low else ""
    named = company and company.lower() in low and company.lower() not in me.lower()
    return company if named else domain, host if host and host not in PLATFORMS else domain
