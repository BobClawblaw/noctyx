"""Safe host reboots: refuses while backups, restores, dpkg, or Noctyx jobs are active, tracks LLM impact,
and verifies post-boot recovery (LLMs serving again + hygiene rescan)."""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.parse

from noctyx import hostos
from noctyx.paths import P


def _cm():
    from noctyx import orchestrator as cm
    return cm


def reboot_pending(name=None):
    """Pending reboot records ({host: rec}); a record older than an hour is a waiter that died."""
    cm = _cm()
    out = {}
    for f in (cm.REBOOT_DIR.glob("*.json") if cm.REBOOT_DIR.is_dir() else []):
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        rec["stale"] = time.time() - rec.get("ts", 0) > 3600
        out[rec.get("host", f.stem)] = rec
    return out.get(name) if name else out


def _llm_on(topo, name):
    return (topo.get("hosts", {}).get(name) or {}).get("servers", [])


def llm_impact(c, h):
    """What stops serving if h goes down: its own servers and the other half of a TP pair."""
    cm = _cm()
    try:
        topo = json.loads(cm.LLM_TOPO.read_text())
    except Exception:
        return [], []
    lines, models = [], []
    for s_ in _llm_on(topo, h["name"]):
        if s_.get("role") == "serving":
            lines.append(f"serves {s_.get('model') or '?'} ({s_['engine']}, port {s_.get('port')})")
            models.append({"host": h["name"], "model": s_.get("model"), "engine": s_["engine"]})
            for wn, d in topo.get("hosts", {}).items():
                for w in d.get("servers", []):
                    if w.get("role") == "worker" and w.get("worker_for") == h["name"]:
                        lines.append(f"{wn} is its tensor-parallel worker: idle until {h['name']} is back")
        elif s_.get("role") == "worker" and s_.get("worker_for"):
            hd = s_["worker_for"]
            lines.append(f"tensor-parallel worker for {hd}: {s_.get('model') or '?'} stops on {hd} too"
                         f" (it may need both ranks restarted)")
            models.append({"host": hd, "model": s_.get("model"), "engine": s_["engine"]})
    au = urllib.parse.urlparse((c.get("assistant") or {}).get("url") or "")
    if au.hostname and any(s_.get("target") == f"{au.hostname}:{au.port}" for s_ in _llm_on(topo, h["name"])):
        lines.append("the Noctyx assistant (explain / brief) falls back to another model while it is down")
    return lines, models


def reboot_plan(c, name):
    """{host, head, allowed, blockers, warnings, impact} — the UI's confirm dialog and the
    CLI's last check before rebooting both read this."""
    cm = _cm()
    h = next((x for x in cm.hosts(c) if x["name"] == name), None)
    if not h:
        return {"host": name, "allowed": False, "blockers": [f"unknown host {name}"], "warnings": [], "impact": []}
    head = cm.is_head(c, h)
    plan = {"host": name, "head": head, "allowed": False, "blockers": [], "warnings": [], "impact": []}
    B, W = plan["blockers"], plan["warnings"]
    if h.get("os") == "macos":
        B.append("macOS host: monitored only, Noctyx does not reboot it")
    if not h.get("address"):
        B.append("no reachable address / not a managed Linux host")
    if B:
        return plan
    pend = reboot_pending(name)
    if pend and not pend.get("stale"):
        B.append(f"a reboot of {name} is already in progress (requested {pend.get('requested', '?')})")
    try:
        r = cm._on(c, h, f"fuser {P['node_lib']}/agent.lock 2>/dev/null; echo '|';"
                         " fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock 2>/dev/null; echo '|';"
                         f" {hostos.BOOT_ID_CMD}", timeout=25)
        parts = (r.stdout or "").split("|")
        if r.returncode != 0 or len(parts) < 3:
            raise RuntimeError((r.stderr or f"rc={r.returncode}").strip()[-120:])
        if parts[0].strip():
            B.append(f"a backup/restore is writing on {name} right now (agent.lock held)")
        if parts[1].strip():
            B.append(f"the package manager is running on {name} (dpkg lock held): a reboot mid-upgrade can leave it unbootable")
        plan["boot_id"] = parts[2].strip()
    except Exception as e:
        B.append(f"cannot reach {name}: {str(e)[:120]}")
    units = [u for u in cm._active_job_units(timer_jobs=head) if not u.startswith(f"{cm.UNIT}-reboot-")]
    mine = [u for u in units if f"-{name}-" in u]
    if mine:
        B.append(f"Noctyx job running for {name}: {', '.join(mine)}")
    if head:
        others = [u for u in units if u not in mine]
        if others:
            B.append(f"a Noctyx job is running on the head node and would be killed: {', '.join(others)}"
                     f" — try again when it ends (Runs shows it)")
        plan["impact"] += ["the Noctyx console, Prometheus and Alertmanager go down until it is back",
                           "scheduled backups pause; anything due runs on the first tick after boot",
                           "this page reconnects by itself"]
    else:
        fleet = [u for u in units if not cm._JOB_UNIT.match(u).group(2)]
        if fleet:
            W.append(f"a fleet-wide job is running ({', '.join(fleet)}): if it reaches {name} while it is down, "
                     f"{name} is skipped and retried on the next tick")
    lines, _ = llm_impact(c, h)
    plan["impact"] = lines + plan["impact"]
    plan["allowed"] = not B
    return plan


def _wait_llm(c, models, budget=600):
    """Rerun discovery until every model that was serving is serving again, or the budget ends."""
    cm = _cm()
    if not models:
        return [], []
    t0 = time.time()
    while True:
        cm._llm_refresh(wait=True)
        try:
            topo = json.loads(cm.LLM_TOPO.read_text())
        except Exception:
            topo = {}
        back = [m for m in models if any(s_.get("role") == "serving" and s_.get("target") and s_.get("model") == m["model"]
                                          for s_ in _llm_on(topo, m["host"]))]
        missing = [m for m in models if m not in back]
        if not missing or time.time() - t0 > budget:
            return back, missing
        print(f"  waiting for {', '.join(m['host'] + ' ' + str(m['model']) for m in missing)} "
              f"({int(time.time() - t0)}s)", flush=True)
        time.sleep(60)


def _reboot_finish(c, h, rec):
    """Everything after the host is back: LLMs, a Hygiene rescan, the audit line."""
    cm = _cm()
    back, missing = _wait_llm(c, rec.get("models") or [])
    for m in back:
        print(f"  ✓ {m['host']} serving {m['model']} again")
    for m in missing:
        print(f"  ✗ {m['host']} {m['model']} NOT serving after 10 min — check its service (TP pairs may need both ranks restarted)")
    print("hygiene rescan:")
    try:
        cm.cmd_hygiene(argparse.Namespace(host=h["name"], fix=None))
    except SystemExit:
        pass
    except Exception as e:
        print(f"  hygiene rescan failed: {e}")
    cm.audit("reboot", host=h["name"], rc=0 if not missing else 1, requested=rec.get("requested"),
             down_s=rec.get("down_s"), back_s=rec.get("back_s"),
             llm_back=[m["model"] for m in back], llm_missing=[m["model"] for m in missing])
    try:
        (cm.REBOOT_DIR / f"{h['name']}.json").unlink()
    except FileNotFoundError:
        pass
    print(f"reboot {h['name']}: done" + (f" — {len(missing)} model(s) not back" if missing else ""))
    if missing:
        sys.exit(1)


def cmd_reboot(a):
    """Reboot a host — refused while anything Noctyx cares about is in flight there.

    --plan     print what blocks it and what goes down with it (JSON), change nothing
    --yes      do it (re-checks the plan first); remote: waits for the new boot and checks LLMs
    --finish   the head node's post-boot half (the UI runs this when it starts on a new boot)
    """
    cm = _cm()
    c = cm.cfg()
    h = cm.host_by_name(c, a.host)
    if a.plan:
        print(json.dumps(reboot_plan(c, h["name"]), indent=1))
        return
    if a.finish:
        rec = reboot_pending(h["name"])
        if not rec:
            sys.exit(f"no pending reboot for {h['name']}")
        now_id = cm.boot_id(c, h)
        if now_id and now_id == rec.get("boot_id"):
            sys.exit(f"{h['name']} has not rebooted yet (same boot_id)")
        try:
            booted = hostos.boot_time() if cm.is_head(c, h) else None
        except Exception:
            booted = None
        if booted and booted >= rec["ts"]:
            rec["down_s"] = round(booted - rec["ts"])
            print(f"{h['name']} booted {datetime.datetime.fromtimestamp(booted, datetime.timezone.utc):%H:%M:%SZ}, "
                  f"{rec['down_s']}s after the reboot was requested")
        _reboot_finish(c, h, rec)
        return
    if not a.yes:
        sys.exit("reboot takes the host down; add --yes if you mean it (see --plan first)")
    plan = reboot_plan(c, h["name"])
    for w in plan["warnings"]:
        print(f"  ! {w}")
    if not plan["allowed"]:
        for b in plan["blockers"]:
            print(f"  REFUSED: {b}")
        cm.audit("reboot-refused", host=h["name"], blockers=plan["blockers"])
        sys.exit(2)
    for line in plan["impact"]:
        print(f"  · {line}")
    _, models = llm_impact(c, h)
    head = plan["head"]
    delay = cm.REBOOT_DELAY["head" if head else "remote"]
    rec = {"host": h["name"], "head": head, "ts": time.time(), "requested": datetime.datetime.now(
           datetime.timezone.utc).isoformat(timespec="seconds"), "boot_id": plan.get("boot_id"), "models": models}
    cm.REBOOT_DIR.mkdir(parents=True, exist_ok=True)
    (cm.REBOOT_DIR / f"{h['name']}.json").write_text(json.dumps(rec, indent=1))
    unit = f"{cm.UNIT}-host-reboot-{int(time.time())}"
    r = cm._on(c, h, f"systemd-run --quiet --collect --unit={unit} --on-active={delay} "
                     f"--description='Noctyx: reboot requested from the console/CLI' systemctl reboot", timeout=30)
    if r.returncode != 0:
        (cm.REBOOT_DIR / f"{h['name']}.json").unlink()
        cm.audit("reboot", host=h["name"], rc=1, error=(r.stderr or "").strip()[-200:])
        sys.exit(f"could not schedule the reboot on {h['name']}: {(r.stderr or '').strip()[-200:]}")
    cm.audit("reboot-requested", host=h["name"], head=head, delay_s=delay, impact=plan["impact"])
    print(f"{h['name']}: reboot in {delay}s", flush=True)
    if head:
        print("the head node cannot watch itself reboot: the UI finishes this job (LLMs, hygiene, audit) "
              "when it starts on the new boot")
        return
    # remote: down (ssh fails or boot_id changes) then up with a new boot_id
    t0, down_at, old = time.time(), None, rec["boot_id"]
    while time.time() - t0 < 1500:
        time.sleep(10)
        bid = cm.boot_id(c, h, timeout=12)
        if bid is None:
            if down_at is None:
                down_at = time.time()
                print(f"  down after {int(down_at - t0)}s", flush=True)
            continue
        if bid != old:
            rec["down_s"] = int((down_at or time.time()) - t0)
            rec["back_s"] = int(time.time() - t0)
            print(f"  back after {rec['back_s']}s (new boot)", flush=True)
            break
    else:
        cm.audit("reboot", host=h["name"], rc=1, error="not back with a new boot after 25 min")
        sys.exit(f"{h['name']} is not back with a new boot after 25 min — check its console/BMC")
    _reboot_finish(c, h, rec)
