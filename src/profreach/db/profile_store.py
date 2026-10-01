from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from profreach.db.engine import get_engine
from profreach.db.models import UserProfile, UserProfileData


class ProfileStore:
    def __init__(self, engine: Engine | None = None):
        self.engine = engine or get_engine()

    def get_profile(self) -> UserProfile | None:
        with Session(self.engine) as session:
            statement = select(UserProfile).order_by(UserProfile.id.desc())
            return session.exec(statement).first()

    def save_profile(
        self,
        profile_data: UserProfileData,
        cv_filename: str | None = None,
    ) -> UserProfile:
        payload = profile_data.model_dump(mode="json")
        with Session(self.engine) as session:
            existing = session.exec(select(UserProfile).order_by(UserProfile.id.desc())).first()
            if existing:
                for field, value in payload.items():
                    setattr(existing, field, value)
                if cv_filename is not None:
                    existing.cv_filename = cv_filename
                existing.updated_at = datetime.now(UTC)
                profile = existing
            else:
                profile = UserProfile(
                    **payload,
                    cv_filename=cv_filename,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            session.add(profile)
            session.commit()
            session.refresh(profile)
            return profile

    def save_email_settings(self, email_facts: str | None, email_signature: str | None) -> None:
        """Store the writer-only facts and sign-off without touching CV-derived fields."""
        with Session(self.engine) as session:
            profile = session.exec(select(UserProfile).order_by(UserProfile.id.desc())).first()
            if profile is None:
                profile = UserProfile()
            profile.email_facts = (email_facts or "").strip() or None
            profile.email_signature = (email_signature or "").strip() or None
            profile.updated_at = datetime.now(UTC)
            session.add(profile)
            session.commit()
