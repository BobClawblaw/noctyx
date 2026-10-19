import subprocess
import sys

import noctyx
from noctyx import cli
from noctyx.banner import render


def test_version():
    assert noctyx.__version__


def test_banner_and_help(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "noctyx console" in out and "██" in out


def test_banner_plain_has_no_escapes():
    assert "\033[" not in render(color=False)


def test_orchestrator_cli_parses(capsys):
    try:
        cli.main(["-h"])
    except SystemExit as e:
        assert e.code == 0
    assert "recipe-deploy" in capsys.readouterr().out


def test_privacy_check_passes():
    r = subprocess.run([sys.executable, "tools/privacy_check.py"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
