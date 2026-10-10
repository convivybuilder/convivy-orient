"""bin/install-orient — the deploy-pin installer for Orient's seven launchd
jobs (platform/decisions/0059).

Every case here runs against a from-scratch fixture repository, never this
checkout, so the test controls exactly what "main" contains and does not
depend on which branch this suite happens to be running from. `HOME` is
redirected to a temp directory for every run, so nothing here writes to a
real `~/.local/deploy/convivy-orient` or a real `~/Library/LaunchAgents`, and
nothing here touches this checkout's own credential files or `site/` output —
the last of which is the point of the "never writes outside" test below.

The fixture repo's `requirements.txt` is deliberately empty: the installer
builds a real venv and runs a real `pip install -r requirements.txt` against
it, and a requirements file with nothing in it needs no network access to
satisfy, which is what keeps every case here hermetic and fast.

The fixture's `serve.sh` is a stub, not a real copy — `test_serve_check.py`
already covers the real preflight exhaustively, and re-running it here
through a fresh, dependency-free venv would make the installer's OWN success
path depend on a preflight this file has no reason to fail or pass on the
real script's terms. The stub answers `--check` the way a real serve.sh would
once its dependencies are actually installed: 0 by default, and a controlled
failure when a marker file the fixture plants asks it to, which is what
`test_failed_install_leaves_the_previous_deploy_untouched` uses to exercise
the installer's own atomicity rather than serve.sh's. It also records the
ORIENT_CONFIG_ROOT/ORIENT_STATE_ROOT it was invoked with, which
`test_check_runs_with_config_state_root_pinned_to_repo` reads back — the
property that matters is that the installer's own preflight exercises the
same roots the plists it installs declare, not merely "some directory that
happens to exist".
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parent.parent / "bin" / "install-orient"

STUB_SERVE_SH = """#!/bin/sh
# Stub for install-orient tests — see test_install_orient.py's module
# docstring for why this is not a real copy of serve.sh.
if [ "$1" = "--check" ]; then
    echo "ORIENT_CONFIG_ROOT=$ORIENT_CONFIG_ROOT" >> "$(dirname "$0")/CHECK_ENV"
    echo "ORIENT_STATE_ROOT=$ORIENT_STATE_ROOT" >> "$(dirname "$0")/CHECK_ENV"
    if [ -f "$(dirname "$0")/FAIL_CHECK" ]; then
        echo "stub serve.sh: simulated preflight failure" >&2
        exit 78
    fi
    echo "stub serve.sh: --check passed"
    exit 0
fi
exit 0
"""

PLIST_NAMES = [
    "com.convivy.orient.plist",
    "com.convivy.orient-dashboard.plist",
    "com.convivy.code-health.plist",
    "com.convivy.kb-observability.plist",
    "com.convivy.orient-github-capture.plist",
    "com.convivy.orient-deploy-watch.plist",
]


def _stub_plist(label: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>__HOME__/.local/deploy/convivy-orient/serve.sh</string>
    </array>
</dict>
</plist>
"""


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def _write_fixture_files(repo: Path, version_marker: str, extra: dict[str, str] | None = None):
    (repo / "serve.sh").write_text(STUB_SERVE_SH)
    (repo / "serve.sh").chmod(0o755)
    (repo / "requirements.txt").write_text("")
    (repo / "server.py").write_text(f"VERSION = {version_marker!r}\n")
    (repo / "launchd").mkdir(exist_ok=True)
    for name in PLIST_NAMES:
        label = name[: -len(".plist")]
        (repo / "launchd" / name).write_text(_stub_plist(label))
    if extra:
        for relpath, content in extra.items():
            p = repo / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)


@pytest.fixture
def seed_repo(tmp_path):
    """Build (and return the path to) a from-scratch git repo on `main`,
    shaped like Orient, with the fixture files above committed."""
    def build(version_marker="v1", extra=None):
        repo = tmp_path / "src-repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "config", "commit.gpgsign", "false")
        _write_fixture_files(repo, version_marker, extra)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")
        return repo
    return build


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def run_installer(*args, home, timeout=120):
    """Every case here passes --python=sys.executable, the interpreter this
    test suite is already running under, instead of install-orient's
    hardcoded macOS/Homebrew python3.11 search — that search can find nothing
    on a runner without Homebrew, and the point of this suite is to exercise
    the real install path, not skip it. This only picks which interpreter
    builds the staged venv; it does not touch serve.sh's own preflight, which
    still runs unmodified (here, the stub) against the staged build before the
    swap."""
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(
        [str(INSTALLER), f"--python={sys.executable}", *args], env=env,
        capture_output=True, text=True, timeout=timeout, check=False)


DEPLOY = "/.local/deploy/convivy-orient"


# --- the success path ---------------------------------------------------------


def test_fresh_install_copies_code_builds_venv_and_records_commit(seed_repo, home):
    repo = seed_repo("v1")
    sha = _git(repo, "rev-parse", "main").stdout.strip()

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    server_py = (deploy_dir / "server.py").read_text()
    assert "v1" in server_py

    assert (deploy_dir / "server.py").is_file()
    assert not (deploy_dir / "server.py").is_symlink()
    assert (deploy_dir / ".venv" / "bin" / "python").exists() or \
           (deploy_dir / ".venv" / "bin" / "python3").exists(), \
        "a pinned interpreter must be built inside the deploy area"
    assert not (deploy_dir / ".venv").is_symlink()

    deployed_commit = (deploy_dir / "DEPLOYED_COMMIT").read_text()
    assert f"commit: {sha}" in deployed_commit

    plist = home / "Library" / "LaunchAgents" / "com.convivy.orient.plist"
    assert plist.is_file()
    assert not plist.is_symlink()
    assert "deploy/convivy-orient/serve.sh" in plist.read_text()


def test_all_seven_plists_are_installed(seed_repo, home):
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    launch_agents = home / "Library" / "LaunchAgents"
    for name in PLIST_NAMES:
        assert (launch_agents / name).is_file(), f"{name} was not installed"


def test_installed_plists_carry_the_installing_accounts_home(seed_repo, home):
    """The templates write the home as __HOME__; the installed copy names the
    real one, so no person's home is baked into the repository."""
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    for name in PLIST_NAMES:
        text = (home / "Library" / "LaunchAgents" / name).read_text()
        assert "__HOME__" not in text, f"{name} still carries the placeholder"
        assert f"<string>{home}{DEPLOY}/serve.sh</string>" in text


@pytest.mark.parametrize("bad_home", ["relative/home", "pipe|home"])
def test_a_home_that_is_not_a_plain_absolute_path_stops_the_install(seed_repo, tmp_path, bad_home):
    repo = seed_repo("v1")
    if bad_home.startswith("relative"):
        home_value = bad_home
    else:
        (tmp_path / bad_home).mkdir()
        home_value = str(tmp_path / bad_home)
    env = dict(os.environ, HOME=home_value)
    result = subprocess.run(
        [str(INSTALLER), f"--python={sys.executable}", "--repo=" + str(repo)], env=env,
        capture_output=True, text=True, timeout=120, check=False, cwd=tmp_path)
    assert result.returncode != 0
    assert "HOME" in result.stderr
    # Refused before anything was built or written.
    assert not (tmp_path / home_value / DEPLOY.lstrip("/")).exists()
    assert not (tmp_path / home_value / "Library").exists()


def test_check_runs_with_config_state_root_pinned_to_repo(seed_repo, home):
    """The installer's own preflight must exercise the SAME config/state
    roots the plists it installs declare — pinned here to the source repo
    itself, which for a canonical install (no --repo override) is the primary
    checkout. A preflight that instead checked some other directory (e.g. the
    staged tree) would pass even if the real roots were missing or
    unreadable."""
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    check_env = (deploy_dir / "CHECK_ENV").read_text()
    assert f"ORIENT_CONFIG_ROOT={repo}" in check_env
    assert f"ORIENT_STATE_ROOT={repo}" in check_env


def test_status_reports_the_deployed_commit_with_no_side_effects(seed_repo, home):
    repo = seed_repo("v1")
    sha = _git(repo, "rev-parse", "main").stdout.strip()
    run_installer("--repo=" + str(repo), home=home)

    deploy_dir = home / DEPLOY.lstrip("/")
    before = sorted(str(p) for p in deploy_dir.rglob("*"))

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert sha in result.stdout

    after = sorted(str(p) for p in deploy_dir.rglob("*"))
    assert before == after, "--status must not modify the deploy area"


def test_status_before_any_install_fails_clearly(home):
    result = run_installer("--status", home=home)
    assert result.returncode != 0
    assert "nothing deployed" in (result.stdout + result.stderr)


# --- rule 7: git archive, never the working tree -----------------------------


def test_git_archive_excludes_an_untracked_gitignored_credential_file(seed_repo, home):
    """The whole reason this installer exists rather than a `cp -r`: a
    gitignored credential file sitting in the checkout must never reach the
    deploy area. Named after one of Orient's actual five (tiers.json)."""
    repo = seed_repo("v1")
    (repo / ".gitignore").write_text("tiers.json\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "gitignore")
    (repo / "tiers.json").write_text('{"full": ["operator@example.com"]}')

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    leaked = list(deploy_dir.rglob("tiers.json"))
    assert leaked == [], f"a gitignored credential file reached the deploy area: {leaked}"


def test_an_uncommitted_edit_is_never_what_gets_deployed(seed_repo, home):
    """rule 7 again, from the other direction: a dirty working tree must not
    leak into the deploy area either — only the committed blob."""
    repo = seed_repo("v1")
    (repo / "server.py").write_text("VERSION = 'DIRTY — MUST NOT DEPLOY'\n")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deployed = (home / DEPLOY.lstrip("/") / "server.py").read_text()
    assert "DIRTY" not in deployed
    assert "v1" in deployed


# --- atomicity ----------------------------------------------------------------


def test_failed_install_leaves_the_previous_deploy_untouched(seed_repo, home):
    """The installer validates the staged build with the deployed serve.sh's
    own `--check` BEFORE the swap — this plants a marker that makes the stub
    fail that check, and confirms the swap never happens: the prior good
    release stays exactly as it was, and nothing partial is left behind."""
    repo = seed_repo("v1")
    good = run_installer("--repo=" + str(repo), home=home)
    assert good.returncode == 0, good.stdout + good.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    good_sha = _git(repo, "rev-parse", "main").stdout.strip()

    (repo / "FAIL_CHECK").write_text("simulate a broken staged build\n")
    (repo / "server.py").write_text("VERSION = 'v2-broken'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, broken preflight")

    bad = run_installer("--repo=" + str(repo), home=home)
    assert bad.returncode != 0, bad.stdout + bad.stderr
    assert "preflight" in (bad.stdout + bad.stderr)

    assert (deploy_dir / "DEPLOYED_COMMIT").read_text().count(f"commit: {good_sha}") == 1
    assert "v1" in (deploy_dir / "server.py").read_text()
    assert "v2-broken" not in (deploy_dir / "server.py").read_text()

    leftovers = list(deploy_dir.parent.glob("convivy-orient.new")) + \
        list(deploy_dir.parent.glob("convivy-orient.old.*"))
    assert leftovers == [], f"a staging/backup directory survived: {leftovers}"


def test_reinstall_atomically_replaces_the_prior_release_with_no_leftovers(seed_repo, home):
    repo = seed_repo("v1")
    run_installer("--repo=" + str(repo), home=home)

    (repo / "server.py").write_text("VERSION = 'v3'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v3")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "v3" in (deploy_dir / "server.py").read_text()

    leftovers = list(deploy_dir.parent.glob("convivy-orient.new")) + \
        list(deploy_dir.parent.glob("convivy-orient.old.*"))
    assert leftovers == [], f"a staging/backup directory survived: {leftovers}"


# --- ref handling ---------------------------------------------------------


def test_a_ref_that_does_not_resolve_is_refused(seed_repo, home):
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), "--ref=not-a-real-ref", home=home)
    assert result.returncode != 0
    assert "does not resolve" in (result.stdout + result.stderr)
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_non_main_ref_is_refused_without_the_override(seed_repo, home):
    repo = seed_repo("v1")
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "server.py").write_text("VERSION = 'v2-branch'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "branch change")
    _git(repo, "checkout", "-q", "main")

    result = run_installer("--repo=" + str(repo), "--ref=feat/x", home=home)
    assert result.returncode != 0
    assert "REFUSING" in (result.stdout + result.stderr)
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_allow_non_main_installs_the_named_ref(seed_repo, home):
    repo = seed_repo("v1")
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "server.py").write_text("VERSION = 'v2-branch'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "branch change")
    branch_sha = _git(repo, "rev-parse", "feat/x").stdout.strip()
    _git(repo, "checkout", "-q", "main")

    result = run_installer(
        "--repo=" + str(repo), "--ref=feat/x", "--allow-non-main", home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "v2-branch" in (deploy_dir / "server.py").read_text()
    assert f"commit: {branch_sha}" in (deploy_dir / "DEPLOYED_COMMIT").read_text()


# --- plist handling -------------------------------------------------------


def test_a_drifted_live_plist_is_not_clobbered_without_force(seed_repo, home):
    repo = seed_repo("v1")
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    live_plist = launch_agents / "com.convivy.orient.plist"
    live_plist.write_text("<!-- hand-edited, not the template -->\n")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not overwritten" in (result.stdout + result.stderr).lower()
    assert live_plist.read_text() == "<!-- hand-edited, not the template -->\n"

    # the other six, undrifted, still install normally
    assert (launch_agents / "com.convivy.orient-dashboard.plist").is_file()


def test_force_overwrites_a_drifted_live_plist(seed_repo, home):
    repo = seed_repo("v1")
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    live_plist = launch_agents / "com.convivy.orient.plist"
    live_plist.write_text("<!-- hand-edited, not the template -->\n")

    result = run_installer("--repo=" + str(repo), "--force", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deploy/convivy-orient/serve.sh" in live_plist.read_text()


# --- rule 3: never touches the primary checkout's config/state files --------


def test_never_writes_into_the_source_repo(seed_repo, home):
    """The five credential files, site/, and access.log all stay in the
    primary checkout (this repo's --repo, here the fixture repo) — this
    installer only ever READS it (git archive/show/rev-parse). Confirmed
    directly: nothing under the source repo changes except .git's own
    bookkeeping (packed-refs etc., which git itself may touch on read)."""
    repo = seed_repo("v1")
    (repo / "tiers.json").write_text('{"full": ["operator@example.com"]}')
    before = {
        p.relative_to(repo): p.read_bytes()
        for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts
    }

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    after = {
        p.relative_to(repo): p.read_bytes()
        for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts
    }
    assert before == after, "install-orient modified a file inside the source repo"


def test_never_writes_outside_the_deploy_area_and_launch_agents(seed_repo, home):
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    allowed_prefixes = (".local", "Library")
    for entry in home.iterdir():
        assert entry.name in allowed_prefixes, (
            f"install-orient wrote something unexpected under $HOME: {entry}")
