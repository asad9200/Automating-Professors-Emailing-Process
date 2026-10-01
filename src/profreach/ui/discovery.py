import logging
from urllib.parse import urlparse

import httpx
import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from profreach.agents.professor_discovery_agent import ProfessorDiscoveryAgent
from profreach.agents.university_agent import UniversityAgent, UniversityResolutionError
from profreach.config import get_settings
from profreach.db.discovery_store import DiscoveryStore
from profreach.db.engine import get_engine, initialize_database
from profreach.db.models import DegreeSeeking, Professor, TargetUniversity, TargetUniversityData
from profreach.logging_config import configure_logging
from profreach.tools.search import SearchProviderError, make_search_provider
from profreach.tools.web import OfficialPageFetcher
from profreach.ui.common import apply_app_theme

logger = logging.getLogger(__name__)


@st.cache_data(ttl=86400, show_spinner=False)
def _search_universities(query: str) -> list[tuple[str, str]] | None:
    try:
        response = httpx.get(
            "https://universities.hipolabs.com/search",
            params={"name": query},
            timeout=4,
        )
        response.raise_for_status()
        results = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    matches = {
        (item["name"].strip(), item["country"].strip())
        for item in results
        if item.get("name", "").strip() and item.get("country", "").strip()
    }
    return sorted(matches, key=lambda item: (item[0].casefold(), item[1].casefold()))[:15]


def _use_university_suggestion() -> None:
    selected = st.session_state.get("university_suggestion")
    if selected:
        st.session_state["university_name"] = selected[0]
        st.session_state["university_suggestion"] = None


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()]


def _target_form(store: DiscoveryStore, settings) -> None:
    st.subheader("Target university")
    university_name = st.text_input(
        "University name",
        key="university_name",
        help=(
            "Start typing for worldwide suggestions. "
            "You can enter any name, even if it is not listed."
        ),
    )
    query = university_name.strip()
    if len(query) >= 2:
        suggestions = _search_universities(query)
        if suggestions is None:
            st.caption("University suggestions are unavailable. You can still enter any name.")
        elif suggestions:
            st.selectbox(
                "Matching universities worldwide",
                suggestions,
                index=None,
                format_func=lambda item: f"{item[0]} · {item[1]}",
                key="university_suggestion",
                on_change=_use_university_suggestion,
            )
        else:
            st.caption("No suggestions found. You can still use the university name you entered.")

    with st.form("target_university_form"):
        location = st.text_input("City, state/province, and country")
        department = st.text_input("Department")
        field_of_study = st.text_input("Field of study")
        research_interests = st.text_area("Research interests", height=90)
        degree = st.selectbox("Degree sought", list(DegreeSeeking))
        submitted = st.form_submit_button("Find official faculty directory", type="primary")

    if not submitted:
        return
    required_fields = (
        university_name.strip(),
        location.strip(),
        department.strip(),
        field_of_study.strip(),
    )
    if not all(required_fields):
        st.error("Enter the university, location, department, and field of study.")
        return

    target_data = TargetUniversityData(
        university_name=university_name.strip(),
        location=location.strip(),
        department=department.strip(),
        field_of_study=field_of_study.strip(),
        research_interests=_lines(research_interests),
        target_degree=degree,
    )
    try:
        provider = make_search_provider(settings)
        fetcher = OfficialPageFetcher(
            user_agent=settings.user_agent,
            delay_seconds=settings.scrape_delay_seconds,
            playwright_fallback=False,
        )
        resolution = UniversityAgent(provider, page_fetcher=fetcher).resolve(target_data)
        target = store.save_target(target_data, resolution)
    except (UniversityResolutionError, SearchProviderError) as error:
        st.error(str(error))
        return
    except Exception:
        logger.exception("University resolution failed")
        st.error("The official university directory could not be resolved.")
        return

    st.session_state["discovery_target_id"] = target.id
    st.success(f"Official domain: {resolution.official_domain}")
    st.link_button("Open faculty directory", resolution.faculty_directory_url)


def _target_options(targets: list[TargetUniversity], selected_id: int | None):
    if not targets:
        st.info("Resolve and save a university above to begin professor discovery.")
        return None
    ids = [target.id for target in targets]
    index = ids.index(selected_id) if selected_id in ids else 0
    return st.selectbox(
        "Saved target",
        targets,
        index=index,
        format_func=lambda target: (
            f"{target.university_name} | {target.department} | {target.location}"
        ),
        key="saved_discovery_target",
    )


def _run_discovery(target: TargetUniversity, store: DiscoveryStore, settings) -> None:
    if not target.official_domain or not target.faculty_directory_url:
        st.error("This target has no verified faculty directory. Resolve it again first.")
        return
    target_data = TargetUniversityData(
        university_name=target.university_name,
        location=target.location,
        department=target.department,
        field_of_study=target.field_of_study,
        research_interests=target.research_interests,
        target_degree=target.target_degree,
    )
    with st.spinner("Searching official pages and reading faculty profiles..."):
        report = ProfessorDiscoveryAgent(
            settings=settings,
            search_provider=make_search_provider(settings),
        ).discover(
            target_data,
            target.official_domain,
            target.faculty_directory_url,
        )
        professors = store.save_professors(target.id, report.professors)
    st.session_state[f"discovery_report_{target.id}"] = {
        "pages_visited": report.pages_visited,
        "found": len(report.professors),
        "stored": len(professors),
        "errors": report.page_errors,
        "reasons": report.error_reasons,
        "search_errors": report.search_errors,
    }


def _professor_frame(professors: list[Professor]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "professor_id": professor.id,
                "selected": professor.selected,
                "name": professor.name,
                "title": professor.title or "",
                "relevance_score": professor.relevance_score,
                "match_reason": professor.relevance_reason,
                "email": professor.email or "Missing",
                "email_confidence": professor.email_confidence.value,
                "research_areas": "; ".join(professor.research_areas),
                "profile_url": professor.profile_url or "",
                "lab_url": professor.lab_url or "",
                "email_source_url": professor.email_source_url or "",
                "source_url": professor.source_url,
            }
            for professor in professors
        ]
    )


def _selection_table(target_id: int, store: DiscoveryStore) -> None:
    professors = store.list_professors(target_id)
    if not professors:
        st.info("No professors saved yet. Run discovery for this target.")
        return

    st.subheader(f"Professors ({len(professors)})")
    st.caption("Select candidates to include in the CSV export. Email addresses are never guessed.")
    with st.form(f"professor_selection_{target_id}"):
        edited = st.data_editor(
            _professor_frame(professors),
            hide_index=True,
            width="stretch",
            disabled=[
                "professor_id",
                "name",
                "title",
                "relevance_score",
                "match_reason",
                "email",
                "email_confidence",
                "research_areas",
                "profile_url",
                "lab_url",
                "email_source_url",
                "source_url",
            ],
            column_config={"professor_id": None},
            key=f"professor_table_{target_id}",
        )
        saved = st.form_submit_button("Save selection")

    if saved:
        selected_ids = {
            int(row["professor_id"])
            for row in edited.to_dict(orient="records")
            if row.get("selected") and row.get("professor_id") is not None
        }
        store.update_selection(target_id, selected_ids)
        st.rerun()

    selected = [professor for professor in professors if professor.selected]
    if selected:
        export_frame = _professor_frame(selected).drop(columns=["professor_id", "selected"])
        st.download_button(
            "Export selected professors as CSV",
            data=export_frame.to_csv(index=False).encode("utf-8"),
            file_name="selected_professors.csv",
            mime="text/csv",
        )
    else:
        st.caption("No professors selected.")


def _registered_domain(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return UniversityAgent._registered_domain(parsed.hostname)


def _directory_tools(target: TargetUniversity, store: DiscoveryStore, settings) -> None:
    with st.expander("Use a different faculty directory URL"):
        st.caption(
            "Paste the department's faculty list from the university's own website. "
            "Crawling stays limited to that website's domain."
        )
        new_url = st.text_input("Faculty directory URL", key=f"override_url_{target.id}")
        if st.button("Use this URL", key=f"override_{target.id}"):
            domain = _registered_domain(new_url)
            if domain is None:
                st.error("Enter a full https:// URL.")
            else:
                store.update_directory(target.id, domain, new_url.strip())
                st.session_state.pop(f"discovery_report_{target.id}", None)
                st.success(f"Directory set on {domain}. Select Discover professors.")
                st.rerun()

    with st.expander("Import saved page (for sites that block automated access)"):
        st.caption(
            "Open the faculty list in your browser, save it with Ctrl+S as 'Webpage, HTML only', "
            "and upload it here with the address it came from. For paginated lists, upload each "
            "saved page."
        )
        page_url = st.text_input(
            "Address of the saved page",
            value=target.faculty_directory_url or "",
            key=f"import_url_{target.id}",
        )
        files = st.file_uploader(
            "Saved HTML files",
            type=["html", "htm"],
            accept_multiple_files=True,
            key=f"import_files_{target.id}",
        )
        if st.button("Extract professors", key=f"import_{target.id}", disabled=not files):
            domain = _registered_domain(page_url)
            if domain is None or domain != target.official_domain:
                st.error(
                    f"The address must be an https:// page on {target.official_domain}. "
                    "Set the directory URL above first if the domain is wrong."
                )
                return
            pages = [
                (page_url.strip(), file.getvalue().decode("utf-8", errors="replace"))
                for file in files
            ]
            target_data = TargetUniversityData(
                university_name=target.university_name,
                location=target.location,
                department=target.department,
                field_of_study=target.field_of_study,
                research_interests=target.research_interests,
                target_degree=target.target_degree,
            )
            report = ProfessorDiscoveryAgent(settings=settings).discover_saved_pages(
                target_data, target.official_domain, pages
            )
            saved = store.save_professors(target.id, report.professors)
            if report.professors:
                st.success(f"Extracted {len(report.professors)} faculty records.")
            else:
                st.warning("No one with an academic title was found in the uploaded pages.")
            st.session_state[f"discovery_report_{target.id}"] = {
                "pages_visited": len(pages),
                "found": len(report.professors),
                "stored": len(saved),
                "errors": [],
                "reasons": {},
            }


def render_discovery_page(engine: Engine | None = None) -> None:
    apply_app_theme()
    settings = get_settings()
    configure_logging(settings)
    database_engine = engine or get_engine(settings.database_url)
    initialize_database(database_engine)
    store = DiscoveryStore(database_engine)

    st.title("Professor discovery")
    _target_form(store, settings)

    targets = store.list_targets()
    selected_id = st.session_state.get("discovery_target_id")
    target = _target_options(targets, selected_id)
    if target is None:
        return
    st.session_state["discovery_target_id"] = target.id

    if target.official_domain:
        st.caption(
            f"Official domain: {target.official_domain} · "
            f"Faculty directory: {target.faculty_directory_url}"
        )
    if st.button("Discover professors", type="primary", key=f"discover_{target.id}"):
        try:
            _run_discovery(target, store, settings)
        except Exception:
            logger.exception("Professor discovery workflow failed")
            st.error("Discovery could not finish. Saved results are preserved; please retry.")

    report = st.session_state.get(f"discovery_report_{target.id}")
    if report:
        st.info(
            f"Visited {report['pages_visited']} official pages; "
            f"found {report['found']} faculty records; "
            f"{len(report['errors'])} page errors."
        )
        if report.get("search_errors"):
            st.warning(
                "Some additional official-page searches failed; the saved directory was "
                "still crawled."
            )
            for reason in report["search_errors"]:
                st.caption(reason)
        if report["errors"]:
            with st.expander("Pages that could not be read", expanded=not report["found"]):
                for url in report["errors"]:
                    reason = report.get("reasons", {}).get(url, "")
                    st.write(f"{url} — {reason}" if reason else url)
        elif not report["found"] and not report.get("search_errors"):
            st.warning(
                "The page was read, but nobody on it has an academic title such as Professor "
                "or Lecturer. It may be a staff, athletics, or contact directory rather than the "
                "department's faculty list. Use a different directory URL below."
            )
    _directory_tools(target, store, settings)

    _selection_table(target.id, store)
