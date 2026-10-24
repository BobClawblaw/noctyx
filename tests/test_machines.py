"""/machines: every machine on one page, worst first, each saying why it needs a look."""
from noctyx import console, heal


def test_the_summary_puts_problems_first_and_says_why(monkeypatch):
    hosts = [{"name": n, "repo": "r", "arch": "aarch64", "os": "linux", "monitored_only": False} for n in ("gpu-01", "gpu-02", "gpu-03")]
    monkeypatch.setattr(console, "_MACHINES", {"ts": 0, "data": None})
    monkeypatch.setattr(console, "overview", lambda: {"hosts": hosts, "facts": {}, "versions": {"hosts": {}},
        "snapshots": {"hosts": {}}, "schedule": {"rows": [
            {"host": "gpu-01", "profile": "system", "enabled": True, "health": "ok", "last_ts": 1},
            {"host": "gpu-02", "profile": "system", "enabled": True, "health": "stale", "last_ts": 1}]}})
    monkeypatch.setattr(console, "fleet_metrics", lambda: {"hosts": {
        "gpu-01": {"up": 1, "root_pct": 40, "llm": {"servers": [{"model": "m", "gen_tps": 12.5}]}},
        "gpu-02": {"up": 1, "root_pct": 40},
        "gpu-03": {"up": 1, "root_pct": 88, "reboot": 1, "llm": {"worker_for": "gpu-01", "worker_model": "m"}}}, "fleet": {}})
    monkeypatch.setattr(console, "alerts_view", lambda: {"active": []})
    monkeypatch.setattr(heal, "summary", lambda: {"problems": [], "repairs": []})
    monkeypatch.setattr(console, "_signals", lambda: {"ssh_new": {"gpu-03": [({"addr": "192.0.2.9", "user": "ops"}, 1.0)]}})
    monkeypatch.setattr(console.CM, "cfg", lambda: {})
    monkeypatch.setattr(console.CM, "recipes_cfg", lambda c: {"slots": {"pair": {"head": "gpu-01", "worker": "gpu-03"}}})
    monkeypatch.setattr(console.CM, "is_head", lambda c, h: False)
    monkeypatch.setattr(console, "CFG", {"hosts": hosts})
    d = console.machines_view()
    got = [(m["name"], m["status"]) for m in d["machines"]]
    assert got == [("gpu-02", "bad"), ("gpu-03", "warn"), ("gpu-01", "ok")]
    m = {x["name"]: x for x in d["machines"]}
    assert {"text": "system backup stale", "level": "bad"} in m["gpu-02"]["why"]
    why3 = [w["text"] for w in m["gpu-03"]["why"]]
    assert "root disk 88% full" in why3 and "reboot required" in why3 and "ssh login from a new address" in why3
    assert m["gpu-01"]["role"] == ["pair pair head"] and m["gpu-03"]["role"] == ["pair pair worker (with gpu-01)"]
    assert m["gpu-03"]["serves"] == [{"model": "m", "worker_for": "gpu-01"}]
    assert d["fleet"]["bad"] == 1 and d["fleet"]["warn"] == 1 and d["fleet"]["tps"] == 12.5
