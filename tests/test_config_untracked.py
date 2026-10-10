"""No config file with a committed `.example` template is itself tracked.

An `.example` file exists because somebody decided the real file stays out of
git. A tracked real file beside one is that decision made and not carried out:
jwt.json, tiers.json and roles.json were all tracked until
issue_01M2RVJK4DDNJSGK5Y2AETKB43, and a public export carries the tree.

This check sees only files that have a template. concierge.json has none, so
only its .gitignore entry keeps it out of git.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import paths

REPO_ROOT = Path(paths.__file__).resolve().parent


def _tracked() -> set[str]:
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
                         capture_output=True, text=True, check=True).stdout
    return {p for p in out.split("\0") if p}


def test_no_file_with_an_example_template_is_tracked():
    tracked = _tracked()
    examples = sorted(p for p in tracked if ".example." in Path(p).name)
    # The check is empty if the templates themselves went missing.
    assert "jwt.example.json" in examples and "tiers.example.json" in examples
    leaked = [p.replace(".example.", ".", 1) for p in examples
              if p.replace(".example.", ".", 1) in tracked]
    assert leaked == [], f"tracked despite a committed .example template: {leaked}"
