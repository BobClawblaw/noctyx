"""The assistant's LLM link, for everything outside the console: finding an endpoint, testing it, and asking it
for help while a site is being set up.

The assistant is any OpenAI-compatible server (vLLM, SGLang, llama.cpp, MTPLX, TensorFold, Ollama, LM Studio).
`noctyx init` configures it first, so every later step that fails can ask it what went wrong.
"""
import concurrent.futures
import json
import os
import pathlib
import platform
import sys
import time
import urllib.error
import urllib.request

from .paths import P, CONFIG

# where OpenAI-compatible servers usually listen: recipe slots, vLLM/SGLang, llama.cpp, MTPLX, Ollama, LM Studio
COMMON_PORTS = (30000, 8000, 8080, 9502, 9503, 9501, 11434, 1234, 5000)

SETUP_PROMPT = """You are the setup assistant of Noctyx, a self-hosted operator for small AI clusters (a head node \
running a web console, a directory service, a Hugging Face mirror and restic backups to a NAS or a local disk, \
plus agents on GPU nodes). A person is installing it for the first time and a step just failed. Using the \
install guide below and the facts you are given, say in plain words what most likely went wrong and give the \
exact commands that fix it, for this platform. Be brief: at most 8 lines, commands in backticks. If the facts \
are not enough, name the one command whose output would tell. Never suggest turning off a firewall, TLS \
verification or any other protection, and never suggest installing tools Noctyx installs itself (restic and \
rest-server come from `noctyx setup tools`)."""


def _key(name):
    try:
        return pathlib.Path(P["llm_keys"], name).read_text().strip() if name else None
    except OSError:
        return None


def _hdr(key):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def models(url, key=None, timeout=5):
    """The model ids an endpoint serves; [] if it answers but lists none. Raises if it does not answer."""
    req = urllib.request.Request(url.rstrip("/") + "/models", headers=_hdr(key))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return [m.get("id") for m in d.get("data", []) if isinstance(m, dict) and m.get("id")]


def chat(url, messages, model=None, key=None, timeout=60, max_tokens=400, temperature=0.2):
    """-> (text, model). Thinking off where the server supports the switch: setup help wants an answer, not a
    long reasoning trace. An empty model means the endpoint's first."""
    model = model or (models(url, key, timeout=10) or [""])[0]
    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}, "messages": messages}
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), headers=_hdr(key))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    m = (d.get("choices") or [{}])[0].get("message") or {}
    return (m.get("content") or m.get("reasoning_content") or "").strip(), d.get("model") or model


def test(url, model=None, key=None, timeout=60):
    """One short completion: {ok, text, model, ms, models} or {ok: False, error}."""
    out = {"ok": False, "url": url}
    try:
        out["models"] = models(url, key, timeout=10)
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code} from /models" + (": it wants an API key" if e.code in (401, 403) else "")
        return out
    except Exception as e:
        out["error"] = f"no answer from {url}/models: {getattr(e, 'reason', e)}"
        return out
    if not out["models"] and not model:
        out["error"] = "the endpoint answers but serves no model"
        return out
    t0 = time.time()
    try:
        text, used = chat(url, [{"role": "user", "content": "Reply with exactly: Noctyx assistant OK"}],
                          model=model or out["models"][0], key=key, timeout=timeout, max_tokens=24, temperature=0)
        out.update(ok=bool(text), text=text[:200], model=used, ms=round((time.time() - t0) * 1000))
        if not text:
            out["error"] = "empty answer"
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}: {e.read()[:160].decode(errors='replace')}"
    except Exception as e:
        out["error"] = str(getattr(e, "reason", e))[:160]
    return out


def discover(addresses=("127.0.0.1",), ports=COMMON_PORTS, timeout=1.5):
    """Every address:port that answers /v1/models with at least one model -> [{url, models}], fastest first."""
    def one(ap):
        url = f"http://{ap[0]}:{ap[1]}/v1"
        try:
            t0 = time.time()
            ms = models(url, timeout=timeout)
            return {"url": url, "models": ms, "ms": round((time.time() - t0) * 1000)} if ms else None
        except Exception:
            return None
    todo = [(a, p) for a in dict.fromkeys(addresses) for p in ports]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(todo) or 1)) as ex:
        found = [r for r in ex.map(one, todo) if r]
    return sorted(found, key=lambda r: r["ms"])


def configured(config=CONFIG):
    """The site's assistant block, or None when there is no config yet or it is off."""
    try:
        a = json.loads(pathlib.Path(config).read_text()).get("assistant") or {}
    except (OSError, ValueError):
        return None
    return a if a.get("enabled") and a.get("url") else None


GUIDE_URL = "https://raw.githubusercontent.com/BobClawblaw/noctyx/main/docs/guide/{}"


def _guide_page(name):
    """One guide page: from the checkout, else the published copy (the wheel ships no docs), cached a day."""
    try:
        return (pathlib.Path(__file__).resolve().parents[2] / "docs" / "guide" / name).read_text()
    except OSError:
        pass
    cache = pathlib.Path(P["console_state"]) / "guide" / name
    try:
        if time.time() - cache.stat().st_mtime < 86400:
            return cache.read_text()
    except OSError:
        pass
    try:
        with urllib.request.urlopen(GUIDE_URL.format(name), timeout=10) as r:
            text = r.read().decode("utf-8", "replace")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
        return text
    except Exception:
        try:
            return cache.read_text()          # stale beats none
        except OSError:
            return ""


def _guide():
    """The install and troubleshooting guides the assistant answers from."""
    return "\n\n".join(t[:24000] for t in (_guide_page("install.md"), _guide_page("troubleshooting.md")) if t)


def facts():
    if sys.platform == "darwin":
        svc = (f"macOS: Noctyx's services are launchd daemons /Library/LaunchDaemons/{P['launchd_prefix']}.<name>.plist "
               f"(launchctl print|kickstart -k system/{P['launchd_prefix']}.<name>), logs in /var/log/noctyx/; "
               "there is no systemctl or journalctl")
    else:
        svc = f"Linux: Noctyx's services are systemd units {P['unit_prefix']}-<name>.service (journalctl -u for logs)"
    return (f"platform: {platform.system()} {platform.release()} ({platform.machine()}); python {sys.version.split()[0]}; "
            f"euid {os.geteuid()}; config {CONFIG} ({'present' if os.path.exists(CONFIG) else 'missing'}); "
            f"restic/rest-server in /usr/local/bin; {svc}")


def ask(question, system=None, max_tokens=500, a=None):
    """-> (text, model) from the site's assistant, or (None, why)."""
    a = a or configured()
    if not a:
        return None, "no assistant configured (noctyx init sets one up)"
    try:
        return chat(a["url"], [{"role": "system", "content": system or SETUP_PROMPT},
                               {"role": "user", "content": question}],
                    model=a.get("model") or None, key=_key(a.get("key_host")),
                    timeout=int(a.get("timeout_seconds", 60)), max_tokens=max_tokens)
    except Exception as e:
        return None, str(getattr(e, "reason", e))[:160]


def setup_help(step, error, context=""):
    """Ask the assistant about a failed setup step and print its answer. Silent when there is none: a failed
    step must never fail harder because the LLM is down."""
    if os.environ.get("NOCTYX_NO_ASSIST") or not configured():
        return
    guide = _guide()
    q = (f"Step: noctyx {step}\nError:\n{error.strip()[-3000:]}\n\nFacts: {facts()}\n{context}".strip()
         + (f"\n\n--- install guide ---\n{guide}" if guide else ""))
    print("\n  asking the assistant…", flush=True)
    text, model = ask(q)
    if text:
        print(f"\n  ┌ assistant ({model})")
        for line in text.splitlines():
            print(f"  │ {line}")
        print("  └ (NOCTYX_NO_ASSIST=1 turns this off)")
    else:
        print(f"  (the assistant could not answer: {model})")


def cmd_ask(argv):
    """noctyx ask QUESTION: the setup assistant, grounded in the install guide and this machine's facts."""
    q = " ".join(argv).strip() or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    if not q:
        sys.exit("usage: noctyx ask QUESTION   (or pipe text in, e.g. an error log)")
    guide = _guide()
    text, model = ask(f"{q}\n\nFacts: {facts()}" + (f"\n\n--- install guide ---\n{guide}" if guide else ""),
                      system=SETUP_PROMPT.replace("and a step just failed", "and has a question"), max_tokens=800)
    if not text:
        sys.exit(f"no answer: {model}")
    print(text)
    print(f"\n— {model}")
    return 0
