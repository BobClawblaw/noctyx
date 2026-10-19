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
clock, status pills (*backups N/N on schedule*, alerts) and your sign-in state. The gear opens the display
settings. Click the logo for About.

| Key | Goes to |
|---|---|
| `1` | Dashboard `/` |
| `2` | Cluster `/cluster` |
| `3` | Recipes `/recipes` |
| `4` | Configuration `/config` |
| `5` | Kiosk `/kiosk` |
| `Esc` | closes menus and the settings drawer |

Number keys do nothing while you type in a field or hold Ctrl, Alt or Cmd.

## Dashboard (`/`)

![Dashboard](../screenshots/dashboard.png)

The panels sit on a 12-column grid. Drag a panel to reorder it, resize it to change its width or height, and
collapse it with ▾. The layout is saved in the browser. **Reset panel layout** is in the settings drawer.

| Panel | Shows |
|---|---|
| Cluster | fleet tokens/s, running requests, GPU power, tokens generated in 24 h, network, health. One row per node and model |
| Fleet | a card per node: CPU, GPU, temperature and power, network, memory, disk, the model it serves, uptime, its next scheduled backup |
| Machine Report | the selected node, with tabs **overview, llm, network, storage, gpu, health, backups**, and *backup now* / *verify now* |
| Storage Tiers | NAS free and used space, size per repository, growth forecast |
| Alerts | firing alerts with LLM triage (when an assistant is configured) |
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

A wall display with no menus and large type: GPU slots, a tile per node, fleet totals and alerts. Every source
refreshes together every 10 seconds.

## About (`/about`)

![About](../screenshots/about.png)

Shows the version, license and change date, Python and restic versions, this site (head, hosts, slots, recipes,
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
| Theme | **Noctyx** (navy and neon cyan), **Ember** (charcoal and amber), **Mono** (grayscale), **GitHub** (GitHub Dark/Light Default), **Catppuccin** (Mocha/Latte), **Custom** (nine colours: background, panels, text, muted text, accent, lines, good, warning, bad; the rest is mixed from them, and the mode follows the background) |
| Temperature | °F or °C (readings and alert thresholds stay in °C underneath) |
| Layout | reset panel layout (this browser) |

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
| `/api/v1/…` | the JSON the panels read. `POST /api/v1/action` runs actions ([accounts](accounts.md#api-token-for-scripts)) |

## Prometheus `/metrics`

The console exports its own state for Prometheus, cached for 30 seconds:

- **backup schedule health:** `cm_backup_health_code` (0 ok, 1 late, 2 stale, 3 never), `cm_backup_due` and the
  last-success, last-failure and next-due timestamps
- **runs:** `cm_run_*`
- **repositories, tiers and spindles:** `cm_repo_bytes`, `cm_tier_free_bytes`, `cm_tier_mounted`,
  `cm_spindle_busy`, `cm_rest_endpoint_up`
- **NAS RAID:** `cm_nas_raid_*`
- **hygiene:** `cm_hygiene_items`
- **GPU and temperatures from the node collectors:** `cm_gpu_*`, `cm_system_temperature_celsius`
- **per-source health:** `cm_source_ok{source=…}`. A failing source drops its own series and never the whole
  scrape.

`noctyx tool install-alerting` adds this scrape job (named after `prom_job_console`, default `noctyx`) to
`/etc/prometheus/prometheus.yml`, along with the alert rules and Alertmanager:

```yaml
  - job_name: noctyx
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/noctyx-ca.crt
    static_configs:
      - targets: ['127.0.0.1:20996']
```

The job scrapes `127.0.0.1`, so the console's certificate needs `IP:127.0.0.1` in its subjectAltName (see
[install](install.md#3-tls-ca-and-server-certificate)). The target port is fixed at 20996.
