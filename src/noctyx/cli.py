"""noctyx — the night-shift operator for your AI cluster.

  noctyx console              the web console (head node)
  noctyx directory-service    discovery + read-only inventory for restore images
  noctyx mirror               the Hugging Face mirror server (HF_ENDPOINT)
  noctyx llm-discover         find LLM servers on the fleet (timer)
  noctyx mtplx-exporter       Apple-silicon LLM server metrics for Prometheus
  noctyx tool NAME [ARGS]     a setup script rendered for this site (install-node-metrics, install-mac-metrics,
                              install-alerting, install-llm-monitoring)
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

TOOLS = ("install-node-metrics", "install-mac-metrics", "install-alerting", "install-llm-monitoring")


def tool(name, args):
    """Render the metrics directory for this site into a temp dir and run one of its scripts there."""
    from .paths import P, CONFIG, render
    if name not in TOOLS:
        sys.exit(f"unknown tool {name!r} (tools: {', '.join(TOOLS)})")
    src = pathlib.Path(__file__).resolve().parent / "metrics"
    extra = {"NX_PKG_PARENT": str(src.parents[1]), "NX_CONFIG": CONFIG, "NX_PY": sys.executable}
    with tempfile.TemporaryDirectory(prefix="noctyx-tool-") as td:
        for f in src.iterdir():
            if f.is_file() and not f.name.endswith(".py"):
                text = render(f.read_text())
                for k, v in extra.items():
                    text = text.replace(f"@{k}@", v)
                (pathlib.Path(td) / f.name).write_text(text)
                shutil.copymode(f, pathlib.Path(td) / f.name)
        env = {**os.environ, "NOCTYX_CONFIG": CONFIG, "PYTHONPATH": extra["NX_PKG_PARENT"], "NX_PY": sys.executable}
        return subprocess.call([str(pathlib.Path(td) / f"{name}.sh"), *args], env=env)


def main(argv=None):
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
