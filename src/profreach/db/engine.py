import logging
from functools import lru_cache

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

from profreach.config import Settings, get_settings
from profreach.db.models import (
    EmailDraft,
    OutreachRun,
    Professor,
    ProfessorPublication,
    ProfessorResearch,
    SendLog,
    TargetUniversity,
    UserProfile,
)

logger = logging.getLogger(__name__)
_TABLES = [
    UserProfile,
    TargetUniversity,
    Professor,
    ProfessorResearch,
    ProfessorPublication,
    EmailDraft,
    SendLog,
    OutreachRun,
]


@lru_cache
def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


def initialize_database(engine: Engine | None = None, settings: Settings | None = None) -> None:
    database_engine = engine or get_engine((settings or get_settings()).database_url)
    SQLModel.metadata.create_all(database_engine, tables=[model.__table__ for model in _TABLES])
    _add_missing_columns(database_engine)


def _add_missing_columns(engine: Engine) -> None:
    """Add nullable columns introduced after a table was first created."""
    inspector = inspect(engine)
    with engine.begin() as connection:
        for model in _TABLES:
            table = model.__table__
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or column.primary_key:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                logger.info("Adding column %s.%s", table.name, column.name)
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}')
                )
