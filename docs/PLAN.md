# Noctyx — product plan

**Status:** pre-alpha. This tree is the clean home of the product; the code is being extracted from
a private, single-site implementation that has run a real GPU cluster in production since 2026-09.
**Date:** 2026-10-19 (status updated 2026-10-20)

## Decisions taken

| | |
|---|---|
| Name | **Noctyx** (replaces the working name "ClusterManager", which is descriptive and crowded: NVIDIA's Bright Cluster Manager / Base Command Manager, Adaptive Cluster Manager, IBM Platform Cluster Manager, Gluu Cluster Manager) |
| License | **Apache License, Version 2.0** (since 2026-10-21; the tree launched under BSL 1.1) — free to use, modify and redistribute, commercial use included |
| Who sells support | Anyone may sell Noctyx (Apache 2.0); we aim to be the default: the maintainer's hosting, the Fleet console and paid support remain the revenue lines |
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

**Who it is for.** Home labs, hobbyists, students and non-profit research — the community that
writes and benchmarks recipes, and Noctyx's distribution channel — and startups and companies
running their own inference hardware (a few GPU nodes to a few racks), AI consultancies and
integrators deploying for clients, labs inside companies. The software is free for all of them.

## 2. Licensing and how the project makes money

**What Apache License 2.0 means** (changed from BSL 1.1 on 2026-10-21; the copyright stays with the
contributors, §5 of the license carries their contributions under the same terms — no CLA needed):

| Use | Allowed without paying? |
|---|---|
| Read, copy, modify, fork, redistribute the source | Yes (derivatives keep the Apache 2.0 notice obligations) |
| Any use, personal or commercial, at any scale | Yes |
| Offering Noctyx to third parties as a hosted or managed service | Yes |
| Selling Noctyx itself, or a support contract for it | Yes — anyone may; we compete on being the maintainer |

It is true open source (OSI-approved); say "open source", not "source-available".

**Revenue lines**, in launch order:
0. **Donations** — the README carries the bitcoin address `bc1q9echf820hapc04y0f636uxdry2ahyx32tkleff`,
   Noctyx's own.
1. **Support** — business-hours or 24×7 response on the open-source core; paid onboarding (we bring a
   new site from zero to "every model deployable, every node restorable").
2. **Noctyx Fleet** (later) — multi-site console, SSO/RBAC, audit export, alert routing to
   PagerDuty/Slack/Teams, off-site replication policies, compliance reports. SaaS/subscription;
   lives in a separate repository so the open-source core stays complete for single sites.
3. **Hosted control plane** (later) — the console as a service, agents at the customer's site.

**Pricing — hypotheses to validate in the beta, not decisions.** Anchor against "what an avoided
outage or a re-downloaded 200 GB model costs" for support, and against self-hosting effort for Fleet
and hosting.

**No enforcement.** The license asks nothing of users: no license keys, no DRM, no phone-home, no
audit clause. Money comes from convenience (support, hosting, Fleet), not from the code.

**Legal setup — before the first public release** (needs a lawyer; this plan is not legal advice):
- [ ] A company (e.g. an LLC) to own the project trademark and to sign customer contracts; assign
      the copyright it holds to the company.
- [ ] Trademark: clearance search for **NOCTYX** (software, class 9 and 42), then file. Register
      the domains (noctyx.ai / .dev were unregistered at last check; .com was taken).
- [ ] Support terms, privacy policy for the website.
- [ ] Accounting for bitcoin income (donations may be taxed differently from service revenue); once
      the company exists, decide whether service payments move to an address the company controls,
      separate from the donation address.

- [ ] Third-party notices: restic and rest-server (BSD-2-Clause), Prometheus / Alertmanager /
      node_exporter (Apache-2.0), macmon and any bundled JS — keep `THIRD_PARTY.md` exact.

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
it — adoption is the point; the implementation is Apache 2.0 too). Then:
- discovery shows "deployable" for every spec'd recipe, with no guessing;
- benchmark leaderboards can link speed results to recipes by repo and commit;
- recipe authors get a reason to add the section: one-click installs for their users.

## 6. Phases

**Phase 0 — Foundations (this week)**
- [x] Name, license, this plan, the tree
- [x] Privacy check in CI (`tools/privacy_check.py`)
- [ ] Legal setup started (§2 checklist): company, trademark search, lawyer for the grant text
- [ ] Domains registered

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
both platforms. Left: splitting the two large modules, the web UI out of Python, `noctyx migrate` (move nodes
to Noctyx's own layout), a restore on real hardware, Alertmanager on a macOS head.
 Port module by module from the private implementation, each
through the privacy check, none of its history. Order: config + inventory → jobs/locks/audit →
protect (restic, schedule, dumps) → cache → deploy → watch → console API → web UI split out of
Python → agent. Exit: a fresh site goes from `pip install` + `noctyx init` to nightly backups,
a working model cache and one deployed recipe, with zero edits to code.

**Phase 2 — Harden (3–4 weeks). Started 2026-10-19:** console accounts (scrypt passwords, viewer /
operator / admin, signed HttpOnly SameSite=Strict session cookies, sign-in lockout, Origin check on every
POST, per-user display settings), themes and a settings drawer, About, Kiosk. Console auth and roles; scoped sudo; backup target plug-ins
(S3/B2, local disk, NAS over REST); packaging (wheel + .deb for the agent), upgrades between
versions; CI with a simulated cluster (containers as nodes, a fake GPU exporter, a stub
OpenAI-compatible server for deploy tests); hardware-in-the-loop runs on the reference cluster before
each release; user docs (install, concepts, recipes, restore drills).

**Phase 3 — Private beta (4–6 weeks).** 3–5 outside sites (DGX Spark pairs, RTX workstations) from
the recipe and benchmark community; `[deploy]` spec v0 with PRs to a few popular recipes; weekly
releases; collect pricing evidence. Exit: two sites running unattended for two weeks; one recovered
from a real failure with Noctyx.

**Phase 4 — Launch (2–3 weeks).** Public repo, website, docs, support offerings, Fleet/hosting
signups. Announce in the recipe community with real numbers.

**Phase 5 — Fleet and hosted (after revenue).** Multi-site console, SSO, alert routing, off-site
replication, then the hosted control plane.

## 7. Moving the reference cluster onto Noctyx

The private implementation keeps running the reference cluster until Noctyx reaches parity there.
Then the site switches to Noctyx with its own private `site.toml` (never committed to the public
repo), and the private implementation is archived. The reference cluster stays the hardware test
bed for every release.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Apache 2.0 lets anyone sell or host it | Expected of open source; we compete on being the maintainer: fastest development, the recipe community channel, the Fleet and hosting products |
| A big vendor ships the same layer | Speed: own the recipe spec and the community channel first; the brand is ours |
| Restore bugs lose someone's data | Restore drills in CI and on the reference cluster each release; "verify" on by default |
| A deploy takes down production | Plans by default, confirm with impact, GPU locks, automatic rollback — already built; keep it the default |
| Trademark conflict found late | Clearance search before any public launch |

## 9. Publication rules

- The public repo starts fresh: no history from the private implementation.
- No site information in anything public: no names or e-mail addresses of people, host names, LAN or
  VPN addresses, home-directory paths, NAS names, or other private projects. (The bitcoin address for
  donations and service payments is deliberate.) Examples use generic
  names (`head`, `gpu-01`, `nas`) and documentation address ranges (192.0.2.0/24).
- `tools/privacy_check.py` runs in CI on every change. It checks generic patterns (private address
  ranges, home paths, non-noreply e-mail addresses) and, on a maintainer's machine, extra patterns from
  a git-ignored `.privacy-patterns` file — so the list of private strings is never itself published.
- Commits are authored by the project identity only.

## 10. Open decisions

1. Support/Fleet/hosting pricing (per-node support price, Fleet tier) — after beta evidence.
2. The company: jurisdiction and structure — with the lawyer.
3. Whether the Mac becomes a deploy slot (MLX recipes) in 1.0 or later.
4. A slot spanning both GB10 pairs (4-node recipes) — after the spec settles.
