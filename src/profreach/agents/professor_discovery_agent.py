import json
import logging
import re
from collections import deque
from functools import partial
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field

from profreach.config import Settings, get_settings
from profreach.db.models import TargetUniversityData
from profreach.tools.embedded_sources import (
    EmbeddedSourceError,
    fetch_sheet_rows,
    find_google_sheets,
    rows_to_people,
)
from profreach.tools.search import SearchProvider, SearchProviderError
from profreach.tools.web import OfficialPageFetcher, PageFetchError, load_in_browser

logger = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TITLE_RE = re.compile(
    r"\b(?:(?:Assistant|Associate|Full|Adjunct|Teaching|Research|Clinical|Distinguished)\s+)*"
    r"(?:Professor|Lecturer|Research Scientist)(?:\s+(?:Emeritus|Emerita|in Residence))?\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"(?:research\s+(?:interests?|areas?|focus|topics?)|areas?\s+of\s+(?:interest|expertise))"
    r"\s*:?\s*(.*?)"
    r"(?=\b(?:education|office hours|teaching schedule|contact|email|lab|laboratory)\s*:?\b|$)",
    re.IGNORECASE,
)
_PROFILE_PATH_TERMS = ("faculty", "people", "directory", "homepages", "profiles", "professor")
_SKIP_LINK_TEXT = {
    "faculty",
    "people",
    "directory",
    "profile",
    "website",
    "homepage",
    "lab",
    "next",
    "previous",
    "load more people",
    "load more",
    "more people",
}
_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "in",
    "of",
    "research",
    "the",
    "with",
}


# Directory listings often abbreviate research areas.
_AREA_ABBREVIATIONS = {
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "rl": "reinforcement learning",
    "nlp": "natural language processing",
    "cv": "computer vision",
    "hci": "human computer interaction",
    "db": "databases",
    "se": "software engineering",
    "pl": "programming languages",
    "ds": "data science",
    "hpc": "high performance computing",
    "iot": "internet of things",
    "ssys": "software systems",
}


class DiscoveredProfessor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str | None = None
    email: str | None = None
    email_source_url: str | None = None
    email_confidence: str = "missing"
    profile_url: str | None = None
    lab_url: str | None = None
    research_areas: list[str] = Field(default_factory=list)
    relevance_score: int = Field(ge=0, le=100)
    relevance_reason: str
    source_url: str


class DiscoveryReport(BaseModel):
    professors: list[DiscoveredProfessor] = Field(default_factory=list)
    pages_visited: int = 0
    page_errors: list[str] = Field(default_factory=list)
    error_reasons: dict[str, str] = Field(default_factory=dict)
    search_errors: list[str] = Field(default_factory=list)


class ProfessorDiscoveryAgent:
    def __init__(
        self,
        page_fetcher: OfficialPageFetcher | None = None,
        settings: Settings | None = None,
        sheet_client: httpx.Client | None = None,
        search_provider: SearchProvider | None = None,
    ):
        self.settings = settings or get_settings()
        browser = (
            partial(_browser_loader, headless=self.settings.browser_headless)
            if self.settings.browser_fallback
            else None
        )
        self.page_fetcher = page_fetcher or OfficialPageFetcher(
            user_agent=self.settings.user_agent,
            delay_seconds=self.settings.scrape_delay_seconds,
            browser_loader=browser,
        )
        self.sheet_client = sheet_client
        self.search_provider = search_provider
        self._sheets_read: set[str] = set()

    def discover(
        self,
        target: TargetUniversityData,
        official_domain: str,
        faculty_directory_url: str,
        follow_links: bool = True,
    ) -> DiscoveryReport:
        logger.info("Starting professor discovery for %s", target.university_name)
        page_limit = max(1, self.settings.scrape_max_pages)
        search_errors: list[str] = []
        seed_urls = [faculty_directory_url]
        if self.search_provider:
            for query in self._search_queries(target, official_domain):
                try:
                    results = self.search_provider.search(query, max_results=10)
                except SearchProviderError as error:
                    logger.warning("Official faculty page search failed: %s", error)
                    search_errors.append(str(error))
                    continue
                seed_urls.extend(
                    result.url
                    for result in results
                    if self._is_discovery_url(result.url, official_domain)
                )
        directory_queue = deque(dict.fromkeys(seed_urls))
        profile_queue: deque[str] = deque()
        queued = set(directory_queue)
        visited: set[str] = set()
        professors: dict[str, DiscoveredProfessor] = {}
        page_errors: list[str] = []
        error_reasons: dict[str, str] = {}

        while (directory_queue or profile_queue) and len(visited) < page_limit:
            queue = directory_queue if directory_queue else profile_queue
            page_url = queue.popleft()
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                html = self.page_fetcher.fetch(page_url, official_domain)
            except PageFetchError as error:
                logger.warning("Could not read faculty page %s: %s", page_url, error)
                page_errors.append(page_url)
                error_reasons[page_url] = str(error)
                continue

            soup = BeautifulSoup(html, "html.parser")
            page_candidates = self._parse_page(soup, page_url, official_domain, target)
            page_candidates += self._embedded_candidates(html, page_url, official_domain, target)
            for candidate in page_candidates:
                key = candidate.profile_url or self._normalize(candidate.name)
                existing = professors.get(key)
                professors[key] = self._merge(existing, candidate)
                if (
                    follow_links
                    and candidate.profile_url
                    and candidate.profile_url not in queued
                    and candidate.profile_url not in visited
                    and self._needs_profile_page(candidate)
                ):
                    profile_queue.append(candidate.profile_url)
                    queued.add(candidate.profile_url)

            if not follow_links:
                continue
            for next_url in self._faculty_directory_links(soup, page_url, official_domain):
                if next_url not in queued and next_url not in visited:
                    directory_queue.appendleft(next_url)
                    queued.add(next_url)
            for next_url in self._pagination_urls(soup, page_url, official_domain):
                if next_url not in queued and next_url not in visited:
                    directory_queue.append(next_url)
                    queued.add(next_url)

        report = DiscoveryReport(
            professors=[
                professor
                for professor in professors.values()
                if self._is_faculty(professor.title, professor.research_areas)
            ],
            pages_visited=len(visited),
            page_errors=page_errors,
            error_reasons=error_reasons,
            search_errors=search_errors,
        )
        logger.info(
            "Completed professor discovery for %s: %s records from %s pages",
            target.university_name,
            len(report.professors),
            report.pages_visited,
        )
        return report

    @staticmethod
    def _belongs_to_domain(url: str, official_domain: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        domain = official_domain.lower().rstrip(".")
        return parsed.scheme == "https" and (host == domain or host.endswith(f".{domain}"))

    @classmethod
    def _is_discovery_url(cls, url: str, official_domain: str) -> bool:
        if not cls._belongs_to_domain(url, official_domain):
            return False
        path = urlparse(url).path.lower()
        if any(term in path for term in ("/news", "/audience", "/events", "/media", "/blog")):
            return False
        return any(term in path for term in (*_PROFILE_PATH_TERMS, "contacts"))

    @classmethod
    def _faculty_directory_links(
        cls,
        soup: BeautifulSoup,
        page_url: str,
        official_domain: str,
    ) -> list[str]:
        results = []
        for anchor in soup.find_all("a", href=True):
            label = " ".join(
                (
                    anchor.get_text(" ", strip=True),
                    anchor.get("title", ""),
                    anchor.get("aria-label", ""),
                )
            ).lower()
            if not re.search(r"\b(professors?|faculty)\b", label):
                continue
            url = urljoin(page_url, anchor["href"])
            path = urlparse(url).path.lower()
            if "contacts" not in path and not any(term in path for term in _PROFILE_PATH_TERMS):
                continue
            if cls._is_discovery_url(url, official_domain):
                results.append(url)
        return list(dict.fromkeys(results))

    @staticmethod
    def _search_queries(target: TargetUniversityData, official_domain: str) -> list[str]:
        terms = [target.department, target.field_of_study, *target.research_interests]
        queries = []
        seen: set[str] = set()
        for term in terms:
            cleaned = term.strip().replace('"', "")
            if not cleaned or cleaned.casefold() in seen:
                continue
            seen.add(cleaned.casefold())
            queries.append(f'site:{official_domain} "{cleaned}" professor faculty')
        return queries

    def _embedded_candidates(
        self,
        html: str,
        page_url: str,
        official_domain: str,
        target: TargetUniversityData,
    ) -> list[DiscoveredProfessor]:
        """People from public sheets that this official page loads with JavaScript."""
        candidates = []
        for source in find_google_sheets(html):
            if source.sheet_id in self._sheets_read:
                continue
            self._sheets_read.add(source.sheet_id)
            try:
                client = self.sheet_client or httpx.Client(
                    headers={"User-Agent": self.settings.user_agent}
                )
                people = rows_to_people(fetch_sheet_rows(source, client))
            except EmbeddedSourceError as error:
                logger.info("Embedded sheet on %s unreadable: %s", page_url, error)
                continue
            logger.info("Read %s people from a sheet embedded in %s", len(people), page_url)
            for person in people:
                profile_url = person.profile_url
                host = (urlparse(profile_url).hostname or "").lower() if profile_url else ""
                if profile_url and not (
                    host == official_domain or host.endswith(f".{official_domain}")
                ):
                    profile_url = None
                # A listed person with an official profile is kept; the profile page must
                # then confirm an academic title (checked when the crawl finishes).
                if not profile_url and not self._is_faculty(person.title, person.research_areas):
                    continue
                score, reason = self._score_relevance(
                    target.research_interests or [target.field_of_study, target.department],
                    person.research_areas,
                )
                candidates.append(
                    DiscoveredProfessor(
                        name=person.name,
                        title=person.title,
                        email=person.email,
                        email_source_url=source.public_url if person.email else None,
                        email_confidence="found_on_page" if person.email else "missing",
                        profile_url=profile_url,
                        research_areas=person.research_areas,
                        relevance_score=score,
                        relevance_reason=reason,
                        source_url=page_url,
                    )
                )
        return candidates

    def discover_saved_pages(
        self,
        target: TargetUniversityData,
        official_domain: str,
        pages: list[tuple[str, str]],
    ) -> DiscoveryReport:
        """Extract professors from pages the user saved in a browser (for blocked sites)."""
        settings = self.settings.model_copy(update={"scrape_max_pages": 1})
        combined: dict[str, DiscoveredProfessor] = {}
        for url, html in pages:
            agent = ProfessorDiscoveryAgent(_SavedPageFetcher({url: html}), settings)
            report = agent.discover(target, official_domain, url, follow_links=False)
            for candidate in report.professors:
                key = candidate.profile_url or self._normalize(candidate.name)
                combined[key] = self._merge(combined.get(key), candidate)
        return DiscoveryReport(professors=list(combined.values()), pages_visited=len(pages))

    @classmethod
    def _parse_page(
        cls,
        soup: BeautifulSoup,
        page_url: str,
        official_domain: str,
        target: TargetUniversityData,
    ) -> list[DiscoveredProfessor]:
        root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
        current_path = urlparse(page_url).path.lower()
        results = {
            candidate.profile_url or cls._normalize(candidate.name): candidate
            for candidate in cls._structured_candidates(soup, page_url, official_domain, target)
        }
        heading = root.find("h1")
        if heading and any(term in current_path for term in _PROFILE_PATH_TERMS):
            heading_text = re.sub(
                r"\s*:\s*faculty home page.*$",
                "",
                heading.get_text(" ", strip=True),
                flags=re.IGNORECASE,
            )
            profile_name = cls._clean_name(heading_text)
            if profile_name:
                profile_container = heading.find_parent("article") or root
                candidate = cls._candidate_from_block(
                    profile_container,
                    profile_name,
                    page_url,
                    page_url,
                    target,
                    official_domain,
                )
                candidate = candidate or cls._provisional_candidate(
                    profile_container,
                    profile_name,
                    page_url,
                    page_url,
                    target,
                )
                if candidate:
                    key = candidate.profile_url or cls._normalize(candidate.name)
                    results[key] = cls._merge(results.get(key), candidate)
                return list(results.values())

        for anchor in root.find_all("a", href=True):
            profile_url = urljoin(page_url, anchor["href"])
            if not cls._is_profile_link(anchor, profile_url, official_domain):
                continue
            name = cls._clean_name(anchor.get_text(" ", strip=True))
            if not name:
                continue
            block = cls._candidate_block(anchor)
            candidate = cls._candidate_from_block(
                block,
                name,
                profile_url,
                page_url,
                target,
                official_domain,
            )
            if candidate is None:
                candidate = cls._provisional_candidate(
                    block,
                    name,
                    profile_url,
                    page_url,
                    target,
                )
            if candidate:
                key = candidate.profile_url or cls._normalize(candidate.name)
                results[key] = cls._merge(results.get(key), candidate)

        if not results and any(term in current_path for term in _PROFILE_PATH_TERMS):
            heading = root.find(["h1", "h2"])
            name = cls._clean_name(heading.get_text(" ", strip=True)) if heading else ""
            if name:
                candidate = cls._candidate_from_block(
                    root,
                    name,
                    page_url,
                    page_url,
                    target,
                    official_domain,
                )
                if candidate:
                    results[candidate.profile_url or cls._normalize(candidate.name)] = candidate
        return list(results.values())

    @classmethod
    def _provisional_candidate(
        cls,
        block: Tag,
        name: str,
        profile_url: str,
        source_url: str,
        target: TargetUniversityData,
    ) -> DiscoveredProfessor:
        text = block.get_text(" ", strip=True)
        areas = cls._research_areas(text)
        email, confidence = cls._extract_email(block)
        score, reason = cls._score_relevance(
            target.research_interests or [target.field_of_study, target.department],
            areas,
        )
        return DiscoveredProfessor(
            name=name,
            email=email,
            email_source_url=source_url if email else None,
            email_confidence=confidence,
            profile_url=profile_url,
            research_areas=areas,
            relevance_score=score,
            relevance_reason=reason,
            source_url=source_url,
        )

    @classmethod
    def _structured_candidates(
        cls,
        soup: BeautifulSoup,
        page_url: str,
        official_domain: str,
        target: TargetUniversityData,
    ) -> list[DiscoveredProfessor]:
        people = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(script.string or script.get_text())
            except (json.JSONDecodeError, TypeError):
                continue
            people.extend(cls._json_objects(payload))

        for scope in soup.select('[itemscope][itemtype*="Person"]'):
            properties: dict[str, list[str]] = {}
            for element in scope.select("[itemprop]"):
                prop = element.get("itemprop")
                if not isinstance(prop, str):
                    continue
                value = (
                    element.get("content")
                    or element.get("href")
                    or element.get("src")
                    or element.get_text(" ", strip=True)
                )
                if value:
                    properties.setdefault(prop, []).append(str(value))
            people.append({"@type": "Person", **properties})

        candidates = []
        for person in people:
            if not isinstance(person, dict):
                continue
            types = cls._structured_values(person.get("@type"))
            is_person = any(
                value.rstrip("/").rsplit("/", 1)[-1].casefold() == "person" for value in types
            )
            if not is_person:
                continue
            names = cls._structured_values(person.get("name"))
            name = cls._clean_name(names[0]) if names else ""
            if not name:
                continue
            titles = cls._structured_values(person.get("jobTitle"))
            title = ", ".join(dict.fromkeys(titles)) or None
            areas = []
            for prop in ("knowsAbout", "researchInterest", "expertise"):
                areas.extend(cls._structured_values(person.get(prop)))
            for description in cls._structured_values(person.get("description")):
                areas.extend(cls._research_areas(description))
            areas = list(dict.fromkeys(area.strip() for area in areas if area.strip()))
            if not cls._is_faculty(title, areas):
                continue

            profile_url = page_url
            for value in cls._structured_values(person.get("url")):
                candidate_url = urljoin(page_url, value)
                if cls._belongs_to_domain(candidate_url, official_domain):
                    profile_url = candidate_url
                    break
            emails = cls._structured_values(person.get("email"))
            email_match = _EMAIL_RE.search(emails[0].removeprefix("mailto:")) if emails else None
            email = email_match.group(0) if email_match else None
            score, reason = cls._score_relevance(
                target.research_interests or [target.field_of_study, target.department],
                areas,
            )
            candidates.append(
                DiscoveredProfessor(
                    name=name,
                    title=title,
                    email=email,
                    email_source_url=page_url if email else None,
                    email_confidence="found_on_page" if email else "missing",
                    profile_url=profile_url,
                    research_areas=areas,
                    relevance_score=score,
                    relevance_reason=reason,
                    source_url=page_url,
                )
            )
        return candidates

    @classmethod
    def _json_objects(cls, value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._json_objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._json_objects(child)

    @classmethod
    def _structured_values(cls, value) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            return [item for child in value for item in cls._structured_values(child)]
        if isinstance(value, dict):
            return [
                item
                for key in ("name", "@value", "description")
                for item in cls._structured_values(value.get(key))
            ]
        return []

    @classmethod
    def _candidate_from_block(
        cls,
        block: Tag,
        name: str,
        profile_url: str,
        source_url: str,
        target: TargetUniversityData,
        official_domain: str,
    ) -> DiscoveredProfessor | None:
        text = block.get_text(" ", strip=True)
        title_match = _TITLE_RE.search(text)
        title = title_match.group(0).strip() if title_match else None
        research_areas = cls._research_areas(text)
        if title and any(word in title.lower() for word in ("lecturer", "scientist")):
            faculty_match = re.search(
                r"(?:Assistant|Associate|Full|Adjunct|Teaching|Research)?\s*Professor",
                text,
                re.I,
            )
            title = faculty_match.group(0).strip() if faculty_match else title
        if not cls._is_faculty(title, research_areas):
            return None

        email, confidence = cls._extract_email(block)
        email_source_url = source_url if email else None
        lab_url = cls._find_lab_url(block, source_url, official_domain)
        score, reason = cls._score_relevance(
            target.research_interests or [target.field_of_study, target.department],
            research_areas,
        )
        return DiscoveredProfessor(
            name=name,
            title=title,
            email=email,
            email_source_url=email_source_url,
            email_confidence=confidence,
            profile_url=profile_url,
            lab_url=lab_url,
            research_areas=research_areas,
            relevance_score=score,
            relevance_reason=reason,
            source_url=source_url,
        )

    @staticmethod
    def _candidate_block(anchor: Tag) -> Tag:
        for parent in anchor.parents:
            if not isinstance(parent, Tag):
                continue
            classes = " ".join(parent.get("class", [])).lower()
            is_heading = re.fullmatch(r"h[1-6]", parent.name or "") is not None
            has_person_container_class = any(
                marker in classes
                for marker in (
                    "faculty",
                    "person",
                    "profile",
                    "directory",
                    "member",
                    "card",
                    "views-row",
                )
            )
            if parent.name in {"article", "li", "tr"} or (
                has_person_container_class and not is_heading
            ):
                return parent
            if parent.name == "main" or parent.get("role") == "main":
                break

        heading = anchor.find_parent(re.compile(r"^h[1-6]$"))
        if heading:
            fragments = [str(heading)]
            for sibling in heading.find_next_siblings():
                if sibling.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    break
                fragments.append(str(sibling))
            return BeautifulSoup("".join(fragments), "html.parser")
        return anchor.parent if isinstance(anchor.parent, Tag) else anchor

    @staticmethod
    def _is_profile_link(anchor: Tag, url: str, official_domain: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        domain = official_domain.lower()
        if parsed.scheme != "https" or not (host == domain or host.endswith(f".{domain}")):
            return False
        if any(parsed.path.lower().endswith(ext) for ext in (".pdf", ".jpg", ".png", ".docx")):
            return False
        text = ProfessorDiscoveryAgent._clean_name(anchor.get_text(" ", strip=True))
        if not text or text.lower() in _SKIP_LINK_TEXT:
            return False
        path = parsed.path.lower()
        if "/staff-directory/" in path:
            return False
        return any(term in path for term in _PROFILE_PATH_TERMS) and len(text.split()) >= 2

    @staticmethod
    def _clean_name(value: str) -> str:
        name = re.sub(r"\s+", " ", value).strip(" \t\r\n:;,-")
        if not 2 <= len(name.split()) <= 7 or len(name) > 90:
            return ""
        if re.search(
            r"\b(search|directory|faculty|department|contact|next|previous)\b",
            name,
            re.I,
        ):
            return ""
        return name

    @staticmethod
    def _is_faculty(title: str | None, research_areas: list[str]) -> bool:
        if title:
            lowered = title.lower()
            return "professor" in lowered or "lecturer" in lowered or "scientist" in lowered
        return bool(research_areas)

    @staticmethod
    def _extract_email(block: Tag) -> tuple[str | None, str]:
        for anchor in block.find_all("a", href=True):
            href = anchor["href"].strip()
            if href.lower().startswith("mailto:"):
                match = _EMAIL_RE.search(href[7:])
                if match and not match.group(0).lower().startswith("no_email@"):
                    return match.group(0), "found_on_page"
        text = block.get_text(" ", strip=True)
        direct_match = _EMAIL_RE.search(text)
        if direct_match and not direct_match.group(0).lower().startswith("no_email@"):
            return direct_match.group(0), "found_on_page"

        decoded = re.sub(r"\s*(?:\[at\]|\(at\)|\{at\})\s*", "@", text, flags=re.I)
        decoded = re.sub(r"\s+at\s+", "@", decoded, flags=re.I)
        decoded = re.sub(r"\s*(?:\[dot\]|\(dot\)|\{dot\})\s*", ".", decoded, flags=re.I)
        decoded = re.sub(r"\s+dot\s+", ".", decoded, flags=re.I)
        match = _EMAIL_RE.search(decoded)
        if match and not match.group(0).lower().startswith("no_email@"):
            return match.group(0), "decoded"
        return None, "missing"

    @staticmethod
    def _research_areas(text: str) -> list[str]:
        match = _RESEARCH_RE.search(text)
        if not match:
            return []
        section = re.split(r"\s*[;|,]\s*", match.group(1))
        return [
            area.strip(" .,:;-")
            for area in section
            if area.strip(" .,:;-") and len(area.strip()) < 100
        ]

    @staticmethod
    def _find_lab_url(block: Tag, source_url: str, official_domain: str) -> str | None:
        for anchor in block.find_all("a", href=True):
            label = anchor.get_text(" ", strip=True).lower()
            if not any(
                word in label for word in ("lab", "laboratory", "research group", "group website")
            ):
                continue
            candidate = urljoin(source_url, anchor["href"])
            host = (urlparse(candidate).hostname or "").lower()
            if host == official_domain or host.endswith(f".{official_domain}"):
                return candidate
        return None

    @classmethod
    def _score_relevance(cls, interests: list[str], areas: list[str]) -> tuple[int, str]:
        interest_terms = cls._tokens(" ".join(interests))
        area_terms = cls._tokens(" ".join(areas))
        shared = sorted(interest_terms.intersection(area_terms))
        if not interest_terms:
            return 0, "No research interests were provided for comparison."
        if not shared:
            return 0, "No explicit keyword overlap; review manually."
        score = min(100, round(100 * len(shared) / len(interest_terms)))
        reason = f"Matches stated research terms: {', '.join(shared[:5])}."
        return score, reason

    @staticmethod
    def _tokens(value: str) -> set[str]:
        words = re.findall(r"[a-z0-9]+", value.lower())
        expanded = " ".join(_AREA_ABBREVIATIONS.get(word, word) for word in words)
        return {
            token
            for token in re.findall(r"[a-z0-9]+", expanded)
            if len(token) > 2 and token not in _STOPWORDS
        }

    @classmethod
    def _pagination_urls(
        cls,
        soup: BeautifulSoup,
        page_url: str,
        official_domain: str,
    ) -> list[str]:
        path = urlparse(page_url).path.lower()
        if any(term in path for term in ("/news", "/audience", "/events", "/media", "/blog")):
            return []
        if not any(term in path for term in (*_PROFILE_PATH_TERMS, "contacts")):
            return []
        results = []
        for anchor in soup.find_all("a", href=True):
            label = anchor.get_text(" ", strip=True).lower()
            rel = " ".join(anchor.get("rel", [])).lower()
            aria = anchor.get("aria-label", "").lower()
            title = anchor.get("title", "").lower()
            next_page = any(
                marker in value
                for marker in ("next", "load more")
                for value in (label, rel, aria, title)
            )
            if not next_page:
                continue
            url = urljoin(page_url, anchor["href"])
            if not cls._is_discovery_url(url, official_domain):
                continue
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme == "https" and (
                host == official_domain or host.endswith(f".{official_domain}")
            ):
                results.append(url)
        return results

    @staticmethod
    def _needs_profile_page(professor: DiscoveredProfessor) -> bool:
        return professor.email is None or not professor.research_areas or professor.lab_url is None

    @staticmethod
    def _merge(
        existing: DiscoveredProfessor | None,
        candidate: DiscoveredProfessor,
    ) -> DiscoveredProfessor:
        if existing is None:
            return candidate
        merged = existing.model_dump()
        for key, value in candidate.model_dump().items():
            current = merged.get(key)
            has_value = value not in (None, [], "", "missing")
            is_empty = current in (None, [], "", "missing")
            if has_value and (is_empty or key == "source_url"):
                merged[key] = value
        if candidate.email and candidate.email_confidence == "found_on_page":
            merged["email"] = candidate.email
            merged["email_confidence"] = candidate.email_confidence
            merged["email_source_url"] = candidate.email_source_url
        if candidate.relevance_score > existing.relevance_score:
            merged["relevance_score"] = candidate.relevance_score
            merged["relevance_reason"] = candidate.relevance_reason
        return DiscoveredProfessor.model_validate(merged)

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


class _SavedPageFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def fetch(self, url: str, official_domain: str) -> str:
        if url not in self.pages:
            raise PageFetchError("Only the imported pages are read.")
        return self.pages[url]


def _browser_loader(url: str, raw: bool, headless: bool) -> tuple[int, str]:
    return load_in_browser(url, raw=raw, headless=headless)
