# Hugging Face mirror

The mirror keeps one deduplicated copy of every Hugging Face model the cluster has downloaded, on the NAS, and
serves it as a Hugging Face endpoint on the LAN. Clients set `HF_ENDPOINT` to the mirror. A file the store has is
served locally, anything else passes through to huggingface.co, and when huggingface.co is unreachable the stored
copy is served. `noctyx hf-harvest` fills the store by copying model files out of the hosts' own Hugging Face
caches. Every file is checked against its content hash, so the same model on five hosts is stored once.

## How it is served

The mirror is `noctyx mirror`, a small stdlib HTTP server. `noctyx setup units` (or `noctyx install-head --apply`)
installs it as `noctyx-hf-mirror.service` on a Linux head and as the launchd daemon `dev.noctyx.hf-mirror` on a
macOS head. It runs as the `repo_owner` user (default `noctyx`) and has read-only access to the store.

On a macOS head whose store is on a NAS share or an external disk, macOS refuses the daemon that volume until
Noctyx's Python has Full Disk Access: `sudo noctyx setup privacy` names the binary and checks the grant. The
harvest (below) has so far been run from Linux heads only: it uses `ssh` and `rsync` from the head to each
host.

| Setting | Source | Default |
|---|---|---|
| Port | `hf_mirror.port` (the unit sets `NOCTYX_HF_PORT`) | `20995` |
| Store | `hf_mirror.store`. The mirror serves `<store>/hub` (the unit sets `NOCTYX_HF_STORE`). | `/srv/noctyx/hf-mirror` |
| Upstream | `NOCTYX_HF_UPSTREAM` | `huggingface.co` |
| Request log | `NOCTYX_HF_DEBUG=1` prints every request to stderr | off |

It listens on every interface over plain HTTP, and it has no authentication of its own. Anyone who can reach the
port can read every stored file, including files of gated models that a host downloaded with its own token. Keep
the port on a trusted network.

### Using it from a client

```sh
export HF_ENDPOINT=http://head-01:20995
huggingface-cli download example/model-27b
```

In a container, pass `-e HF_ENDPOINT=http://head-01:20995`. [Recipe deploys](recipes.md) set it by themselves. The
console's Config page shows the exact value under **Model mirror**.

Pass a token as usual (`HF_TOKEN`, or `huggingface-cli login`) for gated repos. The mirror forwards it to
huggingface.co.

### What it answers

| Request | Behavior |
|---|---|
| `/{repo}/resolve/{rev}/{path}` (also under `datasets/` and `spaces/`) | A branch name is first resolved to a commit at huggingface.co (cached 5 min, failures 1 min), so `main` still means the current `main`. Offline, the harvested ref is used. The file is served from the store if it was harvested at that commit. If not, the mirror asks huggingface.co for the file's hash and serves the stored blob when it has those exact bytes. Otherwise the request passes through. Range requests work. |
| `/api/{models,datasets,spaces}/{repo}`, `…/revision/{rev}`, `…/tree/{rev}[/path]` | Proxied. If huggingface.co is unreachable, returns a 5xx, or answers 401/403/404 (offline, gated without a token, deleted), and the store has the repo, the mirror answers from the store. |
| Anything else | Streamed through to huggingface.co unchanged. Redirects to its CDN are passed on. |
| `/` or `/cm-status` | JSON: store path, request counters, store totals, time of the last harvest |
| `/metrics` | Prometheus: `noctyx_hfmirror_requests_total{result=hit\|miss\|fallback\|errors}`, `noctyx_hfmirror_bytes_total{result=hit\|miss}` (prefix: `paths.metric_prefix`) |

Every response carries an `X-CM-Mirror` header: `hit` (from the store), `miss` (passed through), `fallback`
(answered from the store because upstream failed) or `none`.

The store is a plain Hugging Face hub cache (`models--org--name/{blobs,snapshots,refs}`). A host that mounts it can
also use it read-only as `HF_HUB_CACHE`.

## Harvesting

```sh
sudo noctyx hf-harvest                        # every source host
sudo noctyx hf-harvest --host gpu-01          # one host (runs even when hf_mirror.harvest is off)
sudo noctyx hf-harvest --repo Qwen            # only repos whose cache dir name contains this
sudo noctyx hf-harvest --ignore-window        # do not wait for the backup window or a busy NAS
```

`hf-harvest` runs as a detached job and streams its log. `--foreground` and `--no-follow` work as for the other
long commands.

For each source host:

1. **Find caches.** `cache/lister.py` runs on the host as root, over ssh (locally on the head). It finds every
   directory that holds `models--*`, `datasets--*` or `spaces--*` repos and lists their refs, snapshots and blobs.
   It only reads.
2. **Copy what is missing.** Blobs the store lacks are copied with `rsync` into a staging folder next to the store,
   limited by `bwlimit_mb` if set. Before each copy, harvest waits while the backup window is open (and from 15
   minutes before it opens) or while another job holds the NAS lock. The window is
   `schedule.defaults.system.window`.
3. **Verify.** Each blob is hashed against its own name: sha256 for LFS files, git blob sha1 for the others. A
   mismatch is dropped and counted as a bad hash.
4. **Link.** Snapshot symlinks are added for each commit. A ref (such as `main`) goes to whichever host fetched it
   most recently.

Progress and the totals go to `<store>/status.json`, which the console and `/cm-status` read. Only one harvest runs
at a time. A second one prints `already running — skipped`. The exit code is 1 if any host failed.

### Source hosts and scan roots

Harvest reads from every host in `hosts` that:

- is enabled and has an `address`,
- does not use the `windows-class` repo,
- and is not listed in `hf_mirror.exclude_hosts`.

| Host OS | Where it looks |
|---|---|
| Linux | Every path in `hf_mirror.scan_roots` (default `/`) |
| macOS | The ssh user's home directory (`~`) |

The scan stays on the filesystem of each root and goes at most 7 levels deep. It skips:

- `/proc`, `/sys`, `/dev`, `/run`, `/snap`, `/tmp`, `/var/lib/docker` and `/var/lib/containerd`;
- macOS system folders (`/System`, `/Library`, `/private`, `/Volumes`);
- `node_modules`, `.git`, `site-packages` and `__pycache__`;
- the NAS mount, meaning the common parent of the store and `server.repo_root`, so harvest never scans its own store;
- every path in `hf_mirror.skip`.

Partial downloads (`*.incomplete`) are ignored.

## Configuration

The `hf_mirror` block of the site config:

```json
"hf_mirror": {
 "store": "/mnt/nas/hf-mirror",
 "port": 20995,
 "harvest": true,
 "scan_roots": ["/"],
 "exclude_hosts": [],
 "bwlimit_mb": 0
}
```

| Key | Meaning | Default |
|---|---|---|
| `store` | Store directory. The hub cache is `<store>/hub`. | `/srv/noctyx/hf-mirror` |
| `port` | The mirror's port. Also the port in the `HF_ENDPOINT` that recipe deploys get. | `20995` |
| `harvest` | Run harvest from `nightly` and from a plain `hf-harvest` | `true` |
| `scan_roots` | Where to look on Linux hosts | `["/"]` |
| `exclude_hosts` | Host names to leave out | `[]` |
| `bwlimit_mb` | Copy bandwidth limit in MB/s, 0..10000 (0 = none) | `0` |
| `skip` | Extra paths the scan never enters | none |

Under **Configuration › Model mirror**, the console shows:

- the server's status, the store's totals and each host's last harvest;
- the `HF_ENDPOINT` value;
- a **harvest now** button (operator);
- the editable settings: nightly harvest on/off, which hosts to harvest from, and the bandwidth limit.

The settings are saved with the backup policy (admin), through `policy-set`. Change `store`, `port`, `scan_roots`
and `skip` in the site config, then re-run `noctyx install-head --apply` so the unit picks up a new store or port.

To keep recipe deploys from getting `HF_ENDPOINT`, set `recipes.hf_mirror` to `false`.

## Nightly integration

`noctyx-nightly.timer` runs `noctyx nightly` every day at 07:30, with up to 15 minutes of random delay, after the
backup window. Its steps are `hf-harvest`, then `recipe-discover`, and on Sundays the retention dry-run and a
verify. Each step runs as its own process, so one failure does not skip the others. With `hf_mirror.harvest` set to
`false`, the harvest step prints that it is off and does nothing.
