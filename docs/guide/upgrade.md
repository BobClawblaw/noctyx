# Upgrading Noctyx

An upgrade replaces the package and re-renders the head node's services: systemd units on Linux, launchd daemons
in `/Library/LaunchDaemons` on a Mac (made from the same unit templates). Then you restart the long-running
services and refresh the node agents if the agent changed. Rolling back is the same process with the older
version. `noctyx versions` tells you, per machine, which of the components Noctyx depends on are behind and the
command that upgrades each. This page also covers moving a site from an earlier layout to Noctyx with the `paths`
block, so the directories, agent and unit names stay where they are.

Related pages: [install](install.md) · [site config](site-config.md#paths) · [console](console.md).

## Before you start

```bash
noctyx --version
sudo cp -a /etc/noctyx/site.json /etc/noctyx/site.json.$(date +%F)    # the CLI rewrites blocks of it in place
sudo noctyx tick --dry-run                                              # anything due soon?
systemctl list-units 'noctyx-*' --state=running                         # Linux: detached jobs in flight
ls /var/log/noctyx/jobs/*.pid                                           # macOS: a detached job's pid file sits next to its log
```

Long jobs (backups, verifies, prunes, restores, recipe switches) never run inside the console. On Linux each is
its own transient systemd unit (`noctyx-<command>-<target>-<time>-<pid>`); on a Mac it is its own session with a pid
file next to its log in `job_logs`. Restarting the console does not stop them, and when it starts again it picks
up the jobs it was following. The console's Runs panel shows them on both. Upgrade outside the backup window
anyway.

## Upgrade

**1. Get the new code.**

```bash
cd /opt/noctyx-src && git fetch --tags && git checkout <new tag>    # or: git pull
sudo -H /opt/noctyx/bin/pip install /opt/noctyx-src                 # reinstall into the venv
noctyx --version
```

On a Mac the venv is the one install made with Homebrew's Python (`/opt/homebrew/bin/python3 -m venv
/opt/noctyx`); the commands are the same. `pip install` run as root from a checkout leaves a root-owned `build/`
and `src/noctyx.egg-info` in it: remove them with `sudo rm -rf build src/noctyx.egg-info` before your next `git`
command there.

If the services run straight from a checkout (`PYTHONPATH=src`), the `git` step is enough.

**2. Re-render the head node's services.** Compare first, then apply:

```bash
sudo noctyx install-head | less                 # what the new templates render to (plists on a Mac)
sudo noctyx install-head --apply                # the same as: sudo noctyx setup units
```

- **Linux:** writes the units, `daemon-reload`, `enable --now`. Services that are already running keep running the
  old code until step 3.
- **macOS:** a daemon whose plist changed is unloaded, rewritten and loaded again (which restarts it); an unchanged
  one is left alone.

`sudo noctyx setup all` after an upgrade runs only the steps that are not done yet. It reports finished steps and
leaves them alone, so it never restarts every service; run `setup units` or `setup metrics` by name to re-render
those.

**3. Restart the long-running services.** The timer-driven services (tick, nightly, hygiene, retention) start a
fresh process each run, so they pick up the new code by themselves.

```bash
# Linux
sudo systemctl restart noctyx-console noctyx-directory noctyx-hf-mirror
systemctl status noctyx-console --no-pager
# macOS
for s in console directory hf-mirror; do sudo launchctl kickstart -k system/dev.noctyx.$s; done
sudo launchctl print system/dev.noctyx.console | grep -E 'state|pid'
```

(`noctyx` and `dev.noctyx` are the defaults of `paths.unit_prefix` and `paths.launchd_prefix`.) Open browsers
notice the new console build and reload once on their own.

**4. Refresh the node agents** when the release notes say the agent, the dumps hook or the exclusions changed, or
`noctyx versions` shows an agent as behind:

```bash
sudo noctyx install-agent                       # every Linux host Noctyx backs up; or --host NAME
sudo noctyx policy-apply                        # policy files only
```

**5. Check.**

```bash
sudo noctyx setup verify                        # every check, and a backup+restore round trip (--no-canary skips it)
sudo noctyx versions                            # every component on every machine
```

Then open the console's About page and confirm the version and the console build.

### A Mac head after a Homebrew Python update

Full Disk Access is granted to the Python binary itself, by its path in Homebrew's Cellar
(`/opt/homebrew/Cellar/python@3.x/<version>/…/bin/python3.x`). A Homebrew update of that Python moves it, and the
grant no longer applies: the scheduler, retention and the mirror lose the NAS share. `sudo noctyx setup verify`
reports it (*full disk access*); `sudo noctyx setup privacy` names the new path to add in System Settings ›
Privacy & Security › Full Disk Access, then proves the grant from launchd. A new *minor* Python (3.14 → 3.15)
removes the interpreter the venv points at: make the venv again (`/opt/homebrew/bin/python3 -m venv --clear
/opt/noctyx`), reinstall, and re-grant.

### Certificates made before key identifiers

Python 3.13 and newer verify certificates strictly and refuse a chain without key identifiers; certificates made
by earlier versions of `noctyx setup tls` lack them. `setup verify` shows it (*strict clients*). `sudo noctyx setup
tls` renews the server certificate under the same CA, so no node needs re-installing.

## Component versions

`noctyx versions` probes every machine (the head locally, the Linux nodes over ssh) for the components Noctyx
depends on, and compares each with the version Noctyx pins and the latest upstream release:

```bash
sudo noctyx versions                 # every machine
sudo noctyx versions --host gpu-01   # one machine; the others keep their last answer
sudo noctyx versions --refresh       # ask GitHub again now (otherwise once a day)
sudo noctyx versions --json
```

```text
head-01
  ✓ restic         0.19.1         pin 0.19.1    latest 0.19.1    current
  ↑ prometheus     2.45.3         pin —         latest 3.15.0    3.15.0 released (apt manages it)
gpu-01
  ✓ restic         0.19.1         pin 0.19.1    latest 0.19.1    current
  ✗ agent          25d3e6609bb7   pin —         latest —         older than this Noctyx's agent

to upgrade (apt takes what the distro carries, which can lag the upstream release):
  head-01      prometheus     sudo apt-get install --only-upgrade prometheus
  gpu-01       agent          sudo noctyx install-agent --host gpu-01
```

| Component | Where | Upgraded by |
|---|---|---|
| noctyx | the head | the steps above |
| restic | the head and every Linux node | head: `setup tools --force`; node: `install-agent --host NAME` |
| rest-server | the head (unless the endpoint is on the NAS) | `setup tools --force` |
| prometheus | the head | `setup metrics --force` |
| node_exporter | the head and every Linux node | `noctyx tool install-node-metrics --host NAME` (a head whose exporter came from `setup metrics`: `setup metrics --force`) |
| agent | every Linux node (compared by hash with the agent this Noctyx renders) | `install-agent --host NAME` |

A component is judged by who installed it. One in `/usr/local/bin` was installed by Noctyx and is held to Noctyx's
pin (`✗` behind when older). One installed by apt (`/usr/bin`) or Homebrew is compared with upstream only, and its
upgrade command is that package manager's. States: `✓` current, `↑` a newer release is out, `✗` behind or missing,
`·` newer than the pin (upgraded by hand), `?` the machine did not answer. A Mac head has the head's rows; a Mac that is only monitored runs
nothing Noctyx installs, so it has none. When `setup tools --force` or `setup metrics --force` replaces a binary, it
restarts the service that runs it (`rest-server`, `prometheus`, `node-exporter`), so the new binary is the one running.

**node_exporter from apt.** A Linux distro's archive can lag upstream by years (Ubuntu 24.04 carries 1.7.0), so
`apt-get install --only-upgrade` changes nothing. `noctyx tool install-node-metrics` runs Noctyx's pinned
node_exporter instead, checked against its SHA-256, in `/usr/local/bin/node_exporter`. The distro's package stays
installed: the same unit, user and `ARGS`. A drop-in
(`/etc/systemd/system/prometheus-node-exporter.service.d/noctyx.conf`) points the unit at the pinned binary and keeps
the two defaults the distro build changes: the systemd collector on, and textfiles read from
`/var/lib/prometheus/node-exporter`. It turns the cpufreq collector off: in 1.12.1 it never returns on a GB10
(DGX Spark), which times out every scrape, and Noctyx reads none of its series. To go back to the distro's binary, remove the drop-in and run
`systemctl daemon-reload && systemctl restart prometheus-node-exporter`.

### Acting on a component alert

The console's Alerts panel and each machine's Components table carry the buttons; the CLI does the same:

```bash
sudo noctyx upgrade --host gpu-01 --component node_exporter     # Noctyx's own upgrade, then checks gpu-01 again
sudo noctyx versions-skip --host gpu-01 --component node_exporter --release 1.12.1   # stay on what runs
sudo noctyx versions-skip --host all --component prometheus --release 3.15.1         # every host
sudo noctyx versions-skip --host gpu-01 --component node_exporter --undo             # alert on it again
```

- **upgrade** runs the row's Noctyx upgrade (the table above) as a job, then runs `noctyx versions --host` for
  that machine, so the alert clears when the component is current. It needs an admin. Noctyx only ever
  installs its own pin, so a row it cannot fix says why in place of the button: Noctyx itself (pip), anything
  Homebrew manages, a head's Prometheus from apt (2.x to 3.x changes its config and storage), and a release newer
  than Noctyx's pin, which arrives with the Noctyx release that moves the pin.
- **skip** marks that release as skipped (`<state>/version-skips.json`) for that host, or for every host with
  `--host all`. The row reads `skipped`, its alert resolves, and a later release alerts again. It needs an
  operator, like a silence. Unlike a silence it has no end time, and it is tied to the release, not the alert.
- The alert's text names the command that works (`noctyx upgrade …`, or why there is none), not a package
  manager command that would change nothing.

The nightly run refreshes the inventory (`<state>/versions.json`, upstream releases cached in
`<state>/upstream-releases.json`). The console shows it in each machine's Components table and exports it on
`/metrics`; the alert rules `ComponentBehind`, `ComponentUpgradeAvailable` and `VersionsUnknown` notify on it.

**Moving a pin.** The pinned versions and their SHA-256 sums live in
[`src/noctyx/components.py`](../../src/noctyx/components.py). Change a version and its sums (from the release's own
sums file) together, in one commit. Setup refuses a download whose sum does not match. Then run the upgrade command
on each machine.

## Roll back

```bash
cd /opt/noctyx-src && git checkout <previous tag>
sudo -H /opt/noctyx/bin/pip install /opt/noctyx-src
sudo noctyx install-head --apply
sudo systemctl restart noctyx-console noctyx-directory noctyx-hf-mirror        # macOS: launchctl kickstart -k, as above
sudo noctyx install-agent                       # only if the upgrade refreshed the agents
```

If a newer version added keys to the site config, older versions ignore keys they do not read. Restore the
`site.json` copy you made only if the newer version rewrote a block in a form the older one rejects. State files
(`state`, `console_state`, `audit`) need no conversion.

## Migrating from an earlier layout

A cluster set up by an earlier tool can run Noctyx without moving anything. Every file, directory, unit and user
name Noctyx touches is a key in [`paths`](site-config.md#paths), and the site config's `"paths"` block overrides
any of them. For example, a site whose files lived under `/etc/oldtool`, whose units were called `oldtool-*` and
whose agent was `/usr/local/bin/oldtool-agent`:

```json
"paths": {
  "etc": "/etc/oldtool",
  "state": "/etc/oldtool/state",
  "tls": "/etc/oldtool/tls",
  "repo_keys": "/etc/oldtool/repo-keys",
  "repo_creds": "/etc/oldtool/repo-creds",
  "ui_token": "/etc/oldtool/ui-token",
  "audit": "/var/lib/oldtool/audit.jsonl",
  "unit_prefix": "oldtool",
  "repo_owner": "oldtool",
  "repo_dir_prefix": "ot-",
  "agent_bin": "/usr/local/bin/oldtool-agent",
  "node_etc": "/etc/oldtool",
  "node_lib": "/var/lib/oldtool",
  "result_token": "OLDTOOL_RESULT",
  "snapshot_tag": "oldtool"
}
```

Steps:

1. **Write the block, then check what is in effect.** Every key you leave out keeps the Noctyx default.

   ```bash
   noctyx paths
   ```

2. **Keep copies of the existing units.** `install-head --apply` writes `/etc/systemd/system/<unit_prefix>-*`.
   With the old prefix, it overwrites the old units of the same name.

   ```bash
   sudo mkdir -p /root/units-before-noctyx && sudo cp -a /etc/systemd/system/oldtool-* /root/units-before-noctyx/
   ```

3. **Stop the old tool's timers and services** so that two schedulers never run at once. Then install Noctyx's
   units:

   ```bash
   sudo noctyx install-head                 # read it first
   sudo noctyx install-head --apply
   ```

4. **Check the existing repositories.** Noctyx expects `<repo_root>/<repo_dir_prefix><repo>/<repo>`, keys at
   `<repo_keys>/<repo>.key` and endpoint passwords at `<repo_creds>/<repo_dir_prefix><repo>.pass`. Then confirm
   it can read them:

   ```bash
   sudo noctyx snapshots --limit 5          # through each node's agent
   sudo noctyx tick --dry-run               # read on the head, straight from the repositories
   ```

   The scheduler counts a snapshot towards a profile only if the snapshot carries a `profile-<name>` tag. Hosts
   whose old snapshots lack that tag show as "never" until their first Noctyx backup.

5. **Nodes.** The orchestrator reads the agent's result line by `result_token`, and it also accepts the older
   tokens it knows. Nodes can therefore keep their existing agent until you run `install-agent`, which installs
   the Noctyx agent at `agent_bin`.

6. **Move later, one key at a time.** Change a key, move the files it names, then re-run `install-head --apply`
   (for head paths) or `install-agent` (for node paths). Remove the key from the block once its files sit at the
   Noctyx default.

To test without touching the site config, a `NOCTYX_PATH_<NAME>` environment variable overrides a single key
(for example `NOCTYX_PATH_UNIT_PREFIX=oldtool noctyx paths`).
