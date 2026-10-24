# Early warnings

Alerts that fire before something breaks, from what the machines already report. They cover:
- NAS and NFS performance;
- the RoCE link between the GPU pairs;
- ssh logins;
- how fast storage fills.

They arrive like every other Noctyx alert: the Alerts panel, the morning brief, Alertmanager's receivers.
Each machine's numbers are in its Machine Report, on the **health** tab.

| Alert | Fires when | Severity |
|---|---|---|
| `NfsMountHung` | the NFS probe has waited over 3 min for a mount (a hung mount: anything that touches it waits) | critical |
| `NfsProbeFailing` | the probe cannot write, read or remove its file on a mount, for 10 min | warning |
| `NfsSlow` | the probe's write or read falls below `nfs_slow_ratio` (25 %) of its 7-day median, or a small-file round trip takes over `nfs_op_seconds` (0.5 s), for 30 min, while no backup holds the NAS | warning |
| `NfsRetransmitting` | over 1 % of a machine's NFS calls are retransmitted, for 15 min | warning |
| `RestEndpointSlow` | the NAS backup endpoint takes over `endpoint_seconds` (2 s) to answer, for 15 min | warning |
| `FabricLinkDown` | a RoCE port that was up over half of the last week is down | critical |
| `FabricLinkSlow` | an active RoCE port runs below the rate it had this week (it renegotiated: cable or port) | warning |
| `FabricLinkErrors` | a RoCE port logged link drops, link recoveries, symbol errors or receive errors in the last hour | warning |
| `FabricRetransmits` | a RoCE port retransmits, times out or reorders more than `fabric_retransmits_per_second` (1/s), for 10 min | warning |
| `SshFailedLoginsBurst` | more than `ssh_failed_per_15m` (20) failed or unknown-user ssh logins on a machine in 15 min | warning |
| `SshLoginFromNewAddress` | a login was accepted from an address no login came from before (for an hour) | warning |
| `StorageFullSoon` | a filesystem under half free fills within `full_within_days` (7) at its rate of the last week, for 6 hours | warning |
| `StorageGrewFast` | a filesystem grew by more than `growth_gb_per_hour` (200 GB) in the last hour | info |

These come on top of the alerts that were already there: `NasLowSpace`, `NasRaidDegraded`, `RestEndpointDown` and
`HostRootFilesystemFull`.

## Where the numbers come from

**NFS mounts.** On every machine with a read-write NFS mount, the textfile collector starts a probe every 5
minutes as its own transient unit, `<textfile_unit>-nfsprobe`. For each mount the probe:
1. writes 64 MiB, direct and fsynced;
2. reads it back, direct;
3. does ten 4 KiB create, fsync and remove round trips, which is what a backup's lock and index files cost;
4. removes its file.

It works in `<mount>/.<unit_prefix>-probe/<host>`, and every step has a timeout. Because the probe is its own
unit, a hung mount stalls it and never the collector. The collector reports how long a running probe has been
going (`noctyx_nfs_probe_running_seconds`), and that is what `NfsMountHung` reads.

The metrics are:
- `noctyx_nfs_probe_write_bytes_per_second`;
- `noctyx_nfs_probe_read_bytes_per_second`;
- `noctyx_nfs_probe_small_op_seconds`;
- `noctyx_nfs_probe_ok`.

Each is labelled with the mount and its source. node_exporter's `node_nfs_rpc_retransmissions_total` and
`node_nfs_rpcs_total` give the retransmissions.

**The NAS endpoint.** The console times its check of the backup endpoint (`noctyx_rest_endpoint_seconds`).

**The RoCE fabric.** node_exporter's InfiniBand collector reads each ConnectX port: its state, rate, error counters,
and RoCE retransmit, timeout and sequence counters (`node_infiniband_*`). A GB10 pair talks over this link. The
Spark's NVLink is inside the chip, so `nvidia-smi nvlink` has nothing to show there.

**ssh.** The collector reads sshd's journal since the last run (it keeps a cursor), and counts:
- failed logins (`noctyx_ssh_failed_logins_total`);
- logins for users that do not exist (`noctyx_ssh_invalid_user_attempts_total`);
- accepted logins by method (`noctyx_ssh_accepted_logins_total`).

It keeps the addresses a login was ever accepted from. On its first run, the last 30 days' addresses count as
known. After that, an accepted login from a new address appears for a week as
`noctyx_ssh_new_source_login_timestamp_seconds{addr, user}`. The 20 addresses with the most failures are in
`noctyx_ssh_failed_by_source_total`.

**Storage.** node_exporter's filesystem metrics cover local disks and NFS mounts, so the NAS volume is covered
through the head's mount. A Mac's APFS system volumes share one container with `/`, so only `/` and its other
volumes are watched.

Linux machines get all of this through the node collector (`noctyx tool install-node-metrics`). A Mac reports
storage only.

## Thresholds

The site config's `signals` block, set with `noctyx signals-set`. Every key is optional:

| Key | Default |
|---|---|
| `full_within_days` | `7` |
| `growth_gb_per_hour` | `200` |
| `nfs_slow_ratio` | `0.25` |
| `nfs_op_seconds` | `0.5` |
| `endpoint_seconds` | `2` |
| `ssh_failed_per_15m` | `20` |
| `fabric_retransmits_per_second` | `1` |

```bash
sudo noctyx signals-set --json '{"growth_gb_per_hour": 500, "full_within_days": 3}'
sudo noctyx heal          # the alert rules with the new values now (else at heal's next run, within 5 minutes)
```

`StorageFullSoon` follows the week's trend and waits 6 hours, so a download or benchmark run that is cleaned up
again does not set it off; a disk that keeps filling for days does. `StorageGrewFast` (info) does show such a run.
Silence it on the Alerts panel for the run, or raise `growth_gb_per_hour`.
