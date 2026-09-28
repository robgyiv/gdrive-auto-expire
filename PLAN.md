# gdrive-auto-expire

## Context

Sharing a file (a CV, a document) with someone outside email or LinkedIn means uploading it to Drive and
setting it public-with-link. The problem is the cleanup: the link stays public forever because revoking it
is a manual step that's easy to forget. This tool makes the expiry part of the share action — you state the
lifetime up front (`--days 14`) and the revoke happens on its own.

**The core constraint:** Drive's API cannot do this for us. `expirationTime` on a permission is
[valid only for `user` and `group` permissions](https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions) —
never for `anyone` (link sharing), which is exactly the permission type this tool creates. So the deadline
has to be tracked locally and acted on by a scheduled process. That is the whole reason this is a stateful
tool with a cron job rather than a single API call.

**Decisions made:** local state file + cron sweep; the tool manages its own crontab line; full command
surface in v1; log file plus a macOS notification when something expires; uploads go to a configurable
destination folder defaulting to `gdrive-auto-expire/`, which the tool creates and owns.

**Known limitation, accepted:** cron does not replay runs missed while the Mac was asleep or off. With a
15-minute interval the next tick after wake catches up, so the practical cost is a late revoke — inherent
to any local scheduler, and the sweep is idempotent so lateness is the only failure mode.

## Shape of it

```
$ gdrive-auto-expire share --file ~/Documents/cv.pdf --days 14
Uploaded cv.pdf to gdrive-auto-expire/, public link active, expires 2026-10-12 09:14 (revoke)
https://drive.google.com/file/d/1AbC.../view

$ gdrive-auto-expire list
ID        NAME     EXPIRES            IN        ON EXPIRY
a3f91c2e  cv.pdf   2026-10-12 09:14   13d 22h   revoke

# hourly-ish, from cron:
$ gdrive-auto-expire sweep
revoked public access: cv.pdf
```

## Commands

| Command | Behaviour |
|---|---|
| `share --file PATH --days N [--delete] [--folder PATH]` | Upload into the destination folder, make public-with-link, record deadline. `--days` accepts fractions (`0.01`) which makes manual testing trivial. `--folder` overrides the destination for one share; `--folder ''` puts it in My Drive root. |
| `config get [KEY] \| set KEY VALUE` | Read/write settings. `folder` is the default destination — `config set folder archive/cv` changes it permanently. |
| `list [--all]` | Active shares with time remaining; `--all` includes completed ones. |
| `revoke ID` | Expire now, ignoring the deadline. |
| `extend ID --days N` | Push the deadline out from *now*. |
| `sweep [--dry-run]` | Process everything due. What cron calls. |
| `auth login \| logout \| status` | OAuth flow; show which account is connected. |
| `cron install [--every 15m] \| uninstall \| status` | Manage the tagged crontab line. |

`ID` accepts any unique prefix of the short id, or the filename when unambiguous.

## Files

New modules under `src/gdrive_auto_expire/`:

- **`cli.py`** — argparse subcommands, output formatting. `__init__.main` re-exported to keep the existing
  `[project.scripts]` entry point in `pyproject.toml` working.
- **`__main__.py`** — so `python -m gdrive_auto_expire` works; the fallback cron invocation if the console
  script isn't on an absolute path.
- **`config.py`** — paths and constants, all in one place.
- **`auth.py`** — OAuth desktop flow (`InstalledAppFlow.run_local_server`), token load/refresh/save.
- **`drive.py`** — thin Drive wrapper: `upload`, `make_public`, `revoke`, `trash`, `ensure_folder`. The only
  module that touches the API, so tests stub exactly one seam.
- **`state.py`** — load/save with `flock` + atomic `os.replace`; lookup by id prefix.
- **`sweep.py`** — the expiry logic, pure enough to test against a fake clock.
- **`cron.py`** — crontab read/rewrite.
- **`notify.py`** — logging setup and the macOS notification.

Dependencies to add: `google-api-python-client`, `google-auth-oauthlib`, `google-auth`. CLI parsing stays on
stdlib `argparse` — no need for click/typer at this size.

## Paths

```
~/.config/gdrive-auto-expire/client_secret.json   OAuth client (user-provided, 0600)
~/.config/gdrive-auto-expire/token.json           refresh token (0600)
~/.config/gdrive-auto-expire/config.json          settings + resolved folder-id cache
~/.local/state/gdrive-auto-expire/shares.json     state
~/.local/state/gdrive-auto-expire/lock            flock target
~/.local/state/gdrive-auto-expire/sweep.log       log
```

The log lives in the state dir rather than `~/Library/Logs` on purpose: cron on modern macOS hits TCC
restrictions on `~/Library`, and a silently unwritable log from a background job is a bad failure mode.

## State record

```json
{
  "version": 1,
  "shares": [{
    "id": "a3f91c2e",
    "file_id": "1AbC...", "name": "cv.pdf",
    "link": "https://drive.google.com/file/d/1AbC.../view",
    "permission_id": "anyoneWithLink",
    "created_at": "2026-09-28T09:14:02Z",
    "expires_at": "2026-10-12T09:14:02Z",
    "action": "revoke",
    "status": "active",
    "completed_at": null, "last_error": null
  }]
}
```

Times stored UTC, displayed local. `status` moves `active` → `expired` | `missing` (file already gone from
Drive) | `failed` (error, retried next sweep).

## Implementation notes

**Auth.** Scope is `https://www.googleapis.com/auth/drive.file` — the app only ever sees files it created,
which is the right blast radius here and is also
[Google's only non-sensitive Drive scope](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
so no verification review. One setup trap worth documenting loudly in the README: an OAuth client left in
**Testing** publishing status issues refresh tokens that
[expire after 7 days](https://developers.google.com/identity/protocols/oauth2). Publish to "In Production"
at setup or you'll be re-authing constantly.

**Upload.** `files().create` with `MediaFileUpload(resumable=True)`, `fields='id,name,webViewLink'`.

**Destination folder.** Defaults to `gdrive-auto-expire/` at My Drive root. The tool creates it on first
use and caches the id in `config.json`; `--folder` overrides for a single share, `config set folder <path>`
changes the default permanently. Nested paths work (`gdrive-auto-expire/recruiters`) — each segment is
resolved among the tool's own folders and created if missing, walking down the chain. A cached id that 404s
(you deleted the folder in Drive) is discarded and the folder recreated, so the tool self-heals rather than
erroring.

```json
{ "folder": "gdrive-auto-expire",
  "folder_ids": { "gdrive-auto-expire": "1XyZ...",
                  "gdrive-auto-expire/recruiters": "1QrS..." } }
```

Resolution is `files().list` with `q="name='gdrive-auto-expire' and
mimeType='application/vnd.google-apps.folder' and 'root' in parents and trashed=false"`, then
`files().create` with the folder mimeType if there's no hit.

**The `drive.file` constraint, written down so it isn't rediscovered later.** Every folder in the path must
be one this tool created. It cannot upload into — or even see — a folder you made by hand in the Drive web
UI; that returns 404 on the parent and the `files().list` lookup above comes back empty. So if you later
create folders manually and wonder why the tool ignores them, this is why, and the fix is moving to the
restricted `drive` scope with the verification cost that carries.

**Share.** `permissions().create(body={'type':'anyone','role':'reader','allowFileDiscovery':False})`, storing
the returned permission id.

**`--delete` trashes rather than permanently deletes.** Access is gone either way, but trash is recoverable
for 30 days — the safer default for an unattended background job acting on a two-week-old decision. Flagging
it because it's a small liberty with your wording; swapping to `files().delete` is a one-line change if
you'd rather it be permanent, or it can be a separate `--purge` flag.

**Sweep is idempotent and forgiving.** A 404 on the permission means someone already revoked it — success,
not an error. A 404 on the file means it's gone — mark `missing`, done. Errors are recorded in `last_error`
and retried on the next tick rather than dropped. Every interactive command opportunistically sweeps first,
so the tool self-heals even with cron broken.

**Locking.** Cron and an interactive command can collide. Single `flock`: sweep takes it non-blocking and
exits quietly if held; interactive commands block briefly. Writes go through tempfile + `os.replace`.

**Cron line.** Read `crontab -l` (treat "no crontab for user" as empty), drop any line carrying the
`# gdrive-auto-expire` tag, append the new one, write back via `crontab -`. Tagging means we never disturb
your other entries, and install is idempotent.

```
*/15 * * * * /abs/path/gdrive-auto-expire sweep >> ~/.local/state/gdrive-auto-expire/sweep.log 2>&1  # gdrive-auto-expire
```

Absolute paths throughout, since cron's environment is nearly empty.

**Notification.** `osascript -e 'display notification ...'` on expiry. Honest caveat: osascript from cron
runs outside the Aqua session bootstrap namespace and the notification may not appear. The code attempts it,
falls back silently to log-only on failure, and the README documents the
`launchctl asuser $UID osascript ...` wrapper as the fix if you find they don't show up.

## Verification

1. `uv sync`, then `gdrive-auto-expire auth login` → browser consent → `auth status` shows the account.
2. `gdrive-auto-expire share --file <test.pdf> --days 0.01` (~15 min) — check Drive in a browser: a
   `gdrive-auto-expire/` folder now exists at My Drive root with the file in it. Open the printed link in
   a private window to confirm it's genuinely public.
3. `gdrive-auto-expire list` shows it counting down.
4. `--folder recruiters/acme` on a second share creates the nested chain; `config set folder archive` then a
   bare `share` lands in `archive/`. Delete `gdrive-auto-expire/` in the Drive UI and share again — it's
   recreated rather than erroring on the stale cached id.
5. `gdrive-auto-expire sweep --dry-run` — reports nothing due yet.
6. Wait out the deadline, run `sweep` — private window now 404s, notification fires, log line written,
   `list --all` shows `expired`. Run `sweep` again: no-op, no error.
7. Same cycle with `--delete`, confirming the file lands in Drive's trash.
8. `cron install --every 15m` then `crontab -l` to confirm one tagged line; `cron install` again to confirm
   it's idempotent; `cron uninstall` to confirm it removes only that line.
9. `uv run pytest` — unit tests over a fake Drive service and a frozen clock: due/not-due selection, revoke
   vs delete, 404 handling, idempotency, state round-trip under concurrent writes, folder path resolution
   (create, reuse cached id, recover from a stale one), cron line add/update/remove.
