"""Publication lookups from OpenAlex, Semantic Scholar, and optional SerpAPI Google Scholar."""

import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
_RETRY_STATUS = {429, 500, 502, 503, 504}
_NAME_NOISE = {"dr", "prof", "professor", "phd", "jr", "sr", "ii", "iii"}
_AFFILIATION_STOPWORDS = {"the", "of", "at", "and", "in", "university", "college", "school", "inc"}


class ScholarlyAPIError(RuntimeError):
    """Raised when a publication provider cannot return usable data."""


class AuthorCandidate(BaseModel):
    source: str
    author_id: str
    name: str
    affiliations: list[str] = Field(default_factory=list)
    works_count: int = 0
    topics: list[str] = Field(default_factory=list)


class Paper(BaseModel):
    title: str
    year: int | None = None
    venue: str | None = None
    abstract_summary: str | None = None
    url: str | None = None
    doi: str | None = None
    citation_count: int | None = None
    authors: list[str] = Field(default_factory=list)
    source: str


class PublicationSource(Protocol):
    name: str

    def find_authors(self, name: str) -> list[AuthorCandidate]: ...

    def recent_papers(self, author_id: str, limit: int = 25) -> list[Paper]: ...


def get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """GET a JSON document, retrying rate limits and server errors with backoff."""
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = client.get(url, params=params, headers=headers)
            if response.status_code in _RETRY_STATUS:
                raise httpx.HTTPStatusError(
                    f"Retryable status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            return payload
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
            retryable = not isinstance(error, httpx.HTTPStatusError) or (
                error.response.status_code in _RETRY_STATUS
            )
            if not retryable or attempt + 1 >= retries:
                break
            sleep(1.0 * (2**attempt))
    raise ScholarlyAPIError(f"Request to {httpx.URL(url).host} failed") from last_error


def summarize_abstract(abstract: str | None, max_chars: int = 350) -> str | None:
    """Return the opening sentences of an abstract, trimmed to a short excerpt."""
    if not abstract:
        return None
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", abstract)).strip()
    if not text:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = ""
    for sentence in sentences[:2]:
        if summary and len(summary) + len(sentence) + 1 > max_chars:
            break
        summary = f"{summary} {sentence}".strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rsplit(" ", 1)[0] + "..."
    return summary


def reconstruct_inverted_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positions: dict[int, str] = {}
    for word, offsets in index.items():
        for offset in offsets:
            positions[offset] = word
    return " ".join(positions[offset] for offset in sorted(positions)) or None


def name_tokens(name: str) -> list[str]:
    tokens = re.findall(r"[a-z]+", name.lower().replace("-", " "))
    return [token for token in tokens if token not in _NAME_NOISE]


def names_match(expected: str, candidate: str) -> bool:
    """Match on family name plus first initial, tolerating middle names and initials."""
    left, right = name_tokens(expected), name_tokens(candidate)
    if not left or not right:
        return False
    return left[-1] == right[-1] and left[0][0] == right[0][0]


def affiliation_matches(university_name: str, affiliations: list[str]) -> bool:
    target = _affiliation_tokens(university_name)
    if not target:
        return False
    for affiliation in affiliations:
        tokens = _affiliation_tokens(affiliation)
        # A shorter affiliation must still carry two distinctive words, so
        # "Berkeley College" does not match "University of California, Berkeley".
        if tokens and (target <= tokens or (len(tokens) >= 2 and tokens <= target)):
            return True
    return False


def _affiliation_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 1 and token not in _AFFILIATION_STOPWORDS
    }


def _short_id(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


class OpenAlexClient:
    name = "openalex"
    base_url = "https://api.openalex.org"

    def __init__(
        self,
        mailto: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.mailto = mailto
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=20)
        self.sleep = sleep

    def _params(self, extra: dict[str, Any]) -> dict[str, Any]:
        params = dict(extra)
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def find_authors(self, name: str) -> list[AuthorCandidate]:
        payload = get_json(
            self.client,
            f"{self.base_url}/authors",
            params=self._params({"search": name, "per_page": 10}),
            sleep=self.sleep,
        )
        candidates = []
        for item in payload.get("results", []):
            affiliations = [
                institution.get("display_name", "")
                for institution in item.get("last_known_institutions") or []
            ]
            affiliations += [
                (affiliation.get("institution") or {}).get("display_name", "")
                for affiliation in item.get("affiliations") or []
            ]
            candidates.append(
                AuthorCandidate(
                    source=self.name,
                    author_id=_short_id(item.get("id", "")),
                    name=item.get("display_name") or "",
                    affiliations=[value for value in dict.fromkeys(affiliations) if value],
                    works_count=item.get("works_count") or 0,
                    topics=[
                        topic.get("display_name", "")
                        for topic in (item.get("topics") or [])[:6]
                        if topic.get("display_name")
                    ],
                )
            )
        return candidates

    def recent_papers(self, author_id: str, limit: int = 25) -> list[Paper]:
        payload = get_json(
            self.client,
            f"{self.base_url}/works",
            params=self._params(
                {
                    "filter": f"author.id:{author_id}",
                    "sort": "publication_date:desc",
                    "per_page": min(limit, 50),
                }
            ),
            sleep=self.sleep,
        )
        papers = []
        for item in payload.get("results", []):
            title = item.get("display_name") or item.get("title")
            if not title or item.get("type") in {"erratum", "retraction", "peer-review"}:
                continue
            source = ((item.get("primary_location") or {}).get("source") or {}).get("display_name")
            doi = item.get("doi")
            papers.append(
                Paper(
                    title=title,
                    year=item.get("publication_year"),
                    venue=source,
                    abstract_summary=summarize_abstract(
                        reconstruct_inverted_abstract(item.get("abstract_inverted_index"))
                    ),
                    url=doi or item.get("id"),
                    doi=doi.removeprefix("https://doi.org/") if doi else None,
                    citation_count=item.get("cited_by_count"),
                    authors=[
                        (authorship.get("author") or {}).get("display_name", "")
                        for authorship in item.get("authorships") or []
                        if (authorship.get("author") or {}).get("display_name")
                    ],
                    source=self.name,
                )
            )
        return papers


class SemanticScholarClient:
    name = "semantic_scholar"
    base_url = "https://api.semanticscholar.org/graph/v1"

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client or httpx.Client(timeout=20)
        self.headers = {"x-api-key": api_key} if api_key else {}
        self.sleep = sleep

    def find_authors(self, name: str) -> list[AuthorCandidate]:
        payload = get_json(
            self.client,
            f"{self.base_url}/author/search",
            params={"query": name, "fields": "name,affiliations,paperCount", "limit": 10},
            headers=self.headers,
            sleep=self.sleep,
        )
        return [
            AuthorCandidate(
                source=self.name,
                author_id=str(item.get("authorId")),
                name=item.get("name") or "",
                affiliations=item.get("affiliations") or [],
                works_count=item.get("paperCount") or 0,
            )
            for item in payload.get("data", [])
            if item.get("authorId")
        ]

    def recent_papers(self, author_id: str, limit: int = 25) -> list[Paper]:
        payload = get_json(
            self.client,
            f"{self.base_url}/author/{author_id}/papers",
            params={
                "fields": "title,year,venue,abstract,tldr,externalIds,url,citationCount,authors",
                "limit": 100,
            },
            headers=self.headers,
            sleep=self.sleep,
        )
        papers = []
        for item in payload.get("data", []):
            if not item.get("title"):
                continue
            tldr = (item.get("tldr") or {}).get("text")
            doi = (item.get("externalIds") or {}).get("DOI")
            papers.append(
                Paper(
                    title=item["title"],
                    year=item.get("year"),
                    venue=item.get("venue") or None,
                    abstract_summary=summarize_abstract(tldr or item.get("abstract")),
                    url=item.get("url"),
                    doi=doi,
                    citation_count=item.get("citationCount"),
                    authors=[
                        author.get("name", "")
                        for author in item.get("authors") or []
                        if author.get("name")
                    ],
                    source=self.name,
                )
            )
        papers.sort(key=lambda paper: paper.year or 0, reverse=True)
        return papers[:limit]


class SerpApiScholarClient:
    """Optional Google Scholar results; used only to supplement verified authors."""

    name = "google_scholar"
    endpoint = "https://serpapi.com/search.json"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=30)
        self.sleep = sleep

    def search_author_papers(self, name: str, university: str, limit: int = 10) -> list[Paper]:
        payload = get_json(
            self.client,
            self.endpoint,
            params={
                "engine": "google_scholar",
                "q": f'author:"{name}" {university}',
                "num": min(limit, 20),
                "api_key": self.api_key,
            },
            sleep=self.sleep,
        )
        family_name = (name_tokens(name) or [""])[-1]
        papers = []
        for item in payload.get("organic_results", []):
            info = item.get("publication_info") or {}
            summary = info.get("summary") or ""
            authors = [author.get("name", "") for author in info.get("authors") or []]
            if not any(names_match(name, author) for author in authors) and (
                not family_name or family_name not in summary.lower()
            ):
                continue
            year_match = re.search(r"\b(19|20)\d{2}\b", summary)
            venue_match = re.search(r"-\s*([^-]+?),\s*(?:19|20)\d{2}", summary)
            if not item.get("title"):
                continue
            papers.append(
                Paper(
                    title=item["title"],
                    year=int(year_match.group(0)) if year_match else None,
                    venue=venue_match.group(1).strip() if venue_match else None,
                    abstract_summary=summarize_abstract(item.get("snippet")),
                    url=item.get("link"),
                    authors=[author for author in authors if author],
                    source=self.name,
                )
            )
        return papers
