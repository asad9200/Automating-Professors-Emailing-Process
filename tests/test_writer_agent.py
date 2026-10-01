import pytest

from profreach.agents.writer_agent import (
    WriterAgent,
    WriterError,
    WriterOutput,
    validate_email,
    validate_followup,
    word_count,
)
from profreach.config import Settings
from profreach.db.models import (
    DegreeSeeking,
    ProfessorPublication,
    ProfessorResearch,
    ResearchStatus,
)

PAPER_TITLE = "Learning Dexterous Robot Grasps from Tactile Feedback"
SIGNATURE = "Kind regards,\nJordan Lee\n+1 555 0100"


class FakeModel:
    """Returns queued structured results and records every prompt."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        self.kwargs = kwargs
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return self.results.pop(0)


def email(opening=None, closing=None, extra="", words=110, attach=False):
    filler = " ".join(["detail"] * words)
    opening = opening or (
        "Your CoRL paper on learning dexterous robot grasps from tactile feedback stayed "
        "with me because it treats touch as the main signal rather than an afterthought."
    )
    closing = closing or "Are you considering PhD students for the Fall 2027 intake?"
    cv = "My CV is attached. " if attach else ""
    return (
        "Dear Professor Chen,\n\n"
        f"{opening}\n\n"
        "I'm Jordan Lee, and I finished a BS in Computer Science at State University. I'm "
        f"applying for a PhD position for Fall 2027. {extra} {filler}\n\n"
        f"{cv}{closing}\n\n{SIGNATURE}"
    )


def output(**overrides):
    values = {
        "subject": "Prospective PhD student - tactile robot grasping (Fall 2027)",
        "email": email(),
        "paper_title": PAPER_TITLE,
        "sentences_to_check": [],
        "contact_instructions_followed": "none found",
    }
    values.update(overrides)
    return values


def papers(authors=None):
    return [
        ProfessorPublication(
            professor_id=1,
            title=PAPER_TITLE,
            year=2025,
            venue="CoRL",
            abstract_summary="Grasping with learned tactile policies.",
            url="https://doi.org/10.1/x",
            authors=authors if authors is not None else ["Avery Chen", "Sam Diaz"],
            source="openalex",
        )
    ]


@pytest.fixture
def signed_profile(engine, profile):
    from profreach.db.profile_store import ProfileStore

    ProfileStore(engine).save_email_settings("Projects: tactile glove dataset.", SIGNATURE)
    return ProfileStore(engine).get_profile()


def check(value, **kwargs):
    defaults = {
        "papers": papers(),
        "degree": "PhD",
        "start_term": "Fall 2027",
        "signature": SIGNATURE,
        "student_name": "Jordan Lee",
    }
    defaults.update(kwargs)
    return validate_email(WriterOutput(**value), **defaults)


def test_valid_email_passes_and_notes_are_built(make_professor, signed_profile):
    model = FakeModel([output(sentences_to_check=["I'm curious about touch sensing."])])
    research = ProfessorResearch(
        professor_id=1,
        status=ResearchStatus.DONE,
        openalex_author_id="A1",
        contact_instructions="Prospective students should apply through the portal first.",
    )

    result = WriterAgent(settings=Settings(), model=model).write(
        signed_profile,
        make_professor(),
        research,
        papers(),
        DegreeSeeking.PHD,
        "Fall 2027",
        colleagues=["Avery Chen", "Sam Diaz"],
    )

    assert result.cited_paper_titles == [PAPER_TITLE]
    assert model.kwargs == {"method": "json_schema"}
    system, human = model.calls[0][0][1], model.calls[0][1][1]
    assert "Open directly with the professor's specific work" in system or "open directly" in system
    assert SIGNATURE in system
    assert "tactile glove dataset" in human
    assert "Paper used: " + PAPER_TITLE + ", CoRL, 2025" in result.match_note
    assert "OpenAlex author A1" in result.match_note
    assert "apply through the portal" in result.match_note
    assert "I'm curious about touch sensing." in result.match_note
    assert "co-authored with Sam Diaz" in result.match_note


def test_rule_violations_are_sent_back_and_fixed(make_professor, signed_profile):
    bad = output(email=email(extra="I am passionate and this resonates with me."))
    model = FakeModel([bad, output()])

    WriterAgent(model=model).write(
        signed_profile, make_professor(), None, papers(), DegreeSeeking.PHD, "Fall 2027"
    )

    feedback = model.calls[1][1][1]
    assert "banned phrases: passionate, resonates" in feedback


def test_gives_up_after_repeated_violations(make_professor, signed_profile):
    bad = output(paper_title="A Paper That Does Not Exist")
    model = FakeModel([bad] * 4)

    with pytest.raises(WriterError, match="not one of the VERIFIED PAPERS"):
        WriterAgent(model=model).write(
            signed_profile, make_professor(), None, papers(), DegreeSeeking.PHD, "Fall 2027"
        )


def test_papers_without_the_professor_as_author_are_excluded(make_professor, signed_profile):
    with pytest.raises(WriterError, match="No verified publications"):
        WriterAgent(model=FakeModel([])).write(
            signed_profile,
            make_professor(),
            None,
            papers(authors=["Someone Else"]),
            DegreeSeeking.PHD,
            None,
        )


def test_style_rules():
    assert check(output()) == []
    assert check(output(email=email(opening="My name is Jordan and I study ML here."))) != []
    assert any("question" in p for p in check(output(email=email(extra="Is this okay?"))))
    assert any(
        "call" in p
        for p in check(output(email=email(extra="Could we schedule a short call next week.")))
    )
    assert any("bullet" in p for p in check(output(email=email(extra="\n- point one\n"))))
    assert any("arXiv" in p for p in check(output(email=email(extra="It was on arXiv."))))
    assert any("words" in p for p in check(output(email=email(words=5))))
    assert any("sign-off" in p for p in check(output(email=email().replace("+1 555 0100", ""))))
    assert any("Spring 2028" in p for p in check(output(), start_term="Spring 2028"))


def test_cv_line_must_match_attachment():
    attached = output(email=email(attach=True))
    assert check(attached, attach_cv=True) == []
    assert any("attached" in p for p in check(attached, attach_cv=False))
    assert any("My CV is attached" in p for p in check(output(), attach_cv=True))


def test_masters_degree_phrasing_is_accepted():
    body = (
        email()
        .replace("PhD position", "Master's position")
        .replace("PhD students", "Master's students")
    )
    assert check(output(email=body), degree="Master's") == []


def test_followup_rules(make_professor, signed_profile):
    body = (
        "Dear Professor Chen,\n\n"
        "I wrote to you two weeks ago about joining your group. "
        + " ".join(["word"] * 55)
        + " Are you considering PhD students for Fall 2027?\n\n"
        + SIGNATURE
    )
    model = FakeModel([{"body": body}])

    subject, text = WriterAgent(model=model).write_followup(
        signed_profile, make_professor(), "PhD inquiry", "Original body", 12
    )

    assert subject == "Re: PhD inquiry"
    assert 50 <= word_count(text) <= 140
    assert validate_followup(body.replace("?", "."), SIGNATURE) != []


def test_invented_labels_and_reworded_grades_are_rejected():
    profile_text = '{"publications": [{"title": "Cardio risk paper"}], "gpa": "4.00/4.00"}'
    invented = output(email=email(extra="In my final-year project I built a pipeline."))
    reworded = output(email=email(extra="I graduated with a perfect GPA."))
    exact = output(email=email(extra="I graduated with a CGPA of 4.00/4.00."))

    assert any("final-year" in p for p in check(invented, profile_text=profile_text))
    assert any("4.00/4.00" in p for p in check(reworded, gpa="4.00/4.00"))
    assert check(exact, gpa="4.00/4.00", profile_text=profile_text) == []


def test_em_dashes_and_hosting_venues():
    from profreach.agents.writer_agent import is_repository_venue

    dashed = output(email=email(extra="It taught me to debug systems\u2014carefully."))
    assert any("em dashes" in p for p in check(dashed))
    assert is_repository_venue("Underline Science Inc.")
    assert is_repository_venue("arXiv (Cornell University)")
    assert not is_repository_venue("Nature Communications")
