"""Every change checks itself and undoes itself: a bad result, an error or a refusal leaves the machine as it
was; a rollout changes one machine first and stops at the first failure; noctyx heal repairs what it safely
can, a bounded number of times, and leaves the rest to a person."""
import pytest

from noctyx import change, components as comp, heal, upgrades


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(change, "LEDGER", tmp_path / "changes.jsonl")
    monkeypatch.setattr(change, "LOCK", tmp_path / "change.lock")
    monkeypatch.setattr(heal, "HEAL_FILE", tmp_path / "heal.json")
    monkeypatch.setattr(heal, "LOG_FILE", tmp_path / "heal-log.jsonl")
    monkeypatch.setattr(heal, "LOCK_FILE", tmp_path / "heal.lock")
    from noctyx import orchestrator as cm
    monkeypatch.setattr(cm, "AUDIT", tmp_path / "audit.jsonl")
    return tmp_path


class Fake(change.Change):
    kind = "fake"

    def __init__(self, name="gpu-01", refuse=None, fail_apply=False, bad=(), bad_after_rollback=(), late=()):
        super().__init__({}, {"name": name})
        self.steps, self.refuse, self.fail_apply = [], refuse, fail_apply
        self.bad, self.bad_after_rollback, self.late = list(bad), list(bad_after_rollback), list(late)

    def preflight(self):
        self.steps.append("preflight")
        if self.refuse:
            raise change.Refused(self.refuse)

    def snapshot(self):
        self.steps.append("snapshot")
        self.snap["kept"] = "old"

    def apply(self):
        self.steps.append("apply")
        if self.fail_apply:
            raise RuntimeError("the installer exited 1")

    def verify(self):
        self.steps.append("verify")
        if "verify" in self.steps[:-1] and self.late:
            return self.late
        return self.bad

    def rollback(self):
        self.steps.append("rollback")

    def verify_rollback(self):
        return self.bad_after_rollback


def quiet(_):
    pass


def test_a_change_that_works_is_recorded():
    ch = Fake()
    rec = change.run(ch, log=quiet)
    assert rec["outcome"] == "ok" and ch.steps == ["preflight", "snapshot", "apply", "verify"]
    assert change.ledger()[-1]["snap"] == {"kept": "old"}


def test_a_refusal_changes_nothing():
    ch = Fake(refuse="no room for the copy")
    rec = change.run(ch, log=quiet)
    assert rec["outcome"] == "refused" and ch.steps == ["preflight"] and "no room" in rec["problems"][0]


def test_a_bad_result_or_an_error_rolls_back():
    ch = Fake(bad=["a scrape took 10.0s"])
    assert change.run(ch, log=quiet)["outcome"] == "rolled_back" and ch.steps[-1] == "rollback"
    ch = Fake(fail_apply=True)
    rec = change.run(ch, log=quiet)
    assert rec["outcome"] == "rolled_back" and "verify" not in ch.steps and "exited 1" in rec["problems"][0]
    rec = change.run(Fake(bad=["down"], bad_after_rollback=["still down"]), log=quiet)
    assert rec["outcome"] == "rollback_failed" and rec["after_rollback"] == ["still down"]


def test_one_change_at_a_time():
    with change._held():
        assert change.busy()
        assert change.run(Fake(), log=quiet)["outcome"] == "refused"
    assert not change.busy()


def test_a_rollout_goes_one_first_and_stops_at_the_first_failure():
    a, b, c = Fake("a"), Fake("b", bad=["down"]), Fake("c")
    res = change.rollout([a, b, c], soak=1, log=quiet, sleep=lambda s: None)
    assert [r["outcome"] for r in res["results"]] == ["ok", "rolled_back"]
    assert res["untouched"] == ["c"] and c.steps == [] and res["stopped"].startswith("b")


def test_a_canary_that_breaks_while_watched_is_undone_and_nothing_else_changes():
    a, b = Fake("a", late=["broke later"]), Fake("b")
    res = change.rollout([a, b], soak=300, log=quiet, sleep=lambda s: None)
    assert a.steps[-1] == "rollback" and b.steps == [] and res["untouched"] == ["b"]
    assert change.last_change("a", "fake") is None             # the later rollback supersedes the ok


def test_last_change_is_the_newest_and_recent():
    change.run(Fake("a"), log=quiet)
    assert change.last_change("a", "fake")["outcome"] == "ok"
    assert change.last_change("a", "fake", within=-1) is None
    assert change.last_change("b", "fake") is None


# ── what the registry offers for each row ───────────────────────────────────
def _row(component, installed, source, state="upgrade", latest=None):
    return {"component": component, "installed": installed, "source": source, "state": state,
            "latest": latest or comp.PINS.get(component, {}).get("version"), "detail": ""}


def test_the_head_services_are_changes_and_a_major_jump_gets_a_trial():
    head = {"name": "head", "local": True}
    f = comp.fix(_row("prometheus", "2.45.3", "distro"), head, True)
    assert f["change"] == "prometheus" and "trial" in f["what"] and f["argv"][:3] == ["upgrade", "--host", "head"]
    f = comp.fix(_row("prometheus", "3.15.0", "adopted", state="behind"), head, True)
    assert f["change"] == "prometheus" and "trial" not in f["what"]
    assert comp.fix(_row("alertmanager", "0.26.0", "distro"), head, True)["change"] == "alertmanager"
    assert comp.fix(_row("prometheus", "3.15.0", "noctyx", state="behind"), head, True)["argv"] == ["setup", "metrics", "--force"]
    assert "why" in comp.fix(_row("alertmanager", "0.26.0", "brew"), {"name": "mac", "os": "macos"}, True)


def test_an_adopted_unit_is_found_first():
    snip = comp.probe_snippet(next(x for x in comp.COMPONENTS if x["name"] == "prometheus"))
    assert snip.index("prometheus.service.d/noctyx.conf") < snip.index("/usr/bin/prometheus ")
    assert "adopted" in snip


def test_an_optional_component_that_is_not_there_has_no_row():
    c = {"server": {"address": "192.0.2.10", "hostname": "head"}, "hosts": []}
    inv = comp.inventory(c, lambda h, s: "==prometheus==\n2.45.3 distro\n", {"prometheus/prometheus": {"tag": "3.15.1", "ts": 9e12},
                                                                            "prometheus/alertmanager": {"tag": "0.34.1", "ts": 9e12},
                                                                            "restic/restic": {"tag": "0.19.1", "ts": 9e12},
                                                                            "restic/rest-server": {"tag": "0.14.0", "ts": 9e12},
                                                                            "prometheus/node_exporter": {"tag": "1.12.1", "ts": 9e12},
                                                                            "BobClawblaw/noctyx": {"tag": "0.1.0", "ts": 9e12}},
                         version="0.1.0")
    names = [r["component"] for r in inv["hosts"]["head"]]
    assert "prometheus" in names and "alertmanager" not in names


# ── what "it works" means ───────────────────────────────────────────────────
def test_comparing_two_prometheus_states():
    before = {"ready": True, "targets": {"node a:9100": ("up", "", 100.0), "llm b:8000": ("down", "refused", 100.0)},
              "rules": 45, "rules_bad": [], "alertmanagers": 1}
    same = {**before, "targets": {"node a:9100": ("up", "", 200.0), "llm b:8000": ("down", "refused", 200.0)}}
    assert upgrades.compare(before, same, since=150) == []
    worse = {**same, "targets": {"node a:9100": ("down", "unsupported Content-Type", 200.0)}, "rules_bad": ["DiskFull"],
             "alertmanagers": 0}
    bad = upgrades.compare(before, worse)
    assert any("Content-Type" in b for b in bad) and any("DiskFull" in b for b in bad) and any("Alertmanager" in b for b in bad)
    assert not any("Alertmanager" in b for b in upgrades.compare(before, worse, alerting=False))
    assert upgrades.compare(before, {"ready": False, "error": "refused"}) == ["not answering: refused"]


def test_a_trial_sends_no_alerts():
    cfg = "global:\n  scrape_interval: 15s\nalerting:\n  alertmanagers:\n  - static_configs:\n    - targets: ['localhost:9093']\nrule_files:\n  - r.yml\n"
    out = upgrades.strip_alerting(cfg)
    assert "alertmanagers" not in out and "rule_files:\n  - r.yml" in out and "scrape_interval" in out


def test_prometheus_timestamps():
    assert upgrades._ts("2026-10-24T04:43:26.977234716Z") == pytest.approx(1792817006.977, abs=0.01)
    assert upgrades._ts("") == 0.0


# ── the repair loop ─────────────────────────────────────────────────────────
@pytest.fixture()
def one_finding(monkeypatch):
    from noctyx import orchestrator as cm
    calls = []

    def repair():
        calls.append(1)
        return False, "still failed"
    found = [heal.Finding("service", "head", "noctyx-console.service is failed", repair, "restart it"),
             heal.Finding("exporter", "gpu-01", "its exporter runs but Prometheus cannot scrape it")]
    monkeypatch.setattr(heal, "check_services", lambda c, st, head: [found[0]])
    monkeypatch.setattr(heal, "check_dropins", lambda *a: [])
    monkeypatch.setattr(heal, "_targets", lambda: [])
    monkeypatch.setattr(heal, "check_exporters", lambda *a: [found[1]])
    monkeypatch.setattr(heal, "check_textfiles", lambda *a: [])
    monkeypatch.setattr(heal, "check_rules", lambda *a: [])
    for name in ("check_disk", "check_units", "check_nas", "check_backups", "check_slots", "check_upgrades"):
        monkeypatch.setattr(heal, name, lambda *a: [])
    monkeypatch.setattr(cm, "reboot_pending", lambda name=None: {})
    return calls


def test_a_repair_is_tried_a_few_times_then_left_to_a_person(one_finding):
    c = {"server": {"address": "192.0.2.10", "hostname": "head"}, "hosts": []}
    for _ in range(heal.TRIES):
        st = heal.run(c, log=quiet)
        assert not st["problems"][0]["needs_person"] or len(one_finding) == heal.TRIES
    st = heal.run(c, log=quiet)
    assert len(one_finding) == heal.TRIES                                  # no fourth try
    svc = next(p for p in st["problems"] if p["check"] == "service")
    exp = next(p for p in st["problems"] if p["check"] == "exporter")
    assert svc["needs_person"] and exp["needs_person"]                    # no safe repair: a person at once
    assert len(heal.summary()["repairs"]) == heal.TRIES


def test_a_dry_run_repairs_nothing(one_finding):
    c = {"server": {"address": "192.0.2.10", "hostname": "head"}, "hosts": []}
    st = heal.run(c, dry=True, log=quiet)
    assert one_finding == [] and not st.get("repairs") and not heal.HEAL_FILE.exists()


def test_heal_waits_while_a_change_runs(one_finding):
    c = {"server": {"address": "192.0.2.10", "hostname": "head"}, "hosts": []}
    with change._held():
        st = heal.run(c, log=quiet)
    assert one_finding == [] and st["skipped"]


# ── the console's queue ─────────────────────────────────────────────────────
def test_a_failed_upgrade_cancels_the_queued_ones_like_it(monkeypatch):
    from noctyx import console
    jobs = {"a": {"id": "a", "action": "upgradeComponent", "params": {"host": "gpu-01", "component": "node_exporter"},
                  "done": True, "running": False, "rc": 1},
            "b": {"id": "b", "action": "upgradeComponent", "params": {"host": "gpu-02", "component": "node_exporter"},
                  "done": False, "running": False},
            "c": {"id": "c", "action": "upgradeComponent", "params": {"host": "gpu-02", "component": "restic"},
                  "done": False, "running": False},
            "d": {"id": "d", "action": "backup", "params": {}, "done": False, "running": False}}
    monkeypatch.setattr(console, "JOBS", jobs)
    console._cancel_like(jobs["a"])
    assert "gpu-01" in jobs["b"]["cancelled"] and "cancelled" not in jobs["c"] and "cancelled" not in jobs["d"]
