import subprocess
import sys

import noctyx
from noctyx import cli


def test_version():
    assert noctyx.__version__


def test_cli_help(capsys):
    assert cli.main([]) == 0
    assert "noctyx" in capsys.readouterr().out


def test_privacy_check_passes():
    r = subprocess.run([sys.executable, "tools/privacy_check.py"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
