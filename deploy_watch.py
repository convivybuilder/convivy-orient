#!/usr/bin/env python3.11
"""Automated deploy watcher (issue_01M0EERRFC8NJ8QS5Q6EEE87JY).

Closes the gap that let the live site sit stale for three days (2026-08-19
incident): merging a PR on GitHub does nothing to the mini's checkout by
itself, and pulling the checkout does nothing to the long-running `server.py`
process, which imports every module once at start. Both steps were manual and
nothing signalled when they were overdue.

Every run (launchd, StartInterval): fetch `origin/main`, fast-forward the
checkout if it's behind, then compare the checkout's HEAD against the commit
`serve.sh` last recorded itself serving (`~/.orient-control/orient-serving-
commit`, written just before `server.py` execs — see serve.sh). A mismatch
covers BOTH failure modes from the incident: a checkout that just got pulled
but never restarted (by this script or by a human running `git pull` by hand),
and a checkout freshly fast-forwarded by this run. Either way, kickstart the
launchd job and wait for the new marker to land AND the port to accept
connections before calling it done — "process exists" isn't verification;
serve.sh only writes the marker after every fatal startup step succeeded.

A fast-forward that fails (diverged history, a conflicting local change) is
never forced — it's reported and left for a human, same as a restart that
times out. Both failure paths POST a `deploy_failure` signal (visible at
/archives) AND exit 1, so a stuck launchd job shows up in its own error log
even if the signal POST itself can't reach a wedged server.

Refuses outright if the checkout isn't on `main` (incident, 2026-08-21): a
feature branch checked out in this same directory is an ancestor of
origin/main whenever it hasn't diverged, so `git merge --ff-only origin/main`
trivially succeeds ("Already up to date") without moving HEAD off it — and
this script went on to kickstart the live server onto unmerged, in-progress
commits, six times over ~45 minutes before anyone noticed. The working tree
the rest of this script assumes is `main` is a precondition worth checking on
every run, not an invariant worth trusting.

ADR-0059's deploy-area cutover (epic issue_01M19RSV6PNDR562PHXTDD80E5) moved
what `server.py` execs from this checkout to a separate area built by
`bin/install-orient` (`~/.local/deploy/convivy-orient`, its own pinned venv).
Fast-forwarding this checkout and kickstarting the launchd job no longer
reaches the running process at all — it execs from the deploy area, which
only `bin/install-orient` writes. So this cycle now reconciles three
independently-stale things, not two: the checkout against `origin/main`
(fast-forward), the deploy area against the checkout (install, read from the
deploy area's own `DEPLOYED_COMMIT` file), and the running server against the
checkout (restart, same served-marker check as before). All three can be
true in one cycle — the common case is a merge that needs installing and
then restarting — so `decide_action` returns independent flags rather than a
single next-action value; see its docstring for the four-hash state space.
`bin/install-orient` itself is never touched by this script's own
fast-forward target the way `server.py` is: it's read fresh from the
checkout every cycle (see `run_install`'s docstring for why that has to stay
true even when the installer is what's broken).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import vault_plane

# launchd runs this file from the checkout itself, so the checkout is its own
# directory, wherever the operator cloned it. bin/install-orient also copies
# this file into DEPLOY_DIR, and that copy must never be what launchd runs:
# REPO_DIR would then be the deploy area, which has no .git, and run_install()
# would execute the deployed installer (agent-platform#469). The plist names
# the checkout's copy.
REPO_DIR = Path(__file__).resolve().parent
SERVED_MARKER = Path.home() / ".orient-control" / "orient-serving-commit"
STARTED_MARKER = Path.home() / ".orient-control" / "orient-restart-started"
TOKEN_FILE = Path.home() / ".orient-control" / "capture-token"

PRODUCT = "convivy-orient"
DEPLOY_DIR = Path.home() / ".local" / "deploy" / PRODUCT
DEPLOYED_COMMIT_FILE = DEPLOY_DIR / "DEPLOYED_COMMIT"

LAUNCHD_LABEL = "com.convivy.orient"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8099

INSTALL_TIMEOUT_S = 600  # bin/install-orient builds a fresh venv from
# requirements.txt every run (git archive, pip install, serve.sh --check) —
# nothing here touches the network beyond pip's index, but a cold pip cache
# or a slow index has no ceiling like a local git/launchctl call does, so
# this gets its own generous timeout rather than reusing LOCAL_TIMEOUT_S.
# com.convivy.orient-deploy-watch's StartInterval is 300s; launchd will not
# start a second instance of a label that's still running (it's StartInterval,
# not cron), so a cycle that runs long — including one that spends most of
# this timeout building a venv — delays the next scheduled kick rather than
# racing it. 600s intentionally exceeds StartInterval for that reason.

# The installer's own refusal line (bin/install-orient, the plist-copy loop)
# when a live plist has drifted from its template — it exits 0 either way, so
# this is the only way to notice the drift happened. Grepped verbatim rather
# than retyped so a wording change in the installer doesn't silently stop
# matching.
PLIST_REFUSED_MARKER = "live plist differs from the template — NOT overwritten"

STEP_FAST_FORWARD = "fast_forward"
STEP_INSTALL = "install"
STEP_RESTART = "restart"

# issue_01M0Y94CXQ8S1QPMWS319YS0JN: the 90s above was an estimate, and it was
# wrong by 2.5x — serve.sh writes the *served* marker only after nine
# builders and an alembic migration, and on 2026-08-25, PR #208's own merge
# took 236s from kickstart to that marker (deploy_watch's own alert
# timestamps: 22:29:47 merge, 22:34:03 alert at the old 150s ceiling,
# 22:35:29 marker landed — 86s after the false alarm). serve.sh now also
# writes a *started* marker (see serve.sh) as the very first thing a real
# start does, before any builder runs — measured here, on this machine, with
# ORIENT_STATE_ROOT/ORIENT_CONTROL_DB pointed at scratch copies so the timing
# run touched no production state: cold imports + the KB-index preflight read
# take ~0.6s, and the full nine-builder chain + `alembic upgrade head`
# together take ~9s against real production-scale data (373 docs, 2511
# activity sessions, 3305 ledger facts) when the machine is otherwise idle.
# The ~26x gap between that clean run and the 236s observed live is
# contention on a shared dev machine, not a cost the code itself pays — which
# is also why a single fixed ceiling covering the observed worst case is
# fragile (a busier night can exceed any number picked this way) and the
# better fix is what these two constants now do instead.
#
# RESTART_TIMEOUT_S now bounds only "did serve.sh even reach the started
# marker" — cheap, local, bounded work (imports, one sqlite read, one
# alembic-chain resolution, no network) that took ~0.6s clean. 60s leaves
# ~100x headroom for contention while still catching a real early failure
# (crash loop, broken venv, an unresolvable migration chain) in under a
# quarter of the old ceiling instead of after it.
RESTART_TIMEOUT_S = 60
# RESTART_CONVERGING_GRACE_S bounds "did the builder chain and migration
# finish once we know the new process launched" — this is the phase that
# took 236s live. 360s gives that ~1.5x headroom over the one live measurement
# we have, on the same reasoning: real restarts run under real contention,
# not the ~9s clean-machine figure above.
RESTART_CONVERGING_GRACE_S = 360
# Total worst-case time to an alert is now 420s (up from 300s) — still inside
# a single com.convivy.orient-deploy-watch cycle's own docstring precedent
# (INSTALL_TIMEOUT_S already exceeds this job's 300s StartInterval; a cycle
# that runs long delays the next scheduled kick rather than racing it, same
# as here). A genuinely wedged deploy — serve.sh never reaches the started
# marker at all — now surfaces in RESTART_TIMEOUT_S (60s), faster than
# before; a deploy that starts but never finishes surfaces at up to 420s,
# slower than before but no longer false-positiving on a healthy one.
RESTART_POLL_S = 5

# A single connect attempt reported a healthy server as down (observed: False,
# True, True, True across ~10s against a listener `lsof` confirmed live and
# `curl` answered throughout — issue_01M0TZTRK8JM09DD3N6CS6G95H). Retry a
# couple of times before trusting a closed result; this is a few hundred ms,
# not a timeout re-guess.
PORT_PROBE_ATTEMPTS = 3
PORT_PROBE_RETRY_S = 0.5

RESTART_REACHED = "reached"
RESTART_CONVERGING = "converging"
RESTART_WEDGED = "wedged"

# A hung git/launchctl call would otherwise block this launchd job indefinitely
# — no exit, no stderr, no signal — silently recreating the exact "nobody
# signalled it was overdue" failure this script exists to close, one layer
# down (review finding, PR #187). NETWORK for the one call that leaves the
# machine (fetch); LOCAL for everything else, which only touches the checkout
# or launchd and should never take long.
NETWORK_TIMEOUT_S = 120
LOCAL_TIMEOUT_S = 30


class DeployWatchError(Exception):
    """A step failed in a way that needs a human — never auto-retried harder."""


# --- pure decision -------------------------------------------------------------


def decide_action(checkout_head: str, origin_head: str, deployed_commit: str | None,
                   served_commit: str | None) -> frozenset[str]:
    """Which of three independent steps this cycle needs, given all four
    commit hashes this pipeline can now disagree about since ADR-0059 split
    "checked out" from "installed" from "running": the checkout (git HEAD),
    `origin/main`, what `bin/install-orient` last staged into the deploy area
    (`DEPLOYED_COMMIT_FILE`, or None if nothing's ever been installed there),
    and what the running server is actually serving (`SERVED_MARKER`, or None
    before serve.sh has ever written it).

    Returns a set, not a single next-action value, because more than one step
    can be needed in the same cycle — the common case is a merge that needs
    installing and then restarting — and a caller re-derives this against an
    updated checkout after fast-forwarding, so each flag has to be gettable
    on its own rather than folded into one enum (see `run`):

      STEP_FAST_FORWARD — checkout_head != origin_head: the checkout is
        behind origin, fast-forward it.
      STEP_INSTALL — deployed_commit != checkout_head: the deploy area
        doesn't match the checkout (nothing installed yet, or a fast-forward
        or a human's `git pull` moved the checkout since the last install).
      STEP_RESTART — served_commit != checkout_head: the running server
        isn't serving what the checkout (and, once installed, the deploy
        area) now holds.

    Empty iff all four commits already agree — including the state that used
    to loop forever before this rewrite: checkout == origin (no fast-forward
    needed) but deployed and served both still behind it, which used to read
    as "restart_only" and kickstart the live server onto whatever the deploy
    area already had, every 5 minutes, without ever installing the newer
    commit.
    """
    steps = set()
    if checkout_head != origin_head:
        steps.add(STEP_FAST_FORWARD)
    if deployed_commit != checkout_head:
        steps.add(STEP_INSTALL)
    if served_commit != checkout_head:
        steps.add(STEP_RESTART)
    return frozenset(steps)


# --- impure: git -----------------------------------------------------------------


def _run(argv: list[str], timeout_s: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False,
                               timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise DeployWatchError(f"{' '.join(argv)} timed out after {timeout_s}s") from None


def _git(*args: str, timeout_s: float = LOCAL_TIMEOUT_S) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(REPO_DIR), *args], timeout_s)


def git_head(ref: str = "HEAD") -> str:
    cp = _git("rev-parse", ref)
    if cp.returncode != 0:
        raise DeployWatchError(f"git rev-parse {ref} failed: {cp.stderr.strip()}")
    return cp.stdout.strip()


def git_fetch_main() -> None:
    cp = _git("fetch", "origin", "main", "--quiet", timeout_s=NETWORK_TIMEOUT_S)
    if cp.returncode != 0:
        raise DeployWatchError(f"git fetch origin main failed: {cp.stderr.strip()}")


def git_current_branch() -> str:
    """The checked-out branch name, or 'HEAD' in detached-HEAD state
    (`--abbrev-ref`'s own convention for that case — still a usable, distinct
    value, not an error)."""
    cp = _git("rev-parse", "--abbrev-ref", "HEAD")
    if cp.returncode != 0:
        raise DeployWatchError(f"git rev-parse --abbrev-ref HEAD failed: {cp.stderr.strip()}")
    return cp.stdout.strip()


def git_head_is_ancestor_of_origin_main() -> bool:
    """True iff HEAD holds no commit `origin/main` lacks — HEAD is
    `origin/main` or strictly behind it, so fast-forwarding onto it is safe.

    The branch-name check alone doesn't close the incident this guards
    against (security review, PR #189): a checkout can be on a branch
    literally named `main` that's ahead of origin (unpushed local commits —
    the same "developed directly in this checkout" mistake, just without
    also switching branches). `git merge --ff-only origin/main` treats that
    case as "Already up to date" too — HEAD never moves, and the unpushed
    commits stay live. Any non-zero exit (a real diverged/ahead state, OR
    the check itself failing for some other reason) reads as "not safe" —
    fail closed rather than assume safety we can't confirm."""
    cp = _git("merge-base", "--is-ancestor", "HEAD", "origin/main")
    return cp.returncode == 0


def git_ff_only_merge() -> None:
    """Fast-forward the checkout to origin/main. Never forces — a diverged
    history or a local change that would be overwritten fails the merge
    cleanly, with the working tree untouched, and that's reported as a
    failure rather than resolved automatically."""
    cp = _git("merge", "--ff-only", "origin/main")
    if cp.returncode != 0:
        raise DeployWatchError(
            f"git merge --ff-only origin/main failed (checkout may have diverged "
            f"or have local changes): {cp.stderr.strip()}")


# --- impure: restart + verification ---------------------------------------------


def read_served_commit() -> str | None:
    try:
        return SERVED_MARKER.read_text().strip()
    except OSError:
        return None


def read_started_commit() -> str | None:
    """serve.sh writes this the moment a real start begins, before the
    builder chain or the control_db migration run — evidence the new process
    launched, independent of whether it's finished (see serve.sh and
    RESTART_TIMEOUT_S above). Missing reads as None, same contract as
    read_served_commit: never mistaken for "already there".

    This file is only ever overwritten, never deleted — a crash that happens
    after this write but before the served marker leaves the SAME value on
    disk, unchanged, for every subsequent retry that targets the same commit
    (a crash-looping build always writes the identical SHA). Reading it in
    isolation, as a static "does it equal the target" check, therefore can't
    tell cycle 4 of a crash loop from cycle 1 — see wait_for_restart's
    `initial_started_commit` for the baseline that makes that distinction
    (issue_01M0Y94CXQ8S1QPMWS319YS0JN, PR #255 security review)."""
    try:
        return STARTED_MARKER.read_text().strip()
    except OSError:
        return None


def read_deployed_commit() -> str | None:
    """The commit `bin/install-orient` last staged into the deploy area, read
    from its `DEPLOYED_COMMIT_FILE` (format: `ref: …` / `commit: <sha>` /
    `installed_at: …` / `installed_from: …` — `--status` prints it verbatim).

    Missing or unparseable both read as "nothing deployed" — None — same as
    a missing served marker, and for the same reason: either must mean
    "install", never "up to date". Treating an unparseable file as already
    current would leave a broken or hand-edited marker permanently believed.
    """
    try:
        text = DEPLOYED_COMMIT_FILE.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("commit:"):
            sha = line[len("commit:"):].strip()
            return sha or None
    return None


def run_install() -> subprocess.CompletedProcess:
    """Run this checkout's own `bin/install-orient`, never the deploy area's
    copy — carried forward from agent-platform#469 (ADR-0059's other deploy-
    watch migration): a deployed installer that broke could not deploy its
    own fix, so `REPO_DIR` stays the one source of truth for where the
    installer (and this script) are read from, same as every git call above.
    No `--force`: a live plist that has drifted from its template is refused,
    not clobbered, by design — see PLIST_REFUSED_MARKER and `run`'s use of
    it — since auto-clobbering seven launchd definitions unattended on a
    5-minute cron is worse than the plist silently not updating.
    """
    return _run([str(REPO_DIR / "bin" / "install-orient")], INSTALL_TIMEOUT_S)


def kickstart() -> None:
    cp = _run(["launchctl", "kickstart", "-k", f"gui/{_gui_uid()}/{LAUNCHD_LABEL}"],
              LOCAL_TIMEOUT_S)
    if cp.returncode != 0:
        raise DeployWatchError(f"launchctl kickstart failed: {cp.stderr.strip()}")


def _gui_uid() -> str:
    cp = _run(["id", "-u"], LOCAL_TIMEOUT_S)
    if cp.returncode != 0:
        raise DeployWatchError(f"id -u failed: {cp.stderr.strip()}")
    return cp.stdout.strip()


def _port_open(host: str, port: int, timeout: float,
                attempts: int = PORT_PROBE_ATTEMPTS,
                retry_delay_s: float = PORT_PROBE_RETRY_S) -> bool:
    """A single sample transiently reports a healthy server as down (see
    PORT_PROBE_ATTEMPTS above) — retry a few times before reporting closed."""
    for attempt in range(attempts):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(retry_delay_s)
    return False


@dataclass(frozen=True)
class RestartResult:
    """What wait_for_restart decided, plus the observations it decided from —
    carried out so a caller never needs a second, later probe to build a
    message. A fresh probe taken after the fact is a different instant from
    the one that decided the outcome and can disagree with it."""
    state: str  # RESTART_REACHED | RESTART_CONVERGING | RESTART_WEDGED
    marker: str | None
    port_open: bool
    started_marker: str | None = None  # appended, defaulted: keeps every
    # existing 3-positional-arg construction (state, marker, port_open)
    # working unchanged.


def wait_for_restart(expected_commit: str, initial_commit: str | None,
                      initial_started_commit: str | None = None,
                      timeout_s: int = RESTART_TIMEOUT_S,
                      poll_s: int = RESTART_POLL_S) -> RestartResult:
    """Block until serve.sh's *served* marker matches `expected_commit` AND
    the port accepts a connection, or `timeout_s` elapses. Both checks
    matter: the marker alone doesn't prove the process bound the port; the
    port alone doesn't prove it's serving THIS commit (a hung old process
    could still be listening while the new one fails to start).

    Returns a state, not a bool: "gave up" is not one thing. A served marker
    that never left `initial_commit` (the marker read at the start of THIS
    wait — the pre-restart marker on the first call, and on the grace retry
    the marker observed when that first wait ran out), AND no evidence from
    the *started* marker either (see below), with the port never seen open,
    is a wedged server. Either marker showing progress — the served marker
    moved, or the started marker moved to `expected_commit` — is still
    converging, and every restart-timeout failure observed live to date was
    this shape, not a true wedge (issue_01M0TZTRK8JM09DD3N6CS6G95H,
    issue_01M0Y94CXQ8S1QPMWS319YS0JN).

    `initial_started_commit` — the started marker read BEFORE this wait's own
    kickstart, same idiom as `initial_commit` for the served marker — is what
    "moved" means for the started marker too: PR #255's security review
    (issue_01M0Y94CXQ8S1QPMWS319YS0JN follow-up) found that a bare `==
    expected_commit` check credits a value that was ALREADY there before this
    attempt started as if it were fresh evidence. Because the started marker
    is only ever overwritten, never deleted (see read_started_commit), a
    crash loop against the same broken commit rewrites it to the identical
    SHA on every retry — a bare equality check reads every single retry as
    "the process just launched," so a two-phase wait (and, worse, every
    launchd cycle that follows) reports RESTART_CONVERGING forever and never
    RESTART_WEDGED, on the most common real cause of a stuck deploy: a commit
    that breaks the build. Reseeding this baseline between retries, exactly
    as the grace retry below already reseeds `initial_commit` from the marker
    it actually observed, closes that gap."""
    # **Only something the NEW process itself writes counts as evidence — an
    # open port does not, and neither does anything else.** `kickstart()`
    # uses `launchctl kickstart -k`, which tears the old process down
    # asynchronously, so the OLD process can still answer the port when the
    # first poll runs. An earlier version latched "the port was open at some
    # point" as progress, which read as converging in exactly the case that
    # has none: old process still listening, old process dies, new process
    # crashes at startup and never writes a marker or reopens the port. That
    # turned a genuine wedge into 300s of silence followed by an alert saying
    # "the rebuild is slower than the ceiling, not stuck" — false for that
    # input (PR #208 review). The started marker (issue_01M0Y94CXQ8S1QPMWS319YS0JN)
    # is written by serve.sh before any builder runs, so it closes the gap
    # the served marker alone left: for most of a restart's real duration
    # (up to 236s observed live, PR #208's own merge on 2026-08-25) the
    # served marker hasn't moved yet even though the new process is alive
    # and working — without the started marker, that whole window reads as
    # "no evidence", identical to a true wedge.
    #
    # `initial_commit == expected_commit` seeds this True: a re-run against an
    # already-served commit has nothing to move, and calling that wedged would
    # alert on a deploy that is simply already done.
    progress_seen = initial_commit == expected_commit
    marker: str | None = None
    started: str | None = initial_started_commit
    port_open = False
    deadline = time.monotonic() + timeout_s
    while True:
        marker = read_served_commit()
        started = read_started_commit()
        port_open = _port_open(SERVER_HOST, SERVER_PORT, 3)
        if marker != initial_commit:
            progress_seen = True
        # `started != initial_started_commit` is the "moved" half of this
        # check, same shape as `marker != initial_commit` above — without it,
        # a started marker that already read `expected_commit` before this
        # wait even began (the previous, crashed attempt's leftover write,
        # since the marker is never deleted) would be credited again here as
        # if the current attempt had just produced it. See this function's
        # docstring and read_started_commit's for why that's the bug.
        elif started != initial_started_commit and started == expected_commit:
            progress_seen = True
        if marker == expected_commit and port_open:
            return RestartResult(RESTART_REACHED, marker, port_open, started)
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_s)
    if not progress_seen:
        return RestartResult(RESTART_WEDGED, marker, port_open, started)
    return RestartResult(RESTART_CONVERGING, marker, port_open, started)


# --- impure: signal on failure ---------------------------------------------------


def capture_token() -> str:
    tok = os.environ.get("ORIENT_CAPTURE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        sys.exit(f"no capture token: set ORIENT_CAPTURE_TOKEN or create {TOKEN_FILE}")


def post_failure_signal(body: str, dedup_key: str, refs: dict) -> None:
    """Best-effort — a wedged server is exactly the case where this POST may
    itself fail, so a failure here is swallowed (the exit-1 + stderr below is
    the guaranteed-visible path; this is the nicer one when the server is up
    enough to take it).

    `body` must be a fixed, closed-set reason string — never raw git/launchctl
    stderr (security review, PR #187). /archives renders to any authenticated
    team-site session, and this repo's git config already registers a
    credential helper for HTTPS GitHub auth; a future HTTPS remote with an
    embedded token would put that token in a git-auth failure's stderr, one
    `str(ex)` away from a public-shaped alert body. Full stderr still reaches
    the caller's own `print(..., file=sys.stderr)` — that's the local,
    guaranteed-visible path this is layered on top of, not a replacement
    for it."""
    payload = {
        "source": "deploy_watch",
        "kind": "deploy_failure",
        "actor": "deploy_watch",
        "product": PRODUCT,
        "body": body,
        "refs": refs,
        "dedup_key": dedup_key,
    }
    try:
        token = capture_token()
        req = urllib.request.Request(
            vault_plane.url("/api/signals"),
            data=json.dumps(payload).encode("utf-8"),
            headers=vault_plane.headers(token=token, json_body=True),
            method="POST")
        urllib.request.urlopen(req, timeout=10).close()
    except (urllib.error.URLError, OSError, SystemExit):
        pass


# --- the run -----------------------------------------------------------------


def run() -> int:
    try:
        git_fetch_main()
        local = git_head()
        origin = git_head("origin/main")
        branch = git_current_branch()
    except DeployWatchError as ex:
        print(str(ex), file=sys.stderr)  # full detail — stderr is the local, guaranteed-visible path
        post_failure_signal("fetch failed", "deploy-watch:fail:fetch", {})
        return 1

    # Incident, 2026-08-21: a co did PR development directly in this checkout
    # (should have used a worktree) instead of on `main`. Its feature-branch
    # commits were themselves ancestors of origin/main, so `git merge
    # --ff-only origin/main` in the pull_and_restart branch below trivially
    # succeeds ("Already up to date") WITHOUT checking which branch it ran
    # on or whether HEAD was actually safe to be there — leaving HEAD right
    # where it was — and this script went on to kickstart the live server
    # onto unmerged, in-progress code. Six restarts over ~45 minutes before
    # anyone noticed. Two checks, both required (security review, PR #189 —
    # the branch name alone doesn't catch unpushed commits on a checkout
    # that IS named `main`): refusing outright here, before any merge/
    # restart logic runs, is the actual fix — the working tree the rest of
    # this script assumes is `main`, unmodified, is a precondition worth
    # checking on every run, not an invariant worth trusting.
    if branch != "main" or not git_head_is_ancestor_of_origin_main():
        msg = (f"checkout is on branch {branch!r} at {local}, not a clean "
               f"origin/main ({origin}) — refusing to touch it")
        print(msg, file=sys.stderr)
        # `branch` is attacker-influenceable (anyone who can create a branch
        # and get it checked out here controls this string — verified branch
        # names permit HTML/quote metacharacters) and /archives renders to any
        # authenticated session, so it goes in `refs` (structured, not
        # string-rendered) — never interpolated into `body`, which stays the
        # fixed, closed-set reason this file's other failure paths use.
        post_failure_signal("checkout on unexpected branch",
                             "deploy-watch:fail:wrong-branch",
                             {"branch": branch, "local": local, "origin": origin})
        return 1

    deployed = read_deployed_commit()
    served = read_served_commit()
    # Read before this cycle's own kickstart, same idiom as `served` above —
    # a leftover started-marker value from a PRIOR (possibly crash-looping)
    # attempt at this same commit must not be credited as this cycle's own
    # evidence; see wait_for_restart's docstring.
    started = read_started_commit()
    action = decide_action(local, origin, deployed, served)

    if not action:
        return 0

    if STEP_FAST_FORWARD in action:
        try:
            git_ff_only_merge()
            local = git_head()
        except DeployWatchError as ex:
            print(str(ex), file=sys.stderr)
            post_failure_signal("merge failed (diverged or local changes present)",
                                 f"deploy-watch:fail:merge:{local}:{origin}",
                                 {"local": local, "origin": origin})
            return 1
        # The merge just moved the checkout, so STEP_INSTALL/STEP_RESTART as
        # decided above are stale the instant HEAD moves — re-derive them
        # against where it landed rather than where it started.
        action = decide_action(local, origin, deployed, served)

    if STEP_INSTALL in action:
        try:
            install_cp = run_install()
        except DeployWatchError as ex:
            print(str(ex), file=sys.stderr)
            post_failure_signal("install failed", f"deploy-watch:fail:install:{local}",
                                 {"target": local})
            return 1
        install_output = install_cp.stdout + install_cp.stderr
        if install_cp.returncode != 0:
            # A failed install must never be followed by a restart — that
            # would kickstart the live server onto a half-installed or
            # unchanged deploy area, exactly the failure ADR-0059 removes.
            print(install_output, file=sys.stderr)
            post_failure_signal("install failed", f"deploy-watch:fail:install:{local}",
                                 {"target": local, "returncode": install_cp.returncode})
            return 1
        if PLIST_REFUSED_MARKER in install_output:
            # The code deploy itself succeeded — this reports without failing
            # the run, since bin/install-orient exits 0 on this path by
            # design (rule 8: never auto-clobber a live plist that's
            # drifted). Nothing else surfaces that 0-exit warning; its own
            # stderr lands in orient-deploy-watch.err.log, which nobody reads.
            print(install_output, file=sys.stderr)
            post_failure_signal("install succeeded but a live plist needs --force",
                                 f"deploy-watch:plist-drift:{local}", {"target": local})

    if STEP_RESTART not in action:
        print(f"deploy_watch: now serving {local}")
        return 0

    try:
        kickstart()
    except DeployWatchError as ex:
        print(str(ex), file=sys.stderr)
        post_failure_signal("kickstart failed", f"deploy-watch:fail:kickstart:{local}",
                             {"target": local})
        return 1

    result = wait_for_restart(local, served, initial_started_commit=started)
    waited_s = RESTART_TIMEOUT_S

    if result.state == RESTART_CONVERGING:
        # Seed the retry from the marker we ACTUALLY observed, not from the
        # pre-restart one. Passing `served` again meant any value other than it
        # tripped `marker_ever_moved` on the retry's first poll — so a marker
        # that moved once to an intermediate value and then froze there for the
        # whole grace window still came back CONVERGING, and the alert said
        # "the rebuild is slower than the ceiling, not stuck" about a process
        # that had in fact stopped (PR #208 review). Seeded this way, a frozen
        # intermediate marker classifies WEDGED, which is what it is. The
        # started marker gets the identical treatment for the identical
        # reason (PR #255 security review) — reseeded from what this wait
        # actually observed, not re-passed as the pre-kickstart value, so a
        # started marker frozen since the FIRST phase stops being credited as
        # new progress in the retry.
        result = wait_for_restart(local, result.marker,
                                  initial_started_commit=result.started_marker,
                                  timeout_s=RESTART_CONVERGING_GRACE_S)
        waited_s = RESTART_TIMEOUT_S + RESTART_CONVERGING_GRACE_S

    # **`body` stays a fixed, closed-set string on BOTH paths; every observed
    # value travels in `refs`.** That is the rule the rest of this file follows
    # and `post_failure_signal`'s docstring gives the reason: `/archives` renders
    # a body to any authenticated team-site session. The marker is written by
    # `serve.sh` and is a bare SHA today, but nothing at this READ site enforces
    # that, and a guarantee living one file away is exactly what should not back
    # a rendered string (PR #208 review — both reviewers, independently). stderr
    # is the permissive path and keeps the detail.
    if result.state == RESTART_WEDGED:
        msg = (f"restart for {local} is wedged: the marker never left {served!r} "
               f"after {waited_s}s (port open={result.port_open})")
        print(msg, file=sys.stderr)
        post_failure_signal("restart wedged: the serving marker never moved",
                            f"deploy-watch:fail:restart-wedged:{local}",
                            {"target": local, "marker": result.marker,
                             "port_open": result.port_open, "waited_s": waited_s})
        return 1

    if result.state == RESTART_CONVERGING:
        msg = (f"restart for {local} is still converging after {waited_s}s — the "
               f"rebuild is slower than the ceiling, not stuck (served marker="
               f"{result.marker!r}, port open={result.port_open})")
        print(msg, file=sys.stderr)
        post_failure_signal("restart still converging past the ceiling",
                            f"deploy-watch:fail:restart-slow:{local}",
                            {"target": local, "marker": result.marker,
                             "port_open": result.port_open, "waited_s": waited_s})
        return 1

    print(f"deploy_watch: now serving {local}")
    return 0


def main() -> None:
    argparse.ArgumentParser(description="Poll origin/main and keep the live "
                                         "deploy current").parse_args()
    sys.exit(run())


if __name__ == "__main__":
    main()
