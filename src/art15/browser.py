"""A headless Chromium for pages that only exist after JavaScript runs.

A fresh profile every run: no logins, no cookies, not the user's own browser. It only opens pages.
"""

from typing import Any, Self

BLOCKED = {"image", "media", "font"}  # not needed to read a page
TIMEOUT = 20_000  # ms


class Browser:
    def __enter__(self) -> Self:
        # Imported here: tests and `--no-browser` need no Chromium.
        from playwright.sync_api import Error, sync_playwright

        self._error = Error
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch()
        except Error as e:
            self._pw.stop()
            raise SystemExit("No browser yet: run `uv run playwright install chromium`, or pass --no-browser.") from e
        self._context = self._browser.new_context(user_agent="Mozilla/5.0 art15")
        self._context.route("**/*", self._filter)
        return self

    def _filter(self, route: Any) -> None:
        if route.request.resource_type in BLOCKED:
            route.abort()
        else:
            route.continue_()

    def html(self, url: str) -> str:
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except self._error:  # sites that never go quiet are still readable
                pass
            return str(page.content())
        except self._error:
            return ""
        finally:
            page.close()

    def __exit__(self, *exc: object) -> None:
        self._browser.close()
        self._pw.stop()
