import logging
import time
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    url: str
    content: str = ""


class SearchProvider(Protocol):
    def search(self, query: str, max_results: int = 8) -> list[SearchResult]: ...


class SearchProviderError(RuntimeError):
    """Raised when web search cannot return usable results."""


class TavilySearchProvider:
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=20)

    def search(self, query: str, max_results: int = 8) -> list[SearchResult]:
        if not self.api_key:
            raise SearchProviderError(
                "Set TAVILY_API_KEY in .env to discover official university pages."
            )
        try:
            response = self.client.post(
                self.endpoint,
                json={
                    "api_key": self.api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
            return [SearchResult.model_validate(item) for item in payload.get("results", [])]
        except SearchProviderError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise SearchProviderError("Tavily search failed. Check the key and retry.") from error


class DuckDuckGoSearchProvider:
    """Free web search that needs no API key; retries its occasional empty responses."""

    def __init__(self, client_factory=None, attempts: int = 3, sleep=time.sleep):
        self.client_factory = client_factory
        self.attempts = max(1, attempts)
        self.sleep = sleep

    def _client(self):
        if self.client_factory is not None:
            return self.client_factory()
        from ddgs import DDGS

        return DDGS()

    def search(self, query: str, max_results: int = 8) -> list[SearchResult]:
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            try:
                items = self._client().text(query, max_results=max_results) or []
            except Exception as error:
                logger.info("DuckDuckGo search attempt %s failed", attempt + 1, exc_info=True)
                last_error = error
                items = []
            results = [
                SearchResult(
                    title=item.get("title") or "",
                    url=item.get("href") or item.get("url") or "",
                    content=item.get("body") or "",
                )
                for item in items
                if item.get("href") or item.get("url")
            ]
            if results:
                return results
            if attempt + 1 < self.attempts:
                self.sleep(2.0 * (attempt + 1))
        raise SearchProviderError(
            "Free web search returned no results; wait a minute and retry."
        ) from last_error


def make_search_provider(settings) -> SearchProvider:
    choice = settings.search_provider.strip().lower()
    if choice == "tavily" or (choice == "auto" and settings.tavily_api_key):
        return TavilySearchProvider(settings.tavily_api_key)
    return DuckDuckGoSearchProvider()
