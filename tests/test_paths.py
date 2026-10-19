import re

from noctyx import paths


def test_defaults_are_noctyx_layout():
    assert paths.DEFAULTS["agent_bin"] == "/usr/local/bin/noctyx-agent"
    assert paths.DEFAULTS["result_token"] == "NOCTYX_RESULT"


def test_site_overrides_and_unknown_keys_ignored(site_config, tmp_path):
    p = paths.load(str(site_config))
    assert p["agent_bin"] == "/usr/local/bin/legacy-agent"
    assert p["node_etc"] == "/etc/legacy"
    assert p["state"] == paths.DEFAULTS["state"]
    bad = tmp_path / "s.json"
    bad.write_text('{"paths": {"nonsense": "/x"}}')
    assert "nonsense" not in paths.load(str(bad))


def test_env_overrides(site_config, monkeypatch):
    monkeypatch.setenv("NOCTYX_PATH_STATE", "/tmp/nx-state")
    assert paths.load(str(site_config))["state"] == "/tmp/nx-state"


def test_render_fills_every_node_template(site_config):
    from noctyx import orchestrator as o
    p = paths.load(str(site_config))
    for name in ("noctyx-agent.sh", "noctyx-dumps.py"):
        out = paths.render((o.NX_HOME / "agent" / name).read_text(), p)
        assert not re.search(r"@[A-Z_]+@", out), name
    agent = paths.render((o.NX_HOME / "agent" / "noctyx-agent.sh").read_text(), p)
    assert "printf 'LEGACY_RESULT %s" in agent and "/etc/legacy/client.conf" in agent


def test_units_render_completely():
    from noctyx import orchestrator as o
    c = o.cfg()
    for n in o.HEAD_UNITS:
        assert not re.search(r"@[A-Z_]+@", o.render_unit(c, n)), n


def test_common_exclusions_take_site_extras():
    from noctyx import orchestrator as o
    text = o.common_exclusions(o.cfg())
    assert "/data/**" in text and "/boot" not in [l.strip() for l in text.splitlines()]


def test_agent_result_parsing_accepts_old_and_new_tokens():
    from noctyx import orchestrator as o
    out = 'noise\nLEGACY_RESULT {"a": 1}\nNOCTYX_RESULT {"b": 2}\nCLUSTERMAN_RESULT {"c": 3}\n'
    assert o.result_lines(out) == ['{"a": 1}', '{"b": 2}', '{"c": 3}']


def test_shipped_files_are_not_empty():
    """A truncated template ships silently (an empty cloud-init seed boots the stock installer)."""
    from noctyx import orchestrator as o
    empty = [str(f) for f in o.NX_HOME.rglob("*") if f.is_file() and f.stat().st_size == 0
             and f.name != "__init__.py" and "__pycache__" not in f.parts]
    assert not empty, empty


def test_restore_templates_render_completely():
    from noctyx import orchestrator as o
    c = o.cfg()
    for f in (o.NX_HOME / "restore").rglob("*"):
        if f.is_file() and "__pycache__" not in f.parts:
            out = o.render_site(c, f.read_text())
            assert not re.search(r"@[A-Z_]+@", out), f.name
    seed = (o.NX_HOME / "restore" / "cidata" / "user-data").read_text()
    assert "bootcmd:" in seed and "noctyx-menu.sh" in seed


def test_console_defaults_validation():
    from noctyx import orchestrator as o
    ok, errs = o.validate_console({"mode": "system", "theme": "catppuccin", "tunit": "C",
                                   "custom": {"bg": "#101010", "accent": "#ABCDEF"}})
    assert not errs and ok["custom"]["accent"] == "#abcdef"
    for bad in ({"mode": "dim"}, {"theme": "nope"}, {"tunit": "K"}, {"custom": {"bg": "red"}}, {"custom": {"x": "#000000"}}):
        assert o.validate_console(bad)[1], bad


def test_nas_name_from_the_spindle_holding_the_repositories():
    from noctyx import orchestrator as o
    c = {"server": {"repo_root": "/mnt/store/backups"}, "spindles": {"hot": {"mount": "/mnt/nvme"}, "store": {"mount": "/mnt/store"}}}
    assert o.nas_name(c) == "store"
    c["server"]["nas_name"] = "explicit"
    assert o.nas_name(c) == "explicit"
    assert o.nas_name({"server": {}, "spindles": {}}) == "nas"


def test_accounts_passwords_sessions_roles(tmp_path, monkeypatch):
    from noctyx import accounts as ac
    monkeypatch.setitem(ac.P, "etc", str(tmp_path))
    monkeypatch.setitem(ac.P, "console_state", str(tmp_path))
    ac.add("alice", "operator", "correct horse battery")
    assert (tmp_path / "users.json").stat().st_mode & 0o777 == 0o600
    assert "correct horse" not in (tmp_path / "users.json").read_text()
    assert ac.verify("alice", "correct horse battery") == "operator"
    assert ac.verify("alice", "wrong") is None and ac.verify("nobody", "x" * 12) is None
    s = ac.make_session("alice", ttl=60)
    assert ac.check_session(s) == ("alice", "operator")
    name, exp, sig = s.rsplit(".", 2)
    assert ac.check_session(f"{name}.{int(exp) + 999}.{sig}") is None        # tampered expiry
    assert ac.check_session(f"bob.{exp}.{sig}") is None                        # another name
    assert ac.check_session(ac.make_session("alice", ttl=-1)) is None         # expired
    ac.set_role("alice", "admin")
    assert ac.check_session(s) == ("alice", "admin")                           # role read live
    ac.delete("alice")
    assert ac.check_session(s) is None                                         # gone with the user
    import pytest
    with pytest.raises(ValueError):
        ac.add("Bad Name", "admin", "long enough pw")
    with pytest.raises(ValueError):
        ac.add("carol", "root", "long enough pw")
    with pytest.raises(ValueError):
        ac.add("carol", "viewer", "short")


def test_demo_data_is_scrubbed_and_served():
    import json, pathlib, re
    from noctyx import console
    d = pathlib.Path(console.__file__).parent / "demo"
    blob = (d / "fixtures.json").read_text() + (d / "site.json").read_text()
    for bad in (r"192\.168\.", r"\b10\.\d+\.\d+\.\d+\b", r"/home/(?!ops\b)", r"\.ts\.net"):
        assert not re.search(bad, blob), bad
    console.DEMO = str(d / "fixtures.json")
    body = json.loads(console.demo_answer("/api/v1/overview"))
    assert body["demo"] is True
    assert console.demo_answer("/api/v1/metrics/spark-1?tab=gpu") is not None
    assert console.demo_answer("/api/v1/no-such-report") is None
