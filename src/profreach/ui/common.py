import streamlit as st
from sqlalchemy.engine import Engine

from profreach.config import Settings, get_settings
from profreach.db.engine import get_engine, initialize_database
from profreach.db.models import TargetUniversity
from profreach.gmail.client import Attachment
from profreach.logging_config import configure_logging

CV_SESSION_KEY = "cv_attachment"


def page_context(engine: Engine | None = None) -> tuple[Settings, Engine]:
    settings = get_settings()
    configure_logging(settings)
    database_engine = engine or get_engine(settings.database_url)
    initialize_database(database_engine)
    return settings, database_engine


def apply_app_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --ink: #171717;
            --muted-ink: #626262;
            --paper: #f5f5f5;
            --surface: #ffffff;
            --line: #dedede;
            --black: #111111;
            --black-hover: #333333;
        }

        .stApp {
            color: var(--ink);
            background:
                radial-gradient(ellipse at 88% 0%, rgba(0, 0, 0, .035), transparent 34rem),
                linear-gradient(180deg, #ffffff 0%, var(--paper) 58%, #eeeeee 100%);
        }

        [data-testid="stHeader"] {
            background: rgba(255, 255, 255, .88);
        }

        [data-testid="stAppViewContainer"] .main .block-container {
            max-width: 1440px;
            padding: 2.2rem clamp(1.25rem, 4vw, 3.5rem) 4rem;
            animation: page-arrive .55s cubic-bezier(.2, .7, .25, 1) both;
        }

        h1, h2, h3 {
            color: var(--ink);
            font-family: Georgia, "Times New Roman", serif;
            letter-spacing: 0;
        }

        h1 {
            margin: .15rem 0 .4rem;
            font-size: clamp(2rem, 3vw, 2.65rem);
            line-height: 1.15;
        }

        h2, h3 {
            line-height: 1.3;
        }

        [data-testid="stCaptionContainer"] {
            color: var(--muted-ink);
        }

        [data-testid="stSidebar"] {
            background: #090909;
            border-right: 1px solid #292929;
        }

        [data-testid="stSidebar"] > div:first-child {
            padding-top: 1.5rem;
        }

        [data-testid="stSidebarNav"]::before {
            display: block;
            margin: 0 0 1.5rem .35rem;
            color: #f2f2f2;
            content: "EMAILERING  /  ACADEMIC OUTREACH";
            font-size: .68rem;
            font-weight: 700;
            letter-spacing: .08em;
        }

        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] p {
            color: #e5e5e5;
        }

        [data-testid="stSidebarNav"] {
            padding: .25rem .7rem;
        }

        [data-testid="stSidebarNav"] a {
            border: 1px solid transparent;
            border-radius: 6px;
            color: #d5d5d5;
            transition: background-color .18s ease, border-color .18s ease, transform .18s ease;
        }

        [data-testid="stSidebarNav"] a:hover {
            transform: translateX(3px);
            border-color: #444444;
            background: #202020;
            color: #fff;
        }

        [data-testid="stSidebarNav"] a[aria-current="page"] {
            border-color: #555555;
            background: #303030;
            color: #fff;
        }

        .stButton > button, .stFormSubmitButton > button {
            min-height: 2.65rem;
            border-radius: 5px;
            border: 1px solid #d0d0d0;
            color: var(--ink);
            background: var(--surface);
            font-weight: 600;
            transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
        }

        .stButton > button:hover, .stFormSubmitButton > button:hover {
            transform: translateY(-1px);
            border-color: #888888;
            box-shadow: 0 5px 14px rgba(0, 0, 0, .10);
            color: var(--black);
        }

        .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
            border-color: var(--black);
            background: var(--black);
            color: #fff;
        }

        .stButton > button[kind="primary"]:hover,
        .stFormSubmitButton > button[kind="primary"]:hover {
            border-color: var(--black-hover);
            background: var(--black-hover);
            color: #fff;
        }

        [data-testid="stTextInput"] input,
        [data-testid="stTextArea"] textarea,
        [data-testid="stSelectbox"] [role="combobox"],
        [data-testid="stMultiSelect"] [role="combobox"] {
            border-radius: 5px;
            border-color: #d4d4d4;
            background-color: rgba(255, 255, 255, .88);
        }

        [data-testid="stTextInput"] input:focus,
        [data-testid="stTextArea"] textarea:focus {
            border-color: var(--black);
            box-shadow: 0 0 0 1px var(--black);
        }

        [data-testid="stExpander"] {
            border-color: var(--line);
            border-radius: 6px;
            background: rgba(255, 255, 255, .62);
        }

        [data-testid="stDataFrame"], [data-testid="stDataEditor"] {
            border: 1px solid var(--line);
            border-radius: 6px;
            background: var(--surface);
        }

        [data-testid="stAlert"] {
            border-radius: 5px;
        }

        hr {
            border-color: var(--line);
        }

        @keyframes page-arrive {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                scroll-behavior: auto !important;
                animation-duration: .01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: .01ms !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def target_selector(targets: list[TargetUniversity], key: str) -> TargetUniversity | None:
    if not targets:
        st.info("Resolve a university on the Discover professors page first.")
        return None
    ids = [target.id for target in targets]
    remembered = st.session_state.get("discovery_target_id")
    index = ids.index(remembered) if remembered in ids else 0
    target = st.selectbox(
        "Target",
        targets,
        index=index,
        format_func=lambda item: f"{item.university_name} | {item.department}",
        key=key,
    )
    st.session_state["discovery_target_id"] = target.id
    return target


def session_attachment() -> Attachment | None:
    """The CV uploaded for attaching lives only in this browser session's memory."""
    return st.session_state.get(CV_SESSION_KEY)
