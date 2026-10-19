# Upgrading Noctyx

An upgrade replaces the package and re-renders the head node's systemd units. Then you restart the long-running
services and refresh the node agents if the agent changed. Rolling back is the same process with the older
version. This page also covers moving a site from an earlier layout to Noctyx with the `paths` block, so the
directories, agent and unit names stay where they are.

Related pages: [install](install.md) · [site config](site-config.md#paths) · [console](console.md).

## Before you start

```bash
noctyx --version
sudo cp -a /etc/noctyx/site.json /etc/noctyx/site.json.$(date +%F)    # the CLI rewrites blocks of it in place
sudo noctyx tick --dry-run                                              # anything due soon?
systemctl list-units 'noctyx-*' --state=running                         # detached jobs in flight
```

Long jobs (backups, verifies, prunes, restores, recipe switches) run as their own transient systemd units
(`noctyx-<command>-<target>-<time>-<pid>`), not inside the console. Restarting the console does not stop them,
and when it starts again it picks up the jobs it was following. To keep things simple, upgrade outside the
backup window anyway.

## Upgrade

**1. Get the new code.**

```bash
cd /opt/noctyx-src && git fetch --tags && git checkout <new tag>    # or: git pull
sudo /opt/noctyx/bin/pip install /opt/noctyx-src                    # reinstall into the venv
noctyx --version
```

If the units run straight from a checkout (`PYTHONPATH=src`), the `git` step is enough.

**2. Re-render the head node's units.** Compare first, then apply:

```bash
sudo noctyx install-head | less                 # what the new templates render to
sudo noctyx install-head --apply                # writes them, daemon-reload, enable --now
```

`--apply` enables and starts services, but it does not restart services that are already running.

**3. Restart the long-running services.** The timer-driven services (tick, nightly, hygiene, retention) start a
fresh process each run, so they pick up the new code by themselves.

```bash
sudo systemctl restart noctyx-console noctyx-directory noctyx-hf-mirror
systemctl status noctyx-console --no-pager
```

Open browsers notice the new console build and reload once on their own.

**4. Refresh the node agents** when the release notes say the agent, the dumps hook or the exclusions changed:

```bash
sudo noctyx install-agent                       # every enabled host; or --host NAME
sudo noctyx policy-apply                        # policy files only
```

**5. Check.**

```bash
noctyx status
sudo noctyx tick --dry-run
```

Then open the console's About page and confirm the version and the console build.

## Roll back

```bash
cd /opt/noctyx-src && git checkout <previous tag>
sudo /opt/noctyx/bin/pip install /opt/noctyx-src
sudo noctyx install-head --apply
sudo systemctl restart noctyx-console noctyx-directory noctyx-hf-mirror
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
