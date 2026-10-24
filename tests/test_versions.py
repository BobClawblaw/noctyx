"""A component alert can be acted on: Noctyx's own upgrade where it installs the component, a reason where it
does not, and skipping a release resolves the alert until a later one appears."""
from noctyx import components as comp

NODE = {"name": "gpu-01", "address": "192.0.2.21", "arch": "aarch64", "repo": "gpu-01"}
MAC = {"name": "mac-01", "address": "192.0.2.30", "os": "macos", "backup": False}


def _row(component, installed, source, state="upgrade", latest=None):
    return {"component": component, "installed": installed, "source": source, "state": state,
            "latest": latest or comp.PINS.get(component, {}).get("version"), "detail": ""}


def test_the_distro_exporter_is_replaced_by_the_pin():
    f = comp.fix(_row("node_exporter", "1.7.0", "distro"), NODE, False)
    assert f["argv"] == ["tool", "install-node-metrics", "--host", "gpu-01"] and "1.12.1" in f["what"]
    head = comp.fix(_row("node_exporter", "1.7.0", "distro"), {"name": "head", "local": True}, True)
    assert head["argv"] == ["tool", "install-node-metrics", "--host", "head"]


def test_what_noctyx_does_not_install_says_why():
    assert "major upgrade" in comp.fix(_row("prometheus", "2.45.3", "distro"), {"name": "head"}, True)["why"]
    assert "Homebrew" in comp.fix(_row("node_exporter", "1.7.0", "brew"), MAC, False)["why"]
    assert "pip" in comp.fix(_row("noctyx", "0.1.0", "noctyx", latest="0.2.0"), {"name": "head"}, True)["why"]
    pinned = _row("node_exporter", comp.PINS["node_exporter"]["version"], "noctyx", latest="99.0.0")
    assert "moves the pin" in comp.fix(pinned, NODE, False)["why"]           # newer upstream than the pin
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
