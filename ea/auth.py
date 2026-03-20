"""
auth.py

OAuth2 credential management for Gmail and Google Calendar APIs.

File paths are resolved in this order:
  1. Explicit argument passed to the function
  2. [auth] section of config.toml
  3. Project root (same directory as ea.py)

First-time setup:
  1. Create a GCP project at https://console.cloud.google.com
  2. Enable the Gmail API and Google Calendar API
  3. Create an OAuth 2.0 Desktop Client under APIs & Services → Credentials
  4. Download the JSON and save it (default name: credentials.json)
  5. Run: python ea.py auth
"""

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

_ROOT = Path(__file__).parent.parent


def _resolve(path: str | Path | None, config_key: str, default: str) -> Path:
    """
    Resolve a file path:
      explicit arg → config.toml [auth] → default filename in project root.
    """
    if path is not None:
        return Path(path)
    try:
        from ea.config import load_config
        cfg = load_config()
        value = cfg.get("auth", {}).get(config_key)
        if value:
            p = Path(value)
            return p if p.is_absolute() else _ROOT / p
    except Exception:
        pass
    return _ROOT / default


def load_creds(
    credentials_file: str | Path | None = None,
    token_file: str | Path | None = None,
) -> Credentials:
    """
    Return valid OAuth2 credentials, refreshing or re-authorizing as needed.

    Args:
        credentials_file: Path to the OAuth2 client secret JSON downloaded from
                          GCP. Defaults to the path in config.toml [auth], then
                          credentials.json in the project root.
        token_file:       Path where the user token is cached. Defaults to the
                          path in config.toml [auth], then token.json in the
                          project root.

    Raises:
        FileNotFoundError: if credentials_file is missing and no token exists.
    """
    creds_path = _resolve(credentials_file, "credentials_file", "credentials.json")
    token_path = _resolve(token_file, "token_file", "token.json")

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"credentials file not found: {creds_path}\n\n"
                    "To set up Google API access:\n"
                    "  1. Go to https://console.cloud.google.com\n"
                    "  2. Create a project and enable Gmail API + Google Calendar API\n"
                    "  3. Create an OAuth 2.0 Desktop Client credential\n"
                    "  4. Download the JSON and set its path in config.toml [auth] or\n"
                    "     pass --credentials to the CLI\n"
                    "  5. Run: python ea.py auth"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())

    return creds


def run_auth_flow(
    credentials_file: str | Path | None = None,
    token_file: str | Path | None = None,
) -> None:
    """Force a fresh OAuth2 browser consent flow and save the token."""
    token_path = _resolve(token_file, "token_file", "token.json")
    if token_path.exists():
        token_path.unlink()
    creds = load_creds(credentials_file=credentials_file, token_file=token_file)
    print(f"Authorized. Token saved to {token_path}")
    print(f"Scopes: {', '.join(creds.scopes or SCOPES)}")


def check_auth(token_file: str | Path | None = None) -> None:
    """Print current auth status without triggering a browser flow."""
    token_path = _resolve(token_file, "token_file", "token.json")
    if not token_path.exists():
        print(f"Not authorized (no token at {token_path}). Run: python ea.py auth")
        return
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.valid:
            print(f"Authorized and valid  ({token_path})")
            print(f"Scopes: {', '.join(creds.scopes or [])}")
        elif creds.expired and creds.refresh_token:
            print("Token expired — will refresh automatically on next poll")
        else:
            print("Token invalid. Run: python ea.py auth")
    except Exception as e:
        print(f"Auth error: {e}")
