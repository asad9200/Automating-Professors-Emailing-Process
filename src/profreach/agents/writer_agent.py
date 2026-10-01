import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from profreach.config import Settings, get_settings
from profreach.db.models import (
    DegreeSeeking,
    Professor,
    ProfessorPublication,
    ProfessorResearch,
    TargetUniversity,
    UserProfile,
)
from profreach.llm import LLMConfigurationError, create_chat_model, structured
from profreach.tools.scholarly import names_match

logger = logging.getLogger(__name__)

MIN_WORDS = 150
MAX_WORDS = 230
FOLLOWUP_MIN_WORDS = 50
FOLLOWUP_MAX_WORDS = 120

# Phrases the student never wants in an email, plus generic flattery.
BANNED_PHRASES = (
    "i hope this email finds you well",
    "i hope this message finds you well",
    "i am writing to express",
    "passionate",
    "delve",
    "resonates",
    "resonated",
    "aligns perfectly",
    "esteemed",
    "cutting-edge",
    "cutting edge",
    "groundbreaking",
    "ground-breaking",
    "leverage",
    "synergy",
    "i am confident that",
    "furthermore",
    "moreover",
    "in today's world",
    "i would be honored",
    "i would be honoured",
    "renowned",
    "world-class",
    "pioneering",
    "illustrious",
    "leading expert",
)
_PLACEHOLDER_RE = re.compile(
    r"\[[^\]]*(?:your|name|insert|university|date|intake)[^\]]*\]|\{\{|\bTBD\b|\bXX+\b", re.I
)
_REPOSITORY_RE = re.compile(
    r"\b(?:arxiv|biorxiv|medrxiv|chemrxiv|ssrn|zenodo|research square|preprints\.org|"
    r"cornell university)\b",
    re.I,
)
_MEETING_RE = re.compile(
    r"\b(?:a (?:quick |short |brief )?call|phone call|video call|meeting|meet with|zoom|skype|"
    r"collaborat\w*|schedule a|chat with)\b",
    re.I,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.M)
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿]")
_WEAK_OPENINGS = ("my name", "i am ", "i'm ", "i hope", "allow me", "i would like to introduce")
_TITLE_STOPWORDS = {
    "a", "an", "the", "of", "and", "for", "in", "on", "to", "with", "via", "by", "from",
    "using", "towards", "toward", "based", "into", "its", "their", "under", "through",
}  # fmt: skip
_DEGREE_PHRASES = {
    DegreeSeeking.PHD: "PhD",
    DegreeSeeking.MS: "Master's",
    DegreeSeeking.POSTDOC: "postdoctoral",
    DegreeSeeking.RESEARCH_ASSISTANT: "research assistant",
}
_DEGREE_TERMS = {
    "phd": ("phd", "ph d", "doctoral", "doctorate"),
    "ms": ("ms", "m s", "msc", "master", "masters", "master s"),
    "master s": ("ms", "msc", "master", "masters", "master s"),
    "postdoc": ("postdoc", "postdoctoral", "post doctoral"),
    "postdoctoral": ("postdoc", "postdoctoral", "post doctoral"),
    "research assistant": ("research assistant", "research assistantship"),
}

_SYSTEM_PROMPT = """# ROLE
You write short, personal emails from a prospective {degree} student to university professors.
Each email must read as if the student wrote it personally after genuinely reading the
professor's work: warm, specific, plain and honest. It must never read like a template or
like AI-generated text.

# STEP 1: CHOOSE AND CHECK THE WORK (before writing)
1. From VERIFIED PAPERS choose ONE recent paper (prefer the last 3 years) that best connects to
   the student's background. Every listed paper is already verified: the professor's name and
   university affiliation were matched, and when an author list is shown the professor is on it.
   Never use a paper that is not listed.
2. Use the venue exactly as given. When "is_preprint" is true or no venue is given, call it
   "your recent preprint" or "your recent paper"; never name a repository such as arXiv.
3. From the summary, pick ONE concrete idea, method or finding you can describe in plain words
   in a single sentence. Do not add claims about the paper beyond its summary.
4. Read CONTACT INSTRUCTIONS and the PAGE EXCERPT for instructions to prospective students
   (e.g. "apply through the portal first", "put a keyword in the subject line"). Follow them
   exactly and report them in contact_instructions_followed ("none found" if none).

# STEP 2: WRITE THE EMAIL
Greeting: "Dear Professor <Last name>,"

Paragraph 1, the professor's work (2-4 sentences): open directly with the professor's specific
work, not with the student's name. For example: "Your Nature Communications paper on a fully
autonomous thyroid ultrasound robot stayed with me because..." or "I came across your work on
three-way decisions in medical diagnosis, and one idea stood out...". Describe the one
concrete idea in plain language and say honestly why it caught the student's attention.

Paragraph 2, the student and the genuine connection (3-5 sentences): introduce the student
briefly (name, degree and university, grade or award if listed, and that they are applying
for a {degree} position for {intake}). Connect the professor's idea to ONE specific part of the
student's real work (a paper, project or experience) in concrete terms: what the student did,
what they found, what was missing. Mention the planned thesis only where it fits naturally.

Paragraph 3 (optional, 1-3 sentences): one personal or field experience, only if it adds
something real. If the field is new to the student, say so honestly in one sentence and show
willingness to learn.

Closing (1-2 lines): {cv_line}End with ONE simple yes/no question, such as "Are you considering
{degree} students for the {intake} intake?" or "Would you consider me as a {degree} student in
your group?". Do not ask for a call, meeting or collaboration.

Then end the email with exactly this sign-off, unchanged:
{signature}

Subject line: specific and short, naming the research topic, for example
"Prospective {degree} student - fairness for under-represented groups ({intake})". Follow any
subject-line rule from the professor's website.

# STYLE RULES
- {min_words}-{max_words} words, not counting the sign-off. Short paragraphs.
- Write the way a thoughtful student talks: natural, direct, modest. Contractions are fine.
- Vary sentence length. Avoid long, stacked clauses.
- Mention only ONE of the professor's papers. Use at most one number from the student's work.
- Never use these words or phrases: {banned}.
- No bullet points, no bold text, no emojis, no em dashes. Plain text only.
- Do not flatter. One honest sentence about why the work interested the student is enough.

# HONESTY RULES (strict)
- Use ONLY facts from STUDENT PROFILE and the professor data provided. Never invent papers,
  venues, results, skills, tools, roles or experiences.
- Never claim experience the student does not have.
- Describe each student item with the same category and wording as STUDENT PROFILE: a paper
  stays a paper (never call it a final-year project, coursework or thesis), and grades and
  awards are written exactly as listed (for example "CGPA 4.00/4.00", not "a perfect GPA").
- If a sentence assumes anything about the student to sound personal, list it in
  sentences_to_check so the student can check it.
- Do not stretch connections. If the link is weak, keep it modest.

# SELF-CHECK BEFORE ANSWERING (rewrite if any answer is no)
1. Does paragraph 1 open with the professor's specific work?
2. Is the paper one of the VERIFIED PAPERS?
3. Is every claim about the student found in STUDENT PROFILE?
4. Is the connection specific, not "this aligns with my interests"?
5. Is the email {min_words}-{max_words} words with no banned phrases?
6. Does it end with exactly one simple question and no call request?
7. Would a professor believe a real student wrote this personally?

# OUTPUT
subject: the subject line.
email: the full email from greeting to sign-off, ready to send.
paper_title: the exact title of the one paper used, copied from VERIFIED PAPERS.
sentences_to_check: sentences that assume something about the student (empty if none).
contact_instructions_followed: instructions found and how you followed them, or "none found".
"""


class WriterOutput(BaseModel):
    subject: str = Field(description="Specific, short subject line naming the research topic")
    email: str = Field(description="Full plain-text email from greeting to sign-off")
    paper_title: str = Field(description="Exact title of the one verified paper used")
    sentences_to_check: list[str] = Field(
        default_factory=list,
        description="Sentences that assume something about the student",
    )
    contact_instructions_followed: str = Field(
        default="none found",
        description="Instructions for prospective students found and how they were followed",
    )


class EmailComposition(BaseModel):
    subject: str
    body: str
    cited_paper_titles: list[str]
    match_note: str = Field(description="Notes for the student; never sent")


class FollowUpComposition(BaseModel):
    body: str = Field(description="Plain-text follow-up from greeting to sign-off")


class WriterError(RuntimeError):
    """Raised when a compliant email cannot be produced."""


def _plain_punctuation(text: str) -> str:
    """Replace typographic hyphens and spaces some models emit with plain characters."""
    for character in ("‐", "‑", "‒"):
        text = text.replace(character, "-")
    return text.replace(" ", " ").replace(" ", " ")


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def _normalize(text: str) -> str:
    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _mentions_degree(normalized_body: str, degree: str) -> bool:
    terms = _DEGREE_TERMS.get(_normalize(degree), (_normalize(degree),))
    padded = f" {normalized_body} "
    return any(f" {term} " in padded for term in terms)


def degree_phrase(degree: DegreeSeeking | str) -> str:
    if isinstance(degree, DegreeSeeking):
        return _DEGREE_PHRASES[degree]
    return str(degree)


def default_signature(profile: UserProfile) -> str:
    return profile.email_signature or f"Kind regards,\n{profile.name or ''}".strip()


def _without_signature(body: str, signature: str) -> str:
    stripped = body.rstrip()
    if signature and stripped.endswith(signature.strip()):
        return stripped[: -len(signature.strip())].rstrip()
    return stripped


def _title_words(title: str) -> set[str]:
    return {
        word for word in _normalize(title).split() if len(word) > 3 and word not in _TITLE_STOPWORDS
    }


# Hosting platforms that are not publication venues.
_HOSTING_VENUE_RE = re.compile(r"\b(?:underline|figshare|osf|researchgate|academia\.edu)\b", re.I)


def is_repository_venue(venue: str | None) -> bool:
    return not venue or bool(_REPOSITORY_RE.search(venue) or _HOSTING_VENUE_RE.search(venue))


def authored_by(paper: ProfessorPublication, professor_name: str) -> bool:
    """True unless the paper has an author list that does not include the professor."""
    authors = paper.authors or []
    return not authors or any(names_match(professor_name, author) for author in authors)


def _style_problems(body: str, signature: str, question_limit: int = 1) -> list[str]:
    problems: list[str] = []
    lowered = body.lower().replace("’", "'")
    banned = [phrase for phrase in BANNED_PHRASES if phrase in lowered]
    if banned:
        problems.append(f"Remove these banned phrases: {', '.join(banned)}.")
    if _PLACEHOLDER_RE.search(body):
        problems.append("Remove placeholder text.")
    if _BULLET_RE.search(body):
        problems.append("Do not use bullet points or numbered lists.")
    if "**" in body or "__" in body:
        problems.append("Do not use bold or other formatting.")
    if _EMOJI_RE.search(body):
        problems.append("Remove emojis.")
    if "—" in body:
        problems.append("Replace em dashes with commas or full stops.")
    if _MEETING_RE.search(_without_signature(body, signature)):
        problems.append("Do not ask for a call, meeting or collaboration.")
    questions = _without_signature(body, signature).count("?")
    if questions != question_limit:
        problems.append(
            f"The email has {questions} question marks; end with exactly one simple yes/no "
            "question and ask nothing else."
        )
    if signature and not body.rstrip().endswith(signature.strip()):
        problems.append(f"End the email with exactly this sign-off:\n{signature}")
    return problems


def validate_email(
    output: WriterOutput,
    papers: list[ProfessorPublication],
    degree: str,
    start_term: str | None,
    signature: str,
    student_name: str | None = None,
    attach_cv: bool = False,
    profile_text: str | None = None,
    gpa: str | None = None,
) -> list[str]:
    problems: list[str] = []
    body = output.email.strip()
    content = _without_signature(body, signature)
    words = word_count(content)
    if not output.subject.strip():
        problems.append("The subject line is empty.")
    elif len(output.subject) > 110:
        problems.append("Shorten the subject line to under 110 characters.")
    if _PLACEHOLDER_RE.search(output.subject):
        problems.append("Remove placeholder text from the subject.")
    if not MIN_WORDS <= words <= MAX_WORDS:
        problems.append(
            f"The email has {words} words before the sign-off; it must have "
            f"{MIN_WORDS}-{MAX_WORDS}."
        )

    known = {_normalize(paper.title): paper for paper in papers}
    paper = known.get(_normalize(output.paper_title))
    if paper is None:
        problems.append(
            f'"{output.paper_title}" is not one of the VERIFIED PAPERS; choose one and copy '
            "its exact title into paper_title."
        )
    else:
        words_in_title = _title_words(paper.title)
        shared = words_in_title & set(_normalize(content).split())
        if len(shared) < min(2, len(words_in_title)):
            problems.append("Describe the chosen paper's actual topic in paragraph 1.")
    if _REPOSITORY_RE.search(content):
        problems.append(
            'Never name a repository such as arXiv as a venue; say "your recent preprint".'
        )

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
    if not paragraphs or not paragraphs[0].lower().startswith(("dear professor", "dear dr")):
        problems.append('Start with "Dear Professor <Last name>,".')
    opening = paragraphs[1].lower() if len(paragraphs) > 1 else ""
    first_name = (student_name or "").split()[0].lower() if student_name else ""
    if opening.startswith(_WEAK_OPENINGS) or (first_name and opening.startswith(first_name)):
        problems.append(
            "Open paragraph 1 with the professor's specific work, not with the student."
        )

    problems += _style_problems(body, signature)
    normalized = _normalize(content)
    if not _mentions_degree(normalized, degree):
        problems.append(f"Say that the student is applying for a {degree} position.")
    if start_term and _normalize(start_term) not in normalized:
        problems.append(f"Mention the intended intake ({start_term}).")
    if attach_cv and "my cv is attached" not in normalized:
        problems.append('Include the line "My CV is attached." in the closing.')
    if not attach_cv and re.search(r"\battach(?:ed|ment)\b", content, re.I):
        problems.append("Do not say anything is attached; no CV is attached to this email.")
    problems += _invented_detail_problems(content, profile_text, gpa)
    return problems


# Labels models like to add that change what the student actually did.
_INVENTED_LABELS = (
    "final-year",
    "final year",
    "capstone",
    "senior project",
    "coursework",
    "dissertation",
    "summer school",
)


def _invented_detail_problems(content: str, profile_text: str | None, gpa: str | None) -> list:
    problems = []
    lowered = content.lower()
    profile_lowered = (profile_text or "").lower()
    if profile_text:
        invented = [
            label for label in _INVENTED_LABELS if label in lowered and label not in profile_lowered
        ]
        if invented:
            problems.append(
                "These labels are not in the student profile; describe the work as the profile "
                f"does: {', '.join(invented)}."
            )
    grade = re.search(r"\d+(?:\.\d+)?", gpa or "")
    if grade and re.search(r"\b(?:c?gpa|grade|perfect score)\b", lowered):
        if grade.group(0) not in content:
            problems.append(f"Write the grade exactly as in the profile ({gpa}).")
    return problems


def validate_followup(body: str, signature: str = "") -> list[str]:
    content = _without_signature(body, signature)
    problems = []
    words = word_count(content)
    if not FOLLOWUP_MIN_WORDS <= words <= FOLLOWUP_MAX_WORDS:
        problems.append(
            f"The follow-up has {words} words before the sign-off; it must have "
            f"{FOLLOWUP_MIN_WORDS}-{FOLLOWUP_MAX_WORDS}."
        )
    return problems + _style_problems(body, signature)


def applicant_facts(profile: UserProfile) -> dict[str, Any]:
    """Only non-empty profile fields, so the model sees exactly what the student stated."""
    data = profile.model_dump(
        mode="json",
        exclude={
            "id",
            "created_at",
            "updated_at",
            "cv_filename",
            "email_facts",
            "email_signature",
        },
        warnings=False,
    )

    def prune(value: Any) -> Any:
        if isinstance(value, dict):
            pruned = {key: prune(item) for key, item in value.items()}
            return {key: item for key, item in pruned.items() if item not in (None, "", [], {})}
        if isinstance(value, list):
            return [item for item in (prune(item) for item in value) if item not in (None, "", {})]
        return value

    facts = prune(data)
    if profile.email_facts:
        facts["additional_facts"] = profile.email_facts
    return facts


def _paper_record(paper: ProfessorPublication) -> dict[str, Any]:
    preprint = is_repository_venue(paper.venue)
    return {
        "title": paper.title,
        "year": paper.year,
        "venue": None if preprint else paper.venue,
        "is_preprint": preprint,
        "summary": paper.abstract_summary,
        "authors": (paper.authors or [])[:6],
    }


def _verification_note(
    research: ProfessorResearch | None,
    paper: ProfessorPublication,
    professor: Professor,
    university: str | None,
) -> str:
    sources = []
    if research and research.openalex_author_id:
        sources.append(f"OpenAlex author {research.openalex_author_id}")
    if research and research.semantic_scholar_author_id:
        sources.append(f"Semantic Scholar author {research.semantic_scholar_author_id}")
    where = f" with affiliation {university}" if university else ""
    how = f"matched by name{where} ({', '.join(sources)})" if sources else "matched by name"
    listed = (
        f"; {professor.name} appears in the paper's author list"
        if paper.authors and authored_by(paper, professor.name)
        else "; the source gave no author list, so check the paper page"
    )
    return f"yes, {how}{listed}"


def _overlap_note(paper: ProfessorPublication, colleagues: list[str]) -> str:
    overlaps = [
        colleague
        for colleague in colleagues
        if any(names_match(colleague, author) for author in paper.authors or [])
    ]
    if not overlaps:
        return "none found among the other professors you selected"
    return (
        "this paper is co-authored with "
        + ", ".join(overlaps)
        + ", whom you are also emailing; consider using a different paper for one of them"
    )


def build_notes(
    output: WriterOutput,
    paper: ProfessorPublication,
    research: ProfessorResearch | None,
    professor: Professor,
    university: str | None,
    colleagues: list[str],
) -> str:
    venue = "preprint" if is_repository_venue(paper.venue) else paper.venue
    link = paper.url or (f"https://doi.org/{paper.doi}" if paper.doi else "no link")
    found = research.contact_instructions if research and research.contact_instructions else None
    instructions = output.contact_instructions_followed.strip() or "none found"
    if found and found not in instructions:
        instructions = f"{instructions} (found on page: {found})"
    checks = "; ".join(output.sentences_to_check) if output.sentences_to_check else "none"
    return "\n".join(
        [
            f"- Paper used: {paper.title}, {venue}, {paper.year or 'year unknown'}, {link}",
            f"- Authorship verified: {_verification_note(research, paper, professor, university)}",
            f"- Contact instructions found on professor's website: {instructions}",
            f"- Sentences to check (assumptions about the student): {checks}",
            f"- Overlap warning: {_overlap_note(paper, colleagues)}",
        ]
    )


class WriterAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        model: Any = None,
        max_attempts: int = 4,
    ):
        self.settings = settings or get_settings()
        self.model = model
        self.max_attempts = max(1, max_attempts)

    def _model(self) -> Any:
        if self.model is None:
            try:
                self.model = create_chat_model(self.settings)
            except LLMConfigurationError as error:
                raise WriterError(str(error)) from error
        return self.model

    def write(
        self,
        profile: UserProfile,
        professor: Professor,
        research: ProfessorResearch | None,
        papers: list[ProfessorPublication],
        degree: DegreeSeeking | str,
        start_term: str | None,
        instructions: str | None = None,
        previous: EmailComposition | None = None,
        attach_cv: bool = False,
        target: TargetUniversity | None = None,
        colleagues: list[str] | None = None,
    ) -> EmailComposition:
        logger.info("Starting email writing for professor %s", professor.id)
        verified = [paper for paper in papers if authored_by(paper, professor.name)]
        if not verified:
            raise WriterError(
                "No verified publications are cached for this professor; run research first "
                "or write this email manually."
            )
        degree_text = degree_phrase(degree)
        signature = default_signature(profile)
        intake = start_term or "the next"
        system = _SYSTEM_PROMPT.format(
            degree=degree_text,
            intake=intake,
            signature=signature,
            cv_line='"My CV is attached." ' if attach_cv else "",
            min_words=MIN_WORDS,
            max_words=MAX_WORDS,
            banned=", ".join(f'"{phrase}"' for phrase in BANNED_PHRASES),
        )
        request = {
            "PROFESSOR": {
                "name": professor.name,
                "title": professor.title,
                "university": target.university_name if target else None,
                "department": target.department if target else None,
                "listed_research_areas": professor.research_areas,
                "research_themes": research.themes if research else [],
                "open_positions": research.open_positions_evidence if research else None,
            },
            # The top-ranked papers only; free model tiers limit tokens per minute.
            "VERIFIED PAPERS": [_paper_record(paper) for paper in verified[:5]],
            "CONTACT INSTRUCTIONS": (research.contact_instructions if research else None)
            or "none found",
            "PAGE EXCERPT": (research.lab_page_excerpt or "")[:800] if research else None,
            "STUDENT PROFILE": applicant_facts(profile),
            "POSITION SOUGHT": degree_text,
            "INTAKE": start_term or "not specified; do not name a specific term",
            "CV ATTACHED": attach_cv,
        }
        human = "Write the email.\n\n" + json.dumps(request, indent=2, ensure_ascii=False)
        if previous is not None:
            human += (
                f"\n\nPrevious draft to revise:\nSubject: {previous.subject}\n\n{previous.body}"
            )
        if instructions:
            human += f"\n\nNotes from the student for this email: {instructions}"

        messages: list[tuple[str, str]] = [("system", system), ("human", human)]
        problems: list[str] = []
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = structured(self._model(), WriterOutput).invoke(messages)
                output = WriterOutput.model_validate(raw)
            except Exception as error:
                # Free tiers sometimes return truncated or invalid JSON; try again.
                logger.info(
                    "Writer response unusable for professor %s on attempt %s: %s",
                    professor.id,
                    attempt,
                    type(error).__name__,
                )
                problems = ["The model returned an unusable response."]
                continue
            output.email = _plain_punctuation(output.email.strip())
            output.subject = _plain_punctuation(output.subject)
            problems = validate_email(
                output,
                verified[:5],
                degree_text,
                start_term,
                signature,
                profile.name,
                attach_cv,
                profile_text=json.dumps(applicant_facts(profile), ensure_ascii=False),
                gpa=profile.gpa,
            )
            if not problems:
                paper = next(
                    p for p in verified if _normalize(p.title) == _normalize(output.paper_title)
                )
                logger.info(
                    "Completed email writing for professor %s on attempt %s", professor.id, attempt
                )
                return EmailComposition(
                    subject=output.subject.strip(),
                    body=output.email,
                    cited_paper_titles=[paper.title],
                    match_note=build_notes(
                        output,
                        paper,
                        research,
                        professor,
                        target.university_name if target else None,
                        [name for name in colleagues or [] if name != professor.name],
                    ),
                )
            logger.info(
                "Email draft for professor %s failed %s checks on attempt %s",
                professor.id,
                len(problems),
                attempt,
            )
            messages = [
                ("system", system),
                (
                    "human",
                    f"{human}\n\nYour last draft was:\nSubject: {output.subject}\n\n"
                    f"{output.email}\n\nFix these problems and rewrite the whole email:\n- "
                    + "\n- ".join(problems),
                ),
            ]
        raise WriterError("Could not produce a compliant email: " + " ".join(problems))

    def write_followup(
        self,
        profile: UserProfile,
        professor: Professor,
        original_subject: str,
        original_body: str,
        days_since: int,
    ) -> tuple[str, str]:
        logger.info("Starting follow-up writing for professor %s", professor.id)
        signature = default_signature(profile)
        system = (
            "You write a short, polite follow-up from a student to a professor who has not "
            'replied. Use only facts from the original email. Greeting: "Dear Professor '
            '<Last name>,". In 2-3 short sentences, mention the earlier email in one sentence, '
            "restate the request in one sentence, and end with ONE simple yes/no question. "
            f"{FOLLOWUP_MIN_WORDS}-{FOLLOWUP_MAX_WORDS} words before the sign-off. Plain, "
            "natural and modest; contractions are fine. No flattery, no guilt-tripping, no "
            "request for a call or meeting, no bullet points. Never use these phrases: "
            + ", ".join(f'"{phrase}"' for phrase in BANNED_PHRASES)
            + f".\nEnd with exactly this sign-off, unchanged:\n{signature}"
        )
        human = (
            f"Professor: {professor.name}\n"
            f"Days since the original email: {days_since}\n\n"
            f"Original subject: {original_subject}\n\nOriginal email:\n{original_body}"
        )
        messages: list[tuple[str, str]] = [("system", system), ("human", human)]
        problems: list[str] = []
        for _ in range(self.max_attempts):
            raw = structured(self._model(), FollowUpComposition).invoke(messages)
            body = FollowUpComposition.model_validate(raw).body.strip()
            problems = validate_followup(body, signature)
            if not problems:
                subject = original_subject
                if not subject.lower().startswith("re:"):
                    subject = f"Re: {subject}"
                logger.info("Completed follow-up writing for professor %s", professor.id)
                return subject, body
            messages = [
                ("system", system),
                (
                    "human",
                    f"{human}\n\nYour last draft was:\n{body}\n\nFix these problems:\n- "
                    + "\n- ".join(problems),
                ),
            ]
        raise WriterError("Could not produce a compliant follow-up: " + " ".join(problems))
