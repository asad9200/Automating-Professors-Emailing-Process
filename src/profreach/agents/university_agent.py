import logging
import re
from typing import Protocol
from urllib.parse import urlparse

import tldextract
from pydantic import BaseModel

from profreach.db.models import TargetUniversityData
from profreach.tools.search import SearchProvider, SearchProviderError, SearchResult
from profreach.tools.web import PageFetchError, RobotsDisallowed

logger = logging.getLogger(__name__)
_EXTRACT_DOMAIN = tldextract.TLDExtract(suffix_list_urls=())
_STOPWORDS = {"the", "of", "at", "and", "university", "college", "school"}
_DIRECTORY_TERMS = ("faculty", "people", "directory", "professor", "staff")
_ACRONYM_SKIP = {"of", "the", "at", "and", "in", "for"}
# Athletics, alumni, and similar sites often use the university name but list no professors.
_NON_ACADEMIC_RE = re.compile(
    r"\b(athletics?|sports?|varsity|coach(?:es|ing)?|alumni|tickets|bookstore|foundation)\b",
    re.IGNORECASE,
)
_ACADEMIC_SUFFIX_RE = re.compile(r"\.(?:edu|ac\.[a-z]{2}|edu\.[a-z]{2})$")


class UniversityResolutionError(RuntimeError):
    """Raised when an official university faculty directory cannot be resolved."""


class UniversityResolution(BaseModel):
    official_domain: str
    department_url: str
    faculty_directory_url: str


class PageFetcher(Protocol):
    def fetch(self, url: str, official_domain: str) -> str: ...


class UniversityAgent:
    def __init__(
        self,
        search_provider: SearchProvider,
        page_fetcher: PageFetcher | None = None,
        max_checks: int = 4,
    ):
        self.search_provider = search_provider
        self.page_fetcher = page_fetcher
        self.max_checks = max_checks

    def resolve(self, target: TargetUniversityData) -> UniversityResolution:
        logger.info("Starting university resolution for %s", target.university_name)
        try:
            university_results = self.search_provider.search(
                f"{target.university_name} {target.location} official university website",
                max_results=8,
            )
            official_domain = self._select_official_domain(target, university_results)
            scoped_query = f"site:{official_domain} {target.department} faculty directory"
            department_results = self.search_provider.search(scoped_query, max_results=10)
        except SearchProviderError as error:
            raise UniversityResolutionError(str(error)) from error

        scoped_results = [
            result
            for result in department_results
            if self._belongs_to_domain(result.url, official_domain)
        ]
        ranked = self._rank_directories(scoped_results, target.department)
        directory = self._first_working(ranked, official_domain)
        if directory is None:
            raise UniversityResolutionError(
                f"No faculty directory was found on {official_domain}. "
                "Try a more specific department name."
            )
        department = self._select_department(scoped_results, target.department, directory)
        resolution = UniversityResolution(
            official_domain=official_domain,
            department_url=department.url if department else directory.url,
            faculty_directory_url=directory.url,
        )
        logger.info(
            "Resolved official department and faculty directory for %s",
            target.university_name,
        )
        return resolution

    @classmethod
    def _select_official_domain(
        cls,
        target: TargetUniversityData,
        results: list[SearchResult],
    ) -> str:
        target_tokens = cls._identity_tokens(target.university_name)
        acronym = cls._acronym(target.university_name)
        scores: dict[str, int] = {}
        for rank, result in enumerate(results):
            parsed = urlparse(result.url)
            if parsed.scheme != "https" or not parsed.hostname:
                continue
            if cls._is_non_academic(f"{result.title} {parsed.hostname}"):
                continue
            domain = cls._registered_domain(parsed.hostname)
            haystack = cls._normalize(f"{result.title} {result.content} {domain}")
            matched = target_tokens.intersection(set(haystack.split()))
            if not matched:
                continue
            label = domain.split(".", 1)[0]
            academic_domain = cls._is_academic_domain(domain)
            # "ualberta" contains "alberta"; "ubc" is the acronym of the full name.
            name_in_domain = label == acronym or any(
                len(token) > 3 and token in label for token in target_tokens
            )
            explicit_official_title = "official" in result.title.lower()
            if not (academic_domain or name_in_domain or explicit_official_title):
                continue
            score = len(matched) * 10 + (15 if name_in_domain else 0)
            score += (5 if academic_domain else 0) + max(0, 5 - rank)
            # Sum across results so the site that dominates the results wins.
            scores[domain] = scores.get(domain, 0) + score
        if not scores:
            raise UniversityResolutionError(
                f"Could not verify an official domain for {target.university_name}."
            )
        return max(scores.items(), key=lambda item: item[1])[0]

    @staticmethod
    def _is_non_academic(value: str) -> bool:
        return bool(_NON_ACADEMIC_RE.search(value))

    @classmethod
    def _acronym(cls, value: str) -> str:
        words = [word for word in cls._normalize(value).split() if word not in _ACRONYM_SKIP]
        return "".join(word[0] for word in words)

    def _first_working(
        self,
        ranked: list[SearchResult],
        official_domain: str,
    ) -> SearchResult | None:
        """Prefer the best-ranked directory that loads; search results can be stale."""
        if not ranked or self.page_fetcher is None:
            return ranked[0] if ranked else None
        blocked: SearchResult | None = None
        for candidate in ranked[: self.max_checks]:
            try:
                self.page_fetcher.fetch(candidate.url, official_domain)
                return candidate
            except RobotsDisallowed:
                blocked = blocked or candidate
            except PageFetchError as error:
                logger.info("Directory candidate unusable: %s", error)
                if "blocked" in str(error):
                    blocked = blocked or candidate
        # A blocked page is still the right page; the user can import it from a browser.
        return blocked or ranked[0]

    @classmethod
    def _select_directory(
        cls,
        results: list[SearchResult],
        department: str = "",
    ) -> SearchResult | None:
        ranked = cls._rank_directories(results, department)
        return ranked[0] if ranked else None

    @classmethod
    def _rank_directories(
        cls,
        results: list[SearchResult],
        department: str = "",
    ) -> list[SearchResult]:
        department_tokens = cls._identity_tokens(department)
        candidates = [
            result
            for result in results
            if any(term in f"{result.title} {result.url}".lower() for term in _DIRECTORY_TERMS)
            and not cls._is_non_academic(f"{result.title} {result.url}")
        ]
        if not candidates:
            return []

        department_stems = {token[:5] for token in department_tokens}

        def score(result: SearchResult) -> int:
            text = f"{result.title} {result.url}".lower()
            stems = {word[:5] for word in cls._normalize(text).split()}
            # Stems let "computing-science" match a "Computer Science" department.
            return (
                sum(term in text for term in ("faculty", "directory", "people", "professor")) * 2
                + len(department_stems & stems) * 4
                - (2 if "staff" in text and "faculty" not in text else 0)
                - (4 if "contact" in text else 0)
            )

        return sorted(candidates, key=score, reverse=True)

    @classmethod
    def _select_department(
        cls,
        results: list[SearchResult],
        department: str,
        directory: SearchResult,
    ) -> SearchResult | None:
        department_tokens = cls._identity_tokens(department)
        candidates = [result for result in results if result.url != directory.url]
        candidates = [
            result
            for result in candidates
            if department_tokens.intersection(
                set(cls._normalize(f"{result.title} {result.url}").split())
            )
        ]
        return candidates[0] if candidates else None

    @classmethod
    def _registered_domain(cls, hostname: str) -> str:
        extracted = _EXTRACT_DOMAIN(hostname)
        return extracted.top_domain_under_public_suffix or hostname.lower().removeprefix("www.")

    @staticmethod
    def _is_academic_domain(domain: str) -> bool:
        return bool(_ACADEMIC_SUFFIX_RE.search(domain))

    @staticmethod
    def _belongs_to_domain(url: str, official_domain: str) -> bool:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        domain = official_domain.lower().rstrip(".")
        return urlparse(url).scheme == "https" and (host == domain or host.endswith(f".{domain}"))

    @classmethod
    def _identity_tokens(cls, value: str) -> set[str]:
        return {
            token
            for token in cls._normalize(value).split()
            if len(token) > 1 and token not in _STOPWORDS
        }

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
