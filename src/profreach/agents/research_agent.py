import logging
import re
from datetime import UTC, datetime
from typing import Protocol

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from profreach.config import Settings, get_settings
from profreach.db.models import OpenPositions, Professor, ResearchStatus, TargetUniversity
from profreach.tools.scholarly import (
    AuthorCandidate,
    OpenAlexClient,
    Paper,
    PublicationSource,
    ScholarlyAPIError,
    SemanticScholarClient,
    SerpApiScholarClient,
    affiliation_matches,
    names_match,
)
from profreach.tools.web import OfficialPageFetcher, PageFetchError

logger = logging.getLogger(__name__)

_NOT_ACCEPTING_PATTERNS = [
    r"\b(?:am|is|are)?\s*not\s+(?:currently\s+)?(?:accepting|taking|recruiting|hiring)\b[^.]*",
    r"\bno\s+(?:open\s+)?(?:positions|openings|vacancies)\b[^.]*",
]
_ACCEPTING_PATTERNS = [
    r"\b(?:accepting|recruiting|looking\s+for|seeking|hiring)\s+(?:new\s+)?(?:highly\s+)?"
    r"(?:motivated\s+)?(?:ph\.?d\.?|graduate|master'?s|ms|postdoc\w*|undergraduate)?\s*"
    r"(?:students?|researchers?|fellows?|research\s+assistants?)\b",
    r"\bopen\s+(?:ph\.?d\.?\s+|postdoc(?:toral)?\s+|research\s+|graduate\s+)?"
    r"(?:positions?|openings?)\b",
    r"\bprospective\s+(?:ph\.?d\.?\s+|graduate\s+|master'?s\s+)?students\b",
    r"\b(?:ph\.?d\.?|postdoc(?:toral)?|graduate)\s+(?:positions?|openings?)\s+(?:are\s+)?"
    r"available\b",
]
_TITLE_STOPWORDS = {"a", "an", "the", "of", "and", "for", "in", "on", "to", "with", "via"}


class ResearchResult(BaseModel):
    status: ResearchStatus
    themes: list[str] = Field(default_factory=list)
    open_positions: OpenPositions = OpenPositions.UNKNOWN
    open_positions_evidence: str | None = None
    open_positions_url: str | None = None
    lab_page_excerpt: str | None = None
    contact_instructions: str | None = None
    openalex_author_id: str | None = None
    semantic_scholar_author_id: str | None = None
    sources: list[str] = Field(default_factory=list)
    papers: list[Paper] = Field(default_factory=list)
    error: str | None = None


class PageFetcher(Protocol):
    def fetch(self, url: str, official_domain: str) -> str: ...


class ResearchAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        openalex: PublicationSource | None = None,
        semantic_scholar: PublicationSource | None = None,
        serpapi: SerpApiScholarClient | None = None,
        page_fetcher: PageFetcher | None = None,
        now: datetime | None = None,
    ):
        self.settings = settings or get_settings()
        self.openalex = openalex or OpenAlexClient(
            mailto=self.settings.openalex_mailto,
            api_key=self.settings.openalex_api_key,
        )
        self.semantic_scholar = semantic_scholar or SemanticScholarClient(
            api_key=self.settings.semantic_scholar_api_key
        )
        if serpapi is None and self.settings.serpapi_api_key:
            serpapi = SerpApiScholarClient(self.settings.serpapi_api_key)
        self.serpapi = serpapi
        self.page_fetcher = page_fetcher or OfficialPageFetcher(
            user_agent=self.settings.user_agent,
            delay_seconds=self.settings.scrape_delay_seconds,
        )
        self.now = now

    def research(
        self,
        professor: Professor,
        target: TargetUniversity,
        user_interests: list[str],
    ) -> ResearchResult:
        logger.info("Starting research enrichment for professor %s", professor.id)
        errors: list[str] = []
        sources: list[str] = []
        papers: list[Paper] = []
        themes: list[str] = []

        openalex_author = self._verified_author(
            self.openalex, professor.name, target.university_name, errors
        )
        openalex_papers: list[Paper] = []
        if openalex_author:
            openalex_papers = self._papers(self.openalex, openalex_author, errors)
            papers.extend(openalex_papers)
            themes = openalex_author.topics
            sources.append(self.openalex.name)

        s2_author = self._semantic_scholar_author(
            professor.name, target.university_name, openalex_papers, errors
        )
        if s2_author:
            papers.extend(self._papers(self.semantic_scholar, s2_author, errors))
            sources.append(self.semantic_scholar.name)

        if self.serpapi and (openalex_author or s2_author):
            try:
                papers.extend(
                    self.serpapi.search_author_papers(professor.name, target.university_name)
                )
                sources.append(self.serpapi.name)
            except ScholarlyAPIError as error:
                errors.append(f"{self.serpapi.name}: {error}")

        page = self._read_official_pages(professor, target)
        ranked = rank_papers(
            merge_papers(papers),
            user_interests,
            self.settings.research_max_papers,
            (self.now or datetime.now(UTC)).year,
        )
        verified = bool(openalex_author or s2_author)
        providers_failed = len(errors) >= 2 and not verified
        if verified:
            status = ResearchStatus.DONE
        elif providers_failed:
            status = ResearchStatus.FAILED
        else:
            status = ResearchStatus.NO_VERIFIED_AUTHOR
        result = ResearchResult(
            status=status,
            themes=themes or list(professor.research_areas),
            open_positions=page["status"],
            open_positions_evidence=page["evidence"],
            open_positions_url=page["url"],
            lab_page_excerpt=page["excerpt"],
            contact_instructions=page["instructions"],
            openalex_author_id=openalex_author.author_id if openalex_author else None,
            semantic_scholar_author_id=s2_author.author_id if s2_author else None,
            sources=sources,
            papers=ranked,
            error="; ".join(errors) or None,
        )
        logger.info(
            "Completed research enrichment for professor %s: status=%s papers=%s",
            professor.id,
            result.status.value,
            len(result.papers),
        )
        return result

    @staticmethod
    def _verified_author(
        source: PublicationSource,
        name: str,
        university_name: str,
        errors: list[str],
    ) -> AuthorCandidate | None:
        try:
            candidates = source.find_authors(name)
        except ScholarlyAPIError as error:
            errors.append(f"{source.name}: {error}")
            return None
        verified = [
            candidate
            for candidate in candidates
            if names_match(name, candidate.name)
            and affiliation_matches(university_name, candidate.affiliations)
        ]
        return max(verified, key=lambda candidate: candidate.works_count, default=None)

    def _semantic_scholar_author(
        self,
        name: str,
        university_name: str,
        openalex_papers: list[Paper],
        errors: list[str],
    ) -> AuthorCandidate | None:
        try:
            candidates = [
                candidate
                for candidate in self.semantic_scholar.find_authors(name)
                if names_match(name, candidate.name)
            ]
        except ScholarlyAPIError as error:
            errors.append(f"{self.semantic_scholar.name}: {error}")
            return None
        by_affiliation = [
            candidate
            for candidate in candidates
            if affiliation_matches(university_name, candidate.affiliations)
        ]
        if by_affiliation:
            return max(by_affiliation, key=lambda candidate: candidate.works_count)
        if not openalex_papers:
            return None
        # Semantic Scholar often omits affiliations; confirm identity through shared papers.
        known_titles = {title_key(paper.title) for paper in openalex_papers}
        for candidate in sorted(candidates, key=lambda c: c.works_count, reverse=True)[:3]:
            try:
                candidate_papers = self.semantic_scholar.recent_papers(candidate.author_id, 50)
            except ScholarlyAPIError as error:
                errors.append(f"{self.semantic_scholar.name}: {error}")
                return None
            shared = known_titles & {title_key(paper.title) for paper in candidate_papers}
            if len(shared) >= 2:
                return candidate
        return None

    @staticmethod
    def _papers(
        source: PublicationSource,
        author: AuthorCandidate,
        errors: list[str],
    ) -> list[Paper]:
        try:
            return source.recent_papers(author.author_id, 25)
        except ScholarlyAPIError as error:
            errors.append(f"{source.name}: {error}")
            return []

    def _read_official_pages(self, professor: Professor, target: TargetUniversity) -> dict:
        result = {
            "status": OpenPositions.UNKNOWN,
            "evidence": None,
            "url": None,
            "excerpt": None,
            "instructions": None,
        }
        if not target.official_domain:
            return result
        urls = [url for url in dict.fromkeys([professor.lab_url, professor.profile_url]) if url]
        for url in urls:
            try:
                html = self.page_fetcher.fetch(url, target.official_domain)
            except PageFetchError as error:
                logger.info("Could not read page for professor %s: %s", professor.id, error)
                continue
            text = page_text(html)
            if result["excerpt"] is None and text:
                result["excerpt"] = text[:1200]
            if result["instructions"] is None:
                result["instructions"] = find_contact_instructions(text)
            status, evidence = detect_open_positions(text)
            if status is not OpenPositions.UNKNOWN and result["url"] is None:
                result.update(status=status, evidence=evidence, url=url)
        return result


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        element.decompose()
    root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    return re.sub(r"\s+", " ", root.get_text(" ", strip=True)).strip()


def detect_open_positions(text: str) -> tuple[OpenPositions, str | None]:
    """Classify explicit statements about openings; silence stays unknown."""
    for status, patterns in (
        (OpenPositions.NOT_ACCEPTING, _NOT_ACCEPTING_PATTERNS),
        (OpenPositions.MENTIONED, _ACCEPTING_PATTERNS),
    ):
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return status, _sentence_around(text, match.start(), match.end())
    return OpenPositions.UNKNOWN, None


_INSTRUCTION_RE = re.compile(
    r"\b(?:prospective|subject line|do not (?:e-?mail|contact)|don't (?:e-?mail|contact)|"
    r"please (?:apply|include|mention|read)|apply (?:through|via|first)|application portal|"
    r"before (?:e-?mailing|contacting)|unsolicited|in your e-?mail)\b",
    re.IGNORECASE,
)


def find_contact_instructions(text: str, limit: int = 4) -> str | None:
    """Sentences telling prospective students how (or whether) to get in touch."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    found = [sentence.strip()[:300] for sentence in sentences if _INSTRUCTION_RE.search(sentence)]
    return " ".join(dict.fromkeys(found[:limit])) or None


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start))
    right_candidates = [index for index in (text.find(".", end), text.find("!", end)) if index >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    sentence = text[left + 1 : right].strip()
    return sentence[:300]


def title_key(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def merge_papers(papers: list[Paper]) -> list[Paper]:
    """Deduplicate by DOI or normalized title, filling gaps from other providers."""
    merged: list[Paper] = []
    for paper in papers:
        index = next(
            (
                position
                for position, existing in enumerate(merged)
                if title_key(existing.title) == title_key(paper.title)
                or (paper.doi and existing.doi and paper.doi.lower() == existing.doi.lower())
            ),
            None,
        )
        if index is None:
            merged.append(paper)
            continue
        existing = merged[index]
        update = {
            field: value
            for field, value in paper.model_dump().items()
            if field != "source"
            and value not in (None, "", [])
            and getattr(existing, field) in (None, "", [])
        }
        sources = existing.source.split("+")
        if paper.source not in sources:
            update["source"] = "+".join([*sources, paper.source])
        merged[index] = existing.model_copy(update=update)
    return merged


def rank_papers(
    papers: list[Paper],
    interests: list[str],
    limit: int,
    current_year: int,
) -> list[Paper]:
    """Prefer recent papers, breaking ties toward overlap with the applicant's interests."""
    interest_terms = {
        token
        for token in re.findall(r"[a-z0-9]+", " ".join(interests).lower())
        if len(token) > 2 and token not in _TITLE_STOPWORDS
    }

    def score(paper: Paper) -> tuple[float, int, int]:
        text = f"{paper.title} {paper.abstract_summary or ''}".lower()
        overlap = len(interest_terms & set(re.findall(r"[a-z0-9]+", text)))
        age = current_year - paper.year if paper.year else 99
        recency = max(0, 6 - age)
        return (recency + 1.5 * overlap, paper.year or 0, paper.citation_count or 0)

    candidates = [paper for paper in papers if len(paper.title.split()) >= 3]
    return sorted(candidates, key=score, reverse=True)[: max(1, limit)]
