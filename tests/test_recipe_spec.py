"""Tests for the `[deploy]` recipe.toml spec v0 parser, catalog, unit generation, and discovery."""
import pytest

from noctyx import discover, orchestrator as o, recipespec

VALID_TOML = """
[deploy]
spec = 0
hardware = "gb10"
nodes = 2
memory_gib = 110
prepare = "python3 -m spark image"
start = "python3 -m spark up"
stop = "python3 -m spark down"
start_timeout_s = 3600
port_env = "PORT"
health = "/health"
models = "/v1/models"
served = ["Qwen3.8-Flash-Next-INT4-Mixed"]

[[deploy.weights]]
repo = "ExampleOrg/Qwen3.8-Flash-Next-INT4-Mixed"
revision = "f6f90ddc39e9d212aa5b96f43999183deb4ce192"

[deploy.settings.WORKER]
description = "ssh login of the worker node"
required = true

[deploy.settings.HCA]
description = "RDMA device"
default = "rocep1s0f1"
"""


def test_parse_valid_deploy_spec():
    spec, errs = recipespec.parse_deploy_toml(VALID_TOML)
    assert errs == [] and spec is not None
    assert spec["spec"] == 0 and spec["hardware"] == "gb10" and spec["nodes"] == 2
    assert spec["prepare"] == "python3 -m spark image"
    assert spec["start"] == "python3 -m spark up" and spec["stop"] == "python3 -m spark down"
    assert spec["served"] == ["Qwen3.8-Flash-Next-INT4-Mixed"]
    assert spec["weights"][0]["revision"] == "f6f90ddc39e9d212aa5b96f43999183deb4ce192"

    env, missing = recipespec.resolve_settings(spec, {"PORT": "30000"})
    assert missing == ["WORKER"] and env["HCA"] == "rocep1s0f1"
    env2, missing2 = recipespec.resolve_settings(spec, {"PORT": "30000", "WORKER": "ops@gpu-02"})
    assert missing2 == [] and env2["WORKER"] == "ops@gpu-02"
    dotenv = recipespec.render_dotenv(env2)
    assert "HCA=rocep1s0f1" in dotenv and "WORKER=ops@gpu-02" in dotenv


def test_parse_deploy_spec_rejects_bad_fields():
    # No [deploy] table -> (None, [])
    assert recipespec.parse_deploy_toml("[other]\nx = 1\n") == (None, [])
    # Bad TOML
    spec, errs = recipespec.parse_deploy_toml("[deploy\n")
    assert spec is None and errs
    # Branch name instead of pinned commit sha, missing stop, wrong spec version
    bad = """
[deploy]
spec = 99
start = "echo hi"
[[deploy.weights]]
repo = "Org/Model"
revision = "main"
"""
    spec, errs = recipespec.parse_deploy_toml(bad)
    assert spec is None and len(errs) >= 3
    assert any("deploy.spec" in e for e in errs)
    assert any("deploy.stop" in e for e in errs)
    assert any("40-char commit sha" in e for e in errs)


def test_catalog_and_unit_generation_support_spec_kind():
    c = {"recipes": {"slots": {"pair": {"head": "gpu-01", "worker": "gpu-02", "port": 30000}}, "catalog": {}}}
    errs = o.validate_catalog_entry("my-spec-recipe", {
        "repo": "https://github.com/example/my-spec-recipe",
        "kind": "spec",
        "ref": "main",
        "slots": ["pair"],
    }, c)
    assert errs == []
    spec, _ = recipespec.parse_deploy_toml(VALID_TOML)
    rec = {"recipe": "my-spec-recipe", "kind": "spec", "sha": "a" * 40, "dir": "/opt/recipes/r",
           "user": "ops", "home": "/srv/ops", "spec": spec}
    unit = o._unit_text(rec, "pair", {"port": 30000, "health_timeout_s": 1800}, {"PORT": "30000", "WORKER": "ops@gpu-02"})
    assert "Type=oneshot" in unit and "RemainAfterExit=yes" in unit
    assert "ExecStart=/bin/bash -lc 'python3 -m spark up'" in unit
    assert "ExecStop=/bin/bash -lc 'python3 -m spark down'" in unit
    assert "TimeoutStartSec=3900" in unit


def test_discover_detects_deploy_spec_repos(monkeypatch):
    c = {"hosts": [{"name": "gpu-01", "os": "linux"}],
         "recipes": {"slots": {"pair": {"head": "gpu-01", "worker": "gpu-02", "hw": "gb10", "gpus": 2, "port": 30000}},
                     "catalog": {}, "discover_queries": ["q1"]}}
    monkeypatch.setattr(discover, "_gh_search", lambda q: [{
        "full_name": "example/custom-model",
        "html_url": "https://github.com/example/custom-model",
        "clone_url": "https://github.com/example/custom-model.git",
        "default_branch": "main",
        "description": "A spec-compliant recipe",
        "stargazers_count": 42,
        "pushed_at": "2026-11-15T00:00:00Z",
        "archived": False,
        "fork": False,
    }])
    monkeypatch.setattr(discover, "_raw", lambda full, br, path: VALID_TOML if path == "recipe.toml" else None)
    monkeypatch.setattr(discover, "_speed_sources", lambda c: {})
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)

    res = discover.recipe_discover(c)
    assert len(res["items"]) == 1
    it = res["items"][0]
    assert it["kind"] == "spec" and it["spec"] is True and it["deployable"] is True
    assert it["hw"] == "gb10" and it["units"] == 2 and it["slots"] == ["pair"]
    assert it["models"] == ["ExampleOrg/Qwen3.8-Flash-Next-INT4-Mixed"]
