"""The diagnosis of a problem self-repair leaves to a person: the evidence a person would look up (read with
fixed commands, redacted), one request per problem while it says the same thing, a budget per run, and a broken
assistant that never breaks the repair loop."""
import json
import time

import pytest

from noctyx import change, diagnose, heal, llm
from noctyx import orchestrator as o

C = {"server": {"hostname": "head", "address": "192.0.2.10"},
     "hosts": [{"name": "gpu-01", "address": "192.0.2.21", "arch": "x86_64", "repo": "gpu-01"},
               {"name": "mac-01", "address": "192.0.2.30", "os": "macos"}]}


def problem(**kw):
    p = {"key": "units/gpu-01", "check": "units", "host": "gpu-01", "what": "myapp.service failed",
         "detail": "exit 1", "why": "not in restart_units: a person's turn", "since": time.time() - 7200,
         "needs_person": True, "state": "needs_person"}
    p.update(kw)
    return p


@pytest.fixture(autouse=True)
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(o, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(change, "LEDGER", tmp_path / "changes.jsonl")
    monkeypatch.setattr(llm, "configured", lambda config=None: {"enabled": True, "url": "http://192.0.2.9/v1"})
    monkeypatch.delenv("NOCTYX_NO_ASSIST", raising=False)
    (tmp_path / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"ts": "T1", "event": "backup", "host": "gpu-01", "rc": 0, "snapshot": "abc"},
        {"ts": "T2", "event": "hygiene", "host": "gpu-01", "items": 2},
        {"ts": "T3", "event": "backup", "host": "gpu-02", "rc": 0})) + "\n")
    change.record({"ts": "T0", "ts_epoch": time.time() - 3600, "host": "gpu-01", "what": "node_exporter 1.12.1 on gpu-01",
                   "outcome": "ok"})
    change.record({"ts": "Tx", "ts_epoch": time.time() - 9 * 86400, "host": "gpu-01", "what": "an old change", "outcome": "ok"})


def test_the_evidence_is_what_a_person_would_look_up_first_and_is_redacted():
    ran = []

    def run(c, host, cmd, timeout=0):
        ran.append((host["name"], cmd))
        return "Nov 01 myapp[1]: connect failed, token=abcdef0123456789abcdef password: hunter2-very-secret"
    ev = diagnose.evidence(C, problem(), [{"key": "units/gpu-01", "ts": "T", "how": "restart it", "ok": False, "detail": "still failed"},
                                          {"key": "disk/gpu-01", "how": "other", "ok": True}], run)
    assert "problem: myapp.service failed" in ev and "open for: 2h00m" in ev
    assert "restart it -> failed: still failed" in ev and "other" not in ev
    assert len(ran) == 1 and ran[0][0] == "gpu-01" and "journalctl" in ran[0][1] and "-u myapp.service" in ran[0][1]
    assert "connect failed" in ev and "hunter2" not in ev and "abcdef0123456789" not in ev
    assert "node_exporter 1.12.1 on gpu-01: ok" in ev and "an old change" not in ev
    assert "snapshot=abc" in ev and "gpu-02" not in ev and "hygiene" not in ev.split("audit trail")[1]
    ran.clear()
    diagnose.evidence(C, problem(check="backups", what="the last two backups failed", detail=""), (), run)
    assert [("last-backup.log" in c) for _, c in ran] == [True]
    ran.clear()
    diagnose.evidence(C, problem(host="mac-01", key="units/mac-01"), (), run)          # a Mac: nothing is run there
    assert ran == []


def test_a_unit_name_cannot_carry_a_command():
    ran = []
    diagnose.evidence(C, problem(what="x.service; rm -rf / failed", detail="$(reboot).service"), (),
                      lambda c, h, cmd, timeout=0: ran.append(cmd) or "")
    assert all("rm -rf" not in c and "$(" not in c for c in ran)


def test_each_problem_is_diagnosed_once_while_it_says_the_same(monkeypatch):
    asked = []

    def ask(a, system, user, max_tokens=0):
        asked.append(user)
        return f"cause {len(asked)}", "model-a"
    run = lambda c, h, cmd, timeout=0: "log"
    quiet = lambda *a, **k: None
    p1, p2, p3 = problem(), problem(key="disk/gpu-01", check="disk", what="/ is 95% full"), problem(key="x/gpu-01", what="third")
    waiting = problem(key="w/gpu-01", needs_person=False)
    frozen = problem(key="heal/head", check="heal")
    assert diagnose.attach(C, [p1, p2, p3, waiting, frozen], {}, log=quiet, run=run, ask=ask) == 2     # the budget
    assert p1["diagnosis"]["text"] == "cause 1" and p2["diagnosis"]["model"] == "model-a" and "diagnosis" not in p3
    assert "diagnosis" not in waiting and "diagnosis" not in frozen
    old = {p["key"]: p for p in (p1, p2, p3)}
    n1, n2, n3 = problem(), problem(key="disk/gpu-01", check="disk", what="/ is 97% full"), problem(key="x/gpu-01", what="third")
    assert diagnose.attach(C, [n1, n2, n3], old, log=quiet, run=run, ask=ask) == 2
    assert n1["diagnosis"]["text"] == "cause 1"                         # the same problem: kept, not asked again
    assert n2["diagnosis"]["text"] == "cause 3" and n3["diagnosis"]["text"] == "cause 4"   # changed; new
    old = {"units/gpu-01": {**n1, "diagnosis": {**n1["diagnosis"], "ts": time.time() - diagnose.AGAIN_AFTER - 1}}}
    again = problem()
    diagnose.attach(C, [again], old, log=quiet, run=run, ask=ask)
    assert again["diagnosis"]["text"] == "cause 5"                      # a day later: looked at again


def test_no_assistant_or_a_broken_one_changes_nothing(monkeypatch):
    quiet = lambda *a, **k: None
    run = lambda c, h, cmd, timeout=0: "log"
    p = problem()
    assert diagnose.attach(C, [p], {}, log=quiet, run=run, ask=lambda *a, **k: (None, "no endpoint")) == 1
    assert "diagnosis" not in p

    def boom(*a, **k):
        raise RuntimeError("the model server fell over")
    had = {"units/gpu-01": {"diagnosis": {"text": "earlier", "model": "m", "ts": 0, "of": "other"}}}
    diagnose.attach(C, [p], had, log=quiet, run=run, ask=boom)
    assert p["diagnosis"]["text"] == "earlier"                          # the earlier one stands
    monkeypatch.setenv("NOCTYX_NO_ASSIST", "1")
    assert diagnose.attach(C, [problem()], {}, log=quiet, run=run, ask=boom) == 0
    monkeypatch.delenv("NOCTYX_NO_ASSIST")
    monkeypatch.setattr(llm, "configured", lambda config=None: None)
    assert diagnose.diagnose(C, problem(), run=run) is None


def test_the_repair_loop_keeps_the_diagnosis_with_the_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(heal, "HEAL_FILE", tmp_path / "heal.json")
    monkeypatch.setattr(heal, "LOG_FILE", tmp_path / "heal-log.jsonl")
    monkeypatch.setattr(heal, "LOCK_FILE", tmp_path / "heal.lock")
    monkeypatch.setattr(change, "LOCK", tmp_path / "change.lock")
    monkeypatch.setattr(o, "reboot_pending", lambda name=None: {})
    for name in ("check_services", "check_dropins", "check_exporters", "check_textfiles", "check_rules", "check_disk",
                 "check_nas", "check_backups", "check_slots", "check_serving", "check_answers", "check_upgrades"):
        monkeypatch.setattr(heal, name, lambda *a: [])
    monkeypatch.setattr(heal, "_targets", lambda: [])
    monkeypatch.setattr(heal, "check_units", lambda *a: [heal.Finding("units", "gpu-01", "myapp.service failed",
                                                                      why="not in restart_units")])
    monkeypatch.setattr(diagnose, "_run", lambda c, h, cmd, timeout=0: "log")
    asked = []
    monkeypatch.setattr(llm, "ask_any", lambda a, system, user, max_tokens=0: (asked.append(1) or "it ran out of memory", "model-a"))
    monkeypatch.setattr(diagnose, "evidence", lambda c, p, repairs=(), run=None: "evidence")
    cfg = {**C, "hosts": [], "heal": {}}
    out = []
    st = heal.run(cfg, log=out.append)
    assert st["problems"][0]["diagnosis"]["text"] == "it ran out of memory" and any("diagnosis (model-a)" in l for l in out)
    assert heal.load()["problems"][0]["diagnosis"]["model"] == "model-a"
    heal.run(cfg, log=out.append)
    assert len(asked) == 1                                              # the next run keeps it
    heal.run(cfg, dry=True, log=out.append)
    assert len(asked) == 1
    assert any(s.get("kind") == "diagnosis" for r in heal.runs(everything=True) for s in r.get("steps", []))
