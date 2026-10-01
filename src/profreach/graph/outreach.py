"""Outreach pipeline: research -> write -> Gmail drafts -> (human approval) -> send.

The compiled graph pauses before ``send`` on every pass. Approvals happen in the UI by
changing draft status in the database; resuming the thread sends approved drafts only.
"""

import logging
import operator
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from profreach.agents.gmail_agent import GmailAgent
from profreach.agents.research_agent import ResearchAgent
from profreach.agents.writer_agent import WriterAgent, WriterError
from profreach.config import Settings, get_settings
from profreach.db.models import DraftStatus
from profreach.db.outreach_store import OutreachStore
from profreach.db.profile_store import ProfileStore
from profreach.gmail.auth import GmailAuthError
from profreach.gmail.client import GmailError

logger = logging.getLogger(__name__)
_OPEN_STATUSES = {DraftStatus.DRAFT, DraftStatus.APPROVED}


class OutreachState(TypedDict, total=False):
    target_id: int
    professor_ids: list[int]
    start_term: str | None
    attach_cv: bool
    force_research: bool
    create_gmail_drafts: bool
    send_limit: int | None
    wait_between_sends: bool
    research: dict[str, str]
    written: dict[str, str]
    gmail_sync: dict[str, Any]
    sent: Annotated[list[int], operator.add]
    errors: Annotated[list[str], operator.add]


@dataclass
class OutreachServices:
    store: OutreachStore
    profile_store: ProfileStore
    research_agent: Callable[[], ResearchAgent]
    writer: Callable[[], WriterAgent]
    gmail_agent: Callable[[], GmailAgent] | None = None
    settings: Settings = field(default_factory=get_settings)
    progress: Callable[[str], None] = lambda message: None


def build_outreach_graph(services: OutreachServices) -> StateGraph:
    store = services.store

    def research(state: OutreachState) -> dict:
        logger.info("Starting research node for %s professors", len(state["professor_ids"]))
        target = store.get_target(state["target_id"])
        profile = services.profile_store.get_profile()
        interests = list(profile.research_interests if profile else [])
        interests += target.research_interests if target else []
        results: dict[str, str] = {}
        errors: list[str] = []
        agent: ResearchAgent | None = None
        for professor_id in state["professor_ids"]:
            professor = store.get_professor(professor_id)
            if professor is None or target is None:
                continue
            if not state.get("force_research") and store.research_is_fresh(
                professor_id, services.settings.research_cache_days
            ):
                results[str(professor_id)] = "cached"
                continue
            services.progress(f"Researching {professor.name}")
            try:
                agent = agent or services.research_agent()
                result = agent.research(professor, target, interests)
                store.save_research(professor_id, result)
                results[str(professor_id)] = result.status.value
            except Exception as error:
                logger.exception("Research failed for professor %s", professor_id)
                store.mark_research_failed(professor_id, type(error).__name__)
                results[str(professor_id)] = "failed"
                errors.append(f"Research failed for {professor.name}.")
        logger.info("Completed research node")
        return {"research": results, "errors": errors}

    def write(state: OutreachState) -> dict:
        logger.info("Starting writer node")
        profile = services.profile_store.get_profile()
        target = store.get_target(state["target_id"])
        if profile is None or target is None:
            return {"written": {}, "errors": ["Save your profile before writing emails."]}
        results: dict[str, str] = {}
        errors: list[str] = []
        writer: WriterAgent | None = None
        colleagues = [
            professor.name
            for professor in (store.get_professor(pid) for pid in state["professor_ids"])
            if professor is not None
        ]
        for professor_id in state["professor_ids"]:
            professor = store.get_professor(professor_id)
            if professor is None:
                continue
            if store.active_draft(professor_id):
                results[str(professor_id)] = "existing"
                continue
            papers = store.list_publications(professor_id)
            if not papers:
                results[str(professor_id)] = "no_verified_papers"
                errors.append(f"No verified papers for {professor.name}; email not written.")
                continue
            services.progress(f"Writing email to {professor.name}")
            try:
                writer = writer or services.writer()
                composition = writer.write(
                    profile,
                    professor,
                    store.get_research(professor_id),
                    papers,
                    target.target_degree,
                    state.get("start_term"),
                    attach_cv=bool(state.get("attach_cv")),
                    target=target,
                    colleagues=colleagues,
                )
                store.create_draft(
                    professor_id,
                    subject=composition.subject,
                    body=composition.body,
                    match_note=composition.match_note,
                    cited_paper_titles=composition.cited_paper_titles,
                    start_term=state.get("start_term"),
                    attach_cv=bool(state.get("attach_cv")),
                )
                results[str(professor_id)] = "written"
            except WriterError as error:
                results[str(professor_id)] = "failed"
                errors.append(f"{professor.name}: {error}")
            except Exception:
                logger.exception("Writing failed for professor %s", professor_id)
                results[str(professor_id)] = "failed"
                errors.append(f"Writing failed for {professor.name}.")
        logger.info("Completed writer node")
        return {"written": results, "errors": errors}

    def gmail_drafts(state: OutreachState) -> dict:
        if not state.get("create_gmail_drafts") or services.gmail_agent is None:
            return {"gmail_sync": {"skipped": True}}
        logger.info("Starting Gmail draft node")
        services.progress("Saving Gmail drafts")
        try:
            report = services.gmail_agent().sync_drafts(
                store.list_drafts(statuses=_OPEN_STATUSES, professor_ids=state["professor_ids"])
            )
        except (GmailAuthError, GmailError) as error:
            return {"gmail_sync": {"error": str(error)}, "errors": [str(error)]}
        return {"gmail_sync": report.model_dump(mode="json")}

    def send(state: OutreachState) -> dict:
        logger.info("Starting send node")
        if services.gmail_agent is None:
            return {"errors": ["Gmail is not configured."]}
        try:
            report = services.gmail_agent().send_approved(
                professor_ids=state["professor_ids"],
                limit=state.get("send_limit"),
                wait=bool(state.get("wait_between_sends")),
                progress=services.progress,
            )
        except (GmailAuthError, GmailError) as error:
            return {"errors": [str(error)]}
        errors = [f"Draft {draft_id}: {reason}" for draft_id, reason in report.failed.items()]
        if report.stopped_reason:
            services.progress(report.stopped_reason)
        return {"sent": report.sent, "errors": errors}

    def after_send(state: OutreachState) -> str:
        remaining = store.list_drafts(statuses=_OPEN_STATUSES, professor_ids=state["professor_ids"])
        return "send" if remaining else END

    graph = StateGraph(OutreachState)
    graph.add_node("research", research)
    graph.add_node("write", write)
    graph.add_node("gmail_drafts", gmail_drafts)
    graph.add_node("send", send)
    graph.add_edge(START, "research")
    graph.add_edge("research", "write")
    graph.add_edge("write", "gmail_drafts")
    graph.add_edge("gmail_drafts", "send")
    graph.add_conditional_edges("send", after_send, ["send", END])
    return graph


def sqlite_checkpointer(path: str) -> Any:
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


class OutreachRunner:
    def __init__(self, services: OutreachServices, checkpointer: Any):
        self.services = services
        self.graph = build_outreach_graph(services).compile(
            checkpointer=checkpointer,
            interrupt_before=["send"],
        )

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def start(
        self,
        target_id: int,
        professor_ids: list[int],
        start_term: str | None,
        attach_cv: bool = False,
        force_research: bool = False,
        create_gmail_drafts: bool = True,
    ) -> str:
        thread_id = f"target-{target_id}-{uuid.uuid4().hex[:8]}"
        self.services.store.create_run(target_id, thread_id, professor_ids, start_term)
        self.graph.invoke(
            {
                "target_id": target_id,
                "professor_ids": professor_ids,
                "start_term": start_term,
                "attach_cv": attach_cv,
                "force_research": force_research,
                "create_gmail_drafts": create_gmail_drafts,
                "send_limit": None,
                "wait_between_sends": False,
                "sent": [],
                "errors": [],
            },
            self._config(thread_id),
        )
        self._record_stage(thread_id)
        return thread_id

    def resume_send(self, thread_id: str, limit: int | None = None, wait: bool = False) -> dict:
        """Continue a paused run: sends approved drafts only, then pauses again if needed."""
        config = self._config(thread_id)
        if not self.is_waiting_to_send(thread_id):
            return self.state(thread_id)
        self.graph.update_state(config, {"send_limit": limit, "wait_between_sends": wait})
        if not self.is_waiting_to_send(thread_id):
            self._record_stage(thread_id)
            return self.state(thread_id)
        self.graph.invoke(None, config)
        self._record_stage(thread_id)
        return self.state(thread_id)

    def is_waiting_to_send(self, thread_id: str) -> bool:
        return "send" in self.graph.get_state(self._config(thread_id)).next

    def state(self, thread_id: str) -> dict:
        return dict(self.graph.get_state(self._config(thread_id)).values)

    def _record_stage(self, thread_id: str) -> None:
        stage = "awaiting_approval" if self.is_waiting_to_send(thread_id) else "complete"
        self.services.store.update_run_stage(thread_id, stage)
