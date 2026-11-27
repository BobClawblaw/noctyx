# Noctyx — product plan

**Status:** pre-alpha. This tree is the clean home of the product; the code is being extracted from
a private, single-site implementation that has run a real GPU cluster in production since 2026-09.
**Date:** 2026-10-19 (status updated 2026-11-27)

## Decisions taken

| | |
|---|---|
| Name | **Noctyx** (replaces the working name "ClusterManager", which is descriptive and crowded: NVIDIA's Bright Cluster Manager / Base Command Manager, Adaptive Cluster Manager, IBM Platform Cluster Manager, Gluu Cluster Manager) |
| License | **PolyForm Noncommercial License 1.0.0** (source-available, noncommercial) — 100% free for personal, hobbyist, educational, and public research use |
| Commercial use & support | **No selling Noctyx or paid support.** Nobody is allowed to sell Noctyx, offer paid hosted/managed Noctyx, or sell paid support. Donationware via Bitcoin only |
| Public repo | A fresh repository with no history from the private implementation and no site-specific information (see §9) |

## 1. What Noctyx is

The night-shift operator for a small self-hosted AI cluster: GPU boxes, TP pairs and a NAS, run by
one person or a small team. One console that **deploys** model recipes safely, **caches** every model
locally, **protects** the machines and their data, and **watches** all of it with an LLM that explains
what it sees.

| Pillar | What it does | Built (private) |
|---|---|---|
| **Deploy** | Slots (a GPU box or a TP pair) serve one recipe each. Deploy = build and download while the current model keeps serving, then switch under the slot's GPU lock and roll back automatically if the new one does not come up. Discovery ranks public recipes that fit your hardware by measured speed or recency | yes |
| **Cache** | One deduplicated copy of every Hugging Face model on the cluster, served back as `HF_ENDPOINT`: local first, upstream passthrough, serves the local copy when the Hub is unreachable | yes |
| **Protect** | Nightly restic snapshots of every node, Docker volumes, consistent database dumps (postgres, mariadb, redis, sqlite — discovered, no setup), bare-metal restore images, retention, verification | yes |
| **Watch** | Live fleet console: GPU/CPU/NVMe temperatures, power, tokens/s per model, alerts with LLM triage, a morning brief, host hygiene with one-click fixes | yes |

**Positioning.** Backup tools and GPU cluster managers both exist; neither serves the fast-growing
crowd running community model recipes on DGX Sparks, RTX workstations and Macs. A search on
2026-10-19 found ~80 public recipes for this hardware, the popular ones with hundreds to 1,500 stars,
almost all hand-run shell scripts with no deploy, rollback or health layer. Noctyx is that layer.
Lead with Deploy; Protect is why people trust it with production.

**Who it is for.** Home labs, hobbyists, students, educators, and non-profit / public research — the community that
writes and benchmarks recipes for DGX Sparks, RTX workstations, and Macs. The software is 100% free for all of them.

## 2. Licensing and donations

**What PolyForm Noncommercial License 1.0.0 means** (source-available, noncommercial):

| Use | Allowed? |
|---|---|
| Read, copy, modify, fork, redistribute the source for noncommercial purposes | Yes (must preserve `LICENSE` and `Required Notice:` lines) |
| Personal, hobbyist, home-lab, educational, charitable, and public research use | Yes — 100% free |
| Selling Noctyx, offering paid hosted/managed Noctyx, or selling paid support | **No** — strictly prohibited |

**Donationware only.** There are no paid tiers, no enterprise editions, no paid onboarding, and no paid support
contracts. Voluntary Bitcoin tips are welcome at `bc1q9echf820hapc04y0f636uxdry2ahyx32tkleff`.

**No enforcement.** The software asks nothing of users: no license keys, no DRM, no phone-home, no telemetry, no
audit clause.

**Notices & third-party licenses:**
- Keep `THIRD_PARTY.md` / notices accurate for bundled and pinned third-party tools: restic and rest-server
  (BSD-2-Clause), Prometheus / Alertmanager / node_exporter (Apache-2.0), bundled console fonts (SIL OFL), and the
  open recipe deploy spec (`docs/spec/recipe-deploy.md`, CC-BY-4.0).
- `"Noctyx"` and its logo remain trademarks of their owner; the license grants no rights in the name or logo.

## 3. Where the private implementation stands (measured 2026-10-19)

About 10,000 lines: an orchestrator CLI (≈2,800 lines, Python stdlib), a web console (≈3,900 lines:
Python server with the HTML/CSS/JS embedded as one string), a per-node agent (bash), a database-dump
hook, the HF mirror (server + harvester), metrics exporters, a bare-metal restore image, one test file.

Site specifics to remove (counts are lines in code, not docs):

| What | Lines | Files |
|---|---|---|
| The NAS's name and paths | 65 | 14 |
| One site's data mount paths | 24 | 13 |
| Host names | 23 | 11 |
| One engine's install paths and names | 54 | 5 |
| LAN addresses | 6 | 5 |

And the single-site assumptions behind them: one Synology NAS over NFS as the only backup target
(including a uid workaround), the head node doubling as the UI host, root SSH to every node, UI
authentication off, self-signed TLS.

## 4. Target architecture

```
noctyx/                     Python package (3.11+), stdlib-first; one wheel + a node agent
  config/                   schema, validation, `noctyx init` (discovers hosts, writes site.toml)
  inventory/                hosts, roles, hardware facts, addresses (DHCP/mDNS aware)
  jobs/                     detached jobs (systemd-run), locks, audit log
  protect/                  restic driver, schedule, retention, verify, restore, db dumps
    targets/                plug-ins: nas-nfs, nas-rest, s3/b2, local-disk
  deploy/                   slots, catalog, recipe kinds, switch/rollback, discovery
    kinds/                  plug-ins: spark, systemd (setup.sh+serve.sh), script (start.sh), spec (§5)
  cache/                    HF mirror server + harvester
  watch/                    metrics (Prometheus queries), alerts, hygiene, temperatures
  assistant/                LLM endpoint config, explain / triage / brief
  console/                  HTTP API (JSON) — no HTML in Python
web/                        the console UI as real files (JS/CSS), built into the wheel
agent/                      node agent: install, restic wrapper, pre-hooks, exporters
restore-image/              bare-metal restore stick builder
```

Principles: every site fact comes from `site.toml` (no literals in code); plug-in interfaces for
backup targets and recipe kinds; the console is a client of the JSON API; nothing runs as root that
does not need to (a scoped sudoers file per node instead of root SSH).

**Security defaults for a product:** console login on by default (local accounts; SSO in Fleet),
per-action authorization for anything that stops a model or touches a backup, TLS with a real
certificate path (ACME or a site CA), secrets only in root-only files, never in config or logs.

## 5. The recipe deploy spec — the standard-setting move

Today Noctyx needs an adapter per recipe layout. Publishing a small, open `[deploy]` section for
`recipe.toml` (draft: [spec/recipe-deploy.md](spec/recipe-deploy.md)) lets any recipe author make a
repo one-click deployable: hardware, node count, prepare/start/stop/health commands, model pins, the
settings a site must supply. The spec itself is published under **CC-BY-4.0** (anyone may implement
it — adoption is the point). Then:
- discovery shows "deployable" for every spec'd recipe, with no guessing;
- benchmark leaderboards can link speed results to recipes by repo and commit;
- recipe authors get a reason to add the section: one-click installs for their users.

## 6. Phases

**Phase 0 — Foundations**
- [x] Name, license, this plan, the tree
- [x] Privacy check in CI (`tools/privacy_check.py`)

**Phase 1 — Extract (3–4 weeks). In progress — 2026-10-19:** orchestrator, console, directory service,
HF mirror, node agent + dumps hook, metrics exporters and installers, head-node units and the CLI are
in the package with every site fact moved to the config (`paths`, `common_exclusions_extra`,
`hf_mirror.scan_roots`, `recipes.harness_dir`); the reference cluster's own config runs on it beside the
old tool (console on a trial port, timers still the old tool's). **Cut over the same day:** the reference
cluster runs on Noctyx's units. **Bare-metal restore image ported** (2026-10-19): `noctyx tool build-stick`
builds the dual-arch (x86_64 + arm64) recovery stick from the site config; a stick built into an image
file through a loop device passed all its checks (its key opens a live repository) and booted in UEFI VMs
on both arches into the recovery menu with the live inventory and snapshots. **First-time setup and a
macOS head** (2026-10-20): `noctyx init` (the assistant first, then a site config from questions),
`noctyx setup all` / `setup verify` (every step, then an end-to-end check with a canary backup and restore),
`setup metrics` (a pinned Prometheus) and `noctyx versions` (every critical component per machine, with
alerts when one is behind or can be upgraded). The head runs on Linux (systemd) or macOS (launchd, made from
the same unit templates); only Linux hosts are backed up, Macs are monitored and report Time Machine. CI tests
both platforms. **Module split started** (2026-11-14): the console's page is a file (`assets/console.html`), no
longer a string in `console.py`; recipe discovery (`discover.py`) and the schedule's rules (`schedule.py`) are out
of the orchestrator. Left: the rest of the orchestrator split (recipe deployment, the HF harvest, reboot), the
page's CSS and JS as files of their own, `noctyx migrate` (move nodes to Noctyx's own layout), a restore on real
hardware, Alertmanager on a macOS head.
 Port module by module from the private implementation, each
through the privacy check, none of its history. Order: config + inventory → jobs/locks/audit →
protect (restic, schedule, dumps) → cache → deploy → watch → console API → web UI split out of
Python → agent. Exit: a fresh site goes from `pip install` + `noctyx init` to nightly backups,
a working model cache and one deployed recipe, with zero edits to code.

**Added 2026-11-14 and 15, on the reference cluster first:**
- *Deploy:* a model swapped by hand is recorded (`recipe-adopt`) and a later swap alerts; watch-only slots; an
  answer check per slot (`slot-check`: fixed prompts, greedy, compared with the replies recorded for the
  deployment, after every switch and hourly); who is on each slot's GPU and who holds its lock.
- *Protect:* restore rehearsals (`rehearse`: every node restores a few files from its newest snapshot and compares
  them, monthly, with alerts); a backup's retry after a stale repository lock now runs in the same tick.
- *Watch:* a site's own pins for Prometheus, Alertmanager and node_exporter (`versions-pin`) with each release's
  notes summarised; the daily hygiene run fixes the space items a site hands to it; a nightly log digest of what
  is new in every node's warnings and errors; panels scroll sideways with a visible scrollbar.
- *The assistant:* one client for every caller with fallback to any model server, its health shown and alerted;
  a diagnosis under each problem self-repair leaves to a person; Ask can offer a console action as a button the
  person clicks.
- *Tests:* the schedule, the tick, backup refusals and recipe deploys run against a simulated cluster in CI
  (stand-in nodes; not yet containers as nodes).

**Added 2026-11-27 (0.9.6):**
- *Deploy:* implemented the open `[deploy]` `recipe.toml` spec v0 (`recipespec.py`, `kind: "spec"`) across prepare, systemd unit generation, switching, and `recipe-discover`.
- *Cache:* opt-in atomic write-through caching in `cache/mirror.py` (`hf_mirror.write_through`, SHA-256 verified, Hub→CDN redirect support, Prometheus metrics, `/config` toggle).
- *Protect:* pluggable secondary 3-2-1 backup targets (`targets.py`: `local`, `rest`, `s3`, `b2`, `sftp`) with root-only env credentials, `restic init --copy-chunker-params`, `noctyx copy --target`, and `/config` UI.
- *Watch:* structured external phone/chat/email alert routing (`alerting.py`: `ntfy`, `pushover`, `slack`, `telegram`, `webhook`, `email`), Alertmanager on both Linux systemd and macOS launchd (`setup alerting`, `alertmanager.service`), and `/config` UI.
- *Architecture & Hardening:* finished orchestrator split (`recipes.py`, `harvest.py`, `reboot.py`); split console UI into `console.html`, `console.css`, `theme.js`, and `console.js`; session revocation on password change (`session_epoch`), HTTP security headers, POST body cap, filename sanitization; scoped least-privilege sudoers generator (`noctyx sudoers`); pure-stdlib Debian `.deb` node agent packager (`noctyx build-deb`).
- *Tests & CI:* added `bash -n`, `shellcheck`, and `node --check` in CI; simulated cluster harness (`tools/sim_cluster.py`) with stub OpenAI slot, fake Prometheus/Alertmanager, real `restic` target copy, and `dpkg-deb` verification (290 tests passing).

Still open: `noctyx migrate`, a slot deployed end-to-end by Noctyx on the reference cluster, and a bare-metal restore drill on real hardware.

**Phase 2 — Harden (3–4 weeks). Started 2026-10-19:** console accounts (scrypt passwords, viewer /
operator / admin, signed HttpOnly SameSite=Strict session cookies, sign-in lockout, Origin check on every
POST, per-user display settings), themes and a settings drawer, About, Kiosk. Console auth and roles; scoped sudo; backup target plug-ins
(S3/B2, local disk, NAS over REST); packaging (wheel + .deb for the agent), upgrades between
versions; CI with a simulated cluster (containers as nodes, a fake GPU exporter, a stub
OpenAI-compatible server for deploy tests); hardware-in-the-loop runs on the reference cluster before
each release; user docs (install, concepts, recipes, restore drills).

**Phase 3 — Community beta (4–6 weeks).** 3–5 outside sites (DGX Spark pairs, RTX workstations) from
the recipe and benchmark community; `[deploy]` spec v0 with PRs to a few popular recipes; weekly
releases. Exit: two sites running unattended for two weeks; one recovered
from a real failure with Noctyx.

**Phase 4 — Public release.** Public repo, GitHub Pages site, complete guides, and community announcement with real numbers.

**Phase 5 — Multi-site (later, still free).** Optional multi-site federation / read-only roll-up view for hobbyists running more than one cluster.

## 7. Moving the reference cluster onto Noctyx

The private implementation keeps running the reference cluster until Noctyx reaches parity there.
Then the site switches to Noctyx with its own private `site.toml` (never committed to the public
repo), and the private implementation is archived. The reference cluster stays the hardware test
bed for every release.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Unauthorized commercial resale or paid support | PolyForm Noncommercial 1.0.0 prohibits commercial use, resale, and paid support; `Required Notice:` states this explicitly |
| Restore bugs lose someone's data | Restore drills in CI and on the reference cluster each release; "verify" on by default |
| A deploy takes down production | Plans by default, confirm with impact, GPU locks, automatic rollback — already built; keep it the default |

## 9. Publication rules

- The public repo starts fresh: no history from the private implementation.
- No site information in anything public: no names or e-mail addresses of people, host names, LAN or
  VPN addresses, home-directory paths, NAS names, or other private projects. (The bitcoin address for
  voluntary donations is deliberate.) Examples use generic
  names (`head`, `gpu-01`, `nas`) and documentation address ranges (192.0.2.0/24).
- `tools/privacy_check.py` runs in CI on every change. It checks generic patterns (private address
  ranges, home paths, non-noreply e-mail addresses) and, on a maintainer's machine, extra patterns from
  a git-ignored `.privacy-patterns` file — so the list of private strings is never itself published.
- Commits are authored by the project identity only.

## 10. Open decisions

1. Whether the Mac becomes a deploy slot (MLX recipes) in 1.0 or later.
2. A slot spanning both GB10 pairs (4-node recipes) — after the spec settles.
