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


# ── hygiene: what a site hands to the daily run ─────────────────────────────
def test_hygiene_auto_is_a_sites_choice_from_a_fixed_list(site, monkeypatch, capsys):
    assert o.hygiene_auto(o.cfg()) == []
    for bad in ('{"auto": ["docker-containers"]}', '{"auto": ["unit:nginx.service"]}', '{"auto": "journal"}',
                '{"other": []}', '["journal"]', 'nope'):
        with pytest.raises(SystemExit, match="invalid"):
            o.cmd_hygiene_set(argparse.Namespace(json=bad))
    o.cmd_hygiene_set(argparse.Namespace(json='{"auto": ["docker-images", "journal"]}'))
    assert o.hygiene_auto(o.cfg()) == ["journal", "docker-images"]
    sent = []
    monkeypatch.setattr(o, "HYGIENE_FILE", o.STATE_DIR / "hygiene.json")
    monkeypatch.setattr(o, "ssh", lambda c, h, cmd, timeout=0: sent.append(cmd) or argparse.Namespace(
        stdout=o.P["result_token"] + ' {"event": "hygiene", "items": []}\n', stderr="", returncode=0))
    o.cmd_hygiene(argparse.Namespace(host=None, fix="auto"))          # the daily run
    o.cmd_hygiene(argparse.Namespace(host=None, fix="manual"))        # anything else goes as asked
    o.cmd_hygiene(argparse.Namespace(host=None, fix=None))
    assert [s.split("hygiene", 1)[1] for s in sent] == [" --fix auto,journal,docker-images", " --fix manual", ""]
    o.cmd_hygiene_set(argparse.Namespace(json='{"auto": []}'))
    assert o.hygiene_auto(o.cfg()) == [] and "hygiene" in o.cfg() and "auto" not in o.cfg()["hygiene"]


def test_the_agent_fixes_a_tier_named_in_a_list_but_a_failed_unit_only_by_id():
    import re
    import subprocess
    src = (o.NX_HOME / "agent" / "noctyx-agent.sh").read_text()
    fn = re.search(r"^hyg_want_fix\(\) \{.*?^  return 1; \}", src, re.M | re.S).group(0)

    def want(fix, tier, item):
        return subprocess.run(["bash", "-c", fn + f'\nHYG_FIX="$1"; hyg_want_fix "$2" "$3"', "_", fix, tier, item]).returncode == 0
    assert want("auto", "auto", "reboot-flag") and not want("auto", "manual", "journal")
    assert want("auto,journal", "auto", "reboot-flag") and want("auto,journal", "manual", "journal")
    assert not want("auto,journal", "manual", "docker-images") and not want("", "auto", "reboot-flag")
    assert want("manual", "manual", "old-kernels") and not want("manual", "explicit", "unit:nginx.service")
    assert not want("auto,explicit", "explicit", "unit:nginx.service")        # never by tier
    assert want("unit:nginx.service", "explicit", "unit:nginx.service") and want("all", "explicit", "unit:x.service")
