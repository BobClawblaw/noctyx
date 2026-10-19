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

**NAS** (backup target): reachable over ssh as `server.nas_ssh.user`. The repository endpoint is restic's
[rest-server](https://github.com/restic/rest-server), which `noctyx nas-rest deploy` copies there (see
[the repository endpoint](#the-repository-endpoint-on-the-nas)).

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

## 3. TLS: CA and server certificate

The console (`:20996`) and the directory service (`:20999`) serve HTTPS with `server.tls.cert` and
`server.tls.key`. Clients trust `server.tls.ca`. `nas-rest deploy` and `noctyx tool install-node-metrics` sign
their certificates with the CA key at `<tls>/ca.key` (`tls` in [paths](site-config.md#paths), default
`/etc/noctyx/tls`).

Noctyx does not create the CA itself. Create it once with openssl. The example uses head address `192.0.2.10`
and name `head-01`:

```bash
cd /etc/noctyx/tls     # sudo mkdir -p -m 700 /etc/noctyx/tls first
sudo openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/O=Noctyx/CN=Noctyx CA" \
  -keyout ca.key -out ca.crt
sudo openssl req -new -newkey rsa:2048 -nodes -subj "/O=Noctyx/CN=head-01" -keyout server.key -out server.csr
printf 'subjectAltName=IP:192.0.2.10,IP:127.0.0.1,DNS:head-01\nextendedKeyUsage=serverAuth\n' | sudo tee server.ext
sudo openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 \
  -extfile server.ext -out server.crt
sudo chmod 600 ca.key server.key
```

The certificate includes `IP:127.0.0.1` because the Prometheus job that `install-alerting` adds scrapes the
console on loopback.

The directory service runs as the repository owner (`repo_owner`, default `noctyx`), so that user must be able to
read `server.key`. For example: `sudo chgrp noctyx server.key && sudo chmod 640 server.key`.

To change the console port, set `server.ui_port` (default `20996`) and run `install-head --apply` again. The
`NOCTYX_CONSOLE_PORT` environment variable overrides it for a one-off run.

## 4. The repository owner and the NAS mount

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin noctyx   # = paths.repo_owner
sudo mkdir -p /mnt/nas && sudo mount /mnt/nas                              # your spindle's mount
```

Repositories live at `<server.repo_root>/<repo_dir_prefix><repo>/<repo>` (prefix default `nx-`). Anything that
writes to a live repository (restic on the head, the HF mirror service, the directory service) runs as
`repo_owner`.

## 5. Create a repository

Each entry in `repositories` needs these four things. Noctyx reads them but does not create them:

| What | Where |
|---|---|
| restic password | `<repo_keys>/<repo>.key` (default `/etc/noctyx/repo-keys/`), root-only |
| endpoint password | `<repo_creds>/<repo_dir_prefix><repo>.pass` (default `/etc/noctyx/repo-creds/nx-<repo>.pass`) |
| endpoint login | a line for user `<repo_dir_prefix><repo>` in `<etc>/htpasswd` (default `/etc/noctyx/htpasswd`) |
| the repository | `restic init` at `<repo_root>/<repo_dir_prefix><repo>/<repo>`, owned by `repo_owner` |

For a repository named `gpu-01`, run this as root:

```bash
install -d -m 700 /etc/noctyx/repo-keys /etc/noctyx/repo-creds
openssl rand -hex 32 > /etc/noctyx/repo-keys/gpu-01.key
openssl rand -hex 24 > /etc/noctyx/repo-creds/nx-gpu-01.pass
chmod 600 /etc/noctyx/repo-keys/gpu-01.key /etc/noctyx/repo-creds/nx-gpu-01.pass
htpasswd -B -i /etc/noctyx/htpasswd nx-gpu-01 < /etc/noctyx/repo-creds/nx-gpu-01.pass   # -c the first time
install -d -o noctyx -g noctyx /mnt/nas/backups/nx-gpu-01
setpriv --reuid=noctyx --regid=noctyx --clear-groups \
  restic -r /mnt/nas/backups/nx-gpu-01/gpu-01 --password-file /dev/stdin init < /etc/noctyx/repo-keys/gpu-01.key
```

The root shell opens the key file and passes it on stdin, so the repository files are owned by `noctyx` and the
key stays root-only.

## The repository endpoint on the NAS

Nodes back up over HTTPS to rest-server at `https://<server.rest_host>:<server.rest_port>/<user>/<repo>`.
rest-server runs with `--private-repos`, so each login sees only its own repository. When `rest_host` is set and
differs from `server.address`, the endpoint runs on the NAS:

```bash
sudo noctyx nas-rest deploy    # cert for the NAS from the CA, binary, htpasswd -> ~/<nas_ssh.dir> on the NAS; restarts it
sudo noctyx nas-rest status    # start | stop | restart | status
```

`deploy` copies `/usr/local/bin/rest-server` from the head, so install that binary on the head first. Every
`tick` and every backup checks that the endpoint is up and restarts it over ssh if it is down.

> **Known gap.** `deploy` also uploads a start script, `run.sh`, from `server/nas-rest-server.sh`, and that file
> is not shipped in the package yet. Until it is, put your own `run.sh` (`start|stop|restart|status`, running
> rest-server with `--private-repos --tls` on `rest_port` against the repository root) in `~/<nas_ssh.dir>/` on
> the NAS. Nothing in Noctyx starts an endpoint on the head itself, so if `rest_host` is empty you have to run
> rest-server there yourself.

## 6. Install the head node's units

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

## 7. Add the nodes

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

## 8. First backup

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

## 9. Verify

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
