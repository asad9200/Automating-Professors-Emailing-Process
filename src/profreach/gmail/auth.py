import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# compose: create, update, and send drafts. readonly: read threads to detect replies.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailAuthError(RuntimeError):
    """Raised when Gmail credentials are missing or cannot be refreshed."""


class GmailAccessDenied(GmailAuthError):
    """Google refused sign-in, usually because the account is not a listed test user."""


SIGN_IN_TIMEOUT_SECONDS = 180
_ACCESS_DENIED_HELP = (
    "Google did not allow this sign-in. While the app is in Testing, only accounts listed as "
    "Test users can sign in: open Google Cloud > Google Auth Platform > Audience, select "
    "Add users under Test users, add the exact Gmail address you are signing in with, save, "
    "and try again. Make sure it is the same Google Cloud project that created the client file."
)


def is_connected(token_path: str) -> bool:
    return Path(token_path).is_file()


def load_credentials(
    credentials_path: str,
    token_path: str,
    allow_browser_flow: bool = False,
) -> Any:
    """Load the saved OAuth token, refreshing it or running the desktop consent flow."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials = None
    token_file = Path(token_path)
    if token_file.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except ValueError:
            logger.warning("Saved Gmail token is unreadable; a new sign-in is required")
    if credentials and credentials.valid:
        return credentials
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            _save_token(token_file, credentials.to_json())
            return credentials
        except RefreshError:
            logger.warning("Gmail token refresh failed; a new sign-in is required")
    if not allow_browser_flow:
        raise GmailAuthError("Gmail is not connected. Connect Gmail on the Send page first.")
    if not Path(credentials_path).is_file():
        raise GmailAuthError(
            f"OAuth client file {credentials_path} was not found. Follow the README's "
            "Google Cloud setup and save the Desktop app client JSON there."
        )
    from google_auth_oauthlib.flow import WSGITimeoutError
    from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    try:
        # Google shows its own error page without redirecting back when an account is not
        # an approved tester, so the local listener must not wait forever.
        credentials = flow.run_local_server(
            port=0,
            open_browser=True,
            timeout_seconds=SIGN_IN_TIMEOUT_SECONDS,
        )
    except WSGITimeoutError as error:
        raise GmailAccessDenied(
            "Sign-in was not completed within 3 minutes. If Google showed "
            "'Error 403: access_denied', your account is not a test user yet. "
            + _ACCESS_DENIED_HELP
        ) from error
    except AccessDeniedError as error:
        raise GmailAccessDenied(_ACCESS_DENIED_HELP) from error
    _save_token(token_file, credentials.to_json())
    logger.info("Gmail OAuth consent completed")
    return credentials


def disconnect(token_path: str) -> None:
    Path(token_path).unlink(missing_ok=True)


def build_gmail_service(credentials: Any) -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _save_token(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
