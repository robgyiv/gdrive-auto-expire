"""OAuth desktop flow and token handling.

The scope is ``drive.file`` only, so the app can see nothing in Drive it did
not create itself. That is both the right blast radius and Google's only
non-sensitive Drive scope, which keeps this out of verification review.
"""

from __future__ import annotations

import json
import os

from . import config


class AuthError(RuntimeError):
    """Something is wrong with the credentials that the user has to fix."""


def _require_client_secret():
    path = config.client_secret_path()
    if not path.exists():
        raise AuthError(
            f"no OAuth client at {path}\n"
            "Create a Desktop-app OAuth client in Google Cloud Console, download\n"
            f"the JSON and save it there (chmod 600). See the README for setup."
        )
    return path


def _save_credentials(creds) -> None:
    config.ensure_dirs()
    path = config.token_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(creds.to_json())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_credentials(refresh: bool = True):
    """Return stored credentials, refreshing them if needed, else None."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = config.token_path()
    if not path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), config.SCOPES)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError(f"stored token at {path} is unreadable: {exc}") from exc

    if creds.valid:
        return creds
    if refresh and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # The usual cause: the OAuth client is still in Testing publishing
            # status, where refresh tokens die after seven days.
            raise AuthError(
                f"could not refresh the stored token: {exc}\n"
                "Run `gdrive-auto-expire auth login` again. If this keeps happening,\n"
                "set the OAuth client's publishing status to 'In Production' — in\n"
                "'Testing' Google expires refresh tokens after 7 days."
            ) from exc
        _save_credentials(creds)
        return creds
    return creds if creds.valid else None


def login(port: int = 0):
    """Run the local-server consent flow and store the resulting token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = _require_client_secret()
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), config.SCOPES)
    creds = flow.run_local_server(port=port, open_browser=True)
    _save_credentials(creds)
    return creds


def logout() -> bool:
    """Delete the stored token. True if there was one."""
    path = config.token_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def require_credentials():
    creds = load_credentials()
    if creds is None:
        raise AuthError("not signed in — run `gdrive-auto-expire auth login`")
    return creds
