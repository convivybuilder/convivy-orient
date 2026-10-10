#!/usr/bin/env python3.11
"""GitHub review capture for the signal-memory plane (S1, ARCH-349).

Captures the engineering signal that today rots in GitHub — review verdicts,
review comments, and PR/issue comments across the convivy org — into the signal
store, through the API like every other writer (never by opening the DB).

Two modes:
  --backfill    every PR/comment in the repo list (the "copy all the old ones" pass)
  (default)     incremental: only what changed since the per-repo cursor
                (~/.orient-control/github-capture.cursor.json), with a 10-minute
                overlap; dedup_key makes the overlap harmless.

Efficiency: review comments and issue comments come from the REPO-WIDE endpoints
(2 paginated calls per repo, `since`-filtered) — not 3 calls per PR. Review
VERDICTS have no repo-wide endpoint, so those iterate PRs (updated-since-cursor
only, on the incremental path).

Every row is idempotent via dedup_key gh:<repo>:pr<n>:<type>:<id>, so a crash and
rerun, or an overlapping window, never duplicates. Auth: a bearer token from
$ORIENT_CAPTURE_TOKEN or ~/.orient-control/capture-token (0600, never committed),
mapped in api-tokens.json to a capture identity with products ["*"]. GitHub auth
rides the mini's `gh` login.

Transforms are pure functions (GitHub JSON → signal payload) so they unit-test
without network; fetching and posting are thin I/O wrappers around `gh api` and
urllib to loopback:8099.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import vault_plane
from owner_only_paths import ensure_owner_only_dir, ensure_owner_only_file

ORG = "convivy"
CURSOR_FILE = Path.home() / ".orient-control" / "github-capture.cursor.json"
TOKEN_FILE = Path.home() / ".orient-control" / "capture-token"
OVERLAP = dt.timedelta(minutes=10)

# Repos that are pure sandboxes/mirrors — no engineering signal worth indexing.
DEFAULT_EXCLUDE = {"vivy-ownerrez-sandbox-tools", "constance-sevenshifts-sandbox-tools"}


# --- pure transforms (unit-tested) ---------------------------------------------


def _pr_number_from_url(url: str) -> int | None:
    """…/pulls/123 or …/issues/123 → 123."""
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def signal_from_review(repo: str, pr_number: int, review: dict) -> dict | None:
    """A review VERDICT event. Empty-body COMMENTED reviews are shells wrapping
    inline comments (captured separately) — skip those; keep APPROVED /
    CHANGES_REQUESTED even with an empty body, because the verdict IS the signal."""
    state = (review.get("state") or "").upper()
    body = (review.get("body") or "").strip()
    if state == "COMMENTED" and not body:
        return None
    if not state:
        return None
    return {
        "source": "github_review",
        "kind": "review_verdict",
        "product": repo,
        "stream": f"{repo}#{pr_number}",
        "actor": (review.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": {"pr": pr_number, "url": review.get("html_url") or ""},
        "meta": {"state": state},
        "produced_at": review.get("submitted_at") or "",
        "dedup_key": f"gh:{repo}:pr{pr_number}:review:{review.get('id')}",
    }


_CHANGELOG_BODY_CLIP = 4000


def signal_from_merge(repo: str, pr: dict) -> dict | None:
    """A merged-PR event (ARCH-359) — the in-box changelog's raw material. The
    merge IS the record: title + description become the changelog entry's
    summary, produced_at is when the merge happened (not when we captured it).
    Unmerged PRs (open, closed-without-merge) return None — the changelog only
    ever shows what actually shipped."""
    merged_at = pr.get("merged_at")
    if not merged_at:
        return None
    number = pr.get("number")
    author = (pr.get("user") or {}).get("login") or "unknown"
    title = (pr.get("title") or "").strip()
    description = (pr.get("body") or "").strip()
    body = f"{title}\n\n{description}" if description else title
    if len(body) > _CHANGELOG_BODY_CLIP:
        body = body[:_CHANGELOG_BODY_CLIP].rstrip() + " …"
    return {
        "source": "github",
        "kind": "changelog",
        "stream": repo,
        "actor": author,
        "product": repo,
        "body": body,
        "refs": {"pr": number, "url": pr.get("html_url") or "",
                 "merged_at": merged_at, "author": author},
        "meta": {},
        "produced_at": merged_at,
        "dedup_key": f"gh:{repo}:pr{number}:merged",
    }


def signal_from_review_comment(repo: str, rc: dict) -> dict | None:
    """An inline review comment (repo-wide /pulls/comments endpoint)."""
    pr_number = _pr_number_from_url(rc.get("pull_request_url") or "")
    body = (rc.get("body") or "").strip()
    if pr_number is None or not body:
        return None
    refs = {"pr": pr_number, "url": rc.get("html_url") or ""}
    if rc.get("path"):
        refs["path"] = rc["path"]
    return {
        "source": "github_review",
        "kind": "review_comment",
        "product": repo,
        "stream": f"{repo}#{pr_number}",
        "actor": (rc.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": refs,
        "meta": {},
        "produced_at": rc.get("created_at") or "",
        "dedup_key": f"gh:{repo}:pr{pr_number}:rc:{rc.get('id')}",
    }


def signal_from_issue_comment(repo: str, ic: dict) -> dict | None:
    """A conversation comment (repo-wide /issues/comments endpoint — covers both
    PRs and issues; the html_url distinguishes them)."""
    number = _pr_number_from_url(ic.get("issue_url") or "")
    body = (ic.get("body") or "").strip()
    if number is None or not body:
        return None
    is_pr = "/pull/" in (ic.get("html_url") or "")
    return {
        "source": "github_review" if is_pr else "github_issue",
        "kind": "pr_comment" if is_pr else "issue_comment",
        "product": repo,
        "stream": f"{repo}#{number}",
        "actor": (ic.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": {"pr" if is_pr else "issue": number, "url": ic.get("html_url") or ""},
        "meta": {},
        "produced_at": ic.get("created_at") or "",
        "dedup_key": f"gh:{repo}:pr{number}:ic:{ic.get('id')}",
    }


# --- I/O: gh api fetches, signal POSTs, the cursor ------------------------------


class GhFetchError(RuntimeError):
    """A gh api fetch failed. Raised (never swallowed as []) so a failed READ is
    always counted as a repo error — otherwise the cursor would advance past a
    window that was never actually read, silently losing everything in it beyond
    the 10-minute overlap."""


def gh_json(path: str) -> list:
    """`gh api --paginate` a list endpoint. Raises GhFetchError on any fetch or
    parse failure; capture_repo counts it and the repo's cursor stays put."""
    try:
        out = subprocess.run(["gh", "api", "--paginate", path],
                             capture_output=True, text=True, timeout=600)
    except Exception as ex:
        raise GhFetchError(f"gh api {path}: {type(ex).__name__}") from ex
    if out.returncode != 0:
        raise GhFetchError(f"gh api {path}: {out.stderr.strip()[:200]}")
    text = out.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Older gh emitted one JSON array PER PAGE, concatenated. Normalize the
        # seams ONLY when plain parsing fails — a blind replace would corrupt
        # any captured body containing a literal "][" (markdown reference
        # links, arr[i][j]); modern gh returns one merged array that parses
        # directly.
        try:
            data = json.loads(text.replace("]\n[", ",").replace("][", ","))
        except json.JSONDecodeError as ex:
            raise GhFetchError(f"gh api {path}: unparseable output") from ex
    return data if isinstance(data, list) else [data]


def org_repos() -> list[str]:
    return [r["name"] for r in gh_json(f"/orgs/{ORG}/repos")
            if not r.get("archived") and r["name"] not in DEFAULT_EXCLUDE]


def capture_token() -> str:
    tok = os.environ.get("ORIENT_CAPTURE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        sys.exit(f"no capture token: set ORIENT_CAPTURE_TOKEN or create {TOKEN_FILE}")


def post_signal(payload: dict, token: str) -> str:
    """POST one signal. Returns 'created' | 'duplicate' | 'error:<code>'."""
    req = urllib.request.Request(
        vault_plane.url("/api/signals"),
        data=json.dumps(payload).encode("utf-8"),
        headers=vault_plane.headers(token=token, json_body=True),
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return "created" if resp.status == 201 else "duplicate"
    except urllib.error.HTTPError as e:
        return f"error:{e.code}"
    except Exception as ex:
        return f"error:{type(ex).__name__}"


def load_cursor() -> dict:
    try:
        return json.loads(CURSOR_FILE.read_text())
    except Exception:
        return {}


def save_cursor(cur: dict) -> None:
    ensure_owner_only_dir(CURSOR_FILE.parent)
    ensure_owner_only_file(CURSOR_FILE)
    CURSOR_FILE.write_text(json.dumps(cur, indent=1))


# --- the capture run -------------------------------------------------------------


def capture_repo(repo: str, since: str | None, token: str, poster=post_signal) -> dict:
    """Capture one repo. `since` None = full backfill. Returns a tally dict.

    A fetch failure (GhFetchError) aborts the rest of THIS repo and counts as an
    error, so main() withholds the repo's cursor and the next run re-covers the
    whole window (dedup absorbs the re-capture). Partial capture before the
    failure is kept — those POSTs already landed."""
    tally = {"created": 0, "duplicate": 0, "skipped": 0, "errors": 0}

    def emit(payload):
        if payload is None:
            tally["skipped"] += 1
            return
        r = poster(payload, token)
        tally["created" if r == "created" else "duplicate" if r == "duplicate" else "errors"] += 1

    try:
        since_q = f"&since={since}" if since else ""
        for rc in gh_json(f"/repos/{ORG}/{repo}/pulls/comments?sort=updated&direction=asc&per_page=100{since_q}"):
            emit(signal_from_review_comment(repo, rc))
        for ic in gh_json(f"/repos/{ORG}/{repo}/issues/comments?sort=updated&direction=asc&per_page=100{since_q}"):
            emit(signal_from_issue_comment(repo, ic))

        # Review verdicts: no repo-wide endpoint, so walk PRs — all of them on a
        # backfill, only updated-since-cursor ones incrementally.
        prs = gh_json(f"/repos/{ORG}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100")
        for pr in prs:
            if since and (pr.get("updated_at") or "") < since:
                break  # sorted by updated desc — everything past here predates the cursor
            n = pr.get("number")
            emit(signal_from_merge(repo, pr))
            for review in gh_json(f"/repos/{ORG}/{repo}/pulls/{n}/reviews?per_page=100"):
                emit(signal_from_review(repo, n, review))
    except GhFetchError as ex:
        print(f"  {ex}", file=sys.stderr)
        tally["errors"] += 1
    return tally


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture GitHub review signal into the ops plane")
    ap.add_argument("--backfill", action="store_true", help="capture everything, ignore the cursor")
    ap.add_argument("--repos", help="comma-separated repo names (default: all non-archived org repos)")
    args = ap.parse_args()

    token = capture_token()
    try:
        repos = [r.strip() for r in args.repos.split(",")] if args.repos else org_repos()
    except GhFetchError as ex:
        sys.exit(f"cannot enumerate org repos: {ex}")
    cursor = {} if args.backfill else load_cursor()
    run_started = dt.datetime.now(dt.timezone.utc)

    grand = {"created": 0, "duplicate": 0, "skipped": 0, "errors": 0}
    for repo in repos:
        since = None if args.backfill else cursor.get(repo)
        t = capture_repo(repo, since, token)
        print(f"{repo}: {t}")
        for k in grand:
            grand[k] += t[k]
        if t["errors"] == 0:
            # next incremental picks up from just before this run (overlap; dedup
            # absorbs the double-capture). A repo with errors keeps its old cursor
            # so nothing is skipped past.
            cursor[repo] = (run_started - OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_cursor(cursor)  # the backfill also seeds the cursor, so the poller starts warm
    print(f"total: {grand}")
    if grand["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
