from typing import Any

import httpx
import pytest

from art15 import finder
from art15.llm import Ask

PAD = "<p>" + "Some words about how this shop works. " * 60 + "</p>"
HOME = '<a href="/privacy">Privacy</a><a href="/privacy#rights">Your rights</a><a href="/jobs">Jobs</a>' + PAD


def answer(address: str = "", quote: str = "", form: int = 0, follow: int = 0) -> Ask:
    return lambda prompt, schema: {"address": address, "quote": quote, "webform_link": form, "next_link": follow}


def site(pages: dict[str, str]) -> finder.Read:
    return lambda url: pages.get(url, "")


def test_rules_win_without_the_model() -> None:
    def no_model(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("rules should have found it")

    r = finder.walk("shop.it", "Shop", site({"https://shop.it/": "<p>Write to dpo@shop.it</p>"}), no_model)
    assert r.found is not None and (r.found.email, r.how) == ("dpo@shop.it", "found")


def test_rules_get_the_whole_walk_before_the_model() -> None:
    home = '<a href="/privacy">Privacy</a><a href="https://shop.it/privacy-portal/form">Privacy request form</a>' + PAD
    pages = {"https://shop.it/": home, "https://shop.it/privacy": "<p>Write to dpo@shop.it</p>" + PAD}
    r = finder.walk("shop.it", "Shop", site(pages), answer(form=2))  # the model would have stopped on the home page
    assert r.found is not None and (r.found.email, r.how) == ("dpo@shop.it", "found")


def test_pages_are_read_once() -> None:
    reads: list[str] = []

    def read(url: str) -> str:
        reads.append(url)
        return HOME if url == "https://shop.it/" else ""

    finder.walk("shop.it", "Shop", read, answer())
    assert len(reads) == len(set(reads))


def test_model_address_needs_proof() -> None:
    page = "<p>Our EU representative is Prighter, reachable at rep_shop@prighter.com for all requests.</p>" + PAD
    pages = {"https://shop.it/": page}
    good = "Our EU representative is Prighter, reachable at rep_shop@prighter.com for all requests."
    r = finder.walk("shop.it", "Shop", site(pages), answer("rep_shop@prighter.com", good))
    assert r.found is not None and (r.status, r.how, r.found.email) == ("email", "agent", "rep_shop@prighter.com")
    # An invented address, a made-up quote, a quote that doesn't hold the address, another law's address:
    assert finder.walk("shop.it", "Shop", site(pages), answer("dpo@shop.it", "Write to dpo@shop.it please.")).found is None
    assert finder.walk("shop.it", "Shop", site(pages), answer("rep_shop@prighter.com", "Write to rep_shop@prighter.com now.")).found is None
    assert finder.walk("shop.it", "Shop", site(pages), answer("other@shop.it", good)).found is None
    brazil = {"https://shop.it/": "<p>If you are in Brazil, write to help@shop.it for all your requests.</p>" + PAD}
    assert finder.walk("shop.it", "Shop", site(brazil), answer("help@shop.it", "If you are in Brazil, write to help@shop.it")).found is None
    third = {"https://shop.it/": "<p>Google can be reached at contact@google.com for its own services.</p>" + PAD}
    assert finder.walk("shop.it", "Shop", site(third), answer("contact@google.com", "Google can be reached at contact@google.com")).found is None


def test_address_with_words_around_it() -> None:
    line = "If you have a question about your data, please contact hello@shop.it at any time."
    pages = {"https://shop.it/": f"<p>{line}</p>" + PAD}
    r = finder.walk("shop.it", "Shop", site(pages), answer("Email us: hello@shop.it", line))
    assert r.found is not None and r.found.email == "hello@shop.it"


def test_www_when_the_plain_domain_fails() -> None:
    pages = {"https://www.shop.it/": "<p>Write to dpo@shop.it</p>" + PAD}
    r = finder.walk("shop.it", "Shop", site(pages), None)
    assert r.found is not None and r.found.url == "https://www.shop.it/"


def test_model_sees_clean_links_and_cannot_invent_one() -> None:
    prompts: list[str] = []
    visited: list[str] = []

    def ask(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        prompts.append(prompt)
        return {"address": "", "quote": "", "webform_link": 0, "next_link": 99}

    def read(url: str) -> str:
        visited.append(url)
        return HOME if url == "https://shop.it/" else ""

    finder.walk("shop.it", "Shop", read, ask)
    assert "#rights" not in prompts[0] and "/jobs" not in prompts[0] and "https://shop.it/privacy" in prompts[0]
    assert set(visited) <= {"https://shop.it/", "https://www.shop.it/", "https://shop.it/privacy", "https://shop.it/privacy-policy"}  # 99 went nowhere


def test_off_site_links() -> None:
    html = ('<a href="https://policies.google.com/privacy">Google privacy</a>'
            '<a href="https://privacy.microsoft.com/x">Microsoft privacy</a>'
            '<a href="https://privacyportal-de.onetrust.com/webform/abc">Privacy requests</a>'
            '<a href="https://privacy.paramount.com/policy">Privacy</a>'
            '<a href="https://evil.example/privacy">Privacy</a>')
    links = [u for u, _ in finder.candidate_links(html, "https://shop.it/", set(), {"shop.it"})]
    assert links == ["https://privacyportal-de.onetrust.com/webform/abc", "https://privacy.paramount.com/policy"]


def test_webform() -> None:
    home = '<a href="https://privacyportal-de.onetrust.com/webform/abc">Send a data request</a>' + PAD
    r = finder.walk("shop.it", "Shop", site({"https://shop.it/": home}), answer(form=1))
    assert r.found is not None and r.status == "webform" and r.found.webform.endswith("/webform/abc")
    plain = '<a href="https://shop.it/privacy">Privacy</a>' + PAD
    assert finder.walk("shop.it", "Shop", site({"https://shop.it/": plain}), answer(form=1)).status == "nothing"  # not a form
    contact = '<a href="https://shop.it/kontakt/kontaktformular">Kontaktformular</a>' + PAD
    assert finder.walk("shop.it", "Shop", site({"https://shop.it/": contact}), answer(form=1)).status == "nothing"  # not for data requests


def test_walk_stops_at_the_page_limit() -> None:
    chain = {f"https://shop.it/privacy{i}": f'<a href="/privacy{i + 1}">Privacy</a>' + PAD for i in range(20)}
    chain["https://shop.it/"] = '<a href="/privacy0">Privacy</a>' + PAD
    assert len(finder.walk("shop.it", "Shop", site(chain), answer(follow=1)).pages) == finder.MAX_PAGES


def status_error(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(str(code), request=httpx.Request("GET", "http://x"), response=httpx.Response(code))


def test_read_page_uses_the_browser_only_when_needed() -> None:
    def no_browser(url: str) -> str:
        raise AssertionError("the plain download was enough")

    assert finder.read_page("u", lambda u: PAD, no_browser) == PAD
    assert finder.read_page("u", lambda u: "<p>loading</p>", lambda u: PAD) == PAD  # thin page

    def missing(url: str) -> str:
        raise status_error(404)

    assert finder.read_page("u", missing, no_browser) == ""

    def down(url: str) -> str:
        raise httpx.ConnectError("no route")

    assert finder.read_page("u", down, lambda u: PAD) == PAD


def test_blocked_stays_blocked() -> None:
    def refused(url: str) -> str:
        raise status_error(403)

    def no_browser(url: str) -> str:
        raise AssertionError("a refusal must not be retried another way")

    with pytest.raises(finder.Refused):
        finder.read_page("u", refused, no_browser)
    with pytest.raises(finder.Refused):
        finder.read_page("u", lambda u: "<p>Just a moment...</p>", no_browser)

    def read(url: str) -> str:
        raise finder.Refused(url)

    assert finder.walk("shop.it", "Shop", read, None).status == "blocked"


def test_relevant_parts_keeps_contacts_of_a_long_page() -> None:
    lines = [f"Filler line number {i} about nothing much at all." for i in range(2000)]
    lines[1990] = "Our representative: rep@shop.it"
    out = finder.relevant_parts("\n".join(lines))
    assert "rep@shop.it" in out and len(out) <= finder.MAX_TEXT


def test_identify() -> None:
    foot = "Sent to Ada Rossi. Shop Italia S.r.l., Via Roma 1, Milano. www.shop.it | Follow us on facebook.com/shop"

    def says(company: str, website: str) -> Ask:
        return lambda prompt, schema: {"company": company, "website": website}

    assert finder.identify("em1.mailer.net", foot, says("Shop Italia S.r.l.", "www.shop.it"), "Ada Rossi") == ("Shop Italia S.r.l.", "shop.it")
    assert finder.identify("em1.mailer.net", foot, says("Made Up Ltd", "made-up.com")) == ("em1.mailer.net", "em1.mailer.net")  # not in the footer
    assert finder.identify("em1.mailer.net", foot, says("Ada Rossi", "facebook.com/shop"), "Ada Rossi ada@x.org") == ("em1.mailer.net", "em1.mailer.net")
    assert finder.identify("em1.mailer.net", "x" * 5000 + foot, says("Shop Italia S.r.l.", "www.shop.it"))[1] == "shop.it"  # the end of the mail


def test_a_bad_answer_costs_a_page_not_the_run() -> None:
    def broken(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("not JSON")

    assert finder.walk("shop.it", "Shop", site({"https://shop.it/": PAD}), broken).status == "nothing"
    assert finder.identify("mailer.net", "text", broken) == ("mailer.net", "mailer.net")


def test_a_dead_model_stops_the_run() -> None:
    def down(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(httpx.ConnectError):
        finder.walk("shop.it", "Shop", site({"https://shop.it/": PAD}), down)
    with pytest.raises(httpx.ConnectError):
        finder.identify("mailer.net", "text", down)
