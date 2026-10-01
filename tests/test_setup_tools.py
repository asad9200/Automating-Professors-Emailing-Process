import json

import httpx
import pytest

from profreach.config import Settings
from profreach.setup_tools import (
    SetupError,
    save_oauth_client,
    service_statuses,
    update_env_file,
    validate_oauth_client,
    verify_groq_key,
)

DESKTOP_CLIENT = json.dumps(
    {"installed": {"client_id": "123.apps.googleusercontent.com", "client_secret": "x"}}
).encode()


def test_update_env_file_replaces_keys_and_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nGROQ_API_KEY=old\nLOG_LEVEL=INFO\n", encoding="utf-8")

    update_env_file({"GROQ_API_KEY": "gsk_new", "SERPAPI_API_KEY": "s"}, path=str(env))

    assert env.read_text(encoding="utf-8") == (
        "# comment\nGROQ_API_KEY=gsk_new\nLOG_LEVEL=INFO\nSERPAPI_API_KEY=s\n"
    )


def test_update_env_file_starts_from_template(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("GROQ_API_KEY=\nLLM_PROVIDER=groq\n", encoding="utf-8")
    env = tmp_path / ".env"

    update_env_file({"GROQ_API_KEY": "gsk_x"}, path=str(env), template=str(template))

    assert env.read_text(encoding="utf-8") == "GROQ_API_KEY=gsk_x\nLLM_PROVIDER=groq\n"


def groq_client(status):
    def respond(request):
        assert request.headers["authorization"] == "Bearer gsk_test"
        return httpx.Response(status, request=request, json={"data": []})

    return httpx.Client(transport=httpx.MockTransport(respond))


def test_verify_groq_key():
    verify_groq_key("gsk_test", groq_client(200))
    with pytest.raises(SetupError, match="rejected"):
        verify_groq_key("gsk_test", groq_client(401))
    with pytest.raises(SetupError, match="start with gsk_"):
        verify_groq_key("sk-wrong", groq_client(200))


def test_oauth_client_must_be_a_desktop_app(tmp_path):
    assert validate_oauth_client(DESKTOP_CLIENT)["installed"]["client_id"]
    with pytest.raises(SetupError, match="Desktop app"):
        validate_oauth_client(json.dumps({"web": {"client_id": "x"}}).encode())
    with pytest.raises(SetupError, match="not the JSON"):
        validate_oauth_client(b"not json")

    target = tmp_path / "credentials.json"
    save_oauth_client(DESKTOP_CLIENT, str(target))
    assert target.read_bytes() == DESKTOP_CLIENT


def test_service_statuses_reflect_configuration(tmp_path):
    settings = Settings(
        llm_provider="groq",
        groq_api_key=None,
        tavily_api_key=None,
        gmail_token_path=str(tmp_path / "token.json"),
        gmail_credentials_path=str(tmp_path / "credentials.json"),
    )
    statuses = {status.name: status for status in service_statuses(settings)}

    assert not statuses["AI writing"].ready
    assert statuses["Gmail"].detail == "One-time Google setup needed"
    assert statuses["Web search"].ready and "DuckDuckGo" in statuses["Web search"].detail
