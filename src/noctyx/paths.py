"""Every file, directory, unit and user name Noctyx touches, in one place.

Defaults are Noctyx's own layout. A site overrides any of them in its config's "paths" block, e.g. a
site that ran an earlier tool keeps its existing layout and moves later (or never). Environment
variables NOCTYX_PATH_<NAME> override both, for tests.

Head node (where the orchestrator and console run): config, keys, TLS, state, locks, logs, audit.
Nodes (every backup client): the agent, its config, hooks, dumps, lock.
"""
import json
import os
import pathlib

CONFIG = os.environ.get("NOCTYX_CONFIG", "/etc/noctyx/site.json")

DEFAULTS = {
    # head node
    "etc": "/etc/noctyx",                         # keys, tokens, TLS, rendered files
    "state": "/etc/noctyx/state",                 # scheduler / recipe / discovery state
    "locks": "/var/lock/noctyx",                  # per-spindle and per-slot locks
    "job_logs": "/var/log/noctyx/jobs",           # detached jobs' logs
    "audit": "/var/lib/noctyx/audit.jsonl",       # every action, one JSON line each
    "console_state": "/var/lib/noctyx-console",   # console caches: hygiene, probes, topology, jobs
    "tls": "/etc/noctyx/tls",                     # ca.crt, ca.key, server cert/key
    "repo_keys": "/etc/noctyx/repo-keys",         # <repo>.key (restic passwords), root-only
    "repo_creds": "/etc/noctyx/repo-creds",       # <prefix><repo>.pass (repository endpoint logins)
    "llm_keys": "/etc/noctyx/llm-keys",           # assistant / LLM endpoint keys, root-only
    "ui_token": "/etc/noctyx/ui-token",           # console bearer token
    "nas_rest": "/etc/noctyx/nas-rest",           # the NAS repository endpoint's TLS material
    "alert_env": "/etc/noctyx/alert-channel.env", # optional external alert channel
    "unit_prefix": "noctyx",                      # systemd units: <prefix>-backup-<host>-<ts>…, <prefix>-tick.timer
    "recipe_unit_prefix": "noctyx-recipe",        # <prefix>-<slot>.service on a slot's head
    "repo_owner": "noctyx",                       # local user that owns repository files on the NAS mount
    "repo_dir_prefix": "nx-",                     # <repo_root>/<prefix><repo>/<repo>
    "recipe_root": "~/.local/share/noctyx/recipes",
    "gpu_lock": "/var/lock/noctyx-gpu.lock",
    "mirror_unit": "noctyx-hf-mirror",            # the HF mirror's service
    "restore_script": "",                         # what /api/v1/restore-script serves ("" = the packaged one)
    "iso_dir": "/srv/noctyx/isos",                # cached Ubuntu live ISOs + restic binaries for recovery sticks
    "vmtest_dir": "/srv/noctyx/vmtest",           # VM boot tests of recovery media
    "rehearsal_dir": "/var/tmp/noctyx-rehearsal", # head-node rebuild rehearsal scratch (hundreds of GB)
    "stick_label": "NOCTYX",                      # the recovery stick's payload partition (FAT, max 11 chars)
    "live_label": "NOCTYXLIVE",                   # its ext4 live-image partition      # a slot's GPU lock when the slot names none
    # Prometheus wiring (the console's queries and the alert rules use these names)
    "prom_node_targets": "/etc/prometheus/noctyx-targets.json",   # file_sd for node_exporter on every node
    "prom_llm_targets": "/etc/prometheus/llm-targets.json",        # file_sd for LLM servers (llm-discover)
    "prom_job_console": "noctyx",                                  # job scraping the console's /metrics
    "prom_job_nodes": "noctyx-nodes",                              # job scraping node_exporter
    "metric_prefix": "noctyx",                                     # <prefix>_gpu_power_watts … in every metric Noctyx makes
    "textfile_name": "noctyx.prom",                                # node_exporter textfile the collectors write
    "textfile_unit": "noctyx-node-textfile",                       # the collector's binary and timer on every node
    "launchd_prefix": "dev.noctyx",                                # launchd labels on Macs
    # every node
    "agent_bin": "/usr/local/bin/noctyx-agent",
    "dumps_bin": "/usr/local/bin/noctyx-dumps",
    "node_etc": "/etc/noctyx",                    # client.conf, exclusions, profiles, hooks, CA
    "node_lib": "/var/lib/noctyx",                # agent lock, dumps, restores, fs metadata
    "result_token": "NOCTYX_RESULT",              # the agent's machine-readable result line
    "snapshot_tag": "noctyx",                     # tag on every snapshot the agent takes
    "agent_unit": "noctyx-backup",                # optional node-local timer unit
}


def load(config=CONFIG):
    try:
        site = (json.loads(pathlib.Path(config).read_text()).get("paths") or {})
    except (OSError, ValueError):
        site = {}
    p = {**DEFAULTS, **{k: v for k, v in site.items() if k in DEFAULTS}}
    for k in p:
        v = os.environ.get(f"NOCTYX_PATH_{k.upper()}")
        if v:
            p[k] = v
    return p


P = load()


def render(text, p=None):
    """Fill a node-side template (@AGENT_BIN@, @NODE_ETC@ …) with this site's paths."""
    p = p or P
    for k, v in p.items():
        text = text.replace(f"@{k.upper()}@", str(v))
    return text
