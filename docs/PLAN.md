# Noctyx — product plan

**Status:** pre-alpha. This tree is the clean home of the product; the code is being extracted from
a private, single-site implementation that has run a real GPU cluster in production since 2026-09.
**Date:** 2026-10-19

## Decisions taken

| | |
|---|---|
| Name | **Noctyx** (replaces the working name "ClusterManager", which is descriptive and crowded: NVIDIA's Bright Cluster Manager / Base Command Manager, Adaptive Cluster Manager, IBM Platform Cluster Manager, Gluu Cluster Manager) |
| License | **Business Source License 1.1** — production use free for non-commercial purposes at any scale; every for-profit use needs a commercial license; each release converts to **Apache 2.0** four years after it ships |
| Who may sell it | Only the Licensor. Nobody else may sell it, host it as a service, or run it inside a business without a commercial license |
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

**Who it is for.**
- *Free:* home labs, hobbyists, students, non-profit research — the community that writes and
  benchmarks recipes, and Noctyx's distribution channel.
- *Paying:* startups and companies running their own inference hardware (a few GPU nodes to a few
  racks), AI consultancies and integrators deploying for clients, labs inside for-profit companies.

## 2. Licensing and how only the Licensor makes money

**What BSL 1.1 with our Additional Use Grant does:**

| Use | Allowed without paying? |
|---|---|
| Read, copy, modify, fork, redistribute the source | Yes (forks stay under the same license) |
| Non-production use by anyone (evaluation, development, testing) | Yes — BSL always allows it |
| Production use by an individual: personal, hobby, home lab | Yes, any size |
| Teaching and research at a non-profit institution | Yes, any size |
| Any production use by or for a for-profit organization | **No — commercial license** |
| Offering Noctyx (or its output) to third parties as a hosted or managed service | **No — commercial license** |
| A version older than four years | Yes — it is Apache 2.0 by then |

It is *source-available*, not "open source" (OSI forbids field-of-use limits); say "source-available"
everywhere. The four-year conversion keeps goodwill: the community always gets yesterday's Noctyx
under a true open-source license.

**Revenue lines**, in launch order:
0. **Donations** — Noctyx is donationware for its free users: the README and COMMERCIAL.md carry
   the bitcoin address `bc1q9echf820hapc04y0f636uxdry2ahyx32tkleff`, Noctyx's own.
1. **Commercial license** — paid in bitcoin to the same address (quote → payment → transaction id +
   a message signed by one of the transaction's input addresses, which proves the payer → signed
   license file; verification with BIP-322); card/invoice billing once the company exists — annual subscription per managed GPU node (a node = one machine with an
   accelerator; a TP pair = two nodes), with a site license above a threshold. Includes updates.
2. **Support** — business-hours or 24×7 tiers on top of the license; paid onboarding (we bring a
   new site from zero to "every model deployable, every node restorable").
3. **Noctyx Fleet** (later) — multi-site console, SSO/RBAC, audit export, alert routing to
   PagerDuty/Slack/Teams, off-site replication policies, compliance reports. Licensed separately;
   lives in a separate private repository so the BSL core stays complete for single sites.
4. **Hosted control plane** (later) — the console as a service, agents at the customer's site.
   Only the Licensor may offer this; the license forbids it for everyone else.

**Pricing — hypotheses to validate in the beta, not decisions.** Anchor high against enterprise
cluster managers (sold per node per year, aimed at data centres) and low against "free scripts".
Test a startup tier (small node count, self-serve) and a standard tier; ask beta sites what one
avoided outage or one avoided re-download of a 200 GB model is worth to them.

**Enforcement.** Honest-customer model at launch: no DRM, no phone-home. The console shows the
license state ("non-commercial", or the licensee and node count from a signed license file) and the
commercial agreement carries an audit clause. Add license-key checks only to Fleet features, never to
the free core.

**Legal setup — before the first public release** (needs a lawyer; this plan is not legal advice):
- [ ] A company (e.g. an LLC) to own the copyright and trademark and to sign customer contracts;
      assign the existing code to it and change `Licensor` in LICENSE to the company.
- [ ] Lawyer review of the Additional Use Grant wording (it may only *add* rights — BSL covenant 2).
- [ ] Trademark: clearance search for **NOCTYX** (software, class 9 and 42), then file. Register
      the domains (noctyx.ai / .dev were unregistered at last check; .com was taken).
- [ ] Contributor License Agreement (Apache-ICLA style: contributor keeps copyright, grants the
      Licensor the right to license contributions commercially). Enforced by a CLA bot on PRs.
- [ ] Commercial license agreement + order form; support terms; privacy policy for the website.
- [ ] Accounting for bitcoin income: license payments are business revenue (donations may be taxed
      differently); once the company exists, decide whether license payments move to an address the
      company controls, separate from the donation address.
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
it — adoption is the point; the implementation stays BSL). Then:
- discovery shows "deployable" for every spec'd recipe, with no guessing;
- benchmark leaderboards can link speed results to recipes by repo and commit;
- recipe authors get a reason to add the section: one-click installs for their users.

## 6. Phases

**Phase 0 — Foundations (this week)**
- [x] Name, license, this plan, the tree
- [x] Privacy check in CI (`tools/privacy_check.py`)
- [ ] Legal setup started (§2 checklist): company, trademark search, lawyer for the grant text
- [ ] Domains registered

**Phase 1 — Extract (3–4 weeks).** Port module by module from the private implementation, each
through the privacy check, none of its history. Order: config + inventory → jobs/locks/audit →
protect (restic, schedule, dumps) → cache → deploy → watch → console API → web UI split out of
Python → agent. Exit: a fresh site goes from `pip install` + `noctyx init` to nightly backups,
a working model cache and one deployed recipe, with zero edits to code.

**Phase 2 — Harden (3–4 weeks).** Console auth and roles; scoped sudo; backup target plug-ins
(S3/B2, local disk, NAS over REST); packaging (wheel + .deb for the agent), upgrades between
versions; CI with a simulated cluster (containers as nodes, a fake GPU exporter, a stub
OpenAI-compatible server for deploy tests); hardware-in-the-loop runs on the reference cluster before
each release; user docs (install, concepts, recipes, restore drills).

**Phase 3 — Private beta (4–6 weeks).** 3–5 outside sites (DGX Spark pairs, RTX workstations) from
the recipe and benchmark community; `[deploy]` spec v0 with PRs to a few popular recipes; weekly
releases; collect pricing evidence. Exit: two sites running unattended for two weeks; one recovered
from a real failure with Noctyx.

**Phase 4 — Launch (2–3 weeks).** Public repo, website, docs, the commercial license store (order
form + signed license files), support intake. Announce in the recipe community with real numbers.

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
| "Source-available" puts off part of the community | Free for every non-commercial user at any size; 4-year Apache conversion; the deploy spec itself is CC-BY |
| A big vendor ships the same layer | Speed: own the recipe spec and the community channel first; the brand is ours |
| Companies use it without paying | Clear license state in the console, audit clause, a cheap startup tier that is easier than evading |
| Restore bugs lose someone's data | Restore drills in CI and on the reference cluster each release; "verify" on by default |
| A deploy takes down production | Plans by default, confirm with impact, GPU locks, automatic rollback — already built; keep it the default |
| Trademark conflict found late | Clearance search before any public launch |

## 9. Publication rules

- The public repo starts fresh: no history from the private implementation.
- No site information in anything public: no names or e-mail addresses of people, host names, LAN or
  VPN addresses, home-directory paths, NAS names, or other private projects. (The bitcoin address for donations and
  license payments is deliberate.) Examples use generic
  names (`head`, `gpu-01`, `nas`) and documentation address ranges (192.0.2.0/24).
- `tools/privacy_check.py` runs in CI on every change. It checks generic patterns (private address
  ranges, home paths, non-noreply e-mail addresses) and, on a maintainer's machine, extra patterns from
  a git-ignored `.privacy-patterns` file — so the list of private strings is never itself published.
- Commits are authored by the project identity only.

## 10. Open decisions

1. Commercial pricing (per-node price, startup tier, site-license threshold) — after beta evidence.
2. The company: jurisdiction and structure — with the lawyer.
3. Whether the Mac becomes a deploy slot (MLX recipes) in 1.0 or later.
4. A slot spanning both GB10 pairs (4-node recipes) — after the spec settles.
