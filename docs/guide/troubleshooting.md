# Troubleshooting

This page lists the messages Noctyx prints, grouped by where you see them, with what each one means and
what to do. The quoted text comes from the code (`orchestrator.py`, `console.py`, the node agent and the
restore scripts). `<…>` marks a value that varies. Two general rules: the audit log
(`/var/lib/noctyx/audit.jsonl`) is the authoritative record of what happened, and the repository decides
what is due, so a job that was skipped or killed is simply retried on the next tick.

## First checks

```bash
noctyx status                      # repos, hosts, network.degraded, the last 8 audit lines
noctyx tick --dry-run              # what is due and why not
sudo noctyx probe --host gpu-01    # the client → head path in Mbit/s
noctyx paths                       # where this site keeps its files
tail -n 20 /var/lib/noctyx/audit.jsonl
systemctl list-units 'noctyx-*'    # running jobs and timers
```

## Backups

| Message | Meaning | What to do |
|---|---|---|
| `SKIP <host>: spindle '<name>' is busy by `<what>` since <ts> (pid <n>) — jobs are serialised per spindle by design` | another job (a backup, retention, copy) is writing to the same target | nothing; this is not a failure. The tick retries. The holder is in `/var/lock/noctyx/<name>.lock` |
| `SKIP <host>: a backup is already running on that host (started elsewhere)` | the agent's one-job lock is held: a manual run, the UI, or the opt-in node-local timer | wait for it. Check `/var/lib/noctyx/progress` on the host |
| `SKIP <host>: measured <n> Mbit/s < floor <f> Mbit/s (would take too long)` | the probe ran below `network.min_throughput_mbit` (default 40) | fix the link, lower the floor in the site config, or pass `--force-low-net` once |
| `SKIP <host>: could not measure the path (probe failed)` | the host's `curl` to `https://<server.address>:<directory_port>/api/v1/probe` returned nothing | check that the directory service is running and reachable from the host, and that the host has the CA at `/etc/noctyx/tls/ca.crt` (`noctyx install-agent` puts it there). `--force-low-net` skips the check |
| `UNLOCK <host>: repo lock error detected — clearing stale locks and retrying once` | an earlier killed run left a restic lock; Noctyx cleared it | nothing, unless the retry fails too |
| `SKIP <host>: repo lock is HELD BY A LIVE WRITER on <other> (shared repo '<repo>')` | another host that shares the repository is backing up now | wait for it to finish. Noctyx will not clear a live lock |
| `FAIL … rc=255` + `NOTE: rc=255 is ssh losing the connection` | ssh dropped; the agent keeps running on the host | check the host's audit; the next tick sees the snapshot |
| `<host> is monitored only (backup: false in the site config)` | `--host` names a host without a repository | give the host a `repo`, or leave it out |
| `unknown host: <name>` | not an enabled host in the site config | check `noctyx hosts` |
| `<host>: no readable snapshot list (agent not installed?)` | `noctyx-agent snapshots` gave no JSON | run `sudo noctyx install-agent --host <host>` |

### What the agent prints on the host

These lines appear in the job log as `[cm-agent] FATAL: …`.

| Message | What to do |
|---|---|
| `missing /etc/noctyx/client.conf — install the agent first` | `sudo noctyx install-agent --host <host>` |
| `/etc/noctyx/client.conf missing <VAR>` / `cannot read <PASSWORD_FILE>` / `missing /etc/noctyx/repo-url` | reinstall the agent; these files carry the repository key and endpoint credential |
| `restic is not installed` | install restic on the node (`install-agent` does this) |
| `another noctyx job is genuinely running (lock holders: <pids>)` | a backup, restore, verify or hygiene run holds the host's lock; wait |
| `no paths file for profile '<p>'` | the profile was never installed on this host: `sudo noctyx install-agent --host <host> --profile <p>` |
| `fs_meta returned '<x>' which is not a directory` | the metadata capture failed; the backup is refused rather than taken without boot metadata. Run `noctyx-agent fs-meta` on the host to see why |
| `warning: REST_PASS_FILE unreadable; REST basic auth may fail` | the endpoint credential file is missing; reinstall the agent |

## Schedule and tick

| Message | Meaning | What to do |
|---|---|---|
| `WARN cannot read repo <repo>: <reason>` and `repo <repo> unreadable this tick — not deciding blind` | the head node could not list the repository (NAS down, mount gone) | its hosts are not scheduled this tick. Check the NAS mount and `noctyx nas-rest status` |
| `failed <t> ago, retry in <t>` | the last attempt failed | wait `schedule.retry_after` (default 1h), or run `noctyx backup --host …` |
| `outside window <HH:MM-HH:MM>` | not due yet | the window is compared with the head node's local clock |
| `already done this window` | a daily-or-longer schedule already has a snapshot from this window | nothing |
| `unknown profile '<p>'` | the schedule names a profile missing from `profiles` | fix the site config |
| `that change leaves a host with a schedule but no interval; give --every` | a `schedule-set` edit would leave a host's profile with no `every` | add `--every` |
| `--every '<v>': use 15m..90d` / `--window '<v>': use HH:MM-HH:MM … or 'any'` | bad value | |
| `--reset needs --host` / `nothing to change: give --every/--window, --disable or --reset` | | |
| health `late` or `stale` in the console | the newest snapshot is older than one or two intervals plus the grace | look for `backup-refused*` or `backup-skipped*` lines in the audit |

## Repository endpoint and NAS

| Message | Meaning | What to do |
|---|---|---|
| `NAS rest-server was down: restart OK/FAILED` | every tick (and the rehearsal) checks that the endpoint on the NAS is up, and restarts it if not | if it says FAILED: `sudo noctyx nas-rest status`, then `sudo noctyx nas-rest restart`, or `deploy` to reinstall it |
| `endpoint https://…: DOWN` | `nas-rest` could not reach the endpoint over TLS with the CA. A 401, 403 or 404 answer counts as up | check the NAS, `server.rest_host` and `rest_port` |
| audit `repo-ownership-sweep-incomplete` | root-owned files remain in a repository after the sweep; the endpoint then answers HTTP 500 and client backups hang | `sudo chown -R <repo_owner>: <repo_root>/…` for the files named |
| `NAS mount not present — check /etc/fstab (_netdev); refusing to copy into an unmounted mountpoint` | `copy` found no NAS mount | mount it |
| `no retired hot tier in the site config … nothing to copy` | `copy` has nothing to do | nothing |

## Retention

| Message | Meaning |
|---|---|
| `SKIP <repo>: not initialised` | no repository directory under `repo_root` |
| `FAIL <repo>: forget --dry-run rc=<n>` | restic could not plan; the key in `/etc/noctyx/repo-keys/<repo>.key` or the mount is wrong |
| `REFUSED <repo>: check failed before forget — deleting nothing` | `restic check` failed, so nothing is removed. Investigate the repository before running `--apply` again |
| `FAIL <repo>: forget rc=… prune rc=… check-after rc=…` | a step after the deletion failed; see the lines printed above it |
| `NOTE: keep_2nd_last_monthly is NOT enforced` | informational; restic has no such flag |

## Restore

| Message | What to do |
|---|---|
| `bad --snapshot (hex id or 'latest')` / `bad --path` | the id must be 8 to 64 hex characters; the path must be absolute |
| `--in-place overwrites files on the live host; add --yes if you mean it` | add `--yes`, or restore to staging instead |
| recovery engine: `no Noctyx server found … Discovery is UDP/20997 broadcast.` | bring the network up and pass `--server <head address>` |
| `LVM restore (template=…) is Phase 5 and NOT implemented here` | that host uses LVM: use [`noctyx-restore-head.sh`](restore.md#rebuild-the-head-node) |
| `boot mode '<b>' unsupported by this image` | the engine only handles `boot: uefi-*` |
| `--disk required (inventory had no root_disk)` | pass `--disk`, or set `root_disk` for the host and run `noctyx directory` |
| `could not list snapshots: …` | the key or credential does not match, or the endpoint is down |
| `confirmation mismatch, nothing was written` | type the device path exactly as shown |
| head restore: `no LVM metadata backup contains PV <uuid>` | the snapshot's fsmeta has no `lvm-backup/` for that PV; choose another snapshot |
| stick build: `kernel does not see <dev>3 — replug the stick, then: …` | follow the commands it prints (`--boot`, `mkfs.vfat`, `--payload`); for a single-arch stick, `--finish /dev/sdX` |
| stick build: `stick restic differs from the fleet's` | the head node's `restic` version is not `restic_version`; align them |
| stick build: `the stick's <repo> key + credential do not open the live repository` | the keys or creds on the head node are out of date, or the endpoint is down |

## Jobs

| Message | Meaning | What to do |
|---|---|---|
| `noctyx must run as root (ssh key, locks, systemd-run) — use sudo` | detached jobs are started with `systemd-run` | use `sudo` |
| `could not start <unit>: …` | `systemd-run` failed | read the error; a unit with the same name may still exist |
| `follow: <unit> ended without an exit record (killed?) — see <log>` (exit 125) | the job was killed before it could write `NOCTYX_EXIT` | read the log and the audit for the real outcome |
| console job marked lost (`rc=-2`, "UI restarted while this job was in flight") | the console restarted while a job that was not detached was running | check the audit or the Machine Report. Detached jobs are followed again after a restart |

## Reboot

`REFUSED: <blocker>` (exit 2) means one of these:

- `a backup/restore is writing on <host> right now (agent.lock held)`
- `the package manager is running on <host> (dpkg lock held)`
- `Noctyx job running for <host>: <units>`
- `a Noctyx job is running on the head node and would be killed`
- `a reboot of <host> is already in progress`
- `cannot reach <host>: …`
- `macOS host: monitored only`

Wait for the blocker to clear, then run `sudo noctyx reboot --host <host> --plan` again. A reboot record left
behind by a waiter that died becomes stale after an hour.

`<host> is not back with a new boot after 25 min — check its console/BMC` means the host did not come back.
Check its console, or its BMC if it has one. `… NOT serving after 10 min` means a model did not come back
after the reboot. Restart its service; a tensor-parallel pair may need both ranks restarted.

## Console

The console works in one of three modes:

- **open:** the unit runs with `NOCTYX_CONSOLE_AUTH=0`, which is what `server.console_auth: false`
  renders. Anyone on the network can look and act.
- **token:** no accounts exist. Viewing is open, and actions need `Authorization: Bearer <token>`, where
  the token is in `/etc/noctyx/ui-token`.
- **accounts:** you sign in, and your role decides what you may do. A bearer token still works for
  scripts.

| Response | Meaning | What to do |
|---|---|---|
| 401 `sign in first` | accounts mode, no session | sign in |
| 401 `bad token` | token mode, a missing or wrong bearer token | use the token from `/etc/noctyx/ui-token` |
| 401 `wrong name or password` | | after 5 failures from one address, sign-ins from there are locked for 30 s, doubling up to 1 h (429 `too many failed sign-ins from here: wait a little`) |
| 403 `<role> may not do this (needs operator\|admin)` | your role is too low. Viewers only look; operators run jobs. The admin-only actions are the policy, schedule, assistant, console defaults, recipe catalog, reboot, NAS endpoint and policy apply | ask an admin: `sudo noctyx user role --name <you> --role operator` |
| 403 `cross-origin request refused` | the POST's `Origin` does not match `Host`, for example a reverse proxy rewriting `Host` | open the console directly, or have the proxy keep `Host` |
| 403 `demo: read-only` | you are on `noctyx demo` | nothing |
| 400 `unknown action or invalid host/profile` | the action is not on the whitelist, or a parameter failed validation, such as a hygiene id that is not in the host's last report | rerun the hygiene report, then retry |
| a panel greyed out, or snapshots shown as `stale` | the last refresh failed. A repository that fails to list keeps its last good numbers, flagged stale (metric `cm_snapshot_listing_stale`) | check the NAS and the repository key on the head node |
| host facts showing `no output` | the console's ssh to the host (5 s connect timeout) got nothing back. Facts are cached for 120 s | check that ssh as `server.ssh.user` with `server.ssh.key` works |

See also: [Backups](backups.md) · [Restore](restore.md) · [Hygiene](hygiene.md) ·
[Accounts](accounts.md)
