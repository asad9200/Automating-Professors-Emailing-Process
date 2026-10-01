from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, func, select

from profreach.db.engine import get_engine
from profreach.db.models import (
    DraftStatus,
    EmailDraft,
    OutreachRun,
    Professor,
    ProfessorPublication,
    ProfessorResearch,
    ResearchStatus,
    SendAction,
    SendLog,
    TargetUniversity,
)

if TYPE_CHECKING:
    from profreach.agents.research_agent import ResearchResult


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; every stored value is UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class OutreachStore:
    def __init__(self, engine: Engine | None = None):
        self.engine = engine or get_engine()

    # Professors and targets

    def get_target(self, target_id: int) -> TargetUniversity | None:
        with Session(self.engine) as session:
            return session.get(TargetUniversity, target_id)

    def get_professor(self, professor_id: int) -> Professor | None:
        with Session(self.engine) as session:
            return session.get(Professor, professor_id)

    def selected_professors(self, target_id: int) -> list[Professor]:
        with Session(self.engine) as session:
            statement = (
                select(Professor)
                .where(Professor.university_id == target_id, Professor.selected)
                .order_by(Professor.relevance_score.desc(), Professor.name)
            )
            return list(session.exec(statement).all())

    # Research cache

    def get_research(self, professor_id: int) -> ProfessorResearch | None:
        with Session(self.engine) as session:
            statement = select(ProfessorResearch).where(
                ProfessorResearch.professor_id == professor_id
            )
            return session.exec(statement).first()

    def research_is_fresh(self, professor_id: int, max_age_days: int) -> bool:
        research = self.get_research(professor_id)
        if research is None or research.status is not ResearchStatus.DONE:
            return False
        fetched_at = as_utc(research.fetched_at)
        return bool(fetched_at and datetime.now(UTC) - fetched_at < timedelta(days=max_age_days))

    def list_publications(self, professor_id: int) -> list[ProfessorPublication]:
        with Session(self.engine) as session:
            statement = (
                select(ProfessorPublication)
                .where(ProfessorPublication.professor_id == professor_id)
                .order_by(ProfessorPublication.rank)
            )
            return list(session.exec(statement).all())

    def save_research(self, professor_id: int, result: ResearchResult) -> ProfessorResearch:
        now = datetime.now(UTC)
        with Session(self.engine) as session:
            research = session.exec(
                select(ProfessorResearch).where(ProfessorResearch.professor_id == professor_id)
            ).first() or ProfessorResearch(professor_id=professor_id)
            payload = result.model_dump(exclude={"papers"})
            for field, value in payload.items():
                setattr(research, field, value)
            research.fetched_at = now
            research.updated_at = now
            session.add(research)
            existing = session.exec(
                select(ProfessorPublication).where(
                    ProfessorPublication.professor_id == professor_id
                )
            ).all()
            # Keep previously cached papers when a refresh fails to verify the author.
            if result.papers or result.status is ResearchStatus.DONE:
                for publication in existing:
                    session.delete(publication)
                for rank, paper in enumerate(result.papers):
                    session.add(
                        ProfessorPublication(
                            professor_id=professor_id,
                            rank=rank,
                            **paper.model_dump(),
                        )
                    )
            session.commit()
            session.refresh(research)
            return research

    def mark_research_failed(self, professor_id: int, error: str) -> None:
        with Session(self.engine) as session:
            research = session.exec(
                select(ProfessorResearch).where(ProfessorResearch.professor_id == professor_id)
            ).first() or ProfessorResearch(professor_id=professor_id)
            research.status = ResearchStatus.FAILED
            research.error = error
            research.updated_at = datetime.now(UTC)
            session.add(research)
            session.commit()

    # Drafts

    def create_draft(self, professor_id: int, **fields: Any) -> EmailDraft:
        with Session(self.engine) as session:
            draft = EmailDraft(professor_id=professor_id, **fields)
            session.add(draft)
            session.commit()
            session.refresh(draft)
            return draft

    def update_draft(self, draft_id: int, **fields: Any) -> EmailDraft:
        with Session(self.engine) as session:
            draft = session.get(EmailDraft, draft_id)
            if draft is None:
                raise KeyError(f"Draft {draft_id} does not exist")
            for field, value in fields.items():
                setattr(draft, field, value)
            draft.updated_at = datetime.now(UTC)
            session.add(draft)
            session.commit()
            session.refresh(draft)
            return draft

    def get_draft(self, draft_id: int) -> EmailDraft | None:
        with Session(self.engine) as session:
            return session.get(EmailDraft, draft_id)

    def active_draft(self, professor_id: int, followup: bool = False) -> EmailDraft | None:
        """Latest non-rejected draft for a professor, so pipeline reruns do not duplicate."""
        with Session(self.engine) as session:
            statement = (
                select(EmailDraft)
                .where(
                    EmailDraft.professor_id == professor_id,
                    EmailDraft.is_followup == followup,
                    EmailDraft.status != DraftStatus.REJECTED,
                )
                .order_by(EmailDraft.id.desc())
            )
            return session.exec(statement).first()

    def list_drafts(
        self,
        target_id: int | None = None,
        statuses: set[DraftStatus] | None = None,
        professor_ids: list[int] | None = None,
    ) -> list[tuple[EmailDraft, Professor]]:
        with Session(self.engine) as session:
            statement = select(EmailDraft, Professor).join(
                Professor, EmailDraft.professor_id == Professor.id
            )
            if target_id is not None:
                statement = statement.where(Professor.university_id == target_id)
            if statuses:
                statement = statement.where(col(EmailDraft.status).in_(statuses))
            if professor_ids is not None:
                statement = statement.where(col(EmailDraft.professor_id).in_(professor_ids))
            statement = statement.order_by(Professor.relevance_score.desc(), EmailDraft.id)
            return [(draft, professor) for draft, professor in session.exec(statement).all()]

    def followup_of(self, draft_id: int) -> EmailDraft | None:
        with Session(self.engine) as session:
            statement = select(EmailDraft).where(EmailDraft.parent_draft_id == draft_id)
            return session.exec(statement).first()

    # Send log

    def add_log(self, draft_id: int, action: SendAction, **fields: Any) -> SendLog:
        with Session(self.engine) as session:
            log = SendLog(draft_id=draft_id, action=action, **fields)
            session.add(log)
            session.commit()
            session.refresh(log)
            return log

    def list_logs(self, limit: int = 200) -> list[SendLog]:
        with Session(self.engine) as session:
            statement = select(SendLog).order_by(SendLog.id.desc()).limit(limit)
            return list(session.exec(statement).all())

    def sent_count_since(self, since: datetime) -> int:
        with Session(self.engine) as session:
            statement = select(func.count()).where(
                SendLog.action == SendAction.SENT,
                SendLog.created_at >= since.astimezone(UTC),
            )
            return int(session.exec(statement).one())

    def last_send(self) -> SendLog | None:
        with Session(self.engine) as session:
            statement = (
                select(SendLog).where(SendLog.action == SendAction.SENT).order_by(SendLog.id.desc())
            )
            return session.exec(statement).first()

    # Pipeline runs

    def create_run(
        self,
        target_id: int,
        thread_id: str,
        professor_ids: list[int],
        start_term: str | None,
    ) -> OutreachRun:
        with Session(self.engine) as session:
            run = OutreachRun(
                target_id=target_id,
                thread_id=thread_id,
                professor_ids=professor_ids,
                start_term=start_term,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

    def update_run_stage(self, thread_id: str, stage: str) -> None:
        with Session(self.engine) as session:
            run = session.exec(
                select(OutreachRun).where(OutreachRun.thread_id == thread_id)
            ).first()
            if run:
                run.stage = stage
                run.updated_at = datetime.now(UTC)
                session.add(run)
                session.commit()

    def list_runs(self, target_id: int | None = None) -> list[OutreachRun]:
        with Session(self.engine) as session:
            statement = select(OutreachRun).order_by(OutreachRun.id.desc())
            if target_id is not None:
                statement = statement.where(OutreachRun.target_id == target_id)
            return list(session.exec(statement).all())
