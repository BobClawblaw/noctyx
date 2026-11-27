"""Tests for the pure-stdlib Debian (.deb) node agent packager."""
import io
import shutil
import subprocess
import tarfile

from noctyx import debpkg


def _parse_ar(data: bytes) -> dict[str, bytes]:
    assert data[:8] == b"!<arch>\n"
    pos = 8
    out = {}
    while pos < len(data):
        hdr = data[pos:pos + 60]
        assert len(hdr) == 60 and hdr[58:60] == b"`\n"
        name = hdr[:16].decode("ascii").strip().rstrip("/")
        size = int(hdr[48:58].decode("ascii").strip())
        pos += 60
        out[name] = data[pos:pos + size]
        pos += size + (size % 2)
    return out


def test_build_agent_deb_structure_and_permissions(tmp_path):
    deb = debpkg.build_agent_deb(
        tmp_path / "noctyx-agent_0.9.5_all.deb",
        version="0.9.5",
        scoped_sudo_user="ops",
        allow_reboot=True,
        allow_recipes=True,
        mtime=1700000000,
    )
    assert deb.is_file()
    members = _parse_ar(deb.read_bytes())
    assert list(members.keys()) == ["debian-binary", "control.tar.gz", "data.tar.gz"]
    assert members["debian-binary"] == b"2.0\n"

    with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as ctf:
        cnames = {m.name: m for m in ctf.getmembers()}
        assert "./control" in cnames and "./postinst" in cnames and "./conffiles" in cnames
        assert cnames["./postinst"].mode == 0o755
        ctrl = ctf.extractfile("./control").read().decode()
        assert "Package: noctyx-agent\n" in ctrl and "Version: 0.9.5\n" in ctrl
        conffiles = ctf.extractfile("./conffiles").read().decode()
        assert "/etc/sudoers.d/noctyx" in conffiles

    with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as dtf:
        dmembers = {m.name: m for m in dtf.getmembers()}
        assert dmembers["./usr/local/bin/noctyx-agent"].mode == 0o755
        assert dmembers["./usr/local/bin/noctyx-dumps"].mode == 0o755
        assert dmembers["./etc/noctyx/exclusions-common.conf"].mode == 0o644
        assert dmembers["./etc/sudoers.d/noctyx"].mode == 0o440
        assert dmembers["./etc/systemd/system/noctyx-backup.service"].mode == 0o644
        assert all(m.uid == 0 and m.gid == 0 for m in dmembers.values())
        agent_txt = dtf.extractfile("./usr/local/bin/noctyx-agent").read().decode()
        assert "@NODE_ETC@" not in agent_txt and "@RESULT_TOKEN@" not in agent_txt

    if shutil.which("dpkg-deb"):
        r1 = subprocess.run(["dpkg-deb", "--info", str(deb)], capture_output=True, text=True)
        assert r1.returncode == 0 and "Package: noctyx-agent" in r1.stdout
        r2 = subprocess.run(["dpkg-deb", "--contents", str(deb)], capture_output=True, text=True)
        assert r2.returncode == 0 and "./usr/local/bin/noctyx-agent" in r2.stdout
