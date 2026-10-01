from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, inspect, text

from profreach.agents.research_agent import ResearchResult
from profreach.db.engine import initialize_database
from profreach.db.models import DraftStatus, ResearchStatus, SendAction
from profreach.db.outreach_store import OutreachStore
from profreach.tools.scholarly import Paper


def result(titles, status=ResearchStatus.DONE):
    return ResearchResult(
        status=status,
        themes=["Robotics"],
        papers=[Paper(title=title, year=2025, source="openalex") for title in titles],
    )


def test_research_cache_replaces_papers_and_tracks_freshness(engine, make_professor):
    store = OutreachStore(engine)
    professor = make_professor()

    store.save_research(professor.id, result(["First Paper Title", "Second Paper Title"]))
    assert store.research_is_fresh(professor.id, 30)
    assert [paper.title for paper in store.list_publications(professor.id)] == [
        "First Paper Title",
        "Second Paper Title",
    ]

    store.save_research(professor.id, result(["Third Paper Title"]))
    assert [paper.title for paper in store.list_publications(professor.id)] == ["Third Paper Title"]

    store.save_research(professor.id, result([], status=ResearchStatus.NO_VERIFIED_AUTHOR))
    assert len(store.list_publications(professor.id)) == 1
    assert not store.research_is_fresh(professor.id, 30)


def test_drafts_filters_and_send_counts(engine, make_professor):
    store = OutreachStore(engine)
    professor = make_professor()
    draft = store.create_draft(professor.id, subject="S", body="B")
    store.create_draft(professor.id, subject="S2", body="B2", status=DraftStatus.REJECTED)

    assert store.active_draft(professor.id).id == draft.id
    assert len(store.list_drafts(statuses={DraftStatus.DRAFT})) == 1
    assert store.list_drafts(professor_ids=[]) == []

    now = datetime.now(UTC)
    store.add_log(draft.id, SendAction.SENT, created_at=now - timedelta(hours=30))
    store.add_log(draft.id, SendAction.SENT, created_at=now - timedelta(hours=1))
    store.add_log(draft.id, SendAction.FAILED, created_at=now)

    assert store.sent_count_since(now - timedelta(hours=24)) == 1


def test_initialize_database_adds_columns_to_existing_tables():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE userprofile (id INTEGER PRIMARY KEY, name VARCHAR)"))

    initialize_database(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("userprofile")}
    assert {"skills", "target_degree", "cv_filename"} <= columns
    assert {"emaildraft", "sendlog", "publication", "professorresearch"} <= set(
        inspect(engine).get_table_names()
    )
