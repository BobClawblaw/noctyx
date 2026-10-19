# Hygiene

Hygiene is a housekeeping report for every Linux node. It lists failed and ghost units, stale reboot flags,
packages that are no longer needed, old kernels, an oversized journal, Docker leftovers, logrotate errors,
a full root filesystem and services running outdated binaries. Items that cannot break anything are fixed
on a timer. Items that reclaim space or remove packages wait for a person. Some are report-only. Every
space item shows how much it would free. This page also covers `noctyx reboot`, which refuses to take a
host down while anything Noctyx cares about is running there.

## Run the report

```bash
sudo noctyx hygiene                         # every host: report only
sudo noctyx hygiene --host gpu-01           # one host
sudo noctyx hygiene --fix auto              # fix the tier that cannot break anything (the daily timer)
sudo noctyx hygiene --host gpu-01 --fix manual                 # every space/package fix on that host
sudo noctyx hygiene --host gpu-01 --fix journal,docker-prune   # specific item ids
```

- The head node runs `noctyx-agent hygiene [--fix …]` on each host over ssh. That covers every enabled
  host except macOS hosts and `windows-class` hosts.
- The agent takes its one-job-per-host lock, so hygiene never runs at the same time as a backup on
  that host.
- `noctyx hygiene` runs as a detached job. Use `--foreground` or `--no-follow` as needed (see
  [Backups › Detached jobs](backups.md#detached-jobs-and-follow)).
- The result is written to `/var/lib/noctyx-console/hygiene.json`, which the console reads, and one
  `hygiene` audit line is written per host.
- `noctyx-hygiene.timer` runs `hygiene --fix auto` every day at 06:45.

Sample output of `--fix auto`:

```text
  gpu-01     4 item(s), 1 fixed
     · [info] old kernels installed — 6.8.0-45-generic: 7 package(s) (running 6.8.0-48-generic; newest kept)  (manual)  frees 0.6 GB
     · [info] journal uses 3.1G — vacuum to 500M keeps recent history  (manual)  frees 2.8 GB
     · [info] docker leftovers — 3 dangling image(s), 2 exited container(s)  (manual)  frees ~4.2 GB
     ✓ [info] ghost failed unit foo.service — its unit file no longer exists; only the failed state lingers  (auto)
reclaimable: about 7.6 GB (the space fixes, if all applied)
hygiene: 4 item(s) across 1 host(s), 1 fixed
```

`✓` marks an item fixed in this run, and `·` an item still open. `(auto)` and `(manual)` show the item's
tier.

## Items

| Id | Severity | Tier | Found when | The fix |
|---|---|---|---|---|
| `unit:<name>` (ghost) | info | auto | a failed unit whose unit file no longer exists | `systemctl reset-failed` |
| `unit:user@<uid>.service` | warning | auto | a user session manager killed by a signal | `reset-failed` + `start` |
| `unit:<name>` (real failure) | warning | manual, **by id only** | any other failed unit; the detail shows its last 3 journal lines | `restart` if the unit is enabled or has `Restart=`; otherwise only clear the failed state |
| `reboot-flag` | info | auto | `/var/run/reboot-required` exists, but the running kernel is the newest installed and the loaded NVIDIA module matches its package | remove the flag files |
| `reboot-needed` | warning | report | the same flag, and a newer kernel or a mismatched NVIDIA module (the detail says which) | use [`noctyx reboot`](#reboot-safely) |
| `apt-autoremove` | info | manual | `apt-get -s autoremove` would remove packages | `apt-get autoremove --purge` |
| `old-kernels` | info | manual | installed `linux-image-*` versions other than the running one and the newest other one | purge every installed `linux-*` package of those versions (their initrd goes with them) |
| `journal` | info | manual | `journalctl --disk-usage` reports 1 G or more | `journalctl --vacuum-size=500M` |
| `docker-prune` | info | manual | dangling images or exited containers exist | `docker image prune -f` and `docker container prune -f` |
| `logrotate` | warning | report | `logrotate -d` reports an error | none |
| `root-space` | warning | report | `/` is 85 % full or more | none |
| `needrestart` | info | report | `needrestart` lists services on outdated binaries. Noctyx's own units, vllm, docker, containerd and ssh are left out of the list | none |

Severity is `info` or `warning`. An item without a tier is report-only: no command fixes it.

### How the freed bytes are worked out

Each space item carries `bytes`, which is what its fix would free, measured the same way the fix works:

- **apt-autoremove:** the sum of `Installed-Size` of exactly the packages autoremove would remove.
- **old-kernels:** the `Installed-Size` of every installed `linux-*` package of those versions, plus
  their `/boot/initrd.img-*` files.
- **journal:** the current usage minus 500 MiB.
- **docker-prune:** Docker's own accounting. It adds the unique size of each dangling image and the size
  of each exited container. Layers that are shared only among dangling images are not counted, so the
  figure runs slightly low. It is marked as an estimate (`bytes_approx`) and is printed with a leading
  **`~`**.

The `reclaimable:` line adds up the bytes of every unfixed space item in the last report.

## `--fix`

| Value | Fixes |
|---|---|
| `auto` | every `auto` item |
| `manual` | every `manual` item **except** real failed units |
| `ID,ID,…` | exactly those ids, including real failed units (`unit:nginx.service`). The console's per-item buttons send this |

A real failed unit is restarted only when its id is named. A bulk `--fix manual` never restarts a service.
The agent also accepts `--fix all`, which fixes every item that has a fix, real failed units included.

In the console, the Hygiene action reports on every host and fixes nothing. Fixing items (`hygieneFix`)
needs the operator role. Each id must be a fixable item in that host's last report.

## Reboot safely

```bash
sudo noctyx reboot --host gpu-01 --plan   # JSON: allowed, blockers, warnings, impact; changes nothing
sudo noctyx reboot --host gpu-01 --yes    # re-checks the plan, then reboots
```

The reboot is **refused** (exit 2, audited as `reboot-refused`) while any of these hold:

- a backup or restore is writing on the host (`/var/lib/noctyx/agent.lock` is held)
- the package manager is running there (a dpkg lock is held)
- a Noctyx job for that host is running on the head node
- a reboot of the host is already in progress (its record is less than an hour old)
- the host cannot be reached
- it is a macOS host, or not a managed Linux host

For the head node, any other running Noctyx job is also a blocker, because the reboot would kill it.

The plan's **impact** lists what goes down with the host: the LLM servers it serves, a tensor-parallel
worker or head that stops with it, and the console's assistant if it points there. For the head node it
adds the console, Prometheus and Alertmanager, and that scheduled backups pause until the first tick after
boot.

After a confirmed reboot of a **remote** host:

- `systemctl reboot` is scheduled 10 s out.
- The command waits for a new `boot_id`, for up to 25 minutes.
- Discovery is rerun until every model that was serving is serving again, for up to 10 minutes.
- A hygiene rescan runs, and one `reboot` audit line is written. The exit code is 1 if a model did not
  come back.

The **head node** cannot watch itself reboot. The reboot is scheduled 20 s out, and a pending record is
left in `/var/lib/noctyx-console/reboots/`. When the console starts on the new boot, it runs
`noctyx reboot --host <head> --finish`, which completes the same post-boot checks.

Without `--yes`, `reboot` changes nothing.

See also: [Backups](backups.md) · [Troubleshooting](troubleshooting.md)
