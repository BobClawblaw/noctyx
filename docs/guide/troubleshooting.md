# Troubleshooting

This page lists the messages Noctyx prints, grouped by where you see them, with what each one means and
what to do. The quoted text comes from the code (`orchestrator.py`, `console.py`, the node agent and the
restore scripts). `<…>` marks a value that varies. Two general rules: the audit log
(`/var/lib/noctyx/audit.jsonl`) is the authoritative record of what happened, and the repository decides
what is due, so a job that was skipped or killed is simply retried on the next tick.

When a setup step fails, Noctyx asks the site's assistant (set up first by `noctyx init`) what went wrong and
prints its answer under the error. You can ask it yourself, grounded in this guide and the machine's facts:
`noctyx ask "why does the endpoint answer 500?"`, or pipe a log into `noctyx ask`. `NOCTYX_NO_ASSIST=1` turns
the automatic answers off.

## First checks

```bash
noctyx status                      # repos, hosts, network.degraded, the last 8 audit lines
noctyx tick --dry-run              # what is due and why not
sudo noctyx probe --host gpu-01    # the client → head path in Mbit/s
noctyx paths                       # where this site keeps its files
tail -n 20 /var/lib/noctyx/audit.jsonl
sudo noctyx setup verify           # every check on the head, a canary backup + restore (--no-canary skips it)
```

Where a head service logs depends on the platform:

| | Linux head | macOS head |
|---|---|---|
| services | systemd units `<unit_prefix>-<name>.service` (`noctyx-console.service` …) | launchd daemons `<launchd_prefix>.<name>` (`dev.noctyx.console` …) in `/Library/LaunchDaemons` |
| state | `systemctl status noctyx-console`, `systemctl list-units 'noctyx-*'` | `sudo launchctl print system/dev.noctyx.console` (state, pid, last exit code) |
| logs | `journalctl -u noctyx-console -n 50` | `sudo tail -50 /var/log/noctyx/dev.noctyx.console.log` |
| restart | `sudo systemctl restart noctyx-console` | `sudo launchctl kickstart -k system/dev.noctyx.console` |
| detached jobs | transient units `noctyx-<cmd>-<target>-<ts>-<pid>`, log in `/var/log/noctyx/jobs/` | a process in its own session, log `/var/log/noctyx/jobs/<name>.log` and its pid in `<name>.log.pid` |

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
| `<host> is monitored only (only Linux hosts are backed up)` | `--host` names a Mac. Macs are monitored, never backed up: Time Machine does that | nothing; the console and `setup verify` report its Time Machine state |
| `<host> is monitored only (backup: false, or no repo, in the site config)` | `--host` names a host without a repository | give the host a `repo`, or leave it out |
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
| `noctyx must run as root (ssh key, locks, detached jobs) — use sudo` | detached jobs run as root: a transient unit (`systemd-run`) on Linux, a process in its own session on macOS | use `sudo` |
| `could not start <unit>: …` | Linux: `systemd-run` failed | read the error; a unit with the same name may still exist |
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

## Setup

| Symptom | Cause | What to do |
|---|---|---|
| `setup` stops at a step with `✗ <step>: …` | that step failed; the steps before it are kept | fix what it says (the assistant's answer follows), then `sudo noctyx setup all` again: finished steps are skipped |
| `CERTIFICATE_VERIFY_FAILED … Missing Authority Key Identifier` from the console, `setup verify` or a script | Python 3.13 and newer verify strictly, and a certificate made by an older Noctyx has no key identifiers. curl and restic still accept it | `sudo noctyx setup tls` renews the server certificate with them and keeps the CA, so no node needs re-installing. `setup verify` checks it in the row `strict clients` |
| the directory service exits with `PermissionError … site.json` | it runs as the repository owner, and the site config is root-only (`chmod 600`) | `sudo noctyx setup owner` makes it `0640 root:<repo_owner>`. The config holds no secrets: keys and logins live in their own root-only files |
| `install-agent` ends with `orchestrator.py: Permission denied` and `policy-apply FAILED` | an older `install-agent.sh` ran the orchestrator file directly; a pip install leaves it non-executable | fixed: it now runs with Noctyx's own Python. Upgrade, then run `install-agent` again |
| `setup tools`: `sha256 <x> is not the pinned <y>; refusing it` | the download does not match the checksum pinned in `components.py` | do not work around it: retry (a broken download), or check what is serving GitHub's release files on your network |
| `setup metrics`: `<prometheus.yml> exists and is not Noctyx's` | the head already has a Prometheus config Noctyx did not write | move it aside, or `sudo noctyx setup metrics --force` to replace it. A Prometheus that is running is kept instead, and the jobs to add are printed |
| `setup verify`: `! assistant  no answer from …` | the LLM endpoint is down or restarting | a warning only: setup works without it. `sudo noctyx init --assistant-only` points it elsewhere |
| alert `NasRaidUnknown` on a site whose NAS Noctyx cannot ssh into | older versions exported the RAID source anyway | gone: the RAID state is only read when `server.nas_ssh` is set |

## macOS head

| Symptom | Cause | What to do |
|---|---|---|
| a daemon never starts; `launchctl print system/dev.noctyx.<name>` shows `last exit code = 78: EX_CONFIG` | launchd opens the job's log file as the job's user before starting it; a log that user cannot write stops it there | setup now creates each daemon's log owned by its user. Re-run the step that installed it (`sudo noctyx setup endpoint`, `units` or `metrics`) |
| the endpoint answers HTTP 500, its log says `operation not permitted`, and restic retries for minutes; or the mirror cannot read its store | macOS privacy (TCC) refuses background daemons a network share or an external disk until they have Full Disk Access. The same commands work from Terminal or ssh, which have their own permission | `sudo noctyx setup privacy`: it names the binaries (`/usr/local/bin/rest-server` and Noctyx's real Python executable), opens System Settings › Privacy & Security › Full Disk Access on the Mac's screen, and checks the grant from launchd itself. Only a person at the Mac can grant it |
| `setup verify`: `✗ full disk access … ERR …` after a Homebrew update | the grant is for the Python binary's path, and a Homebrew Python update installs a new one | `sudo noctyx setup privacy` again and grant the new path it prints |
| `restic is not installed: run noctyx setup tools`, although `setup tools` installed it | macOS's `sudo` drops `/usr/local/bin` from `PATH` | Noctyx now adds it itself. On an older install, call `/usr/local/bin/noctyx` |
| `launchctl bootstrap …: Input/output error` when a step runs again | the old job had not finished unloading | setup now waits for it and retries; run the step again. A re-run whose daemon has not changed restarts nothing |
| `security add-trusted-cert` failed, or the System keychain holds "Noctyx site CA" entries marked untrusted | over ssh or from a script the keychain takes no trust setting (no one is at the screen to approve it), but it still adds the certificate | Noctyx no longer touches the keychain: it hands every tool the CA file. For browsers and curl on the Mac, run the command `setup tls` prints, in Terminal. Remove the clutter: `security find-certificate -a -c "Noctyx site CA" -Z /Library/Keychains/System.keychain`, then `sudo security delete-certificate -Z <SHA-1> /Library/Keychains/System.keychain` for each |
| `noctyx backup` (or another detached job) never returns after the job's log says `NOCTYX_EXIT 0` | older versions kept the finished job as a zombie child, which still looked alive | fixed. Stop the follower; the job itself is done. Logs and pid files are in `/var/log/noctyx/jobs/` |
| `install-agent` or `backup` on a Mac: `monitored only, never backed up` | Macs are monitored only | nothing; see [Backups](backups.md#macs-are-monitored-not-backed-up) |

## Console

The console works in one of three modes:

- **open:** the unit runs with `NOCTYX_CONSOLE_AUTH=0`, which is what `server.console_auth: false`
  renders. Anyone on the network can look and act.
- **token:** no accounts exist. Viewing is open, and actions need `Authorization: Bearer <token>`, where
  the token is in `/etc/noctyx/ui-token`.
- **accounts:** you sign in, and your role decides what you may do. Scripts use a user's API token
  (`noctyx user token`); the site token in `/etc/noctyx/ui-token` is no longer accepted.

| Response | Meaning | What to do |
|---|---|---|
| 401 `sign in first` | accounts mode, no session (or a script still sending the old site token) | sign in; give scripts a user token: `sudo noctyx user token --name <bot>` |
| 401 `bad token` | token mode, a missing or wrong bearer token | use the token from `/etc/noctyx/ui-token` |
| 401 `wrong name or password` | | after 5 failures from one address, sign-ins from there are locked for 30 s, doubling up to 1 h (429 `too many failed sign-ins from here: wait a little`) |
| 403 `<role> may not do this (needs operator\|admin)` | your role is too low. Viewers only look; operators run jobs. The admin-only actions are the policy, schedule, assistant, console defaults, recipe catalog, reboot, NAS endpoint and policy apply | ask an admin: `sudo noctyx user role --name <you> --role operator` |
| 403 `cross-origin request refused` | the POST's `Origin` does not match `Host`, for example a reverse proxy rewriting `Host` | open the console directly, or have the proxy keep `Host` |
| 403 `demo: read-only` | you are on `noctyx demo` | nothing |
| 400 `unknown action or invalid host/profile` | the action is not on the whitelist, or a parameter failed validation, such as a hygiene id that is not in the host's last report | rerun the hygiene report, then retry |
| a panel greyed out, or snapshots shown as `stale` | the last refresh failed. A repository that fails to list keeps its last good numbers, flagged stale (metric `cm_snapshot_listing_stale`) | check the NAS and the repository key on the head node |
| host facts showing `no output` | the console's ssh to the host (5 s connect timeout) got nothing back. Facts are cached for 120 s. A host with `"local": true` (the head) is read without ssh | check that ssh as `server.ssh.user` with `server.ssh.key` works |
| live numbers missing, `/api/v1/metrics` errors `Connection refused` | no Prometheus on `127.0.0.1:9090` (`NOCTYX_PROM` overrides the address) | `sudo noctyx setup metrics` |

See also: [Backups](backups.md) · [Restore](restore.md) · [Hygiene](hygiene.md) ·
[Accounts](accounts.md)
