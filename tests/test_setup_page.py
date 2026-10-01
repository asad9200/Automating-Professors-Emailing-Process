from streamlit.testing.v1 import AppTest
from test_setup_tools import DESKTOP_CLIENT

SCRIPT = """
from profreach.config import get_settings
get_settings.cache_clear()
from profreach.ui.setup import render_setup_page
render_setup_page()
"""


def run_page(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("GROQ_API_KEY", "")
    return AppTest.from_string(SCRIPT, default_timeout=30).run()


def test_setup_page_offers_google_sign_in_only_after_client_upload(monkeypatch, tmp_path):
    page = run_page(monkeypatch, tmp_path)
    assert not page.exception
    assert "Continue with Google" not in [button.label for button in page.button]

    (tmp_path / "credentials.json").write_bytes(DESKTOP_CLIENT)
    page = run_page(monkeypatch, tmp_path)

    assert not page.exception
    assert "Continue with Google" in [button.label for button in page.button]
