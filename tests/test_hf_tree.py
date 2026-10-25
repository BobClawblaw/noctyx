"""The model mirror's tree: sizes per repo and revision, the weight files a revision lacks (an index's weight map,
1-based model shards, 0-based dataset shards, a link whose blob is gone), and the harvest's record of the
hosts holding each repo, with its refs file moved to the Noctyx name."""
import json
import os
import subprocess
import types

from noctyx.cache import tree


def _repo(hub, d, files, refs=None, blobs_missing=()):
    """A hub-cache repo: files {commit: {path: bytes-or-None}}; None makes a link whose blob is not there."""
    rd = hub / d
    (rd / "blobs").mkdir(parents=True, exist_ok=True)
    for commit, fs in files.items():
        for rel, data in fs.items():
            blob = f"b{abs(hash((commit, rel))):x}"
            if data is not None:
                (rd / "blobs" / blob).write_bytes(data)
            p = rd / "snapshots" / commit / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.path.relpath(rd / "blobs" / blob, p.parent), p)
    for name, commit in (refs or {}).items():
        (rd / "refs").mkdir(exist_ok=True)
        (rd / "refs" / name).write_text(commit)
    return rd


C1 = "1" * 40


def test_sizes_and_the_weight_files_a_revision_lacks(tmp_path):
    hub = tmp_path / "hub"
    idx = json.dumps({"weight_map": {"a": "model-00001-of-00003.safetensors", "b": "model-00002-of-00003.safetensors",
                                     "c": "model-00003-of-00003.safetensors"}}).encode()
    _repo(hub, "models--org--full", {C1: {"config.json": b"{}", "model-00001-of-00002.safetensors": b"x" * 100,
                                          "model-00002-of-00002.safetensors": b"y" * 50}}, refs={"main": C1})
    _repo(hub, "models--org--half", {C1: {"model.safetensors.index.json": idx, "model-00001-of-00003.safetensors": b"x" * 10,
                                          "model-00003-of-00003.safetensors": None}})
    _repo(hub, "datasets--org--set", {C1: {"data/test-00000-of-00002.parquet": b"p", "data/test-00001-of-00002.parquet": b"q"}})
    _repo(hub, "models--gpt2", {C1: {"config.json": b"{}"}})
    v = tree.scan(tmp_path)
    r = {x["id"]: x for x in v["repos"]}
    full = r["org/full"]
    assert full["state"] == "complete" and full["bytes"] == 152 and full["refs"] == {"main": C1}
    assert full["snapshots"][0]["refs"] == ["main"] and full["snapshots"][0]["expected"] == 2
    half = r["org/half"]["snapshots"][0]
    assert r["org/half"]["state"] == "incomplete"
    assert half["missing"] == ["model-00002-of-00003.safetensors", "model-00003-of-00003.safetensors"]   # unlinked; blob gone
    assert r["org/set"]["kind"] == "datasets" and r["org/set"]["state"] == "complete"              # 0-based numbering
    assert r["gpt2"]["owner"] == "" and r["gpt2"]["name"] == "gpt2"
    assert v["bytes"] == sum(x["bytes"] for x in v["repos"])


def test_the_cache_is_dropped_after_a_harvest(tmp_path):
    hub = tmp_path / "hub"
    _repo(hub, "models--org--a", {C1: {"config.json": b"{}"}})
    cf = tmp_path / "cache.json"
    first = tree.cached(tmp_path, cf)
    assert tree.cached(tmp_path, cf)["ts"] == first["ts"]
    (tmp_path / "status.json").write_text("{}")                          # a harvest ran since
    os.utime(tmp_path / "status.json", (first["ts"] + 1, first["ts"] + 1))
    assert tree.cached(tmp_path, cf)["ts"] != first["ts"]


def test_the_harvest_records_hosts_and_moves_the_refs_file(tmp_path, monkeypatch):
    from noctyx import orchestrator as o
    hub = tmp_path / "hub"
    rd = _repo(hub, "models--org--a", {C1: {"config.json": b"{}"}})
    blob = next((rd / "blobs").iterdir()).name
    (rd / ".cm-refs.json").write_text(json.dumps({"main": {"commit": C1, "mtime": 5, "host": "gpu-01"}}))
    listing = [{"root": "/root/.cache/huggingface/hub", "repo": "models--org--a", "refs": {"main": [C1, 1]},
                "files": {C1: {"config.json": blob}}, "blobs": {blob: 2}}]
    monkeypatch.setattr(o, "_run_on", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(listing), ""))
    c = {"server": {"ssh": {"user": "ops", "key": "/k"}, "address": "192.0.2.10"}, "hosts": []}
    o.hf_harvest_host(c, {"name": "gpu-02", "address": "192.0.2.22"}, types.SimpleNamespace(repo=None), hub)
    assert not (rd / ".cm-refs.json").exists()
    assert json.loads((rd / ".nx-refs.json").read_text())["main"]["host"] == "gpu-01"     # older mtime: kept
    assert set(json.loads((rd / ".nx-seen.json").read_text())) == {"gpu-02"}
    r = tree.scan(tmp_path)["repos"][0]
    assert r["from"] == ["gpu-01"] and list(r["seen_on"]) == ["gpu-02"]
