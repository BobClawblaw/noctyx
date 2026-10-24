# Self-repair

`noctyx heal` compares what Noctyx manages with what it expects and repairs what it safely can. The heal timer
(`noctyx-heal.timer`, installed by `noctyx install-head --apply`) runs it every 5 minutes; the console's
**repair now** button runs it at once.

```bash
sudo noctyx heal --dry-run      # what it finds and what it would do; repairs nothing
sudo noctyx heal                # repair what it can now
```

| What | Broken when | Repaired by |
|---|---|---|
| the head's services | a Noctyx timer or long-running service, Prometheus or Alertmanager is stopped or failed, or runs but has not answered for 10 minutes | a restart |
| a node's exporter | Prometheus cannot scrape it | undoing Noctyx's own change of it from the last day (see [upgrade](upgrade.md#every-upgrade-checks-itself-and-rolls-back)); else restarting an exporter that is stopped |
| the exporter's drop-in | differs from [`metrics/node-exporter.conf`](../../src/noctyx/metrics/node-exporter.conf) on a machine running Noctyx's exporter | writing it again, as a change: the exporter must then scrape within 5 s, or the old file goes back |
| the textfile collector | its file (`paths.textfile_name`) is more than 5 minutes old | restarting its timer |
| the alert rules | `<prefix>-rules.yml` beside Prometheus's config differs from this Noctyx's rules | writing them again after `promtool check rules`, then a reload; the old file goes back if Prometheus does not load them all |

The drop-in repair covers the case where an exporter was upgraded with an older drop-in: it still answers, but
every scrape times out. The rules repair is also how a Noctyx upgrade's new or changed alert rules reach
Prometheus.

**Bounded.** Each thing is repaired at most 3 times in 6 hours. After that, and at once for anything with no safe
repair (an exporter that runs but cannot be scraped, a machine ssh cannot reach), it stays a problem that needs a
person: the `NoctyxNeedsAttention` alert names it and carries a **repair now** button.

**Careful.** It never stops, restarts or changes a model server, a container, a recipe or a backup. It waits while
a change (`noctyx upgrade`) runs, and leaves a machine alone while it reboots.

**Reported.** Every repair goes to the audit trail (`event: heal`, in Jobs / Audit) and to `<state>/heal.json`.
The console exports `noctyx_heal_problem`, `noctyx_heal_repairs_24h` and `noctyx_heal_last_run_timestamp_seconds`;
`NoctyxRepairedItself` (info) shows the repairs of the last day, `NoctyxHealNotRunning` fires when the timer has
stopped, and the morning brief mentions both. `GET /api/v1/heal` returns the open problems, the day's repairs and
the changes ledger.
