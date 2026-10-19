# Installing Noctyx

This page covers a new installation from start to finish. You install the head node (orchestrator, console, directory
service, HF mirror and timers), set up the TLS material and the repository endpoint on the NAS, create a
repository, add the backup agent to each node and take the first backup. If you only want to look around first,
run the [demo](#demo-mode). It needs no configuration and changes nothing.

Related pages: [site config reference](site-config.md) · [accounts](accounts.md) · [console tour](console.md) ·
[upgrading](upgrade.md).

## Requirements

**Head node** (where the orchestrator and the console run):

| Need | Why |
|---|---|
| Linux with systemd | Noctyx installs its services as systemd units, and long jobs run as transient systemd units (`systemd-run`). |
| Python ≥ 3.11, standard library only | `pyproject.toml`: `requires-python = ">=3.11"`, `dependencies = []` |
| `restic` | The head runs restic against the mounted repositories for prune, snapshot listings and the console's snapshot browser. The recovery tooling pins `restic_version` (default `0.19.1`). |
| `openssl`, `ssh`, `setpriv` | Certificates for the NAS endpoint and metrics, ssh to nodes, and writing repository files as the repository owner. |
| The NAS share mounted | At the `mount` of the spindle that holds `server.repo_root` (see [spindles](site-config.md#spindles)). |
| A root ssh key | `server.ssh.key` (default `/root/.ssh/id_ed25519`), authorised on every node. |
| Prometheus on `127.0.0.1:9090` (optional) | Live metrics in the console. Override the address with the `NOCTYX_PROM` environment variable. Alertmanager is expected on `127.0.0.1:9093` (`NOCTYX_ALERTMANAGER`). |

**Nodes** (backup clients):

- reachable over ssh as `server.ssh.user` (or the host's `ssh_user`), with passwordless `sudo -n`
- Debian/Ubuntu style: `install-agent` installs restic with `apt-get` if it is missing and trusts the CA with
  `update-ca-certificates`
- systemd and `curl`. Before every backup the orchestrator measures the path with curl.

**NAS** (backup target): a share the head mounts. The repository endpoint is restic's
[rest-server](https://github.com/restic/rest-server), on the head (`noctyx setup rest-server`) or on the NAS
over ssh as `server.nas_ssh.user` (`noctyx nas-rest deploy`, see [the endpoint on the NAS](#the-endpoint-on-the-nas)).

## 1. Install the package

From a checkout, into its own virtual environment. The systemd units run the Python interpreter that ran
`install-head`.

```bash
git clone https://github.com/BobClawblaw/noctyx.git /opt/noctyx-src
sudo python3 -m venv /opt/noctyx
sudo /opt/noctyx/bin/pip install /opt/noctyx-src
sudo ln -s /opt/noctyx/bin/noctyx /usr/local/bin/noctyx
noctyx --version
```

`noctyx` with no arguments prints the banner and the command list. `noctyx -h` lists every orchestrator command.

## 2. Write the site config

```bash
sudo mkdir -p /etc/noctyx
sudo cp /opt/noctyx-src/examples/site.example.json /etc/noctyx/site.json
sudo chmod 600 /etc/noctyx/site.json
sudoedit /etc/noctyx/site.json
```

The default path is `/etc/noctyx/site.json`. Set `NOCTYX_CONFIG` to use another one. Fill in at least `server`,
`spindles`, `repositories`, `hosts`, `profiles`, `schedule`, `retention` and `network`. [The site config reference](site-config.md) describes every key.

Check the paths Noctyx will use:

```bash
noctyx paths
```

## 3. Set up the head: `noctyx setup`

`noctyx setup` with no step shows where the site is and the next command to run. Every step can be re-run
safely: it keeps what already exists.

```bash
sudo mkdir -p /mnt/nas && sudo mount /mnt/nas    # your spindle's mount, holding server.repo_root
sudo noctyx setup              # the checklist
sudo noctyx setup owner        # the repository owner (paths.repo_owner, default noctyx) and /etc/noctyx
sudo noctyx setup tls          # the site CA and the head's certificate
sudo noctyx setup repos        # every entry in "repositories": key, endpoint login, restic init
sudo noctyx setup escrow       # an encrypted bundle of every key; copy it OFF this machine
sudo noctyx setup rest-server  # the endpoint on the head; with an endpoint on the NAS: noctyx nas-rest deploy
sudo noctyx setup              # all ✓ but "units": next is install-head (step 4)
```

```text
  ✓ owner     the repository owner noctyx
  ✓ tls       the site CA and the head's certificate
  ✓ repos     repositories (2/2 made)
  ✓ escrow    an escrow bundle of the keys (newest noctyx-keys-20261019.tar.gz.enc)
  ✓ endpoint  the repository endpoint https://192.0.2.10:20998
  · units     the head's services (console, directory, mirror, timers)

next: sudo noctyx install-head --apply
```

Before you start, install `restic`, `openssl` and `htpasswd` (`apt install restic openssl apache2-utils`), and
put restic's [rest-server](https://github.com/restic/rest-server/releases) binary at
`/usr/local/bin/rest-server`. The release asset names use underscores: `rest-server_<version>_linux_amd64`.

### What each step does

| Step | Makes | Notes |
|---|---|---|
| `owner` | the system user `repo_owner`; `<etc>` (0750, group `repo_owner`), `<repo_keys>` and `<repo_creds>` (0700), `<state>` | `server.repo_owner_uid` pins its uid. A NAS's NFS share often refuses unknown uids even as the owner, so match the NAS user's uid. |
| `tls` | `<tls>/ca.key` + `server.tls.ca` (a 10-year CA), `server.tls.cert` + `server.tls.key` (825 days) | The certificate names `server.address`, `server.hostname`, `localhost` and `127.0.0.1`, plus anything in `server.tls.san` (e.g. `["DNS:head-01.lan"]`). The key is readable by `repo_owner` (the endpoint and the directory run as it). The CA is added to the head's trust store. Re-running renews a certificate with under 30 days left and **keeps the CA**. `--force` replaces the CA, and then every node must be re-installed. |
| `repos` | per repository: `<repo_keys>/<repo>.key` (32 random bytes), `<repo_creds>/<prefix><repo>.pass` (hex), a bcrypt line for `<prefix><repo>` in `<etc>/htpasswd`, and `restic init` at `<repo_root>/<prefix><repo>/<repo>` | `restic init` runs as `repo_owner`, because an NFS share usually squashes root. `repo_root` must be writable by that user. |
| `escrow` | `<etc>/escrow/noctyx-keys-<date>.tar.gz.enc`: AES-256 (PBKDF2, 200 000 rounds) over the keys, logins, TLS and htpasswd | The passphrase is asked twice and stored nowhere. **Without the keys every backup is unreadable.** Keep the bundle off this machine and off the NAS, and check that it opens somewhere else. The checklist flags keys made after the newest bundle. |
| `rest-server` | `<unit_prefix>-rest-server.service` running rest-server as `repo_owner` | `--private-repos` (each login reaches only its own repository) and `--append-only` (a node can add snapshots, never delete them; pruning runs on the head). |

### The endpoint on the NAS

When `server.rest_host` is set and differs from `server.address`, the endpoint runs on the NAS instead.
Set `server.nas_ssh.repo_root` to the NAS's own path to the same directory as `repo_root`
(e.g. `/volume1/backups/noctyx`), install the rest-server build for the NAS's CPU at
`/usr/local/bin/rest-server` on the head, then:

```bash
sudo noctyx nas-rest deploy    # a certificate for the NAS from the CA, the binary, run.sh, htpasswd -> ~/<nas_ssh.dir>
sudo noctyx nas-rest status    # start | stop | restart | status
```

Every `tick` and every backup checks the endpoint and restarts it over ssh if it is down, so it comes back
after a NAS reboot without a boot task.

### Ports

To change the console port, set `server.ui_port` (default `20996`) and run `install-head --apply` again. The
`NOCTYX_CONSOLE_PORT` environment variable overrides it for a one-off run.

## 4. Install the head node's units

```bash
sudo noctyx install-head          # dry run: prints every unit, writes nothing
sudo noctyx install-head --apply  # writes /etc/systemd/system/<unit_prefix>-*, daemon-reload, enable --now
```

| Unit (prefix `noctyx`) | What it runs |
|---|---|
| `noctyx-console.service` | the web console on `server.ui_port`, as root |
| `noctyx-directory.service` | the directory and discovery service (`directory_port` TCP, `discovery_udp_port` UDP), as `repo_owner` |
| `noctyx-hf-mirror.service` | the Hugging Face mirror on `hf_mirror.port`, as `repo_owner` |
| `noctyx-tick.timer` | `noctyx tick` every 15 minutes: runs the backups that are due |
| `noctyx-nightly.timer` | 07:30 daily: HF harvest and recipe discovery. On Sundays also a prune dry run and a verify. |
| `noctyx-hygiene.timer` | 06:45 daily: `hygiene --fix auto` |
| `noctyx-retention.timer` | 08:00 on the 1st of each month: `prune --apply`, gated by `restic check` |

The console's sign-in setting comes from `server.console_auth`. See [accounts](accounts.md).

### The directory service

`noctyx-directory` gives recovery media and the orchestrator a read-only, unauthenticated view of the site: it
never serves keys. Its endpoints are `/health`, `/api/v1/hosts`, `/api/v1/ca`, `/api/v1/probe?bytes=N` (the
throughput probe every backup runs first), `/api/v1/restore-script` and `/api/v1/snapshots/<host>`. It also
answers UDP discovery broadcasts on `discovery_udp_port`. Regenerate the inventory it serves after you change
hosts:

```bash
sudo noctyx directory --regen
curl --cacert /etc/noctyx/tls/ca.crt https://192.0.2.10:20999/health
```

## 5. Add the nodes

Authorise the head's root key (`server.ssh.key`) for `server.ssh.user` on each node and give that user
passwordless sudo. Then:

```bash
sudo noctyx install-agent --host gpu-01            # one node
sudo noctyx install-agent                          # every enabled host
sudo noctyx install-agent --host gpu-01 --profile system --with-client-timer
```

For each node, `install-agent`:

1. installs restic if it is missing
2. installs the agent (`agent_bin`, default `/usr/local/bin/noctyx-agent`)
3. installs the CA into `<node_etc>/tls/ca.crt` and the system trust store
4. pipes the repository key, the endpoint password and the repository URL through stdin into root-only files, so
   none of them appear on a command line
5. writes `client.conf`, the exclusions and the profile's paths
6. adds a `noctyx-backup.service`
7. proves the repository opens with a `restic snapshots` unlock test
8. runs `policy-apply` for that host

`--with-client-timer` adds a nightly timer on the node itself. That timer bypasses the head's per-spindle lock.
Leave it off so the head schedules backups.

## 6. First backup

```bash
sudo noctyx hosts                          # what the config says
sudo noctyx probe --host gpu-01            # measured Mbit/s on the real path
sudo noctyx backup --host gpu-01 --dry-run
sudo noctyx backup --host gpu-01           # profile "system" by default
sudo noctyx snapshots --host gpu-01
```

`backup` runs as a detached systemd unit and the command follows its log, so stopping the command does not stop
the job. Add `--foreground` to run it in-process, or `--no-follow` to start it and return. A backup refuses to
start if the probe fails or measures below `network.min_throughput_mbit` (default 40). `--force-low-net`
overrides this. After the first backup, `noctyx tick --dry-run` shows what the schedule will run next.

## 7. Verify

```bash
noctyx status
```

`status` prints the head, its repositories, the number of enabled hosts, any degraded-network note and the last
eight audit lines. Then open `https://<head>:20996` and see [the console tour](console.md). Before anyone else
uses the console, set up [accounts](accounts.md).

## Optional: metrics, alerting, recovery media

Each of these is a script rendered for your site and run as root with `noctyx tool NAME`:

| Tool | Does |
|---|---|
| `install-node-metrics [--host NAME]` | node_exporter with TLS and basic auth on each node, a GPU/Docker textfile collector, and Prometheus file_sd targets |
| `install-mac-metrics NAME` | a Mac (host with `os: macos`, monitored only) |
| `install-alerting` | Alertmanager, Noctyx rules and the console webhook |
| `install-llm-monitoring` | the `llm-discover` timer and a Prometheus `llm` job |
| `build-stick`, `build-stick-single`, `make-restore-usb`, `vm-boot-test`, `rehearse-head` | recovery media and rehearsals |

## Demo mode

```bash
noctyx demo               # http://127.0.0.1:20993
noctyx demo --port 8080
```

The demo runs the console on bundled sample data (a scrubbed real site), over plain HTTP on loopback only, with
sign-in off. It is read-only: every POST is refused. Its state lives in a temporary directory, and it never reads
or writes the real site's config, state, audit log or token.
