"""noctyx — the night-shift operator for your AI cluster.

  noctyx init                 first-time setup: the assistant first, then a site config from questions
                              (--web: the same in a browser, the assistant proposing each step)
  noctyx setup [STEP|all|verify]  set the head up step by step, and prove it works end to end
  noctyx ask QUESTION         the setup assistant (the site's LLM), grounded in the install guide
  noctyx console              the web console (head node)
  noctyx directory-service    discovery + read-only inventory for restore images
  noctyx mirror               the Hugging Face mirror server (HF_ENDPOINT)
  noctyx llm-discover         find LLM servers on the fleet (timer)
  noctyx mtplx-exporter       Apple-silicon LLM server metrics for Prometheus
  noctyx tool NAME [ARGS]     a script rendered for this site: install-node-metrics, install-mac-metrics,
                              install-alerting, install-llm-monitoring; recovery: build-stick, build-stick-single,
                              make-restore-usb, vm-boot-test, rehearse-head
  noctyx demo [--port N]      the console on sample data (a scrubbed real site), read-only, http://127.0.0.1:N
  noctyx paths                the effective paths for this site (paths.py + the site config's "paths")
  noctyx banner               the banner
  noctyx <command> …          everything else: backup, tick, recipe-deploy, hf-harvest, … (`noctyx -h`)
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

# name -> (package dir, script): run with this site's paths and values filled in
TOOLS = {
    "install-node-metrics": ("metrics", "install-node-metrics.sh"),
    "install-mac-metrics": ("metrics", "install-mac-metrics.sh"),
    "install-alerting": ("metrics", "install-alerting.sh"),
    "install-llm-monitoring": ("metrics", "install-llm-monitoring.sh"),
    "build-stick": ("restore", "build-stick.sh"),               # dual-arch recovery stick (x86_64 + arm64)
    "build-stick-single": ("restore", "build-stick-single.sh"), # one-arch stick; --fetch-only caches the ISOs
    "make-restore-usb": ("restore", "make-restore-usb.sh"),     # payload (keys, engine, menu, restic) onto a FAT partition
    "vm-boot-test": ("restore", "vm-boot-test.sh"),             # boot recovery media in a UEFI VM
    "rehearse-head": ("restore", "rehearse-head.sh"),           # rebuild the head node from its snapshot in a VM
}


def tool(name, args):
    """Render one of the package's script directories for this site into a temp dir and run the script there
    (the scripts call their siblings, so the whole directory is rendered)."""
    from .paths import CONFIG
    from . import orchestrator as cm
    if name not in TOOLS:
        sys.exit(f"unknown tool {name!r} (tools: {', '.join(TOOLS)})")
    sub, script = TOOLS[name]
    src = pathlib.Path(__file__).resolve().parent / sub
    c = cm.cfg()
    with tempfile.TemporaryDirectory(prefix="noctyx-tool-") as td:
        for f in src.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts and not (sub == "metrics" and f.suffix == ".py"):
                out = pathlib.Path(td) / f.relative_to(src)
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    out.write_text(cm.render_site(c, f.read_text()))
                except UnicodeDecodeError:
                    out.write_bytes(f.read_bytes())
                shutil.copymode(f, out)
        os.chmod(td, 0o755)
        env = {**os.environ, "NOCTYX_CONFIG": CONFIG, "PYTHONPATH": str(src.parents[1]), "NX_PY": sys.executable}
        return subprocess.call(["bash", str(pathlib.Path(td) / script), *args], env=env)


def _path():
    """Where Noctyx installs restic and rest-server, and where a Mac keeps Homebrew's tools: on PATH even under a
    sudo whose secure_path leaves them out (macOS's does), so every step finds what `setup tools` installed."""
    have = os.environ.get("PATH", "").split(os.pathsep)
    extra = [d for d in ("/usr/local/bin", "/usr/local/sbin", *(("/opt/homebrew/bin",) if sys.platform == "darwin" else ()))
             if d not in have and os.path.isdir(d)]
    if extra:
        os.environ["PATH"] = os.pathsep.join([*have, *extra])


def main(argv=None):
    _path()
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else None
    if cmd in (None, "banner"):
        from .banner import render
        print(render())
        if cmd is None:
            print("\n" + __doc__)
        return 0
    if cmd in ("--version", "version"):
        from . import __version__
        print(f"noctyx {__version__}")
        return 0
    if cmd == "paths":
        from .paths import P, CONFIG
        print(json.dumps({"config": CONFIG, **P}, indent=1))
        return 0
    if cmd == "demo":
        port = argv[argv.index("--port") + 1] if "--port" in argv else "20993"
        here = pathlib.Path(__file__).resolve().parent / "demo"
        scratch = tempfile.mkdtemp(prefix="noctyx-demo-")
        os.environ.update({"NOCTYX_CONFIG": str(here / "site.json"), "NOCTYX_DEMO": str(here / "fixtures.json"),
                           "NOCTYX_CONSOLE_PORT": port, "NOCTYX_CONSOLE_AUTH": "0"})
        for k in ("etc", "state", "locks", "job_logs", "console_state"):      # never the real site's files
            os.environ[f"NOCTYX_PATH_{k.upper()}"] = f"{scratch}/{k}"
        os.environ["NOCTYX_PATH_AUDIT"] = f"{scratch}/audit.jsonl"
        os.environ["NOCTYX_PATH_UI_TOKEN"] = f"{scratch}/ui-token"
        for k in ("etc", "state", "console_state"):
            os.makedirs(f"{scratch}/{k}", exist_ok=True)
        sys.argv = ["noctyx"]
        from . import console
        return console.main()
    if cmd == "init":
        from . import firstrun
        return firstrun.main(argv[1:])
    if cmd == "ask":
        from . import llm
        return llm.cmd_ask(argv[1:])
    if cmd == "tool":
        if len(argv) < 2:
            sys.exit(f"usage: noctyx tool NAME [ARGS]  (tools: {', '.join(TOOLS)})")
        return tool(argv[1], argv[2:])
    sys.argv = ["noctyx", *argv[1:]]
    if cmd == "console":
        from . import console
        return console.main()
    if cmd == "directory-service":
        from . import directory
        return directory.main()
    if cmd == "mirror":
        from .cache import mirror
        return mirror.main()
    if cmd == "llm-discover":
        from .metrics import llm_discover
        return llm_discover.main()
    if cmd == "mtplx-exporter":
        from .metrics import mtplx_exporter
        return mtplx_exporter.main()
    sys.argv = ["noctyx", *argv]
    from . import orchestrator
    return orchestrator.main()


if __name__ == "__main__":
    sys.exit(main())
