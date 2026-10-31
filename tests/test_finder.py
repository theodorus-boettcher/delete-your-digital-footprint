import httpx

from art15 import finder

HOME = '<a href="/it/informativa-privacy">Informativa sulla privacy</a><a href="/jobs">Lavora con noi</a>'
POLICY = """<main><p>Titolare: Shop Italia S.r.l., info@shop.it</p>
<p>Usiamo Google Analytics (privacy@google.com).</p>
<p>Puoi contattare il nostro DPO a dpo [at] shop.it in ogni momento.</p></main>"""


def test_privacy_email() -> None:
    f = finder.privacy_email(POLICY, "https://shop.it/p", "shop.it")
    assert f is not None
    assert f.email == "dpo@shop.it"  # own domain before Google's; info@ is not a privacy address
    assert "DPO" in f.quote
    assert finder.privacy_email('<a href="mailto:privacy@shop.it?subject=x">write us</a>', "u", "shop.it").email == "privacy@shop.it"  # type: ignore[union-attr]
    assert finder.privacy_email("<p>info@shop.it</p>", "u", "shop.it") is None
    assert finder.privacy_email("<p>If you are located in Brazil, write to dpobrazil@shop.it</p>", "u", "shop.it") is None
    ld = '<script type="application/ld+json">{"contactType":"privacy","email":"privacy@shop.it"}</script>'
    assert finder.privacy_email(ld, "u", "shop.it").email == "privacy@shop.it"  # type: ignore[union-attr]


def test_find() -> None:
    pages = {"https://shop.it/": HOME, "https://shop.it/it/informativa-privacy": POLICY}

    def get(url: str) -> str:
        if url not in pages:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url), response=httpx.Response(404))
        return pages[url]

    f = finder.find("shop.it", get)
    assert f is not None and (f.email, f.url) == ("dpo@shop.it", "https://shop.it/it/informativa-privacy")
    assert finder.find("down.example", get) is None
