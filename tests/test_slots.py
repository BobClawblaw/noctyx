"""Slots and what they really serve: an adopted deployment that names its models shows a swap made by hand,
`recipe-adopt` records what answers now, a watch-only slot is never switched, and self-repair reports a swap
once it has lasted. Also `status`: each audit record as a readable line, and the network note with its date."""
import argparse
import json

import pytest

from noctyx import heal
from noctyx import orchestrator as o

SLOTS = {"gpu": {"head": "gpu-01", "port": 30000,
                 "adopt": {"unit": "llm-server.service", "name": "llm-server (by hand)"}},
         "mac": {"head": "gpu-01", "port": 8888, "deploy": False}}


@pytest.fixture()
def site(tmp_path, monkeypatch):
    c = {"server": {"hostname": "head", "address": "192.0.2.10", "ssh": {"user": "ops", "key": "/k"}},
         "hosts": [{"name": "gpu-01", "address": "192.0.2.21", "arch": "x86_64", "repo": "gpu-01"}],
         "recipes": {"slots": json.loads(json.dumps(SLOTS)), "catalog": {}},
         "schedule": {}}
    f = tmp_path / "site.json"
    f.write_text(json.dumps(c, indent=2))
    monkeypatch.setattr(o, "CFG_PATH", str(f))
    monkeypatch.setattr(o, "RECIPE_STATE", tmp_path / "recipes.json")
    monkeypatch.setattr(o, "STATE_DIR", tmp_path)
    monkeypatch.setattr(o, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(o, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(o, "DISCOVER_FILE", tmp_path / "discover.json")
    from noctyx import selfstate
    monkeypatch.setattr(selfstate, "save_config", lambda key: None)
    serving = {"gpu": ["model-a"], "mac": ["model-m"]}
    monkeypatch.setattr(o, "recipe_live", lambda c, n, timeout=3: (
        {"ok": True, "models": serving[n]} if serving.get(n) else {"ok": False, "models": [], "error": "refused"}))
    units = {"llm-server.service", "other.service"}
    monkeypatch.setattr(o, "ssh", lambda c, h, cmd, timeout=0: argparse.Namespace(
        stdout="loaded\n" if any(u in cmd for u in units) else "not-found\n", returncode=0))
    return serving


def adopt(**kw):
    a = argparse.Namespace(slot="gpu", unit=None, name=None, spark_dir=None, yes=True)
    a.__dict__.update(kw)
    return o.cmd_recipe_adopt(a)


def test_a_slot_adopted_by_name_only_cannot_show_a_swap(site):
    s = o.recipe_summary(o.cfg())["slots"]["gpu"]
    assert s["drift"] is None and s["unrecorded"]
    site["gpu"] = ["model-b"]                                # swapped by hand: still nothing to compare with
    assert o.recipe_summary(o.cfg())["slots"]["gpu"]["drift"] is None


def test_adopt_records_what_answers_and_a_later_swap_is_drift(site):
    adopt()
    c = o.cfg()
    ad = c["recipes"]["slots"]["gpu"]["adopt"]
    assert ad["models"] == ["model-a"] and ad["unit"] == "llm-server.service" and ad["name"] == "model-a"
    s = o.recipe_summary(c)["slots"]["gpu"]
    assert s["drift"] is None and not s["unrecorded"]
    site["gpu"] = ["model-b"]
    s = o.recipe_summary(c)["slots"]["gpu"]
    assert "model-b" in s["drift"] and "model-a" in s["drift"]
    adopt(unit="other.service", name="b by hand")            # recording the swap ends the drift
    c = o.cfg()
    assert o.recipe_summary(c)["slots"]["gpu"]["drift"] is None
    assert c["recipes"]["slots"]["gpu"]["adopt"]["unit"] == "other.service"
    assert c["recipes"]["slots"]["gpu"]["conflicts"] == ["llm-server.service", "other.service"]  # a deploy disables both
    assert [json.loads(l)["event"] for l in o.AUDIT.read_text().splitlines()] == ["recipe-adopt"] * 2


def test_adopt_changes_nothing_without_yes_or_with_a_wrong_unit(site):
    before = open(o.CFG_PATH).read()
    with pytest.raises(SystemExit) as e:
        adopt(yes=False)
    assert e.value.code == 2 and open(o.CFG_PATH).read() == before
    with pytest.raises(SystemExit, match="not a unit on gpu-01"):
        adopt(unit="missing.service")
    with pytest.raises(SystemExit, match="not a service unit name"):
        adopt(unit="x; rm -rf /")
    site["gpu"] = None
    with pytest.raises(SystemExit, match="does not answer"):
        adopt()
    assert open(o.CFG_PATH).read() == before


def test_adopting_over_noctyx_own_deployment_keeps_it_as_previous(site):
    own = {"recipe": "r", "sha": "a" * 40, "unit": "noctyx-recipe-gpu.service", "models": ["model-x"],
           "state": "serving"}
    o._save_recipe_state({"slots": {"gpu": {"current": own}}})
    with pytest.raises(SystemExit, match="Noctyx's own deployment"):
        adopt()                                              # which unit serves now must be said
    adopt(unit="other.service")
    cur, prev, hist = o.slot_records(o.cfg(), "gpu")
    assert cur["adopted"] and cur["models"] == ["model-a"] and prev == own and hist[0]["event"] == "adopt"


def test_a_watch_only_slot_is_never_switched_and_adopts_without_a_unit(site):
    c = o.cfg()
    assert any("watch-only" in b for b in o.recipe_plan(c, "mac")["blockers"])
    assert not o.recipe_plan(c, "gpu")["blockers"]
    adopt(slot="mac")
    c = o.cfg()
    assert c["recipes"]["slots"]["mac"]["adopt"]["models"] == ["model-m"]
    assert "unit" not in c["recipes"]["slots"]["mac"]["adopt"] and "conflicts" not in c["recipes"]["slots"]["mac"]
    site["mac"] = ["model-n"]
    s = o.recipe_summary(c)["slots"]["mac"]
    assert s["deploy"] is False and "model-n" in s["drift"]


def test_heal_reports_a_swap_once_it_has_lasted(site, monkeypatch):
    adopt()
    c, st = o.cfg(), {}
    assert heal.check_serving(c, st) == [] and st["serving"] == {}
    site["gpu"] = ["model-b"]
    assert heal.check_serving(c, st) == []                   # just swapped: a deploy in flight looks the same
    st["serving"]["gpu"] -= heal.SLOT_DOWN_FOR + 1
    f, = heal.check_serving(c, st)
    assert f.check == "serving" and f.host == "gpu-01" and f.repair is None and "recipe-adopt --slot gpu" in f.why
    assert "model-b" in f.what and "model-a" in f.what
    monkeypatch.setattr(o, "spindle_holder", lambda n: {"what": "recipe-deploy"})
    assert heal.check_serving(c, st) == [] and "gpu" not in st["serving"]   # a switch in progress
    monkeypatch.setattr(o, "spindle_holder", lambda n: {})
    site["gpu"] = ["model-a"]
    assert heal.check_serving(c, st) == []


def test_status_lines_say_what_happened():
    line = o.audit_line({"ts": "2026-01-02T03:04:05+00:00", "event": "alert", "status": "firing", "name": "NasLowSpace",
                         "host": "gpu-01", "summary": "the NAS is 91% full", "starts": "x"})
    assert line == "2026-01-02T03:04:05+00:00 gpu-01     alert          firing NasLowSpace: the NAS is 91% full"
    assert o.audit_line({"ts": "T", "event": "ui-trigger", "action": "healNow"}).split() == ["T", "ui-trigger", "healNow"]
    assert "3 item(s), 1 fixed" in o.audit_line({"ts": "T", "event": "hygiene", "host": "h", "items": 3, "fixed": 1})
    assert o.audit_line({"ts": "T", "event": "backup", "host": "h", "rc": 0, "snapshot": "abc", "empty": ""}).endswith(
        "backup         rc=0 snapshot=abc")
    assert "rc=" not in o.audit_line({"ts": "T", "event": "versions", "behind": 0, "upgradable": 2})


def test_the_network_note_carries_its_date_and_the_newest_readings():
    now = 1_000_000.0
    note = {"expected_client_to_server_mbit": 1000, "measured_client_to_server_mbit": 90, "degraded": True,
            "degraded_reason": "a bad cable", "measured_ts": "2026-01-02"}
    probes = {"mbit": {"gpu-01": 400.0, "gpu-02": 950.0, "gone": 5.0},
              "at": {"gpu-01": now - 3600, "gpu-02": now - 60, "gone": now - 30 * 86400}}
    out = "\n".join(o.network_lines(note, probes, now))
    assert "gpu-01 400, gpu-02 950 Mbit/s (expected 1000)" in out and "gone" not in out
    assert "NETWORK DEGRADED (noted 2026-01-02): a bad cable" in out and "out of date" not in out
    probes["mbit"]["gpu-01"] = 990.0
    assert "the note is out of date" in "\n".join(o.network_lines(note, probes, now))
    plain = {"expected_client_to_server_mbit": 1000}
    assert "below half the expected speed: gpu-01" in "\n".join(
        o.network_lines(plain, {"mbit": {"gpu-01": 300.0}, "at": {"gpu-01": now}}, now))
    assert o.network_lines(plain, {}, now) == []
