from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # "groq" uses Groq's free tier; "anthropic" uses Claude.
    llm_provider: str = "groq"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    llm_reasoning_effort: str = "medium"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    # "auto" uses Tavily when TAVILY_API_KEY is set, otherwise free DuckDuckGo search.
    search_provider: str = "auto"
    tavily_api_key: str | None = None
    serpapi_api_key: str | None = None
    semantic_scholar_api_key: str | None = None
    openalex_api_key: str | None = None
    openalex_mailto: str | None = None
    database_url: str = "sqlite:///emailering.db"
    checkpoint_db_path: str = "emailering_checkpoints.db"
    log_level: str = "INFO"
    scrape_delay_seconds: float = 1.0
    scrape_max_pages: int = 100
    user_agent: str = "Emailering/0.1 (academic faculty discovery)"
    # When a site refuses plain HTTP (403), open the page in a regular browser window.
    browser_fallback: bool = True
    browser_headless: bool = False
    research_max_papers: int = 8
    research_cache_days: int = 30
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"
    cv_attachment_path: str | None = None
    send_max_per_day: int = 20
    send_min_gap_seconds: int = 120
    send_max_gap_seconds: int = 300
    followup_after_days: int = 10

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
