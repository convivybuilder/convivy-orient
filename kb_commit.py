"""Committing a KB document edit to the knowledge repository — the in-window
editor's save path (issue_01M0TKX6DW2E1TXGN9QT6K61J5 §4, "option C").

**The split, ruled on by the operator and recorded in ADR-0020 (amended by
knowledge#288): the Library ARBITRATES, the Bosun EXECUTES.** Orient holds
no clone of the knowledge repo and no credential scoped to it; `agentctl kb
apply` holds all three — the clone, the credential, and the cage
(sha-gated, policy-gated, validated). This module's whole job is to shell
out to it correctly and translate its outcomes into exceptions
`kb_write_api.save_document` can answer specifically rather than as one
generic failure.

**Two leases, deliberately not reconciled here — and released on opposite
schedules.** `kb_write_api.py` already holds the Library's own checkout
(`library_client.checkout_reference_document`) before this module is ever
called — that is the arbitration lock the spec above names, and
`kb_write_api.save_document` leaves it held on every failure branch: nothing
was written, so there is nothing to release, and a human mid-edit keeps their
lock rather than losing it to a failed save. `agentctl kb checkout` below
takes a SECOND, unrelated lease in agentctl's own store. The Library's
`ReferenceCheckout` carries no content-sha, so the editor's open-time sha comes
from `agentctl kb checkout` too (`read_document`), and the save posts it back
as `base_sha`. Wiring the two together is filed as
`issue_01M0YAS4S64P5TXN7S2C17GHF6` against agent-platform and is not this
module's job — the same holder identity is used for both, so the two leases
are at least legible as one act by one person.

**This second lease IS released here, on every path, success or failure** —
the opposite of the Library checkout above. It exists only to hold
agentctl's lock for one `kb apply` attempt; once that attempt has resolved,
whether it landed or was refused, there is nothing left for it to protect, and
leaving it held would tell every other holder this document is locked for up
to `LEASE_TTL_SECONDS` (4 hours) after the write is already over.

**`read_document` takes the same lease only to read through it, and releases
it before returning.** The editor needs the stored file, frontmatter included,
because the Library serves a body with the frontmatter stripped and a save must
commit the whole file. `agentctl kb checkout` is the one place that hands out
the raw file and its content-sha, so the read borrows that call and gives the
lease straight back rather than holding it for a human's editing session.

**Provenance is `"person"`: a person typed this, and agentctl checks.** The operator's
ruling of 2026-09-23 is that only deterministic code triggered by a human
action may claim `person`. So the save hands `agentctl kb apply` the request's
raw Cloudflare Access assertion on STDIN, never on argv or in the environment
(`_ENV_ALLOWLIST` stays `HOME`/`PATH`). agentctl verifies it against the Access
JWKS, requires the email to be on its own `persons` allowlist, and records the
email IT verified in a `KB-Person:` trailer. No actor flag is sent, so the
person line cannot be a value this service chose. Any verification failure
exits 8 and writes nothing, which surfaces here as `CommitAuthExpired`. This
replaced `"in-session"`, which labelled a person's own edit as an agent's
attestation that the operator agreed (issue_01M2BVTW39V4GR791J2PARA26G). `"proposal"`
was never right either: it takes the PR path, and the operator's ruling on the editor
was "direct commit for full-tier humans, not branch-and-PR".

`_TIMEOUT_S` now also covers agentctl's JWKS fetch, which agentctl caps at 5 s
and usually answers from its local key cache.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

import paths
from logutil import clean_log_field

_AGENTCTL_NAME = "agentctl"

# `agent-platform`'s `kb_writes.py`: `HOLDER_RE = ^[A-Za-z0-9][A-Za-z0-9:._-]{0,63}$`.
# A verified Cloudflare Access email — the only `actor` this module is ever
# called with — fails it outright on the `@`; agentctl refuses the whole
# `kb checkout`/`kb apply` call before the stale-base check, before policy,
# before anything else. `_HOLDER_UNSAFE_RE` matches everything that regex
# does not allow, so `_holder_for` can turn any `actor` into a value the gate
# accepts without this module needing to track that regex's exact grammar.
_HOLDER_UNSAFE_RE = re.compile(r"[^A-Za-z0-9:._-]")

# `kb_writes.content_sha` is `sha256(...).hexdigest()`: 64 lowercase hex digits.
# A save's `base_sha` is CLIENT-SUPPLIED and becomes an argv element of `kb
# apply`, so its shape is checked against this before it can reach a
# subprocess (`kb_write_api.save_document` refuses a save whose sha fails it,
# and `commit_document` re-checks). `fullmatch`, never `match`: this pattern is
# unanchored, so `match` would accept 64 hex digits followed by anything at all.
_CONTENT_SHA_RE = re.compile(r"[0-9a-f]{64}")


def is_content_sha(value: object) -> bool:
    """True only for a plain 64-digit lowercase hex digest."""
    return isinstance(value, str) and _CONTENT_SHA_RE.fullmatch(value) is not None


# `agentctl kb checkout`/`kb apply` do a git fetch, and `apply` additionally
# resets a worktree, commits, pushes, fetches again and reindexes — real
# network round trips to GitHub, not a local-only operation, so this is
# generous rather than tight.
_TIMEOUT_S = 60

# An ALLOWLIST, not the parent env minus a few keys (issue_01KYMHAGQA0M5E1W3D4735H2HY:
# a denylist is one forgotten secret name away from exposure). `agentctl kb
# apply` shells to `git` and to `gh`'s credential helper, both of which need
# HOME (to find ~/.gitconfig and ~/.config/gh/hosts.yml) and PATH (to find the
# git/gh/python binaries themselves) — nothing else is load-bearing, and this
# fails closed if that ever stops being true.
_ENV_ALLOWLIST = ("HOME", "PATH")

# Cap on how much of a subprocess's stderr is worth keeping — long enough for
# a real error, short enough that a document body reflected back by a
# validator error can't turn a refusal into a multi-hundred-KB response.
_STDERR_LIMIT = 2000


class CommitError(Exception):
    """Base class for every way `agentctl kb apply` can refuse or fail to
    write `commit_document`'s call. Callers that don't need to distinguish
    outcomes can catch this instead of each subclass."""


class CommitStale(CommitError):
    """origin/main holds a different version of the document than the one the
    editor opened at — the base-sha precondition was refused. The human must
    reopen the document to read the current version before saving again;
    their edit is not lost, just not yet reconciled against what changed
    underneath it."""


class CommitLeaseConflict(CommitError):
    """Another holder has `doc_id`'s agentctl-level lease. Distinct from the
    409 `kb_write_api.save_document` already returns when the Library
    checkout is held by someone else — this is the second, agentctl-side
    lease (see the module docstring)."""


class CommitRejected(CommitError):
    """`agentctl kb apply` refused the write on its own terms: the KB
    validator rejected the resulting document, or policy refused this
    provenance for this path. The message names which."""


class CommitAuthExpired(CommitError):
    """`kb apply` exit 8: agentctl could not verify the Cloudflare Access
    assertion this save carried (absent, expired, not yet valid, a bad
    signature, or an email off its `persons` allowlist), so it wrote nothing.
    The fix for the human is to reload the page and take a fresh assertion."""


class CommitFailed(CommitError):
    """`agentctl` could not be run, or exited nonzero for a reason not named
    by the more specific exceptions above — missing binary, a timeout, a git
    failure, an exit code this module has no name for."""


def _subprocess_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}


def _holder_for(actor: str) -> str:
    """`actor` (a verified Cloudflare Access email) reshaped into a value
    agentctl's `HOLDER_RE` accepts, by replacing every disallowed character
    with `_` and capping length at 64. `operator@example.com` becomes
    `operator_example.com` — the `@` is the only character an email carries that
    the regex disallows, `.` is already permitted.

    **Collision-free for `tiers.json`'s current `full` list — not injective
    in general, and that is a known, accepted gap, not an oversight**
    (security review, PR #210 round 3). The substitution is NOT collision-
    free for Cloudflare Access emails at large: `a_b@c.example` and `a@b_c.example`
    both reduce to `a_b_c.example`, and the 64-character cap collapses any pair
    that differs only past that point. `commit_document` is reachable only
    through `save_document`'s gate, which requires `tier_for(actor) ==
    "full"`, and on this deployment `tiers.json` is a small, static,
    hand-edited allowlist (`operator@example.com`, `contributor@example.com` at the time
    of this review) that does not collide under this substitution — confirmed
    by direct inspection, not assumed. The agentctl-side lease this holder
    names is taken and released within a single `commit_document` or
    `read_document` call, not held for an editing session, so even a real
    collision would only let two already-authenticated, same-tier `kb.write`
    humans confuse each other's lease/release calls for the few seconds one
    save takes; it cannot forge a write, since `kb apply`'s `--base-sha` gate
    is keyed off content-sha, not the holder string. Making the substitution
    injective (e.g. appending a hash suffix of `actor` before the cap) was
    considered and rejected: it would buy protection against only one
    realistic trigger — the operator hand-adding a third, colliding full-tier identity
    to a file they edit and can see in full at edit time — at the permanent
    cost of every lease-conflict message reading a hash suffix instead of a
    name, for every future reader. If a third full-tier identity is ever added
    that collides with an existing one under this substitution, that call
    should be revisited.

    The real `actor` still appears in the commit message (`commit_document`
    below); only the `--holder` value is reshaped, since that is the one
    agentctl itself parses as an identity token rather than free text."""
    safe = _HOLDER_UNSAFE_RE.sub("_", actor)[:64]
    if not safe or not safe[0].isalnum():
        safe = ("h" + safe)[:64]
    return safe


def _sanitize(text: str) -> str:
    """Strip control characters and cap length before a subprocess's stderr —
    which can contain content the caller influenced, like a document body a
    validator quoted back — reaches a log line or an HTTP response.

    The control-character rule itself is `logutil.clean_log_field`, shared
    with `server.py` and `chart_house_build.py`
    (`issue_01M0VBZRCA15QSVYFMEF8MZ8N0`); replaced with a space rather than
    dropped, so a caller cannot erase evidence of what it sent by embedding
    the delimiter. The two things layered on top here are this module's own:
    stripping leading/trailing whitespace and capping length at
    `_STDERR_LIMIT` with a trailing ellipsis are about shaping a subprocess's
    stderr for a log line or HTTP response, not about the control-character
    rule, so they stay here rather than in the shared function."""
    cleaned = clean_log_field(text).strip()
    if len(cleaned) > _STDERR_LIMIT:
        cleaned = cleaned[:_STDERR_LIMIT] + "…"
    return cleaned


def _run(agentctl: str, args: list[str], *,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run `<agentctl> <args>` as a list (never a shell string), capturing
    output rather than letting it reach this process's own stdout/stderr.

    `agentctl` is the path `commit_document` already resolved once via
    `paths.resolve_tool` — this never re-resolves it, so every subprocess in
    one `commit_document` call invokes the same binary.

    `stdin` is how a secret reaches agentctl: a credential on argv is readable
    by every process on the machine through `ps`, and one in the environment is
    inherited by whatever agentctl spawns. It is passed only when given, so the
    calls that carry no secret run exactly as before."""
    extra = {"input": stdin} if stdin is not None else {}
    try:
        return subprocess.run(
            [agentctl, *args],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
            env=_subprocess_env(), **extra,
        )
    except FileNotFoundError as e:
        raise CommitFailed(f"agentctl is not on PATH: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise CommitFailed(f"agentctl timed out after {_TIMEOUT_S}s running "
                           f"{' '.join(args[:2])}") from e
    except OSError as e:
        raise CommitFailed(f"could not run agentctl: {e}") from e


def _release(agentctl: str, doc_id: str, holder: str) -> None:
    """Best-effort release of the agentctl-side lease `commit_document` took.
    Never raises: a release failure must not replace the outcome of the
    commit itself (success, or a specific `CommitError` already raised) with
    an unrelated one, and there is nothing a caller could do differently in
    response — the lease still expires on its own TTL either way."""
    try:
        _run(agentctl, ["kb", "release", "--holder", holder, "--", doc_id])
    except CommitFailed:
        pass


def _checkout(agentctl: str, doc_id: str, holder: str) -> subprocess.CompletedProcess:
    """`agentctl kb checkout` for `doc_id`, returned only when it succeeded, so
    a caller releases only what this returns; this releases its own failures.

    **`kb checkout` commits the lease before it runs `git fetch origin main`**,
    and that fetch has no timeout of its own. A checkout that exceeds
    `_TIMEOUT_S` (which kills agentctl and raises `CommitFailed` out of `_run`)
    may already hold the lease, and would strand it for `LEASE_TTL_SECONDS`,
    refusing every other holder of this document, the Library's own
    `kb_update` and `kb_propose` included. A nonzero exit is ambiguous the same
    way: a failed fetch exits 0 with `exists: false`, so a nonzero exit means
    an uncaught agentctl error, which can land after the lease was taken, or a
    bad doc ref or holder, which lands before it and leaves nothing to release.
    The exit code cannot tell those apart, so every failure but a conflict
    releases before it raises, and releasing a lease that was never taken is a
    harmless no-op. Callers open their `try` only after this returns, so this
    is the one place that can.

    Exit 3 is another holder's live lease, which is provably not ours, so it
    raises without a release."""
    try:
        checkout = _run(agentctl, ["kb", "checkout", "--holder", holder, "--", doc_id])
    except CommitFailed:
        _release(agentctl, doc_id, holder)
        raise
    if checkout.returncode == 3:
        raise CommitLeaseConflict(_sanitize(checkout.stderr) or
                                  f"{doc_id} is leased by another holder")
    if checkout.returncode != 0:
        _release(agentctl, doc_id, holder)
        raise CommitFailed(_sanitize(checkout.stderr) or
                           f"agentctl kb checkout exited {checkout.returncode}")
    return checkout


def _checkout_payload(checkout: subprocess.CompletedProcess) -> dict:
    try:
        payload = json.loads(checkout.stdout)
    except json.JSONDecodeError as e:
        raise CommitFailed("agentctl kb checkout returned output that could not "
                           "be parsed as JSON") from e
    if not isinstance(payload, dict):
        raise CommitFailed("agentctl kb checkout returned JSON that is not an object")
    return payload


class StoredDocument(NamedTuple):
    """A document's file as `origin/main` holds it: `content` is the raw file,
    frontmatter block included, and `content_sha` is what `kb apply --base-sha`
    would need to write against exactly this version."""
    content: str
    content_sha: str


def read_document(doc_id: str, *, actor: str) -> StoredDocument:
    """The stored file for `doc_id`, frontmatter and body together, as
    `origin/main` holds it — what the editor opens on.

    The Library's `ReferenceDocument.body` has its frontmatter stripped, and
    `kb_write_api.save_document` refuses any body that does not open with a
    `---` block, so an editor seeded from that body could never save. `agentctl
    kb checkout` emits the raw file and its content-sha; this borrows that call
    for the read. Raises the same `CommitError` subclasses `commit_document`
    does for the same conditions (`CommitLeaseConflict`, `CommitFailed`).

    **`kb checkout` exits 0 with `exists: false` and empty content when it
    could not fetch origin/main as well as when the file is missing**, so an
    exit code alone would hand the editor an empty document. Both raise
    `CommitFailed` here: an editor opened on nothing submits nothing, and the
    member would learn that only at save time."""
    holder = _holder_for(actor)
    agentctl = paths.resolve_tool(_AGENTCTL_NAME) or _AGENTCTL_NAME
    checkout = _checkout(agentctl, doc_id, holder)
    try:
        payload = _checkout_payload(checkout)
        content, content_sha = payload.get("content"), payload.get("content_sha")
        if payload.get("exists") is not True:
            raise CommitFailed(f"agentctl kb checkout could not read {doc_id} "
                               "from origin/main (the fetch failed or the file "
                               "is not there)")
        if not isinstance(content, str) or not content or not content_sha:
            raise CommitFailed("agentctl kb checkout returned no content or "
                               "content_sha")
        return StoredDocument(content=content, content_sha=content_sha)
    finally:
        # Released immediately, and this is load-bearing rather than tidiness.
        # The agentctl lease exists to hold agentctl's own lock for ONE `kb
        # apply` attempt (see the module docstring). It pins no sha: the write
        # is gated on the sha the EDITOR opened at, which the save posts back.
        # This call only wanted the file's content, and the lease came along
        # with the checkout. Held for as long
        # as a human spends editing, it would refuse every other writer of this
        # document, the Library's own `kb_update` and `kb_propose` included, for
        # up to `LEASE_TTL_SECONDS`. `commit_document` takes its own fresh lease
        # at save time, so nothing here is needed again. In a `finally` so an
        # exception between the checkout and this line cannot strand it. A
        # checkout that fails releases inside `_checkout`, before this `try`
        # opens; together they cover every path that took the lease.
        _release(agentctl, doc_id, holder)


def commit_document(doc_id: str, *, body: str, actor: str, base_sha: str,
                    assertion: str) -> None:
    """Commit `body` as `doc_id`'s new content, authored as `actor` — the
    verified Cloudflare Access identity, never a value taken from the
    request, matching the structural guarantee `blog_api.save_draft` gives
    its own author field.

    `base_sha` is the content-sha the EDITOR OPENED the document at (the
    `content_sha` the checkout route returned, posted back with the save). It
    is what gates the write, so a commit that landed on origin/main while the
    human was typing is refused rather than overwritten.

    `assertion` is the request's raw `Cf-Access-Jwt-Assertion`, handed to
    `kb apply` on stdin. agentctl verifies it and records the email it
    verified as the commit's person, so `actor` names only the lease holder
    and the message line, never who the commit is attributed to.

    Two `agentctl` calls, sequential: `kb checkout` to take the agentctl-side
    lease, then `kb apply` to perform the sha-gated, policy-gated, validated
    write. The checkout's own `content_sha` is read seconds before the apply,
    so it describes origin/main as of now and would match whatever landed
    during the editing session; it is only checked to be present, and is never
    what `kb apply` compares against. Raises one of the `CommitError`
    subclasses above on any refusal or failure; returns `None` only once the
    write has actually landed on origin/main.

    `doc_id` reaches agentctl only after `--`, in both calls: it is
    Library-controlled state this service does not govern, so nothing here
    should depend on no id in the corpus ever starting with `-`."""
    if not is_content_sha(base_sha):
        # Callers validate first (`save_document` answers 400). This is the
        # last check before the value becomes an argv element.
        raise CommitFailed("the save carries no valid base content-sha")
    holder = _holder_for(actor)
    # Resolved once and reused for every subprocess below (paths.resolve_tool's
    # own contract) — falls back to the bare name so a deployment where
    # agentctl resolves nowhere still fails the same way it always has
    # (FileNotFoundError from subprocess.run -> CommitFailed), rather than
    # this module inventing a new failure mode for that case.
    agentctl = paths.resolve_tool(_AGENTCTL_NAME) or _AGENTCTL_NAME
    checkout = _checkout(agentctl, doc_id, holder)

    try:
        payload = _checkout_payload(checkout)
        if not payload.get("content_sha"):
            raise CommitFailed("agentctl kb checkout returned no content_sha")

        fd, content_path_s = tempfile.mkstemp(prefix="kb-save-", suffix=".md")
        content_path = Path(content_path_s)
        try:
            os.chmod(content_path, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(body)

            # `--allow-overwrite` is required: `kb apply` refuses a whole-
            # document replacement of an existing document without it (exit 1,
            # "target exists, so use edits"). The editor has no edit-anchor
            # model. A human edits a textarea and submits the whole document,
            # so replacing it is what the request is.
            #
            # What covers what: `--base-sha` is the CALLER's open-time sha,
            # so it is the stale-editor guard. It fails the write when
            # origin/main holds a different version than the editor opened
            # (exit 5, `CommitStale`, a 409 telling the human to reopen). The
            # `kb checkout` above only takes the agentctl-side lease; its sha
            # covers nothing across the editing session, and using it here
            # would let this flag overwrite a commit that landed while the
            # human typed. Never drop `--base-sha`, and never feed it the
            # checkout's sha, on the strength of this flag being present.
            apply_ = _run(agentctl, [
                "kb", "apply",
                "--allow-overwrite",
                "--base-sha", base_sha,
                "--provenance", "person",
                "--content-file", str(content_path),
                "--holder", holder,
                "--message", f"Edited in the Orient KB editor by {actor}.",
                "--", doc_id,
            ], stdin=assertion)
        finally:
            content_path.unlink(missing_ok=True)

        if apply_.returncode == 0:
            return

        detail = _sanitize(apply_.stderr)
        if apply_.returncode == 5:
            raise CommitStale(detail or "origin/main moved since checkout; re-open the "
                                        "document and retry")
        if apply_.returncode == 7:
            raise CommitStale(detail or "origin/main advanced while this write was in "
                                        "flight; re-open the document and retry")
        if apply_.returncode == 6:
            raise CommitRejected(detail or "the KB validator rejected this write")
        if apply_.returncode == 4:
            raise CommitRejected(detail or "policy refused this write")
        if apply_.returncode == 8:
            raise CommitAuthExpired(detail or "agentctl could not verify the Access "
                                              "assertion; nothing was written")
        raise CommitFailed(detail or f"agentctl kb apply exited {apply_.returncode}")
    finally:
        _release(agentctl, doc_id, holder)
