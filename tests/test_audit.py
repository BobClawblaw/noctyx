"""The 2026-10-24 audit's findings, each held to its fix: the assistant key stays with its own endpoint, the HF
mirror never reads a path a request names, the agent's error line is JSON, the node's CA path is the node's,
a reloaded config's hosts count, the ownership sweep's find runs, accounts change one at a time, the
discovery reply is current, the restore engine names the ESP it found, and the config writer finds only a
top-level key."""
import json
import os
import pathlib
import subprocess
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "noctyx"


# NX-01
def test_the_assistant_key_goes_to_its_own_endpoint_only(monkeypatch):
    from noctyx import console
    sent = []

    class R:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return self.body

    def urlopen(req, timeout=0):
        sent.append((req.full_url, req.headers.get("Authorization")))
        return R(b'{"data": [{"id": "m"}], "choices": [{"message": {"content": "ok"}}]}')
    monkeypatch.setattr(console.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(console, "_api_key", lambda h: "sekrit" if h == "assistant" else None)
    monkeypatch.setattr(console.CM, "cfg", lambda: {"assistant": {"url": "http://192.0.2.5:8000/v1", "key_host": "assistant"}})
    console.assistant_test("http://192.0.2.66:9000/v1", "m")
    assert sent and all(auth is None for _, auth in sent)               # another url: no key
    sent.clear()
    console.assistant_test("http://192.0.2.5:8000/v1/", "m")
    assert sent and all(auth == "Bearer sekrit" for _, auth in sent)    # its own: the key


# NX-02
def test_the_mirror_never_reads_a_path_a_request_names(tmp_path):
    from noctyx.cache import mirror
    rd = tmp_path / "models--org--x"
    (rd / "refs").mkdir(parents=True)
    (rd / "snapshots" / ("a" * 40)).mkdir(parents=True)
    (rd / "refs" / "main").write_text("a" * 40 + "\n")
    (tmp_path / "secret").write_text("root:x:0:0\n")
    (rd / "refs" / "notasha").write_text("hello\n")
    assert mirror.local_ref(str(rd), "main") == "a" * 40
    assert mirror.local_ref(str(rd), "../../secret") is None             # never opened
    assert mirror.local_ref(str(rd), "notasha") is None                  # a ref that is not a sha is no commit
    assert mirror.local_ref(str(rd), "refs/pr/1") is None                # a PR ref: upstream's to answer
    for bad in ("..", ".", "../..", "a/b", "a\\b", "x\0y"):
        assert not mirror.safe_part(bad)
    assert mirror.safe_part(".gitattributes") and mirror.safe_part("model-00001.safetensors")


# NX-03
def test_the_mac_collector_names_every_metric_with_the_site_prefix():
    text = (ROOT / "metrics" / "mac-textfile.sh").read_text()
    assert "cm_" not in text and "@METRIC_PREFIX@_memory_used_bytes %d" in text


# NX-04
def test_the_probe_uses_the_ca_where_that_machine_has_it(monkeypatch):
    from noctyx import orchestrator as o
    c = {"server": {"address": "192.0.2.10"}}
    assert o.client_ca(c, {"name": "gpu-01", "address": "192.0.2.11"}) == f"{o.P['node_etc']}/tls/ca.crt"
    assert o.client_ca(c, {"name": "head", "address": "192.0.2.10"}) == f"{o.P['tls']}/ca.crt"


# NX-05
def test_a_reloaded_config_brings_its_hosts_and_profiles(tmp_path, monkeypatch):
    from noctyx import console
    f = tmp_path / "site.json"
    f.write_text(json.dumps({"server": {"tls": {}}, "hosts": [{"name": "new-01"}, {"name": "off", "enabled": False}],
                             "profiles": {"system": {}, "extra": {}}}))
    monkeypatch.setattr(console, "CFG_PATH", str(f))
    monkeypatch.setattr(console, "_CFG_MTIME", 0)
    for k in ("CFG", "S", "VALID_HOSTS", "VALID_PROFILES"):
        monkeypatch.setattr(console, k, getattr(console, k))
    console._reload_cfg()
    assert console.VALID_HOSTS == {"new-01"} and console.VALID_PROFILES == {"system", "extra"}


# NX-06
def test_repo_sizes_use_a_du_every_head_has(tmp_path):
    (tmp_path / "f").write_bytes(b"x" * 10000)
    o = subprocess.run(["du", "-sk", str(tmp_path)], capture_output=True, text=True)
    assert o.returncode == 0 and int(o.stdout.split()[0]) * 1024 >= 10000
    assert '["du", "-sb", d]' not in (ROOT / "console.py").read_text()


def _bash_fn(path, name):
    for line in pathlib.Path(path).read_text().splitlines():
        if line.startswith(f"{name}()"):
            return line
    raise AssertionError(name)


# NX-07
@pytest.mark.parametrize("msg", ['plain', 'has "quotes"', "back\\slash", "two\nlines", 'C:\\x "y"\tz'])
def test_the_agents_error_line_is_json_whatever_the_message(msg):
    fn = _bash_fn(ROOT / "agent" / "noctyx-agent.sh", "json_str")
    out = subprocess.run(["bash", "-c", fn + '\nprintf \'{"message":"%s"}\' "$(json_str "$1")"', "_", msg],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out)["message"] == msg.replace("\t", " ")


# NX-08
def test_the_ownership_sweep_hands_find_a_bare_placeholder(monkeypatch):
    from noctyx import orchestrator as o
    seen = []
    monkeypatch.setattr(o.subprocess, "run", lambda argv, **k: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "0", ""))
    monkeypatch.setattr(o, "repo_root", lambda c: "/mnt/nas backups/noctyx")
    o._sweep_root_owned_repo_files({})
    chown = next(a[-1] for a in seen if "chown" in a[-1])
    assert " {} + " in chown and "{{}}" not in chown and "'/mnt/nas backups/noctyx'" in chown


def test_install_agent_refuses_values_a_nodes_shell_would_split():
    text = (ROOT / "agent" / "install-agent.sh").read_text()
    assert "open('$CFG')" not in text and '"$CFG")' in text
    assert "[[ $PROFILE =~ ^[A-Za-z0-9_-]+$ ]]" in text


# NX-10
@pytest.mark.parametrize("esp, n", [("/dev/sda1", "1"), ("/dev/nvme0n1p2", "2"), ("/dev/mmcblk0p12", "12")])
def test_the_restore_engine_names_the_esp_it_found(esp, n):
    out = subprocess.run(["bash", "-c", 'ESPP=$1; ESPN=${ESPP##*[!0-9]}; echo "$ESPN"', "_", esp],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == n
    assert '-p "$ESPN"' in (ROOT / "restore" / "noctyx-restore.sh").read_text()


def test_the_payload_builder_takes_three_arguments_and_no_bare_glob():
    text = (ROOT / "restore" / "make-restore-usb.sh").read_text()
    assert "shopt -s nullglob" in text and 'put 600 "$k" "keys/$(basename "$k")"' in text


# NX-11
def test_the_discovery_reply_is_built_for_each_probe(monkeypatch):
    from noctyx import directory
    inv = {"hosts": [{"name": "gpu-01"}]}
    monkeypatch.setattr(directory, "load", lambda name, default=None: inv)
    assert json.loads(directory.discovery_reply())["hosts"] == ["gpu-01"]
    inv["hosts"].append({"name": "gpu-02"})
    assert json.loads(directory.discovery_reply())["hosts"] == ["gpu-01", "gpu-02"]


# NX-12
def test_accounts_change_one_at_a_time(tmp_path, monkeypatch):
    from noctyx import accounts
    monkeypatch.setattr(accounts, "_users_file", lambda: tmp_path / "users.json")
    monkeypatch.setattr(accounts, "hash_password", lambda pw: "h")
    names = [f"user{i:02d}" for i in range(16)]
    ts = [threading.Thread(target=accounts.add, args=(n, "viewer", "long-enough-password")) for n in names]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(accounts.users()) == names                            # none lost to another's write
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".users.json.")]   # no temp left
    assert oct(os.stat(tmp_path / "users.json").st_mode)[-3:] == "600"


def test_restic_reads_are_bounded(monkeypatch):
    from noctyx import console
    monkeypatch.setattr(console, "_host_repo", lambda h: ("r", "/tmp/r"))
    monkeypatch.setattr(console, "RESTIC_WAIT", 0.05)
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(console, "RESTIC_READS", sem)
    sem.acquire()                                                        # every slot taken
    r = console._restic_ro("gpu-01", ["snapshots"])
    assert r.returncode == 75 and "try again" in r.stderr


# NX-13
def test_the_config_writer_finds_only_a_top_level_key():
    from noctyx import orchestrator as o
    t = ('{\n  "_c": "a \\"schedule\\": { in a comment",\n  "hosts": {"x": {"schedule": {"every": 1}}},\n'
         '  "schedule":{"a": 1},\n  "heal" : {}\n}')
    s = o._top_key(t, "schedule")
    assert t[s[0]:s[1] + 1] == '"schedule":{'
    assert t[o._top_key(t, "heal")[1]] == "{"
    assert o._top_key(t, "_c") is None and o._top_key(t, "missing") is None


def test_the_demo_refuses_a_missing_port(capsys):
    from noctyx import cli
    assert cli.main(["demo", "--port"]) == 2
    assert "a port number" in capsys.readouterr().err
