import json
import os
import sys
import pathlib
import tempfile

import pytest

SITE = {
    "server": {"hostname": "head", "address": "192.0.2.10", "ui_port": 20996, "rest_port": 20998,
               "directory_port": 20999, "discovery_udp_port": 20997,
               "repo_root": "/mnt/nas/backups",
               "nas_ssh": {"user": "backup", "host": "192.0.2.5", "dir": "noctyx-rest", "repo_root": "/volume1/backups/noctyx"}, "ssh": {"user": "ops", "key": "/root/.ssh/id_ed25519"},
               "tls": {"cert": "/x/server.crt", "key": "/x/server.key", "ca": "/x/ca.crt"}},
    "hosts": [{"name": "gpu-01", "address": "192.0.2.21", "arch": "x86_64", "repo": "gpu-01"}],
    "hf_mirror": {"store": "/mnt/nas/hf-mirror", "port": 20995},
    "common_exclusions_extra": ["/data/**"],
    "paths": {"agent_bin": "/usr/local/bin/legacy-agent", "result_token": "LEGACY_RESULT", "node_etc": "/etc/legacy"},
}


# set before any test module imports noctyx: paths.CONFIG is read at import
_SITE_FILE = pathlib.Path(tempfile.mkdtemp(prefix="noctyx-test-")) / "site.json"
_SITE_FILE.write_text(json.dumps(SITE))
os.environ["NOCTYX_CONFIG"] = str(_SITE_FILE)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(scope="session")
def site_config():
    """Every test runs against a throwaway example site (documentation addresses only)."""
    return _SITE_FILE
