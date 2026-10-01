import logging

import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from profreach.agents.profile_agent import ProfileAgent, ProfileExtractionError
from profreach.config import get_settings
from profreach.db.engine import get_engine, initialize_database
from profreach.db.models import (
    DegreeSeeking,
    Education,
    Publication,
    ResearchExperience,
    UserProfile,
    UserProfileData,
)
from profreach.db.profile_store import ProfileStore
from profreach.logging_config import configure_logging
from profreach.tools.cv_parser import CVParseError, extract_cv_text
from profreach.ui.common import apply_app_theme
from profreach.ui.discovery import render_discovery_page
from profreach.ui.outreach import render_outreach_page
from profreach.ui.review import render_review_page
from profreach.ui.send import render_send_page
from profreach.ui.setup import render_setup_page

logger = logging.getLogger(__name__)


def _profile_values(profile: UserProfile | None) -> dict:
    if profile is None:
        return UserProfileData().model_dump(mode="python")
    return profile.model_dump(
        mode="python",
        exclude={"id", "created_at", "updated_at", "cv_filename"},
    )


def _rows_editor(label: str, model: type, values: list, key: str) -> pd.DataFrame:
    with st.expander(label, expanded=bool(values)):
        columns = list(model.model_fields)
        frame = pd.DataFrame(
            [value.model_dump() if hasattr(value, "model_dump") else value for value in values],
            columns=columns,
        )
        return st.data_editor(
            frame,
            num_rows="dynamic",
            width="stretch",
            key=key,
        )


def _clean_cell(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _records(frame: pd.DataFrame, model: type) -> list:
    records = []
    for row in frame.to_dict(orient="records"):
        cleaned = {key: _clean_cell(value) for key, value in row.items()}
        if any(value is not None for value in cleaned.values()):
            records.append(model.model_validate(cleaned))
    return records


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


_REQUIRED_FIELDS = {
    "name": "name",
    "education": "education",
    "research_interests": "research interests",
    "target_degree": "degree sought",
}


def missing_profile_fields(values: dict) -> list[str]:
    return [label for field, label in _REQUIRED_FIELDS.items() if not values.get(field)]


def _render_profile_form(values: dict, store: ProfileStore, cv_filename: str | None) -> None:
    st.subheader("Profile details")
    missing = missing_profile_fields(values)
    if missing:
        st.warning(
            "Please add: " + ", ".join(missing) + ". The email writer only uses facts you "
            "provide, so missing fields cannot be filled in automatically."
        )
    with st.form("profile_form"):
        name = st.text_input("Name", value=values.get("name") or "")
        gpa = st.text_input("GPA", value=values.get("gpa") or "")
        degree_options = [None, *DegreeSeeking]
        current_degree = values.get("target_degree")
        try:
            degree_index = degree_options.index(current_degree)
        except ValueError:
            degree_index = 0
        target_degree = st.selectbox(
            "Degree sought",
            degree_options,
            index=degree_index,
            format_func=lambda degree: degree.value if degree else "Not specified",
        )
        education = _rows_editor("Education", Education, values.get("education", []), "education")
        publications = _rows_editor(
            "Publications", Publication, values.get("publications", []), "publications"
        )
        experience = _rows_editor(
            "Research experience",
            ResearchExperience,
            values.get("research_experience", []),
            "research_experience",
        )
        skills = st.text_area("Skills", value="\n".join(values.get("skills", [])))
        interests = st.text_area(
            "Research interests",
            value="\n".join(values.get("research_interests", [])),
        )
        submitted = st.form_submit_button("Save profile", type="primary")

    if submitted:
        try:
            profile_data = UserProfileData(
                name=name.strip() or None,
                gpa=gpa.strip() or None,
                target_degree=target_degree,
                education=_records(education, Education),
                publications=_records(publications, Publication),
                research_experience=_records(experience, ResearchExperience),
                skills=_lines(skills),
                research_interests=_lines(interests),
            )
            saved = store.save_profile(profile_data, cv_filename=cv_filename)
        except Exception:
            logger.exception("Could not save user profile")
            st.error("The profile could not be saved. Check the entered values and try again.")
            return
        st.session_state["profile_values"] = _profile_values(saved)
        st.session_state["cv_filename"] = saved.cv_filename
        st.success("Profile saved.")


def render_profile_page(engine: Engine | None = None) -> None:
    apply_app_theme()
    settings = get_settings()
    configure_logging(settings)
    database_engine = engine or get_engine(settings.database_url)
    initialize_database(database_engine)
    store = ProfileStore(database_engine)

    st.title("Emailering")
    st.caption("Your academic profile")

    saved_profile = store.get_profile()
    if "profile_values" not in st.session_state:
        st.session_state["profile_values"] = _profile_values(saved_profile)
    if "cv_filename" not in st.session_state:
        st.session_state["cv_filename"] = saved_profile.cv_filename if saved_profile else None

    uploaded_cv = st.file_uploader(
        "Upload a CV to prefill your profile",
        type=["pdf", "docx"],
        max_upload_size=10,
    )
    if st.button("Extract profile from CV", disabled=uploaded_cv is None):
        try:
            assert uploaded_cv is not None
            cv_text = extract_cv_text(uploaded_cv.getvalue(), uploaded_cv.name)
            extracted = ProfileAgent(settings).parse(cv_text)
        except (CVParseError, ProfileExtractionError) as error:
            st.error(str(error))
        except Exception:
            logger.exception("Unexpected CV processing error")
            st.error("The CV could not be processed. Check the file and configuration, then retry.")
        else:
            st.session_state["profile_values"] = extracted.model_dump(mode="python")
            st.session_state["cv_filename"] = uploaded_cv.name
            st.success("CV details extracted. Review and complete the profile below.")

    _render_profile_form(
        st.session_state["profile_values"],
        store,
        st.session_state.get("cv_filename"),
    )
    _render_email_settings(store)


def _render_email_settings(store: ProfileStore) -> None:
    st.subheader("For your emails")
    st.caption(
        "The writer may use only your profile above plus the facts below. List projects, "
        "work, field experience, leadership and languages exactly as you want them described."
    )
    profile = store.get_profile()
    with st.form("email_settings_form"):
        facts = st.text_area(
            "Extra facts the writer may use",
            value=(profile.email_facts if profile else "") or "",
            height=260,
            placeholder="Projects: ...\nWork: ...\nField experience: ...\nLanguages: ...",
        )
        signature = st.text_area(
            "Sign-off (added exactly as written)",
            value=(profile.email_signature if profile else "") or "",
            height=100,
            placeholder="Kind regards,\nYour Name\n+00 000 0000000",
        )
        saved = st.form_submit_button("Save email details", type="primary")
    if saved:
        store.save_email_settings(facts, signature)
        st.success("Saved. New and regenerated emails will use these details.")


def render_home_page(profile_page, discovery_page, outreach_page, review_page, send_page) -> None:
    apply_app_theme()
    st.title("Emailering")
    st.caption("Your academic outreach workspace")
    st.write(
        "Build thoughtful, personalized outreach to professors, from your academic profile "
        "through reviewed email drafts."
    )

    st.subheader("Start your project")
    first, second, third = st.columns(3)
    with first:
        st.markdown("**1. Prepare your profile**")
        st.caption("Add the facts and research interests your emails should use.")
        st.page_link(profile_page, label="Open profile", icon=":material/person:")
    with second:
        st.markdown("**2. Find professors**")
        st.caption("Choose universities and discover faculty in your field.")
        st.page_link(discovery_page, label="Discover professors", icon=":material/search:")
    with third:
        st.markdown("**3. Write and review**")
        st.caption("Research a professor, prepare a draft, and approve it before sending.")
        st.page_link(outreach_page, label="Research & write", icon=":material/edit_note:")

    st.divider()
    st.subheader("Continue outreach")
    left, right = st.columns(2)
    with left:
        st.page_link(review_page, label="Review email drafts", icon=":material/rate_review:")
    with right:
        st.page_link(send_page, label="Send & follow up", icon=":material/send:")


def main() -> None:
    st.set_page_config(page_title="Emailering", page_icon="✉️", layout="wide")
    profile_page = st.Page(render_profile_page, title="Profile", icon=":material/person:")
    discovery_page = st.Page(
        render_discovery_page, title="Discover professors", icon=":material/search:"
    )
    outreach_page = st.Page(
        render_outreach_page, title="Research & write", icon=":material/edit_note:"
    )
    review_page = st.Page(render_review_page, title="Review emails", icon=":material/rate_review:")
    send_page = st.Page(render_send_page, title="Send & follow up", icon=":material/send:")
    setup_page = st.Page(render_setup_page, title="Setup", icon=":material/settings:")
    home_page = st.Page(
        lambda: render_home_page(
            profile_page, discovery_page, outreach_page, review_page, send_page
        ),
        title="Home",
        icon=":material/home:",
        default=True,
    )
    st.navigation(
        {
            "Project": [
                home_page,
                discovery_page,
                outreach_page,
                review_page,
                send_page,
            ],
            "Setup": [setup_page],
            "Profile": [profile_page],
        }
    ).run()


if __name__ == "__main__":
    main()
