"""What a newer release of a component changes, read from its release notes by the site's assistant.

`noctyx versions` says a newer Prometheus, Alertmanager or node_exporter exists; whether to take it is a
person's call, and the first thing they would do is read the release notes. So for each such release Noctyx
fetches the notes from the project's GitHub release once, has the assistant say in a few lines what could matter
to a site that runs the component the way Noctyx sets it up, and keeps that with a link (<state>/release-notes.json).
The row's alert and the Machine Report show it beside the take and skip buttons, and `noctyx versions` prints it.

It is a summary, written by a model, of text the project published: the link is there to read the notes
themselves. With no assistant, the row gets the link and the notes' first lines.
"""
import json
import os
import pathlib
import time
import urllib.request

from noctyx.paths import P

FILE = pathlib.Path(P["state"]) / "release-notes.json"
KEEP = 40                      # entries
# how Noctyx runs each, so the summary can say what applies here
HOW = {
    "prometheus": "one Prometheus on the head node: local TSDB with 30 days' retention, file_sd targets, static "
                  "alerting and recording rules loaded from a rules file, one Alertmanager, no remote write, no agent "
                  "mode, no federation; started by systemd with command-line flags",
    "alertmanager": "one Alertmanager on the head node, not clustered: a webhook receiver to the Noctyx console and "
                    "optionally one e-mail or chat receiver; silences made through its v2 API",
    "node_exporter": "node_exporter on every Linux node with the default collectors plus the textfile and systemd "
                     "collectors, scraped every 15 s; flags come from a systemd drop-in",
}
SYSTEM = """You brief the operator of a small self-hosted cluster on a software release, from its release notes. \
In at most 5 short lines: first, anything that could break or change behaviour for the setup described (removed or \
renamed flags, config or rule syntax, storage format, changed defaults, a minimum version to upgrade from); then \
security fixes; then, in one line, what else is in it. If nothing in the notes touches the setup described, say so \
plainly in one line. Use only the notes; do not guess at changes they do not mention. No preamble, no headings."""


def load():
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "noctyx", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def fetch(repo, version, get=None):
    """A release's notes from GitHub -> {"body", "url", "published"}; raises when there is no such release."""
    d = (get or _get)(f"https://api.github.com/repos/{repo}/releases/tags/v{version.lstrip('v')}")
    return {"body": (d.get("body") or "").strip(), "url": d.get("html_url") or f"https://github.com/{repo}/releases/tag/v{version}",
            "published": (d.get("published_at") or "")[:10]}


def key(component, version):
    return f"{component}@{version.lstrip('v')}"


def note(component, repo, installed, version, get=None, ask=None, cache=None):
    """The kept note for this release, made now if there is none -> {"summary", "model", "url", "published",
    "excerpt", "ts"}, or None when the notes cannot be fetched (tried again next time)."""
    from noctyx import llm
    cache = load() if cache is None else cache
    k = key(component, version)
    if k in cache and (cache[k].get("summary") or not llm.configured()):
        return cache[k]
    try:
        rel = fetch(repo, version, get)
    except Exception:
        return cache.get(k)
    ent = {"url": rel["url"], "published": rel["published"], "excerpt": rel["body"][:400], "summary": None, "model": None,
           "ts": time.time()}
    a = llm.configured()
    if a and rel["body"] and not os.environ.get("NOCTYX_NO_ASSIST"):
        user = (f"Component: {component}. Running now: {installed or 'unknown'}. The release: {version}.\n"
                f"The setup: {HOW.get(component, 'as its project documents it')}.\n\nRelease notes:\n{rel['body'][:14000]}")
        try:
            text, model = (ask or llm.ask_any)(a, SYSTEM, user, max_tokens=300)
        except Exception:
            text, model = None, None
        if text:
            ent.update(summary=text.strip()[:1200], model=model)
    cache[k] = ent
    return ent


def refresh(inv, repos, get=None, ask=None):
    """Notes for every release the inventory offers as an upgrade (state `upgrade`, a component with a repo), kept
    in FILE. inv: `noctyx versions`' inventory; repos: {component: "owner/name"}. -> the notes by key."""
    cache = load()
    want = {}
    for rows in (inv.get("hosts") or {}).values():
        for r in rows:
            if r.get("state") == "upgrade" and r.get("latest") and repos.get(r["component"]):
                want[key(r["component"], r["latest"])] = (r["component"], r.get("installed"), r["latest"])
    for k, (component, installed, version) in want.items():
        note(component, repos[component], installed, version, get, ask, cache)
    keep = dict(sorted(cache.items(), key=lambda kv: -kv[1].get("ts", 0))[:KEEP])
    if keep != load():
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, indent=1))
        os.chmod(tmp, 0o644)
        os.replace(tmp, FILE)
    return {k: keep[k] for k in want if k in keep}


def attach(inv):
    """Put each kept note on the rows it is about (`notes`), for the console and the command line."""
    cache = load()
    for rows in (inv.get("hosts") or {}).values():
        for r in rows:
            n = cache.get(key(r["component"], r["latest"])) if r.get("latest") and r.get("state") in ("upgrade", "skipped") else None
            if n:
                r["notes"] = {x: n.get(x) for x in ("summary", "model", "url", "published", "excerpt")}
    return inv
