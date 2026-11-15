"""The restore rehearsal: which hosts are due, what the head records from each node's result, what the nightly
run asks for, and the node's comparison of restored files with the live ones."""
import argparse
import json
import os
import re
import subprocess
import sys
import time

import pytest

from noctyx import orchestrator as o

C = {"server": {"hostname": "head", "address": "192.0.2.10", "ssh": {"user": "ops", "key": "/k"}},
     "hosts": [{"name": "gpu-01", "address": "192.0.2.21", "arch": "x86_64", "repo": "gpu-01"},
               {"name": "gpu-02", "address": "192.0.2.22", "arch": "x86_64", "repo": "gpu-02"},
               {"name": "mac-01", "address": "192.0.2.30", "os": "macos"}]}


@pytest.fixture()
def site(tmp_path, monkeypatch):
    f = tmp_path / "site.json"
    f.write_text(json.dumps(C))
    monkeypatch.setattr(o, "CFG_PATH", str(f))
    monkeypatch.setattr(o, "STATE_DIR", tmp_path)
    monkeypatch.setattr(o, "REHEARSAL_FILE", tmp_path / "rehearsal.json")
    monkeypatch.setattr(o, "AUDIT", tmp_path / "audit.jsonl")
    nodes = {"gpu-01": {"rc": 0, "snapshot": "abcd1234", "snapshot_time": "2026-01-02T03:04:05", "same": 4,
                        "changed_since": 0, "missing": [], "differs": [], "seconds": 3},
             "gpu-02": {"rc": 0, "snapshot": "ef567890", "snapshot_time": "2026-01-02T03:04:05", "same": 3,
                        "changed_since": 1, "missing": [], "differs": [], "seconds": 4}}
    asked = []

    def ssh(c, h, cmd, timeout=0):
        asked.append((h["name"], cmd))
        r = nodes[h["name"]]
        if r is None:
            return argparse.Namespace(stdout="", stderr="ssh: connect to host: timed out", returncode=255)
        return argparse.Namespace(stdout=o.P["result_token"] + " " + json.dumps({"event": "rehearsal", **r}) + "\n",
                                  stderr="", returncode=r["rc"])
    monkeypatch.setattr(o, "ssh", ssh)
    return nodes, asked


def rehearse(**kw):
    a = argparse.Namespace(host=None, due=False)
    a.__dict__.update(kw)
    try:
        o.cmd_rehearse(a)
    except SystemExit as e:
        return e.code
    return 0


def test_every_backed_up_host_rehearses_and_the_result_is_kept(site, capsys):
    nodes, asked = site
    assert rehearse() == 0
    assert [h for h, _ in asked] == ["gpu-01", "gpu-02"] and all(c.endswith(" rehearse") for _, c in asked)   # never the Mac
    st = o.rehearsals()["hosts"]
    assert st["gpu-01"]["ok"] and st["gpu-01"]["snapshot"] == "abcd1234" and st["gpu-02"]["changed_since"] == 1
    out = capsys.readouterr().out
    assert "4 file(s) restored identical" in out and "1 changed since" in out and "2/2 restored and matched" in out
    assert [json.loads(l)["event"] for l in o.AUDIT.read_text().splitlines()] == ["rehearsal", "rehearsal"]


def test_a_failed_rehearsal_says_why_and_fails_the_run(site, capsys):
    nodes, asked = site
    nodes["gpu-01"] = {"rc": 1, "snapshot": "abcd1234", "same": 2, "changed_since": 0, "missing": [], "differs": ["/etc/fstab"]}
    nodes["gpu-02"] = None
    assert rehearse() == 1
    out = capsys.readouterr().out
    assert "differs from the live file: /etc/fstab" in out and "timed out" in out and "0/2" in out
    st = o.rehearsals()["hosts"]
    assert st["gpu-01"]["ok"] is False and st["gpu-02"]["ok"] is False and "timed out" in st["gpu-02"]["error"]
    with pytest.raises(SystemExit, match="monitored only"):
        o.cmd_rehearse(argparse.Namespace(host="mac-01", due=False))


def test_due_follows_the_sites_interval(site):
    nodes, asked = site
    c = o.cfg()
    assert o.rehearsal_every(c) == 30 * 86400 and o.rehearsal_due(c) == ["gpu-01", "gpu-02"]      # never rehearsed
    assert rehearse(due=True) == 0 and len(asked) == 2
    assert o.rehearsal_due(c) == [] and rehearse(due=True) == 0 and len(asked) == 2               # none due: none asked
    st = o.rehearsals()
    st["hosts"]["gpu-02"]["ts"] -= 31 * 86400
    o.REHEARSAL_FILE.write_text(json.dumps(st))
    assert o.rehearsal_due(c) == ["gpu-02"]
    assert rehearse(due=True) == 0 and [h for h, _ in asked][2:] == ["gpu-02"]
    weekly = {**c, "verification": {"restore_rehearsal": "weekly"}}
    assert o.rehearsal_every(weekly) == 7 * 86400 and o.rehearsal_due(weekly, now=time.time() + 8 * 86400) == ["gpu-01", "gpu-02"]
    off = {**c, "verification": {"restore_rehearsal": "off"}}
    assert o.rehearsal_every(off) is None and o.rehearsal_due(off) == []


def test_the_nightly_run_rehearses_what_is_due(monkeypatch):
    ran = []
    monkeypatch.setattr(o.subprocess, "run", lambda argv, **kw: ran.append(argv[2:]) or argparse.Namespace(returncode=0))
    with pytest.raises(SystemExit) as e:
        o.cmd_nightly(argparse.Namespace())
    assert e.value.code == 0 and ["rehearse", "--due"] in ran


def test_the_node_compares_restored_files_with_the_live_ones(tmp_path):
    src = (o.NX_HOME / "agent" / "noctyx-agent.sh").read_text()
    body = src[src.index("cmd_rehearse() {"):]
    py = re.search(r"<<'PY' > \"\$STATE/rehearsal.json\"\n(.*?)\nPY\n", body, re.S).group(1)
    live, restored = tmp_path / "live", tmp_path / "restored"
    snap = time.time() - 3600

    def put(name, live_text, restored_text, live_age):
        f = live / name
        if live_text is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(live_text)
            os.utime(f, (time.time() - live_age,) * 2)
        if restored_text is not None:
            r = restored / str(f).lstrip("/")
            r.parent.mkdir(parents=True, exist_ok=True)
            r.write_text(restored_text)
        return str(f)
    files = [put("same", "a", "a", 7200), put("edited-since", "new", "old", 60), put("corrupt", "good", "bad", 7200),
             put("not-in-snapshot", "x", None, 7200)]
    stime = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(snap)) + ".123456789Z"

    def run(rc, fs):
        r = subprocess.run([sys.executable, "-", str(restored), stime, str(rc), "abcd1234" * 8, "gpu-01", "repo", "3", *fs],
                           input=py, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)
    d = run(0, files)
    assert (d["same"], d["changed_since"], d["restored"]) == (1, 1, 3)
    assert d["differs"] == [files[2]] and d["missing"] == [files[3]] and d["rc"] == 1 and d["snapshot"] == "abcd1234"
    ok = run(0, [files[0], files[1], files[3]])
    assert ok["rc"] == 0 and ok["differs"] == []                       # a file changed since, one not in it: fine
    assert run(0, [files[3]])["rc"] == 1                                # nothing restored at all is a failure
    assert run(2, [files[0]])["rc"] == 2                                # restic's own failure is kept
