import random
from datetime import UTC, datetime

from fakes import FakeGmailClient
from langgraph.checkpoint.memory import InMemorySaver

from profreach.agents.gmail_agent import GmailAgent
from profreach.agents.research_agent import ResearchResult
from profreach.agents.writer_agent import EmailComposition, WriterError
from profreach.config import Settings
from profreach.db.models import DraftStatus, ResearchStatus
from profreach.db.outreach_store import OutreachStore
from profreach.db.profile_store import ProfileStore
from profreach.graph.outreach import OutreachRunner, OutreachServices
from profreach.tools.scholarly import Paper


class FakeResearch:
    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.calls = []

    def research(self, professor, target, interests):
        self.calls.append(professor.name)
        if professor.name in self.fail_for:
            raise RuntimeError("provider crashed")
        return ResearchResult(
            status=ResearchStatus.DONE,
            papers=[Paper(title=f"Paper By {professor.name}", year=2025, source="openalex")],
        )


class FakeWriter:
    def write(self, profile, professor, research, papers, degree, start_term, **kwargs):
        if professor.name == "Writer Fails":
            raise WriterError("could not comply")
        return EmailComposition(
            subject=f"PhD inquiry for {professor.name}",
            body="Body",
            cited_paper_titles=[papers[0].title],
            match_note="Match",
        )


def build(engine, research, client, settings):
    store = OutreachStore(engine)
    services = OutreachServices(
        store=store,
        profile_store=ProfileStore(engine),
        research_agent=lambda: research,
        writer=FakeWriter,
        gmail_agent=lambda: GmailAgent(
            store,
            client,
            settings=settings,
            clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
            sleep=lambda seconds: None,
            rng=random.Random(0),
        ),
        settings=settings,
    )
    return OutreachRunner(services, InMemorySaver()), store


def test_pipeline_pauses_before_send_and_sends_only_approved(
    engine, target, make_professor, profile
):
    first = make_professor("Avery Chen", "avery@example.edu")
    second = make_professor("Blake Diaz", "blake@example.edu")
    broken = make_professor("Casey Park", "casey@example.edu")
    settings = Settings(send_min_gap_seconds=0, send_max_gap_seconds=0)
    client = FakeGmailClient()
    runner, store = build(engine, FakeResearch(fail_for={"Casey Park"}), client, settings)

    thread_id = runner.start(target.id, [first.id, second.id, broken.id], "Fall 2027")

    assert runner.is_waiting_to_send(thread_id)
    assert client.sent == []
    state = runner.state(thread_id)
    assert state["research"][str(broken.id)] == "failed"
    assert state["written"] == {
        str(first.id): "written",
        str(second.id): "written",
        str(broken.id): "no_verified_papers",
    }
    assert len(client.drafts) == 2

    drafts = {professor.name: draft for draft, professor in store.list_drafts()}
    store.update_draft(drafts["Avery Chen"].id, status=DraftStatus.APPROVED)

    runner.resume_send(thread_id)

    assert client.sent == ["avery@example.edu"]
    assert runner.is_waiting_to_send(thread_id)
    assert store.list_runs()[0].stage == "awaiting_approval"

    store.update_draft(drafts["Blake Diaz"].id, status=DraftStatus.REJECTED)
    runner.resume_send(thread_id)

    assert client.sent == ["avery@example.edu"]
    assert not runner.is_waiting_to_send(thread_id)
    assert store.list_runs()[0].stage == "complete"


def test_rerun_uses_cache_and_keeps_existing_drafts(engine, target, make_professor, profile):
    professor = make_professor()
    research = FakeResearch()
    settings = Settings(research_cache_days=30)
    runner, store = build(engine, research, FakeGmailClient(), settings)

    runner.start(target.id, [professor.id], None, create_gmail_drafts=False)
    second = runner.start(target.id, [professor.id], None, create_gmail_drafts=False)

    assert research.calls == ["Avery Chen"]
    assert runner.state(second)["written"] == {str(professor.id): "existing"}
    assert len(store.list_drafts()) == 1
