from typing import Any

from profreach.config import Settings


class LLMConfigurationError(RuntimeError):
    """Raised when an LLM-backed step is used without a configured provider key."""


def create_chat_model(settings: Settings) -> Any:
    provider = settings.llm_provider.strip().lower()
    if provider == "groq":
        if not settings.groq_api_key:
            raise LLMConfigurationError("Set GROQ_API_KEY in .env to use this step.")
        from langchain_groq import ChatGroq

        options: dict[str, Any] = {}
        if any(name in settings.groq_model for name in ("gpt-oss", "qwen3")):
            # Reasoning models write noticeably better emails when allowed to think first.
            options["reasoning_effort"] = settings.llm_reasoning_effort
        return ChatGroq(
            model=settings.groq_model,
            api_key=settings.groq_api_key,
            # Groq's free tier counts max_tokens toward its tokens-per-minute limit.
            max_tokens=4096,
            max_retries=3,
            **options,
        )
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMConfigurationError("Set ANTHROPIC_API_KEY in .env to use this step.")
        from langchain_anthropic import ChatAnthropic

        # Current Claude models reject sampling parameters such as temperature.
        return ChatAnthropic(
            model=settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            max_tokens=16000,
        )
    raise LLMConfigurationError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r}; use groq or anthropic."
    )


def structured(model: Any, schema: type) -> Any:
    """Bind native JSON-schema structured outputs (supported by both providers)."""
    return model.with_structured_output(schema, method="json_schema")
