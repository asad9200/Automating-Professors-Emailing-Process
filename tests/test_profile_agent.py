import pytest

from profreach.agents.profile_agent import ProfileAgent, ProfileExtractionError
from profreach.config import Settings
from profreach.db.models import DegreeSeeking, UserProfileData


class FakeStructuredModel:
    def __init__(self, result):
        self.result = result
        self.schema = None

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        return self

    def invoke(self, messages):
        assert "Do not infer or invent" in messages[0][1]
        assert "Student CV text" in messages[1][1]
        return self.result


def test_profile_agent_returns_structured_profile():
    model = FakeStructuredModel(
        {
            "name": "Avery Chen",
            "education": [{"institution": "Example University", "degree": "MS"}],
            "skills": ["Python"],
            "research_interests": ["robotics"],
            "target_degree": "PhD",
        }
    )

    profile = ProfileAgent(structured_model=model).parse("Student CV text")

    assert model.schema is UserProfileData
    assert profile.name == "Avery Chen"
    assert profile.education[0].institution == "Example University"
    assert profile.target_degree is DegreeSeeking.PHD
    assert profile.publications == []


def test_profile_agent_requires_text():
    with pytest.raises(ProfileExtractionError, match="no extractable text"):
        ProfileAgent(structured_model=FakeStructuredModel({})).parse("  ")


def test_profile_agent_explains_missing_api_key():
    agent = ProfileAgent(settings=Settings(llm_provider="groq", groq_api_key=None))

    with pytest.raises(ProfileExtractionError, match="enter your profile manually"):
        agent.parse("Student CV text")
