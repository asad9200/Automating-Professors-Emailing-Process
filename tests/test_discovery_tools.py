import httpx
import pytest
from bs4 import BeautifulSoup

from profreach.config import Settings
from profreach.tools.search import (
    DuckDuckGoSearchProvider,
    SearchProviderError,
    TavilySearchProvider,
    make_search_provider,
)
from profreach.tools.web import OfficialPageFetcher, PageFetchError, RobotsDisallowed


def test_tavily_provider_returns_typed_results_and_respects_requested_limit():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={"results": [{"title": "Faculty", "url": "https://example.edu/faculty"}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    results = TavilySearchProvider("test-key", client).search("Example faculty", max_results=3)

    assert results[0].url == "https://example.edu/faculty"
    assert requests[0].read().decode("utf-8") == (
        '{"api_key":"test-key","query":"Example faculty","search_depth":"basic",'
        '"max_results":3,"include_answer":false}'
    )


def test_tavily_requires_a_key():
    with pytest.raises(SearchProviderError, match="TAVILY_API_KEY"):
        TavilySearchProvider(None).search("Example faculty")


def test_fetcher_checks_robots_and_sends_user_agent():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, request=request, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, request=request, text="<main>" + "Faculty " * 40 + "</main>")

    client = httpx.Client(transport=httpx.MockTransport(respond))
    fetcher = OfficialPageFetcher(
        user_agent="Emailering-Test/0.1",
        delay_seconds=0,
        client=client,
        playwright_fallback=False,
    )

    html = fetcher.fetch("https://cs.example.edu/faculty", "example.edu")

    assert "Faculty" in html
    assert requests[0].url.path == "/robots.txt"
    assert requests[1].headers["user-agent"] == "Emailering-Test/0.1"


def test_fetcher_blocks_disallowed_and_nonofficial_pages():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            text="User-agent: *\nDisallow: /private\n",
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    fetcher = OfficialPageFetcher(delay_seconds=0, client=client, playwright_fallback=False)

    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://cs.example.edu/private/faculty", "example.edu")
    with pytest.raises(PageFetchError, match="Only HTTPS"):
        fetcher.fetch("https://example.org/faculty", "example.edu")

    assert len(requests) == 1


def test_detects_javascript_load_more_controls():
    soup = BeautifulSoup("<button>Load More People</button>", "html.parser")

    assert OfficialPageFetcher._has_load_more_control(soup)


def test_duckduckgo_provider_maps_results_and_wraps_errors():
    class Client:
        def text(self, query, max_results):
            assert max_results == 2
            return [{"title": "Berkeley", "href": "https://www.berkeley.edu/", "body": "Home"}]

    results = DuckDuckGoSearchProvider(Client, sleep=lambda _: None).search(
        "Berkeley", max_results=2
    )
    assert results[0].url == "https://www.berkeley.edu/"

    class Broken:
        def text(self, query, max_results):
            raise RuntimeError("rate limited")

    sleeps = []
    with pytest.raises(SearchProviderError, match="Free web search"):
        DuckDuckGoSearchProvider(Broken, sleep=sleeps.append).search("Berkeley")
    assert sleeps == [2.0, 4.0]


def test_search_provider_defaults_to_free_search_without_tavily_key():
    free = make_search_provider(Settings(search_provider="auto", tavily_api_key=None))
    paid = make_search_provider(Settings(search_provider="auto", tavily_api_key="key"))

    assert isinstance(free, DuckDuckGoSearchProvider)
    assert isinstance(paid, TavilySearchProvider)


def test_forbidden_robots_txt_disallows_everything():
    def respond(request):
        return httpx.Response(403, request=request, text="blocked")

    client = httpx.Client(transport=httpx.MockTransport(respond))
    fetcher = OfficialPageFetcher(delay_seconds=0, client=client, playwright_fallback=False)

    with pytest.raises(RobotsDisallowed, match="robots.txt"):
        fetcher.fetch("https://cs.example.edu/faculty", "example.edu")


def test_blocked_page_reports_status_without_retrying():
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(403, request=request, text="blocked")

    client = httpx.Client(transport=httpx.MockTransport(respond))
    fetcher = OfficialPageFetcher(delay_seconds=0, client=client, playwright_fallback=False)

    with pytest.raises(PageFetchError, match="HTTP 403"):
        fetcher.fetch("https://cs.example.edu/faculty", "example.edu")
    assert requests.count("/faculty") == 1


def make_fetcher(responses, browser):
    def respond(request):
        status, text = responses[request.url.path]
        return httpx.Response(status, request=request, text=text)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    return OfficialPageFetcher(
        delay_seconds=0, client=client, playwright_fallback=False, browser_loader=browser
    )


def test_refused_pages_are_opened_in_a_browser():
    calls = []

    def browser(url, raw):
        calls.append((url, raw))
        if url.endswith("robots.txt"):
            return 200, "User-agent: *\nAllow: /\n"
        return 200, "<main>Faculty list</main>"

    fetcher = make_fetcher({"/robots.txt": (403, ""), "/faculty": (202, "challenge")}, browser)

    assert fetcher.fetch("https://cs.example.edu/faculty", "example.edu") == (
        "<main>Faculty list</main>"
    )
    assert calls == [
        ("https://cs.example.edu/robots.txt", True),
        ("https://cs.example.edu/faculty", False),
    ]


def test_robots_rules_read_in_the_browser_are_still_obeyed():
    def browser(url, raw):
        return 200, "User-agent: *\nDisallow: /faculty\n"

    fetcher = make_fetcher({"/robots.txt": (403, "")}, browser)

    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://cs.example.edu/faculty", "example.edu")


def test_pages_still_refused_in_a_browser_are_reported_blocked():
    def browser(url, raw):
        return (200, "") if url.endswith("robots.txt") else (403, "denied")

    fetcher = make_fetcher({"/robots.txt": (404, ""), "/faculty": (403, "")}, browser)

    with pytest.raises(PageFetchError, match="even in a browser"):
        fetcher.fetch("https://cs.example.edu/faculty", "example.edu")
