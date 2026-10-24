#!/usr/bin/env python3
"""Ask Noctyx against the site's real assistant: fixed questions, scored, so a change to its prompt is shown to
help rather than only to differ.

    NOCTYX_CONFIG=/etc/noctyx/site.json PYTHONPATH=src python3 tools/ask_eval.py [--only a,b] [--tz Europe/Paris]
        [--jobs 2] [--prompt my-prompt.txt] [--notes my-notes.txt] [--out report.json]

Each question runs in a new conversation with the console's own read-only tools, on the site's live data and the
assistant set up in Configure › Assistant LLM, so run it on the head as the user the console runs as (the log
tools reach the nodes over its SSH key). Questions name the site's machines by role ({head}, {pair}, {mac}, ...);
one whose machine the site does not have is skipped. Every answer is scored on:

- tool: its first lookup is one this question calls for ("-" = no lookup: the site line or a refusal answers it);
  how many lookups it made is shown too;
- commands: every `noctyx ...` it writes parses with Noctyx's own command line;
- must / never: words the answer has to have, or must not;
- dates: each date it gives (YYYY-MM-DD) is one the tools returned or within a week of today;
- numbers: each number it gives (versions, times, dates and small counts aside) is found in what the tools
  returned, as is or in another unit (°F, GB, GiB, %, minutes); one that is not is listed for a person to read.

The answers are printed, and with --out written as JSON beside the scores. They hold the site's data: keep the
report out of the repository.
"""
import argparse
import contextlib
import io
import json
import math
import pathlib
import re
import shlex
import sys
import threading
import time

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.append(str(SRC))              # after PYTHONPATH: another checkout can be measured too

ANY = None
CASES = [
    # (id, role, question, first tools allowed, must (regex, all), never (regex, none))
    ("now", "admin", "How is the cluster right now?", {"cluster_now"}, [], []),
    ("failed", "admin", "Did anything fail in the last 24 hours?", {"alerts", "backups", "repairs", "cluster_now"}, [], []),
    ("hottest", "admin", "Which GPU ran hottest today?", {"prometheus", "metric_names"}, [r"°F"], []),
    ("storage", "admin", "When will the backup storage fill up?", {"storage"}, [], []),
    ("repaired", "admin", "What did Noctyx repair today?", {"repairs"}, [], []),
    ("upgrades", "admin", "Is everything up to date? What should I upgrade, and how?", {"versions"}, [], []),
    ("rollback", "admin", "Did any upgrade roll back this week, and why?", {"repairs"}, [], []),
    ("pair", "admin", "Is the model on {pair} and {pair_worker} healthy?", {"cluster_now", "recipes", "prometheus"},
     [r"{pair}"], []),
    ("serves", "admin", "What does {pair} serve?", {"-", "recipes", "cluster_now"}, [r"{pair_model}"], []),
    ("lastbackup", "admin", "When did {head} last back up?", {"backups"}, [r"\d{1,2}:\d\d"], []),
    ("verify-local", "admin", "When did the last verify run finish, in my time?", {"backups"}, [r"\d{1,2}:\d\d"], []),
    ("log-viewer", "view", "Show me the last 50 lines of the prometheus service's log on {head}.", ANY,
     [r"operator"], []),
    ("log-admin", "admin", "Show me the last 50 lines of the prometheus service's log on {head}.", {"service_log"}, [], []),
    ("containers", "admin", "Which containers run on {pair}?", {"containers"}, [], []),
    ("find", "admin", "Is /etc/fstab in {linux}'s backups, and since when?", {"find_in_backups", "snapshots"}, [], []),
    ("restore", "admin", "How do I get one file back from a backup?", {"-", "read_guide", "snapshots"},
     [r"Snapshot Browser"], []),
    ("mac-memory", "admin", "How much memory is free on {mac}?", {"cluster_now", "host_details", "prometheus"}, [], []),
    ("reboot", "admin", "Should I reboot {linux}?", {"cluster_now", "host_details", "versions", "hygiene"}, [],
     [r"(?i)\byou should reboot\b"]),
    ("alert-rule", "admin", "Add an alert for a GPU above 85 °C.", ANY, [r"(?i)ships its own|not .{0,40}console|cannot"],
     [r"(?i)I (added|created)", r"(?i)Configure\W{1,4}(alert|rule)"]),
    ("offtopic", "admin", "Write me a Python function that sorts a list.", {"-"}, [r"(?i)cluster|site|Noctyx"],
     [r"def \w+\(", r"sorted\("]),
    ("tokens", "admin", "How many tokens per second did {head}'s model server make over the last hour?",
     {"prometheus", "cluster_now", "metric_names", "recipes"}, [], []),
]


def roles(c, cm):
    """{head}, {pair}, {pair_worker}, {pair_model}, {mac}, {linux}: this site's machines, by what they are."""
    hosts = [h for h in c["hosts"] if h.get("enabled", True) is not False]
    head_addr = (c.get("server") or {}).get("address")
    r = {}
    for h in hosts:
        if h.get("local") or h.get("address") == head_addr:
            r.setdefault("head", h["name"])
        elif h.get("os") == "macos":
            r.setdefault("mac", h["name"])
        else:
            r.setdefault("linux", h["name"])
    try:
        for s in cm.recipe_summary(c)["slots"].values():
            models = (s.get("live") or {}).get("models")
            if s.get("worker") and models:
                r.update(pair=s["head"], pair_worker=s["worker"], pair_model="|".join(re.escape(m) for m in models))
                break
    except Exception:
        pass
    return r


def fill(text, r):
    """{head} and the like replaced; a regex's own braces ({1,2}) left alone."""
    return re.sub(r"\{([a-z_]+)\}", lambda m: r[m.group(1)], text)


class _Got(Exception):
    pass


def cli_check():
    """A function that says whether a `noctyx ...` line parses with Noctyx's own command line."""
    import argparse
    from noctyx import firstrun, orchestrator
    got = {}

    def grab(self, *a, **k):
        got["p"] = self
        raise _Got
    real, argparse.ArgumentParser.parse_args = argparse.ArgumentParser.parse_args, grab
    try:
        orchestrator.main()
    except _Got:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real
    main, init = got["p"], firstrun.parser()
    plain = {"banner", "version", "--version", "paths", "demo", "ask", "tool"}

    def ok(line):
        try:
            argv = shlex.split(line)
        except ValueError:
            return False
        while argv and argv[0] in ("sudo", "-n", "-E"):
            argv.pop(0)
        if not argv or argv.pop(0) != "noctyx":
            return True                         # not a noctyx command: not this check's business
        if not argv or argv[0] in plain or (len(argv) == 1 and re.fullmatch(r"v?\d+(\.\d+)+", argv[0])):
            return True                         # "noctyx 0.1.0": a version, not a command
        if len(argv) == 1 and argv[0] in main._subparsers._group_actions[0].choices:
            return True                         # the command named, not run (`noctyx upgrade` does that)
        p, argv = (init, argv[1:]) if argv[0] == "init" else (main, argv)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            try:
                p.parse_args(argv)
            except SystemExit as e:
                return e.code == 0              # --help
        return True
    return ok


NUM = re.compile(r"(?<![\w.:/#-])-?\d+(?:,\d{3})*(?:\.\d+)?(?![\w:/-]|\.\d)")


def numbers(text):
    out = []
    for m in NUM.finditer(text):
        v = float(m.group().replace(",", ""))
        if v == int(v) and abs(v) <= 12:        # small counts (3 nodes, 2 tries) match anything
            continue
        out.append(v)
    return out


def grounded(v, seen):
    """Whether v is a number the tools returned, as is or in another unit."""
    for t in seen:
        for f in (1, 1e-9, 2 ** -30, 1e-12, 2 ** -40, 1e-6, 2 ** -20, 100, 1 / 60, 1 / 3600, 1 / 86400):
            if math.isclose(v, t * f, rel_tol=0.02, abs_tol=0.6):
                return True
        if math.isclose(v, t * 9 / 5 + 32, abs_tol=1.1):
            return True
    return False


def run_case(case, r, check, tz, timeout):
    from noctyx import copilot
    cid, role, q, first, must, never = case
    need = set(re.findall(r"\{([a-z_]+)\}", q + " ".join(must))) - set(r)
    if need:
        return {"id": cid, "skipped": f"the site has no {', '.join(sorted(need))}"}
    q, must = fill(q, r), [fill(m, r) for m in must]
    try:
        s = copilot.start("ask-eval", role, "F", tz)
    except TypeError:                               # a checkout from before the asker's zone was passed
        s = copilot.start("ask-eval", role, "F")
    t0 = time.time()
    try:
        s.say(q)
        while time.time() - t0 < timeout:
            with s.lock:
                if not s.busy and not s.queue:
                    break
            time.sleep(0.2)
        else:
            return {"id": cid, "question": q, "error": f"no answer in {timeout} s"}
        with s.lock:
            msgs, items = list(s.msgs), list(s.items)
    finally:
        copilot.SESSIONS.pop(s.sid, None)
        s.close()
    calls = [c["function"]["name"] for m in msgs if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    seen_text = " ".join(m["content"] for m in msgs if m["role"] == "tool")
    answer = "\n\n".join(i["text"] for i in items if i["kind"] == "assistant")
    errors = [i["text"] for i in items if i["kind"] == "error"]
    seen = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", seen_text)]
    cmds = [c for c in re.findall(r"`([^`\n]+)`", answer) if re.match(r"(sudo\s+(-\S+\s+)*)?noctyx\b", c)]
    res = {
        "id": cid, "role": role, "question": q, "seconds": round(time.time() - t0, 1), "calls": calls,
        "answer": answer, "errors": errors,
        "tool": first is ANY or (calls[0] if calls else "-") in first,
        "commands_bad": [c for c in cmds if not check(c)],
        "must_missing": [m for m in must if not re.search(m, answer)],
        "never_found": [n for n in never if re.search(n, answer)],
        "numbers_unfound": [v for v in numbers(answer) if not grounded(v, seen)],
        "dates_wrong": [d for d in sorted(set(re.findall(r"\b20\d\d-\d\d-\d\d\b", answer)))
                        if d not in seen_text and abs(time.mktime(time.strptime(d, "%Y-%m-%d")) - t0) > 8 * 86400],
    }
    res["pass"] = bool(answer) and res["tool"] and not (res["commands_bad"] or res["must_missing"] or res["never_found"]
                                                        or res["dates_wrong"])
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--tz", help="the asker's IANA time zone, as a browser would send it")
    ap.add_argument("--jobs", type=int, default=2, help="questions at once (the assistant serves others too)")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--prompt", help="a system prompt of your own to measure instead of the site's (see the guide)")
    ap.add_argument("--notes", help="site notes to measure with it")
    ap.add_argument("--out")
    a = ap.parse_args()
    from noctyx import console, copilot, orchestrator          # importing the console binds the copilot to it
    if a.prompt or a.notes:
        if not hasattr(copilot, "site_prompt"):
            sys.exit("--prompt and --notes need a checkout whose Ask takes a site's own prompt")
        template, notes, custom = copilot.site_prompt()
        if a.prompt:
            template, custom = pathlib.Path(a.prompt).read_text(), True
            why = copilot.check_prompt(template)
            if why:
                sys.exit(why)
        if a.notes:
            notes = pathlib.Path(a.notes).read_text().strip()
        copilot.site_prompt = lambda: (template, notes, custom)
    r = roles(console.CFG, orchestrator)
    check = cli_check()
    cases = [c for c in CASES if not a.only or c[0] in a.only.split(",")]
    results, lock, todo = [], threading.Lock(), list(cases)

    def worker():
        while True:
            with lock:
                if not todo:
                    return
                case = todo.pop(0)
            try:
                res = run_case(case, r, check, a.tz, a.timeout)
            except Exception as e:
                res = {"id": case[0], "error": f"{type(e).__name__}: {e}"}
            with lock:
                results.append(res)
                print(f"· {res['id']}: {'skipped' if 'skipped' in res else ('pass' if res.get('pass') else 'FAIL')}",
                      file=sys.stderr, flush=True)
    threads = [threading.Thread(target=worker) for _ in range(max(1, a.jobs))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    order = [c[0] for c in cases]
    results.sort(key=lambda x: order.index(x["id"]))
    for x in results:
        if "skipped" in x or "error" in x:
            print(f"\n## {x['id']}: {x.get('skipped') or x.get('error')}")
            continue
        flags = [f for f, bad in (("tool", not x["tool"]), ("commands", x["commands_bad"]), ("must", x["must_missing"]),
                                  ("never", x["never_found"]), ("dates", x["dates_wrong"])) if bad]
        print(f"\n## {x['id']} [{x['role']}] {'PASS' if x['pass'] else 'FAIL ' + ', '.join(flags)}  "
              f"({x['seconds']} s, {' > '.join(x['calls']) or 'no lookups'})")
        print(f"Q: {x['question']}")
        for k in ("commands_bad", "must_missing", "never_found", "dates_wrong", "numbers_unfound", "errors"):
            if x[k]:
                print(f"  {k}: {x[k]}")
        print(x["answer"])
    scored = [x for x in results if "pass" in x]
    unfound = sum(len(x["numbers_unfound"]) for x in scored)
    total = sum(len(numbers(x["answer"])) for x in scored)
    print(f"\n{sum(x['pass'] for x in scored)}/{len(scored)} pass; first lookup right {sum(x['tool'] for x in scored)}"
          f"/{len(scored)}; {sum(len(x['calls']) for x in scored)} lookups; "
          f"numbers found in the tools' output {total - unfound}/{total}; "
          f"{sum(x['seconds'] for x in scored):.0f} s")
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(results, indent=1, default=str))
    return 0 if all(x.get("pass") for x in scored) else 1


if __name__ == "__main__":
    sys.exit(main())
