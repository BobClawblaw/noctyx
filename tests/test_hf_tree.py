"""The model mirror's tree: sizes per repo and revision, the weight files a revision lacks (an index's weight map,
1-based model shards, 0-based dataset shards, a link whose blob is gone), and the harvest's record of the
hosts holding each repo, with its refs file moved to the Noctyx name, and --local-dir downloads harvested under
the repo their commit belongs to."""
import json
import os
import pathlib
import subprocess
import time
import types
import urllib.error

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


def test_the_lister_follows_a_shared_blob_store_but_not_out_of_the_cache(tmp_path):
    lister = _lister()
    hub = tmp_path / "hub"
    rd = _repo(hub, "models--org--a", {C1: {"config.json": b"{}"}})
    shared = hub / "blobs" / "ab" / ("ab" + "0" * 62)                 # huggingface_hub's shared-blob store
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"w" * 7)
    outside = tmp_path / "secret"
    outside.write_bytes(b"s" * 3)
    for blob, tgt in (("e" * 64, shared), ("f" * 64, outside)):
        os.symlink(os.path.relpath(tgt, rd / "blobs"), rd / "blobs" / blob)
        os.symlink(f"../../blobs/{blob}", rd / "snapshots" / C1 / f"{blob[0]}.safetensors")
    d = lister.describe(str(hub), "models--org--a")
    assert d["blobs"]["e" * 64] == 7 and "f" * 64 not in d["blobs"]
    assert set(d["files"][C1]) == {"config.json", "e.safetensors"}


def _lister():
    import importlib.util
    spec = importlib.util.spec_from_file_location("lister", pathlib.Path(tree.__file__).with_name("lister.py"))
    lister = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lister)
    return lister


def _local_dir(d, files, commit=C1):
    """A --local-dir download: files {path: bytes}, each with huggingface_hub's metadata (commit, etag, time),
    the etag being the git blob id the Hub gives a small file."""
    import hashlib
    md = d / ".cache" / "huggingface" / "download"
    for rel, data in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(data)
        etag = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        (md / rel).parent.mkdir(parents=True, exist_ok=True)
        (md / f"{rel}.metadata").write_text(f"{commit}\n{etag}\n{os.path.getmtime(d / rel) + 0.5}\n")
    return d


def test_the_lister_finds_local_dir_downloads_and_leaves_out_changed_files(tmp_path):
    lister = _lister()
    d = _local_dir(tmp_path / "models" / "a", {"config.json": b"{}", "sub/w.safetensors": b"w" * 9,
                                                "edited.json": b"1", "odd.json": b"2"})
    os.utime(d / "edited.json", (time.time() + 60, time.time() + 60))        # changed after the download
    md = d / ".cache" / "huggingface" / "download"
    (md / "odd.json.metadata").write_text(f"{C1}\n\"not-a-hash\"\n{time.time() + 60}\n")
    (md / "gone.json.metadata").write_text(f"{C1}\n{'0' * 40}\n1\n")           # file deleted since
    hubs, local = lister.roots([str(tmp_path)])
    assert hubs == [] and local == [str(d)]
    x = lister.describe_local(str(d))
    assert x["local"] and x["name"] == "a" and set(x["files"][C1]) == {"config.json", "sub/w.safetensors"}
    assert {x["paths"][b]: n for b, n in x["blobs"].items()} == {"config.json": 2, "sub/w.safetensors": 9}


def test_the_harvest_stores_a_local_dir_under_its_repo(tmp_path, monkeypatch):
    from noctyx import orchestrator as o
    lister = _lister()
    hub = tmp_path / "hub"
    hub.mkdir()
    a = lister.describe_local(str(_local_dir(tmp_path / "a", {"config.json": b"{}", "w.safetensors": b"w" * 9})))
    b = lister.describe_local(str(_local_dir(tmp_path / "renamed", {"config.json": b"[]"}, commit="2" * 40)))
    calls = []

    def api(path):                                       # huggingface.co: org/A has C1; nothing has commit 2
        calls.append(path)
        if path.startswith("/api/models?search="):
            return [{"id": "other/a-instruct"}, {"id": "org/A"}]
        if path == f"/api/models/org/A/revision/{C1}":
            return {}
        if path.startswith("/api/datasets?search="):
            return []
        raise _http404(path)
    monkeypatch.setattr(o, "_hf_api", api)
    monkeypatch.setattr(o, "_run_on", lambda *x, **k: subprocess.CompletedProcess(x, 0, json.dumps([a, b]), ""))
    c = {"server": {"ssh": {"user": "ops", "key": "/k"}, "address": "192.0.2.10"}, "hosts": []}
    st = o.hf_harvest_host(c, {"name": "head", "address": "192.0.2.10"},
                           types.SimpleNamespace(repo=None, ignore_window=True), hub)
    rd = hub / "models--org--A"
    assert st["new_blobs"] == 2 and st["bad_hash"] == 0 and st["unresolved"] == [str(tmp_path / "renamed")]
    assert list(json.loads((tmp_path / "unresolved.json").read_text())) == [f"{'2' * 40} renamed "]
    assert (rd / "snapshots" / C1 / "w.safetensors").read_bytes() == b"w" * 9
    assert (rd / "snapshots" / C1 / "config.json").is_symlink() and not (hub.parent / ".incoming" / "head" / rd.name).exists()
    calls.clear()                                        # a second host: the store already names the commit
    st = o.hf_harvest_host(c, {"name": "head", "address": "192.0.2.10"},
                           types.SimpleNamespace(repo=None, ignore_window=True), hub)
    assert st["new_blobs"] == 0 and st["repos"] == 1 and not any(C1 in p for p in calls)


def _http404(path):
    import urllib.error
    return urllib.error.HTTPError(path, 404, "Not Found", None, None)


def test_the_lister_reports_the_repos_a_local_dir_names(tmp_path):
    lister = _lister()
    d = _local_dir(tmp_path / "x-src", {"config.json": b'{"_name_or_path": "base/Model-7B"}'})
    (d / "README.md").write_text("[card](https://huggingface.co/org/X) https://huggingface.co/org/X\n"
                                 "https://huggingface.co/datasets/org/data, https://huggingface.co/docs/hub and "
                                 "https://huggingface.co/papers/1234 git clone https://huggingface.co/org/Y.git\n"
                                 "![](https://cdn-uploads.huggingface.co/production/uploads/a.png)\n")
    assert lister.describe_local(str(d))["hints"] == ["models/org/X", "datasets/org/data", "models/org/Y",
                                                      "models/base/Model-7B"]


def test_a_renamed_local_dir_is_found_by_its_readme_or_a_shorter_name(tmp_path, monkeypatch):
    from noctyx import orchestrator as o
    hub = tmp_path / "hub"
    hub.mkdir()
    named = {"name": "x-src", "local": True, "files": {C1: {"a": "b"}}, "hints": ["models/base/Model", "models/org/X"]}
    short = {"name": "Thing-9B-fp8", "local": True, "files": {"2" * 40: {"a": "b"}}, "hints": []}
    calls = []

    def api(path):                       # org/X has C1 (the README's base model does not); Org/Thing-9B has commit 2
        calls.append(path)
        if path == f"/api/models/org/X/revision/{C1}":
            return {"id": "org/X"}
        if path == "/api/models?search=Thing-9B&limit=100":
            return [{"id": "other/Thing-9B-GGUF"}, {"id": "Org/Thing-9B"}]
        if path == f"/api/models/Org/Thing-9B/revision/{'2' * 40}":
            return {"id": "Org/Thing-9B"}
        if "?search=" in path:
            return []
        raise _http404(path)
    monkeypatch.setattr(o, "_hf_api", api)
    assert o._hf_local_repo(hub, named, {}) == "models--org--X"
    assert not any("search" in c for c in calls)
    assert o._hf_local_repo(hub, short, {}) == "models--Org--Thing-9B"
    assert f"/api/models/other/Thing-9B-GGUF/revision/{'2' * 40}" not in calls      # the exact name is asked first

    for err in (OSError("unreachable"), urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)):
        def refused(path):
            calls.append(path)
            raise err
        monkeypatch.setattr(o, "_hf_api", refused)
        memo = {}
        calls.clear()
        assert o._hf_local_repo(hub, named, memo) is None and o._hf_local_repo(hub, short, memo) is None
        assert memo["offline"] and len(calls) == 1 and not memo["neg"]     # nothing more asked; not given up on


def test_a_local_dir_nothing_names_is_asked_again_only_after_a_week(tmp_path, monkeypatch):
    from noctyx import orchestrator as o
    hub = tmp_path / "hub"
    hub.mkdir()
    d = {"name": "models", "local": True, "files": {C1: {"a": "b"}}, "hints": []}
    calls = []

    def api(path):
        calls.append(path)
        if "?search=" in path:
            return [{"id": f"o{i}/models"} for i in range(40)]
        raise _http404(path)
    monkeypatch.setattr(o, "_hf_api", api)
    memo = {}
    assert o._hf_local_repo(hub, d, memo) is None
    assert sum("/revision/" in c for c in calls) == 2 * o.HF_LOCAL_TRIES and memo["neg"]   # models, datasets
    calls.clear()
    assert o._hf_local_repo(hub, d, {"neg": dict(memo["neg"])}) is None and calls == []
    later = {k: t - o.HF_LOCAL_RETRY for k, t in memo["neg"].items()}
    assert o._hf_local_repo(hub, d, {"neg": later}) is None and calls
