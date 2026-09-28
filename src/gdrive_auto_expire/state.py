"""The share records: load, save, lookup.

Cron and an interactive command can run at the same moment, so all writes go
through a single ``flock`` and land via tempfile + ``os.replace``. Readers of
this module get plain dicts — the schema is small enough that a dataclass would
only add ceremony.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

STATE_VERSION = 1

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_MISSING = "missing"
STATUS_FAILED = "failed"

#: Statuses that mean the record is done with; `list` hides these by default.
TERMINAL_STATUSES = (STATUS_EXPIRED, STATUS_MISSING)

ACTION_REVOKE = "revoke"
ACTION_DELETE = "delete"


def now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def deadline_from_days(days: float, start: datetime | None = None) -> datetime:
    """Fractional days are supported on purpose: --days 0.01 is ~15 minutes,
    which makes an end-to-end manual test take a coffee break rather than a
    fortnight."""
    return (start or now()) + timedelta(days=days)


def new_id() -> str:
    return secrets.token_hex(4)


@contextlib.contextmanager
def locked(blocking: bool = True):
    """Hold the advisory lock for the body.

    Yields True when the lock was taken. With ``blocking=False`` a lock already
    held by someone else yields False instead of raising, which is how sweep
    steps aside for an in-flight run.
    """
    config.ensure_dirs()
    fd = os.open(config.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def empty() -> dict:
    return {"version": STATE_VERSION, "shares": []}


def load() -> dict:
    try:
        raw = json.loads(config.state_path().read_text())
    except FileNotFoundError:
        return empty()
    except json.JSONDecodeError:
        return empty()
    if not isinstance(raw, dict) or not isinstance(raw.get("shares"), list):
        return empty()
    raw.setdefault("version", STATE_VERSION)
    return raw


def save(data: dict) -> None:
    config.ensure_dirs()
    path: Path = config.state_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@contextlib.contextmanager
def transaction(blocking: bool = True):
    """Load, hand over, save — under the lock.

    Yields None when the lock could not be taken (``blocking=False`` only), so
    callers can distinguish "nothing to do" from "someone else is doing it".
    """
    with locked(blocking=blocking) as acquired:
        if not acquired:
            yield None
            return
        data = load()
        yield data
        save(data)


def make_record(
    *,
    file_id: str,
    name: str,
    link: str,
    permission_id: str,
    expires_at: datetime,
    action: str = ACTION_REVOKE,
    created_at: datetime | None = None,
    share_id: str | None = None,
) -> dict:
    return {
        "id": share_id or new_id(),
        "file_id": file_id,
        "name": name,
        "link": link,
        "permission_id": permission_id,
        "created_at": to_iso(created_at or now()),
        "expires_at": to_iso(expires_at),
        "action": action,
        "status": STATUS_ACTIVE,
        "completed_at": None,
        "last_error": None,
    }


def is_pending(record: dict) -> bool:
    """Pending records are the ones sweep still has work to do on. A `failed`
    record stays pending so the next tick retries it."""
    return record.get("status") in (STATUS_ACTIVE, STATUS_FAILED)


class AmbiguousMatch(LookupError):
    def __init__(self, needle: str, matches: list[dict]):
        self.needle = needle
        self.matches = matches
        names = ", ".join(f"{m['id']} ({m['name']})" for m in matches)
        super().__init__(f"{needle!r} matches more than one share: {names}")


def find(data: dict, needle: str) -> dict:
    """Resolve a share by id prefix, or by filename when unambiguous.

    Raises LookupError when nothing matches and AmbiguousMatch when several do.
    """
    shares = data.get("shares", [])
    for record in shares:
        if record.get("id") == needle:
            return record

    by_prefix = [r for r in shares if str(r.get("id", "")).startswith(needle)]
    by_name = [r for r in shares if r.get("name") == needle]
    # Prefer a pending record when a name has been reused across shares.
    for candidates in (by_prefix, by_name):
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            pending = [r for r in candidates if is_pending(r)]
            if len(pending) == 1:
                return pending[0]
            raise AmbiguousMatch(needle, candidates)

    raise LookupError(f"no share matches {needle!r}")
