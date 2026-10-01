import httpx
import pytest

from profreach.tools.embedded_sources import (
    EmbeddedSourceError,
    SheetSource,
    fetch_sheet_rows,
    find_google_sheets,
    rows_to_people,
)

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFG"


def test_finds_sheet_referenced_through_a_script_variable():
    html = f"""<script>var peopleSS = '{SHEET_ID}';
      var url = "https://sheets.googleapis.com/v4/spreadsheets/" + peopleSS
        + "/values/Export?key=x";
    </script>"""

    sources = find_google_sheets(html)

    assert sources == [SheetSource(sheet_id=SHEET_ID, sheet_name="Export")]
    assert sources[0].export_url.endswith("tqx=out:csv&sheet=Export")


def test_finds_direct_sheet_links():
    html = f'<iframe src="https://docs.google.com/spreadsheets/d/{SHEET_ID}/pubhtml"></iframe>'

    assert find_google_sheets(html)[0].sheet_id == SHEET_ID


def test_maps_common_column_names_without_confusing_profile_and_areas():
    rows = [
        ["First Name", "Last Name", "Research Profile", "Research Areas", ""],
        ["Michael", "Bowling", "https://apps.example.edu/person/mb", "AI, Games, ML", ""],
        ["Single", "", "", "", ""],
    ]

    people = rows_to_people(rows)

    assert len(people) == 1
    assert people[0].name == "Michael Bowling"
    assert people[0].profile_url == "https://apps.example.edu/person/mb"
    assert people[0].research_areas == ["AI", "Games", "ML"]
    assert people[0].email is None


def test_private_sheets_are_reported_not_parsed():
    def respond(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html>sign in</html>"
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))

    with pytest.raises(EmbeddedSourceError, match="not publicly readable"):
        fetch_sheet_rows(SheetSource(sheet_id=SHEET_ID), client)
