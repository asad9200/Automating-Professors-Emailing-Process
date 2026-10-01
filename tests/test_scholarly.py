import httpx
import pytest

from profreach.tools.scholarly import (
    OpenAlexClient,
    ScholarlyAPIError,
    SemanticScholarClient,
    SerpApiScholarClient,
    affiliation_matches,
    get_json,
    names_match,
    reconstruct_inverted_abstract,
    summarize_abstract,
)


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openalex_parses_authors_and_reconstructs_abstracts():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/authors":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "https://openalex.org/A123",
                            "display_name": "Avery Chen",
                            "works_count": 42,
                            "last_known_institutions": [{"display_name": "Example University"}],
                            "topics": [{"display_name": "Robot Learning"}],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": "Learning Dexterous Grasps",
                        "publication_year": 2025,
                        "type": "article",
                        "doi": "https://doi.org/10.1/abc",
                        "cited_by_count": 3,
                        "primary_location": {"source": {"display_name": "CoRL"}},
                        "abstract_inverted_index": {"We": [0], "grasp": [1], "objects.": [2]},
                    },
                    {"id": "https://openalex.org/W2", "display_name": "Erratum", "type": "erratum"},
                ]
            },
        )

    openalex = OpenAlexClient(mailto="lab@example.org", client=client_for(respond))

    author = openalex.find_authors("Avery Chen")[0]
    papers = openalex.recent_papers(author.author_id)

    assert author.author_id == "A123"
    assert author.affiliations == ["Example University"]
    assert author.topics == ["Robot Learning"]
    assert requests[0].url.params["mailto"] == "lab@example.org"
    assert requests[1].url.params["filter"] == "author.id:A123"
    assert len(papers) == 1
    assert papers[0].abstract_summary == "We grasp objects."
    assert papers[0].doi == "10.1/abc"
    assert papers[0].venue == "CoRL"


def test_semantic_scholar_prefers_tldr_and_sends_api_key():
    seen_headers = []

    def respond(request):
        seen_headers.append(request.headers.get("x-api-key"))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"title": "Old Paper About Robots", "year": 2018, "abstract": "Old."},
                    {
                        "title": "New Paper About Robots",
                        "year": 2025,
                        "venue": "ICRA",
                        "abstract": "Long abstract.",
                        "tldr": {"text": "A short summary."},
                        "externalIds": {"DOI": "10.2/xyz"},
                    },
                ]
            },
        )

    papers = SemanticScholarClient(api_key="s2-key", client=client_for(respond)).recent_papers("9")

    assert [paper.year for paper in papers] == [2025, 2018]
    assert papers[0].abstract_summary == "A short summary."
    assert seen_headers == ["s2-key"]


def test_get_json_retries_rate_limits_then_succeeds():
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    sleeps = []
    payload = get_json(client_for(respond), "https://api.example.org/x", sleep=sleeps.append)

    assert payload == {"ok": True}
    assert sleeps == [1.0]


def test_get_json_does_not_retry_client_errors():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(404)

    with pytest.raises(ScholarlyAPIError):
        get_json(client_for(respond), "https://api.example.org/x", sleep=lambda _: None)
    assert len(calls) == 1


def test_serpapi_keeps_only_results_by_the_named_author():
    def respond(request):
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "Tactile Sensing for Manipulation",
                        "link": "https://example.org/p1",
                        "publication_info": {"summary": "A Chen, B Diaz - Science Robotics, 2024"},
                    },
                    {
                        "title": "Unrelated Work",
                        "publication_info": {"summary": "Z Smith - Nature, 2023"},
                    },
                ]
            },
        )

    papers = SerpApiScholarClient("key", client=client_for(respond)).search_author_papers(
        "Avery Chen", "Example University"
    )

    assert [paper.title for paper in papers] == ["Tactile Sensing for Manipulation"]
    assert papers[0].year == 2024
    assert papers[0].venue == "Science Robotics"


@pytest.mark.parametrize(
    ("expected", "candidate", "result"),
    [
        ("Avery Chen", "Avery J. Chen", True),
        ("Dr. Avery Chen", "A. Chen", True),
        ("Avery Chen", "Blake Chen", False),
        ("Avery Chen", "Avery Chang", False),
    ],
)
def test_names_match(expected, candidate, result):
    assert names_match(expected, candidate) is result


def test_affiliation_matching_requires_the_institution_identity():
    assert affiliation_matches(
        "University of California, Berkeley", ["UC Berkeley", "University of California Berkeley"]
    )
    assert not affiliation_matches(
        "University of California, Berkeley", ["University of California, Davis"]
    )
    assert not affiliation_matches("Example University", [])
    assert not affiliation_matches("University of California, Berkeley", ["Berkeley College"])
    assert affiliation_matches("Carnegie Mellon University", ["Carnegie Mellon"])


def test_abstract_helpers():
    assert reconstruct_inverted_abstract({"b": [1], "a": [0]}) == "a b"
    assert reconstruct_inverted_abstract(None) is None
    summary = summarize_abstract("First sentence. Second sentence. Third sentence.")
    assert summary == "First sentence. Second sentence."
    assert len(summarize_abstract("word " * 200)) <= 350
