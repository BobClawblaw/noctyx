"""Checkpoints: a known state of a machine kept before something changes it, to go back to.

- Before a Noctyx change on a machine (noctyx upgrade, an automatic upgrade, a revert), the change waits for a
  checkpoint: the machine's newest system snapshot when it is younger than max_age_hours, else a new one,
  tagged "checkpoint" and "before-change". The change records which snapshot it was (changes.jsonl).
- Before apt changes packages on a node (the hook in /etc/apt/apt.conf.d), the same, tagged "before-apt"; after
  apt, a "checkpoint-after" line in the node's audit marks the end.
- After a change passes its own check, its ledger record is the machine's known good state for that
  component (noctyx changes).

The site config's "checkpoints" block sets it (DEFAULTS); a checkpoint that cannot be made (the repository away,
a backup holding the NAS, the timeout) does not stop the change unless "require" is set, and says why.

    noctyx checkpoint --host H|all          take one now
    noctyx checkpoints [--host H]           the checkpoint snapshots and what each was taken for
    noctyx checkpoint-set --json '{...}'    the policy, and the apt hook put on (or taken off) the nodes
"""
import datetime
import json
import re
import shlex
import time

DEFAULTS = {"before_changes": True, "before_apt": True, "max_age_hours": 6, "timeout_minutes": 45,
            "apt_timeout_minutes": 20, "require": False}
REASONS = ("change", "apt", "manual")


def _cm():
    from noctyx import orchestrator as cm
    return cm


def policy(c):
    got = c.get("checkpoints") or {}
    return {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS}}


def validate(p):
    """-> (clean, errors) of a "checkpoints" block."""
    if not isinstance(p, dict):
        return None, ["checkpoints: an object"]
    errs, out = [], {}
    for k, v in p.items():
        if k not in DEFAULTS:
            errs.append(f"{k}: unknown (known: {', '.join(DEFAULTS)})")
        elif isinstance(DEFAULTS[k], bool):
            if not isinstance(v, bool):
                errs.append(f"{k}: true or false")
            out[k] = v
        else:
            lo, hi = {"max_age_hours": (0, 168), "timeout_minutes": (1, 240), "apt_timeout_minutes": (1, 120)}[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                errs.append(f"{k}: a whole number from {lo} to {hi}")
            out[k] = v
    return out, errs


def _backed_up(c, host):
    cm = _cm()
    name = host.get("name") if isinstance(host, dict) else host
    return next((h for h in cm.backup_hosts(c) if h["name"] == name), None)


def take(c, host, reason="manual", max_age=None, timeout=None, log=print):
    """A checkpoint of one machine -> {snapshot, taken, age, seconds?, error?} or {skipped: why}."""
    cm = _cm()
    pol = policy(c)
    h = _backed_up(c, host)
    if not h:
        return {"skipped": "Noctyx does not back this machine up"}
    max_age = pol["max_age_hours"] * 3600 if max_age is None else max_age
    timeout = pol["timeout_minutes"] * 60 if timeout is None else timeout
    sp = h.get("spindle", cm.nas_name(c))
    t0 = time.time()
    try:
        with cm.spindle_lock(sp, what=f"checkpoint {h['name']}"):
            r = cm.ssh(c, h, f"{cm.AGENT} checkpoint --reason {shlex.quote(reason)} --max-age {int(max_age)} "
                             f"--timeout {int(timeout)}", timeout=timeout + 180)
    except cm.SpindleBusy as e:
        # a backup is writing to the NAS: wait for nothing, use the newest snapshot there is
        r = cm.ssh(c, h, f"{cm.AGENT} checkpoint --reason {shlex.quote(reason)} --max-age {10 ** 9} --timeout 1",
                   timeout=120)
        got = _result(r)
        got.setdefault("note", f"no new snapshot: {e}")
        return got
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "seconds": round(time.time() - t0)}
    got = _result(r)
    got["seconds"] = round(time.time() - t0)
    return got


def _result(r):
    lines = _cm().result_lines(r.stdout or "")
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("event") == "checkpoint":
            return {k: d.get(k) for k in ("snapshot", "taken", "age", "error", "reason", "incomplete")
                    if d.get(k) is not None}
    return {"error": ((r.stderr or "").strip().splitlines() or [f"the agent exited {r.returncode}"])[-1][-200:]}


def before_change(ch, log=print):
    """change.run calls this between the preflight and the snapshot of a change whose class asks for one.
    -> the record's "checkpoint" (None when the policy turns them off). Raises change.Refused when it is
    required and could not be made."""
    from noctyx import change
    c = ch.c
    pol = policy(c)
    if not pol["before_changes"]:
        return None
    log(f"· checkpoint of {ch.host['name']} (a system snapshot from the last {pol['max_age_hours']} h, or a new one)")
    got = take(c, ch.host, "change", log=log)
    if got.get("skipped"):
        log(f"  no checkpoint: {got['skipped']}")
    elif got.get("error") or not got.get("snapshot"):
        why = got.get("error") or "no snapshot"
        log(f"  ✗ no checkpoint: {why}")
        if pol["require"]:
            raise change.Refused(f"no checkpoint of {ch.host['name']} ({why}) and checkpoints.require is on")
    else:
        log(f"  checkpoint: snapshot {got['snapshot']} " + ("(taken now)" if got.get("taken") else
                                                          f"({int(got.get('age') or 0) // 60} min old)")
            + (f" · {got['note']}" if got.get("note") else "") + (f" · {got['incomplete']}" if got.get("incomplete") else ""))
    return got


def listing(c, host=None):
    """{host: [{id, time, reasons, tags}] newest first, or {"error"}} of the snapshots tagged checkpoint, and the
    changes each one was taken before (from the ledger)."""
    from noctyx import change
    cm = _cm()
    by_snap = {}
    for r in change.ledger(1000):
        s = (r.get("checkpoint") or {}).get("snapshot")
        if s:
            by_snap.setdefault(s, []).append({k: r.get(k) for k in ("id", "what", "outcome", "ts")})
    out = {}
    for h in cm.backup_hosts(c):
        if host and h["name"] != host:
            continue
        try:
            r = cm.ssh(c, h, f"{cm.AGENT} snapshots --tag checkpoint --json", timeout=120)
            snaps = json.loads(r.stdout or "[]") if r.returncode == 0 else None
        except Exception as e:
            out[h["name"]] = {"error": str(e)[:160]}
            continue
        if snaps is None:
            out[h["name"]] = {"error": ((r.stderr or "").strip().splitlines() or ["?"])[-1][-160:]}
            continue
        rows = [{"id": s.get("short_id"), "time": _utc(s.get("time")),
                 "reasons": [t[len("before-"):] for t in s.get("tags") or [] if t.startswith("before-")],
                 "changes": by_snap.get(s.get("short_id"), [])} for s in snaps]
        out[h["name"]] = sorted(rows, key=lambda x: x["time"] or "", reverse=True)
    # changes whose checkpoint was a scheduled snapshot (not tagged checkpoint) are in the ledger only
    return out


def _utc(t):
    """restic's time (the machine's own zone, nanoseconds) -> ISO UTC."""
    try:
        d = datetime.datetime.fromisoformat(re.sub(r"\.\d+", "", t).replace("Z", "+00:00"))
        return d.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return t


# ── the apt hook on the nodes ───────────────────────────────────────────────
def hook_path():
    from noctyx.paths import P
    return f"/etc/apt/apt.conf.d/80{P['unit_prefix']}-checkpoint"


def hook_text(c):
    from noctyx.paths import P
    pol = policy(c)
    agent = P["agent_bin"]
    t = pol["apt_timeout_minutes"] * 60
    pre = (f"[ -x {agent} ] && {agent} checkpoint --reason apt --max-age {pol['max_age_hours'] * 3600} "
           f"--timeout {t} >/dev/null 2>&1 || true")
    post = f"[ -x {agent} ] && {agent} checkpoint-after --reason apt >/dev/null 2>&1 || true"
    assert '"' not in pre + post and "\\" not in pre + post        # an apt.conf string has no escapes
    return (f"// managed by Noctyx (noctyx checkpoint-set): a checkpoint of this machine before apt changes "
            f"packages,\n// at most {pol['apt_timeout_minutes']} min; a failed one never stops apt\n"
            f'DPkg::Pre-Invoke {{ "{pre}"; }};\nDPkg::Post-Invoke {{ "{post}"; }};\n')


def apply_hooks(c, host=None, log=print):
    """Put the apt hook on every backed-up node (before_apt on), or take it off. -> {host: ok/why}"""
    cm = _cm()
    on = policy(c)["before_apt"]
    want = hook_text(c)
    out = {}
    for h in cm.backup_hosts(c):
        if host and h["name"] != host:
            continue
        cmd = (f"[ -d /etc/apt/apt.conf.d ] || {{ echo 'no apt here'; exit 0; }}; printf %s {shlex.quote(want)} > "
               f"{hook_path()}.tmp && chmod 644 {hook_path()}.tmp && mv {hook_path()}.tmp {hook_path()} && "
               f"apt-config dump >/dev/null && echo installed"
               if on else f"rm -f {hook_path()} && echo removed")
        try:
            r = cm._on(c, h, cmd, timeout=60)
            msg = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            ok = r.returncode == 0
            if ok and on and "installed" not in (msg[-1:] or [""])[0]:
                ok = "no apt here" in " ".join(msg)
            if not ok and on:                       # a hook apt cannot parse would break apt: take it off
                cm._on(c, h, f"rm -f {hook_path()}", timeout=60)
            out[h["name"]] = (ok, msg[-1] if msg else f"rc {r.returncode}")
        except Exception as e:
            out[h["name"]] = (False, str(e)[:160])
        log(f"  {'✓' if out[h['name']][0] else '✗'} {h['name']}: {out[h['name']][1]}")
    return out

