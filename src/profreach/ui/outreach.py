import logging

import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from profreach.db.discovery_store import DiscoveryStore
from profreach.db.outreach_store import OutreachStore
from profreach.db.profile_store import ProfileStore
from profreach.gmail.auth import is_connected
from profreach.services import outreach_runner
from profreach.ui.common import apply_app_theme, page_context, session_attachment, target_selector

logger = logging.getLogger(__name__)


def _research_panel(store: OutreachStore, professor_ids: list[int]) -> None:
    st.subheader("Research results")
    for professor_id in professor_ids:
        professor = store.get_professor(professor_id)
        research = store.get_research(professor_id)
        if professor is None:
            continue
        status = research.status.value if research else "not researched"
        with st.expander(f"{professor.name} · {status}"):
            if research is None:
                st.caption("Run the pipeline to research this professor.")
                continue
            if research.themes:
                st.write("**Research themes:** " + "; ".join(research.themes))
            st.write(f"**Open positions:** {research.open_positions.value.replace('_', ' ')}")
            if research.open_positions_evidence:
                st.caption(f'"{research.open_positions_evidence}" — {research.open_positions_url}')
            if research.error:
                st.caption(f"Provider notes: {research.error}")
            papers = store.list_publications(professor_id)
            if papers:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "year": paper.year,
                                "title": paper.title,
                                "venue": paper.venue or "",
                                "summary": paper.abstract_summary or "",
                                "source": paper.source,
                                "url": paper.url or "",
                            }
                            for paper in papers
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            elif research.status.value == "no_verified_author":
                st.warning(
                    "No publication profile matched both this name and this university, so no "
                    "papers were attached. The writer will not draft an email without papers."
                )


def render_outreach_page(engine: Engine | None = None) -> None:
    apply_app_theme()
    settings, database_engine = page_context(engine)
    store = OutreachStore(database_engine)
    st.title("Research & write")
    st.caption(
        "Researches selected professors (OpenAlex, Semantic Scholar, optional Google Scholar, "
        "and their official pages), then drafts one email each. Nothing is sent from here."
    )

    target = target_selector(DiscoveryStore(database_engine).list_targets(), "outreach_target")
    if target is None:
        return
    professors = store.selected_professors(target.id)
    if not professors:
        st.info("Select professors in the discovery table first.")
        return
    profile = ProfileStore(database_engine).get_profile()
    if profile is None:
        st.warning("Save your profile before writing emails.")

    st.write(f"{len(professors)} selected professors · degree sought: {target.target_degree}")
    missing_email = [professor.name for professor in professors if not professor.email]
    if missing_email:
        st.warning(
            "No published email for: " + ", ".join(missing_email) + ". Drafts can be written, "
            "but they cannot be saved to Gmail or sent."
        )

    gmail_ready = is_connected(settings.gmail_token_path)
    with st.form(f"outreach_form_{target.id}"):
        start_term = st.text_input("Intended start term", placeholder="e.g. Fall 2027")
        attach_cv = st.checkbox("Attach my CV to these emails")
        create_drafts = st.checkbox(
            "Save drafts to Gmail after writing",
            value=gmail_ready,
            disabled=not gmail_ready,
            help="Connect Gmail on the Send page to enable.",
        )
        force = st.checkbox(
            f"Refresh research even if cached (cache lasts {settings.research_cache_days} days)"
        )
        submitted = st.form_submit_button("Run research and write emails", type="primary")

    if submitted:
        if attach_cv and session_attachment() is None and not settings.cv_attachment_path:
            st.error("Upload the CV to attach on the Send page, or set CV_ATTACHMENT_PATH.")
        else:
            with st.status("Running outreach pipeline...", expanded=True) as status:
                try:
                    runner = outreach_runner(
                        settings,
                        database_engine,
                        progress=status.write,
                        attachment=session_attachment(),
                    )
                    thread_id = runner.start(
                        target.id,
                        [professor.id for professor in professors],
                        start_term.strip() or None,
                        attach_cv=attach_cv,
                        force_research=force,
                        create_gmail_drafts=create_drafts,
                    )
                    state = runner.state(thread_id)
                except Exception:
                    logger.exception("Outreach pipeline failed")
                    status.update(label="Pipeline stopped", state="error")
                    st.error("The pipeline stopped unexpectedly. Saved progress is kept; retry.")
                else:
                    status.update(label="Research and writing complete", state="complete")
                    written = sum(value == "written" for value in state.get("written", {}).values())
                    st.success(f"Wrote {written} new drafts. Review them on the Review page.")
                    for error in state.get("errors", []):
                        st.warning(error)

    runs = store.list_runs(target.id)
    if runs:
        st.subheader("Pipeline runs")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "thread_id": run.thread_id,
                        "stage": run.stage.replace("_", " "),
                        "professors": len(run.professor_ids),
                        "start_term": run.start_term or "",
                        "created": run.created_at,
                    }
                    for run in runs
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    _research_panel(store, [professor.id for professor in professors])
