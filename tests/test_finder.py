import httpx

from art15 import evals, finder

HOME = '<a href="/it/informativa-privacy">Informativa sulla privacy</a><a href="/jobs">Lavora con noi</a>'
POLICY = """<main><p>Titolare: Shop Italia S.r.l., info@shop.it</p>
<p>Usiamo Google Analytics (privacy@google.com).</p>
<p>Puoi contattare il nostro DPO a dpo [at] shop.it in ogni momento.</p></main>"""
PAD = "<p>" + "Testo della pagina. " * 100 + "</p>"


def test_privacy_email() -> None:
    f = finder.privacy_email(POLICY, "https://shop.it/p", "shop.it")
    assert f is not None
    assert f.email == "dpo@shop.it"  # own domain before Google's; info@ is not a privacy address
    assert "DPO" in f.quote
    assert finder.privacy_email('<a href="mailto:privacy@shop.it?subject=x">write us</a>', "u", "shop.it").email == "privacy@shop.it"  # type: ignore[union-attr]
    assert finder.privacy_email("<p>info@shop.it</p>", "u", "shop.it") is None
    assert finder.privacy_email("<p>We use Google: privacy@google.com</p>", "u", "shop.it") is None  # a third party
    assert finder.privacy_email("<p>If you are located in Brazil, write to dpobrazil@shop.it</p>", "u", "shop.it") is None
    ld = '<script type="application/ld+json">{"contactType":"privacy","email":"privacy@shop.it"}</script>'
    assert finder.privacy_email(ld, "u", "shop.it").email == "privacy@shop.it"  # type: ignore[union-attr]


def test_walk_rules_only() -> None:
    pages = {"https://shop.it/": HOME + PAD, "https://shop.it/it/informativa-privacy": POLICY + PAD}

    def get(url: str) -> str:
        if url not in pages:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url), response=httpx.Response(404))
        return pages[url]

    r = finder.walk("shop.it", "Shop", lambda u: finder.read_page(u, get), None)
    assert r.found is not None and (r.status, r.how) == ("email", "found")
    assert (r.found.email, r.found.url) == ("dpo@shop.it", "https://shop.it/it/informativa-privacy")
    assert finder.walk("down.example", "Down", lambda u: finder.read_page(u, get), None).status == "nothing"


def test_finder_outcome() -> None:
    assert evals.finder_outcome("DPO@shop.it", "dpo@shop.it") == "same address"
    assert evals.finder_outcome("dpo@shop.it", "privacy@mail.shop.it") == "same company domain"
    assert evals.finder_outcome("dpo@shop.it", "privacy@google.com") == "different address"
