import pytest
from google_auth_oauthlib import flow as oauth_flow
from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

from profreach.gmail.auth import SIGN_IN_TIMEOUT_SECONDS, GmailAccessDenied, load_credentials


class FakeFlow:
    def __init__(self, error):
        self.error = error
        self.kwargs = None

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        raise self.error


@pytest.mark.parametrize(
    "error",
    [oauth_flow.WSGITimeoutError("timed out"), AccessDeniedError("access_denied")],
)
def test_blocked_sign_in_explains_test_users(monkeypatch, tmp_path, error):
    client = tmp_path / "credentials.json"
    client.write_text("{}", encoding="utf-8")
    fake = FakeFlow(error)
    monkeypatch.setattr(oauth_flow.InstalledAppFlow, "from_client_secrets_file", lambda *args: fake)

    with pytest.raises(GmailAccessDenied, match="Test users"):
        load_credentials(str(client), str(tmp_path / "token.json"), allow_browser_flow=True)

    assert fake.kwargs["timeout_seconds"] == SIGN_IN_TIMEOUT_SECONDS
    assert not (tmp_path / "token.json").exists()
