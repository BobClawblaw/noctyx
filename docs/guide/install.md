# Installing Noctyx

This page takes a new site from nothing to a verified head node. The head runs the orchestrator, the console, the
directory service, the Hugging Face mirror, the repository endpoint and the timers. It runs on **Linux** (systemd)
or **macOS** (launchd); which one is decided by the machine you install it on, and the same commands work on both.
`noctyx init` writes the site config by asking questions, `noctyx setup all` sets the head up step by step, and
`noctyx setup verify` proves it works end to end. If you only want to look around first, run the
[demo](#demo-mode): it needs no configuration and changes nothing.

Related pages: [site config reference](site-config.md) · [accounts](accounts.md) · [console tour](console.md) ·
[upgrading](upgrade.md) · [troubleshooting](troubleshooting.md).

## Platforms

| Role | Linux | macOS |
|---|---|---|
| Head node (console, orchestrator, endpoint, timers) | yes: systemd units | yes: launchd daemons |
| Backed-up node (the agent, restic snapshots) | yes | **no**: a Mac is monitored only |
| Monitored machine (facts, metrics, LLM servers) | yes | yes |

Noctyx backs up **Linux** machines only. A Mac has Time Machine; Noctyx never installs the agent on one. It reports a
Mac's status instead: vitals, GPU, LLM servers and, for a Mac head, its Time Machine destination and last backup.

## Requirements

**Head node:**

| Need | Linux | macOS |
|---|---|---|
| Service manager | systemd | launchd (built in) |
| Python ≥ 3.11, standard library only | the distribution's `python3` | Homebrew's (`brew install python`); macOS's own `/usr/bin/python3` is too old |
| `openssl`, `htpasswd` | `apt install openssl apache2-utils` | ship with macOS |
| `ssh`, `curl` | `apt install openssh-client curl` | ship with macOS |
| restic, rest-server | `noctyx setup tools` installs them | `noctyx setup tools` installs them |
| Storage for the repositories | a local disk, or a NAS share over NFS or SMB | the same |

restic (0.19.1) and rest-server (0.14.0) no longer need installing by hand: `noctyx setup tools` downloads the
pinned releases for this machine into `/usr/local/bin` and refuses any download whose SHA-256 does not match the
sum pinned in [`components.py`](../../src/noctyx/components.py).

**Nodes** (Linux machines that are backed up):

- reachable over ssh as `server.ssh.user` (or the host's `ssh_user`) with the head's root key, and passwordless
  `sudo -n` there
- Debian/Ubuntu style: `install-agent` installs restic with `apt-get` if it is missing and trusts the CA with
  `update-ca-certificates`
- systemd and `curl`. Before every backup the orchestrator measures the path with curl.

## 1. Install the package

From a checkout, into its own virtual environment. The services run the Python interpreter that ran the setup.

```bash
git clone https://github.com/BobClawblaw/noctyx.git /opt/noctyx-src
sudo python3 -m venv /opt/noctyx                     # macOS: sudo /opt/homebrew/bin/python3 -m venv /opt/noctyx
sudo /opt/noctyx/bin/pip install /opt/noctyx-src
sudo ln -sf /opt/noctyx/bin/noctyx /usr/local/bin/noctyx
noctyx --version
```

`noctyx` with no arguments prints the banner and the command list. `noctyx -h` lists every orchestrator command.

macOS's `sudo` runs with a `PATH` that leaves out `/usr/local/bin`. Noctyx adds `/usr/local/bin` (and Homebrew's
`/opt/homebrew/bin` on a Mac) to its own `PATH` when it starts, so every step finds what `setup tools` installed.
Over a non-interactive ssh session, call it by its full path: `sudo /usr/local/bin/noctyx …`.

## 2. Write the site config: `noctyx init`

```bash
sudo noctyx init
```

`init` writes the site config (`NOCTYX_CONFIG`, default `/etc/noctyx/site.json`) from a few questions with
defaults. Enter takes the `[default]`. It asks, in this order:

1. **The assistant**, first, so it can help with everything after it. `init` looks for OpenAI-compatible servers
   (vLLM, SGLang, llama.cpp, MTPLX, TensorFold, Ollama, LM Studio) on the usual ports of this machine, its LAN address
   and any machines you name, lists what answers, and tests the one you pick with a short completion. An API key
   is asked for (hidden) and stored 0600 in the `llm_keys` directory. From then on, answering **`?`** to any
   question asks the assistant about it, with this machine's facts. `none` skips it: setup works without one.
2. **This machine**: its short name and LAN address (detected; use the wired address, nodes reach the head on it
   and its certificate names it) and the console port (checked free).
3. **Storage**: where the repositories and the model mirror live.

   | Choice | You give | Defaults |
   |---|---|---|
   | `local` | a directory (an external disk is fine) | `/Library/Noctyx` (macOS), `/srv/noctyx` (Linux) |
   | `nfs` | NAS address, export path, mount point, a folder on the share, the uid the NAS lets write | mount at `/Volumes/noctyx-nas` (macOS), `/mnt/noctyx-nas` (Linux) |
   | `smb` | NAS address, share (and folder), NAS user, mount point, a folder on the share | the same mount points |

   Repositories go in `<base>/repos` and the mirror in `<base>/hf-mirror`. For NFS, `init` shows the NAS's exports
   (`showmount -e`) and records the uid in `server.repo_owner_uid`: an NFS share checks the writer's uid, so match
   the NAS user that owns the folder.
4. **Machines**: what Noctyx backs up.
   - a **Mac head** is monitored, not backed up; `init` names its Time Machine destination if it has one
   - a **Linux head** can back itself up (asked; the head holds the keys and the site config)
   - **Linux nodes**, one per line as `NAME ADDRESS`. Each gets its own repository. If root's ssh key
     (`server.ssh.key`) is missing, `init` makes one and prints the line to add to each node's `authorized_keys`,
     for the user the head logs in as (it needs passwordless sudo there).
   - two profiles: `system` (`/`, nightly 01:00–07:00) and `config` (`/etc` and `/usr/local/etc`, a quick first
     backup)
5. **The console**: sign-in on or off, and the first admin account (password asked twice, 10+ characters).

At the end `init` prints the next commands and offers to run `noctyx setup all`.

### In a browser: `noctyx init --web`

```bash
sudo noctyx init --web            # --port N for another port than 20992
```

The same setup as a web page, with the assistant beside every step. `init --web` prints a one-time link
(`https://<this machine>:20992/?t=…`). Opening it sets a cookie in that browser, and only that browser gets in.
The page uses the site's certificate once `setup tls` has made one, else a self-signed certificate made for this
run, so the browser warns once. **Finish** spends the link and stops the page; the console takes over.

1. **The assistant** first, in a form: the page looks for LLM servers (as `init` does), you pick one or type a
   URL, and it is tested and saved. The assistant must support OpenAI tool calls (vLLM, SGLang, llama.cpp,
   TensorFold and Ollama do, with a model that was trained for them).
2. **Then you talk to it.** It looks before it proposes: it reads the site config, scans this machine's `/24` for
   machines with SSH or a node_exporter, probes a machine (SSH login with the head's key, arch, OS, passwordless
   sudo), lists a NAS's NFS exports and reads this guide. Those run as soon as it asks.
3. **Every change is a proposal**: setting up the head, the storage, sign-in, adding a machine, running a
   `noctyx setup` step, installing the agent or a node's metrics. A proposal shows its values and the
   assistant's reason. You can change any value, then approve or reject it. Only an approved proposal runs, and
   it runs the same code as `noctyx init` and `noctyx setup`. Setup steps and installs run as jobs whose output
   shows live; when one fails, the assistant reads the output and says what to fix.
4. **Passwords never reach the LLM.** The first admin's password and an SMB share's password (Linux heads) go into
   forms in the side panel. Escrow's passphrase is typed in a terminal: `sudo noctyx setup escrow`.

The side panel shows where the site stands (assistant, head, storage, sign-in, the head's setup steps, the
machines), the head's public key with the exact line to authorise it on a node, and a **by hand** form for each
change, for when you would rather not ask. A Mac added as a machine is monitored, never backed up; its metrics
are installed over SSH from the head (it needs Homebrew).

The assistant only reaches what its tools reach: no shell, nothing outside this list, and every argument is
checked (names, addresses, paths, ports) before it touches anything.

**Re-running `init`** edits the existing config: its values are the defaults. A site that is already set up keeps
its storage (spindles, `repo_root`, the mirror store), every repository, every host entry as it is, its assistant
and its sign-in setting, unless you change them in the answers. New nodes are only added. `init --assistant-only`
sets up just the LLM link, and works before there is a config.

**Without questions** (scripts, ssh): `init --yes` takes the flags, then the detected values and defaults.

| Flag | |
|---|---|
| `--assistant URL`, `--model ID`, `--scan ADDR …` | the LLM endpoint (`http://host:port/v1`), its model, more machines to look on. A key comes from the `NOCTYX_ASSISTANT_KEY` environment variable, never a flag. |
| `--hostname`, `--address`, `--ui-port` | this machine |
| `--storage local\|nfs\|smb`, `--path DIR` | the storage; `--path` for local |
| `--nas-server`, `--nas-path`, `--nas-user`, `--mount DIR`, `--subdir DIR`, `--owner-uid UID` | a NAS share |
| `--node NAME=ADDRESS` (repeatable), `--ssh-user USER`, `--no-head-backup` | the machines |
| `--no-auth`, `--admin NAME`, `--admin-password-stdin` | the console |
| `--setup` | then run `noctyx setup all` |

```bash
sudo noctyx init --yes --assistant http://192.0.2.21:8000/v1 --storage nfs --nas-server 192.0.2.5 \
  --nas-path /volume1/backups --owner-uid 1026 --node gpu-01=192.0.2.21 --node gpu-02=192.0.2.22 --ssh-user ops \
  --admin alice --admin-password-stdin < password.txt
```

`noctyx paths` prints every file, unit and user name Noctyx will use. The [site config reference](site-config.md)
describes every key, for anything `init` does not ask.

### The config file's mode

`setup owner` makes the config `0640 root:<repo_owner>`: the directory service runs as the repository owner and
reads it. It holds no secrets (keys, logins and the CA key live apart, root-only). Do not make it 0600: the
directory service then cannot start. Re-running `init` keeps the file's owner and mode.

## 3. Set up the head: `noctyx setup`

```bash
sudo noctyx setup              # where the site is, and the next step
sudo noctyx setup all          # every step in order; stops at the first that fails
sudo noctyx setup <step>       # one step
```

```text
  ✓ assistant http://192.0.2.21:8000/v1 qwen
  ✓ tools     restic 0.19.1, rest-server 0.14.0, openssl, htpasswd
  ✓ owner     the repository owner noctyx
  ✓ mount     storage: nas 192.0.2.5:/volume1/backups at /mnt/noctyx-nas
  ✓ tls       the site CA and the head's certificate
  ✓ repos     repositories (2/2 made)
  ✓ endpoint  the repository endpoint https://192.0.2.10:20998
  ✓ units     the head's services (console, directory, mirror, timers)
  ✓ metrics   Prometheus on 127.0.0.1:9090: the console's live numbers
  · escrow    an escrow bundle of the keys

next: sudo noctyx setup escrow      (or every remaining step: sudo noctyx setup all)
```

Every step can be re-run: it keeps what exists. `setup all` skips the steps that are already done (to re-run one
on purpose: `sudo noctyx setup <step>`). `escrow` asks for a passphrase, so `setup all` skips it outside a
terminal; run it yourself.

### What each step does

| Step | Linux | macOS |
|---|---|---|
| `tools` | restic and rest-server, pinned and checked, into `/usr/local/bin`; checks `openssl`, `htpasswd`, `ssh`, `curl`. An existing binary is kept (`--force` replaces it). | the same, Darwin builds |
| `owner` | the system user `repo_owner` (`useradd --system`); `<etc>` (0750, group `repo_owner`), `<repo_keys>` and `<repo_creds>` (0700), `<state>`, the log and state directories; the config 0640 | the same user made with `dscl`: hidden, no login shell, uid below 500 unless `server.repo_owner_uid` pins it |
| `mount` | mounts each spindle that names a `share` (NFS: `mount -t nfs`; SMB: `mount -t cifs` with a 0600 credentials file, the password asked once) and prints its `/etc/fstab` line; creates `repo_root` and the mirror store, owned by `repo_owner`, and checks that user can write them | NFS with `resvport`; SMB with `mount_smbfs`, mounted **as** `repo_owner` (smbfs files belong to whoever mounted the share), which asks the password itself |
| `tls` | a 10-year CA and the head's certificate (825 days), with key identifiers; the CA goes into the system trust store (`update-ca-certificates`) | the same; the CA is **not** put in the System keychain (see [macOS](#macos-specifics)) |
| `repos` | per repository: its key, its endpoint login, a bcrypt `htpasswd` line, and `restic init` as `repo_owner` | the same |
| `endpoint` | rest-server on the head as `repo_owner` (`<unit_prefix>-rest-server.service`), `--private-repos --append-only`; with the endpoint on the NAS, `noctyx nas-rest deploy` instead | the same, as a launchd daemon |
| `units` | the head's services and timers ([below](#the-heads-services)) | the same, as launchd daemons |
| `metrics` | Prometheus and the head's own exporter ([below](#metrics-noctyx-setup-metrics)) | the same |
| `privacy` | (nothing to do) | Full Disk Access for the daemons that read the repositories ([below](#full-disk-access-noctyx-setup-privacy)) |
| `escrow` | an encrypted bundle of every key, login, the TLS material, htpasswd and the site config (AES-256, PBKDF2 200 000 rounds) in `<etc>/escrow/`; the passphrase is asked twice and stored nowhere | the same |

`setup tls`: the certificate names `server.address`, `server.hostname`, `localhost` and `127.0.0.1`, plus anything
in `server.tls.san` (e.g. `["DNS:head-01.lan"]`). Re-running renews a certificate with under 30 days left, or one
made before Noctyx added key identifiers (Python 3.13 and later verify certificates strictly and refuse a chain
without them), and **keeps the CA**. `--force` replaces the CA, and then every node must be re-installed.

`setup escrow`: **without the keys every backup is unreadable.** Keep the bundle off this machine and off the NAS,
and check it opens somewhere else:
`openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in noctyx-keys-<date>.tar.gz.enc | tar tz`. The checklist flags
keys made after the newest bundle.

### When a step fails: the assistant

A step that fails prints the error, then asks the site's assistant what went wrong, with the error, this machine's
platform and service manager, and this install guide, and prints its answer:

```text
  ✗ endpoint: noctyx-rest-server.service: endpoint https://192.0.2.10:20998 DOWN
  ┌ assistant (qwen)
  │ …
  └ (NOCTYX_NO_ASSIST=1 turns this off)
```

`noctyx ask "QUESTION"` asks it anything about the install; pipe a log in with `… | noctyx ask`.

### The endpoint on the NAS

When `server.rest_host` is set and differs from `server.address`, the endpoint runs on the NAS instead.
Set `server.nas_ssh.repo_root` to the NAS's own path to the same directory as `repo_root`
(e.g. `/volume1/backups/noctyx`), install the rest-server build for the NAS's CPU at
`/usr/local/bin/rest-server` on the head, then:

```bash
sudo noctyx nas-rest deploy    # a certificate for the NAS from the CA, the binary, run.sh, htpasswd -> ~/<nas_ssh.dir>
sudo noctyx nas-rest status    # start | stop | restart | status
```

`setup endpoint` runs `nas-rest deploy` itself when the endpoint is on the NAS. Every `tick` and every backup checks
the endpoint and restarts it over ssh if it is down, so it comes back after a NAS reboot without a boot task.

### The head's services

`setup units` (or `noctyx install-head --apply`) installs them; `noctyx install-head` alone prints what it would
write: systemd units on Linux, the launchd property lists made from the same units on macOS.

| Service (prefix `noctyx`) | What it runs |
|---|---|
| `console` | the web console on `server.ui_port`, as root |
| `directory` | the directory and discovery service (`directory_port` TCP, `discovery_udp_port` UDP), as `repo_owner` |
| `hf-mirror` | the Hugging Face mirror on `hf_mirror.port`, as `repo_owner` |
| `tick` | `noctyx tick` every 15 minutes: runs the backups that are due |
| `nightly` | 07:30 daily: HF harvest, recipe discovery, `noctyx versions`. On Sundays also a prune dry run and a verify. |
| `hygiene` | 06:45 daily: `hygiene --fix auto` |
| `retention` | 08:00 on the 1st of each month: `prune --apply`, gated by `restic check` |

On Linux they are `/etc/systemd/system/<unit_prefix>-<name>.service` (and `.timer`); on macOS
`/Library/LaunchDaemons/<launchd_prefix>.<name>.plist`, e.g. `dev.noctyx.console`. A re-run restarts nothing
whose definition did not change. The console's sign-in setting comes from `server.console_auth`, see
[accounts](accounts.md).

To change the console port, set `server.ui_port` (default `20996`) and run `setup units` again. The
`NOCTYX_CONSOLE_PORT` environment variable overrides it for a one-off run.

### Metrics: `noctyx setup metrics`

The console's live numbers (temperatures, power, tokens/s, sparklines) come from a Prometheus on
`127.0.0.1:9090`. `setup metrics` installs one when the head has none:

| Piece | Where |
|---|---|
| Prometheus (pinned, checked) | `/usr/local/bin/prometheus`, config `/etc/prometheus/prometheus.yml` (`paths.prom_config`), data `paths.prom_data` (30 days), as `repo_owner` |
| the head's own node_exporter | `127.0.0.1:20994`, loopback only, as `repo_owner` |
| the textfile collector, every 15 s, as root | GPU, power and LLM-server numbers: `node-textfile.sh` (NVIDIA, Docker) on Linux, `mac-textfile.sh` (powermetrics) on macOS |
| LLM discovery, every 5 min | finds LLM servers and keeps the node-exporter targets file current |

The generated `prometheus.yml` scrapes the console, the head's exporter, every node's exporter (targets in
`paths.prom_node_targets`, TLS and basic auth with `/etc/prometheus/node-exporter.pass`) and the LLM servers, and
loads Noctyx's alert rules. It starts with `# managed by noctyx setup metrics`; `setup metrics` refuses to overwrite
a `prometheus.yml` that is not its own unless `--force`. A head that **already runs a Prometheus** keeps it:
`setup metrics` prints the scrape jobs to add to it instead. On Apple silicon, `brew install macmon` adds CPU and
GPU temperatures (powermetrics has none). Re-runs restart nothing unless something changed; `--force` re-downloads
the binaries.

## 4. Verify: `noctyx setup verify`

```bash
sudo noctyx setup verify             # every check, then a backup + restore round trip
sudo noctyx setup verify --no-canary # the checks only
```

| Section | Checks |
|---|---|
| site | the config, that `repo_owner` can read it, the head's host entry, that the assistant answers |
| tools | restic, rest-server, openssl, htpasswd |
| storage | the owner, each spindle mounted (or present), `repo_root` writable by the owner |
| tls | the CA, the certificate (30+ days), its chain, that it names the head's address, its key identifiers |
| repositories | the endpoint is up, it can read the repositories, each repository opens through it with its key and login; on macOS, Full Disk Access for Noctyx's Python |
| services | console, directory, mirror and rest-server running; the timers scheduled; the console, directory and mirror answer |
| metrics | Prometheus answers; every scrape target is up |
| keys | an escrow bundle newer than every key |
| hosts | each node reachable with the agent installed; a Mac head's Time Machine (a destination, a backup in the last two days) |
| round trip | 1 MiB of random data backed up through the endpoint, restored, compared byte for byte, then the snapshot forgotten on the head |

`✓` passes, `✗` fails, `!` is a warning (the assistant, escrow, Time Machine, LLM scrapes). With failures,
`verify` exits 1 and asks the assistant about them.

## 5. Add the nodes

Nodes are Linux machines. Authorise the head's root key (`server.ssh.key`) for `server.ssh.user` on each node and
give that user passwordless sudo (`init` printed the line). Then:

```bash
sudo noctyx install-agent --host gpu-01            # one node
sudo noctyx install-agent                          # every Linux host Noctyx backs up
sudo noctyx install-agent --host gpu-01 --profile config
```

`install-agent` skips a macOS host: Macs are monitored, never backed up. A host entry with `"local": true` (the
head itself) is installed with the same commands run locally, without ssh to its own address. For each node,
`install-agent`:

1. installs restic if it is missing
2. installs the agent (`agent_bin`, default `/usr/local/bin/noctyx-agent`)
3. installs the CA into `<node_etc>/tls/ca.crt` and the system trust store
4. pipes the repository key, the endpoint password and the repository URL through stdin into root-only files, so
   none of them appear on a command line
5. writes `client.conf`, the exclusions and the profile's paths
6. adds a `noctyx-backup.service`
7. proves the repository opens with a `restic snapshots` unlock test
8. runs `policy-apply` for that host: per-host exclusions, Docker volumes and database dumps, as the site's
   `backup_policy` says (both on by default; `noctyx policy-set` changes them)

`--with-client-timer` adds a nightly timer on the node itself. That timer bypasses the head's per-spindle lock.
Leave it off so the head schedules backups.

## 6. First backup

Start small. The `config` profile (`/etc`, `/usr/local/etc`) takes seconds. Before backing up anything bigger,
check its size on the node (`sudo du -sh <path>`): a home directory can hold hundreds of GB of models.

```bash
sudo noctyx hosts                                    # what the config says
sudo noctyx probe --host gpu-01                      # measured Mbit/s on the real path
sudo noctyx backup --host gpu-01 --profile config --dry-run
sudo noctyx backup --host gpu-01 --profile config
sudo noctyx snapshots --host gpu-01
```

`backup` runs as a detached job (a transient systemd unit on Linux; its own session with a pid file next to its log
on macOS) and the command follows its log, so stopping the command does not stop the job. Add `--foreground` to run
it in-process, or `--no-follow` to start it and return. A backup refuses to start if the probe fails or measures
below `network.min_throughput_mbit` (default 40); `--force-low-net` overrides this. After the first backup,
`noctyx tick --dry-run` shows what the schedule will run next, and `noctyx setup verify` runs every check with the
node included.

## 7. Keep it current: `noctyx versions`

```bash
sudo noctyx versions                 # every machine
sudo noctyx versions --host gpu-01   # one machine
sudo noctyx versions --refresh       # ask GitHub for the latest releases now (otherwise cached a day)
```

```text
gpu-01
  ✓ restic         0.19.1         pin 0.19.1    latest 0.19.1    current
  ↑ node_exporter  1.7.0          pin —         latest 1.12.1    1.12.1 released (apt manages it)
  ✓ agent          25d3e6609bb7   pin —         latest —         current

to upgrade (apt takes what the distro carries, which can lag the upstream release):
  gpu-01       node_exporter  sudo apt-get install --only-upgrade prometheus-node-exporter   (on gpu-01)
```

For every critical component on every machine (Noctyx itself, restic, rest-server, Prometheus, node_exporter and
the agent, which is compared by hash with the one this Noctyx would install) it shows the installed version,
Noctyx's pin, the latest upstream release, where the binary came from, and the command that upgrades it on that
machine. Noctyx's pin applies to what Noctyx installed itself in `/usr/local/bin`; a binary apt or Homebrew manages
is compared with upstream and upgraded by its package manager. A Mac runs nothing Noctyx installs, so it has no
rows.

The nightly run refreshes it. The console exports `noctyx_component_behind` and
`noctyx_component_upgrade_available` per host and component (with the upgrade command as a label), the alert rules
`ComponentBehind`, `ComponentUpgradeAvailable` and `VersionsUnknown` (not checked for three days) notify on them,
and each machine report has a **Components** table.

## macOS specifics

| | |
|---|---|
| Services | launchd daemons `/Library/LaunchDaemons/<launchd_prefix>.<name>.plist` (`dev.noctyx.console`, `dev.noctyx.rest-server`, …), made from the same unit templates as on Linux: the same command, user, environment, restart policy and schedule |
| Logs | `/var/log/noctyx/<label>.log`, e.g. `sudo tail -50 /var/log/noctyx/dev.noctyx.rest-server.log` |
| Control | `sudo launchctl print system/dev.noctyx.console` (state, pid, last exit code); `sudo launchctl kickstart -k system/dev.noctyx.console` restarts it |
| Repository owner | a hidden system user made with `dscl` (no login shell; uid below 500, or `server.repo_owner_uid`) |
| Detached jobs | their own session, the pid in `<job_logs>/<job>.log.pid`, instead of `systemd-run` |
| Backups | none: Time Machine backs the Mac up; `setup verify` and the machine report show its destination and last backup |

### Full Disk Access: `noctyx setup privacy`

macOS protects network shares, external disks and people's own folders from background processes (TCC). A launchd
daemon cannot ask for access; without it, every read of an NFS or SMB share, or of a disk under `/Volumes`, fails
with "operation not permitted". When the repositories are off the Mac's own data volume, two binaries need
**Full Disk Access**:

| Binary | Why |
|---|---|
| `/usr/local/bin/rest-server` | the endpoint reads and writes the repositories |
| the real Python executable behind `/opt/noctyx`, e.g. `/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14` | the scheduler, retention, mirror and console, and the restic they run |

Only a person at the Mac can grant it: System Settings › Privacy & Security › Full Disk Access › +, then switch each
on (⌘⇧G in the file picker takes a path). `sudo noctyx setup privacy` prints the exact paths, opens that pane on the
Mac's screen, shows both binaries in Finder, waits, restarts the services, and proves access from the context that
matters: an authenticated read through the endpoint, and a throwaway launchd job reading the share with Noctyx's
Python. A check from Terminal or ssh would prove nothing about the daemons.

A Homebrew update of Python changes the path in the Cellar, and the grant no longer applies: `setup verify` reports
it ("full disk access") and `setup privacy` shows the new path.

### The CA and the System keychain

`setup tls` does not add the site CA to the System keychain: macOS takes a trust setting there only from someone at
the screen, and unattended it adds the certificate untrusted. Noctyx passes the CA to every client explicitly, so
it does not need it. For browsers and curl on the Mac, run once in Terminal:

```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain /etc/noctyx/tls/ca.crt
```

## The directory service

`noctyx-directory` gives recovery media and the orchestrator a read-only, unauthenticated view of the site: it
never serves keys. Its endpoints are `/health`, `/api/v1/hosts`, `/api/v1/ca`, `/api/v1/probe?bytes=N` (the
throughput probe every backup runs first), `/api/v1/restore-script` and `/api/v1/snapshots/<host>`. It also
answers UDP discovery broadcasts on `discovery_udp_port`. Regenerate the inventory it serves after you change
hosts:

```bash
sudo noctyx directory --regen
curl --cacert /etc/noctyx/tls/ca.crt https://192.0.2.10:20999/health
```

## Ports

| Port | |
|---|---|
| 20996 TCP | the console (`server.ui_port`) |
| 20998 TCP | the repository endpoint (`server.rest_port`) |
| 20999 TCP | the directory service (`server.directory_port`) |
| 20997 UDP | restore discovery (`server.discovery_udp_port`) |
| 20995 TCP | the HF mirror (`hf_mirror.port`) |
| 20994 TCP, loopback | the head's own node_exporter (`setup metrics`) |
| 9090 TCP, loopback | Prometheus (`setup metrics`; `NOCTYX_PROM` points the console elsewhere) |
| 20993 TCP, loopback | the demo |
| 20992 TCP | the setup page, only while `noctyx init --web` runs |

## Optional: more metrics, alerting, recovery media

Each of these is a script rendered for your site and run as root with `noctyx tool NAME`:

| Tool | Does | Platform |
|---|---|---|
| `install-node-metrics [--host NAME]` | node_exporter with TLS and basic auth on each Linux node, a GPU/Docker textfile collector, and Prometheus file_sd targets; with the Prometheus of `setup metrics` it only reloads it (the job is already there) | Linux nodes, from a Linux or macOS head |
| `install-mac-metrics NAME` | a Mac (host with `os: macos`) on the dashboard: node_exporter and the powermetrics collector | a Mac monitored from another head (a Mac head's own numbers come from `setup metrics`) |
| `install-alerting` | Alertmanager, Noctyx rules and the console webhook | Linux head |
| `install-llm-monitoring` | the `llm-discover` timer and a Prometheus `llm` job | Linux head (`setup metrics` does both, on either platform) |
| `build-stick`, `build-stick-single`, `make-restore-usb`, `vm-boot-test`, `rehearse-head` | recovery media and rehearsals | Linux |

## Demo mode

```bash
noctyx demo               # http://127.0.0.1:20993
noctyx demo --port 8080
```

The demo runs the console on bundled sample data (a scrubbed real site), over plain HTTP on loopback only, with
sign-in off. It is read-only: every POST is refused. Its state lives in a temporary directory, and it never reads
or writes the real site's config, state, audit log or token.
