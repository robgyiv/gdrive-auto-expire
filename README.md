# gdrive-auto-expire

Share a file on Google Drive with a public link that revokes itself.

```
$ gdrive-auto-expire share --file ~/Documents/cv.pdf --days 14
Uploaded cv.pdf to gdrive-auto-expire/, public link active, expires 2026-10-12 09:14 (revoke)
https://drive.google.com/file/d/1AbC.../view
id a3f91c2e

$ gdrive-auto-expire list
ID        NAME    EXPIRES           IN        ON EXPIRY
a3f91c2e  cv.pdf  2026-10-12 09:14  13d 22h   revoke
```

You state the lifetime when you share, and the revoke happens on its own.

## Why it needs a state file and a cron job

Drive's API cannot expire a link-share for us. `expirationTime` on a permission is
[valid only for `user` and `group` permissions](https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions),
never for `anyone` — which is exactly the permission type link sharing creates. So the deadline is tracked
locally and acted on by a scheduled sweep.

The known cost: cron does not replay runs missed while the Mac was asleep or off. With a 15-minute interval
the next tick after wake catches up, so the practical failure mode is a late revoke, never a missed one — the
sweep is idempotent.

## Setup

1. **Create an OAuth client.** In [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
   enable the Drive API, then create credentials → *OAuth client ID* → *Desktop app*. Download the JSON.

2. **Set the publishing status to "In Production."** On the OAuth consent screen, leaving the app in
   **Testing** means Google [expires refresh tokens after 7 days](https://developers.google.com/identity/protocols/oauth2)
   and you will be re-authing constantly. This is the single most common setup trap.

3. **Install the client secret and sign in:**

   ```sh
   mkdir -p ~/.config/gdrive-auto-expire
   cp ~/Downloads/client_secret_*.json ~/.config/gdrive-auto-expire/client_secret.json
   chmod 600 ~/.config/gdrive-auto-expire/client_secret.json

   uv sync
   gdrive-auto-expire auth login     # opens a browser for consent
   gdrive-auto-expire auth status    # shows the connected account
   ```

4. **Schedule the sweep:**

   ```sh
   gdrive-auto-expire cron install --every 15m
   gdrive-auto-expire cron status
   ```

## Commands

| Command | Behaviour |
|---|---|
| `share --file PATH --days N [--delete] [--folder PATH]` | Upload into the destination folder, make public-with-link, record the deadline. `--days` accepts fractions (`0.01` ≈ 15 minutes), which makes manual testing quick. `--folder` overrides the destination for one share; `--folder ''` uses My Drive root. |
| `list [--all]` | Active shares with time remaining; `--all` includes completed ones. |
| `link [ID ...] [--all]` | Shareable links. No id: a table of every active share (`--all` includes completed ones); one or more ids: just the bare link(s), one per line. |
| `revoke ID` | Expire now, ignoring the deadline. |
| `extend ID --days N` | Push the deadline out from *now*. |
| `sweep [--dry-run]` | Process everything due. This is what cron calls. |
| `config get [KEY]` / `config set KEY VALUE` | Read/write settings. `config set folder archive/cv` changes the default destination permanently. |
| `auth login \| logout \| status` | OAuth flow and connected account. |
| `cron install [--every 15m] \| uninstall \| status` | Manage the tagged crontab line. |

`ID` accepts any unique prefix of the short id, or the filename when that is unambiguous.

Every interactive command sweeps opportunistically first, so the tool self-heals even with cron broken.

## Where things live

```
~/.config/gdrive-auto-expire/client_secret.json   OAuth client (you provide this, 0600)
~/.config/gdrive-auto-expire/token.json           refresh token (0600)
~/.config/gdrive-auto-expire/config.json          settings + cached folder ids
~/.local/state/gdrive-auto-expire/shares.json     share records
~/.local/state/gdrive-auto-expire/lock            flock target
~/.local/state/gdrive-auto-expire/sweep.log       log
```

The log sits in the state directory rather than `~/Library/Logs` on purpose: cron on modern macOS hits TCC
restrictions on `~/Library`, and a silently unwritable log from a background job is a bad failure mode.

## The destination folder, and one real limitation

Uploads go to `gdrive-auto-expire/` at My Drive root by default. The tool creates it on first use and caches
the id; nested paths (`gdrive-auto-expire/recruiters/acme`) are created segment by segment. Delete the folder
in the Drive UI and the next share recreates it rather than failing on a stale id.

**The scope is `drive.file`**, so the app only ever sees files it created itself. That is the right blast
radius, and it is
[Google's only non-sensitive Drive scope](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
so this needs no verification review.

The consequence, worth knowing before it puzzles you: **every folder in a destination path must be one this
tool created.** A folder you made by hand in the Drive web UI is invisible to the tool — it cannot upload into
it or even find it. Fixing that means moving to the restricted `drive` scope and paying the verification cost,
which is not worth it here.

## `--delete` trashes rather than destroys

`share --delete` moves the file to Drive's trash on expiry instead of only revoking the link. Access is gone
either way, but trash is recoverable for 30 days — the safer default for an unattended job acting on a
two-week-old decision. If you want it permanent, `drive.trash()` swaps to `files().delete` in one line.

## Notifications

Expiry fires a macOS notification via `osascript`, best-effort. From cron this runs outside the Aqua session
bootstrap namespace and may not appear; the code falls back silently to log-only. If you find notifications
don't show, wrap the cron command:

```
launchctl asuser $UID osascript ...
```

The log is always written, so it remains the source of truth either way.

## Development

```sh
uv sync
uv run pytest
```

Tests run against an in-memory fake Drive service and a frozen clock — no network, no credentials, and
`GDRIVE_AUTO_EXPIRE_CONFIG_DIR` / `_STATE_DIR` keep them away from your real config and state.
