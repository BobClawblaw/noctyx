# Site config reference

Everything that varies between sites lives in one JSON file, the site config: hosts, the NAS, schedules,
retention, backup policy, the HF mirror, recipe slots and catalog, the assistant's LLM endpoint, console
defaults and path overrides. Its path is `NOCTYX_CONFIG` (default `/etc/noctyx/site.json`), and
[`examples/site.example.json`](../../examples/site.example.json) is a starting point. This page lists every block
and every key the code reads, with its default. **required** marks a key with no default: the commands that use
it fail without it.

Related pages: [install](install.md) · [accounts](accounts.md) · [console](console.md) · [upgrade](upgrade.md).

**Who writes the file.** Mostly you, by hand. A few commands, and the console actions that call them, rewrite
*one* top-level block in place and leave the rest of the file byte for byte:

| Command | Block |
|---|---|
| `schedule-set` | `schedule` |
| `policy-set` | `backup_policy`, `retention`, `hf_mirror` |
| `assistant-set` | `assistant` |
| `console-set` | `console` |
| `recipe-catalog` | `recipes.catalog` |

A block that is missing is inserted just before `"schedule": {`, so keep a `schedule` block in the file. Every
rewrite is re-parsed before an atomic replace, keeps the file's owner and mode, and is written to the audit log.

## Top level

| Key | Meaning | Default |
|---|---|---|
| `version` | config format version (not read yet) | `1` |
| `server` | the head node, NAS endpoint, ports, ssh, TLS | **required** |
| `spindles` | backup targets (storage devices) by name | `{}` |
| `repositories` | restic repositories | **required** |
| `hosts` | every node | **required** |
| `profiles` | what a backup profile includes | **required** for backups |
| `schedule` | how often each profile runs | none (nothing is scheduled) |
| `retention` | snapshots kept | **required** for `prune` |
| `verification` | `verify` settings | optional |
| `network` | throughput floor and status notes | **required** for `status` and `backup` |
| `backup_policy` | Docker volumes, database dumps, extra excludes | see [backup_policy](#backup_policy) |
| `common_exclusions_extra` | extra lines appended to the baseline exclusion list (e.g. this site's separate mount points) | `[]` |
| `hf_mirror` | Hugging Face mirror | see [hf_mirror](#hf_mirror) |
| `assistant` | the LLM the console asks | off |
| `recipes` | GPU slots and the recipe catalog | none |
| `console` | display defaults for every browser | `{}` |
| `restic_version` | restic release put on recovery sticks, shown in About | `"0.19.1"` |
| `paths` | overrides of file, unit and user names | `{}` (see [paths](#paths)) |

## server

| Key | Meaning | Default |
|---|---|---|
| `hostname` | the head's name (status, directory, About) | **required** |
| `address` | the head's IP. Nodes reach the directory service and mirror here | **required** |
| `ui_port` | console HTTPS port | `20996` |
| `directory_port` | directory service HTTPS port (inventory, throughput probe) | **required** (example `20999`) |
| `discovery_udp_port` | UDP discovery for recovery media | **required** (example `20997`) |
| `rest_port` | repository endpoint (rest-server) port | **required** (example `20998`) |
| `rest_host` | where the endpoint runs. Set and ≠ `address` means the NAS, which `nas-rest` manages | `address` |
| `repo_root` | the directory on the head that holds the repositories (on the NAS mount) | **required** |
| `nas_name` | the NAS's name: its lock key and its TLS name | the spindle whose `mount` holds `repo_root`, else `"nas"` |
| `nas_ssh.user`, `.host`, `.dir` | ssh login to the NAS, and the directory (under its home) for the endpoint | **required** for `nas-rest` |
| `nas_ssh.repo_root` | the NAS's own path to `repo_root` (what the endpoint serves there) | **required** for `nas-rest deploy` |
| `repo_owner_uid` | uid for `noctyx setup owner` to give the repository owner (match the NAS user over NFS) | the next system uid |
| `ssh.user` | ssh user on the nodes (needs passwordless sudo) | **required** |
| `ssh.key` | root's private key used for every ssh | **required** (example `/root/.ssh/id_ed25519`) |
| `tls.cert`, `tls.key` | server certificate and key (console, directory) | **required** |
| `tls.ca` | CA certificate that nodes trust | **required** |
| `tls.san` | extra names for `noctyx setup tls` to put in the certificate, e.g. `["DNS:head-01.lan"]` | `[]` |
| `console_auth` | `false` leaves the console open (see [accounts](accounts.md)) | `true` |
| `hot_root` | a second repository tier, shown in capacity and Storage Tiers | none |

## spindles

`{name: {...}}`. A spindle is one storage target. Its name is the lock that serialises writers, so only one
backup writes to it at a time.

| Key | Meaning | Default |
|---|---|---|
| `mount` | where it is mounted on the head | **required** |

## repositories

A list of `{ "id": "gpu-01" }`. Several hosts can share one repository (cross-host dedup); each host names its
repository in `hosts[].repo`. The example's `spindle` key is not read. A host's spindle comes from
`hosts[].spindle`.

## hosts

| Key | Meaning | Default |
|---|---|---|
| `name` | host name used everywhere | **required** |
| `address` | IP or DNS name used for ssh and scraping | **required** for backups |
| `arch` | `x86_64` / `aarch64` | **required** |
| `repo` | repository id. Without one the host is monitored only | none |
| `os` | `linux`, or `macos` (monitored only, no backup policy) | `linux` |
| `enabled` | `false` hides the host from everything | `true` |
| `backup` | `false` monitors without backing up | `true` |
| `spindle` | spindle lock its backups take | `nas_name` |
| `ssh_user` | overrides `server.ssh.user` for this host | `server.ssh.user` |
| `boot`, `root_disk`, `lvm`, `vg`, `lv`, `restore_template` | bare-metal restore facts written to the directory inventory | none |
| `used_gb` | shown by `noctyx hosts` | none |

## profiles

`{name: {"paths": [...]}}`. `install-agent --profile NAME` writes the profile's `paths` to the node. The default
profile is `system` (`["/"]`). A profile can ship its own exclusion list as `<etc>/exclusions-<profile>.conf`,
which then replaces the common one. The example's `description` and `exclusions` keys are not read.

## schedule

| Key | Meaning | Default |
|---|---|---|
| `tick_minutes` | grace period when judging "late". The timer itself fires every 15 minutes | `15` |
| `retry_after` | wait after a failed attempt | `"1h"` |
| `defaults` | `{profile: {"every": "24h", "window": "01:00-07:00"}}` for every host | `{}` |
| `hosts` | `{host: {profile: {...} or null}}`: a host's overrides. `null` disables that profile there | `{}` |

Durations take `s`, `m`, `h`, `d` and `w`. A window is `HH:MM-HH:MM` (it may wrap midnight) or absent for any
time. `tick` compares it with the head's **local** clock. Edit the block with `schedule-set` (`--every` 15m..90d):

```bash
sudo noctyx schedule-set --profile system --every 1d --window 01:00-07:00   # fleet default
sudo noctyx schedule-set --profile system --host gpu-02 --every 6h          # one host
sudo noctyx schedule-set --profile system --host gpu-02 --reset             # back to the default
sudo noctyx schedule-set --profile system --host gpu-02 --disable
```

## retention

| Key | Meaning | Default |
|---|---|---|
| `daily`, `weekly`, `monthly` | `restic forget --keep-*` per host and tags, each 0..999 (not all 0) | **required** |
| `keep_2nd_last_monthly` | noted but not enforced (restic has no equivalent) | `false` |

`noctyx prune` is a dry run. `prune --apply` (the monthly timer) runs check → forget → prune → check.

## verification

| Key | Meaning | Default |
|---|---|---|
| `read_data_subset` | the share of pack data `noctyx verify` has each host's agent read back (`--subset` overrides) | `"2%"` |

## network

| Key | Meaning | Default |
|---|---|---|
| `min_throughput_mbit` | a backup refuses to start below this measured rate | `40` |
| `degraded`, `degraded_reason` | a note `status` prints | none |
| `expected_client_to_server_mbit`, `measured_client_to_server_mbit` | printed alongside it | none |


## backup_policy

What each backup covers. A host override in `hosts` replaces the boolean values and adds to the lists. Edit it on
the console's Configuration page or with `policy-set`, then push it with `policy-apply`.

| Key | Meaning | Default |
|---|---|---|
| `docker_volumes` | back up `/var/lib/docker/volumes` (image layers stay excluded) | `true` |
| `db_dumps` | dump discovered databases before each `system` backup | `true` |
| `exclude` | extra restic patterns. `!pattern` re-includes | `[]` |
| `skip_dumps` | discovered database ids not to dump | `[]` |
| `sqlite` | extra SQLite files (absolute paths) to copy safely | `[]` |
| `hosts` | `{host: {any of the above}}` | `{}` |

```bash
sudo noctyx policy-show --host gpu-01      # the files policy-apply would write
sudo noctyx policy-set --json '{"policy": {"db_dumps": true, "exclude": ["/srv/scratch/**"]}}'
sudo noctyx policy-apply                   # [--host NAME] [--no-scan]
```

## hf_mirror

| Key | Meaning | Default |
|---|---|---|
| `store` | model store (put it on the NAS). The server serves `<store>/hub` | `/srv/noctyx/hf-mirror` |
| `port` | mirror HTTP port (`HF_ENDPOINT=http://<head>:<port>`) | `20995` |
| `harvest` | the nightly harvest copies new model files in from the hosts | `true` |
| `scan_roots` | where to look for HF caches on Linux hosts | `["/"]` |
| `exclude_hosts` | hosts not harvested | `[]` |
| `bwlimit_mb` | MB/s per copy, 0 = unlimited (0..10000) | `0` |
| `skip` | extra paths never scanned (the NAS mount is always skipped) | `[]` |

## assistant

The OpenAI-compatible endpoint the console asks to explain hygiene findings, triage alerts and write the
morning brief. It only produces text and never runs anything.

| Key | Meaning | Default |
|---|---|---|
| `enabled` | use the assistant | off when absent (the console reads a missing key as off) |
| `url` | base URL ending in `/v1` | none |
| `model` | served model id. Empty = the endpoint's first | `""` |
| `timeout_seconds` | 5..600 | `60` |
| `fallback` | when `url` does not answer, ask any LLM server `llm-discover` found | `true` |
| `key_host` | name of a key file in `llm_keys`. Set the key on the console, never here | none |

```bash
sudo noctyx assistant-set --json '{"enabled": true, "url": "http://192.0.2.21:8000/v1", "timeout_seconds": 60}'
```

## recipes

| Key | Meaning | Default |
|---|---|---|
| `root` | checkout directory on a slot's head (`~` = the slot user's home) | `paths.recipe_root` |
| `hf_mirror` | give deployments `HF_ENDPOINT` pointing at the mirror | `true` |
| `harness_dir` | a recipe-harness checkout whose `stats/` rank discovered recipes by measured speed | `/srv/recipe-harness` |
| `slots` | `{slot: {...}}`, below | `{}` |
| `catalog` | `{name: {...}}`, below | `{}` |

**`slots.<name>`.** A slot is one GPU box, or the head plus worker of a tensor-parallel (TP) pair.

| Key | Meaning | Default |
|---|---|---|
| `head` | host that runs the deployment unit | **required** |
| `worker` | second host of a TP pair | none |
| `port` | port the model serves on | **required** |
| `lock` | GPU lock file on the head | `paths.gpu_lock` |
| `health_timeout_s` | how long a new deployment has to answer `/health` and `/v1/models` | `1800` (the unit's start timeout defaults to `3600`) |
| `settle_s` | pause between stop and start | `5` |
| `gpu_idle_check` | after stopping, wait until `nvidia-smi` shows no compute process | `false` |
| `conflicts` | other systemd units stopped before a switch | `[]` |
| `adopt` | `{"unit": ..., "name": ...}`: the service that served before Noctyx, treated as the current deployment | none |
| `env` | environment for every deployment | `{}` |
| `script_env` | `.env` values for `script` recipes | `{}` |
| `cluster` | `[cluster]` values written into a `spark` recipe's `cluster.toml` (plus `port`) | `{}` |

**`catalog.<name>`** (name: lowercase, digits, `.`, `-`, up to 48 characters):

| Key | Meaning | Default |
|---|---|---|
| `repo` | `https://` or `ssh://` git URL | **required** |
| `kind` | `spark` (`python3 -m spark up/down`), `systemd` (`setup.sh` + `serve.sh`), `script` (`start.sh` + `stop.sh`) | **required** |
| `ref` | branch, tag or commit | `"main"` |
| `slots` | slots it may run on | **required** |
| `env` | `UPPER_CASE` environment | `{}` |
| `overrides` | extra `cluster.toml` sections (`spark`) | `{}` |

```bash
sudo noctyx recipe-catalog --name my-model --json '{"repo": "https://github.com/example/recipe.git", "kind": "systemd", "slots": ["gpu"]}'
noctyx recipe-status
sudo noctyx recipe-deploy --slot gpu --recipe my-model           # prints the plan; --yes does it
```

## console

```json
"console": {"settings": {"mode": "dark", "theme": "noctyx", "tunit": "C"}}
```

These are the display defaults for every browser that has no settings of its own:

| Key | Values |
|---|---|
| `mode` | `dark`, `light`, `system` |
| `theme` | `noctyx`, `ember`, `mono`, `github`, `catppuccin`, `custom` |
| `tunit` | `F` or `C` |
| `custom` | `#rrggbb` for any of `bg`, `panel`, `text`, `muted`, `accent`, `line`, `ok`, `warn`, `bad` |

Set them with `sudo noctyx console-set --json '{"theme": "ember", "mode": "system", "tunit": "C"}'` or the
console's **save as site default** button. Each save replaces the whole `settings` object. See
[the console tour](console.md#display-settings).

## paths

Every file, directory, unit and user name Noctyx touches has a default in
[`src/noctyx/paths.py`](../../src/noctyx/paths.py). The `paths` block overrides any of them. Unknown keys are
ignored. A `NOCTYX_PATH_<NAME>` environment variable overrides both (`NOCTYX_PATH_UNIT_PREFIX=...`).

```bash
noctyx paths     # {"config": ..., every effective path}
```

| Key | Default | Used for |
|---|---|---|
| `etc` | `/etc/noctyx` | users.json, session.key, htpasswd, exclusions |
| `state` | `/etc/noctyx/state` | scheduler, recipe and discovery state |
| `locks` | `/var/lock/noctyx` | spindle and slot locks |
| `job_logs` | `/var/log/noctyx/jobs` | detached jobs' logs |
| `audit` | `/var/lib/noctyx/audit.jsonl` | audit log |
| `console_state` | `/var/lib/noctyx-console` | console caches, per-user settings |
| `tls` | `/etc/noctyx/tls` | `ca.crt`, `ca.key` |
| `repo_keys` | `/etc/noctyx/repo-keys` | `<repo>.key` |
| `repo_creds` | `/etc/noctyx/repo-creds` | `<prefix><repo>.pass` |
| `llm_keys` | `/etc/noctyx/llm-keys` | assistant keys |
| `ui_token` | `/etc/noctyx/ui-token` | console bearer token |
| `nas_rest` | `/etc/noctyx/nas-rest` | the NAS endpoint's TLS material |
| `alert_env` | `/etc/noctyx/alert-channel.env` | optional external alert channel |
| `unit_prefix` | `noctyx` | head units and job units |
| `recipe_unit_prefix` | `noctyx-recipe` | `<prefix>-<slot>.service` |
| `repo_owner` | `noctyx` | user owning repository files |
| `repo_dir_prefix` | `nx-` | `<repo_root>/<prefix><repo>/<repo>`, and the endpoint login |
| `recipe_root` | `~/.local/share/noctyx/recipes` | recipe checkouts |
| `gpu_lock` | `/var/lock/noctyx-gpu.lock` | a slot's default GPU lock |
| `mirror_unit` | `noctyx-hf-mirror` | the mirror's service name |
| `restore_script` | `""` (the packaged one) | what `/api/v1/restore-script` serves |
| `iso_dir`, `vmtest_dir`, `rehearsal_dir` | `/srv/noctyx/isos`, `/srv/noctyx/vmtest`, `/var/tmp/noctyx-rehearsal` | recovery media and rehearsals |
| `stick_label`, `live_label` | `NOCTYX`, `NOCTYXLIVE` | recovery stick partitions |
| `prom_node_targets`, `prom_llm_targets` | `/etc/prometheus/noctyx-targets.json`, `/etc/prometheus/llm-targets.json` | Prometheus file_sd |
| `prom_job_console`, `prom_job_nodes` | `noctyx`, `noctyx-nodes` | Prometheus job names |
| `textfile_name`, `textfile_unit` | `noctyx.prom`, `noctyx-node-textfile` | node collector |
| `launchd_prefix` | `dev.noctyx` | launchd labels on Macs |
| `agent_bin`, `dumps_bin` | `/usr/local/bin/noctyx-agent`, `/usr/local/bin/noctyx-dumps` | on every node |
| `node_etc`, `node_lib` | `/etc/noctyx`, `/var/lib/noctyx` | on every node |
| `result_token` | `NOCTYX_RESULT` | the agent's result line |
| `snapshot_tag` | `noctyx` | tag on every snapshot |
| `agent_unit` | `noctyx-backup` | node-local unit |

[Upgrading](upgrade.md#migrating-from-an-earlier-layout) explains how to use this block to adopt an existing
layout.
