"""Wire real providers into agents and the outreach graph."""

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from sqlalchemy.engine import Engine

from profreach.agents.followup_agent import FollowUpAgent
from profreach.agents.gmail_agent import GmailAgent
from profreach.agents.research_agent import ResearchAgent
from profreach.agents.writer_agent import WriterAgent
from profreach.config import Settings
from profreach.db.outreach_store import OutreachStore
from profreach.db.profile_store import ProfileStore
from profreach.gmail.auth import build_gmail_service, load_credentials
from profreach.gmail.client import Attachment, GmailClient
from profreach.graph.outreach import OutreachRunner, OutreachServices, sqlite_checkpointer


def gmail_client(settings: Settings, allow_browser_flow: bool = False) -> GmailClient:
    credentials = load_credentials(
        settings.gmail_credentials_path,
        settings.gmail_token_path,
        allow_browser_flow=allow_browser_flow,
    )
    return GmailClient(build_gmail_service(credentials))


def gmail_agent(
    settings: Settings,
    engine: Engine,
    attachment: Attachment | None = None,
) -> GmailAgent:
    return GmailAgent(
        OutreachStore(engine),
        gmail_client(settings),
        settings=settings,
        attachment=attachment,
    )


def followup_agent(settings: Settings, engine: Engine) -> FollowUpAgent:
    return FollowUpAgent(
        OutreachStore(engine),
        gmail_client(settings),
        WriterAgent(settings),
        settings=settings,
    )


@lru_cache
def _checkpointer(path: str) -> Any:
    return sqlite_checkpointer(path)


def outreach_runner(
    settings: Settings,
    engine: Engine,
    progress: Callable[[str], None] | None = None,
    attachment: Attachment | None = None,
) -> OutreachRunner:
    services = OutreachServices(
        store=OutreachStore(engine),
        profile_store=ProfileStore(engine),
        research_agent=lambda: ResearchAgent(settings),
        writer=lambda: WriterAgent(settings),
        gmail_agent=lambda: gmail_agent(settings, engine, attachment),
        settings=settings,
        progress=progress or (lambda message: None),
    )
    return OutreachRunner(services, _checkpointer(settings.checkpoint_db_path))
