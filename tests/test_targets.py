"""Tests for pluggable secondary backup targets (local, rest, s3, b2, sftp)."""
import json
import subprocess

from noctyx import targets as bt


def test_validate_all_target_kinds():
    ok_cases = [
        ("usb", {"kind": "local", "path": "/mnt/usb/backups", "mount": "/mnt/usb"}),
        ("remote-nas", {"kind": "rest", "url": "https://192.0.2.50:20998", "ca": "/etc/noctyx/tls/ca.crt"}),
        ("offsite-s3", {"kind": "s3", "endpoint": "https://s3.us-west-004.backblazeb2.com", "bucket": "my-cluster-backups", "region": "us-west-004"}),
        ("offsite-b2", {"kind": "b2", "bucket": "my-b2-bucket"}),
        ("offsite-sftp", {"kind": "sftp", "host": "192.0.2.99", "user": "backup", "path": "/srv/backups", "port": 2222}),
    ]
    for name, cfg in ok_cases:
        clean, errs = bt.validate_target(name, cfg)
        assert errs == [] and clean is not None and clean["kind"] == cfg["kind"]

    bad_cases = [
        ("bad name", {"kind": "local", "path": "/mnt/x"}),
        ("x", {"kind": "unknown"}),
        ("x", {"kind": "local", "path": "relative/path"}),
        ("x", {"kind": "rest", "url": "ftp://192.0.2.50"}),
        ("x", {"kind": "s3", "bucket": "INVALID_BUCKET!"}),
        ("x", {"kind": "b2", "bucket": "ab"}),
        ("x", {"kind": "sftp", "host": "192.0.2.1", "path": "/a/../b", "port": 99999}),
    ]
    for name, cfg in bad_cases:
        clean, errs = bt.validate_target(name, cfg)
        assert clean is None and errs


def test_repo_spec_loads_credentials_into_env_not_display_uri(tmp_path, monkeypatch):
    monkeypatch.setitem(bt.P, "etc", str(tmp_path))
    creds = tmp_path / "target-creds"
    creds.mkdir()
    (creds / "offsite.env").write_text("AWS_ACCESS_KEY_ID=AKIAEXAMPLE\nAWS_SECRET_ACCESS_KEY='supersecret'\n")
    t = {"kind": "s3", "endpoint": "s3.amazonaws.com", "bucket": "cluster-bak", "region": "us-east-1", "prefix": "prod"}
    uri, extra, env = bt.repo_spec("offsite", t, "gpu-01", base_env={})
    assert uri == "s3:s3.amazonaws.com/cluster-bak/prod/nx-gpu-01/gpu-01"
    assert env["RESTIC_REPOSITORY"] == uri
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
    assert env["AWS_SECRET_ACCESS_KEY"] == "supersecret"
    assert env["AWS_DEFAULT_REGION"] == "us-east-1"
    assert "supersecret" not in bt.display_uri(t, "gpu-01")
    ready, _ = bt.check_target_ready("offsite", t)
    assert ready

    # Missing creds fails readiness
    ready2, why = bt.check_target_ready("missing-s3", {"kind": "s3", "endpoint": "s3.amazonaws.com", "bucket": "b"})
    assert not ready2 and "AWS_ACCESS_KEY_ID" in why


def test_copy_to_target_dry_run_and_init_copy(tmp_path, monkeypatch):
    monkeypatch.setitem(bt.P, "repo_keys", str(tmp_path / "keys"))
    monkeypatch.setitem(bt.P, "etc", str(tmp_path / "etc"))
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "gpu-01.key").write_text("repo-pass-123\n")
    primary = tmp_path / "primary" / "nx-gpu-01" / "gpu-01"
    primary.mkdir(parents=True)
    dst_mount = tmp_path / "usb"
    dst_mount.mkdir()

    site_cfg = {
        "server": {"repo_root": str(tmp_path / "primary")},
        "repositories": [{"id": "gpu-01", "spindle": "nas"}],
    }
    target = {"kind": "local", "path": str(dst_mount / "repos"), "mount": str(dst_mount)}
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw["env"]["RESTIC_REPOSITORY"]))
        if "snapshots" in argv:
            if kw["env"]["RESTIC_REPOSITORY"] == str(primary):
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"id": "snap1"}]), "")
            return subprocess.CompletedProcess(argv, 1, "", "not initialized")
        return subprocess.CompletedProcess(argv, 0, "", "")

    # Dry run never runs init or copy
    ok, total = bt.copy_to_target(site_cfg, "usb", target, dry_run=True, run_fn=fake_run)
    assert (ok, total) == (1, 1)
    assert not any("init" in a or "copy" in a for a, _ in calls)

    # Real run inits and copies
    calls.clear()
    audited = []
    ok, total = bt.copy_to_target(site_cfg, "usb", target, dry_run=False, run_fn=fake_run,
                                  audit_fn=lambda ev, **kw: audited.append((ev, kw)))
    assert (ok, total) == (1, 1)
    assert any("init" in a for a, _ in calls)
    assert any("copy" in a for a, _ in calls)
    assert audited[0][0] == "copy" and audited[0][1]["target"] == "usb" and audited[0][1]["snapshots"] == 1
