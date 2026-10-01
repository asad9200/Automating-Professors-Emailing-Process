"""Structured people lists that official pages load from public embedded sources.

Some departments keep their faculty list in a Google Sheet shared as "anyone with the link
can view" and render it with JavaScript. The official page's own script points at the
sheet; we read Google's public CSV export of it instead of scraping the rendered page.
"""

import csv
import io
import logging
import re
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_SHEET_ID = r"[A-Za-z0-9_-]{25,}"
_DIRECT_PATTERNS = [
    re.compile(rf"docs\.google\.com/spreadsheets/d/({_SHEET_ID})"),
    re.compile(rf"sheets\.googleapis\.com/v4/spreadsheets/({_SHEET_ID})"),
]
_VARIABLE_REFERENCE = re.compile(
    r"sheets\.googleapis\.com/v4/spreadsheets/[\"']\s*\+\s*([A-Za-z_$][\w$]*)"
)
_RANGE = re.compile(r"/values/([^?\"'&/]+)")


class SheetSource(BaseModel):
    sheet_id: str
    sheet_name: str | None = None

    @property
    def export_url(self) -> str:
        url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/gviz/tq?tqx=out:csv"
        if self.sheet_name:
            url += f"&sheet={quote(self.sheet_name)}"
        return url

    @property
    def public_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"


class PersonRecord(BaseModel):
    name: str
    title: str | None = None
    email: str | None = None
    profile_url: str | None = None
    research_areas: list[str] = Field(default_factory=list)


class EmbeddedSourceError(RuntimeError):
    """Raised when an embedded source cannot be read."""


def find_google_sheets(html: str) -> list[SheetSource]:
    """Find Google Sheets that a page's markup or scripts reference."""
    found: dict[str, SheetSource] = {}

    def add(sheet_id: str, context: str) -> None:
        range_match = _RANGE.search(context)
        sheet_name = None
        if range_match:
            sheet_name = range_match.group(1).split("!", 1)[0].strip() or None
        if sheet_id not in found:
            found[sheet_id] = SheetSource(sheet_id=sheet_id, sheet_name=sheet_name)

    for pattern in _DIRECT_PATTERNS:
        for match in pattern.finditer(html):
            add(match.group(1), html[match.start() : match.end() + 200])
    for match in _VARIABLE_REFERENCE.finditer(html):
        variable = re.escape(match.group(1))
        assignment = re.search(
            rf"(?:\b(?:var|let|const)\s+)?\b{variable}\s*=\s*[\"']({_SHEET_ID})[\"']",
            html,
        )
        if assignment:
            add(assignment.group(1), html[match.start() : match.end() + 200])
    return list(found.values())


def fetch_sheet_rows(source: SheetSource, client: httpx.Client) -> list[list[str]]:
    try:
        response = client.get(source.export_url, follow_redirects=True, timeout=30)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise EmbeddedSourceError("The page's public sheet could not be read.") from error
    if "text/csv" not in response.headers.get("content-type", ""):
        raise EmbeddedSourceError("The page's sheet is not publicly readable.")
    return list(csv.reader(io.StringIO(response.text)))


def _column(headers: list[str], *terms: str, exclude: tuple[str, ...] = ()) -> int | None:
    for index, header in enumerate(headers):
        if any(term in header for term in terms) and not any(term in header for term in exclude):
            return index
    return None


def rows_to_people(rows: list[list[str]]) -> list[PersonRecord]:
    """Map a sheet with a header row to people, recognising common column names."""
    if len(rows) < 2:
        return []
    headers = [cell.strip().lower() for cell in rows[0]]
    first = _column(headers, "first")
    last = _column(headers, "last", "surname", "family")
    name = _column(headers, "name", exclude=("first", "last", "user"))
    title = _column(headers, "title", "position", "rank", "role")
    email = _column(headers, "email", "e-mail")
    url = _column(headers, "profile", "url", "website", "homepage", "link", "page")
    research = _column(headers, "area", "interest", "topic", "expertise", "keyword")
    if research is None:
        research = _column(headers, "research", exclude=("profile", "url", "link", "page"))

    def cell(row: list[str], index: int | None) -> str:
        return row[index].strip() if index is not None and index < len(row) else ""

    people = []
    for row in rows[1:]:
        full_name = " ".join(part for part in (cell(row, first), cell(row, last)) if part)
        full_name = full_name or cell(row, name)
        if len(full_name.split()) < 2:
            continue
        link = cell(row, url)
        if not link.startswith("https://"):
            link = next(
                (value.strip() for value in row if value.strip().startswith("https://")), ""
            )
        address = cell(row, email)
        areas = [area.strip() for area in re.split(r"[,;|]", cell(row, research)) if area.strip()]
        people.append(
            PersonRecord(
                name=full_name,
                title=cell(row, title) or None,
                email=address if "@" in address else None,
                profile_url=link or None,
                research_areas=areas,
            )
        )
    return people
