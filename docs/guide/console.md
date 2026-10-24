# The console

The console is a single web app on the head node (`https://<head>:20996`, set by `server.ui_port`) for watching
and running the cluster: fleet metrics, LLM throughput, backups, hygiene, recipes, configuration and a wall
display. Each panel polls its own endpoint and repaints itself, so the page never does a full refresh. This page
tours the views, the display settings, keyboard keys and URLs, and the Prometheus endpoint. You can try
everything on sample data with `noctyx demo` ([install](install.md#demo-mode)).

Related pages: [accounts](accounts.md) · [site config](site-config.md) · [install](install.md).

## Navigation

![The header menus](../screenshots/menu.png)

The header has four menus: **Monitor** (Dashboard, Cluster), **Deploy** (Recipes), **Configure** (Backups,
Retention, Model mirror, Assistant LLM) and **Kiosk**. It also shows the page name, the head and node count, the
clock, status pills (*backups N/N on schedule*, alerts) and your sign-in state. **Ask** opens a drawer where the
assistant answers questions about the site from its own data (see [Assistant](assistant.md#ask-noctyx)). The gear
opens the display settings. Click the logo for About.

| Key | Goes to |
|---|---|
| `1` | Dashboard `/` |
| `2` | Cluster `/cluster` |
| `3` | Recipes `/recipes` |
| `4` | Configuration `/config` |
| `5` | Kiosk `/kiosk` |
| `Esc` | closes menus and the settings drawer (and the Ask drawer while you type in it) |

Number keys do nothing while you type in a field or hold Ctrl, Alt or Cmd.

## Dashboard (`/`)

![Dashboard](../screenshots/dashboard.png)

The panels sit on a 12-column grid. Drag a panel to reorder it, resize it to change its width or height, and
collapse it with ▾. The layout is saved in the browser. **Reset panel layout** is in the settings drawer.

| Panel | Shows |
|---|---|
| Cluster | fleet tokens/s, running requests, GPU power, tokens generated in 24 h, network, health. One row per node and model |
| Fleet | a card per node: CPU, GPU, temperature and power, network, memory, disk, the model it serves, uptime, its next scheduled backup |
| Machine Report | the selected node, with tabs **overview, llm, network, storage, gpu, health, backups**, and *backup now* / *verify now* (backed-up hosts only) |
| Storage Tiers | NAS free and used space, size per repository, growth forecast |
| Alerts | firing alerts with LLM triage (when an assistant is configured); silence one for a while; on a component alert *upgrade* it (or *upgrade all N*, one machine first) or *skip* that release ([upgrade](upgrade.md#acting-on-a-component-alert)); on `NoctyxNeedsAttention`, *repair now* ([self-repair](self-repair.md)) |
| Morning Brief | the assistant's summary of the fleet |
| Hygiene | housekeeping findings per host, the space each fix frees, fixes you can run |
| Snapshot Browser | browse and search (`*.conf`) any snapshot; operators can download files and directories. Restore to staging or the host |
| Schedule | what is due, late or stale, and when each profile runs next |
| Runs | recent backup, verify and prune runs and their results |
| Jobs / Audit | recent lines of the audit log |
| Network | last measured throughput per host (*probe all*) |
| Console | buttons for status, probe, capacity, snapshots, schedule dry run, hygiene report, backups and more. In token mode, also the token field |

Click a node in Fleet to open its report:

![Machine Report](../screenshots/node.png)

You can link straight to a node, panel and tab with the hash form `/#host=gpu-01&tab=gpu` or `/#panel=schedule`.
A bare `/#gpu-01` selects a host.

**What the report's overview shows:**

- **Vitals, filesystems, block devices:** the host's facts, read over ssh (for the head, with `"local": true`,
  read on the head itself), cached up to 10 minutes. They need no Prometheus.
- **Live numbers** (the gauges, sparklines and the llm, network, storage, gpu and health tabs) come from Prometheus
  on `127.0.0.1:9090`. A head without one shows the facts only; `sudo noctyx setup metrics` installs it (see
  [install](install.md)).
- **Components:** every critical component Noctyx tracks on that machine (Noctyx itself, restic, rest-server,
  Prometheus, node_exporter, the agent) with the installed version, Noctyx's pin, the latest upstream release, its
  state, and the buttons that act on it: *upgrade* where Noctyx installs it, *skip* a newer release, *unskip*
  (see [upgrade](upgrade.md#acting-on-a-component-alert)). The table
  is `noctyx versions`' last run (nightly, or by hand); the line under it says when. See
  [upgrade](upgrade.md#component-versions).
- **A monitored host** (a Mac, or a host with `"backup": false` or no repository) has no *backup now* / *verify now*
  buttons. In place of the snapshots card and the *Noctyx agent* line it shows its own backups: for a Mac a
  **Time Machine** card (the last finished backup, the destination, *backing up now* while one runs; amber when
  the last one is more than two days old) and a Backups line saying Time Machine backs it up. Noctyx never backs
  a Mac up.

## Cluster (`/cluster`)

![Cluster](../screenshots/cluster.png)

Systems only, one dense row per node: gauges, LLM queue and throughput, and the last hour's charts for
CPU/GPU, tokens/s and network. The footer has the fleet totals.

## Recipes (`/recipes`)

![Recipes](../screenshots/recipes.png)

A card per GPU slot shows what it serves, whether it answers, drift between what Noctyx started and what is
running, and the deployment history. From the card you can:

- **deploy**: build and download while the current model keeps serving, then switch
- **prepare**: build only
- **stop**, **start**, **roll back**

Below the cards, **Discover on GitHub** searches for public recipes that fit this cluster's hardware. Sort them by
`fastest`, `newest` or `stars`, and **add** one to the **Catalog**. The CLI equivalents are `recipe-deploy`,
`recipe-stop`, `recipe-start`, `recipe-rollback`, `recipe-status`, `recipe-catalog` and `recipe-discover`.

## Configuration (`/config`)

![Configuration](../screenshots/config.png)

A sticky bar offers **save & apply**, **re-apply**, **dump databases now** and **discard / reload**. Each section
has an anchor that the Configure menu links to:

| Anchor | Section |
|---|---|
| `/config#sec-backups` | All hosts (Docker volumes, database dumps, extra excludes, extra SQLite files) and the per-host table |
| `/config#sec-retention` | daily, weekly and monthly snapshots kept |
| `/config#sec-mirror` | HF mirror status, `HF_ENDPOINT`, nightly harvest, hosts to harvest, bandwidth limit, **harvest now** |
| `/config#sec-assistant` | assistant endpoint, model, timeout, fallback, API key, **test**, **save assistant** |

Saving runs the same validated CLI commands (`policy-set`, `assistant-set`). See
[the site config](site-config.md#backup_policy). Saving needs the `admin` role in accounts mode.

## Kiosk (`/kiosk`)

![Kiosk](../screenshots/kiosk.png)

A wall display with no menus and large type: GPU slots, a tile per node, fleet totals and alerts. It shows one
display and stays on it — a site setting can never make it alternate views. Every source refreshes together every
10 seconds; the wall never dims, flashes or reflows for a slow or failed fetch — it holds its last good frame.

What the kiosk shows is a site setting (settings › **Kiosk**, saved with *save as site default*, or
`noctyx console-set`). The nodes page never shows alerts; **Alerts** / **Backups** at *Detail* make
`?page=1` / `?page=2` worth visiting (alert summaries with their LLM triage; per-host backup last run / next due / window). Each node tile lists every model it serves with its own
tok/s, plus its 30-minute GPU and CPU traces. **Fit** chooses the layout: `scroll` (the default) keeps the type
full-size and the node grid scrolls inside its own area; `shrink` sizes everything to exactly one screen, down to
scaling the whole view on very small displays.

Any wall browser can choose its view in the URL without touching settings: `/kiosk?page=1` (0 nodes · 1 alerts ·
2 backups), `?fit=shrink`. `?rotation=1` is the one way to get an alternating wall: it pages through
nodes → alerts → backups every 20 seconds and always uses `shrink` — one screen per page is the point.

## About (`/about`)

![About](../screenshots/about.png)

Shows the version, license (Apache 2.0), Python and restic versions, this site (head, hosts, slots, recipes,
backup target, console build), the keyboard keys, documentation links and donation details.

## Sign-in (`/login`)

![Sign-in](../screenshots/login.png)

Appears in accounts mode. See [accounts](accounts.md).

## Display settings

![Display settings](../screenshots/settings.png)

Open the drawer with the gear, or go to `/#settings`. Changes apply as you make them. They change how the console
looks, never what it measures or does.

| Setting | Values |
|---|---|
| Mode | Light, Dark, System (follows the OS as it changes) |
| Theme | **Noctyx** (navy and neon cyan), the **Phosphor** family — **Phosphor**, **Aurora** (teal, violet and rose under northern lights), **Outrun** (sunset over a neon grid), **LCD** (a reflective calculator display by day, blue backlight by night: the easiest on the eyes), **P1 Green** (green-phosphor CRT), **P3 Amber** (amber terminal), **Nixie** (neon tubes, green indicator lamps), **Synthwave** (magenta, violet, cyan), **Arctic** (ice blue and frost), **Red Ops** (all red, keeps night vision in a dark room) — each an instrument panel: segmented LED meters, oscilloscope grids behind every trace, LCD numerals with their unlit segments, and one colour per subsystem everywhere — CPU green, GPU blue, memory violet, network cyan, power yellow, temperature amber, LLM magenta; disk and temperature meters run green → amber → red; its two fonts, Michroma and DSEG7, ship with the console under the SIL OFL, so nothing loads from the internet; a CRT overlay on the dark kiosk), **Ember** (charcoal and amber), **Mono** (grayscale), **GitHub** (GitHub Dark/Light Default), **Catppuccin** (Mocha/Latte), **Custom** (nine colours: background, panels, text, muted text, accent, lines, good, warning, bad; the rest is mixed from them, and the mode follows the background) |
| Temperature | °F or °C (readings and alert thresholds stay in °C underneath) |
| Layout | reset panel layout (this browser) |

**Phosphor effects** (settings drawer, Phosphor themes only; kept with the other display settings, so they can
be a site default or follow your account):

| Effect | Choices | What it is | Cost |
|---|---|---|---|
| Glow | off, soft, normal, bright | how much meters, traces and numerals bloom | none at rest |
| Scanlines | off, kiosk (default), everywhere | fine CRT lines | one still layer |
| Vignette | on (default), off | darker corners | one still layer |
| Grain | off (default), on | faint film grain | one still layer |
| RGB fringe | off (default), on | colour separation on the big numerals | text shadows, drawn once |
| Beam sweep | off (default), on | a band of light rolling down every 9 s | moves by `transform` on the compositor |
| Flicker | off (default), on | a barely-there phosphor flicker | `opacity` on the compositor |

Measured over 8 s of an idle page in headless Chrome, all seven effects on (beam and flicker running) cost
no extra style recalculation or layout, and about 3 ms more main-thread time than none. The moving two stop
when the system asks for reduced motion.

![Phosphor, the kiosk](../screenshots/phosphor-kiosk.png)

![The Phosphor family: Phosphor, P1 Green, P3 Amber, Nixie, Synthwave, Arctic, Red Ops](../screenshots/phosphor-family.jpg)

![Ember, light mode](../screenshots/theme-ember-light.png)

**Where settings are kept**, from the highest priority down:

1. **In this browser.** The default. Stored in localStorage.
2. **In my account.** When you are signed in, *Keep my settings → In my account* stores them on the head so they
   follow you to any browser. See [accounts](accounts.md#display-settings-per-user).
3. **Site defaults.** For every browser without its own settings. Set them with **save as site default** (shown to
   admins, and to everyone in open or token mode) or from the CLI:

   ```bash
   sudo noctyx console-set --json '{"mode": "system", "theme": "noctyx", "tunit": "C"}'
   ```

4. **The shipped look.** Noctyx theme, dark, °F.

**reset** / **use site default** clears this browser's own settings.

## URLs

| URL | What |
|---|---|
| `/` | Dashboard (`#host=…&panel=…&tab=…` to focus) |
| `/cluster`, `/recipes`, `/config`, `/kiosk`, `/about` | the views above |
| `/#settings` | Dashboard with the settings drawer open |
| `/login` | sign-in (`?next=/path`) |
| `/metrics` | Prometheus exposition, no sign-in |
| `/api/v1/versions` | every critical component per machine: installed, pinned, latest, state, upgrade command |
| `/api/v1/heal` | the repair loop: open problems, the last day's repairs, the changes ledger |
| `/api/v1/…` | the JSON the panels read. `POST /api/v1/action` runs actions ([accounts](accounts.md#api-token-for-scripts)) |

## Prometheus `/metrics`

The console exports its own state for Prometheus, cached for 30 seconds:

- **backup schedule health:** `noctyx_backup_health_code` (0 ok, 1 late, 2 stale, 3 never), `noctyx_backup_due`
  and the last-success, last-failure and next-due timestamps
- **runs:** `noctyx_run_*`
- **repositories, tiers and spindles:** `noctyx_repo_bytes`, `noctyx_tier_free_bytes`, `noctyx_tier_mounted`,
  `noctyx_spindle_busy`, `noctyx_rest_endpoint_up`
- **NAS RAID:** `noctyx_nas_raid_*` (only when `server.nas_ssh` is set: the RAID state is read over ssh to the NAS)
- **hygiene:** `noctyx_hygiene_items`
- **components:** `noctyx_component_behind` (1: older than Noctyx's pin, or missing) and
  `noctyx_component_upgrade_available` (1: a newer upstream release), each labelled `host`, `component`,
  `installed`, `pinned`, `latest` and `upgrade` (the command); `noctyx_versions_timestamp_seconds`. The alert rules
  `ComponentBehind`, `ComponentUpgradeAvailable` and `VersionsUnknown` (not checked for 3 days) read them
- **self-repair:** `noctyx_heal_problem` (1: broken, and `noctyx heal` cannot repair it; labelled `check`, `host`,
  `what`), `noctyx_heal_repairs_24h`, `noctyx_heal_last_run_timestamp_seconds`; the rules `NoctyxNeedsAttention`,
  `NoctyxRepairedItself` and `NoctyxHealNotRunning` read them ([self-repair](self-repair.md))
- **GPU and temperatures from the node collectors:** `noctyx_gpu_*`, `noctyx_system_temperature_celsius`
- **per-source health:** `noctyx_source_ok{source=…}`. A failing source drops its own series and never the whole
  scrape.

Every metric Noctyx makes starts with `paths.metric_prefix` (default `noctyx`), in the console's `/metrics`, the
mirror's, the node collectors' and the alert rules alike; a site that kept older dashboards sets its old prefix.

`sudo noctyx setup metrics` writes this scrape job (named after `prom_job_console`, default `noctyx`, with the
target port taken from `server.ui_port`) into the Prometheus config it manages, along with the head's own
exporter, every node's exporter, the LLM servers and the alert rules. On a Linux head that already runs
Prometheus, `setup metrics` prints the jobs instead, and `noctyx tool install-alerting` adds this job to
`/etc/prometheus/prometheus.yml` along with the alert rules and Alertmanager (Linux only, with apt and systemd;
a Mac head has no Alertmanager yet, so its rules evaluate in Prometheus but the Alerts panel stays empty):

```yaml
  - job_name: noctyx
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/noctyx-ca.crt
    static_configs:
      - targets: ['127.0.0.1:20996']
```

The job scrapes `127.0.0.1`, so the console's certificate needs `IP:127.0.0.1` in its subjectAltName (`noctyx setup
tls` puts it there). `install-alerting`'s target port is fixed at 20996.

## Config changes

The console re-reads the site config when the file changes (an `init` re-run, `policy-set`, a hand edit): hosts,
policy and settings apply on the next request, with no restart. The TLS files and the port are read once at start:
after changing them, restart the console (`sudo systemctl restart <unit_prefix>-console` on Linux,
`sudo launchctl kickstart -k system/<launchd_prefix>.console` on a Mac).
