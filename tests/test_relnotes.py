"""Release notes: fetched once per release, summarised by the assistant for the way Noctyx runs the component,
kept with a link, and put on the rows that offer the release."""
import json

import pytest

from noctyx import llm, relnotes

REPOS = {"prometheus": "prometheus/prometheus", "alertmanager": "prometheus/alertmanager", "noctyx": None}


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(relnotes, "FILE", tmp_path / "release-notes.json")
    monkeypatch.setattr(llm, "configured", lambda config=None: {"enabled": True, "url": "http://192.0.2.9/v1"})
    monkeypatch.delenv("NOCTYX_NO_ASSIST", raising=False)


def inv(state="upgrade", latest="9.9.0"):
    return {"hosts": {"head": [{"component": "prometheus", "installed": "9.8.0", "latest": latest, "state": state},
                               {"component": "alertmanager", "installed": "1.0.0", "latest": "1.0.0", "state": "ok"},
                               {"component": "noctyx", "installed": "0.1.0", "latest": "0.2.0", "state": "upgrade"}],
                      "gpu-01": [{"component": "prometheus", "installed": "9.8.0", "latest": latest, "state": state}]}}


def test_each_offered_release_is_fetched_and_summarised_once():
    got, asked = [], []

    def get(url):
        got.append(url)
        return {"body": "## Changes\n* [CHANGE] the --old.flag is removed\n* [BUGFIX] many", "html_url": "https://example.org/r/v9.9.0",
                "published_at": "2026-01-02T03:04:05Z"}

    def ask(a, system, user, max_tokens=0):
        asked.append(user)
        return "The --old.flag is removed: check the unit's flags.", "model-a"
    notes = relnotes.refresh(inv(), REPOS, get, ask)
    assert got == ["https://api.github.com/repos/prometheus/prometheus/releases/tags/v9.9.0"]     # once, not per host
    assert list(notes) == ["prometheus@9.9.0"] and notes["prometheus@9.9.0"]["summary"].startswith("The --old.flag")
    assert "Running now: 9.8.0" in asked[0] and "file_sd targets" in asked[0] and "--old.flag is removed" in asked[0]
    relnotes.refresh(inv(), REPOS, get, ask)
    assert len(got) == 1 and len(asked) == 1                                                       # kept
    rows = relnotes.attach(inv())["hosts"]
    assert rows["head"][0]["notes"] == {"summary": "The --old.flag is removed: check the unit's flags.", "model": "model-a",
                                        "url": "https://example.org/r/v9.9.0", "published": "2026-01-02",
                                        "excerpt": "## Changes\n* [CHANGE] the --old.flag is removed\n* [BUGFIX] many"}
    assert "notes" in rows["gpu-01"][0] and "notes" not in rows["head"][1] and "notes" not in rows["head"][2]
    assert "notes" in relnotes.attach(inv(state="skipped"))["hosts"]["head"][0]                   # a skipped release keeps its note
    assert "notes" not in relnotes.attach(inv(state="ok"))["hosts"]["head"][0]


def test_without_an_assistant_the_row_gets_the_link_and_the_first_lines(monkeypatch):
    monkeypatch.setattr(llm, "configured", lambda config=None: None)
    get = lambda url: {"body": "x" * 900, "html_url": "https://example.org/r", "published_at": "2026-01-02T00:00:00Z"}
    n = relnotes.refresh(inv(), REPOS, get)["prometheus@9.9.0"]
    assert n["summary"] is None and n["url"] == "https://example.org/r" and len(n["excerpt"]) == 400


def test_a_release_whose_notes_cannot_be_fetched_is_tried_again(monkeypatch):
    def gone(url):
        raise OSError("HTTP 404")
    assert relnotes.refresh(inv(), REPOS, gone) == {} and not relnotes.FILE.exists()
    silent = lambda a, system, user, max_tokens=0: (None, "no endpoint")
    get = lambda url: {"body": "notes", "html_url": "u", "published_at": ""}
    n = relnotes.refresh(inv(), REPOS, get, silent)["prometheus@9.9.0"]
    assert n["summary"] is None and n["excerpt"] == "notes"
    n = relnotes.refresh(inv(), REPOS, get, lambda a, s, u, max_tokens=0: ("now it answers", "model-a"))["prometheus@9.9.0"]
    assert n["summary"] == "now it answers"                                      # asked again once the assistant is back


def test_only_the_newest_notes_are_kept():
    get = lambda url: {"body": "notes", "html_url": url, "published_at": ""}
    for i in range(relnotes.KEEP + 5):
        relnotes.refresh(inv(latest=f"9.{i}.0"), REPOS, get, lambda a, s, u, max_tokens=0: ("s", "m"))
    kept = json.loads(relnotes.FILE.read_text())
    assert len(kept) == relnotes.KEEP and f"prometheus@9.{relnotes.KEEP + 4}.0" in kept
