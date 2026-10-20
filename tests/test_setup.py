"""First-time setup on both platforms: launchd made from the systemd units, mounts, the LLM link, noctyx init."""
import http.server
import json
import plistlib
import threading

import pytest

from noctyx import hostos, llm, setup
from noctyx import orchestrator as cm


def _units():
    c = cm.cfg()
    return {f"{cm.UNIT}-{n}": cm.render_unit(c, n) for n in cm.HEAD_UNITS + ("rest-server.service",) + setup.METRICS_UNITS}


def test_every_head_unit_has_a_launchd_form():
    units = _units()
    for n, txt in units.items():
        if not n.endswith(".service"):
            continue
        timer = units.get(n.replace(".service", ".timer"))
        if not timer and "Type=oneshot" in txt:
            continue
        p = plistlib.loads(hostos.launchd_plist(hostos.launchd_label(n, "dev.noctyx"), txt, timer).encode())
        assert p["Label"].startswith("dev.noctyx.") and p["ProgramArguments"][0].startswith("/")
        assert "@" not in json.dumps(p["ProgramArguments"]), n          # every template value filled in
        if timer:
            assert "StartCalendarInterval" in p or "StartInterval" in p
        else:
            assert p["RunAtLoad"] and p["KeepAlive"]


def test_launchd_keeps_user_env_and_schedule():
    units = _units()
    d = plistlib.loads(hostos.launchd_plist("dev.noctyx.directory", units[f"{cm.UNIT}-directory.service"]).encode())
    assert d["UserName"] == "noctyx" and d["EnvironmentVariables"]["NOCTYX_CONFIG"]
    rs = plistlib.loads(hostos.launchd_plist("dev.noctyx.rest-server", units[f"{cm.UNIT}-rest-server.service"]).encode())
    assert "--private-repos" in rs["ProgramArguments"] and "--append-only" in rs["ProgramArguments"]
    t = plistlib.loads(hostos.launchd_plist("dev.noctyx.retention", units[f"{cm.UNIT}-retention.service"],
                                            units[f"{cm.UNIT}-retention.timer"]).encode())
    assert t["StartCalendarInterval"] == {"Hour": 8, "Minute": 0, "Day": 1}
    assert "KeepAlive" not in t


@pytest.mark.parametrize("spec,want", [("*:0/15", {"StartInterval": 900}),
                                       ("*-*-* 07:30:00", {"StartCalendarInterval": {"Hour": 7, "Minute": 30}}),
                                       ("*-*-01 08:00:00", {"StartCalendarInterval": {"Hour": 8, "Minute": 0, "Day": 1}})])
def test_calendar(spec, want):
    assert hostos._calendar(spec) == want


def test_labels():
    assert hostos.launchd_label("noctyx-console.service", "dev.noctyx") == "dev.noctyx.console"
    assert hostos.launchd_label("noctyx-hf-mirror.service", "dev.noctyx") == "dev.noctyx.hf-mirror"


@pytest.mark.parametrize("mac", [True, False])
def test_mount_commands(monkeypatch, mac):
    monkeypatch.setattr(hostos, "IS_MAC", mac)
    nfs = setup.mount_argv({"proto": "nfs", "server": "192.0.2.5", "path": "/volume1/b"}, "/mnt/x")
    assert nfs[-2:] == ["192.0.2.5:/volume1/b", "/mnt/x"] and ("resvport" in nfs[4]) == mac
    smb = setup.mount_argv({"proto": "smb", "server": "192.0.2.5", "path": "b/noctyx", "user": "u"}, "/mnt/x",
                           "/etc/noctyx/smb-nas.cred")
    assert not any("password" in x for x in smb)                     # never on a command line
    assert smb[0] == ("mount_smbfs" if mac else "mount")


@pytest.mark.parametrize("mac", [True, False])
def test_as_user(monkeypatch, mac):
    monkeypatch.setattr(hostos, "IS_MAC", mac)
    argv = hostos.as_user("noctyx", ["restic", "init"])
    assert argv[-2:] == ["restic", "init"] and (argv[0] == "setpriv") != mac


class _LLM(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, d):
        b = json.dumps(d).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send({"data": [{"id": "tiny-model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["model"] == "tiny-model"
        self._send({"model": "tiny-model", "choices": [{"message": {"content": "Noctyx assistant OK"}}]})


@pytest.fixture()
def fake_llm():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_llm_discover_and_test(fake_llm):
    found = llm.discover(["127.0.0.1"], ports=(fake_llm, 1))
    assert [f["url"] for f in found] == [f"http://127.0.0.1:{fake_llm}/v1"] and found[0]["models"] == ["tiny-model"]
    t = llm.test(found[0]["url"])
    assert t["ok"] and t["model"] == "tiny-model"
    assert not llm.test("http://127.0.0.1:1/v1")["ok"]


def test_init_writes_a_working_site(tmp_path, monkeypatch, fake_llm):
    from noctyx import firstrun
    cfg = tmp_path / "site.json"
    monkeypatch.setattr(firstrun, "CONFIG", str(cfg))
    monkeypatch.setattr(firstrun.os, "geteuid", lambda: 0)
    monkeypatch.setitem(firstrun.P, "llm_keys", str(tmp_path / "keys"))
    monkeypatch.setattr(hostos, "IS_MAC", False)       # a Linux head: it backs itself up (a Mac head never does)
    rc = firstrun.main(["--yes", "--assistant", f"http://127.0.0.1:{fake_llm}/v1", "--hostname", "head-01",
                        "--address", "192.0.2.10", "--storage", "nfs", "--nas-server", "192.0.2.5",
                        "--nas-path", "/volume1/backups", "--mount", "/mnt/nas", "--owner-uid", "1026", "--no-auth"])
    assert rc == 0
    c = json.loads(cfg.read_text())
    assert c["assistant"]["url"].endswith(f":{fake_llm}/v1") and c["assistant"]["model"] == "tiny-model"
    assert c["spindles"]["nas"]["share"] == {"proto": "nfs", "server": "192.0.2.5", "path": "/volume1/backups"}
    assert c["server"]["repo_root"] == "/mnt/nas/noctyx/repos" and c["server"]["repo_owner_uid"] == 1026
    head = c["hosts"][0]
    assert head["local"] and head["repo"] == "head-01" and c["repositories"] == [{"id": "head-01", "spindle": "nas"}]
    assert cm.nas_name(c) == "nas" and {"system", "config"} <= set(c["profiles"])
    assert (cfg.stat().st_mode & 0o777) == 0o600
    # re-running keeps the site: its values are the defaults
    assert firstrun.main(["--yes", "--assistant", f"http://127.0.0.1:{fake_llm}/v1", "--no-auth"]) == 0
    c2 = json.loads(cfg.read_text())
    assert c2["spindles"] == c["spindles"] and c2["server"]["address"] == "192.0.2.10" and len(c2["hosts"]) == 1


def test_local_hosts_run_here_not_over_ssh():
    c = cm.cfg()
    assert cm._ssh_argv(c, {"name": "h", "address": "192.0.2.10", "local": True}, "true") == ["sh", "-c", "true"]
    assert cm._ssh_argv(c, {"name": "h", "address": "192.0.2.21"}, "true")[0] == "ssh"


def _init(tmp_path, monkeypatch, port, *flags, mac=False):
    from noctyx import firstrun
    cfg = tmp_path / "site.json"
    monkeypatch.setattr(firstrun, "CONFIG", str(cfg))
    monkeypatch.setattr(firstrun.os, "geteuid", lambda: 0)
    monkeypatch.setitem(firstrun.P, "llm_keys", str(tmp_path / "keys"))
    monkeypatch.setattr(firstrun, "_ssh_key", lambda c: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(firstrun, "_node_arch", lambda c, addr: "aarch64")
    monkeypatch.setattr(firstrun, "_time_machine", lambda: "tm")
    monkeypatch.setattr(hostos, "IS_MAC", mac)
    assert firstrun.main(["--yes", "--assistant", f"http://127.0.0.1:{port}/v1", "--hostname", "head-01",
                          "--address", "192.0.2.10", "--storage", "local", "--path", str(tmp_path / "nx"),
                          "--no-auth", *flags]) == 0
    return json.loads(cfg.read_text())


def test_a_mac_head_is_monitored_and_linux_nodes_get_repositories(tmp_path, monkeypatch, fake_llm):
    c = _init(tmp_path, monkeypatch, fake_llm, "--node", "gpu-01=192.0.2.21", "--node", "gpu-02=192.0.2.22", mac=True)
    head = c["hosts"][0]
    assert head["os"] == "macos" and head["backup"] is False and "repo" not in head
    assert [r["id"] for r in c["repositories"]] == ["gpu-01", "gpu-02"]
    assert [h["name"] for h in cm.backup_hosts(c)] == ["gpu-01", "gpu-02"]
    assert all(h["arch"] == "aarch64" and h["os"] == "linux" for h in c["hosts"][1:])


def test_a_linux_head_backs_itself_up(tmp_path, monkeypatch, fake_llm):
    c = _init(tmp_path, monkeypatch, fake_llm)
    assert c["hosts"][0]["repo"] == "head-01" and [r["id"] for r in c["repositories"]] == ["head-01"]
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    c = _init(fresh, monkeypatch, fake_llm, "--no-head-backup")
    assert c["repositories"] == [] and not cm.backup_hosts(c)


def test_rerunning_init_on_an_existing_site_loses_nothing(tmp_path, monkeypatch, fake_llm):
    """A site set up long ago (comments in spindles, a shared repository, custom host keys, sign-in off):
    a --yes re-run keeps every bit of it, and a new node only adds."""
    site = {"version": 1, "assistant": {"enabled": True, "url": f"http://127.0.0.1:{fake_llm}/v1", "model": "tiny-model"},
            "server": {"hostname": "head-01", "address": "192.0.2.10", "ui_port": 20996, "console_auth": False,
                       "repo_root": "/mnt/nas/backups", "ssh": {"user": "ops", "key": "/root/.ssh/id_ed25519"},
                       "tls": {"cert": "/x/s.crt", "key": "/x/s.key", "ca": "/x/ca.crt"}},
            "spindles": {"_comment": "the NAS and a local disk", "nas": {"mount": "/mnt/nas", "kind": "nas"},
                         "hot": {"mount": "/mnt/hot", "kind": "local"}},
            "repositories": [{"id": "head-01", "spindle": "hot"}, {"id": "gpu-class", "spindle": "nas"},
                             {"id": "archive", "spindle": "nas"}],
            "hosts": [{"name": "head-01", "address": "192.0.2.10", "arch": "x86_64", "repo": "head-01", "used_gb": 900},
                      {"name": "gpu-01", "address": "192.0.2.21", "arch": "aarch64", "repo": "gpu-class", "spindle": "nas"},
                      {"name": "gpu-02", "address": "192.0.2.22", "arch": "aarch64", "repo": "gpu-class"}],
            "hf_mirror": {"store": "/mnt/nas/hf-mirror", "port": 20995}, "profiles": {"system": {"paths": ["/"]}}}
    (tmp_path / "site.json").write_text(json.dumps(site))
    monkeypatch.setattr(hostos, "IS_MAC", False)
    from noctyx import firstrun
    monkeypatch.setattr(firstrun, "CONFIG", str(tmp_path / "site.json"))
    monkeypatch.setattr(firstrun.os, "geteuid", lambda: 0)
    monkeypatch.setattr(firstrun, "_ssh_key", lambda c: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(firstrun, "_node_arch", lambda c, addr: "aarch64")
    assert firstrun.main(["--yes", "--node", "gpu-03=192.0.2.23"]) == 0
    c = json.loads((tmp_path / "site.json").read_text())
    assert c["spindles"] == site["spindles"] and c["server"]["repo_root"] == "/mnt/nas/backups"
    assert c["hf_mirror"]["store"] == "/mnt/nas/hf-mirror" and c["server"]["console_auth"] is False
    assert c["hosts"][:3] == site["hosts"]                       # untouched, custom keys and all; no "local" added
    assert c["hosts"][3]["name"] == "gpu-03"
    assert [r["id"] for r in c["repositories"]] == ["head-01", "gpu-class", "archive", "gpu-03"]
    assert c["assistant"]["url"] == site["assistant"]["url"] and c["server"]["ssh"]["user"] == "ops"


def test_macs_are_never_backup_targets():
    c = {"hosts": [{"name": "m", "address": "192.0.2.30", "os": "macos", "repo": "m"},
                   {"name": "g", "address": "192.0.2.21", "repo": "g"}]}
    assert [h["name"] for h in cm.backup_hosts(c)] == ["g"]


def test_a_finished_job_is_not_running(tmp_path):
    import subprocess, sys
    log = tmp_path / "job.log"
    hostos.spawn_detached("u", [sys.executable, "-c", "pass"], str(log), {})
    import time
    for _ in range(50):
        if not hostos.job_running(str(log)):
            break
        time.sleep(0.1)
    assert not hostos.job_running(str(log))            # reaped, not a zombie that kill(0) still finds


def test_console_overview_with_a_monitored_mac(monkeypatch):
    """A Mac head made by noctyx init has no repo: the console's overview must still answer."""
    from noctyx import console
    monkeypatch.setitem(console.CFG, "hosts", [{"name": "mac", "address": "192.0.2.30", "arch": "aarch64",
                                                 "os": "macos", "backup": False, "local": True}])
    monkeypatch.setattr(console, "host_facts", lambda name, age=120: {"os_lines": ["macOS"]})
    monkeypatch.setattr(console, "snapshot_summary", lambda: {"hosts": {}})
    monkeypatch.setattr(console, "schedule_summary", lambda snaps=None: {"rows": []})
    o = console.overview()
    assert o["hosts"] == [{"name": "mac", "repo": None, "arch": "aarch64", "os": "macos", "used_gb": None,
                           "monitored_only": True}]


def test_mac_facts_time_machine_and_boot_time():
    from noctyx import console
    f = console._parse_facts("==timemachine==\ndest timemachine\nlatest 2026-10-20-120000.previous\nrunning 0\n")
    assert f["timemachine"]["dest"] == "timemachine" and f["timemachine"]["running"] == "0"
    assert f["timemachine"]["latest_ts"] > 1.79e9
    assert "s/^{ sec = " in console.FACTS_MAC               # the boot time takes sec, not usec


def test_interval_timers_and_metrics_config():
    units = _units()
    t = plistlib.loads(hostos.launchd_plist("dev.noctyx.textfile", units[f"{cm.UNIT}-textfile.service"],
                                            units[f"{cm.UNIT}-textfile.timer"]).encode())
    assert t["StartInterval"] == 15 and t["RunAtLoad"] and "UserName" not in t      # powermetrics needs root
    d = plistlib.loads(hostos.launchd_plist("dev.noctyx.llm-discover", units[f"{cm.UNIT}-llm-discover.service"],
                                            units[f"{cm.UNIT}-llm-discover.timer"]).encode())
    assert d["StartInterval"] == 300
    y = setup.prometheus_yml(cm.cfg())
    assert y.startswith(setup.PROM_MARK) and f"127.0.0.1:{cm.HEAD_EXPORTER_PORT}" in y and "job_name: llm" in y


def test_component_status_rules():
    from noctyx import components as comp
    restic = next(x for x in comp.COMPONENTS if x["name"] == "restic")
    assert comp.status(restic, "0.19.1", "0.19.1", "0.19.1")[0] == "ok"
    assert comp.status(restic, "0.18.0", "0.19.1", "0.19.1")[0] == "behind"
    assert comp.status(restic, "0.19.1", "0.19.1", "0.20.0")[0] == "upgrade"
    assert comp.status(restic, None, "0.19.1", None)[0] == "missing"
    agent = next(x for x in comp.COMPONENTS if x["name"] == "agent")
    assert comp.status(agent, "abc", None, None, want_agent="abd")[0] == "behind"
    name, url, sha = comp.asset("prometheus", "macos", "aarch64")
    assert name == "prometheus-3.15.0.darwin-arm64.tar.gz" and url.endswith("/v3.15.0/" + name) and len(sha) == 64


def test_inventory_pins_only_what_noctyx_installed():
    from noctyx import components as comp
    c = {"server": {"hostname": "head-01", "address": "192.0.2.10"},
         "hosts": [{"name": "head-01", "address": "192.0.2.10", "local": True, "repo": "head-01"},
                   {"name": "gpu-01", "address": "192.0.2.21", "repo": "gpu-01"},
                   {"name": "mac", "address": "192.0.2.30", "os": "macos", "backup": False}]}
    answers = {"head-01": {"restic": "0.19.1 noctyx", "rest-server": "0.13.0 noctyx", "prometheus": "2.45.3 distro",
                           "node_exporter": "1.7.0 distro"},
               "gpu-01": {"restic": "0.19.1 noctyx", "node_exporter": "1.7.0 distro", "agent": "abc noctyx"}}

    def probe(h, snippet):
        return "".join(f"=={k}==\n{v}\n" for k, v in answers.get(h["name"], {}).items())
    cache = {repo: {"tag": comp.PINS[n]["version"], "ts": 9e12} for n, repo in
             (("restic", "restic/restic"), ("rest-server", "restic/rest-server"), ("prometheus", "prometheus/prometheus"),
              ("node_exporter", "prometheus/node_exporter"))}
    inv = comp.inventory(c, probe, cache, want_agent="abc", version="0.1.0")
    head = {r["component"]: r for r in inv["hosts"]["head-01"]}
    assert head["rest-server"]["state"] == "behind" and head["rest-server"]["upgrade"] == "sudo noctyx setup tools --force"
    assert head["prometheus"]["pinned"] is None and head["prometheus"]["state"] == "upgrade"
    assert head["prometheus"]["upgrade"] == "sudo apt-get install --only-upgrade prometheus"
    gpu = {r["component"]: r for r in inv["hosts"]["gpu-01"]}
    assert gpu["agent"]["state"] == "ok" and gpu["restic"]["state"] == "ok"
    assert gpu["node_exporter"]["upgrade"].endswith("(on gpu-01)")
    assert inv["hosts"]["mac"] == []                              # a Mac runs nothing Noctyx installs


def test_hygiene_refuses_a_mac(monkeypatch):
    import types
    c = {"hosts": [{"name": "mac", "address": "192.0.2.30", "os": "macos"}], "server": {}}
    monkeypatch.setattr(cm, "cfg", lambda: c)
    with pytest.raises(SystemExit) as e:
        cm.cmd_hygiene(types.SimpleNamespace(host="mac", fix=None))
    assert "Mac" in str(e.value)
