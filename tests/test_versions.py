"""A component alert can be acted on: Noctyx's own upgrade where it installs the component, a reason where it
does not, and skipping a release resolves the alert until a later one appears. A site can move its own pin of
what `noctyx upgrade` installs ahead of Noctyx's, with the release's own sums."""
import json

import pytest

from noctyx import components as comp

NODE = {"name": "gpu-01", "address": "192.0.2.21", "arch": "aarch64", "repo": "gpu-01"}
MAC = {"name": "mac-01", "address": "192.0.2.30", "os": "macos", "backup": False}


def _row(component, installed, source, state="upgrade", latest=None):
    return {"component": component, "installed": installed, "source": source, "state": state,
            "latest": latest or comp.PINS.get(component, {}).get("version"), "detail": ""}


def test_the_distro_exporter_is_replaced_by_the_pin():
    f = comp.fix(_row("node_exporter", "1.7.0", "distro"), NODE, False)
    assert f["change"] == "node_exporter" and f["argv"] == ["upgrade", "--host", "gpu-01", "--component", "node_exporter"]
    assert "1.12.1" in f["what"] and "rolled back" in f["what"]
    head = comp.fix(_row("node_exporter", "1.7.0", "distro"), {"name": "head", "local": True}, True)
    assert head["change"] == "node_exporter" and head["argv"][2] == "head"


def test_what_noctyx_does_not_install_says_why():
    assert "Homebrew" in comp.fix(_row("node_exporter", "1.7.0", "brew"), MAC, False)["why"]
    assert "pip" in comp.fix(_row("noctyx", "0.1.0", "noctyx", latest="0.2.0"), {"name": "head"}, True)["why"]
    pinned = _row("node_exporter", comp.PINS["node_exporter"]["version"], "noctyx", latest="99.0.0")
    assert "versions-pin --component node_exporter" in comp.fix(pinned, NODE, False)["why"]   # newer than the pin
    held = _row("rest-server", comp.PINS["rest-server"]["version"], "noctyx", latest="99.0.0")
    assert "moves the pin" in comp.fix(held, {"name": "head"}, True)["why"]  # not one a site pins itself
    assert comp.fix(_row("node_exporter", "1.12.1", "noctyx", state="ok"), NODE, False) is None


def test_the_agent_and_restic_reinstall():
    assert comp.fix(_row("agent", "abc", "noctyx", state="behind"), NODE, False)["argv"] == ["install-agent", "--host", "gpu-01"]
    assert comp.fix(_row("restic", "0.17.0", "noctyx", state="behind"), NODE, False)["argv"] == ["install-agent", "--host", "gpu-01"]
    assert comp.fix(_row("restic", "0.17.0", "noctyx", state="behind"), {"name": "head"}, True)["argv"] == ["setup", "tools", "--force"]


def test_a_skipped_release_is_quiet_until_a_later_one():
    inv = {"hosts": {"gpu-01": [_row("node_exporter", "1.7.0", "distro", latest="1.12.1")],
                     "gpu-02": [_row("node_exporter", "1.7.0", "distro", latest="1.12.1")]}}
    comp.apply_skips(inv, {"gpu-01/node_exporter": {"release": "1.12.1", "by": "ana"}})
    assert inv["hosts"]["gpu-01"][0]["state"] == "skipped" and "ana" in inv["hosts"]["gpu-01"][0]["detail"]
    assert inv["hosts"]["gpu-02"][0]["state"] == "upgrade"                 # one host's skip is that host's
    later = {"hosts": {"gpu-01": [_row("node_exporter", "1.7.0", "distro", latest="1.13.0")]}}
    comp.apply_skips(later, {"*/node_exporter": {"release": "1.12.1"}})
    assert later["hosts"]["gpu-01"][0]["state"] == "upgrade"               # a later release alerts again
    every = {"hosts": {"gpu-02": [_row("node_exporter", "1.7.0", "distro", latest="1.12.1")]}}
    assert comp.apply_skips(every, {"*/node_exporter": {"release": "v1.12.1"}})["hosts"]["gpu-02"][0]["state"] == "skipped"


# ── a site's own pins ──────────────────────────────────────────────────────
def _sums_file(name, v, drop=None):
    p = comp.PINS[name]
    lines = []
    for i, pl in enumerate(comp.PLATFORMS):
        o, a = pl.split("_")
        f = p["asset"].format(v=v, os=o, arch=a)
        if f != drop:
            lines.append(f"{i:064x}  {f}")
    return "\n".join(lines + ["f" * 64 + "  something-else.zip"])


@pytest.fixture()
def pins(tmp_path, monkeypatch):
    monkeypatch.setattr(comp, "SITE_PINS_FILE", tmp_path / "version-pins.json")
    yield tmp_path / "version-pins.json"
    comp.apply_site_pins({})                                   # the table's own pins again


def test_release_sums_reads_every_platform_from_the_releases_own_file():
    asked = []
    sums = comp.release_sums("prometheus", "v9.9.9", fetch=lambda u: asked.append(u) or _sums_file("prometheus", "9.9.9"))
    assert asked == ["https://github.com/prometheus/prometheus/releases/download/v9.9.9/sha256sums.txt"]
    assert sums == {pl: f"{i:064x}" for i, pl in enumerate(comp.PLATFORMS)}
    with pytest.raises(ValueError, match="does not list prometheus-9.9.9.linux-arm64.tar.gz"):
        comp.release_sums("prometheus", "9.9.9",
                          fetch=lambda u: _sums_file("prometheus", "9.9.9", drop="prometheus-9.9.9.linux-arm64.tar.gz"))
    def gone(u):
        raise OSError("HTTP 404")
    with pytest.raises(ValueError, match="no sums file"):
        comp.release_sums("prometheus", "9.9.9", fetch=gone)


def test_a_site_pin_moves_ahead_only_and_with_every_sum(pins):
    own = comp.builtin_pin("prometheus")
    good = {"version": "99.0.0", "sums": {pl: "a" * 64 for pl in comp.PLATFORMS}}
    assert comp.apply_site_pins({"prometheus": good}) == ["prometheus"]
    assert comp.PINS["prometheus"]["version"] == "99.0.0"
    assert comp.asset("prometheus", "linux", "x86_64") == (
        "prometheus-99.0.0.linux-amd64.tar.gz",
        "https://github.com/prometheus/prometheus/releases/download/v99.0.0/prometheus-99.0.0.linux-amd64.tar.gz",
        "a" * 64)
    for bad in ({"version": "0.0.1", "sums": good["sums"]},                        # older than Noctyx's own pin
                {"version": "99.0.0", "sums": {"linux_amd64": "a" * 64}},          # a platform without a sum
                {"version": "99.0.0; rm", "sums": good["sums"]}, {}):
        assert comp.apply_site_pins({"prometheus": bad}) == []
        assert comp.PINS["prometheus"]["version"] == own                          # and the table's sums are back
        assert comp.asset("prometheus", "linux", "x86_64")[2] != "a" * 64
    assert comp.apply_site_pins({"restic": {"version": "99.0.0", "sums": good["sums"]}}) == []   # not pinnable
    assert comp.PINS["restic"]["version"] == comp.builtin_pin("restic")


def test_a_release_newer_than_the_pin_names_the_site_pin_that_takes_it(pins):
    own = comp.builtin_pin("prometheus")
    host = {"name": "head", "local": True}
    row = _row("prometheus", own, "adopted", latest="99.0.0")
    row["state"] = "upgrade"
    fx = comp.fix(row, host, True)
    assert fx["pin"] == "99.0.0" and "versions-pin --component prometheus --version 99.0.0" in fx["why"]
    comp.apply_site_pins({"prometheus": {"version": "99.0.0", "sums": {pl: "a" * 64 for pl in comp.PLATFORMS}}})
    behind = {**row, "state": "behind"}
    assert comp.fix(behind, host, True)["argv"] == ["upgrade", "--host", "head", "--component", "prometheus"]
    assert "pin" not in comp.fix(_row("restic", "0.1.0", "distro", latest="99.0.0") | {"state": "upgrade"}, host, True)


def test_versions_pin_command(pins, monkeypatch, capsys):
    import argparse
    from noctyx import orchestrator as o
    monkeypatch.setattr(o, "AUDIT", pins.parent / "audit.jsonl")
    monkeypatch.setattr(o, "STATE_DIR", pins.parent)
    monkeypatch.setattr(o, "versions", lambda c: {"hosts": {"head": [{"component": "alertmanager", "state": "behind"}]}})
    monkeypatch.setattr(comp, "_fetch", lambda u: _sums_file("alertmanager", "99.1.0"))
    def run(**kw):
        a = argparse.Namespace(component="alertmanager", version=None, undo=False, yes=False)
        a.__dict__.update(kw)
        return o.cmd_versions_pin(a)
    with pytest.raises(SystemExit) as e:
        run(version="99.1.0")
    assert e.value.code == 2 and not pins.exists()                       # without --yes: the sums, nothing kept
    with pytest.raises(SystemExit, match="not newer than Noctyx's own pin"):
        run(version="0.0.1", yes=True)
    with pytest.raises(SystemExit, match="a site can pin"):
        run(component="restic", version="99.0.0", yes=True)
    run(version="99.1.0", yes=True)
    assert json.loads(pins.read_text())["alertmanager"]["version"] == "99.1.0"
    assert comp.PINS["alertmanager"]["version"] == "99.1.0"
    assert "behind on head" in capsys.readouterr().out
    run(component=None)
    assert "this site pins 99.1.0" in capsys.readouterr().out
    run(undo=True)
    assert json.loads(pins.read_text()) == {} and comp.PINS["alertmanager"]["version"] == comp.builtin_pin("alertmanager")
