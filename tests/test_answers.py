"""The answer check: a slot's replies to the fixed prompts are recorded per model and deployment, a reply that
differs twice is a change, one that differs once is not, a wrong reply is never a baseline, and the repair loop
asks on its interval and reports what a person must look at."""
import json

import pytest

from noctyx import answers, heal
from noctyx import orchestrator as o

GOOD = {"multiply": "391", "primes": "2, 3, 5, 7, 11, 13, 17, 19", "repeat": "The quick brown fox jumps over the lazy dog.",
        "prose": "The sky is blue because air scatters short wavelengths of sunlight the most."}


class Model:
    """What the slot's server says: per prompt id, a reply or a list of replies used one after another."""

    def __init__(self):
        self.replies, self.asked, self.name, self.up = dict(GOOD), [], "model-a", True

    def ask(self, base, model, prompt, timeout=0):
        pid = next(p for p, text, _ in answers.PROMPTS if text == prompt)
        self.asked.append(pid)
        r = self.replies[pid]
        if isinstance(r, Exception):
            raise r
        return r.pop(0) if isinstance(r, list) and len(r) > 1 else r[0] if isinstance(r, list) else r


@pytest.fixture()
def slot(tmp_path, monkeypatch):
    m = Model()
    c = {"server": {"hostname": "head", "address": "192.0.2.10"},
         "hosts": [{"name": "gpu-01", "address": "192.0.2.21", "arch": "x86_64"}],
         "recipes": {"slots": {"gpu": {"head": "gpu-01", "port": 30000,
                                       "adopt": {"unit": "llm.service", "name": "by hand", "models": ["model-a"]}}}}}
    f = tmp_path / "site.json"
    f.write_text(json.dumps(c))
    monkeypatch.setattr(o, "CFG_PATH", str(f))
    for name, path in (("RECIPE_STATE", tmp_path / "recipes.json"), ("AUDIT", tmp_path / "audit.jsonl"),
                       ("LOCK_DIR", tmp_path / "locks"), ("STATE_DIR", tmp_path)):
        monkeypatch.setattr(o, name, path)
    monkeypatch.setattr(answers, "FILE", tmp_path / "slot-answers.json")
    monkeypatch.setattr(answers, "ask", m.ask)
    monkeypatch.setattr(o, "recipe_live", lambda c, n, timeout=3: (
        {"ok": True, "models": [m.name], "url": "http://192.0.2.21:30000"} if m.up else {"ok": False, "models": [], "error": "refused"}))
    return m


def check(**kw):
    return answers.check_slot(o.cfg(), "gpu", wait=0, **kw)["last"]


def test_the_first_replies_are_the_baseline_and_the_same_replies_are_ok(slot):
    assert check()["state"] == "recorded"
    base = answers.load()["gpu"]["baseline"]
    assert base["model"] == "model-a" and base["deployment"] == "by hand" and set(base["answers"]) == set(GOOD)
    assert check()["state"] == "ok" and answers.load()["gpu"]["baseline"] == base
    assert answers.summary("gpu")["state"] == "ok" and answers.summary("gpu")["baseline_model"] == "model-a"


def test_a_reply_that_differs_twice_is_a_change_and_once_is_not(slot):
    check()
    slot.replies["primes"] = ["2, 3, 5, 7, 11, 13, 17, 19.", "2, 3, 5, 7, 11, 13, 17, 19"]      # differs once only
    l = check()
    assert l["state"] == "ok" and l["flaky"] == ["primes"] and answers.load()["gpu"]["flaky_count"] == 1
    slot.replies["primes"] = "2, 3, 5, 7, 11, 13, 17, 19 are the first eight"                  # differs every time
    slot.asked.clear()
    l = check()
    assert l["state"] == "changed" and l["prompts"] == ["primes"] and "was '2, 3, 5" in l["detail"]
    assert slot.asked == ["multiply", "primes", "repeat", "prose", "primes"]                    # asked again once
    assert check(accept=True)["state"] == "recorded" and check()["state"] == "ok"               # meant: accepted


def test_a_wrong_reply_is_reported_and_never_becomes_the_baseline(slot):
    slot.replies["multiply"] = "401"
    l = check()
    assert l["state"] == "wrong" and l["prompts"] == ["multiply"] and "baseline" not in answers.load()["gpu"]
    slot.replies["multiply"] = "391"
    assert check()["state"] == "recorded"
    slot.replies["repeat"] = "ÿÿÿÿ ÿÿ"
    assert check()["state"] == "wrong" and answers.load()["gpu"]["baseline"]["text"]["repeat"].startswith("The quick")


def test_another_model_or_deployment_records_a_new_baseline_and_says_how_it_compares(slot):
    check()
    slot.name, slot.replies["multiply"] = "model-b", "The answer is 391."
    l = check()
    assert l["state"] == "recorded" and l["differs_from_previous"] == ["multiply"]
    assert answers.load()["gpu"]["baseline"]["model"] == "model-b"
    assert "1 of 4 differ from the previous deployment's" in answers.describe("gpu", {"last": l})


def test_a_slot_that_is_down_or_refuses_keeps_its_baseline(slot):
    check()
    slot.up = False
    assert check()["state"] == "down" and "baseline" in answers.load()["gpu"]
    slot.up, slot.replies["primes"] = True, OSError("timed out")
    l = check()
    assert l["state"] == "error" and "primes" in l["detail"] and "baseline" in answers.load()["gpu"]


def test_the_repair_loop_asks_on_its_interval_and_reports_changed_and_wrong(slot, monkeypatch):
    monkeypatch.setattr(heal, "_note", lambda *a, **k: None)
    c = o.cfg()
    assert heal.check_answers(c, {}) == [] and slot.asked == ["multiply", "primes", "repeat", "prose"]   # recorded
    assert heal.check_answers(c, {}) == [] and len(slot.asked) == 4                             # not due again yet
    d = answers.load()
    d["gpu"]["last"]["ts"] -= answers.EVERY + 1
    answers.FILE.write_text(json.dumps(d))
    slot.replies["repeat"] = "The quick brown fox jumps over the lazy dog!"
    f, = heal.check_answers(c, {})
    assert f.check == "answers" and f.host == "gpu-01" and f.repair is None and "slot-check --slot gpu --accept" in f.why
    assert "no longer answers the fixed prompts as recorded" in f.what
    n = len(slot.asked)
    f, = heal.check_answers(c, {})                                     # between checks the result stands
    assert len(slot.asked) == n and f.check == "answers"
    c["recipes"]["answer_check_every"] = "off"
    assert heal.check_answers(c, {}) == [] and answers.every(c) is None
    assert answers.every({"recipes": {"answer_check_every": "6h"}}) == 6 * 3600 and answers.every({}) == answers.EVERY


def test_slot_check_command_and_the_slots_summary(slot, capsys):
    import argparse
    with pytest.raises(SystemExit) as e:
        answers.cmd_slot_check(argparse.Namespace(slot=None, accept=False, json=False))
    assert e.value.code == 0 and "gpu: recorded" in capsys.readouterr().out
    slot.replies["multiply"] = "391 "            # surrounding space is not a change: replies are stripped by the server call
    slot.replies["primes"] = "2,3,5,7,11,13,17,19"
    with pytest.raises(SystemExit) as e:
        answers.cmd_slot_check(argparse.Namespace(slot="gpu", accept=False, json=False))
    out = capsys.readouterr().out
    assert e.value.code == 1 and "CHANGED" in out and "--accept" in out
    assert o.recipe_summary(o.cfg())["slots"]["gpu"]["answers"]["state"] == "changed"
    with pytest.raises(SystemExit, match="unknown slot"):
        answers.cmd_slot_check(argparse.Namespace(slot="nope", accept=False, json=False))
    assert [json.loads(l)["state"] for l in o.AUDIT.read_text().splitlines()] == ["recorded", "changed"]
