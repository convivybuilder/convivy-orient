"""kb_commit.py shells to `agentctl kb checkout` then `agentctl kb apply` —
see the module docstring for the Library-arbitrates/Bosun-executes split.

Every subprocess call is mocked: this module must never actually push to the
knowledge repository as a side effect of running the test suite (a test suite
that pushes to main is worse than no test suite at all). What is pinned here
is the shape of the two `agentctl` invocations — argv as a list, the body
travelling only through `--content-file`, an allowlisted subprocess
environment, a 0600 content file removed afterward — and that each way
`agentctl` can refuse maps to its own exception rather than one generic
failure.

**`ACTOR` is deliberately an email, matching production exactly.** The
Library's checkout records the member's Cloudflare Access email as its holder,
and agentctl's lease is taken under that same string, unchanged
(agent-platform#889 made agentctl accept it).
`test_commit_document_hands_agentctl_the_recorded_holder_unchanged` and its
`read_document` twin below pin that."""
from __future__ import annotations

import json
import stat
import subprocess

import pytest

import kb_commit

DOC = "proj/kind/doc-1"
ACTOR = "operator@example.com"
# The content-sha the editor opened at, as `kb_writes.content_sha` shapes it
# (sha256 hexdigest), and the sha origin/main holds once something else has
# been committed since.
OPEN_SHA = "a" * 64
# A stand-in Access assertion. Only its routing is tested here; agentctl verifies it.
ASSERTION = "stand-in-access-assertion"
MOVED_SHA = "b" * 64


@pytest.fixture(autouse=True)
def _agentctl_resolves_to_the_bare_name(monkeypatch):
    """Every test below mocks `subprocess.run` and asserts on the literal
    `"agentctl"` as argv[0]. Pin `paths.resolve_tool` to `None` (falls back to
    the bare name — see `commit_document`) so those assertions hold
    regardless of whether the machine running the suite happens to have a
    deployed `agentctl` copy or a real one on PATH (issue_01M1DKJYHSH9FT1SBP9068MTT1:
    it does, on the machine this was written on). Tests that exercise the
    resolver itself override this."""
    monkeypatch.setattr(kb_commit.paths, "resolve_tool", lambda name: None)

# A holder as the Library's checkout can record it, distinct from `ACTOR`, so a
# test can tell which argument reached agentctl's `--holder`.
RECORDED_HOLDER = "member@example.com"


def _checkout_ok(cmd, *, sha=OPEN_SHA):
    return subprocess.CompletedProcess(cmd, 0,
        stdout=json.dumps({"source_ref": "projects/proj/kind/doc-1.md", "exists": True,
                           "content": "old body", "content_sha": sha}),
        stderr="")


def _dispatch(checkout_result, apply_result, calls, written):
    """A fake `subprocess.run` that answers `kb checkout`, `kb apply`, then
    `kb release` (`commit_document`'s `finally` releases the agentctl-side
    lease on every path once checkout has succeeded), recording every call
    and (for `apply`) the content file's body and mode at the moment
    `agentctl` would have read it — before this module's `finally` deletes
    it. `release` always answers 0; its own behaviour is pinned separately by
    the `--- lease release ---` tests below."""
    def fake_run(cmd, *, capture_output, text, timeout, env, input=None):
        calls.append(cmd)
        assert capture_output is True and text is True
        assert isinstance(cmd, list)  # argv as a list, never a shell string
        # The assertion is for `kb apply` alone; checkout and release take none.
        if cmd[1:3] != ["kb", "apply"]:
            assert input is None, f"{cmd[1:3]} was handed stdin"
        if cmd[1:3] == ["kb", "checkout"]:
            return checkout_result(cmd)
        if cmd[1:3] == ["kb", "release"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        assert cmd[1:3] == ["kb", "apply"]
        written["stdin"] = input
        idx = cmd.index("--content-file")
        from pathlib import Path
        path = Path(cmd[idx + 1])
        written["body"] = path.read_text()
        written["path"] = path
        written["mode"] = stat.S_IMODE(path.stat().st_mode)
        return apply_result(cmd)
    return fake_run


def test_commit_document_happy_path(monkeypatch):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        lambda cmd: _checkout_ok(cmd, sha="sha-abc123"),
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="kb apply: updated", stderr=""),
        calls, written))

    kb_commit.commit_document(DOC, body="new text", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert len(calls) == 3  # checkout, apply, release
    checkout_cmd, apply_cmd, release_cmd = calls
    holder = ACTOR
    assert checkout_cmd == ["agentctl", "kb", "checkout", "--holder", holder, "--", DOC]
    assert apply_cmd[0:2] == ["agentctl", "kb"] and apply_cmd[2] == "apply"
    assert apply_cmd[-2:] == ["--", DOC]  # doc_id reaches agentctl only after `--`
    assert apply_cmd[apply_cmd.index("--base-sha") + 1] == OPEN_SHA
    assert apply_cmd[apply_cmd.index("--provenance") + 1] == "person"
    # The assertion reaches agentctl on stdin and nowhere else.
    assert written["stdin"] == ASSERTION
    assert all(ASSERTION not in part for c in calls for part in c)
    # No actor flag: agentctl takes the person from the token it verified, so
    # nothing this service chooses can name who the commit is attributed to.
    assert not any(part.startswith("--actor") or part == "--person" for part in apply_cmd)
    # Same (derived) holder identity passed to both leases (module docstring:
    # "at least legible as one act" — the two lease mechanisms are not
    # reconciled here).
    assert apply_cmd[apply_cmd.index("--holder") + 1] == holder
    assert written["body"] == "new text"
    assert release_cmd == ["agentctl", "kb", "release", "--holder", holder, "--", DOC]


def _apply_gated_on(current_sha):
    """A fake `kb apply` that gates on `--base-sha` the way the real one does
    (`cmd_kb_apply` in agent-platform: exit 5, "stale base", when the base
    differs from the sha of the file origin/main holds), so a test can tell
    WHICH sha reached the flag from whether the write lands."""
    def apply_result(cmd):
        base = cmd[cmd.index("--base-sha") + 1]
        if base != current_sha:
            return subprocess.CompletedProcess(cmd, 5, stdout="",
                stderr=f"kb apply: refused - stale base. You wrote against {base[:12]}, "
                       f"origin/main holds {current_sha[:12]}.")
        return subprocess.CompletedProcess(cmd, 0, stdout="kb apply: updated", stderr="")
    return apply_result


def test_apply_allows_overwrite_and_passes_the_callers_sha_as_base(monkeypatch):
    """`kb apply` refuses a whole-document replacement of an existing document
    without `--allow-overwrite` (exit 1, "target exists, so use edits"), and
    the editor only ever submits whole documents, so without the flag every
    save came back 502 (2026-09-18). The flag is only acceptable because
    `--base-sha` stays beside it, and `--base-sha` guards a stale editor only
    when it is the sha the EDITOR opened at. The fresh checkout here reports a
    different sha (main moved during the edit), so the assertion below cannot
    pass by reading the checkout's."""
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        lambda cmd: _checkout_ok(cmd, sha=MOVED_SHA),
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="kb apply: updated", stderr=""),
        calls, written))

    kb_commit.commit_document(DOC, body="new text", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    apply_cmd = calls[1]
    assert apply_cmd[2] == "apply"
    assert "--allow-overwrite" in apply_cmd
    # A flag after `--` would be read as the doc id, not as a flag.
    assert apply_cmd.index("--allow-overwrite") < apply_cmd.index("--")
    assert apply_cmd[apply_cmd.index("--base-sha") + 1] == OPEN_SHA
    assert MOVED_SHA not in apply_cmd


def test_a_commit_that_landed_while_the_editor_was_open_is_refused(monkeypatch):
    """The lost update `--allow-overwrite` would otherwise permit. The editor
    opened at OPEN_SHA; something else committed (origin/main now holds
    MOVED_SHA); the member saves. The in-request checkout reads MOVED_SHA, so
    a `--base-sha` taken from it would pass the apply's own gate and erase the
    other commit. With the caller's sha the apply refuses, and the human is
    told to reopen."""
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        lambda cmd: _checkout_ok(cmd, sha=MOVED_SHA),
        _apply_gated_on(MOVED_SHA), calls, written))

    with pytest.raises(kb_commit.CommitStale):
        kb_commit.commit_document(DOC, body="my edit", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    # And the same document, opened after that commit, saves.
    calls.clear()
    kb_commit.commit_document(DOC, body="my edit", actor=ACTOR, holder=ACTOR, base_sha=MOVED_SHA, assertion=ASSERTION)
    assert calls[1][calls[1].index("--base-sha") + 1] == MOVED_SHA


@pytest.mark.parametrize("bad", [
    None, "", "abc", "a" * 63, "a" * 65, "A" * 64, "g" * 64,
    "a" * 64 + "\n", " " + "a" * 63, "--allow-overwrite", "-" * 64, 12345, ["a" * 64],
], ids=repr)
def test_a_malformed_base_sha_never_reaches_agentctl(monkeypatch, bad):
    """`base_sha` is client-supplied and becomes an argv element, so a value
    that is not a plain 64-digit hex digest is refused before any subprocess
    runs, not merely before `kb apply`."""
    ran = []
    monkeypatch.setattr(kb_commit.subprocess, "run",
                        lambda *a, **k: ran.append(a) or pytest.fail("agentctl was invoked"))

    with pytest.raises(kb_commit.CommitFailed):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=bad, assertion=ASSERTION)

    assert ran == []


def test_commit_document_hands_agentctl_the_recorded_holder_unchanged(monkeypatch):
    """Every agentctl call takes its lease under the holder the Library's
    checkout recorded, byte for byte. Orient used to reshape it to fit an
    older agentctl rule (`@` became `_`), so the two leases named different
    holders. `actor` names only the commit message."""
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        calls, written))

    kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=RECORDED_HOLDER,
                              base_sha=OPEN_SHA, assertion=ASSERTION)

    checkout_cmd, apply_cmd, release_cmd = calls
    for cmd in (checkout_cmd, apply_cmd, release_cmd):
        assert cmd[cmd.index("--holder") + 1] == RECORDED_HOLDER
    assert apply_cmd[apply_cmd.index("--message") + 1].endswith(f"by {ACTOR}.")


def test_body_travels_only_via_content_file_never_via_argv(monkeypatch):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        calls, written))

    secret_marker = "SECRET-BODY-CONTENT-1234"
    kb_commit.commit_document(DOC, body=secret_marker, actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    apply_cmd = calls[1]
    assert "--content" not in apply_cmd  # only --content-file, never the literal flag
    assert not any(secret_marker in str(a) for a in apply_cmd), \
        "the document body must never appear in argv (readable via ps by any process)"
    assert written["body"] == secret_marker


def test_content_file_is_0600_and_removed_after_success(monkeypatch):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        calls, written))

    kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert written["mode"] == 0o600
    assert not written["path"].exists(), "the content file must be removed after use"


def test_content_file_is_removed_even_when_apply_fails(monkeypatch):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="kb apply: boom"),
        calls, written))

    with pytest.raises(kb_commit.CommitFailed):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert not written["path"].exists(), "the content file must be removed even on failure"


def test_subprocess_env_is_allowlisted_not_the_parent_env_minus_a_few_keys(monkeypatch):
    monkeypatch.setenv("HOME", "/srv/operator")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "shh-do-not-leak-me")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "also-secret")

    seen_envs = []

    def fake_run(cmd, *, capture_output, text, timeout, env, input=None):
        seen_envs.append(env)
        if cmd[1:3] == ["kb", "checkout"]:
            return _checkout_ok(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert len(seen_envs) == 3  # checkout, apply, release
    for env in seen_envs:
        assert set(env.keys()) <= {"HOME", "PATH"}
        assert "SUPER_SECRET_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert env["HOME"] == "/srv/operator"
        assert env["PATH"] == "/usr/bin:/bin"


def test_argv_is_a_list_not_a_shell_string(monkeypatch):
    """Guards against a future `shell=True` regression: every call recorded
    by `_dispatch` already asserts `isinstance(cmd, list)`, so this just
    exercises the path and would fail loudly if that assertion trips."""
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        calls, written))
    kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)
    assert len(calls) == 3  # checkout, apply, release


# --- failure modes, each its own exception -----------------------------------


def test_lease_conflict_raises_commit_lease_conflict_and_never_attempts_apply(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 3, stdout="",
            stderr=f"kb checkout: {DOC} is leased by mem@example.com until 2026-08-25T18:00:00. "
                   f"Wait, or coordinate with that holder.")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitLeaseConflict) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert "leased by mem@example.com" in str(exc.value)
    assert len(calls) == 1, ("apply must never be attempted once the lease is refused, "
                             "and neither must a release — this caller never held it")


def _checkout_then_fail(how, calls):
    """A `subprocess.run` whose `kb checkout` fails when agentctl MAY already
    hold the lease. `kb checkout` commits the lease before its `git fetch
    origin main`, which has no timeout of its own, so a slow fetch is killed by
    `_TIMEOUT_S` with the lease possibly already ours; a nonzero exit is
    ambiguous the same way, since the exit code cannot say whether the failure
    fell before or after `acquire`. Release calls answer 0 and are recorded."""
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["kb", "release"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        assert cmd[1:3] == ["kb", "checkout"], f"ran {cmd[1:3]} after a failed checkout"
        if how == "timeout":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
        return subprocess.CompletedProcess(cmd, 1, stdout="",
            stderr="kb checkout: git fetch origin main failed")
    return fake_run


@pytest.mark.parametrize("how", ["timeout", "nonzero-exit"])
@pytest.mark.parametrize("call", [
    lambda: kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION),
    lambda: kb_commit.read_document(DOC, holder=ACTOR),
], ids=["commit_document", "read_document"])
def test_a_checkout_that_fails_after_taking_the_lease_releases_it(monkeypatch, how, call):
    """The regression this pins: the checkout timed out, `_checkout` raised
    before the caller's `try` opened, so no `finally` ran and the lease
    agentctl may have already taken stood for `LEASE_TTL_SECONDS`, refusing every
    other holder of the document. Asserts the release was actually sent, with
    this actor's holder."""
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run", _checkout_then_fail(how, calls))

    with pytest.raises(kb_commit.CommitFailed):
        call()

    holder = ACTOR
    assert calls == [
        ["agentctl", "kb", "checkout", "--holder", holder, "--", DOC],
        ["agentctl", "kb", "release", "--holder", holder, "--", DOC],
    ]


def test_a_failed_release_after_a_failed_checkout_does_not_replace_the_checkout_error(monkeypatch):
    """The checkout timed out and the release then cannot run either (agentctl
    is wedged). The caller must still see the checkout's failure, not whatever
    the release raised."""
    def fake_run(cmd, **kw):
        if cmd[1:3] == ["kb", "release"]:
            raise FileNotFoundError("agentctl vanished")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)
    assert "timed out" in str(exc.value)


def test_stale_base_sha_raises_commit_stale(monkeypatch):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 5, stdout="",
            stderr="kb apply: refused — stale base. You wrote against aaaaaaaaaaaa, "
                   "origin/main holds sha-def456. Re-checkout, merge your change into "
                   "the current content, and retry."),
        calls, written))

    with pytest.raises(kb_commit.CommitStale) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert "stale base" in str(exc.value)


def test_push_race_also_raises_commit_stale(monkeypatch):
    """Exit 7 (`push-race`) is a different failure point than exit 5 (`stale`)
    — the sha precondition passed but origin advanced during the push — but
    the remediation is identical: reopen and retry, so it gets the same
    exception."""
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 7, stdout="",
            stderr="kb apply: push refused — origin/main advanced while this write was "
                   "in flight. Re-checkout and retry."),
        calls, written))

    with pytest.raises(kb_commit.CommitStale) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)
    assert "advanced" in str(exc.value)


def test_validator_rejection_raises_commit_rejected(monkeypatch):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 6, stdout="",
            stderr="kb apply: refused — this write introduces KB validation errors, so it "
                   "would red knowledge CI. Working tree restored.\nmissing required field: title"),
        calls, written))

    with pytest.raises(kb_commit.CommitRejected) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert "missing required field: title" in str(exc.value)


def test_policy_refusal_raises_commit_rejected(monkeypatch):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 4, stdout="",
            stderr="kb apply: refused — in-session provenance is not allowed on this path"),
        calls, written))

    with pytest.raises(kb_commit.CommitRejected) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)
    assert "not allowed" in str(exc.value)


def test_an_unverifiable_assertion_raises_commit_auth_expired_and_still_releases(monkeypatch):
    """agentctl exits 8 when it cannot verify the Access assertion, and writes
    nothing. That is its own error, so the save can tell the human to reload,
    and the agentctl-side lease is still given back."""
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 8, stdout="",
            stderr="kb apply: refused — provenance 'person' needs a valid Cloudflare "
                   "Access assertion on stdin (expired). Nothing was written."),
        calls, written))

    with pytest.raises(kb_commit.CommitAuthExpired) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)
    assert "expired" in str(exc.value)
    assert not isinstance(exc.value, kb_commit.CommitFailed)
    assert calls[-1][1:3] == ["kb", "release"]


def test_the_assertion_never_reaches_the_subprocess_environment(monkeypatch):
    """stdin is the only channel. The environment is inherited by whatever
    agentctl spawns, so the allow-list must stay HOME and PATH."""
    envs = []

    def fake_run(cmd, *, capture_output, text, timeout, env, input=None):
        envs.append(env)
        if cmd[1:3] == ["kb", "checkout"]:
            return _checkout_ok(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)
    assert kb_commit._ENV_ALLOWLIST == ("HOME", "PATH")
    assert all(set(e) <= {"HOME", "PATH"} for e in envs)
    assert all(ASSERTION not in v for e in envs for v in e.values())


def test_agentctl_missing_raises_commit_failed(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'agentctl'")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)


def test_timeout_raises_commit_failed(monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)


def test_unexpected_nonzero_exit_raises_commit_failed_not_a_bare_failure(monkeypatch):
    """An exit code this module has no name for must still surface its detail
    — not collapse into a bare "save failed"."""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="",
            stderr="kb checkout: knowledge index missing at /srv/operator/.knowledge-index/index.db")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)
    assert "knowledge index missing" in str(exc.value)


def test_checkout_output_that_is_not_json_raises_commit_failed(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json at all", stderr="")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)


def test_checkout_output_that_is_json_but_not_an_object_raises_commit_failed(monkeypatch):
    """`_checkout_payload` refuses a non-object rather than reaching for
    `.get` on it. Before it did, `commit_document` let a JSON list or string
    fall through to `payload.get` and raised `AttributeError`, which the route
    answered as a 500; it is now a `CommitFailed`, answered as a 502. The lease
    the checkout took is still released."""
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(["not", "an", "object"]),
                                                stderr=""),
        lambda cmd: pytest.fail("apply must not run on an unusable checkout"),
        calls, {}))

    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert "not an object" in str(exc.value)
    assert [c[1:3] for c in calls] == [["kb", "checkout"], ["kb", "release"]]


def test_checkout_output_missing_content_sha_raises_commit_failed(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"exists": True}), stderr="")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)


# --- agentctl lease release ---------------------------------------------------
#
# PR #210's review: the agentctl-side lease `kb checkout` takes was never
# released on any path, so any OTHER holder was told the document was leased
# for up to `LEASE_TTL_SECONDS` (4 hours) after the write had already landed
# or been refused. Distinct from the LIBRARY checkout `kb_write_api.py`
# holds, which is deliberately left held on a failed save — that lock
# guards a human's in-progress edit and there is another attempt still to
# make; this one only ever pinned a base-sha for one already-resolved `kb
# apply` call.


@pytest.mark.parametrize("apply_returncode,apply_stderr,exc_type", [
    (5, "kb apply: refused — stale base.", kb_commit.CommitStale),
    (6, "kb apply: refused — validation errors.", kb_commit.CommitRejected),
    (4, "kb apply: refused — policy.", kb_commit.CommitRejected),
    (9, "kb apply: some exit code this module has no name for.", kb_commit.CommitFailed),
])
def test_lease_is_released_even_when_apply_is_refused(
        monkeypatch, apply_returncode, apply_stderr, exc_type):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, apply_returncode, stdout="",
                                                stderr=apply_stderr),
        calls, written))

    with pytest.raises(exc_type):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert len(calls) == 3, "checkout, apply, and release must all be attempted"
    release_cmd = calls[2]
    assert release_cmd[1:3] == ["kb", "release"]
    assert release_cmd[-2:] == ["--", DOC]


def test_lease_is_released_when_checkout_succeeds_but_the_stdout_cannot_be_used(monkeypatch):
    """A failure between a successful `kb checkout` and the `kb apply` call
    (bad JSON, missing content_sha) still took the lease — it must still be
    released, not just the two failure shapes on the `kb apply` side."""
    release_calls = []

    def fake_run(cmd, **kw):
        if cmd[1:3] == ["kb", "checkout"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="not json at all", stderr="")
        release_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert len(release_calls) == 1
    assert release_calls[0][1:3] == ["kb", "release"]


def test_lease_release_failure_does_not_mask_a_successful_commit(monkeypatch):
    """`_release` must never let a broken release subprocess turn a landed
    write into a reported failure — there is nothing a caller could do about
    a release failure, and the write already succeeded."""
    def fake_run(cmd, *, capture_output, text, timeout, env, input=None):
        if cmd[1:3] == ["kb", "checkout"]:
            return _checkout_ok(cmd)
        if cmd[1:3] == ["kb", "release"]:
            raise FileNotFoundError("agentctl vanished mid-run")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)  # must not raise


def test_lease_release_failure_does_not_mask_a_real_commit_error(monkeypatch):
    """Same guarantee as above, but when `kb apply` was itself refused: the
    real `CommitStale` must still surface, not a release-side `CommitFailed`."""
    def fake_run(cmd, *, capture_output, text, timeout, env, input=None):
        if cmd[1:3] == ["kb", "checkout"]:
            return _checkout_ok(cmd)
        if cmd[1:3] == ["kb", "release"]:
            raise FileNotFoundError("agentctl vanished mid-run")
        return subprocess.CompletedProcess(cmd, 5, stdout="",
            stderr="kb apply: refused — stale base.")

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitStale):
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)


# --- stderr sanitization ------------------------------------------------------


def test_stderr_control_characters_are_stripped_from_the_exception(monkeypatch):
    poisoned = "kb apply: refused\x1b[31;1mFAKE LOG LINE\x1b[0m\x07\x00"

    def fake_run(cmd, **kw):
        if cmd[1:3] == ["kb", "checkout"]:
            return _checkout_ok(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=poisoned)

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    message = str(exc.value)
    for ch in message:
        assert ch >= " ", f"unescaped control character leaked into: {message!r}"


def test_stderr_sanitization_covers_del_nel_and_unicode_line_separators(monkeypatch):
    """Broadened per `issue_01M0VBZRCA15QSVYFMEF8MZ8N0` (PR #210 security
    review): the original rule (`ch == "\\n" or ch >= " "`) stripped only C0
    controls, leaving DEL (0x7F), NEL (U+0085), LINE SEPARATOR (U+2028) and
    PARAGRAPH SEPARATOR (U+2029) untouched — every one of which
    `str.splitlines()` treats as a line boundary, so a validator quoting an
    attacker-controlled document body back in its stderr could forge an
    extra line through this gap. A real newline is no longer preserved
    either: this is a single-line HTTP `detail`/log field, and collapsing
    every control character removes the question entirely rather than
    chasing which ones a line-oriented reader treats specially."""
    poisoned = ("kb apply: refused\x7fDEL" "\x85NEL"
                " LINE-SEP" " PARA-SEP" "\nreal-newline")

    def fake_run(cmd, **kw):
        if cmd[1:3] == ["kb", "checkout"]:
            return _checkout_ok(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=poisoned)

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    message = str(exc.value)
    for banned in ("\x7f", "\x85", " ", " ", "\n"):
        assert banned not in message, f"{banned!r} leaked into: {message!r}"
    assert message.splitlines() == [message], \
        f"a sanitized message must never split into more than one line: {message!r}"
    # replaced with a space, not dropped — the content is still legible
    for word in ("DEL", "NEL", "LINE-SEP", "PARA-SEP", "real-newline"):
        assert word in message


def test_stderr_sanitization_covers_the_rest_of_c1(monkeypatch):
    """`_sanitize` used to special-case only 0x85 (NEL) within C1 and let the
    rest of the range (0x80-0x84, 0x86-0x9F) through untouched —
    `logutil.clean_log_field`, which `_sanitize` now delegates to, covers all
    of C1 (`issue_01M1KJ4VCR127XJC9SMKPRVGRG`)."""
    poisoned = "kb apply: refused\x9fEND-OF-C1"

    def fake_run(cmd, **kw):
        if cmd[1:3] == ["kb", "checkout"]:
            return _checkout_ok(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=poisoned)

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    message = str(exc.value)
    assert "\x9f" not in message, f"\\x9f leaked into: {message!r}"
    assert "END-OF-C1" in message


def test_stderr_is_capped_in_length(monkeypatch):
    huge = "kb apply: refused — " + ("x" * 10_000)

    def fake_run(cmd, **kw):
        if cmd[1:3] == ["kb", "checkout"]:
            return _checkout_ok(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=huge)

    monkeypatch.setattr(kb_commit.subprocess, "run", fake_run)
    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert len(str(exc.value)) <= kb_commit._STDERR_LIMIT + 1  # +1 for the truncation marker


# --- the subprocess invocation goes through paths.resolve_tool -------------
#
# issue_01M1DKJYHSH9FT1SBP9068MTT1: `_AGENTCTL` used to be a bare literal that
# `shutil.which` resolved directly against PATH, which never includes
# `~/.local/bin` on the deploy-pinned plist. These pin that the actual
# subprocess invocation goes through the resolver, so a future edit that
# reintroduces a literal `"agentctl"` fails here rather than only in production.


def test_commit_document_invokes_the_resolver_returned_path_not_the_bare_name(monkeypatch):
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.paths, "resolve_tool",
                         lambda name: str(kb_commit.paths._DEPLOY_TOOL_BIN / "agentctl"))
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        calls, written))

    kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert len(calls) == 3  # checkout, apply, release
    for cmd in calls:
        assert cmd[0] == str(kb_commit.paths._DEPLOY_TOOL_BIN / "agentctl")


def test_commit_document_resolves_agentctl_exactly_once_and_reuses_it(monkeypatch):
    """`_run` must never re-resolve per subprocess — checkout, apply, and
    release all invoke the one path resolved at the top of `commit_document`,
    so a single save can't straddle two different binaries."""
    resolved_for = []
    monkeypatch.setattr(kb_commit.paths, "resolve_tool",
                         lambda name: resolved_for.append(name) or "agentctl")
    calls, written = [], {}
    monkeypatch.setattr(kb_commit.subprocess, "run", _dispatch(
        _checkout_ok,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        calls, written))

    kb_commit.commit_document(DOC, body="x", actor=ACTOR, holder=ACTOR, base_sha=OPEN_SHA, assertion=ASSERTION)

    assert resolved_for == ["agentctl"]  # exactly one resolve call for all 3 subprocesses


# --- read_document: the editor's load path --------------------------------------
#
# The Library serves a document's body with its frontmatter stripped, and
# `save_document` refuses a body that does not open with a `---` block, so the
# editor must open on the stored FILE. `read_document` borrows `kb checkout` for
# that and must hand the lease straight back.

_STORED_FILE = ("---\nid: proj/kind/doc-1\ntitle: A Document\naudience:\n- team\n---\n\n"
                "> **Provenance:** migrated from somewhere\n\n# A Document\n\nBody text.\n")


def _stored_checkout(cmd, *, content=_STORED_FILE, sha="sha-stored", exists=True):
    return subprocess.CompletedProcess(cmd, 0,
        stdout=json.dumps({"source_ref": "projects/proj/kind/doc-1.md", "exists": exists,
                           "content": content, "content_sha": sha}), stderr="")


def _read_dispatch(checkout_result, calls, *, release_raises=None):
    def fake_run(cmd, *, capture_output, text, timeout, env, input=None):
        calls.append(cmd)
        if cmd[1:3] == ["kb", "checkout"]:
            return checkout_result(cmd)
        assert cmd[1:3] == ["kb", "release"], f"read_document ran {cmd[1:3]}"
        if release_raises:
            raise release_raises
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return fake_run


def test_read_document_returns_the_stored_file_with_its_frontmatter(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run",
                        _read_dispatch(_stored_checkout, calls))

    stored = kb_commit.read_document(DOC, holder=ACTOR)

    assert stored.content.startswith("---\n")
    assert stored.content == _STORED_FILE
    assert stored.content_sha == "sha-stored"
    holder = ACTOR
    assert calls == [
        ["agentctl", "kb", "checkout", "--holder", holder, "--", DOC],
        ["agentctl", "kb", "release", "--holder", holder, "--", DOC],
    ]


@pytest.mark.parametrize("stdout", [
    "not json at all",                                         # unparseable
    json.dumps(["not", "an", "object"]),                       # parses, wrong shape
    json.dumps({"exists": True, "content": _STORED_FILE}),     # no content_sha
    json.dumps({"exists": True, "content": "", "content_sha": "s"}),   # no content
    # `kb checkout` exits 0 with exists:false and empty content when the fetch
    # of origin/main failed as well as when the file is absent.
    json.dumps({"exists": False, "content": "", "content_sha": "sha-of-nothing"}),
])
def test_read_document_releases_the_lease_when_the_payload_is_unusable(monkeypatch, stdout):
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run", _read_dispatch(
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""), calls))

    with pytest.raises(kb_commit.CommitFailed):
        kb_commit.read_document(DOC, holder=ACTOR)

    assert [c[1:3] for c in calls] == [["kb", "checkout"], ["kb", "release"]], \
        "the lease `kb checkout` took must be released even though the read failed"


def test_read_document_releases_the_lease_on_an_exception_it_has_no_name_for(monkeypatch):
    """The release sits in a `finally`, so an error nobody anticipated between
    the checkout and the return cannot strand the lease for its 4-hour TTL."""
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run",
                        _read_dispatch(_stored_checkout, calls))

    def boom(_checkout):
        raise RuntimeError("unforeseen")

    monkeypatch.setattr(kb_commit, "_checkout_payload", boom)
    with pytest.raises(RuntimeError):
        kb_commit.read_document(DOC, holder=ACTOR)

    assert [c[1:3] for c in calls] == [["kb", "checkout"], ["kb", "release"]]


def test_read_document_holds_no_lease_after_it_returns(monkeypatch):
    """The lease is released before the caller sees the content, not deferred
    to the caller: an editing session is minutes to hours long."""
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run",
                        _read_dispatch(_stored_checkout, calls))
    kb_commit.read_document(DOC, holder=ACTOR)
    assert calls[-1][1:3] == ["kb", "release"]


def test_a_failed_release_does_not_lose_the_content_that_was_read(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run", _read_dispatch(
        _stored_checkout, calls, release_raises=FileNotFoundError("agentctl vanished")))
    assert kb_commit.read_document(DOC, holder=ACTOR).content == _STORED_FILE


def test_read_document_reports_another_holders_lease_as_a_lease_conflict(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run", _read_dispatch(
        lambda cmd: subprocess.CompletedProcess(cmd, 3, stdout="",
            stderr="kb checkout: proj/kind/doc-1 is leased by someone-else"), calls))

    with pytest.raises(kb_commit.CommitLeaseConflict) as exc:
        kb_commit.read_document(DOC, holder=ACTOR)

    assert "leased by someone-else" in str(exc.value)
    assert [c[1:3] for c in calls] == [["kb", "checkout"]], \
        "no lease of ours was taken, so there is nothing to release"


def test_read_document_reports_an_agentctl_failure_as_commit_failed(monkeypatch):
    monkeypatch.setattr(kb_commit.subprocess, "run", _read_dispatch(
        lambda cmd: subprocess.CompletedProcess(cmd, 1, stdout="",
            stderr="kb checkout: knowledge index missing"), []))
    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.read_document(DOC, holder=ACTOR)
    assert "knowledge index missing" in str(exc.value)


def test_read_document_hands_agentctl_the_recorded_holder_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_commit.subprocess, "run",
                        _read_dispatch(_stored_checkout, calls))
    kb_commit.read_document(DOC, holder=RECORDED_HOLDER)
    assert [cmd[1:3] for cmd in calls] == [["kb", "checkout"], ["kb", "release"]]
    for cmd in calls:
        assert cmd[cmd.index("--holder") + 1] == RECORDED_HOLDER


def test_read_document_says_origin_was_unreadable_when_checkout_reports_exists_false(monkeypatch):
    """Exit 0 with `exists: false` is how `kb checkout` reports a failed fetch
    as well as an absent file. The empty content would trip the generic "no
    content" refusal on its own, so this pins that the `exists` check is what
    speaks, and names origin/main as the thing that could not be read."""
    monkeypatch.setattr(kb_commit.subprocess, "run", _read_dispatch(
        lambda cmd: _stored_checkout(cmd, content="", sha="sha-of-nothing", exists=False), []))
    with pytest.raises(kb_commit.CommitFailed) as exc:
        kb_commit.read_document(DOC, holder=ACTOR)
    assert "origin/main" in str(exc.value)
