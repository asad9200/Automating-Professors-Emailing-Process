import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from profreach.db.engine import initialize_database
from profreach.db.models import (
    DegreeSeeking,
    EmailConfidence,
    Professor,
    TargetUniversity,
    UserProfileData,
)
from profreach.db.profile_store import ProfileStore


@pytest.fixture
def engine():
    database = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_database(database)
    return database


@pytest.fixture
def target(engine):
    with Session(engine) as session:
        item = TargetUniversity(
            university_name="Example University",
            location="Example City",
            department="Computer Science",
            field_of_study="Computer Science",
            research_interests=["robot learning"],
            target_degree=DegreeSeeking.PHD,
            official_domain="example.edu",
            department_url="https://cs.example.edu/",
            faculty_directory_url="https://cs.example.edu/faculty",
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return item


@pytest.fixture
def make_professor(engine, target):
    def create(name="Avery Chen", email="avery@example.edu", selected=True, **fields):
        with Session(engine) as session:
            professor = Professor(
                university_id=target.id,
                name=name,
                title="Assistant Professor",
                email=email,
                email_source_url="https://cs.example.edu/people/avery" if email else None,
                email_confidence=(
                    EmailConfidence.FOUND_ON_PAGE if email else EmailConfidence.MISSING
                ),
                profile_url=f"https://cs.example.edu/people/{name.split()[0].lower()}",
                lab_url="https://robotics.example.edu/lab",
                research_areas=["Robotics"],
                relevance_score=80,
                relevance_reason="Matches robotics.",
                source_url="https://cs.example.edu/faculty",
                selected=selected,
                **fields,
            )
            session.add(professor)
            session.commit()
            session.refresh(professor)
            return professor

    return create


@pytest.fixture
def profile(engine):
    return ProfileStore(engine).save_profile(
        UserProfileData(
            name="Jordan Lee",
            education=[{"institution": "State University", "degree": "BS", "field": "CS"}],
            skills=["Python", "PyTorch"],
            research_interests=["robot learning"],
            target_degree=DegreeSeeking.PHD,
        )
    )
