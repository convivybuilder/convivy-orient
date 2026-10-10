"""Deploy watcher tests (issue_01M0EERRFC8NJ8QS5Q6EEE87JY).

decide_action is pure and carries the three-independently-stale-things logic
from ADR-0059's deploy-area cutover (checkout vs deploy area vs running
process), so it's tested directly across the four-hash state space. The rest
is impure (git/launchctl/socket/HTTP/subprocess) and tested through
monkeypatched seams, same as github_capture.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import deploy_watch as dw


# --- decide_action: the pure core, now over four hashes / three flags -----------


def test_up_to_date_when_all_four_agree():
    assert dw.decide_action("abc", "abc", "abc", "abc") == frozenset()


def test_fast_forward_when_checkout_behind_origin():
    # deployed and served happen to equal the checkout here — still behind
    # origin, still needs the merge.
    assert dw.decide_action("abc", "def", "abc", "abc") == {dw.STEP_FAST_FORWARD}


def test_install_when_checkout_current_but_deploy_area_stale():
    # The loop this rewrite closes: checkout already matches origin (no
    # fast-forward needed this cycle) but the deploy area was never installed
    # to it — a human's git pull, or a prior cycle's fast-forward that never
    # got followed by an install.
    assert dw.decide_action("abc", "abc", "def", "abc") == {dw.STEP_INSTALL}


def test_restart_when_checkout_and_deploy_area_current_but_process_stale():
    # The deploy area already matches the checkout, but the running server is
    # serving something else (a human installed by hand without a kickstart,
    # or this is the first run since serve.sh started writing the marker).
    assert dw.decide_action("abc", "abc", "abc", "def") == {dw.STEP_RESTART}


def test_restart_when_served_marker_missing():
    # First run ever, or serve.sh hasn't written the marker yet — treated the
    # same as a stale process, not as up to date.
    assert dw.decide_action("abc", "abc", "abc", None) == {dw.STEP_RESTART}


def test_install_when_deployed_commit_missing():
    # Nothing has ever been installed to the deploy area — treated the same
    # as a stale deploy area, never as "up to date" (a missing/unparseable
    # DEPLOYED_COMMIT must never read as already current).
    assert dw.decide_action("abc", "abc", None, "abc") == {dw.STEP_INSTALL}


def test_the_bug_this_rewrite_closes_checkout_current_deploy_and_served_both_stale():
    # Exactly the state that used to loop forever: checkout == origin (no
    # fast-forward needed) but deployed and served both still behind it. The
    # old two-hash decide_action read this as "restart_only" and kickstarted
    # the live server onto whatever the deploy area already had, every 5
    # minutes, without ever installing the newer commit. Now it needs both
    # steps.
    assert dw.decide_action("abc", "abc", "old", "old") == {dw.STEP_INSTALL, dw.STEP_RESTART}


def test_fast_forward_and_install_and_restart_all_at_once():
    # The common case in practice: a merge landed, nothing has been installed
    # or restarted onto it yet.
    assert dw.decide_action("abc", "def", "old", "old") == {
        dw.STEP_FAST_FORWARD, dw.STEP_INSTALL, dw.STEP_RESTART}


def test_install_needed_but_restart_not_deployed_and_served_diverge_from_each_other():
    # An edge case the independent-flags model still gets right even though
    # it can't arise from this script's own steps alone: the running server
    # already serves code matching the checkout, but the deploy area's
    # install marker names something else (e.g. a --allow-non-main test
    # install ran after the server last started). Restart isn't needed —
    # what's running already matches the checkout — but the deploy area
    # should still be reconciled.
    assert dw.decide_action("abc", "abc", "other", "abc") == {dw.STEP_INSTALL}


def test_fast_forward_and_install_without_restart():
    # The checkout is behind and the deploy area is stale relative to it, but
    # the running server happens to already serve what the checkout is about
    # to become (served == checkout, coincidentally not origin either — a
    # human kickstarted early). Fills out the eight-combination state space
    # alongside the fast-forward-and-restart-only case below.
    assert dw.decide_action("abc", "def", "other", "abc") == {
        dw.STEP_FAST_FORWARD, dw.STEP_INSTALL}


def test_fast_forward_and_restart_without_install():
    # The checkout is behind and the running server is stale, but the deploy
    # area already matches the (stale) checkout — a prior cycle installed to
    # it and only the restart never happened.
    assert dw.decide_action("abc", "def", "abc", "other") == {
        dw.STEP_FAST_FORWARD, dw.STEP_RESTART}


# --- decide_action's flags are independent, not a single next-action value -----


def test_decide_action_returns_a_frozenset_not_a_string():
    # Three things can be true in the same cycle (fast-forward, then install,
    # then restart) — the old string-valued single-action return couldn't
    # represent that, which is exactly what let the fast-forward+install case
    # collapse into "restart_only" and loop.
    result = dw.decide_action("abc", "def", "old", "old")
    assert isinstance(result, frozenset)


# --- git seams ---------------------------------------------------------------------


class _FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_git_head_raises_on_failure(monkeypatch):
    monkeypatch.setattr(dw.subprocess, "run",
                        lambda *a, **k: _FakeRun(returncode=128, stderr="not a repo"))
    with pytest.raises(dw.DeployWatchError):
        dw.git_head()


def test_git_ff_only_merge_raises_without_forcing(monkeypatch):
    # A diverged history or a conflicting local change makes `git merge --ff-only`
    # fail on its own — this just confirms the failure surfaces as our error type
    # rather than being swallowed.
    monkeypatch.setattr(dw.subprocess, "run",
                        lambda *a, **k: _FakeRun(returncode=1, stderr="not possible to fast-forward"))
    with pytest.raises(dw.DeployWatchError):
        dw.git_ff_only_merge()


def test_a_hung_git_call_times_out_instead_of_blocking_forever(monkeypatch):
    # Review finding, PR #187: a stalled fetch (network partition, SSH auth hang)
    # must not block the whole launchd job indefinitely with no signal — every
    # git/launchctl call goes through _run, which must convert a timeout into our
    # own error type rather than letting subprocess.TimeoutExpired propagate raw.
    def fake_run(*a, **k):
        raise dw.subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout"))

    monkeypatch.setattr(dw.subprocess, "run", fake_run)
    with pytest.raises(dw.DeployWatchError):
        dw.git_fetch_main()


def test_kickstart_raises_on_failure(monkeypatch):
    monkeypatch.setattr(dw.subprocess, "run",
                        lambda *a, **k: _FakeRun(returncode=1, stderr="no such service"))
    monkeypatch.setattr(dw, "_gui_uid", lambda: "501")
    with pytest.raises(dw.DeployWatchError):
        dw.kickstart()


# --- _port_open: retries a transient closed result before trusting it -----------


def test_port_open_true_on_first_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(dw.socket, "create_connection",
                        lambda *a, **k: calls.append(1) or _FakeSocket())
    assert dw._port_open("h", 1, 1) is True
    assert len(calls) == 1


def test_port_open_retries_a_transient_failure_then_succeeds(monkeypatch):
    # Observed live: False, True, True, True across ~10s against a listener
    # that was up throughout (issue_01M0TZTRK8JM09DD3N6CS6G95H) — one failed
    # connect must not be trusted as "closed".
    attempts = [OSError("connection refused"), _FakeSocket()]

    def fake_connect(*a, **k):
        result = attempts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(dw.socket, "create_connection", fake_connect)
    monkeypatch.setattr(dw.time, "sleep", lambda s: None)
    assert dw._port_open("h", 1, 1) is True


def test_port_open_reports_closed_only_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(dw.socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    sleeps = []
    monkeypatch.setattr(dw.time, "sleep", lambda s: sleeps.append(s))
    assert dw._port_open("h", 1, 1, attempts=3, retry_delay_s=0.1) is False
    assert sleeps == [0.1, 0.1]  # slept between attempts, not after the last one


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- wait_for_restart: marker AND port, not either alone; state, not a bool -----
#
# Every test below pins `read_started_commit` explicitly (usually to `None`,
# "not yet") rather than leaving it to fall through to the real function —
# this suite runs on the same machine `deploy_watch.py` runs on, and its
# outcome must not depend on whatever ~/.orient-control/orient-restart-started
# happens to hold at the moment the suite runs.


def test_wait_for_restart_reaches_when_marker_and_port_both_match(monkeypatch):
    monkeypatch.setattr(dw, "read_served_commit", lambda: "abc")
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: True)
    result = dw.wait_for_restart("abc", "old", timeout_s=1, poll_s=0)
    assert result.state == dw.RESTART_REACHED
    assert result.marker == "abc"
    assert result.port_open is True


def test_wait_for_restart_is_wedged_when_marker_never_moves_and_port_never_opens(monkeypatch):
    # No sign of life at all — this is the only state that should alert.
    monkeypatch.setattr(dw, "read_served_commit", lambda: "old")
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(dw.time, "sleep", lambda s: None)
    result = dw.wait_for_restart("abc", "old", timeout_s=0.01, poll_s=0)
    assert result.state == dw.RESTART_WEDGED


def test_wait_for_restart_is_converging_when_marker_moved_but_not_yet_at_target(monkeypatch):
    # A hung old process can still be listening on 8099 while the new one
    # hasn't started yet — the port alone must not be read as success, but a
    # marker that has moved off the pre-deploy commit is not a wedge either.
    monkeypatch.setattr(dw, "read_served_commit", lambda: "mid")
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(dw.time, "sleep", lambda s: None)
    result = dw.wait_for_restart("abc", "old", timeout_s=0.01, poll_s=0)
    assert result.state == dw.RESTART_CONVERGING


def test_wait_for_restart_is_converging_when_marker_reached_but_port_not_open(monkeypatch):
    # The mechanism the original filing missed: the marker already matches
    # the target and only the port hasn't (yet, or transiently) accepted a
    # connection. Every restart-timeout failure observed live was this shape
    # or the one above — never a true wedge.
    monkeypatch.setattr(dw, "read_served_commit", lambda: "abc")
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(dw.time, "sleep", lambda s: None)
    result = dw.wait_for_restart("abc", "old", timeout_s=0.01, poll_s=0)
    assert result.state == dw.RESTART_CONVERGING
    assert result.marker == "abc"
    assert result.port_open is False


def test_wait_for_restart_is_converging_when_only_the_started_marker_has_moved(monkeypatch):
    # The fix in this file (issue_01M0Y94CXQ8S1QPMWS319YS0JN): serve.sh writes
    # the started marker before any builder runs, well before the served
    # marker (written after all nine builders + the migration) has any chance
    # to move. That gap used to read as "no evidence at all" — the exact
    # shape of the 2026-08-25 false alarm on PR #208's own merge.
    monkeypatch.setattr(dw, "read_served_commit", lambda: "old")  # unmoved — still building
    monkeypatch.setattr(dw, "read_started_commit", lambda: "abc")  # the new process launched
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(dw.time, "sleep", lambda s: None)
    result = dw.wait_for_restart("abc", "old", timeout_s=0.01, poll_s=0)
    assert result.state == dw.RESTART_CONVERGING


def test_wait_for_restart_does_not_credit_a_started_marker_already_at_target(monkeypatch):
    # PR #255 security review, issue_01M0Y94CXQ8S1QPMWS319YS0JN follow-up: the
    # started marker is only ever overwritten, never deleted, so a crash-
    # looping build rewrites it to the SAME target SHA on every retry. A bare
    # `read_started_commit() == expected_commit` check can't tell that from
    # fresh evidence — `initial_started_commit` (read before THIS wait began,
    # same idiom as `initial_commit` for the served marker) is what lets a
    # value that was already there before this attempt started fail to count
    # as this attempt's own progress.
    monkeypatch.setattr(dw, "read_served_commit", lambda: "old")  # never moves
    monkeypatch.setattr(dw, "read_started_commit", lambda: "abc")  # frozen, pre-existing
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(dw.time, "sleep", lambda s: None)
    result = dw.wait_for_restart("abc", "old", initial_started_commit="abc",
                                  timeout_s=0.01, poll_s=0)
    assert result.state == dw.RESTART_WEDGED, (
        "a started marker that already read the target before this wait "
        "began is stale evidence, not new progress")


def test_wait_for_restart_carries_the_deciding_observation_out(monkeypatch):
    # A caller must be able to build its message from the returned result
    # without a second, later probe — a fresh probe is a different instant
    # from the one that decided the outcome and can disagree with it.
    probes = iter([False, False, True])
    monkeypatch.setattr(dw, "read_served_commit", lambda: "abc")
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: next(probes))
    monkeypatch.setattr(dw.time, "sleep", lambda s: None)
    result = dw.wait_for_restart("abc", "old", timeout_s=1, poll_s=0)
    assert result.state == dw.RESTART_REACHED
    assert result.port_open is True  # the observation that decided REACHED, not a fresh probe


class _FakeClock:
    """A monotonic clock `time.sleep` actually advances, so a test can run a
    poll loop across a simulated 236s or 420s without a single real second
    passing — every wait_for_restart call below uses this instead of a tiny
    stand-in timeout_s, so it exercises the REAL RESTART_TIMEOUT_S /
    RESTART_CONVERGING_GRACE_S values, not a value chosen to make the test
    fast."""
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_a_restart_observed_taking_236s_is_not_reported_wedged(monkeypatch):
    """The exact incident this file fixes (issue_01M0Y94CXQ8S1QPMWS319YS0JN):
    PR #208's own merge on 2026-08-25 took 236s from kickstart to serve.sh's
    served marker, against the OLD 150s ceiling with no started-marker
    evidence — deploy_watch alerted at 22:34:03, 86s before the marker
    actually landed at 22:35:29, on a deploy that was healthy throughout.

    This plants that exact timeline — started marker within seconds,
    served marker + open port only at 236s — against the real production
    constants (not a shrunk test-only timeout) and asserts the full
    two-phase wait reaches RESTART_REACHED without ever reporting wedged or
    exceeding the stated total ceiling."""
    clock = _FakeClock()
    monkeypatch.setattr(dw.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(dw.time, "sleep", clock.sleep)
    monkeypatch.setattr(dw, "read_started_commit",
                        lambda: "new" if clock.now >= 3 else "old")
    monkeypatch.setattr(dw, "read_served_commit",
                        lambda: "new" if clock.now >= 236 else "old")
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: clock.now >= 236)

    result = dw.wait_for_restart("new", initial_commit="old",
                                  timeout_s=dw.RESTART_TIMEOUT_S, poll_s=5)
    assert result.state != dw.RESTART_WEDGED, (
        "a restart that started within seconds and served within the "
        "measured 236s must never read as wedged")

    if result.state == dw.RESTART_CONVERGING:
        result = dw.wait_for_restart("new", initial_commit=result.marker,
                                      timeout_s=dw.RESTART_CONVERGING_GRACE_S,
                                      poll_s=5)

    assert result.state == dw.RESTART_REACHED
    assert clock.now <= dw.RESTART_TIMEOUT_S + dw.RESTART_CONVERGING_GRACE_S


def test_a_restart_that_never_shows_any_sign_of_life_is_wedged_within_the_stated_bound(monkeypatch):
    """The value this whole file protects: a restart that never launches —
    no started marker, no served marker movement, no open port, ever — must
    still be caught, and caught within RESTART_TIMEOUT_S specifically, not
    after however long a healthy build chain might have taken. Asserting the
    elapsed simulated time (not just the eventual state) is what pins the
    bound rather than only the classification."""
    clock = _FakeClock()
    monkeypatch.setattr(dw.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(dw.time, "sleep", clock.sleep)
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    monkeypatch.setattr(dw, "read_served_commit", lambda: "old")
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: False)

    result = dw.wait_for_restart("new", initial_commit="old",
                                  timeout_s=dw.RESTART_TIMEOUT_S, poll_s=5)

    assert result.state == dw.RESTART_WEDGED
    # Bounded by the poll granularity, not open-ended: a true wedge must be
    # reported at RESTART_TIMEOUT_S, not RESTART_TIMEOUT_S plus the whole
    # converging grace period on top.
    assert clock.now <= dw.RESTART_TIMEOUT_S + 5
    assert dw.RESTART_TIMEOUT_S < 150, (
        "a true wedge should surface FASTER than the old 150s ceiling, not "
        "slower — the started marker is what buys that speed-up, not a "
        "longer wait")


# --- run(): orchestration over the seams ------------------------------------------


def _patch_common(monkeypatch, *, fetch_ok=True, merge_ok=True, install_ok=True,
                   plist_drift=False, kickstart_ok=True,
                   restart_ok=True, heads=("abc", "abc"), deployed=None, served="abc",
                   started=None, branch="main", ancestor_ok=True):
    local_head, origin_head = heads
    # Defaults to local_head, not origin_head: the fake git_head below doesn't
    # simulate HEAD actually moving after a merge (it's a fixed lookup table,
    # same simplification the pre-existing merge/restart tests already relied
    # on), so a default tied to origin_head would spuriously trigger
    # STEP_INSTALL on every "checkout behind origin" test even though that
    # test isn't about installing. Tests that care about the install step
    # pass an explicit `deployed=`.
    if deployed is None:
        deployed = local_head

    def fake_fetch():
        if not fetch_ok:
            raise dw.DeployWatchError("fetch failed")

    checkout = {"head": local_head}

    def fake_head(ref="HEAD"):
        return origin_head if ref == "origin/main" else checkout["head"]

    def fake_merge():
        if not merge_ok:
            raise dw.DeployWatchError("merge failed")
        checkout["head"] = origin_head  # a successful ff-only merge always lands exactly on origin

    def fake_run_install():
        if not install_ok:
            raise dw.DeployWatchError("install failed")
        stdout = f"! {dw.PLIST_REFUSED_MARKER} (--force to replace): com.convivy.orient.plist\n" \
            if plist_drift else "Install complete.\n"
        return dw.subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    def fake_kickstart():
        if not kickstart_ok:
            raise dw.DeployWatchError("kickstart failed")

    monkeypatch.setattr(dw, "git_fetch_main", fake_fetch)
    monkeypatch.setattr(dw, "git_head", fake_head)
    monkeypatch.setattr(dw, "git_current_branch", lambda: branch)
    monkeypatch.setattr(dw, "git_head_is_ancestor_of_origin_main", lambda: ancestor_ok)
    monkeypatch.setattr(dw, "git_ff_only_merge", fake_merge)
    monkeypatch.setattr(dw, "read_deployed_commit", lambda: deployed)
    monkeypatch.setattr(dw, "read_served_commit", lambda: served)
    # Defaults to None (serve.sh hasn't reached the started marker), never to
    # the real machine's own ~/.orient-control file — a test's outcome must
    # not depend on incidental state on whatever machine runs it.
    monkeypatch.setattr(dw, "read_started_commit", lambda: started)
    monkeypatch.setattr(dw, "run_install", fake_run_install)
    def fake_wait_for_restart(expected, initial, initial_started_commit=None,
                              timeout_s=None, poll_s=None):
        if restart_ok:
            return dw.RestartResult(dw.RESTART_REACHED, expected, True)
        # No sign of life at all — the wedged state, so run() alerts on the
        # first call rather than granting a converging grace period.
        return dw.RestartResult(dw.RESTART_WEDGED, initial, False)

    monkeypatch.setattr(dw, "kickstart", fake_kickstart)
    monkeypatch.setattr(dw, "wait_for_restart", fake_wait_for_restart)
    monkeypatch.setattr(dw, "post_failure_signal", lambda *a, **k: None)


def test_git_current_branch_raises_on_failure(monkeypatch):
    monkeypatch.setattr(dw.subprocess, "run",
                        lambda *a, **k: _FakeRun(returncode=128, stderr="not a repo"))
    with pytest.raises(dw.DeployWatchError):
        dw.git_current_branch()


def test_head_is_ancestor_true_when_merge_base_says_so(monkeypatch):
    monkeypatch.setattr(dw.subprocess, "run", lambda *a, **k: _FakeRun(returncode=0))
    assert dw.git_head_is_ancestor_of_origin_main() is True


def test_head_is_ancestor_false_on_any_nonzero_exit(monkeypatch):
    # Fail closed: exit 1 ("not an ancestor") and any other failure both read
    # as "not safe" — the check itself erroring must never be read as safety.
    monkeypatch.setattr(dw.subprocess, "run", lambda *a, **k: _FakeRun(returncode=1))
    assert dw.git_head_is_ancestor_of_origin_main() is False
    monkeypatch.setattr(dw.subprocess, "run", lambda *a, **k: _FakeRun(returncode=128, stderr="boom"))
    assert dw.git_head_is_ancestor_of_origin_main() is False


def test_run_refuses_to_touch_a_non_main_checkout(monkeypatch):
    # Incident, 2026-08-21: a feature branch checked out in this same
    # directory is an ancestor of origin/main whenever it hasn't diverged, so
    # `git merge --ff-only origin/main` trivially succeeds ("Already up to
    # date") without changing HEAD — and this script went on to kickstart the
    # live server onto unmerged commits, six times over ~45 minutes. This
    # pins the fix: refuse before any merge/restart logic runs at all.
    _patch_common(monkeypatch, heads=("feature-tip", "abc"), served="old", branch="my-feature-branch")
    merge_calls, kickstart_calls = [], []
    monkeypatch.setattr(dw, "git_ff_only_merge", lambda: merge_calls.append(1))
    monkeypatch.setattr(dw, "kickstart", lambda: kickstart_calls.append(1))
    assert dw.run() == 1
    assert merge_calls == []
    assert kickstart_calls == []


def test_run_refuses_a_checkout_named_main_but_ahead_of_origin(monkeypatch):
    # Security review, PR #189: the branch-name check alone misses this case
    # — a checkout literally on `main` with unpushed local commits hits the
    # exact same "Already up to date" no-op as a feature branch would.
    _patch_common(monkeypatch, heads=("unpushed-local-commit", "abc"), served="old",
                  branch="main", ancestor_ok=False)
    merge_calls, kickstart_calls = [], []
    monkeypatch.setattr(dw, "git_ff_only_merge", lambda: merge_calls.append(1))
    monkeypatch.setattr(dw, "kickstart", lambda: kickstart_calls.append(1))
    assert dw.run() == 1
    assert merge_calls == []
    assert kickstart_calls == []


def test_run_posts_a_fixed_body_with_the_branch_only_in_refs(monkeypatch):
    # Security review, PR #189: a git branch name is caller-controlled (valid
    # names include HTML/quote metacharacters) and /signals renders `body` to
    # any authenticated session — the branch name must land only in `refs`
    # (structured), never interpolated into `body`.
    _patch_common(monkeypatch, heads=("x", "y"), served="x", branch="<script>evil</script>")
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda body, key, refs: posted.append((body, key, refs)))
    dw.run()
    assert len(posted) == 1
    body, key, refs = posted[0]
    assert "<script>" not in body
    assert "<script>" not in key
    assert body == "checkout on unexpected branch"
    assert refs["branch"] == "<script>evil</script>"


def test_run_proceeds_normally_when_on_main(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), served="abc", branch="main")
    assert dw.run() == 0


def test_run_does_nothing_when_up_to_date(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), served="abc")
    calls = []
    monkeypatch.setattr(dw, "kickstart", lambda: calls.append("kickstart"))
    assert dw.run() == 0
    assert calls == []


def test_run_restarts_without_merging_when_only_process_is_stale(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), served="old")
    merge_calls = []
    monkeypatch.setattr(dw, "git_ff_only_merge", lambda: merge_calls.append(1))
    assert dw.run() == 0
    assert merge_calls == []  # checkout already matched origin — no merge needed


def test_run_merges_then_restarts_when_checkout_is_behind(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "def"), served="abc")
    merge_calls = []
    monkeypatch.setattr(dw, "git_ff_only_merge", lambda: merge_calls.append(1))
    assert dw.run() == 0
    assert merge_calls == [1]


def test_run_fails_loud_on_fetch_error(monkeypatch):
    _patch_common(monkeypatch, fetch_ok=False)
    assert dw.run() == 1


def test_run_fails_loud_on_diverged_merge_without_forcing(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "def"), served="abc", merge_ok=False)
    kickstart_calls = []
    monkeypatch.setattr(dw, "kickstart", lambda: kickstart_calls.append(1))
    assert dw.run() == 1
    assert kickstart_calls == []  # never kickstarts over a merge it couldn't verify


def test_run_fails_loud_when_kickstart_itself_fails(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), served="old", kickstart_ok=False)
    restart_calls = []
    monkeypatch.setattr(dw, "wait_for_restart",
                        lambda expected, initial: restart_calls.append(1)
                        or dw.RestartResult(dw.RESTART_REACHED, expected, True))
    assert dw.run() == 1
    assert restart_calls == []  # never waits for a restart that was never launched


def test_run_fails_loud_when_restart_is_wedged(monkeypatch):
    # No sign of life at all — the state this fix keeps alerting on.
    _patch_common(monkeypatch, heads=("abc", "abc"), served="old", restart_ok=False)
    assert dw.run() == 1


def test_run_posts_failure_signal_on_wedged_restart(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), served="old", restart_ok=False)
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda body, key, refs: posted.append(key))
    dw.run()
    assert len(posted) == 1
    assert "wedged" in posted[0]


def test_run_succeeds_and_prints_now_serving_when_restart_converges_late(monkeypatch):
    # The bug this fix closes: a restart that converges after the first
    # timeout must still exit 0 and print the success line — it must not be
    # reported as a failure, and it must not be silently dropped either.
    _patch_common(monkeypatch, heads=("abc", "abc"), served="old")
    calls = []

    def fake_wait(expected, initial, initial_started_commit=None,
                  timeout_s=None, poll_s=None):
        calls.append(timeout_s)
        if len(calls) == 1:
            return dw.RestartResult(dw.RESTART_CONVERGING, expected, False)
        return dw.RestartResult(dw.RESTART_REACHED, expected, True)

    monkeypatch.setattr(dw, "wait_for_restart", fake_wait)
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda *a, **k: posted.append(1))
    assert dw.run() == 0
    assert calls == [None, dw.RESTART_CONVERGING_GRACE_S]
    assert posted == []  # a late success must never post a failure signal


def test_run_fails_loud_when_still_converging_past_the_grace_period(monkeypatch):
    # Genuinely slower than both ceilings — alert, but distinctly from wedged.
    _patch_common(monkeypatch, heads=("abc", "abc"), served="old")
    monkeypatch.setattr(dw, "wait_for_restart",
                        lambda expected, initial, initial_started_commit=None,
                        timeout_s=None, poll_s=None:
                        dw.RestartResult(dw.RESTART_CONVERGING, expected, False))
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda body, key, refs: posted.append((body, key)))
    assert dw.run() == 1
    body, key = posted[0]
    assert "wedged" not in body
    assert "converging" in body
    assert "slow" in key


def test_an_early_port_answer_from_the_dying_old_process_is_not_progress(monkeypatch):
    """The blocking finding from PR #208's review, and the case the first cut
    got wrong.

    `kickstart()` uses `launchctl kickstart -k`, which tears the old process
    down asynchronously — so the OLD process can still answer the port when the
    first poll runs. If that counts as progress, this sequence reads as
    converging when it is a wedge: old process still listening, old process
    dies, new process crashes at startup and never writes a marker or reopens
    the port. It would then wait 300s instead of 150s and alert with "the
    rebuild is slower than the ceiling, not stuck", which is false for this
    input.

    Only the marker counts, because only the new process writes it."""
    ports = iter([True, False, False, False, False, False])
    monkeypatch.setattr(dw, "read_served_commit", lambda: "old")
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    monkeypatch.setattr(dw, "_port_open",
                        lambda *a, **k: next(ports, False))
    r = dw.wait_for_restart("new", initial_commit="old",
                                      timeout_s=0.25, poll_s=0.05)
    assert r.state == dw.RESTART_WEDGED


def test_a_rerun_against_an_already_served_commit_is_not_wedged(monkeypatch):
    """Nothing has to move when the marker already reads the target, so
    "the marker never moved" must not mean "wedged" there — that would alert
    on a deploy which is simply already done."""
    monkeypatch.setattr(dw, "read_served_commit", lambda: "new")
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: False)
    r = dw.wait_for_restart("new", initial_commit="new",
                                      timeout_s=0.15, poll_s=0.05)
    assert r.state != dw.RESTART_WEDGED


def test_a_marker_frozen_at_an_intermediate_value_is_wedged_not_slow(monkeypatch):
    """The grace retry is seeded from the OBSERVED marker, not the pre-restart
    one, so a marker that moved once and then stopped is called what it is.

    Passing `served` to the retry meant any value other than it tripped
    `marker_ever_moved` on the first poll, so this case came back CONVERGING and
    the alert said "not stuck" about a process that had stopped. In a change
    whose whole purpose is that these alerts stop lying, that message mattered
    (PR #208 review)."""
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal",
                        lambda body, key, refs: posted.append((body, refs)))
    monkeypatch.setattr(dw, "git_fetch_main", lambda: None)
    monkeypatch.setattr(dw, "git_head", lambda *a: "target")
    monkeypatch.setattr(dw, "git_current_branch", lambda: "main")
    monkeypatch.setattr(dw, "git_head_is_ancestor_of_origin_main", lambda: True)
    monkeypatch.setattr(dw, "decide_action", lambda *a: frozenset({dw.STEP_RESTART}))
    monkeypatch.setattr(dw, "kickstart", lambda: None)
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(dw, "read_started_commit", lambda: None)
    # `wait_for_restart`'s `timeout_s` default binds at DEF time, so patching
    # RESTART_TIMEOUT_S does nothing for run()'s first call — it waits the real
    # 60s. Wrap the function instead, keeping the real classification logic.
    real_wait = dw.wait_for_restart
    monkeypatch.setattr(
        dw, "wait_for_restart",
        lambda expected, initial, initial_started_commit=None, timeout_s=None,
        poll_s=None: real_wait(
            expected, initial, initial_started_commit=initial_started_commit,
            timeout_s=0.15, poll_s=0.05))
    # moves off the pre-restart marker once, then freezes there for good
    reads = iter(["was-serving"] + ["intermediate"] * 40)
    monkeypatch.setattr(dw, "read_served_commit", lambda: next(reads, "intermediate"))

    assert dw.run() == 1
    assert posted, "no signal posted"
    body, refs = posted[-1]
    assert "wedged" in body, f"a frozen marker should read as wedged, got {body!r}"
    assert "converging" not in body


def test_repeated_crash_loop_cycles_against_the_same_commit_wedge_not_converge(monkeypatch):
    """PR #255 security review, issue_01M0Y94CXQ8S1QPMWS319YS0JN follow-up:
    a crash AFTER serve.sh writes the started marker but BEFORE it writes the
    served marker, repeated across retries of the SAME broken commit, used to
    be permanently misclassified as "still converging" — the started marker
    on disk is never deleted, only overwritten, and a crash-looping build
    rewrites it to the identical target SHA every attempt, so a bare
    equality check credited every single retry as fresh evidence a process
    had "just launched."

    Calls `run()` several times in a row against markers that persist on
    disk between calls, the way real files would across separate launchd
    cycles: `kickstart()` simulates serve.sh writing the started marker to
    the target commit and then crashing, every attempt; the served marker
    never moves, because the build never finishes. Asserts every cycle
    reports wedged, never converging — including the first, where the
    two-phase wait's own grace retry already has to reject the marker its
    own first phase observed as stale rather than fresh."""
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal",
                        lambda body, key, refs: posted.append((body, refs, key)))
    monkeypatch.setattr(dw, "git_fetch_main", lambda: None)
    monkeypatch.setattr(dw, "git_head", lambda *a: "target")
    monkeypatch.setattr(dw, "git_current_branch", lambda: "main")
    monkeypatch.setattr(dw, "git_head_is_ancestor_of_origin_main", lambda: True)
    monkeypatch.setattr(dw, "decide_action", lambda *a: frozenset({dw.STEP_RESTART}))
    monkeypatch.setattr(dw, "_port_open", lambda *a, **k: False)

    disk = {"started": None, "served": "was-serving"}
    monkeypatch.setattr(dw, "read_started_commit", lambda: disk["started"])
    monkeypatch.setattr(dw, "read_served_commit", lambda: disk["served"])

    def fake_kickstart():
        # serve.sh writes the started marker before the builder chain that
        # then crashes — every attempt against this broken commit reaches
        # this line and rewrites the SAME target SHA; the served marker,
        # written only after the builder chain finishes, never gets touched.
        disk["started"] = "target"

    monkeypatch.setattr(dw, "kickstart", fake_kickstart)

    real_wait = dw.wait_for_restart
    monkeypatch.setattr(
        dw, "wait_for_restart",
        lambda expected, initial, initial_started_commit=None, timeout_s=None,
        poll_s=None: real_wait(
            expected, initial, initial_started_commit=initial_started_commit,
            timeout_s=0.1, poll_s=0.02))

    results = [dw.run() for _ in range(4)]

    assert results == [1, 1, 1, 1], "every crash-loop cycle must fail loud"
    assert len(posted) == 4, "every cycle must post a failure signal"
    for body, refs, key in posted:
        assert "wedged" in body, (
            f"a commit that crashes the same way on every retry must read as "
            f"wedged, not permanently converging — got {body!r}")
        assert "converging" not in body
        assert "wedged" in key


def test_neither_restart_signal_body_carries_an_observed_value(monkeypatch):
    """`body` renders to any authenticated team-site session; `refs` does not.
    Both new failure paths must keep every observed value — the marker above
    all — out of `body` (PR #187's security review, re-raised on #208 by both
    reviewers independently).

    **This test asserts a signal was actually posted.** An earlier draft used
    `if not posted: continue`, which made it pass even with the bug deliberately
    reintroduced, because `run()` never reached the call at all. A test that
    cannot fail is worse than no test, and this one was written to close a gap."""
    for state in (dw.RESTART_WEDGED, dw.RESTART_CONVERGING):
        posted = []
        monkeypatch.setattr(dw, "post_failure_signal",
                            lambda body, key, refs: posted.append((body, refs)))
        monkeypatch.setattr(dw, "git_fetch_main", lambda: None)
        monkeypatch.setattr(dw, "git_head", lambda *a: "abc123")
        monkeypatch.setattr(dw, "git_current_branch", lambda: "main")
        monkeypatch.setattr(dw, "git_head_is_ancestor_of_origin_main", lambda: True)
        # the sentinel is the SERVED MARKER too, not only the RestartResult's:
        # the wedged path's stderr message interpolates `served` while the
        # converging path interpolates `result.marker`, so a sentinel on only
        # one of them lets the other path's leak through untested. That is
        # exactly how the first draft of this test passed with the bug
        # deliberately reintroduced.
        monkeypatch.setattr(dw, "read_served_commit", lambda: "SENTINEL-MARKER")
        monkeypatch.setattr(dw, "kickstart", lambda: None)
        monkeypatch.setattr(
            dw, "wait_for_restart",
            lambda *a, _s=state, **k: dw.RestartResult(_s, "SENTINEL-MARKER", False))
        monkeypatch.setattr(dw, "decide_action", lambda *a: frozenset({dw.STEP_RESTART}))
        rc = dw.run()
        assert rc == 1, f"{state} must exit non-zero"
        assert posted, f"{state} posted no signal — this test would be vacuous"
        body, refs = posted[-1]
        assert "SENTINEL-MARKER" not in body, (
            f"an observed value reached a rendered signal body: {body!r}")
        assert refs.get("marker") == "SENTINEL-MARKER", (
            "the marker should travel in refs, which is not rendered")


def test_run_never_forwards_raw_process_stderr_into_the_signal_body(monkeypatch):
    # Security review, PR #187: /signals renders to any authenticated team-site
    # session, so a git/launchctl failure's raw stderr (which could carry a
    # credential if origin were ever an HTTPS remote with an embedded token)
    # must never reach post_failure_signal's `body` — only a fixed reason string.
    secret_stderr = "fatal: Authentication failed for 'https://x-access-token:ghp_SECRET@github.com/...'"

    def fake_fetch():
        raise dw.DeployWatchError(f"git fetch origin main failed: {secret_stderr}")

    monkeypatch.setattr(dw, "git_fetch_main", fake_fetch)
    bodies = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda body, key, refs: bodies.append(body))
    dw.run()
    assert bodies == ["fetch failed"]
    assert "SECRET" not in bodies[0]


# --- read_deployed_commit: parses DEPLOYED_COMMIT, same "missing means install
# never up to date" contract as read_served_commit ---------------------------


def test_read_deployed_commit_parses_the_commit_line(monkeypatch, tmp_path):
    marker = tmp_path / "DEPLOYED_COMMIT"
    marker.write_text("ref: main\ncommit: abc123\ninstalled_at: 2026-08-31T00:00:00Z\n"
                      "installed_from: /srv/convivy-orient\n")
    monkeypatch.setattr(dw, "DEPLOYED_COMMIT_FILE", marker)
    assert dw.read_deployed_commit() == "abc123"


def test_read_deployed_commit_is_none_when_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(dw, "DEPLOYED_COMMIT_FILE", tmp_path / "does-not-exist")
    assert dw.read_deployed_commit() is None


def test_read_deployed_commit_is_none_when_commit_line_missing(monkeypatch, tmp_path):
    # Never reads as "up to date" — a hand-edited or truncated marker with no
    # `commit:` line must fall back to "install", same as a missing file.
    marker = tmp_path / "DEPLOYED_COMMIT"
    marker.write_text("ref: main\ninstalled_at: 2026-08-31T00:00:00Z\n")
    monkeypatch.setattr(dw, "DEPLOYED_COMMIT_FILE", marker)
    assert dw.read_deployed_commit() is None


# --- run_install: the checkout's own installer, never the deploy area's ----------


def test_run_install_invokes_the_checkout_installer_not_the_deploy_area(monkeypatch):
    # agent-platform#469's constraint, carried forward: a deployed installer
    # that broke could not deploy its own fix, so REPO_DIR — the checkout —
    # must stay the one thing this script ever executes as the installer,
    # never anything under DEPLOY_DIR.
    calls = []
    monkeypatch.setattr(dw, "_run", lambda argv, timeout_s: calls.append((argv, timeout_s))
                        or dw.subprocess.CompletedProcess(argv, 0, "", ""))
    dw.run_install()
    assert len(calls) == 1
    argv, timeout_s = calls[0]
    assert argv == [str(dw.REPO_DIR / "bin" / "install-orient")]
    assert "deploy" not in argv[0]
    # REPO_DIR is the file's own directory, so pin what that must be: a git
    # checkout, never DEPLOY_DIR or anything inside it.
    assert dw.DEPLOY_DIR != dw.REPO_DIR and dw.DEPLOY_DIR not in dw.REPO_DIR.parents
    assert (dw.REPO_DIR / ".git").exists()
    assert "--force" not in argv
    assert timeout_s == dw.INSTALL_TIMEOUT_S


def test_install_timeout_is_its_own_generous_ceiling_not_the_local_one():
    # bin/install-orient builds a venv from scratch every run — a plain
    # git/launchctl call's 30s ceiling would false-positive on a slow but
    # healthy install.
    assert dw.INSTALL_TIMEOUT_S > dw.LOCAL_TIMEOUT_S


def test_run_install_raises_on_a_hung_process(monkeypatch):
    def fake_run(*a, **k):
        raise dw.subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout"))

    monkeypatch.setattr(dw.subprocess, "run", fake_run)
    with pytest.raises(dw.DeployWatchError):
        dw.run_install()


# --- run(): the install step ------------------------------------------------------


def test_run_installs_when_deploy_area_is_behind_the_checkout(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), deployed="old", served="abc")
    install_calls = []

    def fake_install():
        install_calls.append(1)
        return dw.subprocess.CompletedProcess([], 0, "Install complete.\n", "")

    monkeypatch.setattr(dw, "run_install", fake_install)
    assert dw.run() == 0
    assert install_calls == [1]


def test_run_does_not_install_when_deploy_area_already_matches(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), deployed="abc", served="abc")
    install_calls = []
    monkeypatch.setattr(dw, "run_install", lambda: install_calls.append(1))
    assert dw.run() == 0
    assert install_calls == []


def test_run_fails_loud_when_install_itself_raises(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), deployed="old", served="abc",
                  install_ok=False)
    kickstart_calls = []
    monkeypatch.setattr(dw, "kickstart", lambda: kickstart_calls.append(1))
    assert dw.run() == 1
    assert kickstart_calls == []  # a failed install must never be followed by a restart


def test_run_fails_loud_when_install_exits_nonzero_and_never_restarts(monkeypatch):
    # The failure this whole rewrite exists to prevent: a half-installed or
    # unchanged deploy area must never be followed by a kickstart.
    _patch_common(monkeypatch, heads=("abc", "abc"), deployed="old", served="abc")
    kickstart_calls = []
    monkeypatch.setattr(dw, "run_install",
                        lambda: dw.subprocess.CompletedProcess([], 1, "", "boom"))
    monkeypatch.setattr(dw, "kickstart", lambda: kickstart_calls.append(1))
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda body, key, refs: posted.append((body, key)))
    assert dw.run() == 1
    assert kickstart_calls == []
    body, key = posted[-1]
    assert body == "install failed"
    assert "install" in key


def test_run_never_forwards_raw_install_output_into_the_signal_body(monkeypatch):
    # Same rule as git/launchctl stderr: the installer's own output is never
    # a fixed, closed-set string, so it must never reach a rendered `body`.
    secret = "fatal: some secret-shaped installer output"
    _patch_common(monkeypatch, heads=("abc", "abc"), deployed="old", served="abc")
    monkeypatch.setattr(dw, "run_install",
                        lambda: dw.subprocess.CompletedProcess([], 1, "", secret))
    bodies = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda body, key, refs: bodies.append(body))
    dw.run()
    assert bodies == ["install failed"]
    assert secret not in bodies[0]


def test_run_reports_but_does_not_fail_when_installer_refuses_a_drifted_plist(monkeypatch):
    # Rule 5: never --force a live plist that's drifted — the installer
    # refuses and exits 0. That 0-exit warning lands only in
    # orient-deploy-watch.err.log, which nobody reads, so this is the only
    # place that surfaces it. The code deploy itself still succeeded, so the
    # run must not fail over it.
    _patch_common(monkeypatch, heads=("abc", "abc"), deployed="old", served="abc",
                  plist_drift=True)
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda body, key, refs: posted.append((body, key)))
    assert dw.run() == 0
    assert posted, "no signal posted for the plist drift"
    body, key = posted[-1]
    assert body == "install succeeded but a live plist needs --force"
    assert "plist" in key


def test_run_does_not_report_plist_drift_when_the_installer_did_not_warn(monkeypatch):
    _patch_common(monkeypatch, heads=("abc", "abc"), deployed="old", served="abc",
                  plist_drift=False)
    posted = []
    monkeypatch.setattr(dw, "post_failure_signal", lambda *a, **k: posted.append(1))
    assert dw.run() == 0
    assert posted == []


# --- self-rewrite safety (agent-platform#469): a fast-forward this same
# script just ran can rewrite deploy_watch.py's own source mid-execution.
# Python parses a whole module before running any top-level statement, so
# this holds today only because every def/import lives at module scope and
# nothing runs before the single call to main() — no lazy import inside a
# function body, no re-read of this file's own source after start. These
# tests make that checkable instead of merely true by inspection today. -----


def test_no_lazy_imports_inside_function_bodies():
    # A lazy import would resolve deploy_watch.py's own module (or another
    # module in this checkout) at CALL time, after this run's own
    # fast-forward may have already rewritten the file on disk — a
    # still-running function could start executing code that didn't exist
    # when the interpreter parsed it. Every import must resolve at module
    # load, before main() ever runs.
    tree = ast.parse(Path(dw.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                assert not isinstance(inner, (ast.Import, ast.ImportFrom)), (
                    f"lazy import inside {node.name}")


def test_main_call_is_the_only_top_level_statement_after_every_def_and_import():
    # Every def/class/import/assignment must be fully parsed before the one
    # executable statement (the `if __name__ == "__main__":` guard) runs —
    # otherwise a def added below some other top-level call wouldn't exist
    # yet when that call ran. This is what makes "every function body is
    # parsed before the single top-level call" a checked invariant instead of
    # something only true because nobody's broken it yet.
    tree = ast.parse(Path(dw.__file__).read_text())
    body = tree.body
    assert isinstance(body[-1], ast.If), "expected the __main__ guard to be the final statement"
    for node in body[:-1]:
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant), (
                f"unexpected top-level expression (not the docstring): {ast.dump(node)}")
        else:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                      ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)), (
                f"unexpected top-level statement before the __main__ guard: {ast.dump(node)}")


# --- capture_token: env override, matching every sibling capture script -----------


def test_capture_token_prefers_env_var_over_file(monkeypatch, tmp_path):
    monkeypatch.setattr(dw, "TOKEN_FILE", tmp_path / "unused")
    monkeypatch.setenv("ORIENT_CAPTURE_TOKEN", "env-tok")
    assert dw.capture_token() == "env-tok"


def test_capture_token_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ORIENT_CAPTURE_TOKEN", raising=False)
    token_file = tmp_path / "capture-token"
    token_file.write_text("file-tok\n")
    monkeypatch.setattr(dw, "TOKEN_FILE", token_file)
    assert dw.capture_token() == "file-tok"


# --- post_failure_signal: best-effort, never raises -------------------------------


def test_post_failure_signal_swallows_a_dead_server(monkeypatch):
    # The server being unreachable is exactly the case a restart failure needs to
    # report — the report path itself must not raise past that.
    monkeypatch.setattr(dw, "capture_token", lambda: "tok")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(dw.urllib.request, "urlopen", boom)
    dw.post_failure_signal("body", "key", {})  # must not raise
