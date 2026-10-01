import logging
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
# 202 is how some firewalls serve a script challenge instead of the requested page.
_REFUSED = {202, 401, 403}


class PageFetchError(RuntimeError):
    """Raised when an official page cannot be fetched safely."""


class RobotsDisallowed(PageFetchError):
    """Raised when robots.txt disallows fetching a page."""


class BlockedByServer(PageFetchError):
    """Raised when the server refuses the crawler's plain HTTP request (401/403)."""


# (url, raw) -> (status, body). raw=True returns the response body, else the rendered DOM.
BrowserLoader = Callable[[str, bool], tuple[int, str]]


def load_in_browser(url: str, raw: bool = False, headless: bool = False) -> tuple[int, str]:
    """Open a page in a regular Chromium window, as a person would, and return its content.

    Used only after a site refuses plain HTTP requests. It does not hide automation or
    solve challenges; if the site still refuses, the page is treated as blocked.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = response.status if response else 0
            # 202 is a firewall check page that the browser's own scripts complete; once it
            # has, loading the address again returns the real page.
            for _ in range(3):
                if status != 202:
                    break
                page.wait_for_timeout(4000)
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                status = response.status if response else 0
            if raw:
                return status, response.text() if response else ""
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            return status, page.content()
        finally:
            browser.close()


class OfficialPageFetcher:
    def __init__(
        self,
        user_agent: str = "Emailering/0.1 (academic faculty discovery)",
        delay_seconds: float = 1.0,
        retries: int = 3,
        client: httpx.Client | None = None,
        playwright_fallback: bool = True,
        browser_loader: BrowserLoader | None = None,
    ):
        self.user_agent = user_agent
        self.delay_seconds = max(0.0, delay_seconds)
        self.retries = max(1, retries)
        self.client = client or httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
        )
        self.playwright_fallback = playwright_fallback
        self.browser_loader = browser_loader
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request_at = 0.0

    def fetch(self, url: str, official_domain: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        domain = official_domain.lower().lstrip(".").rstrip(".")
        if parsed.scheme != "https" or not self._is_official_host(host, domain):
            raise PageFetchError("Only HTTPS pages on the resolved official domain may be fetched.")
        if not self._can_fetch(url):
            raise RobotsDisallowed(
                "The site's robots.txt does not allow automated access to this page."
            )

        try:
            response = self._get_with_retries(url)
        except BlockedByServer:
            if self.browser_loader is None:
                raise
            return self._browser_page(url)
        soup = BeautifulSoup(response.text, "html.parser")
        if self.playwright_fallback and (
            len(soup.get_text(" ", strip=True)) < 120 or self._has_load_more_control(soup)
        ):
            rendered = self._fetch_with_playwright(url)
            if rendered:
                return rendered
        return response.text

    @staticmethod
    def _has_load_more_control(soup: BeautifulSoup) -> bool:
        return any(
            "load more" in element.get_text(" ", strip=True).lower()
            for element in soup.find_all(["button", "a"])
        )

    @staticmethod
    def _is_official_host(host: str, domain: str) -> bool:
        return bool(host and domain and (host == domain or host.endswith(f".{domain}")))

    def _can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            robots_url = urljoin(f"{origin}/", "robots.txt")
            response = self._get_with_retries(robots_url, robots_request=True)
            parser = RobotFileParser()
            parser.set_url(robots_url)
            if response.status_code in _REFUSED and self.browser_loader is not None:
                # Firewalls often refuse plain HTTP clients; read the real rules in a browser.
                status, text = self._browser_call(robots_url, raw=True)
                response = httpx.Response(status, text=text)
            if response.status_code in _REFUSED:
                # A forbidden robots.txt means the site disallows crawling entirely.
                parser.disallow_all = True
            elif response.status_code < 400:
                parser.parse(response.text.splitlines())
            else:
                parser.parse([])
            self._robots[origin] = parser
        return parser.can_fetch(self.user_agent, url)

    def _get_with_retries(self, url: str, robots_request: bool = False) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._polite_delay()
            try:
                response = self.client.get(url, headers={"User-Agent": self.user_agent})
                if robots_request and response.status_code in {*_REFUSED, 404, 410}:
                    return response
                if response.status_code == 202:
                    # Firewalls answer 202 with a script challenge instead of the page.
                    raise BlockedByServer(self._status_message(202))
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as error:
                last_error = error
                status = error.response.status_code
                if status in {401, 403}:
                    raise BlockedByServer(self._status_message(status)) from error
                if status != 429 and status < 500:
                    raise PageFetchError(self._status_message(status)) from error
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (2**attempt))
            except httpx.ConnectError as error:
                # DNS or connection failures rarely fix themselves within a retry window.
                raise PageFetchError(
                    "Could not connect to the site; the address may be outdated."
                ) from error
            except httpx.HTTPError as error:
                last_error = error
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (2**attempt))
        raise PageFetchError("The page could not be reached; retry later.") from last_error

    @staticmethod
    def _status_message(status: int) -> str:
        if status in _REFUSED:
            return f"HTTP {status}: the site blocked automated access to this page."
        if status in {404, 410}:
            return f"HTTP {status}: the page does not exist; check the directory URL."
        return f"HTTP {status}: the page could not be read."

    def _browser_call(self, url: str, raw: bool) -> tuple[int, str]:
        self._polite_delay()
        try:
            return self.browser_loader(url, raw)
        except Exception as error:
            logger.info("Browser load failed for %s", url, exc_info=True)
            raise PageFetchError("The page could not be opened in a browser.") from error

    def _browser_page(self, url: str) -> str:
        logger.info("Opening %s in a browser after the server refused plain HTTP", url)
        status, html = self._browser_call(url, raw=False)
        if status in _REFUSED:
            raise BlockedByServer(
                f"HTTP {status}: the site blocked automated access even in a browser. "
                "Use Import saved page for this site."
            )
        if status >= 400:
            raise PageFetchError(self._status_message(status))
        return html

    def _polite_delay(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def _fetch_with_playwright(self, url: str) -> str | None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(user_agent=self.user_agent)
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                for _ in range(20):
                    load_more = page.get_by_text("Load More People", exact=False)
                    if not load_more.count():
                        break
                    self._polite_delay()
                    load_more.first.click(timeout=3000)
                content = page.content()
                browser.close()
                return content
        except Exception:
            logger.info("JavaScript rendering fallback failed for %s", url, exc_info=True)
            return None
