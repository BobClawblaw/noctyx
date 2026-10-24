"""The thresholds of the fleet's early warnings (metrics/rules.yml, the "signals" group): the site config's
"signals" block over DEFAULTS, rendered into the alert rules as @SIG_*@. noctyx signals-set changes them, and
noctyx heal puts the rendered rules in place within 5 minutes.

    key                           alert                     fires when
    full_within_days              StorageFullSoon           a filesystem fills within this many days at its weekly rate
    growth_gb_per_hour            StorageGrewFast           a filesystem grew by more than this in an hour
    nfs_slow_ratio                NfsSlow                   the probe's write or read is below this share of its 7-day median
    nfs_op_seconds                NfsSlow                   a 4 KiB create+fsync+remove takes longer than this
    endpoint_seconds              RestEndpointSlow          the NAS backup endpoint takes longer than this to answer
    ssh_failed_per_15m            SshFailedLoginsBurst      more failed or invalid-user sshd logins than this in 15 min
    fabric_retransmits_per_second FabricRetransmits         a RoCE port retransmits (or times out, or reorders) this often
"""
DEFAULTS = {"full_within_days": 7, "growth_gb_per_hour": 200, "nfs_slow_ratio": 0.25, "nfs_op_seconds": 0.5,
            "endpoint_seconds": 2, "ssh_failed_per_15m": 20, "fabric_retransmits_per_second": 1}
LIMITS = {"full_within_days": (1, 90), "growth_gb_per_hour": (1, 100000), "nfs_slow_ratio": (0.01, 0.99),
          "nfs_op_seconds": (0.001, 60), "endpoint_seconds": (0.1, 60), "ssh_failed_per_15m": (1, 100000),
          "fabric_retransmits_per_second": (0.001, 1000000)}


def policy(c):
    got = c.get("signals") or {}
    return {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS}}


def validate(p):
    """-> (clean, errors) of a "signals" block."""
    if not isinstance(p, dict):
        return None, ["signals: an object"]
    errs, out = [], {}
    for k, v in p.items():
        if k not in DEFAULTS:
            errs.append(f"{k}: unknown (known: {', '.join(DEFAULTS)})")
            continue
        lo, hi = LIMITS[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            errs.append(f"{k}: a number from {lo} to {hi}")
        out[k] = v
    return out, errs


def placeholders(c):
    """{SIG_KEY: "value"} for render_site."""
    return {f"SIG_{k.upper()}": f"{v:g}" for k, v in policy(c).items()}
