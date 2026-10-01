import logging
from pathlib import Path

import streamlit as st
from sqlalchemy.engine import Engine

from profreach.config import get_settings
from profreach.gmail.auth import GmailAccessDenied, GmailAuthError, disconnect, is_connected
from profreach.gmail.client import GmailError
from profreach.services import gmail_client
from profreach.setup_tools import (
    SetupError,
    save_oauth_client,
    service_statuses,
    update_env_file,
    verify_groq_key,
)
from profreach.ui.common import apply_app_theme, page_context

logger = logging.getLogger(__name__)
GROQ_KEYS_URL = "https://console.groq.com/keys"
CLOUD_PROJECT_URL = "https://console.cloud.google.com/projectcreate"
GMAIL_API_URL = "https://console.cloud.google.com/apis/library/gmail.googleapis.com"
AUDIENCE_URL = "https://console.cloud.google.com/auth/audience"
CLIENTS_URL = "https://console.cloud.google.com/auth/clients/create"


def _reload_settings():
    get_settings.cache_clear()
    return get_settings()


def render_google_connect(settings, key: str = "google") -> bool:
    """The Continue with Google control; returns True when Gmail is connected."""
    if is_connected(settings.gmail_token_path):
        address = st.session_state.get("gmail_address")
        if address is None:
            try:
                address = gmail_client(settings).my_address()
                st.session_state["gmail_address"] = address
            except (GmailAuthError, GmailError):
                address = None
        left, right = st.columns([3, 1])
        if address:
            left.success(f"Signed in with Google as **{address}**.")
        else:
            left.warning("Your Google sign-in expired. Sign in again.")
        if right.button("Sign out", key=f"{key}_signout"):
            disconnect(settings.gmail_token_path)
            st.session_state.pop("gmail_address", None)
            st.rerun()
        if address:
            return True

    if not Path(settings.gmail_credentials_path).is_file():
        st.info("Finish the one-time Google setup below, then Continue with Google appears here.")
        return False
    st.caption(
        "A Google sign-in page opens in your browser. Choose your account and select Allow. "
        "Emailering can then save drafts and send the emails you approve."
    )
    if st.button(
        "Continue with Google",
        icon=":material/login:",
        type="primary",
        key=f"{key}_signin",
    ):
        try:
            with st.spinner(
                "Waiting for you to finish signing in with Google (up to 3 minutes)..."
            ):
                address = gmail_client(settings, allow_browser_flow=True).my_address()
        except GmailAccessDenied as error:
            st.error(str(error))
            st.link_button("Open Test users", AUDIENCE_URL, icon=":material/open_in_new:")
        except (GmailAuthError, GmailError) as error:
            st.error(str(error))
        except Exception:
            logger.exception("Google sign-in failed")
            st.error(
                "Google sign-in did not finish. If Google said access is blocked, add your "
                "account as a test user (step 3 below) and try again."
            )
        else:
            st.session_state["gmail_address"] = address
            st.rerun()
    return False


def _status_section(settings) -> None:
    st.subheader("Status")
    columns = st.columns(4)
    for column, status in zip(columns, service_statuses(settings), strict=True):
        icon = "✅" if status.ready else "⚠️"
        column.markdown(f"{icon} **{status.name}**  \n{status.detail}")


def _groq_section(settings) -> None:
    st.subheader("1. AI writing (free)")
    if settings.groq_api_key:
        st.success("A Groq key is saved. You can replace it below.")
    st.write(
        "Groq's free tier writes your emails and reads your CV. Sign in to Groq with your "
        "Google account, create a key, and paste it here. You only do this once."
    )
    st.link_button("Get a free Groq key", GROQ_KEYS_URL, icon=":material/open_in_new:")
    with st.form("groq_key_form", clear_on_submit=True):
        key = st.text_input("Groq API key", type="password", placeholder="gsk_...")
        submitted = st.form_submit_button("Test and save key", type="primary")
    if submitted:
        try:
            with st.spinner("Checking the key with Groq..."):
                verify_groq_key(key)
        except SetupError as error:
            st.error(str(error))
            return
        update_env_file({"GROQ_API_KEY": key.strip(), "LLM_PROVIDER": "groq"})
        _reload_settings()
        st.success("Key works and is saved on this computer.")
        st.rerun()


def _google_section(settings) -> None:
    st.subheader("2. Gmail: Continue with Google")
    render_google_connect(settings, key="setup_google")
    has_client = Path(settings.gmail_credentials_path).is_file()
    with st.expander("One-time Google setup (about 5 minutes)", expanded=not has_client):
        st.write(
            "Google requires every app that uses Gmail to have its own sign-in ID. You create "
            "it once in your free Google Cloud account; afterwards signing in is one click."
        )
        st.markdown("**Step 1.** Create a project (any name, for example *emailering*).")
        st.link_button("Create project", CLOUD_PROJECT_URL, icon=":material/open_in_new:")
        st.markdown("**Step 2.** Turn on the Gmail API for that project and select **Enable**.")
        st.link_button("Open Gmail API", GMAIL_API_URL, icon=":material/open_in_new:")
        st.markdown(
            "**Step 3.** Open **Audience**. If asked, select **Get started**, enter an app name "
            "and your email, choose **External**, and finish. Then, under **Test users**, select "
            "**Add users** and add the exact Gmail address you will sign in with. Skipping this "
            "causes Google's 'Error 403: access_denied'."
        )
        st.link_button("Open Audience", AUDIENCE_URL, icon=":material/open_in_new:")
        st.markdown(
            "**Step 4.** Create a client: application type **Desktop app**, any name, then "
            "**Create** and **Download JSON**."
        )
        st.link_button("Create client", CLIENTS_URL, icon=":material/open_in_new:")
        st.markdown("**Step 5.** Drop the downloaded file here.")
        uploaded = st.file_uploader("Google client file (.json)", type=["json"])
        if uploaded is not None:
            try:
                save_oauth_client(uploaded.getvalue(), settings.gmail_credentials_path)
            except SetupError as error:
                st.error(str(error))
            else:
                st.success("Saved. Now select Continue with Google above.")
        if has_client:
            st.caption("A client file is already saved; uploading a new one replaces it.")
        st.caption(
            "While the app is in testing, Google shows an unverified-app warning; choose "
            "Continue for your own account. Google asks you to sign in again about every "
            "7 days in testing mode."
        )


def _optional_section(settings) -> None:
    with st.expander("Optional: faster or extra sources"):
        st.write(
            "Everything works without these. Add them only if you want higher limits or "
            "Google Scholar results."
        )
        with st.form("optional_keys", clear_on_submit=True):
            s2 = st.text_input("Semantic Scholar key (higher rate limits)", type="password")
            serp = st.text_input("SerpAPI key (adds Google Scholar)", type="password")
            tavily = st.text_input("Tavily key (alternative web search)", type="password")
            saved = st.form_submit_button("Save optional keys")
        if saved:
            values = {
                name: value.strip()
                for name, value in (
                    ("SEMANTIC_SCHOLAR_API_KEY", s2),
                    ("SERPAPI_API_KEY", serp),
                    ("TAVILY_API_KEY", tavily),
                )
                if value.strip()
            }
            if values:
                update_env_file(values)
                _reload_settings()
                st.success(f"Saved {len(values)} key(s).")
                st.rerun()


def render_setup_page(engine: Engine | None = None) -> None:
    apply_app_theme()
    settings, _ = page_context(engine)
    st.title("Setup")
    st.caption("Connect your accounts once. Keys stay on this computer in the .env file.")
    _status_section(settings)
    st.divider()
    _groq_section(settings)
    st.divider()
    _google_section(settings)
    st.divider()
    _optional_section(settings)
