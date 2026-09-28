"""End-to-end command behaviour with the Drive seam stubbed out."""

from __future__ import annotations

from datetime import timedelta

import pytest

from gdrive_auto_expire import cli, config, state
from gdrive_auto_expire.drive import Drive


@pytest.fixture
def cli_drive(service, monkeypatch):
    """Make every command use the fake Drive instead of real credentials."""
    drive = Drive(service)
    monkeypatch.setattr(cli, "_drive", lambda: drive)
    return drive


@pytest.fixture
def a_file(tmp_path):
    path = tmp_path / "cv.pdf"
    path.write_text("pretend pdf")
    return path


def run(*argv):
    return cli.main(list(argv))


def test_share_records_and_reports(cli_drive, service, a_file, capsys):
    assert run("share", "--file", str(a_file), "--days", "14") == 0
    out = capsys.readouterr().out
    assert "cv.pdf" in out
    assert "gdrive-auto-expire/" in out

    shares = state.load()["shares"]
    assert len(shares) == 1
    record = shares[0]
    assert record["status"] == state.STATUS_ACTIVE
    assert record["action"] == state.ACTION_REVOKE
    assert record["permission_id"] in service.permissions_store[record["file_id"]]
    # Roughly 14 days out, allowing for the second the test takes.
    remaining = state.from_iso(record["expires_at"]) - state.now()
    assert timedelta(days=13, hours=23) < remaining <= timedelta(days=14)


def test_share_caches_the_folder_id_for_next_time(cli_drive, a_file):
    run("share", "--file", str(a_file), "--days", "1")
    cached = config.load_config()["folder_ids"]
    assert cached["gdrive-auto-expire"]


def test_share_delete_flag_sets_the_action(cli_drive, a_file):
    run("share", "--file", str(a_file), "--days", "1", "--delete")
    assert state.load()["shares"][0]["action"] == state.ACTION_DELETE


def test_share_folder_override(cli_drive, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "1", "--folder", "recruiters/acme")
    assert "recruiters/acme/" in capsys.readouterr().out
    assert "recruiters/acme" in config.load_config()["folder_ids"]
    # The override is for this share only; the default is untouched.
    assert config.load_config()["folder"] == "gdrive-auto-expire"


def test_share_empty_folder_means_my_drive_root(cli_drive, service, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "1", "--folder", "")
    assert "My Drive" in capsys.readouterr().out
    assert service.folders == {}


def test_share_uses_the_configured_default_folder(cli_drive, a_file):
    run("config", "set", "folder", "archive")
    run("share", "--file", str(a_file), "--days", "1")
    assert "archive" in config.load_config()["folder_ids"]


def test_share_rejects_a_missing_file(cli_drive, tmp_path, capsys):
    assert run("share", "--file", str(tmp_path / "nope.pdf"), "--days", "1") == 1
    assert "not a file" in capsys.readouterr().err


def test_share_rejects_a_nonpositive_lifetime(cli_drive, a_file, capsys):
    assert run("share", "--file", str(a_file), "--days", "0") == 1
    assert "greater than zero" in capsys.readouterr().err


def test_list_shows_countdown(cli_drive, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "14")
    capsys.readouterr()
    assert run("list") == 0
    out = capsys.readouterr().out
    assert "ID" in out and "EXPIRES" in out
    assert "cv.pdf" in out
    assert "13d" in out
    assert "revoke" in out


def test_list_hides_completed_shares_until_all(cli_drive, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "14")
    run("revoke", "cv.pdf")
    capsys.readouterr()

    run("list")
    assert "no active shares" in capsys.readouterr().out

    run("list", "--all")
    out = capsys.readouterr().out
    assert "cv.pdf" in out and "expired" in out


def test_revoke_now_ignores_the_deadline(cli_drive, service, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "14")
    file_id = state.load()["shares"][0]["file_id"]
    capsys.readouterr()

    assert run("revoke", "cv.pdf") == 0
    assert "revoked public access" in capsys.readouterr().out
    assert service.permissions_store[file_id] == set()
    assert state.load()["shares"][0]["status"] == state.STATUS_EXPIRED


def test_revoke_accepts_an_id_prefix(cli_drive, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "14")
    share_id = state.load()["shares"][0]["id"]
    capsys.readouterr()
    assert run("revoke", share_id[:3]) == 0


def test_revoke_twice_is_an_error_not_a_second_api_call(cli_drive, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "14")
    run("revoke", "cv.pdf")
    capsys.readouterr()
    assert run("revoke", "cv.pdf") == 1
    assert "already expired" in capsys.readouterr().err


def test_revoke_unknown_id(cli_drive, capsys):
    assert run("revoke", "ffff") == 1
    assert "no share matches" in capsys.readouterr().err


def test_extend_pushes_the_deadline_out_from_now(cli_drive, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "1")
    before = state.from_iso(state.load()["shares"][0]["expires_at"])
    capsys.readouterr()

    assert run("extend", "cv.pdf", "--days", "7") == 0
    after = state.from_iso(state.load()["shares"][0]["expires_at"])
    assert after > before
    assert timedelta(days=6, hours=23) < after - state.now() <= timedelta(days=7)


def test_extend_clears_a_stale_failure(cli_drive, a_file):
    run("share", "--file", str(a_file), "--days", "1")
    with state.transaction() as data:
        data["shares"][0]["status"] = state.STATUS_FAILED
        data["shares"][0]["last_error"] = "network down"

    run("extend", "cv.pdf", "--days", "7")
    record = state.load()["shares"][0]
    assert record["status"] == state.STATUS_ACTIVE
    assert record["last_error"] is None


def test_extend_refuses_a_completed_share(cli_drive, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "1")
    run("revoke", "cv.pdf")
    capsys.readouterr()
    assert run("extend", "cv.pdf", "--days", "7") == 1
    assert "cannot be extended" in capsys.readouterr().err


def test_sweep_dry_run_reports_without_acting(cli_drive, service, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "14")
    capsys.readouterr()

    assert run("sweep", "--dry-run") == 0
    assert "nothing due" in capsys.readouterr().out

    _expire_now()
    assert run("sweep", "--dry-run") == 0
    assert "would revoke: cv.pdf" in capsys.readouterr().out
    assert state.load()["shares"][0]["status"] == state.STATUS_ACTIVE


def test_sweep_expires_what_is_due_and_is_idempotent(cli_drive, service, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "14")
    _expire_now()
    capsys.readouterr()

    assert run("sweep") == 0
    assert "revoked public access: cv.pdf" in capsys.readouterr().out
    assert state.load()["shares"][0]["status"] == state.STATUS_EXPIRED

    assert run("sweep") == 0
    assert capsys.readouterr().out == ""


def test_sweep_steps_aside_when_the_lock_is_held(cli_drive, a_file, capsys):
    run("share", "--file", str(a_file), "--days", "14")
    _expire_now()
    capsys.readouterr()

    import gdrive_auto_expire.state as state_module

    class Held:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    # A second sweep must find nothing to do rather than double-acting.
    original = state_module.transaction
    try:
        state_module.transaction = lambda blocking=True: Held()
        assert run("sweep", "-v") == 0
    finally:
        state_module.transaction = original
    assert "already running" in capsys.readouterr().err
    assert state.load()["shares"][0]["status"] == state.STATUS_ACTIVE


def test_sweep_exits_nonzero_on_failure(cli_drive, service, a_file, capsys, monkeypatch):
    run("share", "--file", str(a_file), "--days", "14")
    _expire_now()
    capsys.readouterr()

    class Boom(Drive):
        def revoke(self, *a, **kw):
            raise RuntimeError("network down")

    monkeypatch.setattr(cli, "_drive", lambda: Boom(service))
    assert run("sweep") == 1
    record = state.load()["shares"][0]
    assert record["status"] == state.STATUS_FAILED
    assert "network down" in record["last_error"]


def test_interactive_commands_sweep_first(cli_drive, service, a_file, capsys):
    """list must self-heal an overdue share even with cron broken."""
    run("share", "--file", str(a_file), "--days", "14")
    file_id = state.load()["shares"][0]["file_id"]
    _expire_now()
    capsys.readouterr()

    run("list", "--all")
    assert service.permissions_store[file_id] == set()
    assert state.load()["shares"][0]["status"] == state.STATUS_EXPIRED


def test_config_get_and_set(cli_drive, capsys):
    assert run("config", "get") == 0
    out = capsys.readouterr().out
    assert "folder = gdrive-auto-expire" in out
    assert "folder_ids" not in out  # a cache, not a setting

    assert run("config", "set", "folder", "archive/cv") == 0
    capsys.readouterr()
    assert run("config", "get", "folder") == 0
    assert capsys.readouterr().out.strip() == "archive/cv"


def test_config_rejects_unsettable_keys(cli_drive, capsys):
    assert run("config", "set", "folder_ids", "x") == 1
    assert "not settable" in capsys.readouterr().err


def test_config_get_unknown_key(cli_drive, capsys):
    assert run("config", "get", "nonsense") == 1
    assert "unknown key" in capsys.readouterr().err


def test_auth_status_when_signed_out(capsys):
    assert run("auth", "status") == 1
    assert "not signed in" in capsys.readouterr().out


def test_auth_logout_when_signed_out(capsys):
    assert run("auth", "logout") == 0
    assert "was not signed in" in capsys.readouterr().out


def test_humanise():
    assert cli.humanise(-5) == "due"
    assert cli.humanise(45) == "45s"
    assert cli.humanise(90) == "1m 30s"
    assert cli.humanise(3600 * 4 + 540) == "4h 09m"
    assert cli.humanise(86400 * 13 + 3600 * 22) == "13d 22h"


def _expire_now():
    """Backdate every pending share so the next sweep finds it due."""
    with state.transaction() as data:
        for record in data["shares"]:
            record["expires_at"] = state.to_iso(state.now() - timedelta(seconds=1))
