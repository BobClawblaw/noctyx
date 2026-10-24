"""Self-repair's safety rules (a budget, a freeze, shadow and report-only modes, no retry once a repair did all it
safely could), its run log, its policy, and the playbooks' decisions: which units it may restart, when a disk,
a model server or an upgrade is its to repair, and which alerts wake it."""
import datetime
import json

import pytest

from noctyx import change, heal

HEAD = {"server": {"address": "192.0.2.10", "hostname": "head"}, "hosts": []}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(change, "LEDGER", tmp_path / "changes.jsonl")
    monkeypatch.setattr(change, "LOCK", tmp_path / "change.lock")
    monkeypatch.setattr(heal, "HEAL_FILE", tmp_path / "heal.json")
    monkeypatch.setattr(heal, "LOG_FILE", tmp_path / "heal-log.jsonl")
    monkeypatch.setattr(heal, "LOCK_FILE", tmp_path / "heal.lock")
    from noctyx import orchestrator as cm
    monkeypatch.setattr(cm, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(cm, "reboot_pending", lambda name=None: {})
    return tmp_path


def quiet(_):
    pass


@pytest.fixture()
def findings(monkeypatch):
    """Every check returns nothing, except "units", which returns what the test puts in the list."""
    got = []
    for name in ("check_services", "check_dropins", "check_exporters", "check_textfiles", "check_rules",
                 "check_disk", "check_nas", "check_backups", "check_slots", "check_upgrades"):
        monkeypatch.setattr(heal, name, lambda *a: [])
    monkeypatch.setattr(heal, "_targets", lambda: [])
    monkeypatch.setattr(heal, "check_units", lambda *a: list(got))
    return got


def repair(result, calls):
    def go():
        calls.append(1)
        return result
    return go


def cfg(**pol):
    return {**HEAD, "heal": pol}


def test_the_budget_caps_repairs_per_hour(findings):
    calls = []
    findings += [heal.Finding("units", f"gpu-{i}", "x.service failed", repair((True, "ok"), calls), "restart it")
                 for i in range(5)]
    st = heal.run(cfg(budget_per_hour=2), log=quiet)
    assert len(calls) == 2
    assert [p["state"] for p in st["problems"]] == ["waiting"] * 3
    assert not any(p["needs_person"] for p in st["problems"])


def test_failed_repairs_in_a_row_freeze_every_repair_until_unfrozen(findings):
    calls = []
    findings += [heal.Finding("units", f"gpu-{i}", "x.service failed", repair((False, "still failed"), calls),
                              "restart it") for i in range(4)]
    st = heal.run(cfg(freeze_after=2), log=quiet)
    assert len(calls) == 2 and st["frozen"]
    frozen = next(p for p in st["problems"] if p["check"] == "heal")
    assert frozen["needs_person"] and "2 repairs in a row failed" in frozen["detail"]
    heal.run(cfg(freeze_after=2), log=quiet)
    assert len(calls) == 2                                               # frozen: nothing more
    assert heal.unfreeze(by="test") and "frozen" not in heal.load()
    heal.run(cfg(freeze_after=2), log=quiet)
    assert len(calls) > 2


def test_a_success_resets_the_failure_streak(findings):
    calls = []
    findings += [heal.Finding("units", "gpu-1", "a failed", repair((False, "no"), calls), "restart"),
                 heal.Finding("units", "gpu-2", "b failed", repair((True, "ok"), calls), "restart"),
                 heal.Finding("units", "gpu-3", "c failed", repair((False, "no"), calls), "restart")]
    st = heal.run(cfg(freeze_after=2), log=quiet)
    assert len(calls) == 3 and not st.get("frozen") and st["fail_streak"] == 1


def test_shadow_and_report_only_never_repair(findings):
    calls = []
    findings.append(heal.Finding("units", "gpu-1", "x.service failed", repair((True, "ok"), calls), "restart it"))
    st = heal.run(cfg(playbooks={"units": "shadow"}), log=quiet)
    assert calls == [] and st["problems"][0]["state"] == "shadow" and st["problems"][0]["needs_person"]
    st = heal.run(cfg(auto=False), log=quiet)
    assert calls == [] and "report only" in st["problems"][0]["why"]


def test_an_off_playbook_is_not_even_checked(findings, monkeypatch):
    ran = []
    monkeypatch.setattr(heal, "check_units", lambda *a: ran.append(1) or [])
    heal.run(cfg(playbooks={"units": "off"}), log=quiet)
    assert ran == []


def test_did_what_it_could_goes_to_a_person_without_a_retry_or_a_freeze(findings):
    calls = []
    findings += [heal.Finding("units", f"gpu-{i}", "full", repair((None, "freed 1 GiB, still 95%"), calls), "clean")
                 for i in range(3)]
    st = heal.run(cfg(freeze_after=2), log=quiet)
    assert len(calls) == 3 and not st.get("frozen")
    assert all(p["needs_person"] and p["state"] == "needs_person" for p in st["problems"])


def test_one_host_run_keeps_the_other_hosts_problems(findings):
    findings.append(heal.Finding("units", "gpu-1", "a failed"))
    findings.append(heal.Finding("units", "gpu-2", "b failed"))
    heal.run(HEAD, log=quiet)
    findings.clear()
    st = heal.run(HEAD, log=quiet, host="gpu-1")
    assert [p["host"] for p in st["problems"]] == ["gpu-2"]


def test_one_run_at_a_time(findings):
    with heal._one_at_a_time():
        st = heal.run(HEAD, log=quiet)
    assert "another heal run" in st["skipped"]


# ── the run log ─────────────────────────────────────────────────────────────
def test_every_run_is_logged_with_its_decisions(findings):
    calls = []
    findings.append(heal.Finding("units", "gpu-1", "x.service failed", repair((True, "active again"), calls),
                                 "restart x.service"))
    heal.run(HEAD, log=quiet, trigger="alert:HostSystemdUnitFailed")
    findings.clear()
    heal.run(HEAD, log=quiet, trigger="timer")
    runs = heal.runs()
    assert len(runs) == 1                                              # by default: the runs that found something
    r = runs[0]
    assert r["trigger"] == "alert:HostSystemdUnitFailed" and r["summary"]["repaired"] == 1
    kinds = [(s["kind"], s.get("host")) for s in r["steps"]]
    assert ("found", "gpu-1") in kinds and ("repair", "gpu-1") in kinds and ("result", "gpu-1") in kinds
    assert len(heal.runs(everything=True)) == 2
    assert heal.runs(host="gpu-2") == [] and heal.runs(q="active again")
    assert heal.runs(run=r["run"])[0]["run"] == r["run"]


def test_commands_are_logged_with_their_output(findings, monkeypatch):
    def go():
        heal._run(["echo", "hello"])
        return True, "ok"
    findings.append(heal.Finding("units", "gpu-1", "x failed", go, "echo"))
    heal.run(HEAD, log=quiet)
    cmd = next(s for s in heal.runs()[0]["steps"] if s["kind"] == "cmd")
    assert cmd["text"] == "echo hello" and cmd["out"] == "hello" and cmd["rc"] == 0


def test_the_log_rotates(findings, monkeypatch):
    monkeypatch.setattr(heal, "LOG_MAX", 10)
    findings.append(heal.Finding("units", "gpu-1", "x failed"))
    heal.run(HEAD, log=quiet)
    heal.run(HEAD, log=quiet)
    assert heal.LOG_FILE.with_name(heal.LOG_FILE.name + ".1").exists() and len(heal.runs()) == 2


# ── the policy ──────────────────────────────────────────────────────────────
def test_the_policy_defaults_upgrades_off():
    pol = heal.policy({})
    assert pol["auto"] and not pol["upgrades"]["auto"] and pol["playbooks"]["upgrades"] == "on"
    pol = heal.policy({"heal": {"upgrades": {"auto": True}, "playbooks": {"disk": "shadow"}}})
    assert pol["upgrades"]["auto"] and pol["upgrades"]["window"] == "03:00-05:00" and pol["playbooks"]["disk"] == "shadow"


@pytest.mark.parametrize("blk, bad", [
    ({"auto": "yes"}, "auto"),
    ({"budget_per_hour": 0}, "budget_per_hour"),
    ({"disk_pct": 100}, "disk_pct"),
    ({"playbooks": {"disk": "maybe"}}, "playbooks"),
    ({"playbooks": {"nope": "on"}}, "playbooks"),
    ({"restart_units": ["*"]}, "matches too much"),
    ({"restart_units": ["docker.service"]}, "never restarts"),
    ({"restart_units": ["bmcbitcoind.service"]}, "never restarts"),
    ({"restart_units": ["tensorfold-qwen38.service"]}, "never restarts"),
    ({"restart_units": ["a; rm -rf /"]}, "not a unit name"),
    ({"upgrades": {"window": "3-5"}}, "window"),
    ({"upgrades": {"window": "04:00-04:00"}}, "window"),
    ({"upgrades": {"components": ["restic"]}}, "components"),
    ({"upgrades": {"when": "now"}}, "unknown"),
    ({"surprise": 1}, "unknown"),
])
def test_the_policy_refuses(blk, bad):
    assert any(bad in e for e in heal.validate_policy(blk)[1])


def test_the_policy_accepts():
    blk = {"auto": True, "budget_per_hour": 5, "freeze_after": 2, "disk_pct": 90,
           "playbooks": {"disk": "shadow", "units": "on"}, "restart_units": ["nginx.service", "myapp-*.service"],
           "restart_slots": ["rtx5090"], "upgrades": {"auto": True, "window": "22:00-02:00",
                                                       "components": ["node_exporter"], "major": False,
                                                       "soak_minutes": 30}}
    out, errs = heal.validate_policy(blk)
    assert errs == [] and out["playbooks"] == {"disk": "shadow"}       # the defaults are not stored


def test_the_window_can_wrap_midnight():
    at = lambda h, m: datetime.datetime(2026, 10, 24, h, m)  # noqa: E731
    assert heal.in_window("03:00-05:00", at(4, 59)) and not heal.in_window("03:00-05:00", at(5, 0))
    assert heal.in_window("22:00-02:00", at(23, 30)) and heal.in_window("22:00-02:00", at(1, 0))
    assert not heal.in_window("22:00-02:00", at(12, 0))


def test_which_units_it_may_restart():
    pol = heal.policy({"heal": {"restart_units": ["myapp-*.service", "docker.service"]}})
    assert heal.may_restart("myapp-web.service", pol)
    assert not heal.may_restart("docker.service", pol)                  # listed, and still never
    assert not heal.may_restart("nginx.service", pol)                   # not listed
    for u in ("containerd.service", "bitcoind.service", "vllm-a.service", "ssh.service", "user@" + "1000.service"):
        assert heal.never_restart(u)


# ── the playbooks ───────────────────────────────────────────────────────────
@pytest.fixture()
def remote(monkeypatch):
    """_ssh answers from a table of (substring -> (rc, output)); every command it saw is kept."""
    seen, table = [], []

    def ssh(c, h, cmd, timeout=30):
        seen.append(cmd)
        for k, v in table:
            if k in cmd:
                return v
        return 0, ""
    monkeypatch.setattr(heal, "_ssh", ssh)
    return seen, table


def test_units_ghost_listed_and_left_to_a_person(monkeypatch, remote):
    seen, table = remote
    from noctyx.paths import P
    table.append(("list-units --failed", (0, "U gone.service not-found\nU myapp-web.service loaded\nJ boom\n"
                                            "U nginx.service loaded\nJ port in use\nJ exiting\n"
                                            f"U {P['unit_prefix']}-tick.service loaded\n")))
    pol = heal.policy({"heal": {"restart_units": ["myapp-*"]}})
    got = {f.what: f for f in heal.check_units({}, {"gpu-1": {"name": "gpu-1"}}, set(), pol)}
    assert set(got) == {"gone.service failed and its unit file is gone", "myapp-web.service failed",
                        "nginx.service failed"}                         # Noctyx's own: the service check's
    assert got["gone.service failed and its unit file is gone"].repair
    assert got["myapp-web.service failed"].repair and not got["nginx.service failed"].repair
    assert got["nginx.service failed"].detail == "not in heal.restart_units: port in use | exiting"
    got["gone.service failed and its unit file is gone"].repair()
    assert seen[-1] == "systemctl reset-failed gone.service"


def test_disk_is_cleared_without_docker_and_a_full_one_goes_to_a_person(monkeypatch, remote):
    seen, table = remote
    monkeypatch.setattr(heal, "_query", lambda q: [{"metric": {"host": "gpu-1"}, "value": [0, "95.2"]},
                                                   {"metric": {"host": "gpu-2"}, "value": [0, "40"]}])
    table += [("journalctl --vacuum-size", (0, "freed=1073741824 pct=94")), ("du -x", (0, "3T\t/\n2T\t/models\n"))]
    linux = {"gpu-1": {"name": "gpu-1"}, "gpu-2": {"name": "gpu-2"}}
    pol = heal.policy({})
    st = {}
    (f,) = heal.check_disk({}, linux, set(), pol, st)
    assert f.host == "gpu-1" and "docker" not in heal.FREE_SPACE.lower()
    ok, detail = f.repair()
    assert ok is None and "still 94% full" in detail and "/models" in detail
    st["repairs"] = [{"key": "disk/gpu-1", "ok": None, "detail": detail, "ts_epoch": __import__("time").time()}]
    (f,) = heal.check_disk({}, linux, set(), pol, st)
    assert f.repair is None                                             # it already did what it could


def test_backup_causes():
    assert "ssh" in heal.backup_cause({"rc": 255})
    assert "could not be read" in heal.backup_cause({"rc": 3, "stderr": "error: open /x: permission denied\n"})
    assert "locked" in heal.backup_cause({"rc": 1, "stderr": "repository is already locked by PID 4"})
    assert "paths file" in heal.backup_cause({"rc": 1, "message": "no paths file for profile 'smoke'"})


@pytest.fixture()
def slot(monkeypatch, remote):
    from noctyx import orchestrator as cm
    live = {"ok": False, "models": []}
    rec = {"current": {"adopted": True, "unit": "llm.service", "name": "llm"}}
    monkeypatch.setattr(cm, "recipes_cfg", lambda c: {"slots": {"gpu": {"head": "gpu-1", "port": 8000}}})
    monkeypatch.setattr(cm, "slot_records", lambda c, n: (rec["current"], None, []))
    monkeypatch.setattr(cm, "host_by_name", lambda c, n: {"name": n})
    monkeypatch.setattr(cm, "recipe_live", lambda c, n: dict(live))
    monkeypatch.setattr(cm, "spindle_holder", lambda n: {})
    remote[1].append(("is-active llm.service", (0, "active")))
    return live, rec


def test_a_model_server_is_restarted_only_after_noctyx_saw_its_own_unit_serve(slot, monkeypatch):
    live, rec = slot
    pol = heal.policy({"heal": {"restart_slots": ["gpu"]}})
    st = {}
    (f,) = heal.check_slots({}, st, pol)                                 # down, never seen up
    assert f.repair is None and "has not seen" in f.detail
    live.update(ok=True, models=["m"])
    assert heal.check_slots({}, st, pol) == [] and st["slots"]["gpu"]["models"] == ["m"]
    live.update(ok=False, models=[])
    assert heal.check_slots({}, st, pol) == []                           # down, but not for 15 min yet
    st["slots"]["gpu"]["down_since"] -= heal.SLOT_DOWN_FOR + 1
    (f,) = heal.check_slots({}, st, pol)
    assert f.repair and "recipe-start --slot gpu" in f.how
    rec["current"] = {**rec["current"], "state": "stopped"}
    assert heal.check_slots({}, st, pol) == []                           # stopped through Noctyx: left alone
    assert heal.check_slots({}, {}, heal.policy({})) == []                # not opted in


def test_a_model_server_answering_from_another_unit_is_not_recorded(slot, remote):
    live, _ = slot
    remote[1].insert(0, ("is-active", (3, "inactive")))
    live.update(ok=True, models=["something else"])
    st = {}
    heal.check_slots({}, st, heal.policy({"heal": {"restart_slots": ["gpu"]}}))
    assert "ok_ts" not in st["slots"]["gpu"]


@pytest.fixture()
def versions(monkeypatch):
    from noctyx import components as comp, orchestrator as cm
    import time
    inv = {"ts": time.time(), "hosts": {
        "head": [{"component": "prometheus", "installed": "3.14.0", "state": "behind", "fix": {"change": "prometheus", "what": "w"}}],
        "gpu-1": [{"component": "node_exporter", "installed": "1.11.0", "state": "behind", "fix": {"change": "node_exporter", "what": "w"}}],
        "gpu-2": [{"component": "node_exporter", "installed": "0.18.0", "state": "behind", "fix": {"change": "node_exporter", "what": "w"}}],
    }}
    monkeypatch.setattr(cm, "versions_view", lambda: inv)
    monkeypatch.setattr(cm, "_view_host", lambda c, n: {"name": n, "local": n == "head"})
    monkeypatch.setattr(cm, "is_head", lambda c, h: h["name"] == "head")
    monkeypatch.setattr(comp, "PINS", {"prometheus": {"version": "3.15.1"}, "node_exporter": {"version": "1.12.1"}})
    monkeypatch.setattr(heal, "in_window", lambda w, now=None: True)
    return inv


def test_automatic_upgrades_are_off_unless_asked(versions):
    assert heal.check_upgrades({}, {}, heal.policy({})) == []


def test_automatic_upgrades_one_per_run_nodes_first_and_no_major(versions):
    pol = heal.policy({"heal": {"upgrades": {"auto": True}}})
    got = heal.check_upgrades({}, {}, pol)
    acting = [f for f in got if f.repair]
    assert [f.host for f in acting] == ["gpu-1"]                         # one, a node before the head
    major = next(f for f in got if f.host == "gpu-2")
    assert major.repair is None and "major" in major.detail


def test_a_failed_automatic_upgrade_is_not_tried_again(versions):
    pol = heal.policy({"heal": {"upgrades": {"auto": True}}})
    st = {"upgrade_failed": {"node_exporter@1.12.1": {"host": "gpu-1", "ts": "2026-10-24T03:00:00Z"}}}
    got = {f.host: f for f in heal.check_upgrades({}, st, pol)}
    assert got["gpu-1"].repair is None and "not tried again" in got["gpu-1"].detail
    assert got["head"].repair                                            # prometheus is another release


def test_automatic_upgrades_wait_out_the_window_and_the_soak(versions, monkeypatch):
    pol = heal.policy({"heal": {"upgrades": {"auto": True, "components": ["node_exporter"]}}})
    monkeypatch.setattr(change, "last_change", lambda host, comp, within=0, outcome="ok": {"host": "gpu-9", "ts": "t"})
    assert not any(f.repair for f in heal.check_upgrades({}, {}, pol))   # gpu-9's upgrade is still soaking
    monkeypatch.setattr(heal, "in_window", lambda w, now=None: False)
    assert heal.check_upgrades({}, {}, pol) == []


# ── alerts wake it ──────────────────────────────────────────────────────────
def test_an_alert_with_a_playbook_runs_heal_for_its_host(monkeypatch, tmp_path):
    from noctyx import console
    started = []
    monkeypatch.setattr(console.CM, "cfg", lambda: {**HEAD, "hosts": [{"name": "gpu-1", "address": "192.0.2.11"}]})
    monkeypatch.setattr(console.CM, "hosts", lambda c: c["hosts"])
    monkeypatch.setattr(console, "audit_write", lambda rec: None)
    monkeypatch.setattr(console, "_HEAL_WOKEN", {})
    fire = {"status": "firing", "name": "HostRootFilesystemFull", "host": "gpu-1"}
    assert console.wake_heal(fire, now=1000, start=started.append)
    assert started[0][-4:] == ["--host", "gpu-1", "--trigger", "alert:HostRootFilesystemFull"]
    assert not console.wake_heal(fire, now=1060, start=started.append)  # twice in 2 min: once
    assert console.wake_heal(fire, now=1200, start=started.append)
    assert not console.wake_heal({**fire, "name": "BackupStale"}, now=5000, start=started.append)   # no playbook
    assert not console.wake_heal({**fire, "status": "resolved"}, now=6000, start=started.append)
    assert not console.wake_heal({**fire, "host": "nope"}, now=7000, start=started.append)
    monkeypatch.setattr(console.CM, "cfg", lambda: {**HEAD, "hosts": [{"name": "gpu-1"}], "heal": {"auto": False}})
    assert not console.wake_heal(fire, now=9000, start=started.append)
    assert len(started) == 2


def test_an_alert_shows_what_self_repair_says_about_it(monkeypatch, tmp_path):
    import io
    from noctyx import console
    am = [{"labels": {"alertname": "HostRootFilesystemFull", "host": "gpu-1", "severity": "warning"},
           "annotations": {"summary": "gpu-1 root filesystem 95% full"}, "startsAt": "t", "fingerprint": "f"},
          {"labels": {"alertname": "BackupStale", "host": "gpu-1", "severity": "warning"}, "annotations": {},
           "startsAt": "t", "fingerprint": "g"}]
    monkeypatch.setattr(console.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(am).encode()))
    monkeypatch.setattr(console, "silences_view", lambda: ([], None))
    monkeypatch.setattr(console, "ALERT_LOG", tmp_path / "alerts.jsonl")
    heal._save({"problems": [{"key": "disk/gpu-1", "check": "disk", "host": "gpu-1", "what": "full",
                              "state": "needs_person", "why": "cleared what it safely can"}]})
    got = {a["name"]: a for a in console.alerts_view()["active"]}
    assert got["HostRootFilesystemFull"]["selfrepair"]["state"] == "needs_person"
    assert "selfrepair" not in got["BackupStale"]


def test_a_head_run_clears_a_ready_problem_that_went_away(findings, monkeypatch):
    monkeypatch.setattr(heal, "check_services", lambda *a: [heal.Finding("ready", "head", "prometheus does not answer")])
    heal.run(HEAD, log=quiet)
    monkeypatch.setattr(heal, "check_services", lambda *a: [])
    st = heal.run(HEAD, log=quiet, host="head")
    assert st["problems"] == []


def test_the_tick_stops_restarting_the_nas_endpoint_after_3_failures(monkeypatch, tmp_path):
    from noctyx import orchestrator as cm
    tried = []
    monkeypatch.setattr(cm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(cm, "rest_on_nas", lambda c: True)
    monkeypatch.setattr(cm, "rest_up", lambda c: False)
    monkeypatch.setattr(cm, "nas_ssh", lambda c, cmd, timeout=0: tried.append(cmd) or type("R", (), {"stdout": b""})())
    c = {"server": {"nas_ssh": {"dir": "rest"}}}
    for _ in range(5):
        cm.ensure_rest(c)
    assert len(tried) == 3
    cm.ensure_rest(c, bounded=False)                                       # heal's own try is its own bound
    assert len(tried) == 4
