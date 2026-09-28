"""Command line surface: argparse subcommands and output formatting."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import auth, config, cron, notify, state
from .drive import Drive, build_service, resolve_folder
from .sweep import run as run_sweep


class CommandError(RuntimeError):
    """A problem to report as a one-line message and a non-zero exit."""


# --- formatting ----------------------------------------------------------


def local(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def humanise(delta_seconds: float) -> str:
    """'13d 22h', '4h 09m', '47s' — two units is enough to act on."""
    if delta_seconds <= 0:
        return "due"
    seconds = int(delta_seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def render_table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


# --- shared plumbing -----------------------------------------------------


def _drive() -> Drive:
    return Drive(build_service(auth.require_credentials()))


def _opportunistic_sweep() -> None:
    """Interactive commands sweep first, so the tool self-heals even with cron
    broken. Quiet: failures here must not derail the command the user asked for.
    """
    now = state.now()
    try:
        with state.transaction(blocking=False) as data:
            if data is None:
                return
            from .sweep import due

            if not due(data, now):
                return
            drive = _drive()
            result = run_sweep(drive, data, at=now, notifier=notify.notify)
        for outcome in result.outcomes:
            print(outcome.message, file=sys.stderr)
    except Exception:  # noqa: BLE001 - best effort by definition
        return


# --- commands ------------------------------------------------------------


def cmd_share(args) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise CommandError(f"not a file: {path}")
    if args.days <= 0:
        raise CommandError("--days must be greater than zero")

    cfg = config.load_config()
    folder = cfg.get("folder", config.DEFAULT_FOLDER) if args.folder is None else args.folder

    drive = _drive()
    parent_id = resolve_folder(drive, folder, cfg)
    config.save_config(cfg)  # persist any folder ids we just resolved

    uploaded = drive.upload(path, parent_id=parent_id)
    permission = drive.make_public(uploaded["id"])

    created = state.now()
    expires = state.deadline_from_days(args.days, created)
    action = state.ACTION_DELETE if args.delete else state.ACTION_REVOKE
    record = state.make_record(
        file_id=uploaded["id"],
        name=uploaded.get("name", path.name),
        link=uploaded.get("webViewLink", ""),
        permission_id=permission["id"],
        expires_at=expires,
        action=action,
        created_at=created,
    )
    with state.transaction() as data:
        data["shares"].append(record)

    where = f"{folder.rstrip('/')}/" if folder else "My Drive"
    verb = "deletes" if args.delete else "revoke"
    print(
        f"Uploaded {record['name']} to {where}, public link active, "
        f"expires {local(expires)} ({verb})"
    )
    if record["link"]:
        print(record["link"])
    print(f"id {record['id']}")
    return 0


def cmd_list(args) -> int:
    _opportunistic_sweep()
    data = state.load()
    shares = data.get("shares", [])
    if not args.all:
        shares = [r for r in shares if state.is_pending(r)]
    if not shares:
        print("no active shares" if not args.all else "no shares recorded")
        return 0

    now = state.now()
    rows = []
    for record in sorted(shares, key=lambda r: r["expires_at"]):
        expires = state.from_iso(record["expires_at"])
        remaining = (
            humanise((expires - now).total_seconds())
            if state.is_pending(record)
            else "-"
        )
        status = record["status"]
        if status == state.STATUS_FAILED and record.get("last_error"):
            status = f"failed ({record['last_error'][:40]})"
        rows.append(
            [
                record["id"],
                record["name"],
                local(expires),
                remaining,
                record.get("action", state.ACTION_REVOKE) if state.is_pending(record) else status,
            ]
        )
    print(render_table(rows, ["ID", "NAME", "EXPIRES", "IN", "ON EXPIRY"]))
    return 0


def _resolve(data: dict, needle: str) -> dict:
    try:
        return state.find(data, needle)
    except state.AmbiguousMatch as exc:
        raise CommandError(str(exc)) from exc
    except LookupError as exc:
        raise CommandError(str(exc)) from exc


def cmd_revoke(args) -> int:
    drive = _drive()
    now = state.now()
    from .sweep import expire_one

    with state.transaction() as data:
        record = _resolve(data, args.id)
        if not state.is_pending(record):
            raise CommandError(
                f"{record['name']} is already {record['status']}, nothing to revoke"
            )
        # Bring the deadline forward and let the normal path do the work, so
        # interactive revoke and the cron sweep cannot drift apart.
        record["expires_at"] = state.to_iso(now)
        outcome = expire_one(drive, record, at=now)
    print(outcome.message)
    return 1 if outcome.is_error else 0


def cmd_extend(args) -> int:
    if args.days <= 0:
        raise CommandError("--days must be greater than zero")
    with state.transaction() as data:
        record = _resolve(data, args.id)
        if not state.is_pending(record):
            raise CommandError(
                f"{record['name']} is already {record['status']} and cannot be extended"
            )
        expires = state.deadline_from_days(args.days)
        record["expires_at"] = state.to_iso(expires)
        # An extension is a decision to keep the share: clear a stale failure so
        # the record reads as healthy again.
        if record["status"] == state.STATUS_FAILED:
            record["status"] = state.STATUS_ACTIVE
            record["last_error"] = None
        name = record["name"]
    print(f"{name} now expires {local(expires)} ({humanise(args.days * 86400)} from now)")
    return 0


def cmd_sweep(args) -> int:
    notify.setup_logging()
    now = state.now()
    # Non-blocking: if another sweep holds the lock, this tick steps aside.
    with state.transaction(blocking=False) as data:
        if data is None:
            if args.verbose:
                print("another sweep is already running", file=sys.stderr)
            return 0
        from .sweep import due

        pending = due(data, now)
        if not pending:
            if args.dry_run or args.verbose:
                print("nothing due")
            return 0
        drive = None if args.dry_run else _drive()
        result = run_sweep(
            drive,
            data,
            at=now,
            dry_run=args.dry_run,
            notifier=None if args.dry_run else notify.notify,
        )
    for outcome in result.outcomes:
        print(outcome.message)
    return 1 if result.errors else 0


def cmd_config(args) -> int:
    cfg = config.load_config()
    if args.config_action == "get":
        if args.key:
            if args.key not in cfg:
                raise CommandError(f"unknown key {args.key!r}")
            value = cfg[args.key]
            print(value if not isinstance(value, dict) else _fmt_dict(value))
            return 0
        for key in sorted(cfg):
            if key == "folder_ids":
                continue
            print(f"{key} = {cfg[key]}")
        return 0

    if args.key not in config.SETTABLE_KEYS:
        raise CommandError(
            f"{args.key!r} is not settable — try: {', '.join(config.SETTABLE_KEYS)}"
        )
    cfg[args.key] = args.value
    config.save_config(cfg)
    print(f"{args.key} = {args.value}")
    return 0


def _fmt_dict(value: dict) -> str:
    return "\n".join(f"{k} = {v}" for k, v in sorted(value.items())) or "(empty)"


def cmd_auth(args) -> int:
    if args.auth_action == "login":
        creds = auth.login()
        print(f"signed in; token saved to {config.token_path()}")
        _print_account(creds)
        return 0
    if args.auth_action == "logout":
        print("signed out" if auth.logout() else "was not signed in")
        return 0

    creds = auth.load_credentials()
    if creds is None:
        print("not signed in")
        return 1
    print("signed in")
    _print_account(creds)
    return 0


def _print_account(creds) -> None:
    """Best effort: the account email needs an API round trip."""
    try:
        info = (
            build_service(creds)
            .about()
            .get(fields="user(emailAddress,displayName),storageQuota")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return
    user = info.get("user") or {}
    email = user.get("emailAddress")
    if email:
        print(f"account: {email}")
    expiry = getattr(creds, "expiry", None)
    if expiry:
        print(f"token valid until {local(expiry.replace(tzinfo=timezone.utc))}")


def cmd_cron(args) -> int:
    if args.cron_action == "install":
        line = cron.install(args.every)
        print("installed crontab entry:")
        print(f"  {line}")
        return 0
    if args.cron_action == "uninstall":
        print("removed crontab entry" if cron.uninstall() else "no crontab entry to remove")
        return 0

    lines = cron.status()
    if not lines:
        print("no crontab entry installed")
        return 1
    print("installed:")
    for line in lines:
        print(f"  {line}")
    hint = cron.env_hint()
    if hint:
        print(f"warning: {hint}", file=sys.stderr)
    return 0


# --- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=config.APP_NAME,
        description="Share files on Google Drive with a public link that revokes itself.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    share = sub.add_parser("share", help="upload a file and start its countdown")
    share.add_argument("--file", required=True, help="path to the file to share")
    share.add_argument(
        "--days",
        required=True,
        type=float,
        help="lifetime in days; fractions allowed (0.01 is about 15 minutes)",
    )
    share.add_argument(
        "--delete",
        action="store_true",
        help="move the file to Drive's trash on expiry instead of only revoking the link",
    )
    share.add_argument(
        "--folder",
        default=None,
        help="destination folder for this share; '' for My Drive root",
    )
    share.set_defaults(func=cmd_share)

    listing = sub.add_parser("list", help="show shares and time remaining")
    listing.add_argument("--all", action="store_true", help="include completed shares")
    listing.set_defaults(func=cmd_list)

    revoke = sub.add_parser("revoke", help="expire a share now")
    revoke.add_argument("id", help="share id prefix or filename")
    revoke.set_defaults(func=cmd_revoke)

    extend = sub.add_parser("extend", help="push a deadline out from now")
    extend.add_argument("id", help="share id prefix or filename")
    extend.add_argument("--days", required=True, type=float)
    extend.set_defaults(func=cmd_extend)

    sweep = sub.add_parser("sweep", help="process everything due (what cron calls)")
    sweep.add_argument("--dry-run", action="store_true", help="report without acting")
    sweep.add_argument("-v", "--verbose", action="store_true")
    sweep.set_defaults(func=cmd_sweep)

    cfg = sub.add_parser("config", help="read or write settings")
    cfg_sub = cfg.add_subparsers(dest="config_action", required=True)
    cfg_get = cfg_sub.add_parser("get")
    cfg_get.add_argument("key", nargs="?")
    cfg_set = cfg_sub.add_parser("set")
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")
    cfg.set_defaults(func=cmd_config)

    auth_parser = sub.add_parser("auth", help="connect a Google account")
    auth_sub = auth_parser.add_subparsers(dest="auth_action", required=True)
    for name in ("login", "logout", "status"):
        auth_sub.add_parser(name)
    auth_parser.set_defaults(func=cmd_auth)

    cron_parser = sub.add_parser("cron", help="manage the scheduled sweep")
    cron_sub = cron_parser.add_subparsers(dest="cron_action", required=True)
    cron_install = cron_sub.add_parser("install")
    cron_install.add_argument("--every", default="15m", help="e.g. 15m, 1h")
    cron_sub.add_parser("uninstall")
    cron_sub.add_parser("status")
    cron_parser.set_defaults(func=cmd_cron)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CommandError, auth.AuthError, cron.CronError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
