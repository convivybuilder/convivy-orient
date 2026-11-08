#!/usr/bin/env python3.11
"""Push the Field Notes corpus to `convivy/swabbys-journal`, which Shipwright's
site renders at https://convivybuilder.org/journal/.

**Orient writes, then starts the site build.** After a publish that made a new
commit, `push_corpus` starts Shipwright's `site.yml` workflow on `main` through
`workflow_dispatch`, so a post goes live a couple of minutes later when the
dispatch starts. The dispatch is one attempt and never retried: a lost one is
caught up by the site's next scheduled run. A publish that made no commit sends no
dispatch, since the site already holds that corpus.

**Every publish writes the whole published corpus to `corpus.json` on `main`**,
via GitHub's Git Data API (blob, tree, commit, ref), not a `git push`, because
this process has no working tree of that repository to push from. Each publish
makes one fast-forward commit on the current head (`force: false`). The tree
holds only `corpus.json`, so the first publish replaces whatever the
repository's initial commit held. An unchanged corpus makes no commit.

**THE CORPUS IS BUILT FROM THE ARCHIVES, AND HOLDS PUBLISHED TEXT ONLY**
(CQ·170, issue_01M5GBJ3H5BFX0CR83019T737D). `_build_corpus` reads every
`kind=post` signal through `library_client` — there is no local post table
any more — and, per slug, selects the LATEST entry whose `meta["reason"]` is
`"publish"` or `"current"` (a migrated post's one-time mirror of its state
at the Archives copy, `bin/copy_posts_to_archives.py`'s own shape, now
retired but still present in the data it wrote). A `reason="save"` entry —
`blog_api.save_draft`'s write to an already-published slug — is deliberately
excluded from that selection: if it were included, saving a live post would
change what this function returns on the very next push, with no publish in
between, which is exactly the leak the DoD forbids. `updated`, `author`,
`authorship` and `body` come from the LATEST such entry.

**`date` IS THE EARLIEST OF A SLUG'S `first-published` MARKER AND ITS
PUBLICATION ENTRIES** — frozen the way the old `blog_posts.published_at`
column was, so re-publishing an edited post must not lift it to the top of
the site. The marker exists because the Archives copy never recorded
`blog_posts.published_at` itself, so a migrated post's `reason="current"`
entry is dated to the COPY, not the original publication
(written once on 2026-10-24 by CQ·170 part A's script, since removed). A post with no marker —
any post first published after that script's one run — falls back to its
first publication entry, which is this same computation with an empty
marker set.

**BEST EFFORT, AND THAT IS A DESIGN DECISION RATHER THAN LAZINESS.** A publish
that succeeded must not be reported as failed because the push did not land.
`push_corpus` never raises. Every failure is swallowed and REPORTED to the
caller, so the publish response can say "published, push failed: <why>"
without anyone reading a log.

**The credential is a fine-grained token scoped to one repository**, not the
machine's `gh` login, with Contents read/write on `convivy/swabbys-journal` and
nothing else. It lives in `~/.orient-control/journal-push-token` or the
`ORIENT_JOURNAL_PUSH_TOKEN` environment variable. `merge_queue_build`
shells to `gh`, which carries the operator's own credentials and could
write anything they can. Reusing it would hand the publish path the whole
account. Without a token the push is skipped and the skip is reported, because
a missing credential is a configuration state rather than an error.

**The dispatch has its own token, with Actions write on `convivy/shipwright`
and nothing else**, in `~/.orient-control/journal-dispatch-token` or the
`ORIENT_JOURNAL_DISPATCH_TOKEN` environment variable. It is not added to the
push token: a fine-grained token applies its permissions to every repository it
selects, so one token for both would carry Contents write onto the repository
the installer is built from. Without it the dispatch is skipped and reported,
and the post goes live at the site's next scheduled run.

**Host allowlist: `api.github.com` only.**
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import blog_common
import library_client

TOKEN_FILE = Path.home() / ".orient-control" / "journal-push-token"
DISPATCH_TOKEN_FILE = Path.home() / ".orient-control" / "journal-dispatch-token"
REPO = os.environ.get("ORIENT_JOURNAL_REPO", "convivy/swabbys-journal")
TIMEOUT = 10.0
API_ROOT = "https://api.github.com"

# The branch the site reads and the file within it. Both are a fixed convention
# the site's renderer depends on, not something a caller chooses.
BRANCH = "main"
CORPUS_PATH = "corpus.json"

# The workflow that renders the journal, and the branch it builds from. The
# site's `journal` environment admits only `main`, so a dispatch on any other
# ref would be refused its build job.
SITE_DISPATCH_PATH = "/repos/convivy/shipwright/actions/workflows/site.yml/dispatches"
SITE_REF = "main"

# The Library product posts are filed under (platform decomposition on E26,
# issue_01M584RKNA5KKFXE00GSHKEEYD's thread) — matches `blog_api.PRODUCT`.
PRODUCT = "company"
KIND_POST = "post"

# The reasons an Archives entry for a slug counts as a PUBLICATION rather than
# a plain edit: `blog_api.publish_draft`'s own write (`"publish"`), and a
# migrated post's one-time mirror of its state at the Archives copy
# (`"current"`, `bin/copy_posts_to_archives.py`'s shape — the script itself is
# retired, but the data it wrote is still what this corpus reads for a post
# never re-published since). `"save"` is deliberately absent: see the module
# docstring.
_PUBLICATION_REASONS = ("publish", "current")


def _at(sig: library_client.SignalRecord) -> dt.datetime:
    return sig.produced_at or sig.ingested_at


def _build_corpus() -> dict:
    """The published corpus, in the exact shape a rendering surface consumes:
    `{"posts": [...], "count": N}` — `push_corpus`'s own input.

    Reads every `kind=post` Archives signal (never `kind=post_draft`, which a
    draft's own write never promotes to), groups them by slug, and per slug
    keeps only the entries in `_PUBLICATION_REASONS`. A slug with none —
    unreachable today, since the first `kind=post` entry a slug ever gets is
    always a `"publish"` or a migrated `"current"` write — is skipped rather
    than guessed at.

    **`date` ALSO CONSULTS THE SLUG'S `first-published` MARKER, IF ANY** — see
    the module docstring. The marker is never a candidate for `latest`,
    `author`, `authorship` or `body`: its `reason` is not in
    `_PUBLICATION_REASONS`, so it never enters `candidates` at all.

    **`author` here is a display name, never the stored identity.** The
    Archives' `actor` is a verified email for a human post, and this is what
    both a public HTML page and a public git history serve it to — an email
    address must not ride along to either (`security-reviewer`, PR #289, on
    the store this replaced).
    """
    sigs = library_client.list_signals(product=PRODUCT, kind=[KIND_POST])
    by_slug: dict[str, list[library_client.SignalRecord]] = {}
    for sig in sigs:
        by_slug.setdefault(sig.stream or "", []).append(sig)

    posts = []
    for slug, slug_sigs in by_slug.items():
        candidates = [s for s in slug_sigs if (s.meta.get("reason") or "") in _PUBLICATION_REASONS]
        if not candidates:
            continue
        markers = [s for s in slug_sigs
                  if (s.meta.get("reason") or "") == blog_common.FIRST_PUBLISHED_REASON]
        first = min(candidates + markers, key=_at)
        latest = max(candidates, key=_at)
        body = library_client.get_signal_blob(latest.id) or ""
        posts.append({
            "slug": slug,
            "title": latest.meta.get("title") or slug,
            "date": _iso(_at(first)),
            "updated": _iso(_at(latest)),
            "author": blog_common._display_author(latest.actor),
            "authorship": latest.meta.get("authorship") or "human",
            "body": body,
        })
    posts.sort(key=lambda p: p["date"] or "", reverse=True)
    return {"posts": posts, "count": len(posts)}


def _iso(value: dt.datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None  # stored naive UTC


def _read_token(env_var: str, path: Path) -> str | None:
    tok = os.environ.get(env_var, "").strip()
    if tok:
        return tok
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _token() -> str | None:
    return _read_token("ORIENT_JOURNAL_PUSH_TOKEN", TOKEN_FILE)


def _dispatch_token() -> str | None:
    return _read_token("ORIENT_JOURNAL_DISPATCH_TOKEN", DISPATCH_TOKEN_FILE)


def _gh(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """One call against the Git Data API. Raises on any non-2xx status, including
    a 404 on the branch: the branch is created with the repository, so a missing
    ref is an error to report rather than a state to repair."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{API_ROOT}{path}", data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _current_head(token: str) -> str:
    """The commit SHA `main` currently points at."""
    obj = _gh("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}", token)
    return obj["object"]["sha"]


def _current_corpus_blob_sha(token: str, commit_sha: str) -> str | None:
    """The blob SHA `corpus.json` holds at `commit_sha`, or None when the commit
    does not hold it. A publish tree holds only `corpus.json`, so it sits at the
    top level of the listing; a non-recursive listing finds it there."""
    commit = _gh("GET", f"/repos/{REPO}/git/commits/{commit_sha}", token)
    tree = _gh("GET", f"/repos/{REPO}/git/trees/{commit['tree']['sha']}", token)
    for entry in tree.get("tree", []):
        if entry.get("path") == CORPUS_PATH:
            return entry.get("sha")
    return None


def _serialize_corpus(data: dict) -> str:
    """Sorted keys and a trailing newline, so an unchanged corpus produces an
    identical blob byte-for-byte — that identity is what lets `push_corpus`
    skip the commit when nothing changed. Post order is not re-sorted here: it
    is already deterministic, coming from `_build_corpus`'s own sort."""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _failure_reason(e: Exception) -> str:
    # The STATUS only. A GitHub error body can echo the request and the
    # repo, and this string reaches a publish response.
    if isinstance(e, urllib.error.HTTPError):
        return f"github answered {e.code}"
    return type(e).__name__


def dispatch_site_build() -> dict:
    """Start Shipwright's site workflow on `main`. Never raises, never retries.

    Returns `{"dispatched": bool, "reason": str | None}`. GitHub answers 204
    with no body once the run is queued; that says the workflow was started,
    not that the build or the deploy succeeded.
    """
    token = _dispatch_token()
    if token is None:
        return {"dispatched": False,
                "reason": f"no site build credential at {DISPATCH_TOKEN_FILE}"}
    try:
        _gh("POST", SITE_DISPATCH_PATH, token, {"ref": SITE_REF})
        return {"dispatched": True, "reason": None}
    except Exception as e:  # noqa: BLE001 — see the module docstring
        return {"dispatched": False, "reason": _failure_reason(e)}


def push_corpus() -> dict:
    """Write the published corpus to `corpus.json` on `main`, then start the
    site build when that made a new commit. Never raises.

    Returns `{"pushed": bool, "corpus_sha": str | None, "reason": str | None,
    "site_build": dict | None}`. `site_build` is `dispatch_site_build()`'s
    result after a new commit, and None when no commit was made.
    `corpus_sha` is the branch head: the just-created commit, the unchanged
    existing head, or None when nothing could be written at all (no credential,
    or the API call failed). `reason` is unset only on a genuine new commit;
    it explains an unchanged corpus as much as a failure, since both are states
    a caller may want to log.
    """
    token = _token()
    if token is None:
        return {"pushed": False, "corpus_sha": None,
                "reason": f"no journal push credential at {TOKEN_FILE}", "site_build": None}
    try:
        content = _serialize_corpus(_build_corpus())
        new_blob_sha = _gh("POST", f"/repos/{REPO}/git/blobs", token,
                           {"content": content, "encoding": "utf-8"})["sha"]

        head_sha = _current_head(token)
        if _current_corpus_blob_sha(token, head_sha) == new_blob_sha:
            return {"pushed": False, "corpus_sha": head_sha, "reason": "unchanged",
                    "site_build": None}

        tree_sha = _gh("POST", f"/repos/{REPO}/git/trees", token, {
            "tree": [{"path": CORPUS_PATH, "mode": "100644", "type": "blob",
                      "sha": new_blob_sha}],
        })["sha"]
        commit_sha = _gh("POST", f"/repos/{REPO}/git/commits", token, {
            "message": "field notes corpus",
            "tree": tree_sha,
            "parents": [head_sha],
        })["sha"]

        # force=False: a concurrent writer's push is a refused fast-forward,
        # not lost history.
        _gh("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}", token,
            {"sha": commit_sha, "force": False})
    except Exception as e:  # noqa: BLE001 — see the module docstring
        return {"pushed": False, "corpus_sha": None, "reason": _failure_reason(e),
                "site_build": None}

    # Outside the try: the commit has landed, so a dispatch failure must not
    # turn this into a failed push. `dispatch_site_build` never raises.
    return {"pushed": True, "corpus_sha": commit_sha, "reason": None,
            "site_build": dispatch_site_build()}


def seed() -> dict:
    """Push the current store with no publish involved, starting the site
    build as a publish would when it makes a commit. The orchestrator runs
    this once after this change merges, to put the corpus on `main` before the
    first publish; this module does not run it."""
    return push_corpus()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Push the Field Notes corpus to convivy/swabbys-journal")
    ap.add_argument("--seed", action="store_true",
                    help="push the current store once, with no publish")
    args = ap.parse_args()
    if not args.seed:
        ap.error("nothing to do without --seed")
    result = seed()
    print(result)
    if result.get("corpus_sha") is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
