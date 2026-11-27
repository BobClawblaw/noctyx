"""Pure-stdlib Debian (.deb) packager for the Noctyx Linux node agent.

Builds a valid `noctyx-agent_<version>_all.deb` archive (standard GNU `ar` container with
`debian-binary`, `control.tar.gz`, and `data.tar.gz`) using Python's standard library only,
so packages can be built identically on Linux or macOS without requiring `dpkg-deb`.
"""
from __future__ import annotations

import gzip
import io
import pathlib
import tarfile
import time

from noctyx import __version__
from noctyx.paths import DEFAULTS, P, render

NX_HOME = pathlib.Path(__file__).resolve().parent


def _ar_member(name: str, data: bytes, mtime: int) -> bytes:
    """Format one GNU/Debian `ar` archive member (60-byte ASCII header + payload + even-byte pad)."""
    hdr = (
        f"{name:<16}"
        f"{mtime:<12d}"
        f"{0:<6d}"
        f"{0:<6d}"
        f"{100644:<8o}"
        f"{len(data):<10d}"
        "`\n"
    ).encode("ascii")
    assert len(hdr) == 60
    pad = b"\n" if (len(data) % 2) else b""
    return hdr + data + pad


def _tar_gz(entries: list[tuple[str, bytes, int]], mtime: int) -> bytes:
    """Build a deterministic root-owned gzipped tar archive from `(arcname, content_bytes, mode)` tuples."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=mtime) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf:
            seen_dirs: set[str] = set()
            for raw_name, content, mode in sorted(entries, key=lambda x: x[0]):
                arcname = "./" + raw_name.lstrip("./")
                # Emit parent directory entries explicitly (dpkg expects them)
                parts = arcname.split("/")[1:-1]
                cur = "."
                for part in parts:
                    cur = f"{cur}/{part}"
                    if cur not in seen_dirs:
                        seen_dirs.add(cur)
                        di = tarfile.TarInfo(cur + "/")
                        di.type = tarfile.DIRTYPE
                        di.mode = 0o755
                        di.uid = di.gid = 0
                        di.uname = di.gname = "root"
                        di.mtime = mtime
                        tf.addfile(di)
                ti = tarfile.TarInfo(arcname)
                ti.size = len(content)
                ti.mode = mode
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = "root"
                ti.mtime = mtime
                tf.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


def _service_unit(paths: dict) -> str:
    return (
        "[Unit]\n"
        "Description=Noctyx backup agent (system)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "Nice=10\n"
        "IOSchedulingClass=best-effort\n"
        "IOSchedulingPriority=7\n"
        f"ExecStart={paths['agent_bin']} backup --profile system\n"
        "TimeoutStartSec=3d\n"
    )


def build_agent_deb(
    out_path: str | pathlib.Path,
    version: str = __version__,
    paths: dict | None = None,
    scoped_sudo_user: str | None = None,
    allow_reboot: bool = False,
    allow_recipes: bool = False,
    mtime: int | None = None,
) -> pathlib.Path:
    """Build `noctyx-agent_<version>_all.deb` at `out_path` and return the output Path."""
    p = {**DEFAULTS, **(paths or {})}
    ts = int(time.time()) if mtime is None else int(mtime)

    agent_sh = render((NX_HOME / "agent" / "noctyx-agent.sh").read_text(), p).encode("utf-8")
    dumps_py = render((NX_HOME / "agent" / "noctyx-dumps.py").read_text(), p).encode("utf-8")
    excl_conf = (NX_HOME / "agent" / "exclusions-common.conf").read_text().encode("utf-8")
    needrestart = render((NX_HOME / "agent" / "needrestart.conf").read_text(), p).encode("utf-8")
    unit_txt = _service_unit(p).encode("utf-8")

    data_entries: list[tuple[str, bytes, int]] = [
        (p["agent_bin"].lstrip("/"), agent_sh, 0o755),
        (p["dumps_bin"].lstrip("/"), dumps_py, 0o755),
        (f"{p['node_etc'].lstrip('/')}/exclusions-common.conf", excl_conf, 0o644),
        (f"etc/needrestart/conf.d/{p['unit_prefix']}.conf", needrestart, 0o644),
        (f"etc/systemd/system/{p['agent_unit']}.service", unit_txt, 0o644),
    ]
    conffiles = [
        f"/{p['node_etc'].lstrip('/')}/exclusions-common.conf",
        f"/etc/needrestart/conf.d/{p['unit_prefix']}.conf",
    ]
    if scoped_sudo_user:
        from noctyx import sudoers
        sud_txt = sudoers.render_sudoers(
            scoped_sudo_user, paths=p, allow_reboot=allow_reboot, allow_recipes=allow_recipes
        ).encode("utf-8")
        data_entries.append(("etc/sudoers.d/noctyx", sud_txt, 0o440))
        conffiles.append("/etc/sudoers.d/noctyx")

    installed_kb = max(1, (sum(len(c) for _, c, _ in data_entries) + 1023) // 1024)
    control_txt = (
        f"Package: noctyx-agent\n"
        f"Version: {version}\n"
        f"Section: admin\n"
        f"Priority: optional\n"
        f"Architecture: all\n"
        f"Maintainer: Noctyx Project <noctyx@users.noreply.github.com>\n"
        f"Depends: bash, coreutils, restic, python3\n"
        f"Recommends: psmisc, rsync\n"
        f"Installed-Size: {installed_kb}\n"
        f"Homepage: https://github.com/BobClawblaw/noctyx\n"
        f"Description: Noctyx Linux node backup and telemetry agent\n"
        f" Installs the Noctyx restic backup agent, database dump hook, common\n"
        f" exclusions, needrestart guard, and systemd backup service.\n"
    ).encode("utf-8")

    postinst_txt = (
        "#!/bin/sh\n"
        "set -e\n"
        f"mkdir -p {p['node_etc']}/hooks/pre.d {p['node_etc']}/hooks/post.d {p['node_lib']}\n"
        f"chmod 0750 {p['node_etc']}/hooks\n"
        "if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then\n"
        "  systemctl daemon-reload || true\n"
        "fi\n"
    ).encode("utf-8")

    control_entries = [
        ("control", control_txt, 0o644),
        ("postinst", postinst_txt, 0o755),
        ("conffiles", ("\n".join(conffiles) + "\n").encode("utf-8"), 0o644),
    ]

    control_tar = _tar_gz(control_entries, ts)
    data_tar = _tar_gz(data_entries, ts)

    deb_bytes = (
        b"!<arch>\n"
        + _ar_member("debian-binary", b"2.0\n", ts)
        + _ar_member("control.tar.gz", control_tar, ts)
        + _ar_member("data.tar.gz", data_tar, ts)
    )
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(deb_bytes)
    return out
