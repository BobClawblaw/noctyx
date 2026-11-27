"""End-to-end integration tests against the simulated AI cluster (stub OpenAI slot, Prometheus, Alertmanager, restic)."""
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
from sim_cluster import SimulatedCluster  # noqa: E402

from noctyx import alerting, answers, console, orchestrator as o, targets as bt


def test_simulated_cluster_slot_answers_and_alerting(tmp_path, monkeypatch):
    with SimulatedCluster(hosts=["gpu-01"], model="sim-qwen-7b") as cl:
        site = {
            "server": {"hostname": "head", "address": "127.0.0.1", "ui_port": 20996,
                       "repo_root": str(tmp_path / "primary"),
                       "ssh": {"user": "ops", "key": "/k"},
                       "tls": {"cert": "/c", "key": "/k", "ca": "/ca"}},
            "hosts": [{"name": "gpu-01", "address": "127.0.0.1", "arch": "x86_64", "repo": "gpu-01"}],
            "repositories": [{"id": "gpu-01", "spindle": "nas"}],
            "recipes": {"slots": {"gpu": {"head": "gpu-01", "port": cl.slot.port,
                                          "adopt": {"unit": "sim.service", "name": "sim", "models": ["sim-qwen-7b"]}}},
                        "catalog": {}},
            "alerting": {"repeat_interval": "4h", "channels": {
                "phone": {"kind": "ntfy", "url": "https://ntfy.sh/sim-topic", "min_severity": "warning"}}},
            "backup_targets": {"usb": {"kind": "local", "path": str(tmp_path / "usb"), "mount": str(tmp_path)}},
            "hf_mirror": {"store": str(tmp_path / "hf"), "port": 20995, "write_through": True},
            "schedule": {},
        }
        cfg_f = tmp_path / "site.json"
        cfg_f.write_text(json.dumps(site))
        monkeypatch.setattr(o, "CFG_PATH", str(cfg_f))
        monkeypatch.setattr(o, "RECIPE_STATE", tmp_path / "recipes.json")
        monkeypatch.setattr(o, "STATE_DIR", tmp_path)
        monkeypatch.setattr(answers, "FILE", tmp_path / "answers.json")

        # 1. Live slot health & models via real HTTP
        lv = o.recipe_live(o.cfg(), "gpu")
        assert lv["ok"] is True and lv["models"] == ["sim-qwen-7b"]

        # 2. Answer check records baseline, then passes, then detects drift (two checks in a row)
        r1 = answers.check_slot(o.cfg(), "gpu")
        assert r1["last"]["state"] == "recorded"
        r2 = answers.check_slot(o.cfg(), "gpu")
        assert r2["last"]["state"] == "ok"
        cl.slot.answer_prefix = "Slightly different wording: "
        answers.check_slot(o.cfg(), "gpu")               # first different reply is pending
        r3 = answers.check_slot(o.cfg(), "gpu")          # twice in a row -> changed
        assert r3["last"]["state"] == "changed"

        # 3. Alerting test posts real alert into simulated Alertmanager
        res = alerting.send_test_alert(am_url=f"http://127.0.0.1:{cl.alertmanager.port}", host="head")
        assert res["ok"] is True
        assert len(cl.alertmanager.alerts) == 1
        assert cl.alertmanager.alerts[0]["labels"]["alertname"] == "NoctyxTestAlert"

        # 4. Console policy_summary exposes alerting, targets, and write_through; copyTarget validates
        monkeypatch.setattr(console, "assistant_summary", lambda: {})
        monkeypatch.setattr(console, "heal_config", lambda c: {})
        monkeypatch.setattr(console, "checkpoint_config", lambda c: {})
        monkeypatch.setattr(console, "hf_summary", lambda c: {**o.hf_cfg(c), "service": "active"})
        ps = console.policy_summary()
        assert ps["hf"]["write_through"] is True
        assert "phone" in ps["alerting"]["config"]["channels"]
        assert ps["targets"]["items"]["usb"]["ready"] is True
        assert console.valid_params("copyTarget", {"target": "usb", "dry_run": True}) is True
        assert console.valid_params("copyTarget", {"target": "nonexistent"}) is False


def test_real_restic_primary_to_secondary_target_copy(tmp_path, monkeypatch):
    """When restic is installed on the test machine, run a genuine backup + secondary target copy round trip."""
    if not shutil.which("restic"):
        return
    monkeypatch.setitem(bt.P, "repo_keys", str(tmp_path / "keys"))
    monkeypatch.setitem(bt.P, "etc", str(tmp_path / "etc"))
    (tmp_path / "keys").mkdir()
    key = tmp_path / "keys" / "gpu-01.key"
    key.write_text("sim-restic-secret-key\n")

    primary = tmp_path / "primary" / "nx-gpu-01" / "gpu-01"
    primary.parent.mkdir(parents=True)
    src_data = tmp_path / "data"
    src_data.mkdir()
    (src_data / "hello.txt").write_text("noctyx 3-2-1 backup test\n")

    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp_path),
           "RESTIC_PASSWORD_FILE": str(key)}
    subprocess.run(["restic", "-r", str(primary), "--no-cache", "init"], check=True, env=env, capture_output=True)
    subprocess.run(["restic", "-r", str(primary), "--no-cache", "backup", "--host", "gpu-01", str(src_data)],
                   check=True, env=env, capture_output=True)

    sec_root = tmp_path / "secondary"
    sec_root.mkdir()
    site = {"server": {"repo_root": str(tmp_path / "primary")},
            "repositories": [{"id": "gpu-01", "spindle": "nas"}]}
    target = {"kind": "local", "path": str(sec_root / "repos"), "mount": str(sec_root)}

    ok, total = bt.copy_to_target(site, "usb", target, host="gpu-01", dry_run=False)
    assert (ok, total) == (1, 1)

    # Verify snapshot actually landed in the secondary repository
    sec_repo = sec_root / "repos" / "nx-gpu-01" / "gpu-01"
    snaps = json.loads(subprocess.check_output(
        ["restic", "-r", str(sec_repo), "--no-cache", "snapshots", "--json"], env=env, text=True
    ))
    assert len(snaps) == 1 and snaps[0]["hostname"] == "gpu-01"
