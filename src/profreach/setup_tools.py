"""Helpers for the in-app Setup page: saving keys, checking them, and Google OAuth files."""

import json
import logging
import os
from pathlib import Path

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"


class SetupError(ValueError):
    """Raised when a setup value is invalid."""


class ServiceStatus(BaseModel):
    name: str
    ready: bool
    detail: str


def update_env_file(values: dict[str, str], path: str = ".env", template: str = ".env.example"):
    """Set keys in .env, keeping every other line; create it from the template if missing."""
    env_path = Path(path)
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif Path(template).is_file():
        lines = Path(template).read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    remaining = dict(values)
    updated = []
    for line in lines:
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#")
            else None
        )
        if key in remaining:
            updated.append(f"{key}={remaining.pop(key)}")
        else:
            updated.append(line)
    updated.extend(f"{key}={value}" for key, value in remaining.items())
    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def verify_groq_key(key: str, client: httpx.Client | None = None) -> None:
    """Raise SetupError unless Groq accepts the key."""
    key = key.strip()
    if not key.startswith("gsk_"):
        raise SetupError("Groq keys start with gsk_. Copy the whole key from the Groq console.")
    http = client or httpx.Client(timeout=20)
    try:
        response = http.get(GROQ_MODELS_URL, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as error:
        raise SetupError("Could not reach Groq. Check your internet connection.") from error
    if response.status_code == 401:
        raise SetupError("Groq rejected this key. Create a new one and paste it again.")
    if response.status_code >= 400:
        raise SetupError(f"Groq returned an error ({response.status_code}). Try again shortly.")


def validate_oauth_client(content: bytes) -> dict:
    """Check an uploaded Google OAuth client JSON is a Desktop app client."""
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SetupError("This is not the JSON file downloaded from Google Cloud.") from error
    if "web" in data:
        raise SetupError(
            "This client is a Web application. Create an OAuth client of type Desktop app "
            "and upload that file instead."
        )
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict) or not installed.get("client_id"):
        raise SetupError("The file has no Desktop app client. Download it again from Clients.")
    return data


def save_oauth_client(content: bytes, path: str) -> None:
    validate_oauth_client(content)
    target = Path(path)
    target.write_bytes(content)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def service_statuses(settings) -> list[ServiceStatus]:
    provider = settings.llm_provider.strip().lower()
    if provider == "anthropic":
        llm_ready = bool(settings.anthropic_api_key)
        llm_detail = "Claude" if llm_ready else "Add an Anthropic key"
    else:
        llm_ready = bool(settings.groq_api_key)
        llm_detail = f"Groq · {settings.groq_model}" if llm_ready else "Add a free Groq key"
    gmail_ready = Path(settings.gmail_token_path).is_file()
    client_ready = Path(settings.gmail_credentials_path).is_file()
    if gmail_ready:
        gmail_detail = "Connected"
    elif client_ready:
        gmail_detail = "Click Continue with Google"
    else:
        gmail_detail = "One-time Google setup needed"
    search = "Tavily" if settings.tavily_api_key else "DuckDuckGo (free, no key)"
    return [
        ServiceStatus(name="AI writing", ready=llm_ready, detail=llm_detail),
        ServiceStatus(name="Gmail", ready=gmail_ready, detail=gmail_detail),
        ServiceStatus(name="Web search", ready=True, detail=search),
        ServiceStatus(name="Publications", ready=True, detail="OpenAlex + Semantic Scholar (free)"),
    ]
