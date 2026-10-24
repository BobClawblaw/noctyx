"""The fleet's early warnings: their thresholds come from the site config, every rule renders with no
placeholder left, and Prometheus's own checker accepts them."""
import re
import shutil
import subprocess

import pytest

from noctyx import signals


def test_thresholds_default_and_override():
    assert signals.policy({})["full_within_days"] == 7
    pol = signals.policy({"signals": {"full_within_days": 3, "nope": 1}})
    assert pol["full_within_days"] == 3 and "nope" not in pol
    assert signals.placeholders({"signals": {"nfs_slow_ratio": 0.3}})["SIG_NFS_SLOW_RATIO"] == "0.3"


@pytest.mark.parametrize("blk, bad", [({"full_within_days": 0}, "1 to 90"), ({"nfs_slow_ratio": 2}, "0.01 to 0.99"),
                                      ({"ssh_failed_per_15m": True}, "a number"), ({"x": 1}, "unknown")])
def test_thresholds_refused(blk, bad):
    assert any(bad in e for e in signals.validate(blk)[1])


def rendered(c):
    from noctyx import orchestrator as cm
    text = (cm.NX_HOME / "metrics" / "rules.yml").read_text()
    text = cm.render(text)
    for k, v in signals.placeholders(c).items():
        text = text.replace(f"@{k}@", v)
    return text


def test_every_signal_rule_renders():
    text = rendered({"signals": {"growth_gb_per_hour": 50}})
    sig = text[text.index("- name: signals"):]
    assert not re.search(r"@[A-Z_]+@", sig)
    assert "> 50 * 1e9" in sig
    for name in ("NfsMountHung", "NfsSlow", "FabricLinkDown", "FabricRetransmits", "SshFailedLoginsBurst",
                 "SshLoginFromNewAddress", "StorageFullSoon", "StorageGrewFast"):
        assert f"alert: {name}" in sig


@pytest.mark.skipif(not shutil.which("promtool"), reason="promtool is not installed here")
def test_promtool_accepts_the_rules(tmp_path):
    text = rendered({})
    text = re.sub(r"@[A-Z_]+@", "x", text)            # the site's other placeholders (job names)
    f = tmp_path / "rules.yml"
    f.write_text(text)
    r = subprocess.run(["promtool", "check", "rules", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
