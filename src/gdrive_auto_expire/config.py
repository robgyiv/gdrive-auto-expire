"""Paths, constants and the settings file.

Every filesystem location the tool uses is resolved here so that nothing else
has to know the layout. The directories are honoured from the environment
(``GDRIVE_AUTO_EXPIRE_CONFIG_DIR`` / ``_STATE_DIR``) which is what the tests
use to stay out of the real ones.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "gdrive-auto-expire"

#: The only non-sensitive Drive scope: the app sees only files it created.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

FOLDER_MIME = "application/vnd.google-apps.folder"

#: Marker appended to the crontab line so we only ever touch our own entry.
CRON_TAG = f"# {APP_NAME}"

DEFAULT_FOLDER = APP_NAME

CONFIG_VERSION = 1


def config_dir() -> Path:
    override = os.environ.get("GDRIVE_AUTO_EXPIRE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / APP_NAME


def state_dir() -> Path:
    override = os.environ.get("GDRIVE_AUTO_EXPIRE_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / APP_NAME


def client_secret_path() -> Path:
    return config_dir() / "client_secret.json"


def token_path() -> Path:
    return config_dir() / "token.json"


def config_path() -> Path:
    return config_dir() / "config.json"


def state_path() -> Path:
    return state_dir() / "shares.json"


def lock_path() -> Path:
    return state_dir() / "lock"


def log_path() -> Path:
    # Deliberately not ~/Library/Logs: cron on modern macOS hits TCC
    # restrictions there, and a silently unwritable log from a background job
    # is a bad failure mode.
    return state_dir() / "sweep.log"


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)


def _defaults() -> dict:
    return {"version": CONFIG_VERSION, "folder": DEFAULT_FOLDER, "folder_ids": {}}


def load_config() -> dict:
    """Read config.json, falling back to defaults for anything absent."""
    data = _defaults()
    try:
        raw = json.loads(config_path().read_text())
    except FileNotFoundError:
        return data
    except json.JSONDecodeError:
        # A corrupt config should not brick the tool; defaults get written back
        # on the next save.
        return data
    if isinstance(raw, dict):
        data.update(raw)
    data.setdefault("folder_ids", {})
    return data


def save_config(data: dict) -> None:
    """Write config.json atomically, 0600 — it sits next to the token."""
    ensure_dirs()
    path = config_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


#: Keys `config set` will accept. `folder_ids` is a cache, not a setting.
SETTABLE_KEYS = ("folder",)
