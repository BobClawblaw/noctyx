# Accounts and access

This page covers who may use the Noctyx console and what each person may do. Access works in one of three modes:
open, accounts or token. In accounts mode each person signs in with a local account that has one of three roles.
Sessions are signed cookies, every sign-in and every action goes to the audit log, and scripts use a per-user API
token that carries its user's role.

Related pages: [install](install.md) · [console tour](console.md) · [site config](site-config.md#server).

## Console modes

The console picks its mode on every request, so adding the first user or changing the setting takes effect
without a restart (changing `console_auth` does need `install-head --apply` and a console restart, because it
reaches the console through its unit):

| Mode | When | Looking | Acting |
|---|---|---|---|
| **open** | `server.console_auth: false` | anyone | anyone. Signing in is optional: it stores your display settings in your account and puts your name in the audit log |
| **accounts** | `console_auth` on (the default) and at least one user | signed-in users only (otherwise redirected to `/login`) | by role, below; scripts use a user's API token |
| **token** | `console_auth` on and no users yet | anyone | only requests that carry the bearer token |

`noctyx user list` prints the current mode. The header shows **open access** in open mode, and your name, role
and **sign out** when you are signed in.

`/metrics` (for Prometheus), the favicon and `/assets/*` never need a sign-in. The login page is `/login`.

## Roles

| Role | May |
|---|---|
| `viewer` | see every page and panel and browse snapshots. Changes nothing, and cannot download files from snapshots (they can hold keys and databases) |
| `operator` | also download files and directories from snapshots, and run jobs: backups, verifies, copy, prune dry run, probes, restores to staging or a fresh directory on the host, hygiene fixes, database dumps, HF harvest, recipe deploy/prepare/stop/start/rollback, recipe discovery, assistant **test**, LLM explanations |
| `admin` | also change configuration and reboot hosts: the actions in `ADMIN_ACTIONS` below, plus setting or clearing the assistant's API key |

Admin-only actions (`ADMIN_ACTIONS` in `console.py`): `setPolicy` (backup policy, retention, mirror settings),
`setSchedule`, `setAssistant`, `setConsoleDefaults` (save as site default), `recipeCatalog`, `reboot`,
`rebootFinish`, `nasRest`, `policyApply`. Every other action needs `operator`.

A denied request returns 401 (*sign in first*) or 403 (*viewer may not do this (needs operator)*).

The console never restores in place. In-place restores are CLI-only: `noctyx restore --in-place --yes`.

## Managing users

Users live in `<etc>/users.json` (default `/etc/noctyx/users.json`, mode 0600, root). Run the commands as root on
the head node:

```bash
sudo noctyx user add --name alice --role admin        # asks for the password twice
sudo noctyx user add --name bob                       # role defaults to viewer
sudo noctyx user role --name bob --role operator
sudo noctyx user passwd --name alice
sudo noctyx user del --name bob                       # also removes bob's saved display settings
sudo noctyx user list
```

- Names: 2 to 32 characters, lowercase letters, digits, `.`, `_` and `-`, starting with a letter.
- Passwords: at least 10 characters, hashed with scrypt. They are never taken from the command line. For
  scripting, pipe one line into `--password-stdin`:

  ```bash
  printf '%s\n' "$PW" | sudo noctyx user add --name ci --role operator --password-stdin
  ```

- Every `user` command is written to the audit log (`user-add`, `user-role` and so on).
- The console has no user management page. Use the CLI.

## Sessions

- Signing in sets the `nx_session` cookie (`HttpOnly; Secure; SameSite=Strict`). It lasts **12 hours**, or
  **30 days** when you tick *keep me signed in for 30 days*.
- The cookie holds `<user>.<expiry>.<hmac>`: nothing secret. It is signed with `<etc>/session.key` (0600, created
  on first use).
- A session ends when it expires, when you sign out, when its user is deleted, or when `session.key` is
  replaced. To sign everyone out at once:

  ```bash
  sudo rm /etc/noctyx/session.key      # a new key is made on the next request
  ```

- **Lockout.** After 5 failed sign-ins from one IP address, that address waits 30 seconds, then 60, 120 and so
  on, up to 1 hour, before it may try again. A successful sign-in clears the count. The counts are kept in
  `<console_state>/login-fails.json`, so restarting the console does not reset them.
- POST requests whose `Origin` header names another site are refused.

## Display settings per user

The gear in the header opens the display settings: theme, mode, °F/°C, panel layout. Signed-in users choose
where their settings are kept:

- **In this browser** (default): stored in the browser's localStorage.
- **In my account**: stored on the head at `<console_state>/user-settings/<user>.json`, and loaded in any browser
  you sign in on.

A browser with no settings of its own uses the site defaults (`console.settings` in the site config). See
[the console tour](console.md#display-settings).

## The audit log

Every action is one JSON line in `<audit>` (default `/var/lib/noctyx/audit.jsonl`), with a UTC `ts` and an
`event`:

| Event | Written by |
|---|---|
| `login` | each sign-in attempt: `user`, `ok`, `ip` |
| `ui-trigger` | each console action: `action`, its parameters, and `user` when signed in |
| `job-detached` | each long job started as its own systemd unit: `unit`, `argv` |
| backup, verify and other job results | the orchestrator and the node agent |
| `schedule-changed`, `policy-changed`, `retention-changed`, `hf-mirror-changed`, `assistant-set`, `console-set`, `install-head`, `user-*` | configuration changes, with before and after where relevant |
| `download` | each file or directory downloaded from a snapshot: `host`, `snapshot`, `path`, `rc` |

To read it:

```bash
sudo tail -n 20 /var/lib/noctyx/audit.jsonl
noctyx status                              # the last 8 lines, formatted
```

The console's **Jobs / Audit** panel shows the most recent lines (`/api/v1/audit?n=N`, at most 200).

## API tokens for scripts

**Accounts mode.** Give the script its own user with the least role it needs, then issue that user a token:

```bash
sudo noctyx user add --name backup-bot --role operator
sudo noctyx user token --name backup-bot          # printed once; only its hash is stored
sudo noctyx user token --name backup-bot --revoke
```

A request with `Authorization: Bearer nx_backup-bot_...` acts as `backup-bot`, with that user's role as it is
now: change the role and the token follows, delete the user and the token stops working. Issuing a new token
replaces the old one. The audit log names the user. `noctyx user list` shows who has a token.

```bash
TOKEN=nx_backup-bot_...                            # from `noctyx user token`
curl --cacert /etc/noctyx/tls/ca.crt -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"action": "backupHost", "params": {"host": "gpu-01"}}' \
  https://head-01:20996/api/v1/action          # -> {"id": "<job id>"}
curl --cacert /etc/noctyx/tls/ca.crt -H "Authorization: Bearer $TOKEN" https://head-01:20996/api/v1/job/<job id>
```

The action names and their parameters are in the `ACTIONS` table in `src/noctyx/console.py`.

**Token mode** (no users yet). The console makes one random site token at `<ui_token>` (default
`/etc/noctyx/ui-token`, 0600) the first time it starts; a request that carries it may do anything. The
console's **Console** panel has a field for pasting it, which it keeps in that browser. Download links ask
the console for a 60-second ticket bound to that one file first, since a link cannot carry the token. To
rotate it, delete the file and restart the console:

```bash
sudo rm /etc/noctyx/ui-token && sudo systemctl restart noctyx-console
```

Once the first user exists the site token is **no longer accepted**: it would act as admin whatever its
holder's role. Move scripts to user tokens before adding users.
