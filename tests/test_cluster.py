"""The head's core paths against a simulated cluster: when a backup is due, what a tick runs and records, why a
backup is refused or skipped, and a recipe deploy's switch, rollback and bookkeeping. No ssh, restic or GPU:
the nodes are a small stand-in that answers the agent's commands and keeps "snapshots"."""
import argparse
import datetime
import fcntl
import json
import subprocess

import pytest

from noctyx import orchestrator as o

DAY = 86400
NIGHT = "01:00-07:00"


def at(hh, mm=0, day=10):
    return datetime.datetime(2026, 3, day, hh, mm)


# ── when a backup is due (pure) ─────────────────────────────────────────────
def test_a_nightly_backup_runs_once_per_window():
    now = at(2).timestamp()
    assert o.due(now, at(2), None, DAY, NIGHT) == (True, "never backed up")
    assert o.due(now, at(12), None, DAY, NIGHT)[0] is False                       # outside the window: wait
    yesterday_night = at(1, 30, day=9).timestamp()
    assert o.due(now, at(2), yesterday_night, DAY, NIGHT)[0] is True              # last night's does not count
    daytime_run = at(15, day=9).timestamp()                                       # a manual run yesterday afternoon
    assert o.due(now, at(2), daytime_run, DAY, NIGHT)[0] is True                  # ... does not push tonight's out
    tonight = at(1, 10).timestamp()
    assert o.due(now, at(2), tonight, DAY, NIGHT) == (False, "already done this window")


def test_every_other_night_and_a_window_over_midnight():
    now = at(2).timestamp()
    assert o.due(now, at(2), at(1, 30, day=9).timestamp(), 2 * DAY, NIGHT)[0] is False   # every 2d: not tonight
    assert o.due(now, at(2), at(1, 30, day=8).timestamp(), 2 * DAY, NIGHT)[0] is True
    late = "22:00-04:00"
    assert o.in_window(at(23), late) and o.in_window(at(3), late) and not o.in_window(at(12), late)
    assert o.due(at(3).timestamp(), at(3), at(22, 30, day=9).timestamp(), DAY, late)[0] is False   # same night
    assert o.due(at(23).timestamp(), at(23), at(22, 30, day=9).timestamp(), DAY, late)[0] is True


def test_short_intervals_go_by_age_and_a_failure_waits_for_the_retry():
    now = at(12).timestamp()
    assert o.due(now, at(12), now - 5 * 3600, 6 * 3600, None)[0] is False
    assert o.due(now, at(12), now - 5 * 3600 - 2700, 6 * 3600, None, slack=900)[0] is True    # within one tick of due
    assert o.due(now, at(12), now - 7 * 3600, 6 * 3600, None)[0] is True
    ok, why = o.due(now, at(12), now - 7 * 3600, 6 * 3600, None, failed_at=now - 600)
    assert ok is False and why.startswith("failed 10m ago")
    assert o.due(now, at(12), now - 7 * 3600, 6 * 3600, None, failed_at=now - 3700)[0] is True


def test_health_measures_against_the_schedule_not_a_raw_age():
    now = at(12).timestamp()
    assert o.health(now, None, DAY, NIGHT) == "never"
    assert o.health(now, now - DAY - 17 * 3600, DAY, NIGHT) == "ok"       # the window alone can make it wait 18 h
    assert o.health(now, now - 2 * DAY - 17 * 3600, DAY, NIGHT) == "late"
    assert o.health(now, now - 4 * DAY, DAY, NIGHT) == "stale"
    assert o.window_gap(NIGHT) == 18 * 3600 and o.window_gap(None) == 0
    nxt = o.next_due(at(12).timestamp(), at(1, 30).timestamp(), DAY, NIGHT)
    assert datetime.datetime.fromtimestamp(nxt) == at(1, day=11)          # tomorrow, when the window opens


# ── the stand-in cluster ────────────────────────────────────────────────────
class Cluster:
    """Three nodes behind one NAS. `net`: Mbit/s per host (None: the probe fails). `backup`: what the agent's
    backup does per host: "ok", "fail", "locked" (a stale repo lock until unlocked), "busy" (the agent's own
    one-job lock). Snapshots it takes are what the scheduler then reads back."""

    def __init__(self):
        self.net = {"gpu-01": 900.0, "gpu-02": 900.0, "gpu-03": 900.0}
        self.backup = {"gpu-01": "ok", "gpu-02": "ok", "gpu-03": "ok"}
        self.snaps = {}                       # (host, profile) -> epoch
        self.unreadable = set()               # repo ids the head cannot read
        self.live_writer = None               # a host holding the shared repo's lock for real
        self.ran, self.unlocked = [], []

    def config(self):
        return {"server": {"hostname": "head", "address": "192.0.2.10", "directory_port": 20999,
                           "repo_root": "/mnt/nas/backups", "ssh": {"user": "ops", "key": "/k"}},
                "spindles": {"nas": {"mount": "/mnt/nas", "writers": 1}},
                "repositories": [{"id": "pair", "members": ["gpu-01", "gpu-02"]}, {"id": "solo", "members": ["gpu-03"]}],
                "hosts": [{"name": "gpu-01", "address": "192.0.2.21", "arch": "aarch64", "repo": "pair"},
                          {"name": "gpu-02", "address": "192.0.2.22", "arch": "aarch64", "repo": "pair"},
                          {"name": "gpu-03", "address": "192.0.2.23", "arch": "x86_64", "repo": "solo"},
                          {"name": "mac-01", "address": "192.0.2.30", "arch": "arm64", "os": "macos"}],
                "profiles": {"system": {}},
                "network": {"min_throughput_mbit": 40},
                "schedule": {"tick_minutes": 15, "retry_after": "1h", "defaults": {"system": {"every": "6h"}}}}

    def net_floor(self, c, h, probe_bytes=0):
        return self.net[h["name"]]

    def ssh_stream(self, c, h, cmd):
        name, how = h["name"], self.backup[h["name"]]
        self.ran.append(name)
        if how == "locked":
            return 1, "", "Fatal: unable to create lock in backend: repository is already locked by PID 1"
        if how == "busy":
            return 1, "", "another noctyx job is running on this host"
        if how == "fail":
            return 3, o.P["result_token"] + ' {"event": "backup", "rc": 3}\n', "Fatal: unable to open repository"
        self.snaps[(name, "system")] = __import__("time").time()
        return 0, o.P["result_token"] + ' {"event": "backup", "rc": 0, "snapshot": "abc123", "seconds": 5}\n', ""

    def ssh(self, c, h, cmd, timeout=0, capture=True):
        if " unlock" in cmd:
            self.unlocked.append(h["name"])
            self.backup[h["name"]] = "ok"
        out = "1234\n" if "fuser" in cmd and h["name"] == self.live_writer else ""
        return argparse.Namespace(stdout=out, stderr="", returncode=0)

    def last_success(self, c):
        errs = [f"{r}: NAS not mounted" for r in sorted(self.unreadable)]
        repo = {h["name"]: h.get("repo") for h in c["hosts"]}
        return {k: v for k, v in self.snaps.items() if repo[k[0]] not in self.unreadable}, errs


@pytest.fixture()
def cluster(tmp_path, monkeypatch):
    cl = Cluster()
    f = tmp_path / "site.json"
    f.write_text(json.dumps(cl.config(), indent=2))
    monkeypatch.setattr(o, "CFG_PATH", str(f))
    for name, path in (("STATE_DIR", tmp_path), ("SCHED_STATE", tmp_path / "schedule-state.json"),
                       ("AUDIT", tmp_path / "audit.jsonl"), ("LOCK_DIR", tmp_path / "locks"),
                       ("PROBE_FILE", tmp_path / "probe-last.json"), ("RECIPE_STATE", tmp_path / "recipes.json")):
        monkeypatch.setattr(o, name, path)
    for name in ("net_floor", "ssh_stream", "ssh", "last_success"):
        monkeypatch.setattr(o, name, getattr(cl, name))
    monkeypatch.setattr(o, "ensure_rest", lambda c, bounded=True: True)
    monkeypatch.setattr(o, "_sweep_root_owned_repo_files", lambda c: None)
    return cl


def tick(dry=False):
    with pytest.raises(SystemExit) as e:
        o.cmd_tick(argparse.Namespace(dry_run=dry))
    return e.value.code


def events():
    return [json.loads(l) for l in o.AUDIT.read_text().splitlines()] if o.AUDIT.exists() else []


def state():
    return json.loads(o.SCHED_STATE.read_text())


# ── the tick ─────────────────────────────────────────────────────────────────
def test_a_tick_backs_up_what_is_due_most_overdue_first_and_never_a_mac(cluster):
    now = __import__("time").time()
    cluster.snaps = {("gpu-01", "system"): now - 8 * 3600, ("gpu-02", "system"): now - 3600,
                     ("gpu-03", "system"): now - 20 * 3600}
    assert tick() == 0
    assert cluster.ran == ["gpu-03", "gpu-01"]                    # gpu-02 is not due; mac-01 is monitored only
    assert {k: v["ok"] for k, v in state().items()} == {"gpu-03/system": True, "gpu-01/system": True}
    assert [e["event"] for e in events()] == ["tick-start", "backup", "tick-start", "backup"]
    assert events()[1]["snapshot"] == "abc123" and events()[1]["host"] == "gpu-03"
    cluster.ran.clear()
    o.cmd_tick(argparse.Namespace(dry_run=False))                 # nothing due now: returns, runs nothing
    assert cluster.ran == []


def test_a_dry_run_tick_only_prints_the_plan(cluster, capsys):
    o.cmd_tick(argparse.Namespace(dry_run=True))
    out = capsys.readouterr().out
    assert cluster.ran == [] and not o.SCHED_STATE.exists() and events() == []
    assert out.count("DUE ") == 3 and "never backed up" in out and "mac-01" not in out


def test_a_failed_backup_fails_the_tick_and_waits_for_the_retry(cluster):
    cluster.backup["gpu-01"] = "fail"
    assert tick() == 1
    assert sorted(cluster.ran) == ["gpu-01", "gpu-02", "gpu-03"] and state()["gpu-01/system"]["ok"] is False
    cluster.ran.clear()
    o.cmd_tick(argparse.Namespace(dry_run=False))                 # inside retry_after: not tried again
    assert cluster.ran == []
    st = state()
    st["gpu-01/system"]["ts"] -= 3700                             # an hour later
    o.SCHED_STATE.write_text(json.dumps(st))
    cluster.backup["gpu-01"] = "ok"
    assert tick() == 0 and cluster.ran == ["gpu-01"]


def test_an_unreadable_repository_is_not_decided_blind(cluster, capsys):
    cluster.unreadable = {"pair"}
    assert tick() == 0
    assert cluster.ran == ["gpu-03"]                              # the pair's hosts are not "never backed up"
    assert "repo pair unreadable this tick" in capsys.readouterr().out


def test_a_slow_or_unmeasurable_path_refuses_the_backup(cluster):
    cluster.net.update({"gpu-01": 12.0, "gpu-02": None})
    assert tick() == 1
    assert cluster.ran == ["gpu-03"]
    refused = {e["host"]: e for e in events() if e["event"].startswith("backup-refused")}
    assert refused["gpu-01"]["event"] == "backup-refused" and refused["gpu-01"]["mbit"] == 12
    assert refused["gpu-02"]["event"] == "backup-refused-unmeasurable"
    assert o._run_backup(o.cfg(), o.host_by_name(o.cfg(), "gpu-01"), "system", False, True) is True   # --force-low-net


def test_a_busy_spindle_skips_the_host_without_counting_a_failure(cluster):
    o.LOCK_DIR.mkdir()
    with open(o.LOCK_DIR / "nas.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)                         # a retention run holds the NAS
        with pytest.raises(SystemExit) as e:
            o.cmd_tick(argparse.Namespace(dry_run=False))
    assert e.value.code == 0 and cluster.ran == [] and not o.SCHED_STATE.exists()
    assert tick() == 0 and len(cluster.ran) == 3                 # free again: the next tick runs them


def test_a_stale_repo_lock_is_cleared_once_and_a_live_writers_never(cluster):
    cluster.backup["gpu-01"] = "locked"
    c = o.cfg()
    assert o._run_backup(c, o.host_by_name(c, "gpu-01"), "system", False, False) is True
    assert cluster.unlocked == ["gpu-01"] and cluster.ran == ["gpu-01", "gpu-01"]
    assert [e["event"] for e in events()] == ["backup-unlock-retry", "backup"]
    cluster.backup["gpu-01"], cluster.live_writer = "locked", "gpu-02"            # gpu-02 writes to the shared repo
    assert o._run_backup(c, o.host_by_name(c, "gpu-01"), "system", False, False) is False
    assert cluster.unlocked == ["gpu-01"] and events()[-1] == {**events()[-1], "event": "backup-refused-live-lock",
                                                                "held_by": "gpu-02"}
    cluster.backup["gpu-03"] = "busy"                                              # the agent's own one-job lock
    assert o._run_backup(c, o.host_by_name(c, "gpu-03"), "system", False, False) is False
    assert events()[-1]["event"] == "backup-skipped-running"


# ── recipe deploys ───────────────────────────────────────────────────────────
OLD = {"recipe": "old", "sha": "a" * 40, "unit": "noctyx-recipe-gpu.service", "dir": "/r/old", "kind": "systemd",
       "unit_text": "[Unit]\n", "models": ["model-old"], "state": "serving", "host": "gpu-03", "home": "/srv/ops"}
NEW = {"recipe": "new", "sha": "b" * 40, "unit": "noctyx-recipe-gpu.service", "dir": "/r/new", "kind": "systemd",
       "unit_text": "[Unit]\n", "host": "gpu-03", "home": "/srv/ops"}


@pytest.fixture()
def slot(cluster, monkeypatch):
    c = cluster.config()
    c["recipes"] = {"slots": {"gpu": {"head": "gpu-03", "port": 30000, "conflicts": ["by-hand.service"]}},
                    "catalog": {"new": {"repo": "https://example.org/new.git", "kind": "systemd", "slots": ["gpu"]}}}
    open(o.CFG_PATH, "w").write(json.dumps(c, indent=2))
    o._save_recipe_state({"slots": {"gpu": {"current": dict(OLD)}}})
    sw = {"calls": [], "rc": {}, "answers": 0}           # rc per target recipe; default 0
    monkeypatch.setattr(o, "_answers_after", lambda c, s: sw.__setitem__("answers", sw["answers"] + 1))

    def switch(c, slot, frm, to, wait_lock=0):
        sw["calls"].append((o.rec_label(frm), o.rec_label(to)))
        rc = sw["rc"].get((to or {}).get("recipe"), 0)
        return rc, ([f"model-{to['recipe']}"] if to and rc == 0 else [])
    monkeypatch.setattr(o, "_switch", switch)
    monkeypatch.setattr(o, "recipe_prepare", lambda c, s, name, ref=None: dict(NEW))
    monkeypatch.setattr(o, "_resolve_ref", lambda repo, ref: "b" * 40)
    monkeypatch.setattr(o, "recipe_live", lambda c, n, timeout=3: {"ok": True, "models": ["model-old"]})
    monkeypatch.setattr(o, "_llm_refresh", lambda wait=False: None)
    return sw


def deploy(**kw):
    a = argparse.Namespace(slot="gpu", recipe="new", ref=None, yes=True, prepare_only=False, wait_lock=0)
    a.__dict__.update(kw)
    return o.cmd_recipe_deploy(a)


def op(what, **kw):
    a = argparse.Namespace(slot="gpu", yes=True, wait_lock=0)
    a.__dict__.update(kw)
    with pytest.raises(SystemExit) as e:
        o._slot_op(a, what)
    return e.value.code


def test_a_deploy_switches_and_keeps_the_old_one_to_go_back_to(slot):
    deploy()
    cur, prev, hist = o.slot_records(o.cfg(), "gpu")
    assert cur["recipe"] == "new" and cur["models"] == ["model-new"] and cur["state"] == "serving"
    assert prev["recipe"] == "old" and hist[0]["event"] == "deploy" and hist[0]["rc"] == 0
    assert slot["calls"] == [("old@aaaaaaaaaaaa", "new@bbbbbbbbbbbb")]
    assert events()[-1]["event"] == "recipe-deploy" and events()[-1]["replaced"] == "old@aaaaaaaaaaaa"
    assert slot["answers"] == 1                                          # what came up is asked the fixed prompts
    assert op("rollback") == 0                                           # and back
    assert slot["answers"] == 2
    cur, prev, _ = o.slot_records(o.cfg(), "gpu")
    assert cur["recipe"] == "old" and prev["recipe"] == "new" and slot["calls"][-1] == ("new@bbbbbbbbbbbb", "old@aaaaaaaaaaaa")


def test_without_yes_a_deploy_prints_the_plan_and_prepare_only_never_switches(slot):
    with pytest.raises(SystemExit) as e:
        deploy(yes=False)
    assert e.value.code == 2 and slot["calls"] == []
    deploy(prepare_only=True)
    assert slot["calls"] == [] and o.slot_records(o.cfg(), "gpu")[0]["recipe"] == "old"
    with pytest.raises(SystemExit) as e:
        deploy(recipe="unknown")
    assert e.value.code == 1 and slot["calls"] == []


def test_a_deploy_that_does_not_come_up_switches_back(slot, capsys):
    slot["rc"]["new"] = 79
    with pytest.raises(SystemExit) as e:
        deploy()
    assert e.value.code == 79 and slot["answers"] == 0
    assert slot["calls"] == [("old@aaaaaaaaaaaa", "new@bbbbbbbbbbbb"), ("new@bbbbbbbbbbbb", "old@aaaaaaaaaaaa")]
    cur, prev, hist = o.slot_records(o.cfg(), "gpu")
    assert cur["recipe"] == "old" and prev is None and hist[0]["rolled_back"] is True and hist[0]["rc"] == 79
    assert "rolled back" in capsys.readouterr().out and events()[-1]["rolled_back"] is True


def test_a_held_gpu_lock_stops_nothing(slot):
    slot["rc"]["new"] = 75
    with pytest.raises(SystemExit) as e:
        deploy()
    assert e.value.code == 75 and len(slot["calls"]) == 1               # no switch back: nothing was stopped
    assert o.slot_records(o.cfg(), "gpu")[0]["recipe"] == "old" and events()[-1]["stage"] == "lock"


def test_a_failed_prepare_stops_nothing(slot, monkeypatch):
    def broken(c, s, name, ref=None):
        raise RuntimeError("the build failed")
    monkeypatch.setattr(o, "recipe_prepare", broken)
    with pytest.raises(SystemExit) as e:
        deploy()
    assert e.value.code == 1 and slot["calls"] == [] and events()[-1]["stage"] == "prepare"


def test_stop_and_start_keep_the_deployment_and_its_state(slot, monkeypatch):
    assert op("stop", yes=False) == 2 and slot["calls"] == []
    assert op("stop") == 0 and slot["calls"] == [("old@aaaaaaaaaaaa", "nothing")]
    cur, prev, _ = o.slot_records(o.cfg(), "gpu")
    assert cur["recipe"] == "old" and cur["state"] == "stopped" and prev is None and slot["answers"] == 0
    o._slot_op(argparse.Namespace(slot="gpu", yes=False, wait_lock=0), "start")   # still answering: nothing to do
    assert len(slot["calls"]) == 1
    monkeypatch.setattr(o, "recipe_live", lambda c, n, timeout=3: {"ok": False, "models": []})
    assert op("start", yes=False) == 0 and slot["calls"][-1] == ("nothing", "old@aaaaaaaaaaaa")
    assert o.slot_records(o.cfg(), "gpu")[0]["state"] == "serving" and slot["answers"] == 1
    with pytest.raises(SystemExit, match="nothing to roll back to"):
        o._slot_op(argparse.Namespace(slot="gpu", yes=True, wait_lock=0), "rollback")


def test_a_slot_busy_with_another_recipe_job_is_refused(slot, capsys):
    o.LOCK_DIR.mkdir(exist_ok=True)
    with open(o.LOCK_DIR / "recipe-gpu.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(SystemExit) as e:
            deploy()
    assert e.value.code == 1 and slot["calls"] == [] and "is busy" in capsys.readouterr().out


def test_the_switch_script_stops_every_conflicting_unit_and_is_valid_bash(cluster, monkeypatch):
    c = cluster.config()
    c["recipes"] = {"slots": {"gpu": {"head": "gpu-03", "port": 30000, "conflicts": ["by-hand.service"],
                                      "gpu_idle_check": True, "lock": "~/.cache/gpu.lock"}}, "catalog": {}}
    scripts = []
    monkeypatch.setattr(o, "_root_stream", lambda c, h, script: scripts.append(script) or (0, "== serving\nNX_MODELS m1 m2\n"))
    monkeypatch.setattr(o, "_home", lambda c, h, user: "/srv/ops")
    adopted = {"recipe": None, "name": "by hand", "adopted": True, "unit": "by-hand.service", "spark_dir": "~/spark"}
    assert o._switch(c, "gpu", adopted, dict(NEW), wait_lock=60) == (0, ["m1", "m2"])
    assert o._switch(c, "gpu", dict(NEW), None) == (0, [])                        # a stop
    assert o._switch(c, "gpu", dict(NEW), adopted) == (0, ["m1", "m2"])           # back to the adopted unit
    for s in scripts:
        r = subprocess.run(["bash", "-n"], input=s, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    go, stop, back = scripts
    assert "for u in by-hand.service noctyx-recipe-gpu.service; do" in go         # the conflict, its own unit, frm
    assert "LOCK=/srv/ops/.cache/gpu.lock" in go and "flock -w 60 9 || exit 75" in go
    assert "cd /srv/ops/spark && python3 -m spark down" in go                    # the adopted spark checkout
    assert "nvidia-smi --query-compute-apps" in go and "exit 77" in go            # the GPU must be idle first
    assert "base64 -d > /etc/systemd/system/noctyx-recipe-gpu.service" in go and "systemctl enable noctyx-recipe-gpu.service" in go
    assert 'echo "== stopped"; exit 0' in stop and "systemctl start" not in stop
    assert "base64 -d" not in back and "systemctl start --no-block by-hand.service" in back


# ── who is on a slot's GPU ───────────────────────────────────────────────────
GPU_OUT = """H 4321
W 5000
W 5001
==
777, 30412
==
    1     900 root     /sbin/init
  777   86400 ops      /srv/engine/bin/engine-native --port 30000 --api-key sk-abcdefghijklmnop1234
 4321     125 ops      flock /srv/locks/gpu.lock python3 -m harness run target
 5000      60 ana      flock /srv/locks/gpu.lock ./bench.sh
"""


def test_gpu_use_names_the_holder_the_waiters_and_the_gpus_processes():
    u = o.parse_gpu_use(GPU_OUT)
    assert u["lock"] == "held" and u["holder"] == {"pid": 4321, "seconds": 125, "user": "ops",
                                                   "cmd": "flock /srv/locks/gpu.lock python3 -m harness run target"}
    assert [p["pid"] for p in u["waiting"]] == [5000, 5001] and u["waiting"][1]["cmd"].startswith("(a process in another")
    g, = u["gpu"]
    assert g["mib"] == 30412 and g["pid"] == 777 and "abcdefghijklmnop" not in g["cmd"]          # a key on a command line
    line = o.gpu_use_line(u)
    assert "engine-native 29.7 GiB" in line and "lock held by flock" in line and "2 waiting" in line
    free = o.parse_gpu_use("==\n==\n    1 9 root /sbin/init\n")
    assert free["lock"] == "free" and free["holder"] is None and free["gpu"] == [] and o.gpu_use_line(free) == "GPU: idle · lock free"
    assert o.parse_gpu_use("N\n==\n==\n")["lock"] == "none"
    assert "could not read" in o.gpu_use_line({"error": "ssh timed out"})


def test_the_gpu_snippet_is_valid_shell_and_reads_the_real_lock_table(tmp_path, cluster):
    c = cluster.config()
    lock = tmp_path / "gpu.lock"
    lock.write_text("")
    c["recipes"] = {"slots": {"gpu": {"head": "gpu-03", "port": 30000, "lock": str(lock)}}, "catalog": {}}
    ran = []

    def on(c_, h, cmd, timeout=0):
        ran.append(cmd)
        return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=30)
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    mp.setattr(o, "_on", on)
    try:
        assert o.slot_gpu_use(c, "gpu")["lock"] == "free"
        with open(lock, "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            u = o.slot_gpu_use(c, "gpu")
        assert u["lock"] == "held" and u["holder"]["pid"] == __import__("os").getpid() and u["path"] == str(lock)
        assert subprocess.run(["sh", "-n"], input=ran[0], capture_output=True, text=True).returncode == 0
        c["recipes"]["slots"]["gpu"]["lock"] = str(tmp_path / "never-made.lock")
        assert o.slot_gpu_use(c, "gpu")["lock"] == "none"
    finally:
        mp.undo()
