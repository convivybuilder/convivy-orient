"""Tests for paths.py — the config/state roots ADR-0059 rule 3 requires.

The property under test is the one the rule exists for: CONFIG_ROOT and
STATE_ROOT default to today's checkout location (a no-op until an install
sets the environment variable), and every module that reads a config file or
writes site/access-log state resolves it through one of these two roots
rather than through its own `Path(__file__).parent`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import access_identity
import api_common
import authz
import build
import paths
import server as srv


REPO_ROOT = Path(paths.__file__).resolve().parent


def test_config_root_defaults_to_the_checkout_directory():
    assert paths.CONFIG_ROOT == REPO_ROOT


def test_state_root_defaults_to_the_checkout_directory():
    assert paths.STATE_ROOT == REPO_ROOT


def _run_with_env(env_overrides: dict) -> str:
    """A fresh interpreter, since CONFIG_ROOT/STATE_ROOT are resolved once at
    import time — mutating os.environ in this process after paths has already
    been imported (by conftest's sys.path-adjacent modules) would not exercise
    the read this test is pinning."""
    code = "import paths; print(paths.CONFIG_ROOT); print(paths.STATE_ROOT)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, env=env_overrides, capture_output=True, text=True, check=True,
    )
    return result.stdout


def test_config_root_is_overridable_by_environment():
    import os
    env = dict(os.environ)
    env["ORIENT_CONFIG_ROOT"] = "/tmp/orient-config-root-test"
    out = _run_with_env(env)
    lines = out.splitlines()
    assert lines[0] == "/tmp/orient-config-root-test"


def test_state_root_is_overridable_by_environment():
    import os
    env = dict(os.environ)
    env["ORIENT_STATE_ROOT"] = "/tmp/orient-state-root-test"
    out = _run_with_env(env)
    lines = out.splitlines()
    assert lines[1] == "/tmp/orient-state-root-test"


# --- a relative root is refused, not silently resolved against cwd -----------
#
# Carried forward from the #233 security review, due at the deploy-area
# cutover (platform/decisions/0059): once bin/install-orient's plists set
# these variables, a relative value would resolve against the process's
# current working directory at import time — reintroducing exactly the
# working-directory dependence this module exists to remove. paths.py is the
# only reader of either variable, so it is the one place that can catch this
# regardless of which future caller sets it.


def _run_with_env_expect_failure(env_overrides: dict) -> subprocess.CompletedProcess:
    code = "import paths"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, env=env_overrides, capture_output=True, text=True, check=False,
    )


def test_a_relative_config_root_is_refused():
    import os
    env = dict(os.environ)
    env["ORIENT_CONFIG_ROOT"] = "relative/config/dir"
    result = _run_with_env_expect_failure(env)
    assert result.returncode != 0
    assert "must be an absolute path" in result.stderr


def test_a_relative_state_root_is_refused():
    import os
    env = dict(os.environ)
    env["ORIENT_STATE_ROOT"] = "relative/state/dir"
    result = _run_with_env_expect_failure(env)
    assert result.returncode != 0
    assert "must be an absolute path" in result.stderr


# --- every config/credential file resolves through CONFIG_ROOT ---------------


def test_tiers_and_jwt_config_resolve_through_config_root():
    assert access_identity.TIERS == paths.CONFIG_ROOT / "tiers.json"
    assert access_identity.JWT_CONFIG == paths.CONFIG_ROOT / "jwt.json"


def test_api_tokens_resolves_through_config_root():
    assert api_common.API_TOKENS == paths.CONFIG_ROOT / "api-tokens.json"


def test_roles_resolves_through_config_root():
    assert authz.ROLES == paths.CONFIG_ROOT / "roles.json"


def test_concierge_config_resolves_through_config_root_by_default():
    """A fresh interpreter, so this does not leave `concierge` reloaded with
    changed module state for tests later in the session (test_room_concierge.py
    monkeypatches concierge.CONFIG directly and would be unaffected either way,
    but reloading a shared module as a side effect of one test is its own
    hazard worth not introducing)."""
    import os
    env = dict(os.environ)
    env.pop("ORIENT_CONCIERGE_CONFIG", None)
    code = "import concierge; print(concierge.CONFIG)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == str(paths.CONFIG_ROOT / "concierge.json")


# --- every site-output / log writer resolves through STATE_ROOT --------------


@pytest.mark.real_access_log
def test_server_site_and_access_log_resolve_through_state_root():
    assert srv.SITE == paths.STATE_ROOT / "site"
    assert srv.ACCESS_LOG == paths.STATE_ROOT / "access.log"


def test_build_out_resolves_through_state_root():
    assert build.OUT == paths.STATE_ROOT / "site"


def test_assets_still_travels_with_the_code_not_state_root():
    """ASSETS is code (brand images shipped with the repo), not state — it must
    stay anchored on the module's own directory even after this change."""
    assert srv.ASSETS == REPO_ROOT / "assets"


# --- resolve_tool: production deploy surface vs the developer symlink farm ---
#
# issue_01M1DKJYHSH9FT1SBP9068MTT1: two deploy-pinned launchd jobs reached a
# platform tool through `~/.local/bin`, the DEVELOPER symlink farm into the
# agent-platform working checkout — the exact failure ADR-0059's deploy-area
# cutover exists to prevent. `resolve_tool` must prefer the production deploy
# area (`_DEPLOY_TOOL_BIN`) when it holds an executable copy of the tool, and
# fall back to ordinary PATH resolution otherwise (a developer checkout or CI,
# where the deploy area doesn't exist).


def test_resolve_tool_prefers_the_deployed_copy_when_present_and_executable(tmp_path, monkeypatch):
    deploy_bin = tmp_path / "deploy-bin"
    deploy_bin.mkdir()
    tool = deploy_bin / "sometool"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    monkeypatch.setattr(paths, "_DEPLOY_TOOL_BIN", deploy_bin)
    monkeypatch.setattr(paths.shutil, "which", lambda name: (_ for _ in ()).throw(
        AssertionError("must not fall back to PATH when the deployed copy resolves")))
    assert paths.resolve_tool("sometool") == str(tool)


def test_resolve_tool_falls_back_to_path_when_the_deployed_copy_is_absent(tmp_path, monkeypatch):
    deploy_bin = tmp_path / "deploy-bin"  # never created
    monkeypatch.setattr(paths, "_DEPLOY_TOOL_BIN", deploy_bin)
    monkeypatch.setattr(paths.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert paths.resolve_tool("sometool") == "/usr/local/bin/sometool"


def test_resolve_tool_falls_back_to_path_when_the_deployed_copy_is_not_executable(tmp_path, monkeypatch):
    deploy_bin = tmp_path / "deploy-bin"
    deploy_bin.mkdir()
    tool = deploy_bin / "sometool"
    tool.write_text("not executable")
    tool.chmod(0o644)
    monkeypatch.setattr(paths, "_DEPLOY_TOOL_BIN", deploy_bin)
    monkeypatch.setattr(paths.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert paths.resolve_tool("sometool") == "/usr/local/bin/sometool"


def test_resolve_tool_returns_none_when_neither_the_deploy_area_nor_path_has_it(tmp_path, monkeypatch):
    deploy_bin = tmp_path / "deploy-bin"  # never created
    monkeypatch.setattr(paths, "_DEPLOY_TOOL_BIN", deploy_bin)
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    assert paths.resolve_tool("sometool") is None


def test_resolve_tool_ignores_a_directory_of_that_name_in_the_deploy_area(tmp_path, monkeypatch):
    """A directory happening to share the tool's name (unlikely, but is.file()
    is the actual guard) must not be treated as the executable."""
    deploy_bin = tmp_path / "deploy-bin"
    deploy_bin.mkdir()
    (deploy_bin / "sometool").mkdir()
    monkeypatch.setattr(paths, "_DEPLOY_TOOL_BIN", deploy_bin)
    monkeypatch.setattr(paths.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert paths.resolve_tool("sometool") == "/usr/local/bin/sometool"


# --- a second account's install never reaches into another person's home ---
#
# `_DEPLOY_TOOL_BIN` once named the operator's home literally. Shipwright's rehearsal
# (account builder-rehearsal on macmini, 2026-10-02) died at import with
# `PermissionError` from `resolve_tool`'s `is_file()`, because stat on another
# person's unreadable home raises EACCES rather than returning False. The same
# literal shape sat in `roster_build.PROJECTS` and `crew_registry.REGISTRY`.


_FOREIGN_HOME_PROBE = """
import json, paths, crew_registry, roster_build
print(json.dumps({
    "deploy_bin": str(paths._DEPLOY_TOOL_BIN),
    "kb_eval": paths.resolve_tool("kb-eval"),
    "registry": str(crew_registry.REGISTRY),
    "repos": [repo for _, _, repo, _ in roster_build.PROJECTS],
    "repo_agents": [roster_build.load_repo_agents(repo) for _, _, repo, _ in roster_build.PROJECTS],
}))
"""


def test_a_second_account_resolves_every_path_under_its_own_home(tmp_path):
    """Run as another account would, with HOME set to that account's home.
    Every path resolves under it, so nothing stats a path in someone else's
    home. Against the literal `/Users/<operator>/...` paths this fails on the
    first assertion."""
    import json
    import os
    home = tmp_path / "adopter"
    own_bin = home / ".local" / "deploy" / "agent-platform-shared" / "bin"
    own_bin.mkdir(parents=True)
    (own_bin / "kb-eval").write_text("#!/bin/sh\n")
    (own_bin / "kb-eval").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "PROJECTS_JSON"}
    env["HOME"] = str(home)
    out = subprocess.run([sys.executable, "-c", _FOREIGN_HOME_PROBE], cwd=REPO_ROOT,
                         env=env, capture_output=True, text=True, check=True).stdout
    got = json.loads(out)
    assert got["deploy_bin"] == str(own_bin)
    assert got["kb_eval"] == str(own_bin / "kb-eval")
    assert got["registry"] == str(home / "Convivy" / "agent-platform" / "config" / "projects.json")
    assert got["repos"] and all(r.startswith(str(home / "Convivy") + "/") for r in got["repos"])
    assert got["repo_agents"] == [{} for _ in got["repos"]]
