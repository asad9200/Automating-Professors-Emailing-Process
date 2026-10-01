from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from profreach.agents.professor_discovery_agent import DiscoveredProfessor
from profreach.agents.university_agent import UniversityResolution
from profreach.db.discovery_store import DiscoveryStore
from profreach.db.engine import initialize_database
from profreach.db.models import DegreeSeeking, EmailConfidence, TargetUniversityData


def make_target():
    return TargetUniversityData(
        university_name="Example University",
        location="Example City",
        department="Computer Science",
        field_of_study="Computer Science",
        research_interests=["robotics"],
        target_degree=DegreeSeeking.PHD,
    )


def make_professor(email: str | None = "alex@example.edu"):
    return DiscoveredProfessor(
        name="Alex Rivera",
        title="Assistant Professor",
        email=email,
        email_source_url="https://cs.example.edu/people/alex" if email else None,
        email_confidence="found_on_page" if email else "missing",
        profile_url="https://cs.example.edu/people/alex",
        research_areas=["Robotics"],
        relevance_score=100,
        relevance_reason="Matches stated research terms: robotics.",
        source_url="https://cs.example.edu/faculty",
    )


def test_discovery_store_upserts_targets_and_preserves_professor_selection():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_database(engine)
    store = DiscoveryStore(engine)
    resolution = UniversityResolution(
        official_domain="example.edu",
        department_url="https://cs.example.edu/",
        faculty_directory_url="https://cs.example.edu/faculty",
    )

    saved_target = store.save_target(make_target(), resolution)
    updated_target = store.save_target(make_target(), resolution)

    assert updated_target.id == saved_target.id
    assert len(store.list_targets()) == 1
    assert updated_target.faculty_directory_url == resolution.faculty_directory_url

    professor = store.save_professors(saved_target.id, [make_professor()])[0]
    store.update_selection(saved_target.id, {professor.id})
    refreshed = store.save_professors(saved_target.id, [make_professor(email=None)])

    assert len(refreshed) == 1
    assert refreshed[0].id == professor.id
    assert refreshed[0].selected is True
    assert refreshed[0].email == "alex@example.edu"
    assert refreshed[0].email_confidence is EmailConfidence.FOUND_ON_PAGE
