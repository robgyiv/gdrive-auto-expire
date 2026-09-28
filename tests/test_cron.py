"""Crontab line add/update/remove against a fake `crontab` binary."""

from __future__ import annotations

import pytest

from gdrive_auto_expire import config, cron


@pytest.fixture
def fake_crontab(monkeypatch):
    """Stand in for the crontab binary, holding its content in memory."""

    class Crontab:
        def __init__(self):
            self.content = ""
            self.missing = True  # no crontab for user, until something is written

        def run(self, argv, **kwargs):
            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            proc = Proc()
            if argv[:2] == ["crontab", "-l"]:
                if self.missing:
                    proc.returncode = 1
                    proc.stderr = "crontab: no crontab for robbie\n"
                else:
                    proc.stdout = self.content
            elif argv[:2] == ["crontab", "-"]:
                self.content = kwargs.get("input", "")
                self.missing = False
            return proc

    fake = Crontab()
    monkeypatch.setattr(cron.subprocess, "run", lambda argv, **kw: fake.run(argv, **kw))
    return fake


@pytest.mark.parametrize(
    "value,expected",
    [
        ("15m", "*/15 * * * *"),
        ("15", "*/15 * * * *"),
        ("1m", "* * * * *"),
        ("30 minutes", "*/30 * * * *"),
        ("2h", "0 */2 * * *"),
        ("6 hours", "0 */6 * * *"),
    ],
)
def test_parse_every(value, expected):
    assert cron.parse_every(value) == expected


@pytest.mark.parametrize("value", ["0m", "90m", "24h", "banana", "", "-5m"])
def test_parse_every_rejects_the_unexpressible(value):
    with pytest.raises(cron.CronError):
        cron.parse_every(value)


def test_no_crontab_reads_as_empty(fake_crontab):
    assert cron.read_crontab() == ""


def test_read_crontab_surfaces_real_errors(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "crontab: permission denied"

    monkeypatch.setattr(cron.subprocess, "run", lambda *a, **kw: Proc())
    with pytest.raises(cron.CronError, match="permission denied"):
        cron.read_crontab()


def test_install_writes_one_tagged_line(fake_crontab):
    line = cron.install("15m")
    assert config.CRON_TAG in line
    assert "sweep" in line
    assert str(config.log_path()) in line
    assert fake_crontab.content.strip() == line


def test_install_is_idempotent(fake_crontab):
    cron.install("15m")
    cron.install("15m")
    assert len(cron.status()) == 1


def test_reinstall_replaces_the_schedule(fake_crontab):
    cron.install("15m")
    cron.install("1h")
    lines = cron.status()
    assert len(lines) == 1
    assert lines[0].startswith("0 */1 * * *")


def test_install_leaves_other_entries_alone(fake_crontab):
    fake_crontab.missing = False
    fake_crontab.content = "0 9 * * * /usr/bin/backup\n@reboot /usr/bin/thing\n"
    cron.install("15m")
    assert "/usr/bin/backup" in fake_crontab.content
    assert "@reboot /usr/bin/thing" in fake_crontab.content
    assert len(cron.status()) == 1


def test_uninstall_removes_only_our_line(fake_crontab):
    fake_crontab.missing = False
    fake_crontab.content = "0 9 * * * /usr/bin/backup\n"
    cron.install("15m")
    assert cron.uninstall() is True
    assert fake_crontab.content.strip() == "0 9 * * * /usr/bin/backup"
    assert cron.status() == []


def test_uninstall_when_nothing_installed(fake_crontab):
    assert cron.uninstall() is False


def test_status_is_empty_before_install(fake_crontab):
    assert cron.status() == []


def test_entry_command_is_absolute():
    command = cron.entry_command()
    assert command.startswith("/")
    assert "gdrive" in command
