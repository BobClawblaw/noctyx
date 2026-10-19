#!/usr/bin/env python3
"""Fail if tracked files contain private information.

Generic patterns live here (private address ranges, home-directory paths, e-mail addresses other than
GitHub noreply). A maintainer's own private strings go in .privacy-patterns (git-ignored, one regex per
line), so the list itself is never published.
"""
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERIC = [
    r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",                          # private (loopback is fine)
    r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
    r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
    r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b",   # CGNAT / tailnets
    r"/home/[a-z_][a-z0-9_-]*/", r"/Users/[A-Za-z][\w.-]*/",
    r"\.ts\.net\b",
    r"[\w.+-]+@(?!users\.noreply\.github\.com|example\.(?:com|org))[A-Za-z0-9][\w-]*\.[A-Za-z]{2,}\b",
]
SKIP = {"LICENSE", "tools/privacy_check.py"}


def files():
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out if f not in SKIP]


def main():
    pats = list(GENERIC)
    # a maintainer's private list: in the checkout (git-ignored), or outside it for a self-hosted CI runner
    for local in (ROOT / ".privacy-patterns", pathlib.Path(os.environ.get("NOCTYX_PRIVACY_PATTERNS", "/nonexistent"))):
        if local.is_file():
            pats += [l.strip() for l in local.read_text().splitlines() if l.strip() and not l.startswith("#")]
    rx = re.compile("|".join(f"(?:{p})" for p in pats), re.I)
    bad = 0
    for f in files():
        try:
            text = (ROOT / f).read_text(errors="replace")
        except (IsADirectoryError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            m = rx.search(line)
            if m:
                bad += 1
                print(f"{f}:{n}: {m.group(0)!r}")
    if bad:
        print(f"privacy_check: {bad} finding(s)", file=sys.stderr)
        return 1
    print("privacy_check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
