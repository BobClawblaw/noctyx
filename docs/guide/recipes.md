# Recipes

A recipe is a git repository that serves one model on specific hardware. Noctyx deploys catalog recipes onto
GPU **slots**: it checks out a pinned commit and builds it while the current model keeps serving, then switches
under the slot's GPU lock, waits for the health check, and switches back on its own if the new deployment does
not come up. This page covers slots, the catalog, the three recipe kinds, the `recipe-*` commands, discovery of
public recipes on GitHub, and the console's Recipes page. The draft recipe format is in
[the recipe deploy spec](../spec/recipe-deploy.md).

All `noctyx` commands run as root on the head node. `recipe-deploy`, `recipe-stop`, `recipe-start` and
`recipe-rollback` run as detached systemd jobs and stream their log. `--foreground` keeps the job in the current
process, and `--no-follow` starts it and returns.

## Slots

A slot is one GPU box, or the head and worker of a tensor-parallel pair. A slot serves one recipe at a time on a
fixed port. Slots live in the site config under `recipes.slots`:

```json
"recipes": {
 "slots": {
  "rtx5090": {"head": "gpu-01", "port": 30000, "health_timeout_s": 900, "gpu_idle_check": true},
  "pair":    {"head": "spark-1", "worker": "spark-2", "port": 30000,
              "cluster": {"worker": "spark-2"}, "script_env": {"WORKER": "spark-2"}}
 },
 "catalog": {}
}
```

| Key | Meaning | Default |
|---|---|---|
| `head` | Host (from `hosts`) that runs the deployment's systemd unit. Required. | |
| `worker` | Second host of a TP pair. Used for the plan, status and discovery fit. The recipe reaches the worker through its own settings (`cluster`, `script_env`). | none |
| `port` | Port the deployment serves on. Health is checked here. Required. | |
| `lock` | GPU lock file on the head. `~` means the slot user's home. | `paths.gpu_lock` = `/var/lock/noctyx-gpu.lock` |
| `health_timeout_s` | How long a start may take before it counts as failed. | `1800`; the unit's `TimeoutStartSec` is this plus 300 |
| `settle_s` | Pause between the stop and the start. | 5 |
| `gpu_idle_check` | After the stop, wait up to 120 s for `nvidia-smi` to show no compute processes. Refuse if the GPU stays busy. | off |
| `conflicts` | Other systemd units on the head to disable before the start, for example a server that ran before Noctyx. | `[]` |
| `root` | Where checkouts go on the head. | `recipes.root`, else `~/.local/share/noctyx/recipes` |
| `env` | Environment for every deployment on this slot. | `{}` |
| `cluster` | Settings merged into a `spark` recipe's `cluster.toml` (the slot's `port` is always added). | `{}` |
| `script_env` | Settings written to a `script` recipe's `.env`. | `{}` |
| `adopt` | `{"unit": "...", "name": "...", "spark_dir": "..."}`: what the slot served before Noctyx. See [Adopted deployments](#adopted-deployments). | none |

Other `recipes` keys: `root` (default checkout root), `hf_mirror` (set `false` to stop deployments from getting
`HF_ENDPOINT`; default `true`), and `harness_dir` (where discovery reads measured speeds; default
`/srv/recipe-harness`).

The deployment runs as the slot user. That is the head host's `ssh_user`, else `server.ssh.user`. Its checkouts are
at `<root>/<recipe>/<first 12 of the sha>`. Its unit is `noctyx-recipe-<slot>.service` (prefix from
`paths.recipe_unit_prefix`).

## The catalog

The catalog names the recipes that may be deployed and the slots each one may run on. Edit it with
`recipe-catalog`. Noctyx checks the entry and then rewrites only the `recipes` block of the site config.

Add or replace an entry:

```sh
sudo noctyx recipe-catalog --name qwen-27b --json '{
  "repo": "https://github.com/example/qwen-27b-rtx-5090.git",
  "ref": "main", "kind": "systemd", "slots": ["rtx5090"],
  "env": {"MAX_MODEL_LEN": "32768"}
}'
```

Remove an entry. Noctyx refuses while the recipe is deployed on a slot, and nothing is stopped:

```sh
sudo noctyx recipe-catalog --name qwen-27b --remove
```

| Field | Rule |
|---|---|
| name (`--name`) | Lowercase letters, digits, `.` and `-`, up to 48 characters |
| `repo` | An `https://` or `ssh://` git URL |
| `ref` | A branch, tag or commit sha (default `main`). It is resolved to a commit with `git ls-remote` when you deploy. |
| `kind` | `spark`, `systemd` or `script` |
| `slots` | One or more slot names from `recipes.slots` |
| `env` | Object of `UPPER_CASE` names. The recipe's environment, on top of the slot's `env`. |
| `overrides` | `spark` only: `{section: {key: value}}` merged into `cluster.toml` |

Other fields are dropped.

## Recipe kinds

Noctyx does not read `[deploy]` from `recipe.toml` yet. It drives a checkout by the files the kind names:

| Kind | Repository provides | Prepare (old one still serving) | Serve (`noctyx-recipe-<slot>.service`) |
|---|---|---|---|
| `systemd` | `setup.sh` and `serve.sh`, both executable | `./setup.sh` with the environment below | `Type=simple`, `ExecStart=<dir>/serve.sh`, restart on failure, log in `<dir>/serve.log` |
| `spark` | A `spark` Python package (`python3 -m spark …`) | `spark validate`. Then `spark image` if the head has at least 40 GiB free, and the weights download unless they share a cache folder with what serves now | `Type=oneshot`: `python3 -m spark up` / `python3 -m spark down` |
| `script` | `start.sh` and `stop.sh`, both executable | Both exist. If `start.sh` mentions `DRY_RUN`, it runs once with `DRY_RUN=1` and must pass. | `Type=oneshot`: `ExecStart=start.sh`, `ExecStop=stop.sh` |

What each kind is given:

- **systemd:** `PORT`, `MODELS=<root>/<recipe>/models` and `TF_SRC=<dir>/engine`, then the slot `env` and the catalog
  `env`.
- **spark:** a `cluster.toml` written into the checkout from the catalog `overrides`, the slot's `cluster` block and
  `port`. Environment: slot `env`, catalog `env` and `HF_ENDPOINT`.
- **script:** a `.env` written into the checkout from the slot's `script_env`, the catalog `env`, `PORT` and
  `HF_ENDPOINT`. The same values go into the unit's environment.

`HF_ENDPOINT` points at the head's [Hugging Face mirror](hf-mirror.md). Spark and script recipes get it in their
environment, and the prepare commands of every kind run with it. It is set when `recipes.hf_mirror` is not `false`
and `hf_mirror.port` is set in the site config.

## Deploying

```sh
sudo noctyx recipe-deploy --slot rtx5090 --recipe qwen-27b            # print the plan, change nothing
sudo noctyx recipe-deploy --slot rtx5090 --recipe qwen-27b --yes      # deploy
sudo noctyx recipe-deploy --slot rtx5090 --recipe qwen-27b --ref v2 --yes
sudo noctyx recipe-deploy --slot rtx5090 --recipe qwen-27b --prepare-only   # check out and build, do not switch
```

| Flag | Meaning |
|---|---|
| `--slot`, `--recipe` | Required. The recipe must list the slot in its `slots`. |
| `--ref` | Branch, tag or commit. Default: the catalog's `ref`. |
| `--yes` | Actually deploy. Without it (and without `--prepare-only`) the plan is printed and the exit code is 2. |
| `--prepare-only` | Only the prepare stage. The slot keeps serving what it serves. |
| `--wait-lock MIN` | Wait this many minutes for the GPU lock. Default 0: fail at once. |

What happens:

1. **Plan.** Noctyx prints what the slot serves now and what stops during the switch. It refuses if the recipe or
   ref is unknown, or if another recipe job holds the slot. Each slot has its own job lock (`recipe-<slot>` in
   `/var/lock/noctyx`).
2. **Prepare, while the current model serves.** Noctyx clones the repo at the pinned commit into a new directory,
   checks `HEAD`, writes the config files, and runs the kind's prepare steps (see the table above). If this fails,
   nothing was stopped.
3. **Switch, under the GPU lock.** Noctyx takes the slot's `lock` with `flock`. If another job holds it (a
   benchmark, say), the exit code is 75 and nothing is stopped. Under the lock it:
   - disables the old unit, the slot's own unit and every unit in `conflicts`, and runs the old checkout's
     `spark down` or `stop.sh`;
   - waits up to 120 s for the port to stop answering (exit 76 if a server Noctyx does not manage still holds it)
     and, with `gpu_idle_check`, for the GPU to be idle (exit 77);
   - writes `/etc/systemd/system/noctyx-recipe-<slot>.service`, enables it and starts it.
4. **Health check.** The deployment is up when `/health` on the port returns `"status": "ok"` and `/v1/models` lists
   at least one id. Noctyx gives up if the unit fails, if the unit is still inactive after 20 s, or after
   `health_timeout_s` (exit 79).
5. **Automatic rollback.** If the new deployment does not come up, Noctyx switches straight back to the previous one
   under the same lock, waiting up to 10 minutes for the lock. It prints `rolled back` or `ROLLBACK FAILED`.

Each deploy is recorded in `/etc/noctyx/state/recipes.json`, with `current`, `previous`, prepared checkouts and the
last 30 events. It also goes to the audit log.

## Stop, start, roll back, status

```sh
sudo noctyx recipe-status                       # slots, what each serves, the catalog
sudo noctyx recipe-status --json                # the same data the console uses
sudo noctyx recipe-stop --slot rtx5090 --yes    # stop; the slot serves nothing
sudo noctyx recipe-start --slot rtx5090         # start the current deployment again (no --yes needed)
sudo noctyx recipe-rollback --slot rtx5090 --yes   # switch back to the previous deployment
```

- Without `--yes`, `recipe-stop` and `recipe-rollback` print the plan and exit 2. All three take `--wait-lock MIN`.
- `recipe-start` does nothing if the slot already serves.
- Noctyx keeps only one previous deployment. A rollback makes the two swap places, so a second rollback goes back to
  where you started.
- `recipe-status` warns about **drift**: the port serves models other than the ones Noctyx last started, or a
  deployment marked serving does not answer.

## Adopted deployments

A slot can start out serving something Noctyx did not deploy. Name its unit under `adopt`, and Noctyx treats it as
the slot's current deployment until the first deploy:

```json
"rtx5090": {"head": "gpu-01", "port": 30000,
            "adopt": {"unit": "llm-server.service", "name": "llm-server (pre-Noctyx)"}}
```

The first deploy disables the adopted unit, and the adopted unit becomes `previous`. `recipe-rollback` then
re-enables it and starts it. For an adopted spark checkout, set `spark_dir` so the switch runs `spark down` there
and the prepare step can tell whether the two share a weights cache.

## Discovering recipes

`recipe-discover` searches GitHub for public recipes that fit this cluster and ranks them:

```sh
sudo noctyx recipe-discover                  # --sort fastest (default)
sudo noctyx recipe-discover --sort newest      # or --sort stars
```

- **Search.** It runs unauthenticated GitHub repository searches: `tensorfold recipe` plus a few queries for each
  kind of GPU this site's slots have (and Apple silicon when a host has `"os": "macos"`), plus `user:<owner>` for
  each GitHub owner already in the catalog. `recipes.discover_queries` replaces the whole list. The searches are
  spaced 6.5 s apart to stay under GitHub's limit. No token is used. Archived repos are skipped. Up to 80 results
  for this site's hardware are kept, plus up to 20 for other hardware as context.
- **Classify.** The hardware comes from the repo name, then its description, then `recipe.toml`: DGX Spark (`gb10`),
  RTX PRO 6000, RTX 5090/5080/4090/3090, H200, H100, B200, A100, L40S or Apple silicon, with a count of units
  (`2x`, `tp=2`, `dual`, …). Anything else is dropped. The kind comes from the files on the default branch:
  `recipe.toml` with `spark/__main__.py` means `spark`, `recipe.toml` with `setup.sh` and `serve.sh` means
  `systemd`, and `start.sh` with `stop.sh` means `script`.
- **Slot hardware.** Each slot's GPU is its `hw` key (e.g. `"hw": "rtx5090"`), or else what its head node's GPU
  reports (the console's facts), or else its name. Its size is `gpus`, or 2 for a slot with a `worker`, else 1.
- **Fit.** A result fits every slot with the same GPU and the right size: a `systemd` recipe needs a one-GPU
  slot; `spark` and `script` recipes need as many as they name. Anything else is listed with the reason, e.g.
  "needs 4× DGX Spark (GB10); this site's DGX Spark (GB10) slots: spark-a (2×)". Every kind deploys as a systemd
  unit, so a macOS host can be monitored but is not a slot.
- **Speed.** A **measured** speed comes from the recipe harness ledger (`<harness_dir>/stats/*.json`, matched to
  repos through the targets' GitHub links) or from the tensorfold.dev leaderboard (matched by the repo or the model
  ids in its `recipe.toml`, on the same hardware). Without one, the first `tok/s` figure in the README counts as a
  **claim**. Claims are unverified and are often an aggregate over many streams.
- **Ranking.** `fastest` puts measured speeds first, then claims, then unknown, newest first among equals. `newest`
  sorts by the last push, `stars` by GitHub stars.

Results are saved to `/etc/noctyx/state/recipe-discover.json` for the console. Discovery adds nothing to the
catalog and deploys nothing. The nightly run (`noctyx nightly`) runs it every day.

## The console's Recipes page

`/recipes` (key `3`) shows one card per slot:

- The current deployment. Adopted ones show their unit. The card also shows what the port serves live and how many
  are connected, any drift warning, prepared checkouts, and history.
- **deploy** (with a recipe and an optional ref) and **prepare**.
- **stop**, **start** and **roll back → previous**.

Below the cards:

- **Discover on GitHub:** results sorted by fastest, newest or stars, measured speeds tagged `measured` and README
  figures tagged `claim`. An option shows the recipes Noctyx cannot deploy here, **search now** runs
  `recipe-discover`, and **add** puts a fitting result into the catalog.
- **Catalog:** the entries, a form to add one, and **remove**.

Each button runs the matching CLI command, which checks everything again. Deploy, prepare, stop, start, roll back and
search need the **operator** role. Catalog changes need **admin**. See [Accounts](accounts.md).
