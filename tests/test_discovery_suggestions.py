import httpx
import pytest

from profreach.ui.discovery import _search_universities


@pytest.fixture(autouse=True)
def clear_suggestion_cache():
    _search_universities.clear()
    yield
    _search_universities.clear()


def test_university_suggestions_are_sorted_and_deduplicated(monkeypatch):
    request = httpx.Request("GET", "https://universities.hipolabs.com/search")
    response = httpx.Response(
        200,
        request=request,
        json=[
            {"name": "Oxford University", "country": "United Kingdom"},
            {"name": "Oxford University", "country": "United Kingdom"},
            {"name": "Oxford College", "country": "United States"},
        ],
    )
    requests = []

    def get(url, *, params, timeout):
        requests.append((url, params, timeout))
        return response

    monkeypatch.setattr("profreach.ui.discovery.httpx.get", get)

    assert _search_universities("Oxford") == [
        ("Oxford College", "United States"),
        ("Oxford University", "United Kingdom"),
    ]
    assert requests == [("https://universities.hipolabs.com/search", {"name": "Oxford"}, 4)]


def test_university_suggestions_fail_soft_when_directory_is_unavailable(monkeypatch):
    def get(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("profreach.ui.discovery.httpx.get", get)

    assert _search_universities("Oxford") is None
