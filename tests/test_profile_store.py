from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from profreach.db.engine import initialize_database
from profreach.db.models import DegreeSeeking, UserProfileData
from profreach.db.profile_store import ProfileStore


def test_profile_store_saves_and_updates_profile():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_database(engine)
    store = ProfileStore(engine)
    profile = UserProfileData(
        name="Avery Chen",
        skills=["Python"],
        target_degree=DegreeSeeking.PHD,
    )

    saved = store.save_profile(profile, cv_filename="avery-cv.pdf")
    loaded = store.get_profile()

    assert loaded is not None
    assert loaded.id == saved.id
    assert loaded.name == "Avery Chen"
    assert loaded.skills == ["Python"]
    assert loaded.target_degree is DegreeSeeking.PHD
    assert loaded.cv_filename == "avery-cv.pdf"

    updated = store.save_profile(profile.model_copy(update={"name": "Avery C."}))

    assert updated.id == saved.id
    assert updated.name == "Avery C."
    assert updated.cv_filename == "avery-cv.pdf"


def test_database_initializes_discovery_tables():
    engine = create_engine("sqlite://")

    initialize_database(engine)

    assert {"userprofile", "targetuniversity", "professor"} <= set(
        inspect(engine).get_table_names()
    )
