"""Due selection, revoke vs delete, 404 handling and idempotency."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from gdrive_auto_expire import state, sweep


@pytest.fixture
def clock():
    return state.now().replace(microsecond=0)


def share(drive, tmp_path, name="cv.pdf", *, action=state.ACTION_REVOKE, expires_in):
    """Upload through the fake Drive and build the matching state record."""
    path = tmp_path / name
    path.write_text("x")
    uploaded = drive.upload(Path(path))
    permission = drive.make_public(uploaded["id"])
    return state.make_record(
        file_id=uploaded["id"],
        name=uploaded["name"],
        link=uploaded["webViewLink"],
        permission_id=permission["id"],
        expires_at=state.now() + expires_in,
        action=action,
    )


def test_due_selects_only_passed_deadlines(drive, tmp_path, clock):
    past = share(drive, tmp_path, "past.pdf", expires_in=-timedelta(minutes=1))
    future = share(drive, tmp_path, "future.pdf", expires_in=timedelta(days=1))
    data = {"version": 1, "shares": [past, future]}
    assert [r["name"] for r in sweep.due(data, clock)] == ["past.pdf"]


def test_due_is_ordered_by_deadline(drive, tmp_path, clock):
    late = share(drive, tmp_path, "late.pdf", expires_in=-timedelta(minutes=1))
    early = share(drive, tmp_path, "early.pdf", expires_in=-timedelta(hours=5))
    data = {"version": 1, "shares": [late, early]}
    assert [r["name"] for r in sweep.due(data, clock)] == ["early.pdf", "late.pdf"]


def test_due_ignores_completed_records(drive, tmp_path, clock):
    done = share(drive, tmp_path, expires_in=-timedelta(days=1))
    done["status"] = state.STATUS_EXPIRED
    assert sweep.due({"version": 1, "shares": [done]}, clock) == []


def test_due_retries_failed_records(drive, tmp_path, clock):
    failed = share(drive, tmp_path, expires_in=-timedelta(days=1))
    failed["status"] = state.STATUS_FAILED
    assert len(sweep.due({"version": 1, "shares": [failed]}, clock)) == 1


def test_revoke_removes_the_permission(drive, service, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))
    outcome = sweep.expire_one(drive, record, at=clock)
    assert outcome.status == state.STATUS_EXPIRED
    assert "revoked public access" in outcome.message
    assert service.permissions_store[record["file_id"]] == set()
    # The file itself stays put on a plain revoke.
    assert service.files_store[record["file_id"]]["trashed"] is False


def test_delete_action_trashes_rather_than_destroying(drive, service, tmp_path, clock):
    record = share(drive, tmp_path, action=state.ACTION_DELETE, expires_in=-timedelta(minutes=1))
    outcome = sweep.expire_one(drive, record, at=clock)
    assert outcome.status == state.STATUS_EXPIRED
    assert "trash" in outcome.message
    assert service.files_store[record["file_id"]]["trashed"] is True


def test_permission_already_gone_is_success(drive, service, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))
    service.permissions_store[record["file_id"]].clear()  # revoked by hand
    outcome = sweep.expire_one(drive, record, at=clock)
    assert outcome.status == state.STATUS_EXPIRED
    assert not outcome.is_error
    assert "already revoked" in outcome.message


def test_file_gone_marks_missing(drive, service, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))
    service.drop_file(record["file_id"])
    outcome = sweep.expire_one(drive, record, at=clock)
    assert outcome.status == state.STATUS_MISSING
    assert not outcome.is_error


def test_delete_of_a_gone_file_marks_missing(drive, service, tmp_path, clock):
    record = share(drive, tmp_path, action=state.ACTION_DELETE, expires_in=-timedelta(minutes=1))
    service.drop_file(record["file_id"])
    outcome = sweep.expire_one(drive, record, at=clock)
    assert outcome.status == state.STATUS_MISSING


def test_unexpected_error_is_recorded_and_retried(drive, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))

    class Boom:
        def revoke(self, *a, **kw):
            raise RuntimeError("network down")

    outcome = sweep.expire_one(Boom(), record, at=clock)
    assert outcome.is_error
    assert record["status"] == state.STATUS_FAILED
    assert "network down" in record["last_error"]
    assert record["completed_at"] is None
    # Still pending, so the next tick picks it up again.
    assert sweep.due({"version": 1, "shares": [record]}, clock)


def test_recovering_after_a_failure_clears_the_error(drive, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))
    record["status"] = state.STATUS_FAILED
    record["last_error"] = "network down"
    outcome = sweep.expire_one(drive, record, at=clock)
    assert outcome.status == state.STATUS_EXPIRED
    assert record["last_error"] is None
    assert record["completed_at"] == state.to_iso(clock)


def test_run_is_idempotent(drive, service, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))
    data = {"version": 1, "shares": [record]}
    first = sweep.run(drive, data, at=clock)
    assert len(first.outcomes) == 1
    before = len(service.calls)
    second = sweep.run(drive, data, at=clock)
    assert second.outcomes == []
    assert len(service.calls) == before  # no API traffic at all on the second pass


def test_dry_run_touches_nothing(drive, service, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))
    data = {"version": 1, "shares": [record]}
    before = len(service.calls)
    result = sweep.run(None, data, at=clock, dry_run=True)
    assert [o.message for o in result.outcomes] == ["would revoke: cv.pdf"]
    assert record["status"] == state.STATUS_ACTIVE
    assert len(service.calls) == before


def test_dry_run_names_the_delete_action(drive, tmp_path, clock):
    record = share(drive, tmp_path, action=state.ACTION_DELETE, expires_in=-timedelta(minutes=1))
    result = sweep.run(None, data := {"version": 1, "shares": [record]}, at=clock, dry_run=True)
    assert "would trash" in result.outcomes[0].message
    assert data["shares"][0]["status"] == state.STATUS_ACTIVE


def test_run_without_drive_requires_dry_run(drive, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))
    with pytest.raises(ValueError):
        sweep.run(None, {"version": 1, "shares": [record]}, at=clock)


def test_notifier_fires_once_per_expiry(drive, tmp_path, clock):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))
    seen = []
    sweep.run(
        drive,
        {"version": 1, "shares": [record]},
        at=clock,
        notifier=lambda title, message: seen.append((title, message)),
    )
    assert len(seen) == 1
    assert "cv.pdf" in seen[0][1]


def test_notifier_is_silent_on_failure(tmp_path, clock, drive):
    record = share(drive, tmp_path, expires_in=-timedelta(minutes=1))

    class Boom:
        def revoke(self, *a, **kw):
            raise RuntimeError("nope")

    seen = []
    result = sweep.run(
        Boom(),
        {"version": 1, "shares": [record]},
        at=clock,
        notifier=lambda *a: seen.append(a),
    )
    assert result.errors
    assert seen == []
