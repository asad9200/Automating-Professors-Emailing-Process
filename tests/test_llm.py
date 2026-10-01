import pytest

from profreach.config import Settings
from profreach.llm import LLMConfigurationError, create_chat_model


def test_groq_is_the_default_provider():
    model = create_chat_model(Settings(llm_provider="groq", groq_api_key="test-key"))

    assert type(model).__name__ == "ChatGroq"
    assert model.model_name == "openai/gpt-oss-120b"


def test_missing_key_names_the_provider_setting():
    with pytest.raises(LLMConfigurationError, match="GROQ_API_KEY"):
        create_chat_model(Settings(llm_provider="groq", groq_api_key=None))
    with pytest.raises(LLMConfigurationError, match="ANTHROPIC_API_KEY"):
        create_chat_model(Settings(llm_provider="anthropic", anthropic_api_key=None))
