"""Does each slot still answer the way it did: a few fixed prompts, asked greedily, compared with the replies
recorded for that deployment.

    noctyx slot-check [--slot NAME] [--accept] [--json]

A port that answers says nothing about what it answers. A changed engine build, cache precision or weight
file, or failing hardware, leaves /health and /v1/models as they were and changes the tokens. So for every
slot Noctyx keeps the replies its model gave to PROMPTS (temperature 0, a fixed seed, thinking off, a few
dozen tokens) and asks again: after each switch and from the repair loop (`noctyx heal`, hourly by default).

  ok        every reply is the recorded one
  recorded  first replies for this model and deployment (after a deploy, an adopt, or --accept): the baseline
  changed   a reply differs from the recorded one, twice in a row: something behind the port changed
  wrong     a reply misses what any working model says (17 x 23, the first primes, a sentence to repeat,
            why the sky is blue)
  down      the slot does not answer; error: the request failed

`changed` and `wrong` are for a person: the repair loop reports them (alert NoctyxNeedsAttention) and never
restarts a model over them. `--accept` records the present replies as the baseline, for a change that was meant.
"""
import hashlib
import json
import os
import pathlib
import re
import time
import urllib.request

from noctyx.paths import P

FILE = pathlib.Path(P["state"]) / "slot-answers.json"
EVERY = 3600                     # the repair loop asks this often (recipes.answer_check_every; "off": never)
MAX_TOKENS = 48
# (id, the prompt, what any working model's reply contains). The first three have one right answer, which every
# healthy model gives; the last is free prose, where a small change in the numbers behind the port (a different
# quantization, cache precision or kernel) shows as different words.
PROMPTS = (
    ("multiply", "What is 17 * 23? Reply with the number only.", r"\b391\b"),
    ("primes", "List the first eight prime numbers, comma separated, and nothing else.",
     r"2,\s*3,\s*5,\s*7,\s*11,\s*13,\s*17,\s*19"),
    ("repeat", "Reply with exactly this sentence: The quick brown fox jumps over the lazy dog.",
     r"The quick brown fox jumps over the lazy dog"),
    ("prose", "In two sentences, explain why the sky is blue.", r"(?i)scatter|wavelength|light"),
)


def _cm():
    from noctyx import orchestrator as cm
    return cm


def ask(base, model, prompt, timeout=120):
    """One greedy reply from an OpenAI-compatible server -> its text."""
    body = {"model": model, "temperature": 0, "seed": 0, "max_tokens": MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False}, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(base.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        m = (json.loads(r.read()).get("choices") or [{}])[0].get("message") or {}
    return (m.get("content") or m.get("reasoning_content") or "").strip()


def load():
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(d):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.chmod(tmp, 0o644)
    os.replace(tmp, FILE)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def every(c):
    """Seconds between the repair loop's checks, or None when the site turned them off."""
    v = (c.get("recipes") or {}).get("answer_check_every", EVERY)
    if v in ("off", False, 0, None):
        return None
    try:
        return max(300.0, _cm().parse_duration(v))
    except ValueError:
        return float(EVERY)


def check_slot(c, slot, accept=False, asker=None, wait=2.0):
    """Ask the slot's model the fixed prompts and compare -> the slot's record, saved. accept: the replies
    become the baseline whatever was recorded."""
    cm = _cm()
    asker = asker or ask
    live = cm.recipe_live(c, slot, timeout=10)
    data = load()
    rec = dict(data.get(slot) or {})
    now = time.time()
    if not live.get("ok"):
        rec["last"] = {"ts": now, "state": "down", "detail": str(live.get("error") or live.get("status") or "")[:120]}
        data[slot] = rec
        _save(data)
        return rec
    model = live["models"][0]
    cur, _, _ = cm.slot_records(c, slot)
    deployment = cm.rec_label(cur)
    got, wrong, err = {}, [], None
    for pid, prompt, expect in PROMPTS:
        try:
            got[pid] = asker(live["url"], model, prompt)
        except Exception as e:
            err = f"{pid}: {str(getattr(e, 'reason', e))[:120]}"
            break
        if not re.search(expect, got[pid]):
            wrong.append(pid)
    if err:
        rec["last"] = {"ts": now, "state": "error", "model": model, "detail": err}
        data[slot] = rec
        _save(data)
        return rec
    base = rec.get("baseline") or {}
    fresh = accept or not base or base.get("model") != model or base.get("deployment") != deployment
    last = {"ts": now, "model": model, "deployment": deployment}
    if wrong:
        last.update(state="wrong", prompts=wrong,
                    detail="; ".join(f"{p}: {got[p][:80]!r}" for p in wrong))
    if fresh:
        if base and not accept:              # a new model or deployment: say how it compares, for the record
            last["differs_from_previous"] = [p for p, _, _ in PROMPTS if _sha(got[p]) != (base.get("answers") or {}).get(p)]
        if not wrong or accept:
            rec["baseline"] = {"model": model, "deployment": deployment, "recorded": now,
                               "answers": {p: _sha(t) for p, t in got.items()}, "text": {p: t[:200] for p, t in got.items()}}
        last.setdefault("state", "recorded")
    elif not wrong:
        changed = [p for p, _, _ in PROMPTS if _sha(got[p]) != base["answers"].get(p)]
        if changed:                          # once more: a reply that differs only once is not a change
            time.sleep(wait)
            again = {}
            for pid, prompt, _ in PROMPTS:
                if pid in changed:
                    try:
                        again[pid] = asker(live["url"], model, prompt)
                    except Exception:
                        again[pid] = got[pid]
            flaky = [p for p in changed if _sha(again[p]) == base["answers"].get(p)]
            changed = [p for p in changed if p not in flaky]
            if flaky:
                last["flaky"] = flaky
                rec["flaky_count"] = rec.get("flaky_count", 0) + 1
        if changed:
            last.update(state="changed", prompts=changed,
                        detail="; ".join(f"{p}: was {(base.get('text') or {}).get(p, '')[:60]!r}, now {got[p][:60]!r}"
                                         for p in changed))
        else:
            last["state"] = "ok"
    rec["last"] = last
    data[slot] = rec
    _save(data)
    return rec


def summary(slot=None):
    """What the console shows per slot: the last check's state and when, and when the baseline was recorded."""
    out = {}
    for n, rec in load().items():
        l = rec.get("last") or {}
        out[n] = {**{k: l.get(k) for k in ("ts", "state", "model", "detail", "prompts", "flaky")},
                  "recorded": (rec.get("baseline") or {}).get("recorded"),
                  "baseline_model": (rec.get("baseline") or {}).get("model")}
    return out.get(slot) if slot else out


def describe(slot, rec):
    l = rec.get("last") or {}
    st = l.get("state", "?")
    n = len(PROMPTS)
    if st == "ok":
        return f"{slot}: ok — {l.get('model')} answers the {n} fixed prompts as recorded"
    if st == "recorded":
        d = l.get("differs_from_previous")
        return (f"{slot}: recorded — {l.get('model')}'s replies are the baseline for {l.get('deployment')}"
                + (f" ({len(d)} of {n} differ from the previous deployment's)" if d is not None else ""))
    if st in ("changed", "wrong"):
        what = "no longer answers as recorded" if st == "changed" else "answers wrongly"
        return f"{slot}: {st.upper()} — {l.get('model')} {what} ({len(l.get('prompts') or [])} of {n}): {l.get('detail')}"
    return f"{slot}: {st} — {l.get('detail') or ''}".rstrip(" —")


def cmd_slot_check(a):
    cm = _cm()
    c = cm.cfg()
    slots = cm.recipes_cfg(c)["slots"]
    if a.slot and a.slot not in slots:
        raise SystemExit(f"unknown slot: {a.slot} (slots: {', '.join(slots) or 'none'})")
    out, bad = {}, 0
    for n in ([a.slot] if a.slot else list(slots)):
        rec = check_slot(c, n, accept=a.accept)
        out[n] = rec
        st = (rec.get("last") or {}).get("state")
        bad += st in ("changed", "wrong")
        if not a.json:
            print(describe(n, rec))
            if st == "changed":
                print(f"  meant to change? `noctyx slot-check --slot {n} --accept` records these replies as the baseline")
        cm.audit("slot-check", slot=n, state=st, model=(rec.get("last") or {}).get("model"),
                 prompts=(rec.get("last") or {}).get("prompts"), accepted=bool(a.accept) or None)
    if a.json:
        print(json.dumps(out, indent=1))
    raise SystemExit(1 if bad else 0)
