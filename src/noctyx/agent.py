"""The assistant's agent core, shared by the web setup (setupagent.py) and the console's copilot (copilot.py).

An LLM works through a fixed set of tools (the OpenAI tools API), never a shell. Every argument it sends is
checked before anything runs. Read-only tools run as soon as it calls them; a tool that changes something
becomes a proposal the person at the page sees, may edit, and approves or rejects, and only an approved
proposal runs. A tool also names the role it needs (view, operate, admin): the assistant is offered only what
the person it talks to could do themselves. What a tool returns is redacted (keys, tokens, passwords) before
the LLM sees it, and treated as data: a log line can say anything, which is why nothing changes without a
person's approval.
"""
import ipaddress
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.error

from . import llm

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$")
USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
GUIDES = ("install", "troubleshooting", "site-config", "accounts", "assistant", "backups", "console", "upgrade")
RANK = {"view": 0, "operate": 1, "admin": 2}
TOOL_MARKUP = re.compile(r"<tool_call>.*?(</tool_call>|\Z)", re.S)


class Bad(ValueError):
    """An argument a tool refuses: said back to the LLM (or the person at the page) as the tool's result."""


# ── argument checks: everything an LLM sends is checked before it touches anything ──
def _address(v):
    v = str(v or "").strip()
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        if HOSTNAME.match(v):
            return v
    raise Bad(f"not an address or host name: {v!r}")


def _name(v):
    v = str(v or "").strip().lower()
    if not NAME.match(v):
        raise Bad(f"a short name (a-z, 0-9, -): {v!r}")
    return v


def _path(v):
    v = str(v or "").strip()
    if not v.startswith("/") or "/.." in v or any(ch in v for ch in " \t\n'\"`$;&|<>\\*?"):
        raise Bad(f"an absolute path without spaces or shell characters: {v!r}")
    return v.rstrip("/") or "/"


def _port(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise Bad(f"a port number: {v!r}")
    if not 1024 <= v <= 65535 or v in (21000, 21001):
        raise Bad(f"port {v}: use 1024-65535 (and never 21000/21001)")
    return v


def _user(v):
    v = str(v or "").strip()
    if not USER.match(v):
        raise Bad(f"a Unix user name: {v!r}")
    return v


def _text(v, n=200):
    v = str(v or "").strip()
    if len(v) > n or any(ch in v for ch in "\n\r\0"):
        raise Bad("text too long or with line breaks")
    return v


def _int(v, lo, hi, default):
    if v in (None, ""):
        return default
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise Bad(f"a whole number: {v!r}")
    return max(lo, min(hi, v))


# ── what a tool returns, before the LLM sees it ──
_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[private key]"),
    (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)((?:pass(?:word|wd|phrase)?|secret|token|api[_-]?key|auth[_-]?key|access[_-]?key|private[_-]?key)"
                r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+"), r"\1[redacted]"),
    (re.compile(r"\b(nx_|hf_|sk-|ghp_|gho_|github_pat_|xox[bap]-)[A-Za-z0-9_-]{12,}"), r"\1[redacted]"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+@"), r"\1[redacted]@"),
]


def redact(text):
    """Keys, tokens and passwords out of a tool's output (a log or a config line can carry any of them)."""
    for rx, sub in _SECRETS:
        text = rx.sub(sub, text)
    return text


def read_guide(page, section=None):
    """A page of the Noctyx guide, or only the sections whose heading contains `section`."""
    if page not in GUIDES:
        raise Bad(f"page is one of {', '.join(GUIDES)}")
    text = llm._guide_page(f"{page}.md")
    sec = _text(section or "", 80).lower()
    if sec:
        parts = [p for p in re.split(r"(?m)^(?=#{1,3} )", text) if p.strip()]
        hit = [p for p in parts if sec in p.splitlines()[0].lower()]
        text = "\n".join(hit) or f"(no heading matching {sec!r}; headings: " + "; ".join(
            p.splitlines()[0].strip("# ") for p in parts if p.startswith("#")) + ")"
    return text[:12000] or "(the guide is not available on this machine)"


# ── tools ──
WHY = {"type": "string", "description": "one or two sentences to the operator, shown on the proposal: what this "
                                        "does and why these values"}
S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}


def fn(name, desc, props, required=(), readonly=True, run=None, need=None):
    """One tool: (name, {readonly, need, run, schema}). A proposal carries its reason (models often send a tool
    call with no text beside it) and needs operate unless it says otherwise; a read needs view."""
    if not readonly:
        props, required = {**props, "why": WHY}, (*required, "why")
    return name, {"readonly": readonly, "run": run, "need": need or ("view" if readonly else "operate"),
                  "schema": {"type": "function", "function": {"name": name, "description": desc, "parameters": {
                      "type": "object", "properties": props, "required": list(required)}}}}


class Session:
    """One person's conversation with the assistant: the messages, the pending proposals and the running jobs.
    The LLM's turns run on a worker thread; the page polls `view()`. A subclass gives the tools, the system
    prompt, the endpoints and what the page shows beside the conversation."""

    TOOLS = {}
    JOB_TOOLS = ()
    NO_ASSISTANT = "no assistant is set up for this site"
    ROUNDS = 12                       # tool rounds per message; the last goes without tools, so it must say something
    MAX_TOKENS = 900

    def __init__(self, role="admin"):
        self.role = role
        self.lock = threading.RLock()
        self.items = []               # what the page shows
        self.msgs = []                # what the LLM sees (after the system prompt)
        self.waiting = {}             # tool_call_id -> proposal id or job id the LLM's last message waits on
        self.props = {}
        self.jobs = {}
        self.notes = []               # what the person did through forms, told to the LLM with the next message
        self.busy = False
        self.advice_only = False      # the endpoint refused tools: answers from the system prompt's context only
        self.used = time.time()
        self.closed = False
        self.ids = itertools.count(1)
        self.revs = itertools.count(1)
        self.work = threading.Condition(self.lock)
        self.queue = []
        threading.Thread(target=self._worker, daemon=True).start()

    # ── what a subclass gives ──
    def system(self):
        raise NotImplementedError

    def endpoints(self):
        """[(url, model, key, timeout)] to try in order, or [] when no assistant is set up."""
        raise NotImplementedError

    def side(self):
        """What the page shows beside the conversation (view()['state'])."""
        return None

    def tools(self):
        """The tools this person may use (their role ranks at least each tool's need)."""
        return {n: t for n, t in self.TOOLS.items() if RANK.get(self.role, -1) >= RANK[t["need"]]}

    # ── the page ──
    def _item(self, **kw):
        kw["id"] = next(self.ids)
        kw["t"] = time.time()
        kw["rev"] = next(self.revs)
        self.items.append(kw)
        return kw

    def _touch(self, key, val, **kw):
        """Update the items whose key is val; the page fetches what changed since its last revision."""
        for i in self.items:
            if i.get(key) == val:
                i.update(kw, rev=next(self.revs))

    def view(self, since=0):
        st = self.side()
        with self.lock:
            return {"items": [i for i in self.items if i["rev"] > since], "rev": max((i["rev"] for i in self.items), default=0),
                    "busy": self.busy or bool(self.queue), "state": st, "advice_only": self.advice_only,
                    "jobs": {k: {"what": j["what"], "rc": j["rc"], "tail": j["log"][-400:]} for k, j in self.jobs.items()}}

    # ── the person ──
    def say(self, text):
        with self.lock:
            if self.busy or self.queue or any(j in self.jobs for j in self.waiting.values()):
                raise Bad("the assistant is still at work: wait for it to finish")
            for cid, pid in list(self.waiting.items()):        # moving on answers what was still open
                if pid in self.props and self.props[pid]["status"] == "pending":
                    self._decide(pid, False, "the operator moved on without deciding")
            text = text.strip()[:4000]
            self.used = time.time()
            self._item(kind="user", text=text)
            notes = "".join(f"(meanwhile the operator, through the page: {n})\n" for n in self.notes)
            self.notes.clear()
            self.msgs.append({"role": "user", "content": notes + text})
            self._kick()

    def decide(self, pid, approve, args=None):
        with self.lock:
            p = self.props.get(pid)
            if not p or p["status"] != "pending":
                raise Bad("no such open proposal")
            if p["tool"] not in self.tools():
                raise Bad(f"{self.role} may not approve {p['tool']}")
            if args is not None:
                p["args"] = {k: v for k, v in args.items() if k in self.TOOLS[p["tool"]]["schema"]["function"]["parameters"]["properties"]}
            if not approve:
                return self._decide(pid, False, "rejected by the operator")
            p["status"] = "running"
        out = self._run(p["tool"], p["args"], p["call"])        # outside the lock: a probe or an install takes a while
        with self.lock:
            self._decide(pid, True, out)

    def act(self, tool, args):
        """A change the person makes through a form, without the LLM: runs at once, and the LLM hears of it."""
        if tool not in self.tools() or self.TOOLS[tool]["readonly"]:
            raise Bad("no such action")
        out = self._run(tool, args, None)
        with self.lock:
            self.notes.append(f"{tool}({json.dumps(args)}) -> {str(out)[:300]}")
        return out

    def close(self):
        """End the worker thread (a session the page no longer uses)."""
        with self.lock:
            self.closed = True
            self.work.notify()

    # ── the LLM ──
    def _kick(self):
        self.queue.append(1)
        self.work.notify()

    def _worker(self):
        while True:
            with self.lock:
                while not self.queue and not self.closed:
                    self.work.wait()
                if self.closed:
                    return
                self.queue.clear()
                self.busy = True
            try:
                self._turns()
            except Exception as e:      # an LLM that is down must not take the page with it
                with self.lock:
                    self._item(kind="error", text=f"the assistant could not answer: {getattr(e, 'reason', e)}")
            finally:
                with self.lock:
                    self.busy = False

    def _complete(self, eps, msgs, tools):
        """One completion from the first endpoint that answers -> (message, model, the endpoints from that one on).
        An endpoint that refuses the tools API (HTTP 400 on a request with tools) is asked again without them, and
        the session goes on as advice only."""
        err = None
        for i, (url, model, key, timeout) in enumerate(eps):
            try:
                try:
                    m, used = llm.complete(url, msgs, model=model, key=key, timeout=timeout, max_tokens=self.MAX_TOKENS,
                                           tools=None if self.advice_only else tools)
                except urllib.error.HTTPError as e:
                    if e.code != 400 or not tools or self.advice_only:
                        raise
                    m, used = llm.complete(url, msgs, model=model, key=key, timeout=timeout, max_tokens=self.MAX_TOKENS)
                    with self.lock:
                        self.advice_only = True
                        self._item(kind="error", text=f"{used} does not take tool calls: the assistant answers from what "
                                                      "it was given, without looking anything up")
                return m, used, eps[i:]
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                err = e
        raise err or RuntimeError("no endpoint")

    def _turns(self):
        eps = self.endpoints()
        if not eps:
            with self.lock:
                self._item(kind="error", text=self.NO_ASSISTANT)
            return
        for n in range(self.ROUNDS):
            last = n == self.ROUNDS - 1
            with self.lock:
                if self.waiting:
                    return
                tools = {} if last else self.tools()
                msgs = [{"role": "system", "content": self.system()}] + self.msgs
            if last:               # an answer now, from what it has found
                msgs.append({"role": "user", "content": "(No more lookups now: answer from what you have found, and "
                                                        "say what is still unknown.)"})
            m, model, eps = self._complete(eps, msgs, [t["schema"] for t in tools.values()] or None)
            calls = m.get("tool_calls") or []
            text = TOOL_MARKUP.sub("", m.get("content") or "").strip()      # a call written as text is not an answer
            with self.lock:
                self.msgs.append({"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})})
                if text:
                    self._item(kind="assistant", text=text, model=model)
                elif not calls:
                    self._item(kind="error", text="the assistant gave no answer: ask again, perhaps more narrowly")
            if not calls:
                return
            for call in calls:
                self._call(call, model)

    def _call(self, call, model=None):
        f = call.get("function") or {}
        name, cid = f.get("name"), call.get("id") or f"call-{next(self.ids)}"
        try:
            args = json.loads(f.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            args = None
        tools = self.tools()
        if args is None or name not in tools:
            with self.lock:
                return self._result(cid, f"error: no tool {name!r}" if args is not None else
                                    "error: the arguments are not a JSON object")
        if tools[name]["readonly"]:
            out = self._run(name, args, cid, quiet=False)
            with self.lock:
                return self._result(cid, out)
        with self.lock:
            pid = f"p{next(self.ids)}"
            self.props[pid] = {"tool": name, "args": args, "status": "pending", "call": cid, "model": model}
            self.waiting[cid] = pid
            self._item(kind="proposal", pid=pid, tool=name, args=args)

    def _result(self, cid, out):
        self.msgs.append({"role": "tool", "tool_call_id": cid,
                          "content": redact(out if isinstance(out, str) else json.dumps(out, default=str))[:12000]})

    def _run(self, name, args, cid, quiet=True):
        try:
            out = self.TOOLS[name]["run"](self, args)
            ok = True
        except Bad as e:
            out, ok = f"refused: {e}", False
        except Exception as e:
            out, ok = f"error: {e}", False
        if not self.TOOLS[name]["readonly"] or not quiet:
            with self.lock:
                self._item(kind="tool", tool=name, args=args, ok=ok,
                           out=redact(out if isinstance(out, str) else json.dumps(out, indent=1, default=str))[:6000],
                           by="assistant" if cid else "operator")
        return out

    def _decide(self, pid, approve, out):
        """Close a proposal (lock held): its result goes to the LLM, or, for a job, when the job ends."""
        p = self.props[pid]
        p["status"] = "approved" if approve else "rejected"
        self._touch("pid", pid, status=p["status"], args=p["args"])
        cid = p["call"]
        if not approve:
            self._result(cid, out)
            self.waiting.pop(cid, None)
        else:
            if p["tool"] in self.JOB_TOOLS and isinstance(out, dict) and out.get("job"):
                self.waiting[cid] = out["job"]
                self.jobs[out["job"]]["call"] = cid
            else:
                self._result(cid, out)
                self.waiting.pop(cid, None)
        if not self.waiting:
            self._kick()

    # ── jobs: `noctyx` subprocesses whose output the LLM reads when they end ──
    def start_job(self, what, argv, env=None):
        with self.lock:
            if any(j["rc"] is None for j in self.jobs.values()):
                raise Bad("a job is still running: wait for it")
            jid = f"j{next(self.ids)}"
            env = {**os.environ, "NOCTYX_NO_ASSIST": "1", "PYTHONUNBUFFERED": "1",
                   "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1]), **(env or {})}
            proc = subprocess.Popen([sys.executable, "-m", "noctyx.cli", *argv], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, env=env)
            self.jobs[jid] = {"id": jid, "what": what, "argv": argv, "log": [], "rc": None, "call": None}
            self._item(kind="job", job=jid, what=what)
            threading.Thread(target=self._pump, args=(jid, proc), daemon=True).start()
            return {"job": jid, "started": what}

    def job_ended(self, jid):
        """Called with the lock held when a job ends."""

    def _pump(self, jid, proc):
        j = self.jobs[jid]
        for line in proc.stdout:
            with self.lock:
                j["log"].append(line.rstrip("\n"))
                del j["log"][:-3000]
        rc = proc.wait()
        with self.lock:
            j["rc"] = rc
            self.job_ended(jid)
            self._touch("job", jid, rc=rc)
            cid = j.get("call")
            tail = "\n".join(j["log"][-60:])
            if cid and self.waiting.get(cid) == jid:
                self._result(cid, f"{j['what']}: exit {rc}\n{tail}")
                self.waiting.pop(cid)
                if not self.waiting:
                    self._kick()
            elif not cid:
                self.notes.append(f"{j['what']} finished with exit {rc}")

    def job_output(self, a):
        j = self.jobs.get(str(a.get("job")))
        if not j:
            raise Bad("no such job")
        return {"job": j["id"], "what": j["what"], "running": j["rc"] is None, "exit": j["rc"], "tail": "\n".join(j["log"][-80:])}
