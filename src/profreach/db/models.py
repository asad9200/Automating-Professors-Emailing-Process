from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class DegreeSeeking(StrEnum):
    PHD = "PhD"
    MS = "MS"
    POSTDOC = "Postdoc"
    RESEARCH_ASSISTANT = "Research Assistant"


class EmailConfidence(StrEnum):
    FOUND_ON_PAGE = "found_on_page"
    DECODED = "decoded"
    MISSING = "missing"


class Education(BaseModel):
    model_config = ConfigDict(extra="forbid")

    institution: str | None = None
    degree: str | None = None
    field: str | None = None
    graduation_year: int | None = None
    details: str | None = None


class Publication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    year: int | None = None
    venue: str | None = None
    doi: str | None = None


class ResearchExperience(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    organization: str | None = None
    dates: str | None = None
    description: str | None = None


class UserProfileData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    education: list[Education] = Field(default_factory=list)
    gpa: str | None = None
    publications: list[Publication] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    research_experience: list[ResearchExperience] = Field(default_factory=list)
    research_interests: list[str] = Field(default_factory=list)
    target_degree: DegreeSeeking | None = None


class UserProfile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str | None = None
    education: list[Education] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    gpa: str | None = None
    publications: list[Publication] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    skills: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    research_experience: list[ResearchExperience] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    research_interests: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    target_degree: DegreeSeeking | None = None
    cv_filename: str | None = None
    # Extra facts the email writer may use (projects, work, field experience, languages).
    email_facts: str | None = None
    # Exact sign-off that ends every email: closing line, name, and optional contact lines.
    email_signature: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TargetUniversityData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    university_name: str
    location: str
    department: str
    field_of_study: str
    research_interests: list[str] = Field(default_factory=list)
    target_degree: DegreeSeeking


class TargetUniversity(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    university_name: str
    location: str
    department: str
    field_of_study: str
    research_interests: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    target_degree: DegreeSeeking
    official_domain: str | None = None
    department_url: str | None = None
    faculty_directory_url: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Professor(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    university_id: int = Field(foreign_key="targetuniversity.id", index=True)
    name: str
    title: str | None = None
    email: str | None = None
    email_source_url: str | None = None
    email_confidence: EmailConfidence = EmailConfidence.MISSING
    profile_url: str | None = None
    lab_url: str | None = None
    research_areas: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    relevance_score: int = Field(default=0, ge=0, le=100)
    relevance_reason: str = ""
    source_url: str
    selected: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    NO_VERIFIED_AUTHOR = "no_verified_author"
    FAILED = "failed"


class OpenPositions(StrEnum):
    MENTIONED = "mentioned"
    NOT_ACCEPTING = "not_accepting"
    UNKNOWN = "unknown"


class DraftStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    REPLIED = "replied"
    REJECTED = "rejected"


class ProfessorResearch(SQLModel, table=True):
    """Cached research enrichment for one professor."""

    id: int | None = Field(default=None, primary_key=True)
    professor_id: int = Field(foreign_key="professor.id", index=True, unique=True)
    status: ResearchStatus = ResearchStatus.PENDING
    themes: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    open_positions: OpenPositions = OpenPositions.UNKNOWN
    open_positions_evidence: str | None = None
    open_positions_url: str | None = None
    lab_page_excerpt: str | None = None
    contact_instructions: str | None = None
    openalex_author_id: str | None = None
    semantic_scholar_author_id: str | None = None
    sources: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    error: str | None = None
    fetched_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProfessorPublication(SQLModel, table=True):
    """A publication by a discovered professor (the spec's Publication record)."""

    __tablename__ = "publication"

    id: int | None = Field(default=None, primary_key=True)
    professor_id: int = Field(foreign_key="professor.id", index=True)
    title: str
    year: int | None = None
    venue: str | None = None
    abstract_summary: str | None = None
    url: str | None = None
    doi: str | None = None
    citation_count: int | None = None
    authors: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    source: str
    rank: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EmailDraft(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    professor_id: int = Field(foreign_key="professor.id", index=True)
    status: DraftStatus = Field(default=DraftStatus.DRAFT, index=True)
    subject: str
    body: str
    match_note: str = ""
    cited_paper_titles: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    start_term: str | None = None
    attach_cv: bool = False
    is_followup: bool = False
    parent_draft_id: int | None = Field(default=None, foreign_key="emaildraft.id")
    gmail_draft_id: str | None = None
    gmail_synced_hash: str | None = None
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    rfc_message_id: str | None = None
    last_error: str | None = None
    sent_at: datetime | None = None
    replied_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SendAction(StrEnum):
    GMAIL_DRAFT_CREATED = "gmail_draft_created"
    GMAIL_DRAFT_UPDATED = "gmail_draft_updated"
    SENT = "sent"
    FAILED = "failed"
    REPLY_DETECTED = "reply_detected"
    FOLLOWUP_DRAFTED = "followup_drafted"


class SendLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    draft_id: int = Field(foreign_key="emaildraft.id", index=True)
    action: SendAction
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    recipient: str | None = None
    detail: str | None = None
    next_send_after: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


class OutreachRun(SQLModel, table=True):
    """One LangGraph outreach thread; its checkpoint lives in the checkpoint database."""

    id: int | None = Field(default=None, primary_key=True)
    target_id: int = Field(foreign_key="targetuniversity.id", index=True)
    thread_id: str = Field(index=True, unique=True)
    professor_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    start_term: str | None = None
    stage: str = "started"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
