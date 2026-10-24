"""Going back: a change that passed can be undone later (the newest in effect first), the undo is a change of its
own (checked, rolled back when it fails); a change waits for a checkpoint of its machine; the
site config keeps its history; Noctyx itself goes back to a recorded commit."""
import json
import subprocess

import pytest

from noctyx import change, checkpoints, selfstate


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(change, "LEDGER", tmp_path / "changes.jsonl")
    monkeypatch.setattr(change, "LOCK", tmp_path / "change.lock")
    from noctyx import orchestrator as cm
    monkeypatch.setattr(cm, "AUDIT", tmp_path / "audit.jsonl")
    return tmp_path


def quiet(_):
    pass


class Box(change.Change):
    """A change to a value in a dict (the machine): its snapshot keeps the value, its rollback puts it back."""
    kind = "box"

    def __init__(self, world, to=None, snap=None, works=lambda v: True):
        super().__init__({}, {"name": "gpu-01"}, snap)
        self.world, self.to, self.works = world, to, works
        self.what = f"box to {to}"

    def prepare_revert(self):
        self.snap["before"] = {"ok": True}

    def snapshot(self):
        self.snap["was"] = self.world["v"]

    def apply(self):
        self.world["v"] = self.to

    def verify(self):
        return [] if self.works(self.world["v"]) else [f"{self.world['v']} does not work"]

    def rollback(self):
        self.world["v"] = self.snap["was"]

    def verify_rollback(self):
        return [] if self.works(self.world["v"]) else [f"{self.world['v']} does not work"]


def undo(world, rid, works=lambda v: True):
    rec, why = change.revertible(rid)
    assert why is None, why
    return change.run(change.Revert(rec, Box(world, snap=rec["snap"], works=works), Box(world, works=works)), log=quiet)


def test_changes_are_undone_newest_first_and_an_undo_is_not_undone():
    world = {"v": 1}
    a = change.run(Box(world, 2), log=quiet)
    b = change.run(Box(world, 3), log=quiet)
    assert change.revertible(a["id"])[1].startswith(f"change {b['id']}")      # the newer one first
    r1 = undo(world, b["id"])
    assert world["v"] == 2 and r1["outcome"] == "ok" and r1["reverts"] == b["id"]
    st = change.state_of()
    assert st[b["id"]] == "undone" and st[a["id"]] == "in effect" and st[r1["id"]] == f"undid {b['id']}"
    assert "run the change again" in change.revertible(r1["id"])[1]
    r2 = undo(world, a["id"])
    assert world["v"] == 1
    assert change.revertible(a["id"])[1] == "it was undone already"
    assert change.stacks().get(("gpu-01", "box")) == []
    assert r2["outcome"] == "ok"


def test_an_undo_that_does_not_work_puts_back_what_ran():
    world = {"v": 1}
    a = change.run(Box(world, 2), log=quiet)
    r = undo(world, a["id"], works=lambda v: v != 1)          # going back to 1 breaks it
    assert r["outcome"] == "rolled_back" and world["v"] == 2
    assert change.state_of()[a["id"]] == "in effect"            # still the one in effect


def test_heals_undo_and_a_late_canary_rollback_count_as_undone():
    recs = [{"id": "aaaa0001", "host": "h", "component": "c", "outcome": "ok"},
            {"id": "aaaa0001-undo", "host": "h", "component": "c", "outcome": "reverted"},
            {"id": "bbbb0002", "host": "h", "component": "c", "outcome": "ok"},
            {"id": "cccc0003", "host": "h", "component": "c", "outcome": "rolled_back", "reverts": "bbbb0002"}]
    assert change.stacks(recs)[("h", "c")] == []
    assert change.state_of(recs)["aaaa0001"] == "undone"


def test_a_change_that_asks_for_one_waits_for_a_checkpoint(monkeypatch):
    seen = []
    monkeypatch.setattr(checkpoints, "take", lambda c, h, reason, **k: seen.append(reason) or
                        {"snapshot": "1a2b3c4d", "taken": True})
    world = {"v": 1}
    ch = Box(world, 2)
    ch.checkpoint = True
    rec = change.run(ch, log=quiet)
    assert seen == ["change"] and rec["checkpoint"]["snapshot"] == "1a2b3c4d"


def test_a_required_checkpoint_that_fails_refuses_the_change(monkeypatch):
    monkeypatch.setattr(checkpoints, "take", lambda c, h, reason, **k: {"error": "the repository does not answer"})
    world = {"v": 1}
    ch = Box(world, 2)
    ch.checkpoint, ch.c = True, {"checkpoints": {"require": True}}
    rec = change.run(ch, log=quiet)
    assert rec["outcome"] == "refused" and world["v"] == 1 and "require" in rec["problems"][0]
    ch = Box(world, 2)
    ch.checkpoint = True
    assert change.run(ch, log=quiet)["outcome"] == "ok"           # not required: it runs and says why


@pytest.mark.parametrize("blk, bad", [({"before_apt": "yes"}, "true or false"), ({"max_age_hours": 500}, "0 to 168"),
                                      ({"nope": 1}, "unknown")])
def test_the_checkpoint_policy_refuses(blk, bad):
    assert any(bad in e for e in checkpoints.validate(blk)[1])


def test_the_apt_hook_never_stops_apt():
    t = checkpoints.hook_text({"checkpoints": {"max_age_hours": 2, "apt_timeout_minutes": 10}})
    assert "DPkg::Pre-Invoke" in t and "--max-age 7200 --timeout 600" in t
    assert t.count("|| true") == 2 and "checkpoint-after --reason apt" in t


def test_the_agent_result_line_is_read(monkeypatch):
    from noctyx import orchestrator as cm
    tok = cm.P["result_token"]
    r = subprocess.CompletedProcess([], 0, stdout=f'noise\n{tok} {{"event":"checkpoint","snapshot":"abcd1234","taken":false,"age":60}}\n',
                                    stderr="")
    assert checkpoints._result(r) == {"snapshot": "abcd1234", "taken": False, "age": 60}
    r = subprocess.CompletedProcess([], 255, stdout="", stderr="ssh: connect to host gpu-01: No route to host\n")
    assert "No route" in checkpoints._result(r)["error"]


# ── the site config's history ───────────────────────────────────────────────
@pytest.fixture()
def site(tmp_path, monkeypatch):
    f = tmp_path / "site.json"
    f.write_text(json.dumps({"a": 1}))
    monkeypatch.setattr(selfstate, "CONFIG", str(f))
    monkeypatch.setattr(selfstate, "CONFIG_HISTORY", tmp_path / "config-history")
    monkeypatch.setattr(selfstate, "SELF_DIR", tmp_path / "self")
    return f


def test_the_site_config_keeps_its_history_and_goes_back(site):
    name = selfstate.save_config("heal")
    site.write_text(json.dumps({"a": 2}))
    h = selfstate.config_history()
    assert [x["name"] for x in h] == [name] and h[0]["why"] == "heal"
    assert '-{"a": 2}' in selfstate.config_diff(name) and '+{"a": 1}' in selfstate.config_diff(name)
    kept = selfstate.config_restore(name)
    assert json.loads(site.read_text()) == {"a": 1}
    assert json.loads((selfstate.CONFIG_HISTORY / kept).read_text()) == {"a": 2}     # the one it replaced
    with pytest.raises(ValueError):
        selfstate.config_diff("../../etc/passwd")


def test_only_the_last_copies_are_kept(site, monkeypatch):
    monkeypatch.setattr(selfstate, "KEEP_CONFIGS", 3)
    for i in range(5):
        selfstate.save_config(f"w{i}")
    assert [x["why"] for x in selfstate.config_history()] == ["w4", "w3", "w2"]


# ── Noctyx itself ───────────────────────────────────────────────────────────
@pytest.fixture()
def repo(tmp_path, monkeypatch, site):
    r = tmp_path / "repo"
    r.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                              capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (r / "f").write_text("1")
    git("add", "f")
    git("commit", "-qm", "one")
    monkeypatch.setattr(selfstate, "PKG", r)
    return r, git


def test_a_self_checkpoint_records_the_commit_and_goes_back_to_it(repo, monkeypatch):
    r, git = repo
    first = git("rev-parse", "HEAD")
    assert selfstate.checkpoint_if_new() and selfstate.checkpoint_if_new() is None     # once per code
    (r / "f").write_text("2")
    git("commit", "-qam", "two")
    assert selfstate.checkpoint_if_new()                                                  # the new code
    plan = selfstate.rollback_plan()
    assert plan["problems"] == [] and plan["target"]["identity"]["git"]["sha"] == first
    assert selfstate.self_rollback(units=False, restart=False, log=quiet) == 0
    assert git("rev-parse", "HEAD") == first and (r / "f").read_text() == "1"


def test_self_rollback_refuses_uncommitted_work(repo):
    r, git = repo
    selfstate.self_checkpoint("one")
    (r / "f").write_text("2")
    git("commit", "-qam", "two")
    (r / "f").write_text("local edit")
    plan = selfstate.rollback_plan()
    assert any("uncommitted" in p for p in plan["problems"])
    assert selfstate.self_rollback(units=False, restart=False, log=quiet) == 2 and (r / "f").read_text() == "local edit"


# ── the console ─────────────────────────────────────────────────────────────
def test_the_console_only_undoes_what_is_in_effect():
    from noctyx import console
    world = {"v": 1}
    a = change.run(Box(world, 2), log=quiet)
    b = change.run(Box(world, 3), log=quiet)
    assert console.valid_params("revertChange", {"id": b["id"]})
    assert not console.valid_params("revertChange", {"id": a["id"]})
    assert not console.valid_params("revertChange", {"id": "; rm -rf /"})
    v = {x["id"]: x for x in console.changes_view()["changes"]}
    assert v[b["id"]]["revertible"] and not v[a["id"]]["revertible"] and "snap" not in v[b["id"]]


def test_a_head_service_revert_knows_whose_data_copy_it_is(tmp_path):
    from noctyx import upgrades
    data = str(tmp_path / "metrics2")
    (tmp_path / "metrics2.noctyx-prev").mkdir()
    (tmp_path / "metrics2.noctyx-prev-bin").mkdir()           # the older single copy: empty = nothing of Noctyx's
    legacy = upgrades.HeadService({}, {"name": "head", "local": True}, snap={"data": data, "prev": data + ".noctyx-prev"})
    assert legacy.revert_missing() is None
    (tmp_path / "metrics2.noctyx-prev.change").write_text("bbbb0002")          # a later change copied the data
    assert "--keep-data" in legacy.revert_missing()
    legacy.keep_data = True
    assert legacy.revert_missing() is None
    (tmp_path / "metrics2.noctyx-prev-bin" / "bbbb0002").mkdir()
    newer = upgrades.HeadService({}, {"name": "head", "local": True},
                                 snap={"data": data, "prev": data + ".noctyx-prev", "data_copy": "bbbb0002",
                                       "bins": str(tmp_path / "metrics2.noctyx-prev-bin" / "bbbb0002")})
    assert newer.revert_missing() is None
    gone = upgrades.HeadService({}, {"name": "head", "local": True},
                                snap={"data": data, "bins": str(tmp_path / "nope"), "data_copy": "x"})
    assert "no checkpoint snapshot" in gone.revert_missing()
    gone.checkpoint_snap = "1a2b3c4d"
    gone.keep_data = True
    assert gone.revert_missing() is None and gone.from_snapshot             # restic has the binaries
