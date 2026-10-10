#!/usr/bin/env python3.11
"""The one http(s)-URL allowlist check, shared by every renderer on the site
that shows a URL it did not itself produce — an issue's or decision's `refs`,
a signal's `refs`/`meta`, a GitHub API response — whether or not the source is
trusted, so a call site does not need to judge that before choosing a check.

`value.startswith(("http://", "https://"))` looks like a scheme check but is
not one: it does not require an authority, and `html.escape` does not encode
control characters, so `"http://\\njavascript:alert(1)"` passes it and lands in
the rendered `href` verbatim. That bypass was found and closed once already
(`overnight_build.safe_url`, PR #141) but the weak `startswith` form kept
getting rewritten at each new call site — this module is the one definition so
that fix lives once.

**Do not write a count into this docstring.** It has now claimed completeness
twice and been wrong twice: `issue_01KZVP35QWBM48W7ZNH5W2SNWT` was filed because
the first claim overstated the module's reach, and the sentence that replaced it
said "the last three call sites" while `changelog_build._pr_link` was a fourth,
found in review of PR #170. A number here is a claim the next reader trusts
instead of checking. Grep for `startswith(("http` before believing any of it.
"""
from __future__ import annotations

from urllib.parse import urlparse


def safe_http_url(value) -> str | None:
    """`value` unchanged if it is a clean http(s) URL, else None.

    The check is `urlparse` scheme + a non-empty netloc, plus a flat rejection
    of control characters: `startswith` alone accepted `"http://\\njavascript:
    alert(1)"`, which `html.escape` does not neutralize (LF is not encoded).
    C1 controls (0x80-0x9F) and U+2028/U+2029 are not rejected: urlparse reads
    them as netloc/path characters and browsers percent-encode them, so they
    cannot reassign an already-parsed scheme.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        return None
    try:
        p = urlparse(v)
    except ValueError:
        # urlparse raises on an unbalanced IPv6-literal bracket
        # ("http://[::1", "http://a[b]c") instead of just failing the
        # scheme/netloc check below. refs is agent-written, so a malformed
        # bracket must reject like any other bad URL, not crash the caller.
        return None
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return v
