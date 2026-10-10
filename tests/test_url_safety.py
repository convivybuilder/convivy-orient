"""`safe_http_url` contract — the one allowlist shared by every renderer that
shows an agent-written URL. Mirrors the cases `overnight_build.safe_url`
pinned before it was rewritten to call this."""
from __future__ import annotations

import ast
import pathlib

import pytest

from url_safety import safe_http_url


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "  javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "//evil.test/x",
    "",
    # Control characters after a valid scheme reject: strip() trims the ends
    # only and html.escape does not encode them, so an accept would land the
    # raw byte in the rendered href.
    "http://\njavascript:alert(1)",
    "http://\rjavascript:alert(1)",
    "http://\tjavascript:alert(1)",
    "http://\x00javascript:alert(1)",
    "\x00http://example.com",
    # Scheme must carry a real authority: startswith passed these through.
    "http://",
    "https:///path",
    None,
    123,
    # Unbalanced IPv6-literal brackets: urlparse raises ValueError on these
    # instead of just failing the scheme/netloc check, so an untrusted value
    # (refs is agent-written) took down the caller instead of being rejected.
    "http://[::1",
    "http://a[b]c",
    "https://x[",
])
def test_safe_http_url_rejects_anything_not_http(bad):
    assert safe_http_url(bad) is None


@pytest.mark.parametrize("good", ["https://github.com/convivy/vivy/pull/1",
                                  "http://example.test/x"])
def test_safe_http_url_allows_http_and_https(good):
    assert safe_http_url(good) == good


# Directories with no renderers in them, plus `tests` — and that exclusion is a REAL
# limit rather than a tidy-up: test files legitimately quote the weak form in
# docstrings explaining what it is, so including them fails the sweep on prose, and a
# weak check inside a test helper would go unseen here. Stated because the reason this
# sweep exists is that a completeness claim got believed twice.
_SWEEP_SKIP = {".venv", "venv", "node_modules", ".git", "__pycache__", "site",
               ".mypy_cache", ".ruff_cache", ".pytest_cache", "tests", "alembic"}

# Exempt by FILE AND ENCLOSING FUNCTION, never by file alone. `library_client` gates
# its own base URL on https at import time — a config value against a transport
# requirement, not a rendered URL — but exempting the whole file would let a future
# render-facing check added there escape too (reviewer, PR #170 round 3).
# Keyed by file and the LINE RANGE of the exempted function, resolved from the AST at
# check time — not by the function's NAME. A name-string key collides when two classes
# in one file each define a method of the same name, which is the weakness the taint
# set was redesigned to shed; carrying a second instance of it in the exemption would
# be the same defect wearing a smaller hat (reviewer, PR #170 round 6).
# `vault_plane._refuse_cleartext` is the same check on the WRITE path, added when
# a reviewer found that the two modules read one `LIBRARY_BASE_URL` and both
# attach a bearer token while only the read path was guarded (PR #175). Same
# reasoning, same exemption shape, and exempted by function rather than by file
# for the same reason.
# `_NoDowngrade.redirect_request` is the same question asked of a redirect
# TARGET rather than of a configured address: `_refuse_cleartext` checks the
# address once at import, and following a 30x to plain http is how that check
# gets bypassed at run time, resending the bearer token in the clear
# (security-reviewer, convivy/agent-platform#400). It renders nothing.
_SWEEP_EXEMPT = {("library_client.py", "_resolve_base_url"),
                 ("vault_plane.py", "_refuse_cleartext"),
                 ("vault_plane.py", "_NoDowngrade.redirect_request")}


def _exempt_ranges(tree, filename: str) -> list[tuple[int, int]]:
    """Line ranges of the functions exempted in this file, by qualified path.

    A qualified path (`Class.method`, `outer.inner`) rather than a bare name, so two
    same-named methods in different classes are distinguishable, and the range is what
    the caller matches against — a hit's line either falls inside an exempted
    function's span or it does not."""
    out = []
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                        (filename, qual) in _SWEEP_EXEMPT:
                    out.append((child.lineno, child.end_lineno or child.lineno))
                walk(child, qual)
    walk(tree, "")
    return out


def _string_literals(node) -> list[str]:
    """The str constants directly inside a node — a bare string, or a tuple/list/set
    of them. Anything else yields nothing rather than guessing."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _weak_scheme_checks(tree, source: str):
    """Every `x.startswith(<http-prefixed literal>)` in an AST, with its line.

    **AST, not grep, and that is the whole point.** A text sweep for
    `startswith(("http` is defeated by ordinary edits nobody intends as evasion:
    wrapping a long call across two lines, or pulling the prefix tuple out into a
    named constant. `url_safety`'s own docstring says the weak form "kept getting
    rewritten at each new call site" through exactly that carelessness, so a guard
    surviving only one formatting style guarantees very little (reviewer, PR #170
    round 3).

    Resolving a module-level constant back to its literals is what catches the
    extracted-tuple shape; without it, `_P = ("http://",)` then `url.startswith(_P)`
    reads as clean."""
    # NO SCOPE MODEL. Every name in the file that is EVER bound to an http-prefixed
    # literal goes in one set, and a `startswith(<name>)` against any of them is a hit.
    #
    # This is deliberately conservative, and it is the third design here. Keying by
    # bare name collided across functions; keying by `(enclosing def name, name)`
    # collided again through class bodies, two same-named methods, and closures — each
    # found by planting, each an ordinary shape rather than evasion. Hand-rolling scope
    # analysis kept producing something almost right, and "almost right" in a guard
    # means a real weak check silently unflagged, which is the failure this sweep
    # exists to prevent.
    #
    # So the trade is made explicitly: this can OVER-flag (a same-named safe constant
    # elsewhere in the file drags an innocent call site in) and cannot UNDER-flag on a
    # name collision. A false positive costs one `_SWEEP_EXEMPT` entry with a reason; a
    # false negative costs the bypass this module was written to close. Correct scope
    # resolution needs real binding analysis, which is not worth building in a test when
    # the conservative direction is free.
    # EVERY binding form, not just `x = ...`. An annotated constant
    # (`_P: tuple[str, ...] = ("http://",)`) is an `ast.AnnAssign`, not an
    # `ast.Assign`, and this codebase already writes module constants that way
    # (`design_system.py`, `merge_queue_build.py:45`) — so the first version was blind
    # to an idiom already in the tree. Chained assignment (`a = b = "http://"`) has
    # more than one target and was likewise missed (reviewer, PR #170 round 6).
    tainted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(v.startswith(("http://", "https://")) for v in _string_literals(value)):
            tainted.update(t.id for t in targets if isinstance(t, ast.Name))

    lines = source.splitlines()
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith" and node.args):
            continue
        arg = node.args[0]
        weak = (arg.id in tainted if isinstance(arg, ast.Name)
                else any(v.startswith(("http://", "https://"))
                         for v in _string_literals(arg)))
        if weak:
            hits.append((node.lineno, lines[node.lineno - 1].strip()))
    return hits


def test_no_renderer_carries_the_weak_check_anywhere_in_the_tree():
    """The invariant `url_safety.py` exists for, enforced instead of documented.

    That module's docstring has claimed completeness twice and been wrong twice —
    once by omission, then again in the very diff written to fix that, which said
    "the last three call sites" while `changelog_build._pr_link` was a fourth. Both
    times a careful reader caught it, which is not a control.

    **What this still does not catch, stated rather than hidden.** A prefix built at
    runtime (`"ht" + "tp://"`) or read from config. A weak check written inside
    `tests/`. A prefix passed in as an argument, returned by a call, or built across
    several assignments — a name is tainted only by a direct binding of literals to it,
    one hop, anywhere in the file. Plain, annotated and chained assignment all count as
    a binding; a walrus, a `for` target and an unpacking do not.

    **And it deliberately OVER-flags rather than risk missing.** Name matching ignores
    scope entirely, so a safe call site can be flagged because an unrelated binding of
    the same name elsewhere in the file holds an http literal. That is the chosen
    direction: a false positive costs one exemption with a reason, a false negative
    costs the bypass. Three earlier designs tried to be precise about scope and each
    left a collision a reviewer had to find.

    **It also cannot tell a renderer's check from an unrelated one.** Any
    `x.startswith(<http-literal>)` fails here, including a legitimate classifier that
    never renders a URL. None exists in the tree today, but the failure message would
    claim it "renders a URL through the bypassed scheme check", which would be false —
    and the path of least resistance under CI pressure is to delete a guard that cries
    wolf. If that day comes, exempt the call site in `_SWEEP_EXEMPT` with a reason;
    do not widen the check.

    It guards against carelessness, which is how this bypass actually recurred twice.
    It is not a proof of absence and must not be described as one."""
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if path.name == "url_safety.py" or _SWEEP_SKIP & set(rel.parts):
            continue
        source = path.read_text(errors="ignore")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for lineno, text in _weak_scheme_checks(tree, source):
            if any(lo <= lineno <= hi for lo, hi in _exempt_ranges(tree, path.name)):
                continue
            offenders.append(f"{rel}:{lineno}: {text}")
    assert not offenders, (
        "these render a URL through the bypassed scheme check instead of "
        "url_safety.safe_http_url:\n  " + "\n  ".join(offenders))
