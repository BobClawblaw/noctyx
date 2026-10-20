# Backups

Noctyx backs up every node with [restic](https://restic.net) into one repository per host class on the
NAS. The head node decides *when* and *whether it is safe*. It takes one writer per spindle, refuses a job
the network cannot carry, and runs whatever the schedule says is due. The node agent
(`/usr/local/bin/noctyx-agent`) does the work: it captures the machine's boot and disk metadata, runs the
database dumps, runs `restic backup`, and reports one result line. This page covers schedules, the backup
policy, retention, verification and the job plumbing.

Paths below are the defaults from `src/noctyx/paths.py`. A site's `"paths"` block can change them, and
`noctyx paths` prints the values in effect.

## Macs are monitored, not backed up

Only Linux hosts are backed up. The agent is a Linux one, and a Mac has Time Machine. A host with
`"os": "macos"` is monitored only: `noctyx backup` and `install-agent` skip it with `monitored only`, and the
console's machine report shows its Time Machine state (destination, last backup, whether one is running)
instead of backup buttons. On a Mac head, `noctyx setup verify` checks that Time Machine has a destination and
a backup less than two days old, as a warning.

The head itself can be a Mac. It still runs every Linux node's backups: the repository endpoint
(rest-server) and the repositories live on its storage, a local disk or a NAS share it mounts over NFS or
SMB, and the nodes send their snapshots to it over HTTPS. On a Mac head, the endpoint and the scheduler need
Full Disk Access to reach a share or an external disk (`sudo noctyx setup privacy`).

## How a backup runs

`noctyx backup [--host NAME] [--profile system] [--dry-run] [--force-low-net]`

For each target host (all enabled Linux hosts that have a `repo` and are not `"backup": false`), the head
node does the following:

1. **Ownership sweep.** Files owned by root inside the repositories under `server.repo_root` are chowned
   back to the repository owner (`noctyx` by default). A root-owned lock or index file makes the NAS
   endpoint answer HTTP 500, and client backups then hang.
2. **Throughput floor.** It runs `curl` *on the host* against
   `https://<server.address>:<server.directory_port>/api/v1/probe?bytes=20000000`, using the host's
   copy of the CA (`/etc/noctyx/tls/ca.crt`).
   - If the probe fails, the host is skipped (`backup-refused-unmeasurable`).
   - If the measured rate is below `network.min_throughput_mbit` (default **40** Mbit/s), the host is
     skipped (`backup-refused`).
   - `--force-low-net` overrides both checks.
3. **Spindle lock.** It takes `/var/lock/noctyx/<spindle>.lock`, where `<spindle>` is the host's
   `spindle` key or, by default, the NAS name (`server.nas_name`, else the spindle whose `mount` holds
   `repo_root`, else `nas`).
   - A backup never waits for the lock. A busy spindle means *skip this host*, not a failure, and the
     other hosts carry on.
   - Retention and `copy` wait for the lock instead.
4. **Agent run.** It runs `ssh <host> sudo -n noctyx-agent backup --profile P` and streams restic's
   output live. A host marked `"local": true` (a Linux head backing itself up) runs the same command
   locally, without ssh.
   - The agent takes its own one-job-per-host lock (`/var/lib/noctyx/agent.lock`).
   - It writes boot, disk, LVM, package and GPU metadata to `/var/lib/noctyx/fsmeta/<UTC stamp>/`.
   - It runs the `pre.d` hooks (database dumps), then
     `restic backup --tag noctyx --tag profile-P --tag host-H` with the newest snapshot of that
     host and profile named explicitly as the parent.
   - It runs the `post.d` hooks last.
5. **Stale repo lock.** If restic fails with a lock-shaped error, the head node first checks whether
   any *other* host that shares the repository holds its agent lock.
   - If one does, a live writer is there: the head node refuses (`backup-refused-live-lock`) and leaves
     the lock alone.
   - Otherwise it runs `noctyx-agent unlock` and retries once.

`--dry-run` passes `--dry-run` to restic. `rc=255` means ssh lost the connection, not that restic failed.
The agent ignores SIGHUP and SIGPIPE and carries on. Its result lands in the host's own audit, and the next
`tick` sees the snapshot.

While a backup runs, the agent writes a heartbeat to `/var/lib/noctyx/progress` every 30 s (seconds
elapsed, MB uploaded, MB scanned). restic's own output goes to `/var/lib/noctyx/last-backup.log` on the
host.

> restic stores no ACLs and no extended attributes. The agent saves them as text in fsmeta (`acls.txt`
> for `/etc /boot /srv /root`, and `xattrs.txt`), and you re-apply them by hand after a restore.

### Profiles

A profile is a named set of paths in the site config:

```json
"profiles": {
  "system": { "description": "whole root filesystem minus the noise list", "paths": ["/"],
              "exclusions": ["common", "per-host"] }
}
```

`noctyx init` writes two profiles: `system` (`/`, nightly in the 01:00–07:00 window) and `config`
(`/etc` and `/usr/local/etc`: configuration only, a few MB). Take the first backup of a new node with
`config`, to prove the path end to end in seconds:

```bash
sudo noctyx install-agent --host gpu-01 --profile config
sudo noctyx backup --host gpu-01 --profile config
```

Before any larger profile, check the size of every path on the node (`sudo du -sh /root /home /srv …`):
a home directory that holds models can be hundreds of GB, and the first backup sends all of it.

`noctyx install-agent --host gpu-01 --profile system` writes the profile's paths to
`/etc/noctyx/profile-system.d/paths` on the node. Every profile uses the packaged baseline list
(`src/noctyx/agent/exclusions-common.conf`) plus the site's `common_exclusions_extra`. If the head node
has `/etc/noctyx/exclusions-<profile>.conf`, that file replaces the common list for the profile, which
suits a profile whose paths are separate mounts. The agent never uses `--one-file-system`, so `/boot` and
`/boot/efi` are always included.

## Schedules and `tick`

The `tick` timer (`noctyx-tick.timer`, every 15 minutes) runs `noctyx tick`. A host and profile pair is
**due** when its newest snapshot *in the repository* is older than `every` and the clock is inside
`window`. The tick reads the repositories, not the audit log, so a run that was skipped, failed or killed
is simply still due on the next tick.

```json
"schedule": {
  "tick_minutes": 15,
  "retry_after": "1h",
  "defaults": { "system": { "every": "24h", "window": "01:00-07:00" } },
  "hosts": {
    "gpu-02": { "system": { "every": "6h" } },
    "spark-1": { "system": null }
  }
}
```

| Key | Meaning |
|---|---|
| `defaults.<profile>` | `every` (a number plus `s` `m` `h` `d` or `w`) and an optional `window` `HH:MM-HH:MM`, which may wrap midnight |
| `hosts.<host>.<profile>` | merged over the default; `null` disables that profile on that host |
| `retry_after` | after a failed attempt, wait this long before trying again (default `1h`) |
| `tick_minutes` | how often the tick timer fires (a divisor of 60; re-run `install-head --apply`), and the slack, so a backup due a few minutes before the next tick still runs now (default 15) |

The rules:

- With a window and `every` of 1 day or more, the schedule means **once per window**. A daytime manual
  run does not push the next nightly run back a night.
- The window is in the head node's local time.
- If a repository cannot be read on a tick, its hosts are not scheduled on that tick ("unreadable this
  tick — not deciding blind").
- Due jobs run one at a time, most overdue first. Each job is re-checked just before it starts.

```bash
noctyx tick --dry-run      # the plan: DUE or not, last, every, next, health, why
sudo noctyx tick           # run what is due (the timer does this)
```

Health is `ok` (age ≤ every + grace), `late` (≤ 2 × every + grace), `stale` (older still) or `never`.
The grace is the time outside the window plus the tick slack.

### `schedule-set`

This command changes the `schedule` block in place. Only that block is rewritten, and the rest of the file
stays byte for byte. Each change is audited as `schedule-changed`.

```bash
sudo noctyx schedule-set --profile system --every 1d --window 01:00-07:00   # fleet default
sudo noctyx schedule-set --profile system --host gpu-02 --every 6h          # one host's override
sudo noctyx schedule-set --profile system --host gpu-02 --window any        # no window for that host
sudo noctyx schedule-set --profile system --host spark-1 --disable          # stop scheduling it there
sudo noctyx schedule-set --profile system --host gpu-02 --reset             # back to the default
```

`--every` must match `^\d{1,4}[mhdw]$` and lie between 15m and 90d. `--disable` and `--reset` cannot be
used together, and `--reset` needs `--host`. A new fleet default needs `--every`. The profile must exist in
`profiles`.

## Backup policy

The `backup_policy` block in the site config says what is covered. If the block is absent, the defaults
apply. Every key has a fleet value, and `hosts.<name>` can override it for one host. A boolean override
replaces the fleet value, and a list override **adds to** the fleet list.

```json
"backup_policy": {
  "docker_volumes": true,
  "db_dumps": true,
  "exclude": ["/var/cache/**", "!/var/cache/keep-me"],
  "skip_dumps": ["docker:scratch-db"],
  "sqlite": ["/srv/app/state.db"],
  "hosts": { "gpu-02": { "docker_volumes": false, "exclude": ["/data/scratch/**"] } }
}
```

| Key | Default | Effect |
|---|---|---|
| `docker_volumes` | `true` | back up `/var/lib/docker/volumes`; image layers and container root filesystems stay excluded. `false` excludes `/var/lib/docker/**` |
| `db_dumps` | `true` | run the dump hook before each `system` backup |
| `exclude` | `[]` | extra restic patterns, applied after the baseline list, so `!pattern` re-includes something the baseline excludes |
| `skip_dumps` | `[]` | discovered database ids not to dump (`docker:NAME`, `host:mysql`, `host:mariadb`, `host:postgresql`, `sqlite:/path`) |
| `sqlite` | `[]` | extra SQLite files to copy with the online backup API (absolute paths) |

The validation rules are:

- A list holds at most 200 entries, each one line of at most 512 printable characters.
- An `exclude` entry must not exclude everything (`/`, `*`, `**`, `/*` and similar).
- A `sqlite` path must be absolute.
- Unknown keys and unknown hosts are refused.
- The policy covers Linux hosts that have a repository, not macOS hosts.

### `policy-set`, `policy-apply`, `policy-show`

```bash
noctyx policy-show --host gpu-01                  # the files policy-apply would write, nothing changed
sudo noctyx policy-apply [--host gpu-01] [--no-scan]
sudo noctyx policy-set --json '{"policy": {"docker_volumes": true, "exclude": ["/var/cache/**"]},
                               "retention": {"daily": 14, "weekly": 8, "monthly": 6}}'
```

`policy-set` takes the policy object itself, or an object with `policy` plus optional `retention`
(all of `daily`, `weekly` and `monthly`, whole numbers from 0 to 999, not all zero) and optional
`hf_mirror` keys. It validates everything first ("refused:" lists every problem), writes only the blocks
that changed, audits them, and then runs `policy-apply` on every host.

`policy-apply` pushes these files to each host through stdin (root-owned):

| File on the node | Contents |
|---|---|
| `/usr/local/bin/noctyx-agent`, `/usr/local/bin/noctyx-dumps` | the current agent and dump tool |
| `/etc/noctyx/hooks/pre.d/10-db-dumps.sh` | the hook: `exec noctyx-dumps hook "$@"` |
| `/etc/noctyx/db-dumps.json` (0600) | `enabled`, `skip`, `sqlite`, `profiles: ["system"]` |
| `/etc/noctyx/exclusions-common.conf` | the baseline list plus `common_exclusions_extra` |
| `/etc/noctyx/profile-*.d/exclusions` | the rendered policy patterns, for each profile already installed |

Unless you pass `--no-scan`, it then runs `noctyx-dumps list` on the host and records what it found in
`/etc/noctyx/state/dumps-discovery.json`. The console reads that file.

### Database dumps

`noctyx-dumps` finds databases without per-database setup:

| Kind | Found by | Dumped with |
|---|---|---|
| Postgres in Docker | a `postgres`/`postmaster` process in the container | `pg_dumpall -U ${POSTGRES_USER:-postgres}` |
| pg0-embedded Postgres in Docker | `~/.pg0/instances/*/instance.json` in the container | that instance's own `pg_dumpall`, with credentials from the JSON |
| MariaDB/MySQL in Docker | `mariadbd`/`mysqld` | `mariadb-dump`/`mysqldump --all-databases --single-transaction --routines --events` as root, with the container's `*_ROOT_PASSWORD` passed as `MYSQL_PWD` |
| Redis/Valkey in Docker | `redis-server`/`valkey-server`, only with a mount | `SAVE`, so the `.rdb` in the volume is current (no separate dump file) |
| MySQL/MariaDB on the host | an active `mysql` or `mariadb` service | `mysqldump --all-databases …` (socket auth as root) |
| PostgreSQL on the host | an active `postgresql` service | `runuser -u postgres -- pg_dumpall` |
| SQLite | `/var/lib/gitea/data/gitea.db`, `/var/lib/grafana/grafana.db`, plus `sqlite` paths | the online backup API, opened read-only |

How the dumps behave:

- Dumps go to `/var/lib/noctyx/dumps` (mode 0700), which is inside the `system` profile.
- Each dump is written to `.partial` and then renamed, so a failed dump never replaces the last good
  one.
- Each dump may take up to 30 minutes.
- A dump file whose database has gone away is deleted after 30 days.
- The last run's results are in `dumps/status.json`.
- As a hook, a failing dump **never blocks** the backup.

```bash
sudo noctyx dumps [--host gpu-01]    # run every enabled dump now, no backup; rc 1 if any failed
```

## Retention and `prune`

```json
"retention": { "daily": 14, "weekly": 8, "monthly": 6 }
```

`noctyx prune` runs on the head node directly against the mounted repositories, as the repository owner,
while holding the NAS lock (it waits for the lock). The default is a **dry run**.

```bash
sudo noctyx prune                         # dry run: keep N, remove M per repository
sudo noctyx prune --host gpu-01           # only that host's snapshots in its repository
sudo noctyx prune --apply                 # check → forget → prune → check
sudo noctyx prune --forget 1a2b3c4d --forget 5e6f7a8b --apply   # these ids instead of the policy
```

- Snapshots are grouped by `host,tags`, not by paths. Every snapshot contains its own timestamped fsmeta
  directory, so grouping by paths would make each snapshot a group of one, and nothing would ever be
  removed.
- `--apply` only deletes after a passing `restic check`. If the check before fails, nothing is deleted
  (`REFUSED … check failed before forget`). If the check after fails, the result is reported as `FAIL`
  and audited.
- If there is nothing to remove, the prune step is skipped.
- `keep_2nd_last_monthly: true` is reported as **not enforced**, because restic has no equivalent flag.
- The `noctyx-retention.timer` runs `prune --apply` on the 1st of each month at 08:00.
- The console's Prune action is always a dry run. `--apply` is CLI-only.

## `verify`, `snapshots`, `capacity`

```bash
sudo noctyx verify [--host gpu-01] [--subset 5%]   # restic check + check --read-data-subset, on the host
noctyx snapshots [--host gpu-01] [--limit 20]      # newest snapshots per host (default 5)
noctyx capacity                                    # free space on repo_root (and hot_root), vs hosts' used_gb
noctyx probe [--host gpu-01]                       # just the throughput measurement
```

`verify` takes its subset from `--subset`, else from `verification.read_data_subset` (default `2%`). `capacity` warns when the measured source size exceeds the free space on the
NAS.

## `nightly`

`noctyx-nightly.timer` (07:30 daily; on a macOS head the launchd daemon `dev.noctyx.nightly`) runs
`noctyx nightly`. Each step runs in its own process, so one failure never skips the rest:

- every day: `hf-harvest`, then `recipe-discover`, then `versions` (every critical component on every
  machine against its pin and the newest release: see [Upgrade](upgrade.md))
- on Sundays, in addition: `prune` (a dry run) and `verify`

The exit code is 1 if any step failed (`WARN <step> failed (rc=N)`).

## Detached jobs and `follow`

`backup`, `verify`, `copy`, `prune`, `restore`, `hygiene`, `reboot` (with `--yes` or `--finish`) and
`hf-harvest` do not run inside your shell. On a Linux head each one starts as its own transient systemd
unit; on a macOS head, as a process in its own session with its pid in `<log>.pid`. Either way, closing the
terminal or restarting the console never kills it.

```text
DETACHED unit=noctyx-backup-gpu-01-20261019T020000Z-12345 log=/var/log/noctyx/jobs/noctyx-backup-gpu-01-20261019T020000Z-12345.log
```

- The command then follows the log and exits with the job's own exit code (from its `NOCTYX_EXIT` line).
- `--no-follow` returns at once.
- `--foreground` runs the job in the current process instead.
- Detaching needs root ("noctyx must run as root … use sudo").

```bash
sudo noctyx backup --host gpu-01 --no-follow
noctyx follow --unit noctyx-backup-gpu-01-20261019T020000Z-12345 \
              --log /var/log/noctyx/jobs/noctyx-backup-gpu-01-20261019T020000Z-12345.log
```

`follow` exits with 125 if the unit ended without an exit record, which means it was killed. On macOS,
`follow` reads the job's pid file to know whether it is still running.

## Logs and the audit

| What | Where |
|---|---|
| every action (head node), one JSON line each | `/var/lib/noctyx/audit.jsonl` |
| the agent's own results (each node) | `/var/lib/noctyx/audit.jsonl` on that node |
| detached jobs' output | `/var/log/noctyx/jobs/<unit>.log` (macOS: and `<unit>.log.pid`) |
| last restic run, progress, last restore/unlock (each node) | `/var/lib/noctyx/last-backup.log`, `progress`, `last-restore.log`, `last-unlock.log` |
| scheduler state (last attempt per host and profile) | `/etc/noctyx/state/schedule-state.json` |
| spindle lock holders | `/var/lock/noctyx/<spindle>.lock` (JSON: `what`, `pid`, `since`) |

`noctyx status` prints the repositories, the host count, any `network.degraded` note and the last eight
audit lines. Audit events you will see include `backup`, `backup-refused`, `backup-refused-unmeasurable`,
`backup-skipped-busy`, `backup-skipped-running`, `backup-unlock-retry`, `tick-start`, `retention-plan`,
`retention`, `verify`, `policy-applied`, `dumps-run` and `job-detached`.

See also: [Restore](restore.md) · [Hygiene](hygiene.md) · [Troubleshooting](troubleshooting.md) ·
[Site config](site-config.md)
