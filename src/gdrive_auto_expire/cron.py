"""Managing our own crontab line.

We read the whole crontab, drop any line carrying our tag, append the new one
and write it back. Tagging is what makes install idempotent and what guarantees
we never disturb anyone else's entries.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

#: `crontab -l` says this on stderr when the user has no crontab at all.
_NO_CRONTAB = "no crontab for"

_INTERVAL = re.compile(r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?$")


class CronError(RuntimeError):
    pass


def parse_every(value: str) -> str:
    """Turn `15m` / `2h` / `30` into the schedule half of a cron line."""
    text = str(value).strip().lower()
    match = _INTERVAL.match(text)
    if not match:
        raise CronError(f"could not read interval {value!r} — try 15m or 2h")
    amount = int(match.group(1))
    unit = (match.group(2) or "m")[0]
    if amount < 1:
        raise CronError("interval must be at least 1 minute")

    if unit == "h":
        if amount > 23:
            raise CronError("hourly intervals above 23h are not expressible in cron")
        return f"0 */{amount} * * *"
    if amount > 59:
        raise CronError("minute intervals above 59m are not expressible in cron")
    if amount == 1:
        return "* * * * *"
    return f"*/{amount} * * * *"


def entry_command() -> str:
    """Absolute command for cron, whose environment is nearly empty.

    Prefers the installed console script; falls back to
    ``<python> -m gdrive_auto_expire`` when it is not on an absolute path we can
    find, which is why ``__main__.py`` exists.
    """
    script = shutil.which("gdrive-auto-expire")
    if script:
        return str(Path(script).resolve())
    return f"{Path(sys.executable).resolve()} -m gdrive_auto_expire"


def build_line(schedule: str) -> str:
    log = config.log_path()
    return (
        f"{schedule} {entry_command()} sweep >> {log} 2>&1  {config.CRON_TAG}"
    )


def read_crontab() -> str:
    proc = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if _NO_CRONTAB in stderr.lower() or not stderr:
            return ""
        raise CronError(f"crontab -l failed: {stderr}")
    return proc.stdout


def write_crontab(content: str) -> None:
    body = content.rstrip("\n")
    payload = (body + "\n") if body else ""
    proc = subprocess.run(
        ["crontab", "-"], input=payload, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise CronError(f"crontab - failed: {(proc.stderr or '').strip()}")


def strip_our_lines(content: str) -> tuple[list[str], list[str]]:
    """Split a crontab into (other lines, ours)."""
    others, ours = [], []
    for line in content.splitlines():
        (ours if config.CRON_TAG in line else others).append(line)
    return others, ours


def install(every: str = "15m") -> str:
    """Add or replace our line. Returns the line installed."""
    schedule = parse_every(every)
    line = build_line(schedule)
    config.ensure_dirs()
    others, _ = strip_our_lines(read_crontab())
    write_crontab("\n".join([*others, line]))
    return line


def uninstall() -> bool:
    """Remove our line, leaving everything else untouched. True if one went."""
    others, ours = strip_our_lines(read_crontab())
    if not ours:
        return False
    write_crontab("\n".join(others))
    return True


def status() -> list[str]:
    """Our installed lines — normally zero or one."""
    _, ours = strip_our_lines(read_crontab())
    return ours


def env_hint() -> str | None:
    """A warning for the `cron status` output when cron looks unusable.

    On modern macOS `cron` needs Full Disk Access to read files under the home
    directory; without it the job runs but every read fails.
    """
    if sys.platform != "darwin":
        return None
    if not os.path.exists("/usr/sbin/cron"):
        return "cron does not appear to be installed at /usr/sbin/cron"
    return None
