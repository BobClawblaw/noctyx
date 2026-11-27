#!/usr/bin/env python3
"""Local-only noctyx hf-harvest for a mac backing its models into a mounted agentnas hub.

Runs the real noctyx.harvest.hf_harvest_host with this mac as a `local` head, so it discovers
the machine's HF caches + --local-dir downloads and rsyncs missing blobs into agentnas's hub
in HF layout. No SSH, no central: store is the mounted NAS share.
"""
import argparse, getpass, json, os, pathlib, sys

os.environ.setdefault("NOCTYX_HF_STORE", "/Volumes/storage-1/hf-mirror")
sys.path.insert(0, os.path.expanduser("~/github/noctyx/src"))

from noctyx import harvest
from noctyx.harvest import hf_harvest_host

STORE = os.environ["NOCTYX_HF_STORE"]
USER = getpass.getuser()
HERE = os.uname().nodename  # this host, treated as a local head backing into the mounted hub
# Where this mac keeps its models. Override with NOCTYX_HF_SCAN_ROOTS (colon-separated).
SCAN = os.environ.get(
    "NOCTYX_HF_SCAN_ROOTS",
    os.path.expanduser("~/models") + ":" + os.path.expanduser("~/.cache/huggingface/hub"),
).split(":")

c = {
    "server": {"address": "127.0.0.1", "ssh": {"key": "/dev/null", "user": USER}},
    "hf_mirror": {
        "store": STORE,
        "harvest": True,
        "scan_roots": SCAN,
        "bwlimit_mb": 0,
    },
    "hosts": [{"name": HERE, "os": "macos", "local": True, "address": "127.0.0.1", "enabled": True}],
}

a = argparse.Namespace(repo=None, host=None, ignore_window=True)


def main():
    hub = pathlib.Path(STORE) / "hub"
    hub.mkdir(parents=True, exist_ok=True)
    st = hf_harvest_host(c, c["hosts"][0], a, hub)
    print(json.dumps({k: st[k] for k in ("repos", "new_blobs", "new_bytes", "bad_hash",
                                         "unverified", "hub_offline")}, indent=2))
    print("skipped:", st["skipped"])
    print("unresolved:", st["unresolved"])


if __name__ == "__main__":
    main()
