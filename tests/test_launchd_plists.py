"""launchd/*.plist must be well-formed XML (issue_01M08YADC32YYE5S0K2N033AES).

`plistlib` is stricter than `launchctl`, which tolerated a bare `--` inside an
XML comment in one of the capture plists for who knows how long — nothing
caught it until someone tried to parse the file rather than load it.
This is exactly the kind of defect a parser catches for free and a reader
does not, so it is pinned here rather than left to be found again by hand.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

LAUNCHD_DIR = Path(__file__).resolve().parent.parent / "launchd"


def _plists():
    return sorted(LAUNCHD_DIR.glob("*.plist"))


def test_launchd_dir_is_not_empty():
    assert _plists(), f"no .plist files found in {LAUNCHD_DIR} — the glob is looking in the wrong place"


@pytest.mark.parametrize("path", _plists(), ids=lambda p: p.name)
def test_plist_is_well_formed(path):
    with path.open("rb") as f:
        plistlib.load(f)  # raises on malformed XML; the assertion is that it doesn't


# --- the repo copy must describe what is actually deployed ---------------------
#
# The other half of issue_01M08YADC32YYE5S0K2N033AES: the vault_plane
# capture jobs' deployed plists all pin VAULTS_ON_LIBRARY=1, and the repo
# copies had silently drifted to carry no such key — so reinstalling from the
# repo would have rolled the write cutover back for all of them. This can't
# check the LIVE ~/Library/LaunchAgents copy (this repo doesn't know the
# deploy host's layout), but it holds the repo's own files to the one rule
# that matters: whichever plists run a vault_plane capture job must pin
# VAULTS_ON_LIBRARY explicitly, not rely on the (correct, but silent) default.
# com.convivy.orient-market-digest.plist retired with /market (the operator, 2026-08-26)
# and its plist was removed from the repo along with it — the operator is unloading the
# live launchd job separately.
_VAULT_PLANE_JOBS = {
    "com.convivy.orient-github-capture.plist",
    "com.convivy.orient-deploy-watch.plist",
}


@pytest.mark.parametrize("name", sorted(_VAULT_PLANE_JOBS))
def test_vault_plane_capture_job_pins_vaults_on_library(name):
    path = LAUNCHD_DIR / name
    with path.open("rb") as f:
        plist = plistlib.load(f)
    env = plist["EnvironmentVariables"] if "EnvironmentVariables" in plist else {}
    # Not env.get("VAULTS_ON_LIBRARY"): that shape is exactly what
    # test_vault_plane.py's sweep flags as a client resolving a plane variable
    # for itself, and this is neither a client nor a runtime decision — it's
    # asserting on one already-written XML file's declared value.
    pinned = env["VAULTS_ON_LIBRARY"] if "VAULTS_ON_LIBRARY" in env else None
    assert pinned == "1", (
        f"{name} must pin VAULTS_ON_LIBRARY=1 to match what's deployed — "
        f"an unpinned copy would silently roll the write cutover back to "
        f"Orient's retired store if ever reinstalled from this repo")


# --- ADR-0059 deploy-area cutover (bin/install-orient) -----------------------
#
# The seven jobs the epic measured (issue_01M19RSV6PNDR562PHXTDD80E5). Six move
# their Program/WorkingDirectory to the deploy area bin/install-orient stages;
# com.convivy.orient-deploy-watch is deliberately excluded here — its plist is
# still versioned (rule 8) but its CONTENT stays pointed at the development
# checkout, because ADR-0059 leaves the deploy-watch mechanism's future to the
# Bosun crew (see this PR's body). Asserting it here would either force that
# decision early or force this test to special-case it, and the second is what
# `_CUTOVER_JOBS` below already does by omission.
DEPLOY_DIR = "__HOME__/.local/deploy/convivy-orient"
PRIMARY_CHECKOUT = "__HOME__/Convivy/convivy-orient"

_CUTOVER_JOBS = {
    "com.convivy.orient.plist",
    "com.convivy.orient-dashboard.plist",
    "com.convivy.code-health.plist",
    "com.convivy.kb-observability.plist",
    "com.convivy.orient-github-capture.plist",
}


@pytest.mark.parametrize("name", sorted(_CUTOVER_JOBS))
def test_cutover_job_points_at_the_deploy_area_not_the_checkout(name):
    path = LAUNCHD_DIR / name
    with path.open("rb") as f:
        plist = plistlib.load(f)

    working_dir = plist.get("WorkingDirectory", "")
    assert working_dir == DEPLOY_DIR, (
        f"{name}'s WorkingDirectory is {working_dir!r}, not the deploy area — "
        f"ADR-0059 rule 1")

    program_args = plist.get("ProgramArguments", [])
    joined = " ".join(program_args)
    assert PRIMARY_CHECKOUT not in joined, (
        f"{name}'s ProgramArguments still names the development checkout: {program_args!r}")
    assert DEPLOY_DIR in joined, (
        f"{name}'s ProgramArguments never names the deploy area: {program_args!r}")


# com.convivy.orient itself execs /bin/sh serve.sh, not the venv directly —
# serve.sh resolves its own PY from $DIR/.venv/bin/python internally (asserted
# in test_serve_check.py), so it is excluded from the direct-interpreter check
# below rather than given a special-cased assertion here.
_DIRECT_INTERPRETER_JOBS = _CUTOVER_JOBS - {"com.convivy.orient.plist"}


@pytest.mark.parametrize("name", sorted(_DIRECT_INTERPRETER_JOBS))
def test_cutover_job_runs_the_deploy_areas_own_venv_not_the_shared_one(name):
    """rule 4: the interpreter travels with the code. No cutover job may exec
    the machine-wide ~/.local/venv-orient (three jobs did) or the bare system
    interpreter (three others did, a live divergence this migration closes)."""
    path = LAUNCHD_DIR / name
    with path.open("rb") as f:
        plist = plistlib.load(f)

    interpreter = plist.get("ProgramArguments", [""])[0]
    assert interpreter == f"{DEPLOY_DIR}/.venv/bin/python", (
        f"{name} execs {interpreter!r}, not the deploy area's own venv")


def test_orient_plist_execs_serve_sh_in_the_deploy_area():
    path = LAUNCHD_DIR / "com.convivy.orient.plist"
    with path.open("rb") as f:
        plist = plistlib.load(f)
    program_args = plist.get("ProgramArguments", [])
    assert program_args == ["/bin/sh", f"{DEPLOY_DIR}/serve.sh"]


@pytest.mark.parametrize("name", sorted(_CUTOVER_JOBS))
def test_cutover_job_pins_config_and_state_root_to_the_primary_checkout(name):
    """rule 3: config/credentials and state/output stay in the primary
    checkout, explicitly pinned rather than left to paths.py's default — a
    process running from the deploy area with these unset would default both
    roots to the deploy area itself, exactly the leak rule 3 forbids."""
    path = LAUNCHD_DIR / name
    with path.open("rb") as f:
        plist = plistlib.load(f)
    env = plist.get("EnvironmentVariables", {})
    assert env.get("ORIENT_CONFIG_ROOT") == PRIMARY_CHECKOUT, (
        f"{name} does not pin ORIENT_CONFIG_ROOT to the primary checkout")
    assert env.get("ORIENT_STATE_ROOT") == PRIMARY_CHECKOUT, (
        f"{name} does not pin ORIENT_STATE_ROOT to the primary checkout")


def test_deploy_watch_plist_is_deliberately_left_pointed_at_the_checkout():
    """The one job ADR-0059 leaves unsettled (Bosun crew's call) — this pins
    down that it is unchanged, so a future edit that silently moves it either
    updates this test on purpose or is caught here first."""
    path = LAUNCHD_DIR / "com.convivy.orient-deploy-watch.plist"
    with path.open("rb") as f:
        plist = plistlib.load(f)
    assert plist.get("WorkingDirectory") == PRIMARY_CHECKOUT
    assert PRIMARY_CHECKOUT in " ".join(plist.get("ProgramArguments", []))
