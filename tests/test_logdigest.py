"""The log digest: a node's lines are counted by kind, a kind no earlier night had is new, the first night only
records, old kinds are forgotten, and the brief gets numbers while the text stays with operators."""
import json
import subprocess
import time

import pytest

from noctyx import llm, logdigest

C = {"server": {"hostname": "head", "address": "192.0.2.10"},
     "hosts": [{"name": "gpu-01", "address": "192.0.2.21", "arch": "x86_64"},
               {"name": "gpu-02", "address": "192.0.2.22", "arch": "x86_64"},
               {"name": "mac-01", "address": "192.0.2.30", "os": "macos"}]}


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(logdigest, "KINDS", tmp_path / "log-kinds.json")
    monkeypatch.setattr(logdigest, "DIGEST", tmp_path / "log-digest.json")
    monkeypatch.setattr(llm, "configured", lambda config=None: {"enabled": True, "url": "http://192.0.2.9/v1"})
    monkeypatch.delenv("NOCTYX_NO_ASSIST", raising=False)


def test_parse_reads_counts_and_ignores_an_empty_journal():
    assert logdigest.parse("   12 kernel: nvme N: timeout\n      1 sshd: error: kex\n") == {"kernel: nvme N: timeout": 12, "sshd: error: kex": 1}
    assert logdigest.parse("      1 -- No entries --\n") == {} and logdigest.parse("") == {}


def test_the_first_night_records_and_later_nights_report_only_what_is_new():
    seen, t = {}, 1_000_000.0
    assert logdigest.fold(seen, "gpu-01", {"a": 5, "b": 1}, t) == []                 # nothing to compare with
    assert logdigest.fold(seen, "gpu-01", {"a": 9, "c": 2, "d": 40}, t + 86400) == [("d", 40), ("c", 2)]
    assert logdigest.fold(seen, "gpu-01", {"c": 1}, t + 2 * 86400) == []             # seen last night: not new
    assert logdigest.fold(seen, "gpu-02", {"a": 1}, t + 2 * 86400) == []             # another host's first night
    logdigest.fold(seen, "gpu-01", {"c": 1}, t + (logdigest.KEEP_DAYS + 1.5) * 86400)
    assert set(seen["gpu-01"]) == {"c"}                                              # the rest was forgotten
    assert logdigest.fold(seen, "gpu-01", {"a": 3, "c": 1}, t + 20 * 86400) == [("a", 3)]   # and is new again


def test_a_run_asks_about_the_new_kinds_and_keeps_the_digest():
    night = {"gpu-01": "  30 kernel: nvme N: I/O timeout\n", "gpu-02": "   2 sshd: error: kex\n"}
    asked = []
    ask = lambda a, system, user, max_tokens=0: (asked.append(user) or "gpu-01's NVMe timeouts deserve a look.", "model-a")
    read = lambda c, h: night[h["name"]]
    d = logdigest.run(C, read=read, ask=ask, now=1_000_000.0)
    assert set(d["hosts"]) == {"gpu-01", "gpu-02"} and d["hosts"]["gpu-01"]["first_night"] and asked == []   # never the Mac
    night["gpu-01"] = "  30 kernel: nvme N: I/O timeout\n   7 engine: CUDA error: token=abcdef0123456789abcdef out of memory\n"
    d = logdigest.run(C, read=read, ask=ask, now=1_086_400.0)
    g = d["hosts"]["gpu-01"]
    assert g["new_total"] == 1 and g["new"][0]["count"] == 7 and "CUDA error" in g["new"][0]["kind"]
    assert "abcdef0123456789" not in json.dumps(d) and "abcdef0123456789" not in asked[0]      # redacted before anyone reads it
    assert d["summary"].startswith("gpu-01's NVMe") and d["model"] == "model-a" and d["hosts"]["gpu-02"]["new_total"] == 0
    assert logdigest.counts() == {"gpu-01": 1}                                                # what the brief is told
    assert "7x  engine: CUDA error" in logdigest.text(d) and "none new" in logdigest.text(d)
    assert oct(logdigest.DIGEST.stat().st_mode & 0o777) == "0o600"


def test_one_host_looks_and_keeps_nothing_and_a_dead_node_does_not_stop_the_rest():
    def read(c, h):
        if h["name"] == "gpu-02":
            raise RuntimeError("ssh: connect timed out")
        return "   1 a\n"
    d = logdigest.run(C, only="gpu-01", read=read)
    assert set(d["hosts"]) == {"gpu-01"} and not logdigest.KINDS.exists() and not logdigest.DIGEST.exists()
    d = logdigest.run(C, read=read)
    assert d["hosts"]["gpu-02"] == {"error": "ssh: connect timed out"} and d["hosts"]["gpu-01"]["lines"] == 1
    assert "could not read its journal" in logdigest.text(d)


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="the count runs on the nodes, which are Linux (GNU sed)")
def test_the_nodes_own_count_takes_out_what_varies(tmp_path):
    lines = ["2026-01-02T03:04:05+0000 gpu-01 kernel[123]: nvme 0000:01:00.0: I/O 17 QID 4 timeout",
             "2026-01-02T03:04:06+0000 gpu-01 kernel[123]: nvme 0000:01:00.0: I/O 99 QID 2 timeout",
             "2026-01-02T03:04:07+0000 gpu-01 resolved[9]: veth1a2b3c4: Failed to determine",
             "2026-01-02T03:04:08+0000 gpu-01 resolved[9]: vethdeadbee: Failed to determine",
             "2026-01-02T03:04:09+0000 gpu-01 dockerd[5]: container 3f2a9c1b7d4e5f60718293a4b5c6d7e8f9a0b1c2 died",
             "2026-01-02T03:04:10+0000 gpu-01 app[7]: request 123e4567-e89b-12d3-a456-426614174000 failed at 0xdeadBEEF"]
    pipeline = logdigest.SNIPPET.split("\n", 1)[1].lstrip(" |")           # everything after journalctl
    r = subprocess.run(["sh", "-c", pipeline], input="\n".join(lines) + "\n", capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    kinds = logdigest.parse(r.stdout)
    assert kinds == {"kernel: nvme N:N:N.N: I/O N QID N timeout": 2, "resolved: vethX: Failed to determine": 2,
                     "dockerd: container H died": 1, "app: request ID failed at H": 1}
    assert subprocess.run(["sh", "-n", "-c", logdigest.SNIPPET]).returncode == 0
