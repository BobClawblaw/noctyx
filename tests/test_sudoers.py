"""Tests for scoped least-privilege sudoers generation."""
import pytest

from noctyx import sudoers


def test_render_scoped_sudoers_least_privilege():
    txt = sudoers.render_sudoers("ops")
    assert "NOPASSWD: ALL" not in txt
    assert "Defaults:ops !requiretty, env_reset" in txt
    assert "Cmnd_Alias NOCTYX_AGENT =" in txt
    assert f"{sudoers.P['agent_bin']} *" in txt
    assert "ops ALL=(root) NOPASSWD: NOCTYX_AGENT, NOCTYX_INSPECT, NOCTYX_UNITS" in txt
    assert "systemctl reboot" not in txt
    assert "noctyx-recipe-" not in txt
    ok, msg = sudoers.validate_sudoers_text(txt)
    assert ok, msg


def test_render_sudoers_optional_capabilities_and_validation():
    txt = sudoers.render_sudoers("ops", allow_reboot=True, allow_recipes=True)
    assert "systemctl reboot" in txt
    assert "noctyx-recipe-*" in txt
    ok, msg = sudoers.validate_sudoers_text(txt)
    assert ok, msg

    with pytest.raises(ValueError):
        sudoers.render_sudoers("root; rm -rf /")
    with pytest.raises(ValueError):
        sudoers.render_sudoers("ops", paths={"agent_bin": "/usr/local/bin/../evil"})

    bad_ok, _ = sudoers.validate_sudoers_text("ops ALL=(ALL) NOPASSWD: ALL\n")
    assert not bad_ok
