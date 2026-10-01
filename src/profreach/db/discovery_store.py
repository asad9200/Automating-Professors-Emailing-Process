from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from profreach.agents.university_agent import UniversityResolution
from profreach.db.engine import get_engine
from profreach.db.models import (
    EmailConfidence,
    Professor,
    TargetUniversity,
    TargetUniversityData,
)

if TYPE_CHECKING:
    from profreach.agents.professor_discovery_agent import DiscoveredProfessor


class DiscoveryStore:
    def __init__(self, engine: Engine | None = None):
        self.engine = engine or get_engine()

    def list_targets(self) -> list[TargetUniversity]:
        with Session(self.engine) as session:
            statement = select(TargetUniversity).order_by(TargetUniversity.created_at.desc())
            return list(session.exec(statement).all())

    def get_target(self, target_id: int) -> TargetUniversity | None:
        with Session(self.engine) as session:
            return session.get(TargetUniversity, target_id)

    def save_target(
        self,
        data: TargetUniversityData,
        resolution: UniversityResolution,
    ) -> TargetUniversity:
        payload = data.model_dump(mode="json")
        with Session(self.engine) as session:
            statement = select(TargetUniversity).where(
                TargetUniversity.university_name == data.university_name,
                TargetUniversity.location == data.location,
                TargetUniversity.department == data.department,
            )
            existing = session.exec(statement).first()
            if existing:
                for field, value in payload.items():
                    setattr(existing, field, value)
                existing.official_domain = resolution.official_domain
                existing.department_url = resolution.department_url
                existing.faculty_directory_url = resolution.faculty_directory_url
                existing.updated_at = datetime.now(UTC)
                target = existing
            else:
                target = TargetUniversity(
                    **payload,
                    official_domain=resolution.official_domain,
                    department_url=resolution.department_url,
                    faculty_directory_url=resolution.faculty_directory_url,
                )
            session.add(target)
            session.commit()
            session.refresh(target)
            return target

    def update_directory(self, target_id: int, official_domain: str, directory_url: str) -> None:
        """Apply a faculty directory URL the user supplied after checking it themselves."""
        with Session(self.engine) as session:
            target = session.get(TargetUniversity, target_id)
            if target is None:
                raise KeyError(f"Target {target_id} does not exist")
            target.official_domain = official_domain
            target.faculty_directory_url = directory_url
            target.department_url = directory_url
            target.updated_at = datetime.now(UTC)
            session.add(target)
            session.commit()

    def list_professors(self, target_id: int) -> list[Professor]:
        with Session(self.engine) as session:
            statement = (
                select(Professor)
                .where(Professor.university_id == target_id)
                .order_by(Professor.relevance_score.desc(), Professor.name)
            )
            return list(session.exec(statement).all())

    def save_professors(
        self,
        target_id: int,
        professors: list[DiscoveredProfessor],
    ) -> list[Professor]:
        with Session(self.engine) as session:
            statement = select(Professor).where(Professor.university_id == target_id)
            existing_professors = list(session.exec(statement).all())
            for candidate in professors:
                existing = next(
                    (
                        professor
                        for professor in existing_professors
                        if (
                            candidate.profile_url and professor.profile_url == candidate.profile_url
                        )
                        or professor.name.casefold() == candidate.name.casefold()
                    ),
                    None,
                )
                payload = candidate.model_dump()
                payload["email_confidence"] = EmailConfidence(payload["email_confidence"])
                payload["university_id"] = target_id
                if existing:
                    for field, value in payload.items():
                        if field in {
                            "selected",
                            "created_at",
                            "updated_at",
                            "email",
                            "email_source_url",
                            "email_confidence",
                        }:
                            continue
                        if value not in (None, [], ""):
                            setattr(existing, field, value)
                    if candidate.email:
                        existing.email = candidate.email
                        existing.email_source_url = candidate.email_source_url
                        existing.email_confidence = EmailConfidence(candidate.email_confidence)
                    existing.updated_at = datetime.now(UTC)
                else:
                    existing = Professor(**payload)
                    session.add(existing)
                    existing_professors.append(existing)
            session.commit()
        return self.list_professors(target_id)

    def update_selection(self, target_id: int, selected_ids: set[int]) -> None:
        with Session(self.engine) as session:
            statement = select(Professor).where(Professor.university_id == target_id)
            for professor in session.exec(statement).all():
                professor.selected = professor.id in selected_ids
                professor.updated_at = datetime.now(UTC)
            session.commit()
