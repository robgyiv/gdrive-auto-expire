"""Expiry logic: decide what is due, act on it, record the outcome.

Kept free of argparse and of credential handling so it can be tested against a
fake Drive and a frozen clock. Idempotent and forgiving by design — a share
already revoked by hand is a success, not an error, and a genuine failure is
recorded and retried on the next tick rather than dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import state
from .drive import Drive
from .notify import get_logger


@dataclass
class Outcome:
    record: dict
    action: str
    status: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.status == state.STATUS_FAILED


@dataclass
class Result:
    outcomes: list[Outcome] = field(default_factory=list)
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.outcomes)

    @property
    def errors(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.is_error]


def due(data: dict, at: datetime) -> list[dict]:
    """Pending records whose deadline has passed, oldest deadline first."""
    pending = [r for r in data.get("shares", []) if state.is_pending(r)]
    ready = [r for r in pending if state.from_iso(r["expires_at"]) <= at]
    return sorted(ready, key=lambda r: r["expires_at"])


def expire_one(drive: Drive, record: dict, *, at: datetime) -> Outcome:
    """Apply a record's expiry action and mutate the record in place."""
    action = record.get("action", state.ACTION_REVOKE)
    name = record.get("name", record["id"])
    try:
        if action == state.ACTION_DELETE:
            result = drive.trash(record["file_id"])
        else:
            result = drive.revoke(record["file_id"], record["permission_id"])
    except Exception as exc:  # noqa: BLE001 - recorded and retried next tick
        record["status"] = state.STATUS_FAILED
        record["last_error"] = f"{type(exc).__name__}: {exc}"
        return Outcome(record, action, state.STATUS_FAILED, f"{name}: {exc}")

    if result == "missing":
        record["status"] = state.STATUS_MISSING
        message = f"file already gone from Drive: {name}"
    elif result == "already":
        record["status"] = state.STATUS_EXPIRED
        message = f"public access was already revoked: {name}"
    elif result == "trashed":
        record["status"] = state.STATUS_EXPIRED
        message = f"moved to Drive trash: {name}"
    else:
        record["status"] = state.STATUS_EXPIRED
        message = f"revoked public access: {name}"

    record["completed_at"] = state.to_iso(at)
    record["last_error"] = None
    return Outcome(record, action, record["status"], message)


def run(
    drive: Drive | None,
    data: dict,
    *,
    at: datetime | None = None,
    dry_run: bool = False,
    notifier=None,
) -> Result:
    """Expire everything due in ``data``, mutating it in place.

    ``drive`` may be None only for a dry run, which is what lets the CLI report
    what is due without needing credentials.
    """
    at = at or state.now()
    result = Result(dry_run=dry_run)
    logger = get_logger()

    for record in due(data, at):
        if dry_run:
            action = record.get("action", state.ACTION_REVOKE)
            verb = "would trash" if action == state.ACTION_DELETE else "would revoke"
            result.outcomes.append(
                Outcome(record, action, record["status"], f"{verb}: {record['name']}")
            )
            continue

        if drive is None:
            raise ValueError("a Drive client is required unless dry_run is set")

        outcome = expire_one(drive, record, at=at)
        result.outcomes.append(outcome)
        if outcome.is_error:
            logger.error("%s", outcome.message)
        else:
            logger.info("%s", outcome.message)
            if notifier is not None:
                notifier("gdrive-auto-expire", outcome.message)

    return result
