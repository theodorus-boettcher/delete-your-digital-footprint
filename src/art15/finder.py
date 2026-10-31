"""A company's privacy email from its own privacy page, for senders with no datenanfragen record."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from art15.audit import policy_text
from art15.companies import registrable

PRIVACY_PAGE = re.compile(r"privacy|datenschutz|privacidad|confidentialit|informativa|privacybeleid|gdpr|rgpd|dsgvo", re.I)
PRIVACY_BOX = re.compile(r"privacy|dpo|datenschutz|gdpr|rgpd|dsgvo|data.?protection|dataprotect|privacidad|protezione|dsb", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
# An address for another law's users (Hinge: "If you are located in Brazil ... dpobrazil@hinge.co") is not ours.
OTHER_LAW = re.compile(r"brazil|brasil|lgpd|california|ccpa|canad|australia|japan|korea|china|india", re.I)


@dataclass
class Found:
    email: str
    url: str
    quote: str  # the line on the page with the address: the evidence


def policy_links(html: str, base: str) -> list[str]:
    out: list[str] = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(base, str(a["href"]))
        if url.startswith("http") and (PRIVACY_PAGE.search(url) or PRIVACY_PAGE.search(a.get_text(" "))) and url not in out:
            out.append(url)
    return out


def _hits(text: str, url: str) -> list[Found]:
    text = re.sub(r"\s*[\[(]at[\])]\s*", "@", text, flags=re.I)
    out = []
    for m in EMAIL.finditer(text):
        quote = re.sub(r"<[^>]+>|\s+", " ", text[max(0, m.start() - 120): m.end() + 60]).strip()
        if PRIVACY_BOX.search(m.group().split("@")[0]) and not OTHER_LAW.search(quote):
            out.append(Found(m.group().lower().rstrip("."), url, quote))
    return out


def privacy_email(html: str, url: str, domain: str) -> Found | None:
    """A privacy/DPO address on the page; the company's own domain first (a policy also names Google's)."""
    mailtos = [str(a["href"])[7:].split("?")[0] for a in BeautifulSoup(html, "html.parser").find_all("a", href=True)
               if str(a["href"]).lower().startswith("mailto:")]
    # Some sites have it only in page data (beehiiv: JSON-LD "contactType": "privacy").
    hits = _hits(policy_text(html) + "\n" + "\n".join(mailtos), url) or _hits(html, url)
    own = domain.split(".")[0]
    hits.sort(key=lambda f: own not in registrable(f.email.split("@")[1]))
    return hits[0] if hits else None


def find(domain: str, get: Callable[[str], str]) -> Found | None:
    home = f"https://{domain}/"
    try:
        html = get(home)
    except httpx.HTTPError:
        return None
    for url in [*policy_links(html, home)[:4], urljoin(home, "/privacy"), urljoin(home, "/privacy-policy"), home]:
        try:
            page = html if url == home else get(url)
        except httpx.HTTPError:
            continue
        if found := privacy_email(page, url, domain):
            return found
    return None
