import pytest

from profreach.agents.university_agent import UniversityAgent, UniversityResolutionError
from profreach.db.models import DegreeSeeking, TargetUniversityData
from profreach.tools.search import SearchResult
from profreach.tools.web import PageFetchError


class FakeSearchProvider:
    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def search(self, query, max_results=8):
        self.queries.append(query)
        return self.responses.pop(0)


def make_target():
    return TargetUniversityData(
        university_name="University of California, Berkeley",
        location="Berkeley, California, USA",
        department="Computer Science",
        field_of_study="Computer Science",
        research_interests=["robotics"],
        target_degree=DegreeSeeking.PHD,
    )


def test_resolves_official_domain_and_directory_only():
    search = FakeSearchProvider(
        [
            [
                SearchResult(
                    title="Berkeley",
                    url="https://www.berkeley.edu/",
                ),
                SearchResult(
                    title="University of California, Berkeley - Wikipedia",
                    url="https://en.wikipedia.org/wiki/University_of_California,_Berkeley",
                ),
            ],
            [
                SearchResult(
                    title="Berkeley CS Faculty Directory",
                    url="https://www2.eecs.berkeley.edu/Faculty/Lists/CS/faculty.html",
                ),
                SearchResult(
                    title="Computer Science Department",
                    url="https://eecs.berkeley.edu/cs/",
                ),
                SearchResult(
                    title="Faculty directory mirror",
                    url="https://example.org/faculty/directory",
                ),
            ],
        ]
    )

    result = UniversityAgent(search).resolve(make_target())

    assert result.official_domain == "berkeley.edu"
    assert result.faculty_directory_url == (
        "https://www2.eecs.berkeley.edu/Faculty/Lists/CS/faculty.html"
    )
    assert result.department_url == "https://eecs.berkeley.edu/cs/"
    assert search.queries[1].startswith("site:berkeley.edu ")


def test_rejects_search_results_without_official_university_match():
    search = FakeSearchProvider(
        [[SearchResult(title="Wikipedia", url="https://en.wikipedia.org/wiki/Example")]]
    )

    with pytest.raises(UniversityResolutionError, match="Could not verify an official domain"):
        UniversityAgent(search).resolve(make_target())


def alberta_target():
    return TargetUniversityData(
        university_name="University of Alberta",
        location="Edmonton, Alberta, Canada",
        department="Computer Science",
        field_of_study="Computer Science",
        research_interests=["machine learning"],
        target_degree=DegreeSeeking.PHD,
    )


def test_prefers_university_domain_over_official_athletics_site():
    search = FakeSearchProvider(
        [
            [
                SearchResult(
                    title="University of Alberta - Wikipedia",
                    url="https://en.wikipedia.org/wiki/UAlberta",
                ),
                SearchResult(
                    title="University of Alberta", url="https://www.ualberta.ca/en/index.html"
                ),
                SearchResult(
                    title="University of Alberta - Official Athletics Website",
                    url="https://bearsandpandas.ca/",
                ),
                SearchResult(title="University of Alberta", url="https://calendar.ualberta.ca/"),
            ],
            [
                SearchResult(
                    title="Faculty directory | Faculty of Science",
                    url="https://www.ualberta.ca/en/science/about-us/contact-us/faculty-directory/index.html",
                ),
                SearchResult(
                    title="Faculty | Computing Science",
                    url="https://www.ualberta.ca/en/computing-science/faculty-and-staff/faculty.html",
                ),
            ],
        ]
    )

    result = UniversityAgent(search).resolve(alberta_target())

    assert result.official_domain == "ualberta.ca"
    assert result.faculty_directory_url.endswith(
        "/computing-science/faculty-and-staff/faculty.html"
    )


def test_accepts_acronym_domains():
    target = make_target().model_copy(update={"university_name": "University of British Columbia"})
    results = [SearchResult(title="The University of British Columbia", url="https://www.ubc.ca/")]

    assert UniversityAgent._select_official_domain(target, results) == "ubc.ca"


def test_skips_stale_directory_links_and_keeps_blocked_pages():
    class Fetcher:
        def __init__(self, failures):
            self.failures = failures

        def fetch(self, url, official_domain):
            if url in self.failures:
                raise PageFetchError(self.failures[url])
            return "<main>ok</main>"

    stale = "https://old.cs.example.edu/faculty-directory"
    good = "https://cs.example.edu/people/faculty"
    results = [
        SearchResult(title="Computer Science Faculty Directory", url=stale),
        SearchResult(title="Faculty", url=good),
    ]
    ranked = UniversityAgent._rank_directories(results, "Computer Science")

    live = UniversityAgent(None, Fetcher({stale: "Could not connect"}))
    assert live._first_working(ranked, "example.edu").url == good

    blocked = UniversityAgent(
        None, Fetcher({stale: "Could not connect", good: "HTTP 403: the site blocked it"})
    )
    assert blocked._first_working(ranked, "example.edu").url == good
