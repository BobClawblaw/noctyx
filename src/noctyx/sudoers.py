"""Scoped sudoers generator for managed Linux nodes (/etc/sudoers.d/noctyx).

Replaces blanket `NOPASSWD: ALL` for the head node's SSH user with an explicit command whitelist
covering Noctyx's agent, backup dumps, restic checks, service management for managed units,
hygiene/diagnostics, and HF mirror rsync reads.
"""
from __future__ import annotations

import re
import subprocess
import tempfile

from noctyx.paths import P

_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9/._@-]+$")


def _check_path(p: str, label: str) -> str:
    if not _SAFE_PATH.match(p or "") or ".." in (p or "").split("/"):
        raise ValueError(f"unsafe {label} path for sudoers: {p!r}")
    return p


def render_sudoers(user: str, paths: dict | None = None, allow_reboot: bool = False,
                   allow_recipes: bool = False) -> str:
    """Render a least-privilege `/etc/sudoers.d/noctyx` drop-in for `user` on a Linux node."""
    if not _USER_RE.match(user or ""):
        raise ValueError(f"invalid Unix username for sudoers: {user!r}")
    p = {**P, **(paths or {})}
    agent_bin = _check_path(p["agent_bin"], "agent_bin")
    dumps_bin = _check_path(p["dumps_bin"], "dumps_bin")
    node_etc = _check_path(p["node_etc"], "node_etc")
    node_lib = _check_path(p["node_lib"], "node_lib")
    unit_pfx = p["unit_prefix"]
    recipe_pfx = p["recipe_unit_prefix"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", unit_pfx):
        raise ValueError(f"unsafe unit_prefix: {unit_pfx!r}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", recipe_pfx):
        raise ValueError(f"unsafe recipe_unit_prefix: {recipe_pfx!r}")

    cmds_agent = [
        f"{agent_bin} *",
        f"{dumps_bin} *",
        f"/usr/bin/restic --cacert {node_etc}/tls/ca.crt --password-file {node_etc}/repo-password --repository-file {node_etc}/repo-url *",
        f"/usr/local/bin/restic --cacert {node_etc}/tls/ca.crt --password-file {node_etc}/repo-password --repository-file {node_etc}/repo-url *",
    ]
    cmds_inspect = [
        "/usr/bin/journalctl *",
        "/bin/journalctl *",
        "/usr/bin/systemctl status *",
        "/usr/bin/systemctl show *",
        "/usr/bin/systemctl is-active *",
        "/usr/bin/systemctl is-enabled *",
        "/usr/bin/systemctl list-units *",
        "/bin/systemctl status *",
        "/bin/systemctl show *",
        "/bin/systemctl is-active *",
        "/bin/systemctl is-enabled *",
        f"/usr/bin/fuser {node_lib}/agent.lock",
        "/usr/bin/fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock",
        "/usr/bin/rsync --server --sender *",
    ]
    cmds_units = [
        "/usr/bin/systemctl daemon-reload",
        f"/usr/bin/systemctl restart {unit_pfx}-*",
        f"/usr/bin/systemctl start {unit_pfx}-*",
        f"/usr/bin/systemctl stop {unit_pfx}-*",
        f"/usr/bin/systemctl enable --now {unit_pfx}-*",
        "/usr/bin/systemctl restart prometheus-node-exporter",
        "/usr/bin/systemctl restart node_exporter",
    ]
    if allow_recipes:
        cmds_units += [
            f"/usr/bin/systemctl start {recipe_pfx}-*",
            f"/usr/bin/systemctl stop {recipe_pfx}-*",
            f"/usr/bin/systemctl restart {recipe_pfx}-*",
            f"/usr/bin/systemctl enable {recipe_pfx}-*",
            f"/usr/bin/systemctl disable --now {recipe_pfx}-*",
        ]
    if allow_reboot:
        cmds_units.append(f"/usr/bin/systemd-run --quiet --collect --unit={unit_pfx}-host-reboot-* * systemctl reboot")

    lines = [
        "# Managed by Noctyx — scoped least-privilege sudoers for a Linux node.",
        "# Install with: sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/noctyx",
        "# Verify with:  sudo visudo -cf /etc/sudoers.d/noctyx",
        f"Defaults:{user} !requiretty, env_reset",
        "",
        "Cmnd_Alias NOCTYX_AGENT = " + ", \\\n    ".join(cmds_agent),
        "Cmnd_Alias NOCTYX_INSPECT = " + ", \\\n    ".join(cmds_inspect),
        "Cmnd_Alias NOCTYX_UNITS = " + ", \\\n    ".join(cmds_units),
        "",
        f"{user} ALL=(root) NOPASSWD: NOCTYX_AGENT, NOCTYX_INSPECT, NOCTYX_UNITS",
        "",
    ]
    return "\n".join(lines)


def validate_sudoers_text(text: str) -> tuple[bool, str]:
    """Validate rendered sudoers text structurally, and with `visudo -cf` if present."""
    if not text or "NOPASSWD: ALL" in text:
        return False, "must not grant unrestricted NOPASSWD: ALL"
    if "Cmnd_Alias NOCTYX_AGENT =" not in text or "ALL=(root) NOPASSWD:" not in text:
        return False, "missing required Noctyx Cmnd_Alias or user grant"
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=True) as tf:
            tf.write(text)
            tf.flush()
            r = subprocess.run(["visudo", "-cf", tf.name], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout).strip()[:300]
            return True, "visudo ok"
    except FileNotFoundError:
        return True, "structural ok (visudo not installed)"
