"""Pluggable backup targets for Noctyx: NAS/local disk, REST server, S3-compatible, Backblaze B2, and SFTP.

Every target resolves a restic repository location and environment (credentials always loaded from
root-only files in `<etc>/target-creds/` or env-file paths — never in `site.json` or command-line args).
Used by `noctyx copy --target <name>` for off-site / secondary 3-2-1 replication alongside the primary
repository root.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

from noctyx.paths import P

TARGET_KINDS = ("local", "rest", "s3", "b2", "sftp")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
_SAFE_URL = re.compile(r"^[A-Za-z0-9._:/@-]{1,512}$")


def _creds_dir():
    return pathlib.Path(P["etc"]) / "target-creds"


def load_env_file(path: str | pathlib.Path) -> dict[str, str]:
    """Read a root-only KEY=VALUE credentials file (comments and blank lines ignored)."""
    out = {}
    p = pathlib.Path(path)
    if not p.is_file():
        return out
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if re.match(r"^[A-Z_][A-Z0-9_]{0,63}$", k):
            out[k] = v
    return out


def validate_target(name: str, cfg: dict):
    """Validate one backup target entry -> (clean_dict | None, errors)."""
    errs = []
    if not _NAME.match(name or ""):
        errs.append("name: lowercase letters, digits, . _ - (max 48)")
    if not isinstance(cfg, dict):
        return None, errs + ["target must be an object"]
    kind = str(cfg.get("kind") or "").strip().lower()
    if kind not in TARGET_KINDS:
        errs.append(f"kind: one of {', '.join(TARGET_KINDS)}")
        return None, errs

    clean = {"kind": kind, "enabled": cfg.get("enabled", True) is not False}
    if cfg.get("prefix"):
        pref = str(cfg["prefix"]).strip("/")
        if not re.match(r"^[A-Za-z0-9._/-]{1,128}$", pref) or ".." in pref.split("/"):
            errs.append("prefix: path segments with letters, digits, . _ - only")
        else:
            clean["prefix"] = pref

    if cfg.get("credentials_file"):
        cf = str(cfg["credentials_file"]).strip()
        if not cf.startswith("/") or ".." in cf.split("/") or any(c in cf for c in "\n\r\0"):
            errs.append("credentials_file: absolute path without '..'")
        else:
            clean["credentials_file"] = cf

    if kind == "local":
        path = str(cfg.get("path") or "").strip()
        if not path.startswith("/") or ".." in path.split("/") or any(c in path for c in "\n\r\0"):
            errs.append("local.path: absolute directory path required")
        else:
            clean["path"] = path.rstrip("/") or "/"
        if cfg.get("mount"):
            clean["mount"] = str(cfg["mount"]).strip()
        if cfg.get("spindle"):
            clean["spindle"] = str(cfg["spindle"]).strip()

    elif kind == "rest":
        url = str(cfg.get("url") or "").strip().rstrip("/")
        if not re.match(r"^https?://[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._/-]*)?$", url):
            errs.append("rest.url: https://host:port[/prefix] required (put credentials in credentials_file, not URL)")
        else:
            clean["url"] = url
        if cfg.get("ca"):
            clean["ca"] = str(cfg["ca"]).strip()

    elif kind == "s3":
        endpoint = str(cfg.get("endpoint") or "s3.amazonaws.com").strip().rstrip("/")
        bucket = str(cfg.get("bucket") or "").strip().strip("/")
        if not re.match(r"^(?:https?://)?[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._/-]*)?$", endpoint):
            errs.append("s3.endpoint: hostname or https://endpoint required")
        if not re.match(r"^[a-z0-9][a-z0-9.-]{1,62}$", bucket):
            errs.append("s3.bucket: valid bucket name required")
        clean["endpoint"] = endpoint
        clean["bucket"] = bucket
        if cfg.get("region"):
            clean["region"] = str(cfg["region"]).strip()

    elif kind == "b2":
        bucket = str(cfg.get("bucket") or "").strip().strip("/")
        if not re.match(r"^[a-z0-9][a-z0-9-]{4,62}$", bucket):
            errs.append("b2.bucket: valid B2 bucket name required")
        clean["bucket"] = bucket

    elif kind == "sftp":
        host = str(cfg.get("host") or "").strip()
        path = str(cfg.get("path") or "").strip()
        user = str(cfg.get("user") or "").strip()
        if not host or not _SAFE_URL.match(host) or "/" in host:
            errs.append("sftp.host: valid hostname or IP required")
        if not path or any(c in path for c in "\n\r\0") or ".." in path.split("/"):
            errs.append("sftp.path: valid remote path required")
        clean["host"] = host
        clean["path"] = path.rstrip("/") or "/"
        if user:
            clean["user"] = user
        if cfg.get("port"):
            try:
                port = int(cfg["port"])
                if not 1 <= port <= 65535:
                    raise ValueError
                clean["port"] = port
            except (TypeError, ValueError):
                errs.append("sftp.port: integer 1..65535")

    return (None, errs) if errs else (clean, [])


def configured_targets(site_cfg: dict) -> dict[str, dict]:
    """Return enabled secondary backup targets from `site_cfg['backup_targets']`."""
    raw = site_cfg.get("backup_targets") or {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, t in raw.items():
        if name.startswith("_") or not isinstance(t, dict):
            continue
        clean, errs = validate_target(name, t)
        if clean and not errs and clean.get("enabled", True):
            out[name] = clean
    return out


def repo_spec(name: str, target: dict, repo_id: str, base_env: dict | None = None):
    """Build `(repo_uri, extra_args, env)` for restic against `target` and `repo_id`.

    Secrets come from `target['credentials_file']` (or `<etc>/target-creds/<name>.env`) and are placed
    into the subprocess environment (`RESTIC_REPOSITORY`, `AWS_ACCESS_KEY_ID`, `B2_ACCOUNT_ID`, etc.)
    rather than argv so credentials never leak into `ps`.
    """
    kind = target["kind"]
    prefix = target.get("prefix")
    rel = f"{prefix}/{P['repo_dir_prefix']}{repo_id}/{repo_id}" if prefix else f"{P['repo_dir_prefix']}{repo_id}/{repo_id}"

    cfile = pathlib.Path(target["credentials_file"]) if target.get("credentials_file") else (_creds_dir() / f"{name}.env")
    creds = load_env_file(cfile)
    env = dict(base_env if base_env is not None else os.environ)
    env.update(creds)
    extra_args = []

    if kind == "local":
        uri = f"{target['path'].rstrip('/')}/{rel}"
    elif kind == "rest":
        url = target["url"].rstrip("/")
        if creds.get("REST_USER") and creds.get("REST_PASSWORD"):
            scheme, _, rest = url.partition("://")
            uri = f"rest:{scheme}://{creds['REST_USER']}:{creds['REST_PASSWORD']}@{rest}/{rel}"
        else:
            uri = f"rest:{url}/{rel}"
        if target.get("ca"):
            extra_args += ["--cacert", target["ca"]]
    elif kind == "s3":
        ep = target["endpoint"].rstrip("/")
        uri = f"s3:{ep}/{target['bucket']}/{rel}"
        if target.get("region"):
            env.setdefault("AWS_DEFAULT_REGION", target["region"])
    elif kind == "b2":
        uri = f"b2:{target['bucket']}:{rel}"
    elif kind == "sftp":
        who = f"{target['user']}@{target['host']}" if target.get("user") else target["host"]
        rpath = f"{target['path'].rstrip('/')}/{rel}"
        if target.get("port"):
            uri = f"sftp://{who}:{target['port']}/{rpath.lstrip('/')}"
        else:
            uri = f"sftp:{who}:{rpath}"
    else:
        raise ValueError(f"unsupported backup target kind: {kind!r}")

    env["RESTIC_REPOSITORY"] = uri
    return uri, extra_args, env


def display_uri(target: dict, repo_id: str) -> str:
    """Credential-free URI safe for logs and audit trails."""
    kind = target["kind"]
    prefix = target.get("prefix")
    rel = f"{prefix}/{P['repo_dir_prefix']}{repo_id}/{repo_id}" if prefix else f"{P['repo_dir_prefix']}{repo_id}/{repo_id}"
    if kind == "local":
        return f"{target['path'].rstrip('/')}/{rel}"
    if kind == "rest":
        return f"rest:{target['url'].rstrip('/')}/{rel}"
    if kind == "s3":
        return f"s3:{target['endpoint'].rstrip('/')}/{target['bucket']}/{rel}"
    if kind == "b2":
        return f"b2:{target['bucket']}:{rel}"
    if kind == "sftp":
        who = f"{target['user']}@{target['host']}" if target.get("user") else target["host"]
        return f"sftp:{who}:{target['path'].rstrip('/')}/{rel}"
    return f"{kind}:{rel}"


def check_target_ready(name: str, target: dict) -> tuple[bool, str]:
    """Pre-flight check before copying (e.g., mount present for local disk, credentials file present for S3/B2)."""
    kind = target["kind"]
    if kind == "local":
        mount = target.get("mount") or target["path"]
        if not os.path.isdir(mount):
            return False, f"target {name}: path/mount {mount} is not present"
        return True, "ready"
    cfile = pathlib.Path(target["credentials_file"]) if target.get("credentials_file") else (_creds_dir() / f"{name}.env")
    creds = load_env_file(cfile)
    if kind == "s3":
        if not (creds.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")):
            return False, f"target {name}: missing AWS_ACCESS_KEY_ID in {cfile}"
        if not (creds.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")):
            return False, f"target {name}: missing AWS_SECRET_ACCESS_KEY in {cfile}"
    elif kind == "b2":
        if not (creds.get("B2_ACCOUNT_ID") or os.environ.get("B2_ACCOUNT_ID")):
            return False, f"target {name}: missing B2_ACCOUNT_ID in {cfile}"
        if not (creds.get("B2_ACCOUNT_KEY") or os.environ.get("B2_ACCOUNT_KEY")):
            return False, f"target {name}: missing B2_ACCOUNT_KEY in {cfile}"
    return True, "ready"


def copy_to_target(site_cfg: dict, target_name: str, target: dict, host: str | None = None,
                   dry_run: bool = False, run_fn=None, audit_fn=None) -> tuple[int, int]:
    """Copy snapshots from the primary `repo_root` into secondary `target`.

    Returns `(ok_count, total_repos)`. Uses `RESTIC_REPOSITORY` env var (never `-r` on argv for remote
    targets) and the same per-repo encryption key so copies stay blob-level incremental.
    """
    from noctyx import orchestrator as cm
    import json
    import time

    ready, why = check_target_ready(target_name, target)
    if not ready:
        raise RuntimeError(why)

    runner = run_fn or subprocess.run
    repos = [r["id"] for r in site_cfg.get("repositories") or []]
    ok = 0
    for repo in repos:
        key = f"{P['repo_keys']}/{repo}.key"
        src = cm.repo_dir(site_cfg, repo)
        if not os.path.isdir(src):
            print(f"SKIP {repo}: primary {src} not initialised")
            continue
        try:
            pw = pathlib.Path(key).read_text().strip()
        except OSError as e:
            print(f"SKIP {repo}: cannot read {key}: {e}")
            continue

        base = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        base.update(RESTIC_PASSWORD=pw)
        _, extra, env = repo_spec(target_name, target, repo, base_env=base)
        copy_env = {**env, "RESTIC_FROM_PASSWORD": pw, "RESTIC_FROM_REPOSITORY": src}
        disp = display_uri(target, repo)

        def _snap_ids(is_src: bool):
            if is_src:
                senv = {**base, "RESTIC_REPOSITORY": src}
                r = runner(["restic", "--no-lock", "--no-cache", "snapshots", "--json"]
                           + (["--host", host] if host else []),
                           capture_output=True, text=True, env=senv, timeout=600)
            else:
                r = runner(["restic", *extra, "--no-lock", "--no-cache", "snapshots", "--json"]
                           + (["--host", host] if host else []),
                           capture_output=True, text=True, env=env, timeout=600)
            if r.returncode != 0:
                return None
            try:
                return {s.get("original") or s["id"] for s in json.loads(r.stdout or "[]")}
            except json.JSONDecodeError:
                return None

        src_ids = _snap_ids(True)
        if src_ids is None:
            print(f"SKIP {repo}: cannot read primary snapshots")
            continue
        dst_ids = _snap_ids(False)
        dst_exists = dst_ids is not None
        pending = src_ids - (dst_ids or set())

        if dry_run:
            print(f"DRY  {repo}: {len(pending)} snapshot(s) of {len(src_ids)} would copy to {target_name} ({disp})")
            ok += 1
            continue
        if not pending:
            print(f"OK   {repo}: {target_name} up to date ({len(src_ids)} snapshots)")
            ok += 1
            continue
        if target["kind"] == "local":
            os.makedirs(os.path.dirname(env["RESTIC_REPOSITORY"]), exist_ok=True)
        if not dst_exists:
            print(f"INIT {target_name} {disp} (same key + chunker params as {repo})")
            r0 = runner(["restic", *extra, "--no-cache", "init", "--copy-chunker-params"],
                        capture_output=True, text=True, env=copy_env, timeout=300)
            if r0.returncode != 0 and "already initialized" not in (r0.stderr or ""):
                print(f"  init failed: {(r0.stderr or '')[-200:]}")
                continue
        t0 = time.time()
        print(f"COPY {repo} ({len(pending)} snapshot(s)) -> {target_name} ({disp})")
        r = runner(["restic", *extra, "--no-cache", "copy"] + (["--host", host] if host else []),
                   capture_output=True, text=True, env=copy_env, timeout=3 * 86400)
        dt = time.time() - t0
        if audit_fn:
            audit_fn("copy", target=target_name, kind=target["kind"], repo=repo,
                     rc=r.returncode, snapshots=len(pending), seconds_wall=round(dt))
        print(f"  {'OK' if r.returncode == 0 else 'FAIL'} target={target_name} repo={repo} wall={dt:.0f}s"
              + ("" if r.returncode == 0 else f"\n    {(r.stderr or '')[-300:]}"))
        if r.returncode == 0:
            ok += 1
    return ok, len(repos)
