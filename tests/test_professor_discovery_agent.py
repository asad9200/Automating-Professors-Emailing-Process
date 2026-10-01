from pathlib import Path

from profreach.agents.professor_discovery_agent import ProfessorDiscoveryAgent
from profreach.config import Settings
from profreach.db.models import DegreeSeeking, TargetUniversityData
from profreach.tools.search import SearchResult
from profreach.tools.web import PageFetchError

FIXTURE = Path(__file__).parent / "fixtures" / "faculty_directory.html"


class FakePageFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url, official_domain):
        self.calls.append((url, official_domain))
        return self.pages[url]


def make_target():
    return TargetUniversityData(
        university_name="Example University",
        location="Example City",
        department="Computer Science",
        field_of_study="Computer Science",
        research_interests=["robotics", "computer vision"],
        target_degree=DegreeSeeking.PHD,
    )


def test_extracts_faculty_and_never_uses_official_domain_mismatches():
    fetcher = FakePageFetcher(
        {"https://cs.example.edu/people": FIXTURE.read_text(encoding="utf-8")}
    )
    agent = ProfessorDiscoveryAgent(
        page_fetcher=fetcher,
        settings=Settings(scrape_max_pages=1),
    )

    report = agent.discover(make_target(), "example.edu", "https://cs.example.edu/people")

    assert [professor.name for professor in report.professors] == ["Avery Chen", "Mina Shah"]
    avery = report.professors[0]
    assert avery.email == "avery.chen@example.edu"
    assert avery.email_confidence == "found_on_page"
    assert avery.email_source_url == "https://cs.example.edu/people"
    assert avery.profile_url == "https://cs.example.edu/people/avery-chen"
    assert avery.lab_url == "https://robotics.example.edu/lab"
    assert avery.research_areas == ["Robotics", "Computer Vision"]
    assert avery.relevance_score == 100
    assert "computer" in avery.relevance_reason

    mina = report.professors[1]
    assert mina.email == "mina@ml.example.edu"
    assert mina.email_confidence == "decoded"
    assert mina.lab_url is None
    assert fetcher.calls == [("https://cs.example.edu/people", "example.edu")]


def test_reads_profile_page_and_enriches_email_source():
    directory_url = "https://cs.example.edu/faculty"
    profile_url = "https://cs.example.edu/people/sam-lee"
    directory = """<main><article class='person-card'>
            <h2><a href='/people/sam-lee'>Sam Lee</a></h2>
            <p>Assistant Professor</p><p>Research Interests: Robotics</p>
        </article></main>"""
    profile = """<body><article class='person-full'><h1>Sam Lee</h1>
            <p>Assistant Professor</p><p>Research Interests: Robotics</p>
            <a href='mailto:sam.lee@example.edu'>Email</a></article>
            <a href='/people/doctoral-student/jules'>Jules Martin</a></body>"""
    fetcher = FakePageFetcher({directory_url: directory, profile_url: profile})
    agent = ProfessorDiscoveryAgent(
        page_fetcher=fetcher,
        settings=Settings(scrape_max_pages=3),
    )

    report = agent.discover(make_target(), "example.edu", directory_url)

    assert len(report.professors) == 1
    assert report.professors[0].email == "sam.lee@example.edu"
    assert report.professors[0].email_confidence == "found_on_page"
    assert report.professors[0].email_source_url == profile_url
    assert report.professors[0].name == "Sam Lee"
    assert report.pages_visited == 2


def test_follows_pagination_and_keeps_results_when_a_page_fails():
    directory_url = "https://cs.example.edu/faculty"
    profile_url = "https://cs.example.edu/people/lee"
    next_page_url = "https://cs.example.edu/faculty?page=2"
    first_page = """<main><article class='faculty-card'>
      <a href='/people/lee'>Morgan Lee</a><p>Professor</p>
      <p>Research Interests: Robotics</p>
    </article><a rel='next' href='/faculty?page=2'>Next</a></main>"""

    class PaginatedFetcher(FakePageFetcher):
        def fetch(self, url, official_domain):
            self.calls.append((url, official_domain))
            if url == next_page_url:
                raise PageFetchError("temporary failure")
            return self.pages[url]

    profile_page = """<main><h1>Morgan Lee</h1><p>Professor</p>
      <p>Research Interests: Robotics</p><a href='mailto:morgan@example.edu'>Email</a></main>"""
    fetcher = PaginatedFetcher({directory_url: first_page, profile_url: profile_page})
    agent = ProfessorDiscoveryAgent(
        page_fetcher=fetcher,
        settings=Settings(scrape_max_pages=3),
    )

    report = agent.discover(make_target(), "example.edu", directory_url)

    assert [professor.name for professor in report.professors] == ["Morgan Lee"]
    assert report.page_errors == [next_page_url]
    assert report.pages_visited == 3


def test_keeps_table_row_contacts_separate():
    directory_url = "https://cs.example.edu/faculty"
    table = """<main><article><table><tbody>
      <tr><td><a href='/people/alex-rivera'>Alex Rivera</a></td>
          <td>Assistant Professor</td><td>alex@example.edu</td></tr>
      <tr><td><a href='/people/bailey-kim'>Bailey Kim</a></td>
          <td>Associate Professor</td><td>bailey@example.edu</td></tr>
    </tbody></table></article></main>"""
    fetcher = FakePageFetcher({directory_url: table})
    agent = ProfessorDiscoveryAgent(
        page_fetcher=fetcher,
        settings=Settings(scrape_max_pages=1),
    )

    report = agent.discover(make_target(), "example.edu", directory_url)

    assert [(professor.name, professor.email) for professor in report.professors] == [
        ("Alex Rivera", "alex@example.edu"),
        ("Bailey Kim", "bailey@example.edu"),
    ]


def test_follows_official_load_more_link():
    directory_url = "https://cs.example.edu/faculty"
    next_page_url = "https://cs.example.edu/faculty?page=1"
    first_page = """<main><article class='faculty-card'>
            <a href='/people/alex-rivera'>Alex Rivera</a><p>Assistant Professor</p>
            <p>Research Interests: Robotics</p><a href='mailto:alex@example.edu'>Email</a>
            <a href='/lab'>Lab</a>
        </article><a class='button' href='?page=1' title='Load more items'>
            Load More People</a></main>"""
    next_page = """<main><article class='faculty-card'>
            <a href='/people/bailey-kim'>Bailey Kim</a><p>Associate Professor</p>
            <p>Research Interests: Robotics</p><a href='mailto:bailey@example.edu'>Email</a>
            <a href='/lab'>Lab</a>
        </article></main>"""
    fetcher = FakePageFetcher({directory_url: first_page, next_page_url: next_page})
    agent = ProfessorDiscoveryAgent(
        page_fetcher=fetcher,
        settings=Settings(scrape_max_pages=2),
    )

    report = agent.discover(make_target(), "example.edu", directory_url)

    assert [professor.name for professor in report.professors] == ["Alex Rivera", "Bailey Kim"]
    assert next_page_url in [url for url, _ in fetcher.calls]


def test_athletics_staff_directory_yields_no_faculty():
    url = "https://bearsandpandas.ca/staff-directory"
    html = (FIXTURE.parent / "athletics_staff.html").read_text(encoding="utf-8")
    agent = ProfessorDiscoveryAgent(page_fetcher=FakePageFetcher({url: html}), settings=Settings())

    report = agent.discover(make_target(), "bearsandpandas.ca", url)

    assert report.professors == []


def test_extracts_from_saved_pages_without_fetching():
    class NoNetwork:
        def fetch(self, url, official_domain):
            raise AssertionError("saved pages must not be fetched")

    url = "https://cs.example.edu/people"
    html = FIXTURE.read_text(encoding="utf-8")
    agent = ProfessorDiscoveryAgent(page_fetcher=NoNetwork(), settings=Settings())

    report = agent.discover_saved_pages(make_target(), "example.edu", [(url, html), (url, html)])

    assert [professor.name for professor in report.professors] == ["Avery Chen", "Mina Shah"]
    assert report.professors[0].email_source_url == url


def test_reads_sheet_backed_directory_and_follows_official_profiles():
    import httpx

    sheet_id = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFG"
    directory_url = "https://www.example.edu/cs/faculty.html"
    profile_url = "https://apps.example.edu/directory/person/rivera"
    directory = f"""<main><h1>Faculty Members</h1><div id="people">Loading ...</div>
      <script>var peopleSS = "{sheet_id}";
      var u = "https://sheets.googleapis.com/v4/spreadsheets/" + peopleSS + "/values/Export";
      </script></main>"""
    profile = """<main><h1>Dr. Alex Rivera, PhD</h1><p>Associate Professor, Computing Science</p>
      <a href="mailto:arivera@example.edu">arivera@example.edu</a></main>"""
    csv_text = (
        "First Name,Last Name,Research Profile,Research Areas\n"
        f'Alex,Rivera,{profile_url},"Robotics, ML"\n'
        "Sam,Outside,https://elsewhere.org/sam,Theory\n"
    )

    def respond(request):
        assert request.url.host == "docs.google.com"
        return httpx.Response(200, headers={"content-type": "text/csv"}, text=csv_text)

    fetcher = FakePageFetcher({directory_url: directory, profile_url: profile})
    agent = ProfessorDiscoveryAgent(
        page_fetcher=fetcher,
        settings=Settings(scrape_max_pages=5),
        sheet_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    report = agent.discover(make_target(), "example.edu", directory_url)

    by_name = {professor.name: professor for professor in report.professors}
    rivera = by_name["Alex Rivera"]
    assert rivera.title == "Associate Professor"
    assert rivera.email == "arivera@example.edu"
    assert rivera.email_source_url == profile_url
    assert rivera.research_areas == ["Robotics", "ML"]
    assert rivera.relevance_score == 33
    assert by_name["Sam Outside"].profile_url is None
    assert [url for url, _ in fetcher.calls] == [directory_url, profile_url]


def test_searches_target_interests_and_only_crawls_official_results():
    directory_url = "https://cs.example.edu/people"
    professor_url = "https://cs.example.edu/faculty/professors/alex-rivera"
    directory = "<main><h1>Computer Science</h1></main>"
    professor = """<main><h1>Alex Rivera</h1><p>Assistant Professor</p>
        <p>Research Interests: Robotics</p></main>"""

    class Search:
        def __init__(self):
            self.queries = []

        def search(self, query, max_results=8):
            self.queries.append(query)
            return [
                SearchResult(title="Alex Rivera", url=professor_url),
                SearchResult(title="External Professor", url="https://outside.example.org/faculty"),
            ]

    search = Search()
    fetcher = FakePageFetcher({directory_url: directory, professor_url: professor})
    agent = ProfessorDiscoveryAgent(
        page_fetcher=fetcher,
        settings=Settings(scrape_max_pages=5),
        search_provider=search,
    )

    report = agent.discover(make_target(), "example.edu", directory_url)

    assert [professor.name for professor in report.professors] == ["Alex Rivera"]
    assert len(search.queries) == 3
    assert any('"robotics"' in query for query in search.queries)
    assert any('"computer vision"' in query for query in search.queries)
    assert [url for url, _ in fetcher.calls] == [directory_url, professor_url]


def test_extracts_json_ld_and_microdata_people():
    directory_url = "https://cs.example.edu/faculty"
    html = """<main>
            <script type="application/ld+json">
                {"@context":"https://schema.org","@graph":[
                    {"@type":"Person","name":"Riley Park","jobTitle":"Professor",
                     "email":"riley.park@example.edu","url":"/faculty/riley-park",
                     "knowsAbout":["Robotics","Computer Vision"]},
                    {"@type":"Person","name":"Jordan Admin","jobTitle":"Administrator"}
                ]}
            </script>
            <div itemscope itemtype="https://schema.org/Person">
                <span itemprop="name">Sam Lee</span>
                <span itemprop="jobTitle">Associate Professor</span>
                <a itemprop="url" href="/faculty/sam-lee"></a>
                <a itemprop="email" href="mailto:sam.lee@example.edu">Email</a>
                <meta itemprop="knowsAbout" content="Robotics">
            </div>
        </main>"""
    agent = ProfessorDiscoveryAgent(
        page_fetcher=FakePageFetcher({directory_url: html}),
        settings=Settings(scrape_max_pages=1),
    )

    report = agent.discover(make_target(), "example.edu", directory_url)

    by_name = {professor.name: professor for professor in report.professors}
    assert set(by_name) == {"Riley Park", "Sam Lee"}
    riley = by_name["Riley Park"]
    assert riley.title == "Professor"
    assert riley.email == "riley.park@example.edu"
    assert riley.profile_url == "https://cs.example.edu/faculty/riley-park"
    assert riley.research_areas == ["Robotics", "Computer Vision"]
    assert riley.relevance_score == 100
    assert by_name["Sam Lee"].email == "sam.lee@example.edu"


def test_follows_professor_categories_and_confirms_name_only_profiles():
    directory_url = "https://cs.example.edu/about/our-people"
    category_url = "https://cs.example.edu/contacts?group=professors"
    next_category_url = "https://cs.example.edu/contacts?group=professors&page=2"
    first_profile_url = "https://cs.example.edu/about/people/morgan-lee"
    second_profile_url = "https://cs.example.edu/about/people/jamie-kim"
    news_url = "https://cs.example.edu/news?page=2"
    pages = {
        directory_url: f'''<main>
                    <a href="{category_url}">Professors, Tenure Stream</a>
                    <a rel="next" href="{news_url}">Next</a>
                </main>''',
        category_url: f'''<main>
                    <article><a href="{first_profile_url}">Morgan Lee</a></article>
                    <a rel="next" href="{next_category_url}">Next</a>
                </main>''',
        next_category_url: f'''<main>
                    <article><a href="{second_profile_url}">Jamie Kim</a></article>
                </main>''',
        first_profile_url: "<main><h1>Morgan Lee</h1><p>Professor</p></main>",
        second_profile_url: "<main><h1>Jamie Kim</h1><p>Associate Professor</p></main>",
    }
    fetcher = FakePageFetcher(pages)
    agent = ProfessorDiscoveryAgent(
        page_fetcher=fetcher,
        settings=Settings(scrape_max_pages=10),
    )

    report = agent.discover(make_target(), "example.edu", directory_url)

    assert {(person.name, person.title) for person in report.professors} == {
        ("Morgan Lee", "Professor"),
        ("Jamie Kim", "Associate Professor"),
    }
    assert news_url not in [url for url, _ in fetcher.calls]
