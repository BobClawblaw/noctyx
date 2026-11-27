"""Parser and validator for the open `[deploy]` section in `recipe.toml` (spec v0).

See docs/spec/recipe-deploy.md. Pure stdlib (`tomllib`, Python 3.11+).
"""
from __future__ import annotations

import re
import shlex
import tomllib

SUPPORTED_SPEC = 0
_ENVK = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_HF_REPO = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_URL_PATH = re.compile(r"^/[A-Za-z0-9._~/%-]{0,200}$")

# Map `[deploy].hardware` identifiers to Noctyx's discover GPU keys
SPEC_HW_MAP = {
    "gb10": "gb10",
    "sm_120": "rtx5090",
    "sm_89": "rtx4090",
    "rtx-pro-6000": "rtx-pro-6000",
    "rtx5090": "rtx5090",
    "rtx5080": "rtx5080",
    "rtx4090": "rtx4090",
    "rtx3090": "rtx3090",
    "h200": "h200",
    "h100": "h100",
    "b200": "b200",
    "a100": "a100",
    "l40s": "l40s",
    "apple-m": "mac",
    "mac": "mac",
    "cpu": "cpu",
}


def _one_line(val):
    return isinstance(val, str) and bool(val.strip()) and not any(c in val for c in "\n\r\0")


def parse_deploy_toml(text: str):
    """Parse `recipe.toml` text -> (clean_spec | None, errors).

    Returns `(None, [])` when the TOML is valid but has no `[deploy]` table.
    """
    try:
        doc = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError as e:
        return None, [f"invalid TOML: {e}"]
    dep = doc.get("deploy")
    if dep is None:
        return None, []
    return validate_deploy(dep)


def validate_deploy(dep):
    """Validate a `[deploy]` dict -> (clean_spec | None, errors)."""
    if not isinstance(dep, dict):
        return None, ["[deploy] must be a table"]
    errs = []
    spec = dep.get("spec")
    if spec != SUPPORTED_SPEC:
        errs.append(f"deploy.spec: expected {SUPPORTED_SPEC}, got {spec!r}")
    start = dep.get("start")
    stop = dep.get("stop")
    if not _one_line(start):
        errs.append("deploy.start: non-empty single-line command required")
    if not _one_line(stop):
        errs.append("deploy.stop: non-empty single-line command required")
    prepare = dep.get("prepare")
    if prepare is not None and not _one_line(prepare):
        errs.append("deploy.prepare: must be a non-empty single-line command")

    hw_raw = dep.get("hardware")
    if hw_raw is not None and (not isinstance(hw_raw, str) or not hw_raw.strip()):
        errs.append("deploy.hardware: must be a non-empty string")
    nodes = dep.get("nodes", 1)
    if isinstance(nodes, bool) or not isinstance(nodes, int) or not 1 <= nodes <= 64:
        errs.append("deploy.nodes: integer 1..64")
    mem = dep.get("memory_gib")
    if mem is not None and (isinstance(mem, bool) or not isinstance(mem, (int, float)) or mem <= 0):
        errs.append("deploy.memory_gib: positive number")

    timeout = dep.get("start_timeout_s")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 86400):
        errs.append("deploy.start_timeout_s: integer 10..86400")

    port_env = dep.get("port_env", "PORT")
    if not isinstance(port_env, str) or not _ENVK.match(port_env):
        errs.append("deploy.port_env: UPPER_CASE env var name")

    health = dep.get("health", "/health")
    models = dep.get("models", "/v1/models")
    if not isinstance(health, str) or not _URL_PATH.match(health):
        errs.append("deploy.health: URL path starting with /")
    if not isinstance(models, str) or not _URL_PATH.match(models):
        errs.append("deploy.models: URL path starting with /")

    served = dep.get("served") or []
    if not isinstance(served, list) or any(not _one_line(x) for x in served):
        errs.append("deploy.served: list of non-empty model id strings")

    weights = []
    raw_w = dep.get("weights") or []
    if not isinstance(raw_w, list):
        errs.append("deploy.weights: array of tables")
    else:
        for i, w in enumerate(raw_w):
            if not isinstance(w, dict):
                errs.append(f"deploy.weights[{i}]: table expected")
                continue
            repo, rev = w.get("repo"), w.get("revision")
            if not isinstance(repo, str) or not _HF_REPO.match(repo):
                errs.append(f"deploy.weights[{i}].repo: expected org/name")
            if not isinstance(rev, str) or not _SHA40.match(rev):
                errs.append(f"deploy.weights[{i}].revision: 40-char commit sha required (not a branch)")
            weights.append({"repo": repo, "revision": rev})

    settings = {}
    raw_s = dep.get("settings") or {}
    if not isinstance(raw_s, dict):
        errs.append("deploy.settings: table expected")
    else:
        for k, v in raw_s.items():
            if not _ENVK.match(str(k)):
                errs.append(f"deploy.settings.{k}: UPPER_CASE env var name")
                continue
            if not isinstance(v, dict):
                errs.append(f"deploy.settings.{k}: table expected")
                continue
            settings[k] = {
                "description": str(v.get("description") or "")[:200],
                "required": bool(v.get("required", False)),
                **({"example": str(v["example"])[:200]} if "example" in v else {}),
                **({"default": str(v["default"])} if "default" in v else {}),
            }

    if errs:
        return None, errs

    hw = SPEC_HW_MAP.get((hw_raw or "").strip().lower(), (hw_raw or "").strip().lower() or None)
    clean = {
        "spec": SUPPORTED_SPEC,
        "hardware": hw,
        "hardware_raw": (hw_raw or "").strip() or None,
        "nodes": nodes,
        "memory_gib": mem,
        "prepare": prepare.strip() if prepare else None,
        "start": start.strip(),
        "stop": stop.strip(),
        "start_timeout_s": timeout,
        "port_env": port_env,
        "health": health,
        "models": models,
        "served": [x.strip() for x in served],
        "weights": weights,
        "settings": settings,
    }
    return clean, []


def resolve_settings(spec: dict, provided_env: dict):
    """Apply defaults from `spec['settings']` and check all required settings are supplied.

    Returns `(merged_env, missing_keys)`.
    """
    env = dict(provided_env or {})
    missing = []
    for k, meta in (spec.get("settings") or {}).items():
        if k not in env or env[k] in (None, ""):
            if "default" in meta:
                env[k] = meta["default"]
            elif meta.get("required"):
                missing.append(k)
    return env, missing


def render_dotenv(env: dict) -> str:
    """Render environment variables for a `.env` file safely."""
    lines = ["# written by `noctyx recipe-deploy` (recipe.toml [deploy] spec + slot/catalog settings)"]
    for k, v in sorted(env.items()):
        lines.append(f"{k}={shlex.quote(str(v))}")
    return "\n".join(lines) + "\n"
