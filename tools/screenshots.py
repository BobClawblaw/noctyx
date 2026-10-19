#!/usr/bin/env python3
"""Regenerate docs/screenshots/*.png from the demo console (sample data, nothing real).

    pip install websockets         # plus chromium or google-chrome on PATH
    python3 tools/screenshots.py [--only dashboard,kiosk]

Starts `noctyx demo` on a free loopback port, drives headless Chrome over the DevTools protocol through each
shot below, and writes 1500x900 PNGs (2x pixel density) into docs/screenshots/.
"""
import asyncio, base64, json, os, pathlib, shutil, socket, subprocess, sys, tempfile, time, urllib.request

import websockets

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
W, H = 1500, 900

# name: (path, [js steps run after load; "sleep:N" waits], full-page height or None)
SHOTS = {
    "dashboard": ("/", ["sleep:3"], None),
    "cluster": ("/cluster", ["sleep:3"], None),
    "node": ("/", ["sleep:2", "selectHost('spark-1')", "sleep:4"], None),
    "recipes": ("/recipes", ["sleep:3"], None),
    "config": ("/config", ["sleep:3"], None),
    "kiosk": ("/kiosk", ["sleep:4"], None),
    "settings": ("/", ["sleep:2", "document.getElementById('hgear').click()", "sleep:1"], None),
    "menu": ("/", ["sleep:2", "[...document.querySelectorAll('.menu-title')].find(b=>/deploy/i.test(b.textContent))?.click()", "sleep:1"], None),
    "theme-ember-light": ("/", ["NXT.save({theme:'ember',mode:'light'})", "sleep:3"], None),
    "phosphor-dashboard": ("/", ["NXT.save({theme:'phosphor',mode:'dark'})", "location.reload()", "sleep:5"], None),
    "phosphor-node": ("/", ["NXT.save({theme:'phosphor',mode:'dark'})", "location.reload()", "sleep:3",
                            "selectHost('spark-1')", "sleep:4"], None),
    "phosphor-kiosk": ("/kiosk", ["NXT.save({theme:'phosphor',mode:'dark'})", "location.reload()", "sleep:5"], None),
    "about": ("/about", ["sleep:3"], None),
    "login": ("/login", ["sleep:1"], None),
}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def browser():
    for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if shutil.which(b):
            return b
    sys.exit("needs chromium or google-chrome on PATH")


async def shoot(base, dbg, only):
    for _ in range(80):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json")); break
        except OSError:
            time.sleep(0.25)
    ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
    async with websockets.connect(ws_url, max_size=200_000_000) as ws:
        n = 0

        async def call(method, **params):
            nonlocal n; n += 1; me = n
            await ws.send(json.dumps({"id": me, "method": method, "params": params}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == me:
                    return m.get("result", m.get("error"))
        await call("Page.enable")
        await call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=False)
        for name, (path, steps, _) in SHOTS.items():
            if only and name not in only:
                continue
            await call("Page.navigate", url=base + "/about")     # same origin first: reset display settings
            await asyncio.sleep(1)
            await call("Runtime.evaluate", expression="localStorage.clear()")
            await call("Page.navigate", url=base + path)
            await asyncio.sleep(2)
            for js in steps:
                if js.startswith("sleep:"):
                    await asyncio.sleep(float(js[6:])); continue
                await call("Runtime.evaluate", expression=js, awaitPromise=True)
            shot = await call("Page.captureScreenshot", format="png")
            f = OUT / f"{name}.png"
            f.write_bytes(base64.b64decode(shot["data"]))
            try:                                    # 256 colours: a third of the size, no visible change
                from PIL import Image
                Image.open(f).convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT,
                                                      dither=Image.Dither.NONE).save(f, optimize=True)
            except ImportError:
                pass
            print("wrote", (OUT / f"{name}.png").relative_to(ROOT))


def main():
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else set()
    OUT.mkdir(parents=True, exist_ok=True)
    port, dbg = free_port(), free_port()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    demo = subprocess.Popen([sys.executable, "-m", "noctyx.cli", "demo", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    prof = tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix="nx-shots-")     # snap browsers: no hidden dirs
    ch = subprocess.Popen([browser(), "--headless=new", "--no-sandbox", "--hide-scrollbars", f"--remote-debugging-port={dbg}",
                           f"--user-data-dir={prof}", f"--window-size={W},{H}", "about:blank"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/me"); break
            except OSError:
                time.sleep(0.25)
        asyncio.run(shoot(f"http://127.0.0.1:{port}", dbg, only))
    finally:
        ch.terminate(); demo.terminate(); ch.wait(); demo.wait(); shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    main()
