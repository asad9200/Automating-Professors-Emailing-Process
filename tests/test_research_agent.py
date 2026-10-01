from datetime import UTC, datetime
from pathlib import Path

from profreach.agents.research_agent import (
    ResearchAgent,
    detect_open_positions,
    merge_papers,
    rank_papers,
)
from profreach.config import Settings
from profreach.db.models import OpenPositions, ResearchStatus
from profreach.tools.scholarly import AuthorCandidate, Paper, ScholarlyAPIError
from profreach.tools.web import PageFetchError

LAB_PAGE = (Path(__file__).parent / "fixtures" / "lab_page.html").read_text(encoding="utf-8")


class FakeSource:
    def __init__(self, name, authors=None, papers=None, fail=False):
        self.name = name
        self.authors = authors or []
        self.papers = papers or {}
        self.fail = fail
        self.paper_calls = []

    def find_authors(self, name):
        if self.fail:
            raise ScholarlyAPIError("down")
        return self.authors

    def recent_papers(self, author_id, limit=25):
        self.paper_calls.append(author_id)
        return self.papers.get(author_id, [])


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url, official_domain):
        self.calls.append((url, official_domain))
        if url not in self.pages:
            raise PageFetchError("missing")
        return self.pages[url]


def paper(title, year=2025, source="openalex", **fields):
    return Paper(title=title, year=year, source=source, **fields)


def make_agent(openalex, s2, fetcher=None):
    return ResearchAgent(
        settings=Settings(research_max_papers=5, serpapi_api_key=None),
        openalex=openalex,
        semantic_scholar=s2,
        page_fetcher=fetcher or FakeFetcher({}),
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )


def test_uses_only_authors_verified_by_name_and_affiliation(make_professor, target):
    professor = make_professor()
    openalex = FakeSource(
        "openalex",
        authors=[
            AuthorCandidate(
                source="openalex",
                author_id="WRONG",
                name="Avery Chen",
                affiliations=["Other Institute of Technology"],
                works_count=900,
            ),
            AuthorCandidate(
                source="openalex",
                author_id="A1",
                name="Avery Chen",
                affiliations=["Example University"],
                works_count=40,
                topics=["Robot Learning", "Tactile Sensing"],
            ),
        ],
        papers={"A1": [paper("Learning Dexterous Robot Grasps", abstract_summary="Grasping.")]},
    )
    s2 = FakeSource("semantic_scholar")
    fetcher = FakeFetcher({"https://robotics.example.edu/lab": LAB_PAGE})

    result = make_agent(openalex, s2, fetcher).research(professor, target, ["robot learning"])

    assert result.status is ResearchStatus.DONE
    assert openalex.paper_calls == ["A1"]
    assert result.openalex_author_id == "A1"
    assert result.themes == ["Robot Learning", "Tactile Sensing"]
    assert [item.title for item in result.papers] == ["Learning Dexterous Robot Grasps"]
    assert result.open_positions is OpenPositions.MENTIONED
    assert "recruiting motivated PhD students" in result.open_positions_evidence
    assert result.open_positions_url == "https://robotics.example.edu/lab"
    assert "tracking" not in result.lab_page_excerpt
    assert fetcher.calls[0] == ("https://robotics.example.edu/lab", "example.edu")


def test_same_name_elsewhere_yields_no_papers(make_professor, target):
    professor = make_professor()
    impostor = AuthorCandidate(
        source="openalex", author_id="X", name="Avery Chen", affiliations=["Elsewhere College"]
    )
    openalex = FakeSource(
        "openalex", authors=[impostor], papers={"X": [paper("Not Theirs At All")]}
    )
    s2 = FakeSource(
        "semantic_scholar",
        authors=[AuthorCandidate(source="semantic_scholar", author_id="S", name="Avery Chen")],
        papers={"S": [paper("Also Not Theirs", source="semantic_scholar")]},
    )

    result = make_agent(openalex, s2).research(professor, target, [])

    assert result.status is ResearchStatus.NO_VERIFIED_AUTHOR
    assert result.papers == []
    assert result.themes == ["Robotics"]


def test_semantic_scholar_is_confirmed_through_shared_papers(make_professor, target):
    professor = make_professor()
    shared = [paper("Tactile Sensing for Manipulation"), paper("Learning Dexterous Robot Grasps")]
    openalex = FakeSource(
        "openalex",
        authors=[
            AuthorCandidate(
                source="openalex",
                author_id="A1",
                name="Avery Chen",
                affiliations=["Example University"],
            )
        ],
        papers={"A1": shared},
    )
    s2_papers = [
        paper("Tactile sensing for manipulation", source="semantic_scholar", venue="RSS"),
        paper("Learning dexterous robot grasps", source="semantic_scholar"),
        paper("Sim to Real Transfer for Hands", source="semantic_scholar", year=2026),
    ]
    s2 = FakeSource(
        "semantic_scholar",
        authors=[AuthorCandidate(source="semantic_scholar", author_id="S1", name="A. Chen")],
        papers={"S1": s2_papers},
    )

    result = make_agent(openalex, s2).research(professor, target, [])

    assert result.semantic_scholar_author_id == "S1"
    titles = [item.title for item in result.papers]
    assert len(titles) == 3
    merged = next(
        item for item in result.papers if item.title == "Tactile Sensing for Manipulation"
    )
    assert merged.venue == "RSS"
    assert merged.source == "openalex+semantic_scholar"


def test_provider_outages_mark_research_failed_without_raising(make_professor, target):
    professor = make_professor()
    result = make_agent(
        FakeSource("openalex", fail=True), FakeSource("semantic_scholar", fail=True)
    ).research(professor, target, [])

    assert result.status is ResearchStatus.FAILED
    assert "openalex" in result.error and "semantic_scholar" in result.error


def test_open_position_detection():
    assert detect_open_positions("I am not accepting new students this year.")[0] is (
        OpenPositions.NOT_ACCEPTING
    )
    assert detect_open_positions("Prospective students should read this page.")[0] is (
        OpenPositions.MENTIONED
    )
    assert detect_open_positions("We study robots.") == (OpenPositions.UNKNOWN, None)


def test_rank_prefers_recent_and_relevant_papers():
    papers = [
        paper("Classic Robot Planning Work", year=2012),
        paper("Recent Vision Transformers Study", year=2026),
        paper("Recent Robot Learning Benchmark", year=2026),
        paper("Short", year=2026),
    ]

    ranked = rank_papers(merge_papers(papers), ["robot learning"], 2, 2026)

    assert [item.title for item in ranked] == [
        "Recent Robot Learning Benchmark",
        "Recent Vision Transformers Study",
    ]


def test_merge_deduplicates_by_doi():
    merged = merge_papers(
        [paper("A Title Here", doi="10.1/X"), paper("A title here (extended)", doi="10.1/x")]
    )

    assert len(merged) == 1
