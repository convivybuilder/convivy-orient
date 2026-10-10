"""GitHub capture transform + tally tests (S1, ARCH-349).

The transforms are pure (GitHub JSON → signal payload) so they test without
network; capture_repo tests with an injected poster + patched gh_json.
"""
from __future__ import annotations

import json
import os

import pytest

import github_capture as gc


class _FakeRun:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_gh_json_preserves_bracket_bodies(monkeypatch):
    # Round-1 finding: a body containing a literal "][" (markdown reference links,
    # arr[i][j]) must survive parsing uncorrupted — parse first, never blind-replace.
    payload = json.dumps([{"body": "see [docs][ref] and arr[i][j]"}])
    monkeypatch.setattr(gc.subprocess, "run", lambda *a, **k: _FakeRun(payload))
    rows = gc.gh_json("/x")
    assert rows[0]["body"] == "see [docs][ref] and arr[i][j]"


def test_gh_json_normalizes_legacy_pagination_seams(monkeypatch):
    # older gh concatenated one array per page; the fallback still handles it
    monkeypatch.setattr(gc.subprocess, "run",
                        lambda *a, **k: _FakeRun('[{"a": 1}]\n[{"a": 2}]'))
    assert [r["a"] for r in gc.gh_json("/x")] == [1, 2]


def test_gh_json_raises_on_fetch_failure(monkeypatch):
    # PR #52 round-1 blocker: a failed fetch must RAISE, never read as an empty
    # page — an empty read would let main() advance the cursor past a window
    # that was never actually fetched (silent permanent data loss).
    monkeypatch.setattr(gc.subprocess, "run",
                        lambda *a, **k: _FakeRun("", returncode=1, stderr="HTTP 502"))
    with pytest.raises(gc.GhFetchError):
        gc.gh_json("/x")
    monkeypatch.setattr(gc.subprocess, "run",
                        lambda *a, **k: _FakeRun("not json at all"))
    with pytest.raises(gc.GhFetchError):
        gc.gh_json("/x")


def test_capture_repo_counts_fetch_failure_as_error(monkeypatch):
    # The repo-level consequence: a GhFetchError mid-repo lands in the tally as
    # an error, which is exactly what withholds the cursor advance in main().
    def fake_gh(path):
        raise gc.GhFetchError(f"gh api {path}: HTTP 502")

    monkeypatch.setattr(gc, "gh_json", fake_gh)
    t = gc.capture_repo("vivy", since=None, token="tok", poster=lambda p, tok: "created")
    assert t["errors"] == 1 and t["created"] == 0


def test_review_verdict_transform_keeps_verdicts_and_skips_shells():
    approved_empty = {"id": 1, "state": "APPROVED", "body": "", "html_url": "u",
                      "user": {"login": "reviewer"}, "submitted_at": "2026-07-01T00:00:00Z"}
    commented_empty = {"id": 2, "state": "COMMENTED", "body": "", "user": {"login": "x"}}
    changes = {"id": 3, "state": "CHANGES_REQUESTED", "body": "fix the cap",
               "user": {"login": "security-reviewer"}, "submitted_at": "2026-07-02T00:00:00Z"}
    a = gc.signal_from_review("vivy", 921, approved_empty)
    assert a and a["kind"] == "review_verdict" and a["meta"]["state"] == "APPROVED"
    assert a["dedup_key"] == "gh:vivy:pr921:review:1" and a["product"] == "vivy"
    assert gc.signal_from_review("vivy", 921, commented_empty) is None  # shell event
    c = gc.signal_from_review("vivy", 921, changes)
    assert c and c["body"] == "fix the cap" and c["actor"] == "security-reviewer"


def test_review_comment_transform():
    rc = {"id": 9, "body": "unused import here", "path": "tests/test_x.py",
          "pull_request_url": "https://api.github.com/repos/convivy/vivy/pulls/921",
          "html_url": "https://github.com/convivy/vivy/pull/921#discussion_r9",
          "user": {"login": "reviewer"}, "created_at": "2026-07-01T00:00:00Z"}
    s = gc.signal_from_review_comment("vivy", rc)
    assert s["stream"] == "vivy#921" and s["refs"]["path"] == "tests/test_x.py"
    assert s["dedup_key"] == "gh:vivy:pr921:rc:9"
    rc_empty = dict(rc, body="  ")
    assert gc.signal_from_review_comment("vivy", rc_empty) is None


def test_issue_comment_transform_distinguishes_pr_from_issue():
    base = {"id": 5, "body": "ship it",
            "issue_url": "https://api.github.com/repos/convivy/vivy/issues/40",
            "user": {"login": "operator-gh"}, "created_at": "2026-07-01T00:00:00Z"}
    pr = gc.signal_from_issue_comment("vivy", dict(base, html_url="https://github.com/convivy/vivy/pull/40#issuecomment-5"))
    assert pr["kind"] == "pr_comment" and pr["refs"]["pr"] == 40
    iss = gc.signal_from_issue_comment("vivy", dict(base, html_url="https://github.com/convivy/vivy/issues/40#issuecomment-5"))
    assert iss["kind"] == "issue_comment" and iss["refs"]["issue"] == 40


def test_merge_transform_returns_signal_for_merged_pr():
    pr = {"number": 42, "title": "feat: add the thing", "body": "Does the thing.\n\nMore detail.",
          "user": {"login": "operator-gh"}, "html_url": "https://github.com/convivy/vivy/pull/42",
          "merged_at": "2026-07-15T12:00:00Z"}
    s = gc.signal_from_merge("vivy", pr)
    assert s["source"] == "github" and s["kind"] == "changelog"
    assert s["stream"] == "vivy" and s["product"] == "vivy"
    assert s["actor"] == "operator-gh"
    assert s["body"] == "feat: add the thing\n\nDoes the thing.\n\nMore detail."
    assert s["produced_at"] == "2026-07-15T12:00:00Z"
    assert s["dedup_key"] == "gh:vivy:pr42:merged"
    assert s["refs"] == {"pr": 42, "url": "https://github.com/convivy/vivy/pull/42",
                         "merged_at": "2026-07-15T12:00:00Z", "author": "operator-gh"}


def test_merge_transform_no_description_uses_title_only():
    pr = {"number": 7, "title": "fix: a bug", "body": None, "user": {"login": "u"},
          "html_url": "h", "merged_at": "2026-07-01T00:00:00Z"}
    s = gc.signal_from_merge("vivy", pr)
    assert s["body"] == "fix: a bug"


def test_merge_transform_returns_none_for_unmerged_pr():
    open_pr = {"number": 5, "title": "wip", "body": "", "user": {"login": "u"},
               "html_url": "h", "merged_at": None}
    assert gc.signal_from_merge("vivy", open_pr) is None
    closed_unmerged = {"number": 6, "title": "abandoned", "user": {"login": "u"}}
    assert gc.signal_from_merge("vivy", closed_unmerged) is None


def test_merge_transform_clips_long_body():
    pr = {"number": 8, "title": "t", "body": "x" * 5000, "user": {"login": "u"},
          "html_url": "h", "merged_at": "2026-07-01T00:00:00Z"}
    s = gc.signal_from_merge("vivy", pr)
    assert len(s["body"]) < 4100
    assert s["body"].endswith("…")


def test_capture_repo_emits_merge_signal_for_merged_prs(monkeypatch):
    def fake_gh(path):
        if "/pulls/comments" in path or "/issues/comments" in path:
            return []
        if path.endswith("/reviews?per_page=100"):
            return []
        if "/pulls?state=all" in path:
            return [{"number": 9, "title": "feat: ship it", "body": "desc",
                     "user": {"login": "u"}, "html_url": "h", "merged_at": "2026-07-15T00:00:00Z",
                     "updated_at": "2026-07-15T00:00:00Z"}]
        return []

    monkeypatch.setattr(gc, "gh_json", fake_gh)
    posted = []
    t = gc.capture_repo("vivy", since=None, token="tok",
                        poster=lambda p, tok: posted.append(p) or "created")
    assert t["created"] == 1 and t["errors"] == 0
    assert posted[0]["kind"] == "changelog" and posted[0]["dedup_key"] == "gh:vivy:pr9:merged"


def test_capture_repo_tallies_and_respects_since(monkeypatch):
    calls = []

    def fake_gh(path):
        calls.append(path)
        if "/pulls/comments" in path:
            return [{"id": 1, "body": "b", "pull_request_url": ".../pulls/7",
                     "html_url": "h", "user": {"login": "u"}, "created_at": "t"}]
        if "/issues/comments" in path:
            return []
        if path.endswith("/reviews?per_page=100"):
            return [{"id": 2, "state": "APPROVED", "body": "", "user": {"login": "u"}}]
        if "/pulls?state=all" in path:
            return [{"number": 7, "updated_at": "2026-07-15T00:00:00Z"},
                    {"number": 6, "updated_at": "2026-07-01T00:00:00Z"}]  # older than cursor
        return []

    monkeypatch.setattr(gc, "gh_json", fake_gh)
    posted = []

    def fake_poster(payload, token):
        posted.append(payload)
        return "created"

    t = gc.capture_repo("vivy", since="2026-07-10T00:00:00Z", token="tok", poster=fake_poster)
    assert t["created"] == 2 and t["errors"] == 0  # 1 review comment + 1 verdict
    # the since cursor stopped the PR walk before #6 — only #7's reviews were fetched
    review_calls = [c for c in calls if c.endswith("/reviews?per_page=100")]
    assert len(review_calls) == 1 and "/pulls/7/" in review_calls[0]
    # and the repo-wide endpoints carried the since filter
    assert any("since=2026-07-10" in c for c in calls if "/pulls/comments" in c)


# --- cursor persistence + owner-only modes (issue_01M2MFGVAW1C8ZQ7G9CT0E1YXZ) --


def test_cursor_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "CURSOR_FILE", tmp_path / "cursor.json")
    assert gc.load_cursor() == {}
    gc.save_cursor({"vivy": "2026-07-10T00:00:00Z"})
    assert gc.load_cursor() == {"vivy": "2026-07-10T00:00:00Z"}


def test_cursor_dir_and_file_are_owner_only_under_a_permissive_umask(tmp_path, monkeypatch):
    """The per-repo cursor is credential-adjacent state in ~/.orient-control and
    must not inherit a permissive umask."""
    monkeypatch.setattr(gc, "CURSOR_FILE", tmp_path / "sub" / "cursor.json")
    old_umask = os.umask(0o022)
    try:
        gc.save_cursor({"vivy": "2026-07-10T00:00:00Z"})
    finally:
        os.umask(old_umask)
    assert oct(gc.CURSOR_FILE.parent.stat().st_mode & 0o777) == oct(0o700)
    assert oct(gc.CURSOR_FILE.stat().st_mode & 0o777) == oct(0o600)
