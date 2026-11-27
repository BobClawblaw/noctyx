"""Unit tests closing coverage gaps on noctyx-dumps, mtplx_exporter, llm_discover, dual_gpt, discover, and schedule."""
import binascii
import importlib.util
import pathlib
import sqlite3
import struct
import subprocess
import sys

from noctyx import discover, schedule
from noctyx.metrics import llm_discover, mtplx_exporter

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "noctyx"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_noctyx_dumps_sqlite_discovery_and_online_backup(tmp_path, monkeypatch):
    dumps = _load_module("noctyx_dumps_mod", SRC / "agent" / "noctyx-dumps.py")
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (k TEXT, v INT)")
    con.execute("INSERT INTO t VALUES ('hello', 42)")
    con.commit()
    con.close()

    out_dir = tmp_path / "dumps"
    monkeypatch.setattr(dumps, "DIR", str(out_dir))
    monkeypatch.setattr(dumps, "STATUS", str(out_dir / "status.json"))
    monkeypatch.setattr(dumps, "KNOWN_SQLITE", [])
    monkeypatch.setattr(dumps, "sh", lambda argv, timeout=30: subprocess.CompletedProcess(argv, 1, "", ""))

    c = {"enabled": True, "skip": set(), "sqlite": [str(db)], "profiles": ["system"]}
    found = dumps.discover(c)
    assert len(found) == 1 and found[0]["kind"] == "sqlite" and found[0]["enabled"] is True

    st = dumps.run(c)
    assert len(st["results"]) == 1 and st["results"][0]["ok"] is True
    backed = sqlite3.connect(st["results"][0]["file"])
    assert backed.execute("SELECT k, v FROM t").fetchall() == [("hello", 42)]
    backed.close()
    assert dumps.fname({"id": "docker:pg_main", "kind": "postgres"}) == "docker-pg_main.sql"
    assert dumps.command({"id": "docker:pg_main", "kind": "postgres"})[0:3] == ["docker", "exec", "pg_main"]


def test_mtplx_exporter_hist_and_host_state():
    h = mtplx_exporter.Hist([0.1, 1.0])
    h.observe(0.05)
    h.observe(0.5, n=2)
    lines = "\n".join(h.lines("m", 'model="x"'))
    assert 'le="0.1"} 1' in lines and 'le="1.0"} 3' in lines and 'le="+Inf"} 3' in lines

    st = mtplx_exporter.HostState("mac-01", "http://127.0.0.1:8888", None)
    polls = [
        {"recent": [{"request_id": "r0", "prompt_tokens": 100, "completion_tokens": 50}]},
        {"recent": [{"request_id": "r0", "prompt_tokens": 100, "completion_tokens": 50},
                    {"request_id": "r1", "served_model_id": "Qwen-MLX", "prompt_tokens": 200, "completion_tokens": 80,
                     "cached_tokens": 50, "ttft_s": 0.2, "request_elapsed_s": 1.2, "decode_elapsed_s": 1.0,
                     "decode_tok_s": 80.0}]},
    ]
    idx = {"i": 0}

    def fake_get(path):
        if path == "/health":
            return {"model": "Qwen-MLX", "startup": {"started_at": 1700000000}}
        res = polls[min(idx["i"], len(polls) - 1)]
        idx["i"] += 1
        return res

    st.get = fake_get
    st.poll()  # First poll seeds r0 without counting history
    assert st.c["requests"] == 0 and st.seeded is True
    st.poll()  # Second poll counts only new r1
    assert st.c["requests"] == 1 and st.c["prompt_tokens"] == 200 and st.c["generation_tokens"] == 80
    out = st.render()
    assert 'up_mtplx{host="mac-01"} 1' in out
    assert 'vllm:generation_tokens_total{model_name="Qwen-MLX",engine="0"} 80' in out


def test_llm_discover_parse_dcmd():
    line = '/tf-worker ["tf-dsv41-lanes","/ckpt","/cache","1","2","192.0.2.1","29500","kit"] null'
    res = llm_discover.parse_dcmd(line)
    assert res is not None and res["engine"] == "tensorfold"
    assert llm_discover.parse_dcmd("bad") is None


def test_dual_gpt_writes_valid_protective_mbr_and_gpt(tmp_path):
    img = tmp_path / "stick.img"
    # 128 MiB sparse file
    with open(img, "wb") as f:
        f.truncate(128 * 1024 * 1024)
    script = (SRC / "restore" / "dual_gpt.py").read_text().replace("@LIVE_LABEL@", "NOCTYXLIVE").replace("@STICK_LABEL@", "NOCTYX")
    sp = tmp_path / "dual_gpt.py"
    sp.write_text(script)
    r = subprocess.run([sys.executable, str(sp), str(img), "16384", "16"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    data = img.read_bytes()
    # Protective MBR
    assert data[510:512] == b"\x55\xAA" and data[446 + 4] == 0xEE
    # Primary GPT header at LBA 1
    hdr = bytearray(data[512:512 + 92])
    assert hdr[0:8] == b"EFI PART"
    stored_crc = struct.unpack_from("<I", hdr, 16)[0]
    hdr[16:20] = b"\x00\x00\x00\x00"
    assert (binascii.crc32(hdr) & 0xFFFFFFFF) == stored_crc
    # Backup GPT header at last LBA
    assert data[-512:-512 + 8] == b"EFI PART"
    # Partition labels present in primary array
    arr = data[1024:1024 + 128 * 4]
    assert "NOCTYXLIVE".encode("utf-16-le") in arr
    assert "NOCTYX".encode("utf-16-le") in arr
    assert "CIDATA".encode("utf-16-le") in arr


def test_discover_hw_helpers_and_schedule_parsing():
    assert discover.hw_key("2x-DGX-Spark-Qwen") == "gb10"
    assert discover.hw_key("apache-spark-sql") is None
    assert discover.hw_key("RTX-5090-vLLM") == "rtx5090"
    assert discover._hw_class("dual-dgx-sparks-llama") == ("gb10", 2)
    assert discover.discover_rank({"speed": {"tps": 100}, "pushed_at": "2026-01-01"}, "fastest") > \
           discover.discover_rank({"claimed_tps": 999, "pushed_at": "2026-11-01"}, "fastest")

    assert schedule.parse_duration("6h") == 6 * 3600
    assert schedule.parse_duration("1d") == 86400
    assert schedule.parse_duration("15m") == 900
