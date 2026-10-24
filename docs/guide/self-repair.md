# Self-repair

`noctyx heal` compares the fleet with what Noctyx expects and repairs what it safely can, with no one at the
keyboard. It runs:

- every 5 minutes, from the heal timer (`noctyx-heal.timer`, installed by `noctyx install-head --apply`);
- at once, for one machine, when an alert it has a playbook for fires ([below](#alerts-wake-it));
- when someone presses **repair now** (the Self-repair page, or a `NoctyxNeedsAttention` alert).

```bash
sudo noctyx heal --dry-run              # what it finds and what it would do; repairs nothing
sudo noctyx heal                        # repair what it can now
sudo noctyx heal --host gpu-01          # only that machine (what an alert runs)
sudo noctyx heal --unfreeze             # let repairs run again after a freeze
sudo noctyx heal-set --json '{"playbooks": {"disk": "shadow"}}'     # change the policy
```

Every run is logged, with what each check found, each decision and why, and every command with its output. The
console's **Self-repair** page (`/heal`) shows the log ([below](#the-log)).

## Playbooks

Each check is a playbook. A playbook is **on** (it repairs), **shadow** (it logs what it would do and leaves it to
a person) or **off** (it does not even check). All are on by default; automatic upgrades also need their own
switch.

| Playbook | Broken when | Repaired by |
|---|---|---|
| `service` | a Noctyx timer or long-running service, Prometheus or Alertmanager is stopped or failed, or runs but has not answered for 10 minutes | a restart |
| `exporter` | Prometheus cannot scrape a node's exporter | undoing Noctyx's own change of it from the last day (see [upgrade](upgrade.md#every-upgrade-checks-itself-and-rolls-back)); else restarting an exporter that is stopped |
| `dropin` | the exporter's drop-in differs from [`metrics/node-exporter.conf`](../../src/noctyx/metrics/node-exporter.conf) on a machine running Noctyx's exporter | writing it again, as a change: the exporter must then scrape within 5 s, or the old file goes back |
| `textfile` | the textfile collector's file (`paths.textfile_name`) is more than 5 minutes old | restarting its timer |
| `rules` | `<prefix>-rules.yml` beside Prometheus's config differs from this Noctyx's rules | writing them again after `promtool check rules`, then a reload; the old file goes back if Prometheus does not load them all |
| `disk` | a root filesystem is fuller than `disk_pct` (92 %) | vacuuming the journal to 1 GiB and clearing apt's package cache ([below](#disk)) |
| `units` | a systemd unit failed | `reset-failed` when its unit file is gone; a restart only for units in `restart_units` ([below](#failed-units)) |
| `nas` | the NAS backup endpoint does not answer | starting it on the NAS |
| `backups` | a backup failed twice in a row | nothing: the tick retries it an hour later. Heal says why it failed (ssh lost, a locked repository, unreadable files, the endpoint down) |
| `slots` | a model server in `restart_slots` stops answering | `noctyx recipe-start`, after 15 minutes ([below](#model-servers)) |
| `upgrades` | `upgrades.auto` is on and a component is older than Noctyx's pin | `noctyx upgrade`, in the window ([below](#automatic-upgrades)) |

The drop-in repair covers an exporter upgraded with an older drop-in: it still answers, but every scrape times
out. The rules repair is also how a Noctyx upgrade's new or changed alert rules reach Prometheus.

## Safety

- **Bounded per thing.** Each thing is repaired at most 3 times in 6 hours. After that it needs a person.
- **Bounded per hour.** At most `budget_per_hour` (10) repairs across the fleet in any hour. The rest wait for
  the next run that has room.
- **Freeze.** `freeze_after` (3) failed repairs in a row stop every repair until someone unfreezes it: **unfreeze**
  on the Self-repair page, or `noctyx heal --unfreeze`. `NoctyxHealFrozen` alerts while it is frozen. A success
  resets the count.
- **No retry when there is nothing left to try.** A repair that did all it safely could (a disk still full after
  the clean-up) goes to a person at once. It is not retried, and it does not count towards a freeze.
- **Off switch.** `"auto": false` turns every repair off: heal still checks and reports what it would do.
- **One at a time.** One heal run at a time (a run that finds another going stops). A repair holds the change
  lock, so it never overlaps a `noctyx upgrade`, and heal does nothing while one runs. A machine that is
  rebooting is left alone.
- **What it never touches.** It never stops, restarts or changes a container, a backup or a recipe deployment.
  The only exception is a model server you list in `restart_slots`, which it starts again. It never prunes Docker,
  never removes packages, kernels or models, and never restarts these units, whatever `restart_units` says:
  `docker*`, `containerd*`, `*bitcoin*`, model servers (`tensorfold*`, `vllm*`,
  `sglang*`, `ollama*`, `mtplx*`, `llama*`, `glm*`, `qwen*`, `deepseek*`, `nemotron*`), `ssh*`, `systemd-*`,
  `networking*`, `NetworkManager*`, `nvidia*`, `user@*`, and mounts, swaps, slices, scopes and targets.

## The policy

The site config's `heal` block ([site config](site-config.md#heal)). Set it on the console (**Configure ›
Self-repair**) or with `noctyx heal-set`, which merges the keys it is given over the stored ones:

```bash
sudo noctyx heal-set --json '{"restart_units": ["nginx.service", "myapp-*.service"]}'
sudo noctyx heal-set --json '{"restart_slots": ["rtx5090"]}'
sudo noctyx heal-set --json '{"upgrades": {"auto": true, "window": "03:00-05:00"}}'
sudo noctyx heal-set --json '{"auto": false}'        # report only
```

A bad value is refused with the reason. This includes a `restart_units` pattern that matches too much (fewer than
4 letters) or names a unit Noctyx never restarts.

### Disk

At `disk_pct` (default 92 %, the `HostRootFilesystemFull` threshold), heal does two things on that machine:

- `journalctl --vacuum-size=1G`;
- `apt-get clean`, which empties the cache of downloaded `.deb` files.

If that brings the disk under the limit, the repair worked. If not, the problem goes to a person with how much was
freed and the machine's largest directories. Heal does not clean that disk again for 6 hours.

It never prunes Docker. `docker container prune` removes stopped containers, including ones someone means to
start again, and image prunes can take what a stopped container needs. Docker leftovers stay a manual
[hygiene](hygiene.md) item.

### Failed units

Each Linux machine is asked for its failed units (`systemctl list-units --failed`). The exporter is not used for
this, because it leaves out a unit whose file is gone.

- **Unit file gone** (a ghost): its failed state is cleared with `systemctl reset-failed`. Nothing runs.
- **Listed in `restart_units`** (names or shell patterns, e.g. `myapp-*.service`), and not a unit Noctyx never
  restarts: `reset-failed`, then `restart`. It must then be active, or have succeeded if it is a oneshot.
- **Anything else:** left to a person, with its last log lines.

Noctyx's own units are the `service` playbook's.

### Model servers

A recipe slot listed in `restart_slots` is started again with `noctyx recipe-start --slot` (under the slot's GPU
lock, waiting for its health check). Heal does this only when all of these hold:

- the slot has not answered for 15 minutes;
- it was not stopped through Noctyx (`noctyx recipe-stop` or the Recipes page): a stopped slot stays stopped;
- nobody else is deploying, stopping or starting it;
- within the last 7 days, heal has seen the slot answer while **the slot's own unit** was active, and with the
  models its deployment expects.

The last rule keeps heal away from a slot whose server was swapped by hand. Restarting the slot's recorded unit
would put the old model back. A slot that answers from something other than its own unit says so in the log, and
heal never starts it.

### Automatic upgrades

**Off by default.** An upgrade can go wrong in ways a check cannot see, so turn it on only when you want it. With
`upgrades.auto` on, a component in `upgrades.components` that is older than Noctyx's pin is upgraded with
`noctyx upgrade` ([upgrade](upgrade.md#component-versions)). It is a change that checks itself and rolls back.

- Only inside `upgrades.window` (default `03:00-05:00`, the head's local time; it may wrap midnight).
- Only Noctyx's own pin, and only what Noctyx upgrades as a change: `node_exporter`, `prometheus`,
  `alertmanager`. Restic, the agent and Noctyx itself stay manual.
- One machine per run, nodes before the head. After an upgrade, the next machine waits `soak_minutes` (60).
- A new major version (Prometheus 2 → 3) waits for a person unless `upgrades.major` is on.
- A release that fails anywhere (rolled back) is not tried again on its own. The problem says where it failed and
  when; `noctyx upgrade` by hand is the way on.
- It needs a versions check from the last 2 days (the nightly run makes one).

## Alerts wake it

The console's Alertmanager webhook runs `noctyx heal --host <machine>` when one of these fires, at most once per
machine every 2 minutes. It does not when `auto` is off or the playbook is off.

| Alert | Playbook |
|---|---|
| `HostRootFilesystemFull` | `disk` |
| `HostSystemdUnitFailed` | `units` |
| `NodeExporterDown` | `exporter` |
| `BackupJobFailed` | `backups` |
| `RestEndpointDown` | `nas` |
| `LLMServerDown` | `slots` |
| `TickTimerInactive` | `service` |
| `ComponentBehind` | `upgrades` |

The Alerts panel shows, under each of these alerts, what self-repair says about it (repaired, waiting, shadow, or
needs a person, and why), with a link to the log.

## The log

`<state>/heal-log.jsonl`, one line per run. It moves to `heal-log.jsonl.1` at 8 MiB, and one old file is kept.

A run records:
- its trigger (`timer`, `alert:<name>`, `console`, `cli`);
- each check's duration;
- its steps:
  - `found`: what a check found;
  - `decide`: why something was not repaired now, such as the budget, a freeze, shadow mode or no safe repair;
  - `repair` and `result`: what was done and how it ended;
  - `cmd`: every command, with its exit code and output;
  - `change`: an upgrade's own progress;
  - `note`: something that waits, such as a service not answering for less than 10 minutes, or an upgrade
    outside its window;
- a summary.

A run that found nothing keeps only its timings.

The **Self-repair** page (Monitor › Self-repair, `/heal`) shows:
- whether repairs are on and whether they are frozen (with **unfreeze**);
- the repairs this hour against the budget;
- whether automatic upgrades are on;
- the open problems with their state and why;
- the last day's repairs;
- each playbook's mode;
- the log, newest first. Filter it by machine, playbook or text, or include the runs that found nothing. Open a run
  for its steps, and a command for its output.

`GET /api/v1/heal/log?n=50&host=&check=&all=1&q=&run=` returns the same runs.

## Reported

- **Audit trail.** Every repair goes to the audit trail (`event: heal`, in Jobs / Audit) and to `<state>/heal.json`.
  So do a freeze (`heal-freeze`), an unfreeze (`heal-unfreeze`), an alert that woke it (`heal-wake`) and a policy
  change (`heal-set`).
- **Metrics.** The console exports:
  - `noctyx_heal_problem`;
  - `noctyx_heal_repairs_24h`;
  - `noctyx_heal_last_run_timestamp_seconds`;
  - `noctyx_heal_frozen`;
  - `noctyx_heal_actions_1h`;
  - `noctyx_heal_auto`.
- **Alerts.**

  | Alert | Fires when |
  |---|---|
  | `NoctyxNeedsAttention` | a problem needs a person. Disk, units, backups, model servers and upgrades have their own alerts, which show self-repair's state, so they are left out |
  | `NoctyxHealFrozen` | repairs are frozen |
  | `NoctyxRepairedItself` (info) | there were repairs in the last day |
  | `NoctyxHealNotRunning` | the timer has stopped |

- **Elsewhere.** The morning brief and Ask Noctyx (its `repairs` tool) read the same state. `GET /api/v1/heal`
  returns the open problems, the day's repairs, the changes ledger and the policy in effect.

## Trying a playbook first

Put it in shadow mode (`{"playbooks": {"units": "shadow"}}`) and read what it would have done in the log for a
while. Or run `noctyx heal --dry-run`: it changes nothing and leaves `heal.json` alone, and its run is logged with
what it would do.
