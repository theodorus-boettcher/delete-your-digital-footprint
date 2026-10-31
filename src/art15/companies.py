"""datenanfragen company records (`companies/*.json`, CC0), indexed by domain and by service name."""

import json
import re
from collections.abc import Collection
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tldextract

Record = dict[str, Any]

QUALITY = {"verified": 0, "tested": 1, "imported": 2, "scraped": 3}

# Personal mail comes from these; a match would be a friend, not a company you have an account with.
FREEMAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.de", "gmx.net", "web.de",
    "t-online.de", "libero.it", "virgilio.it", "alice.it", "tiscali.it", "orange.fr", "free.fr",
}


@cache
def _extract() -> tldextract.TLDExtract:
    # The suffix list bundled with tldextract: no network.
    return tldextract.TLDExtract(suffix_list_urls=())


def registrable(host: str) -> str:
    """mail.google.com -> google.com, em.shop.co.uk -> shop.co.uk."""
    return _extract()(host.lower().strip(".")).top_domain_under_public_suffix


def _host(value: str) -> str:
    value = value.strip()
    if "@" in value and "/" not in value:
        return value.rsplit("@", 1)[1]
    return urlsplit(value if "//" in value else "//" + value).hostname or ""


def _label(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class Companies:
    def __init__(self, root: Path, country: str = "", own: Path | None = None) -> None:
        """`own`: a folder of records in the same format (found by `art15 find`, or written by hand)."""
        self.country = country.lower()
        self.records: dict[str, Record] = {}
        self.by_domain: dict[str, list[str]] = {}
        self.by_name: dict[str, list[str]] = {}
        own_paths = sorted(own.glob("*.json")) if own else []
        for path in [*sorted((root / "companies").glob("*.json")), *own_paths]:
            rec = json.loads(path.read_text(encoding="utf-8"))
            self.records[rec["slug"]] = rec
            # Only the company's own web domains: `email`, `webform` and `sources` often sit at third parties
            # (onetrust.com, zendesk.com, a gmail address, github.com).
            hosts = [rec.get("web", "")] + [r for r in rec.get("runs", []) if re.fullmatch(r"[\w.-]+\.[a-z]{2,}", r)]
            doms = {registrable(_host(h)) for h in hosts if h} - {""}
            for dom in doms:
                self.by_domain.setdefault(dom, []).append(rec["slug"])
            # The domain names too, so paypal.it finds the record that lists paypal.com.
            for name in {*doms, rec["name"], *rec.get("runs", [])}:
                name = _extract()(name).domain if name in doms else name
                if len(_label(name)) >= 4:
                    self.by_name.setdefault(_label(name), []).append(rec["slug"])

    def _best(self, slugs: list[str]) -> str:
        def rank(slug: str) -> tuple[int, int]:
            rec = self.records[slug]
            countries = rec.get("relevant-countries", ["all"])
            local = 0 if self.country in countries else 1 if "all" in countries else 2
            return local, QUALITY.get(rec.get("quality", ""), 4)

        return min(slugs, key=rank)

    def _foreign(self, slug: str) -> bool:
        """A record only for other countries (Burger King Deutschland for a user in Italy): often another company."""
        countries = self.records[slug].get("relevant-countries", ["all"])
        return self.country not in countries and "all" not in countries

    def match(self, sender_domain: str, known: Collection[str] = ()) -> str | None:
        """The company record for a sender's domain: by registrable domain, else by the domain's
        name against `runs` (youtube.com -> google, which runs YouTube). A name match prefers `known`,
        the slugs this mailbox already reached by domain (vinted.it -> the vinted.de record, one request),
        and never picks a record only for other countries."""
        dom = registrable(sender_domain)
        if dom in FREEMAIL:
            return None
        if dom in self.by_domain:
            return self._best(self.by_domain[dom])
        label = _label(_extract()(dom).domain)
        for name in (label, label.removesuffix("mail")):  # facebookmail.com sends Facebook's mail
            slugs = self.by_name.get(name, [])
            if mine := [s for s in slugs if s in known]:
                return self._best(mine)
            if ok := [s for s in slugs if not self._foreign(s)]:
                return self._best(ok)
        return None

    def domains(self, slug: str) -> set[str]:
        return {d for d, slugs in self.by_domain.items() if slug in slugs}
