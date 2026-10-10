"""Which service Orient's own scheduled jobs write to, and the sweep that keeps
the answer in one place.

The four capture and sweep jobs in this repo are HTTP clients of the vault
surface exactly as the fleet's agents are, and they move with the fleet at the
write cutover. Their failure mode is the quietest one in the whole cutover: a
capture job still posting to Orient gets a `200`, writes to a copy nobody reads,
and the Archives simply stop growing with nothing erroring anywhere.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import vault_plane

REPO = Path(__file__).resolve().parent.parent
LIBRARY = "http://127.0.0.1:8100"


@pytest.fixture
def on_library(monkeypatch):
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", True)


@pytest.fixture
def on_orient(monkeypatch):
    """The deliberate opt-out, `VAULTS_ON_LIBRARY=0`. It reaches the superseded
    copy on purpose, which only something inspecting that copy before Stage C
    drops it should ever want."""
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", False)


def test_a_present_but_empty_value_raises_rather_than_reading_as_unset(monkeypatch):
    """**Absent and empty are different states.** A templated deploy emitting
    `VAULTS_ON_LIBRARY=` instead of omitting the key has produced a value it did
    not mean to, and reading that as unset accepts a broken template whose
    destination only happens to be safe today. Matches agent-platform#406, whose
    security review found the same input silently reverting a job to the
    superseded store on the other copy of this module."""
    for raw in ("", "   ", "\n", "\t"):
        monkeypatch.setenv("VAULTS_ON_LIBRARY", raw)
        with pytest.raises(RuntimeError):
            vault_plane._resolve_on_library()
    monkeypatch.delenv("VAULTS_ON_LIBRARY", raising=False)
    assert vault_plane._resolve_on_library() is True, "absent is still the Library"


def test_unset_means_the_library(monkeypatch):
    """**The property the cutover cost us and this restores: doing nothing is
    safe.** The variable defaulted to Orient so the module could land before the
    window changing nothing, and it was then set on 8 of 83 launchd jobs, in no
    login shell and in no git hook — so 75 jobs wrote into the superseded copy and
    collected a `200` for twenty hours. A caller that was never told about the
    cutover now reaches the store that is actually authoritative."""
    monkeypatch.delenv("VAULTS_ON_LIBRARY", raising=False)
    assert vault_plane._resolve_on_library() is True


def test_the_opt_out_is_the_explicit_zero(monkeypatch):
    """`0` is the only value that reaches Orient, so a stale `1` in a plist that
    was repointed during the window keeps working and means what it says.
    Whitespace is stripped, because a plist edit and a shell export both pick it
    up and neither operator meant to change the plane."""
    for raw, expected in (("0", False), ("1", True),
                          (" 0 ", False), ("1\n", True), ("\t0", False)):
        monkeypatch.setenv("VAULTS_ON_LIBRARY", raw)
        assert vault_plane._resolve_on_library() is expected, raw


@pytest.mark.parametrize("raw", ["false", "no", "off", "true", "yes", "on",
                                 "FALSE", "None", "orient", "library", "2", "-1",
                                 "0 0", "o"])
def test_a_value_this_module_cannot_read_raises(monkeypatch, raw):
    """**The whole point of the design, and the half neither obvious alternative
    covers.** Treating everything except `0` as the Library sends an operator who
    typed `false` to the store they meant to leave. Accepting a synonym set is
    always one word short of what somebody types, so it narrows that path while
    teaching a generalisation it does not honour — and `orient` and `library`
    above are the two words an operator is most likely to reach for from this
    module's own vocabulary, neither of which any plausible synonym set contains.

    Raising covers both directions: `true` cannot quietly mean Orient and `false`
    cannot quietly mean the Library."""
    monkeypatch.setenv("VAULTS_ON_LIBRARY", raw)
    with pytest.raises(RuntimeError) as exc:
        vault_plane._resolve_on_library()
    assert raw in str(exc.value), "the refusal has to name the value it refused"
    assert "'0'" in str(exc.value), "and the value that would have worked"


def test_the_opt_out_still_reaches_orient_unchanged(on_orient):
    assert not vault_plane.on_library()
    assert vault_plane.url("/api/issues?state=open") == (
        f"{vault_plane.ORIENT_BASE_URL}/api/issues?state=open")
    assert vault_plane.headers(token="tok") == {"Authorization": "Bearer tok"}


def test_the_address_alone_does_not_move_the_writes(monkeypatch):
    """**The collision this repo would otherwise have had.** `library_client`
    reads `LIBRARY_BASE_URL` and has since the read rewire, so if that name were
    also the cutover switch, pointing Orient's reads at another Library port
    would silently move every capture job's writes days early."""
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", False)
    assert vault_plane.base_url() == vault_plane.ORIENT_BASE_URL


def test_issues_become_items(on_library):
    """The one part of the mapping that is not mechanical. `/resolve` and
    `/prerequisites/verdict` are live routes the Library still serves, kept
    here as general coverage of the prefix rewrite across both a flat and a
    nested action suffix, and `/correct` is the Library's correction route.
    `park`/`unpark` are gone: the Library removed the parked state and both
    routes (library#110, merged 2026-09-11)."""
    assert vault_plane.translate("/api/issues?state=open") == "/items?state=open"
    assert vault_plane.translate("/api/issues/iss_1/resolve") == "/items/iss_1/resolve"
    assert vault_plane.translate("/api/issues/iss_1/prerequisites/verdict") == \
        "/items/iss_1/prerequisites/verdict"
    assert vault_plane.translate("/api/issues/iss_1/correct") == "/items/iss_1/correct"
    assert vault_plane.translate("/api/issues/iss_1") == "/items/iss_1"


def test_signals_just_lose_the_prefix(on_library):
    assert vault_plane.translate("/api/signals") == "/signals"
    assert vault_plane.translate(
        "/api/signals?privacy_class=customer") == "/signals?privacy_class=customer"


def test_the_protocol_header_goes_only_to_the_library(on_library):
    sent = vault_plane.headers(token="tok", json_body=True)
    assert sent[vault_plane.PROTOCOL_HEADER] == vault_plane.PROTOCOL_VERSION
    assert sent["Content-Type"] == "application/json"


# --- the sweep -------------------------------------------------------------------

# **WHAT THIS SWEEP IS, stated because two rounds of review were spent finding
# out.** It catches the NATURAL mistake — a client author writing the obvious
# thing without knowing `vault_plane` exists — and it is not an adversarial
# control. It matches names and call shapes, so an author determined to route
# around it can, through a helper, a dict of endpoints, a `getattr`, or a
# renamed alias whose name says nothing. Full data-flow tracking is a different
# tool and is not worth building for a rule whose violators are honest.
#
# What it does have to catch is every shape a reasonable author would reach for
# unprompted, which is what `_PLANTED` pins: each one was written by a reviewer
# attacking a previous version of this file, not imagined here.

# Every file allowed to name the base URL or the switch. `vault_plane` is the
# module itself; this file feeds deliberate offenders to the detector below.
#
# A two-item literal inside a tracked file, so it cannot grow silently — an
# addition shows up in the diff that adds it.
#
# `library_client.py` is exempt because it OWNS the Library's address — it is
# Orient's read client, pointed there since the read rewire, and the module
# `vault_plane` defers to rather than a job deciding a plane for itself.
_SWEEP_EXEMPT = {
    "vault_plane.py",
    "tests/test_vault_plane.py",
    "library_client.py",
}

# The variables that decide where a client writes. Naming one outside
# `vault_plane` is how a client acquires its own opinion.
_PLANE_VARIABLES = {"ORIENT_BASE_URL", "LIBRARY_BASE_URL", "VAULTS_ON_LIBRARY"}

# The accessors that hand back a bare address. `base_url()` sits next to `url()`
# in the same module and carries no warning, so composing with it is an ordinary
# thing to write and produces the exact bug the module exists to prevent: an
# Orient-shaped path sent to the Library, which 404s (reviewer, round 2).
_PLANE_ACCESSORS = {"base_url"}


def _plane_aliases(tree) -> set[str]:
    """Every name in this file that refers to `vault_plane`.

    **Because `import vault_plane as vp` defeated the whole check** — the first
    version hardcoded the literal module name, so an ordinary alias reproduced
    the sibling repo's shipped bug byte for byte and the sweep said nothing
    (security-reviewer, round 2).
    """
    names = {"vault_plane"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "vault_plane" and alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "vault_plane":
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _env_name_constants(tree) -> set[str]:
    """Names bound to one of the plane variables as a string constant.

    **Because storing the variable's NAME in a constant first evaded the check
    entirely** (security-reviewer, round 3). It is the sibling repo's own idiom —
    agent-platform's `budget.py` and `session_view.py` both read the environment
    that way — and nothing stops a job here being written the same way tomorrow;
    a sweep that only catches the spelling this repo happens to use today is a
    sweep that stops working the first time somebody writes ordinary Python. `_ORIENT_ENV = "ORIENT_BASE_URL"`
    then `os.environ.get(_ORIENT_ENV)` is a client resolving the plane for
    itself, written the way the surrounding code is written.
    """
    named = set()
    for node in ast.walk(tree):
        # `_ENV = "..."` and `_ENV: str = "..."`. The annotated form is a
        # different node type and was missed by the first cut of this
        # (reviewer, round 4) — a module constant carrying a type annotation is
        # ordinary, and the check has to see both spellings of one statement.
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        value = getattr(node, "value", None)
        if not targets or not isinstance(value, ast.Constant):
            continue
        if value.value in _PLANE_VARIABLES:
            named.update(t.id for t in targets if isinstance(t, ast.Name))
    return named


def _reads_a_plane_variable(node, named=frozenset()) -> bool:
    """`os.getenv("ORIENT_BASE_URL")`, `os.environ.get(...)`, `os.environ[...]`,
    and the same three reached through a bare `environ`.

    **Matched on the SHAPE rather than on one spelling.** The first version was a
    line regex for `os.environ.get(`, and a reviewer defeated it in a minute with
    `os.getenv` — an idiom at least as common, which a future client author would
    reach for without knowing this sweep exists (round 1).
    """
    def names_a_plane_variable(expr) -> bool:
        if isinstance(expr, ast.Constant):
            return expr.value in _PLANE_VARIABLES
        return isinstance(expr, ast.Name) and expr.id in named

    if isinstance(node, ast.Call):
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in {"getenv", "get"} and node.args:
            return names_a_plane_variable(node.args[0])
    if isinstance(node, ast.Subscript):
        target = getattr(node.value, "attr", None) or getattr(node.value, "id", None)
        return target == "environ" and names_a_plane_variable(node.slice)
    return False


def _yields_a_base_url(node, aliases: set[str]) -> bool:
    """An expression handing back a bare address: a `*BASE_URL` name, or a call
    to one of the plane's accessors.

    **The name is trusted, not the value.** An alias renamed to something that
    does not end in `BASE_URL` — `endpoint = vault_plane.ORIENT_BASE_URL` — is
    invisible here, and chasing that needs data-flow rather than more AST
    cleverness. Documented rather than silent (reviewer, round 2).
    """
    name = getattr(node, "attr", None) or getattr(node, "id", None)
    if name and name.endswith("BASE_URL"):
        return True
    if isinstance(node, ast.Call):
        called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if called in _PLANE_ACCESSORS:
            owner = getattr(getattr(node.func, "value", None), "id", None)
            return owner in aliases or owner is None
    return False


def _tainted_names(tree, aliases: set[str]) -> set[str]:
    """Names assigned a base address directly — `url = BASE_URL`.

    **Because `url += path` produces no `BinOp` at all**, and that two-line form
    is the house style two clients in this repo already use to extend a URL
    (reviewer, round 2). One assignment deep is as far as this goes: it is a
    taint set, not data-flow analysis, and the sweep's job is the natural
    mistake rather than the determined one.
    """
    tainted = set()
    for node in ast.walk(tree):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        value = getattr(node, "value", None)
        if targets and value is not None and _yields_a_base_url(value, aliases):
            tainted.update(t.id for t in targets if isinstance(t, ast.Name))
    return tainted


def _composes_a_base_url(node, aliases: set[str], tainted=frozenset()) -> bool:
    """A base URL joined to something else. That composition is the moment a
    client decides which service its request reaches.

    Every shape here was written by a reviewer against an earlier version:
    `+` and `%` and `.format` (round 1), `urljoin` and `+=` (round 2). A bare
    assignment is not a composition and is left alone; a message that wants the
    address calls the accessor and does not join a path to it.
    """
    def holds_a_base(*subtrees) -> bool:
        return any(_yields_a_base_url(sub, aliases)
                   or getattr(sub, "id", None) in tainted
                   for tree in subtrees for sub in ast.walk(tree))

    if isinstance(node, ast.JoinedStr):
        # **AN f-STRING IS A COMPOSITION ONLY IF IT JOINS SOMETHING TO THE
        # ADDRESS.** `f"unreachable at {vault_plane.base_url()}"` names the
        # address in a message and routes nothing, which `mcp-ops-server` does
        # today and should keep doing; `f"{BASE_URL}{path}"` and
        # `f"{BASE_URL}/api/issues"` build a request. The difference is a second
        # interpolation, or literal text carrying a path.
        if not holds_a_base(*node.values):
            return False
        interpolations = sum(1 for part in node.values
                             if isinstance(part, ast.FormattedValue))
        literal_path = any(isinstance(part, ast.Constant)
                           and isinstance(part.value, str) and "/" in part.value
                           for part in node.values)
        return interpolations > 1 or literal_path
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return holds_a_base(node.left, node.right)
    # `url = BASE_URL` then `url += path` produces no BinOp at all, and it is the
    # house style two clients in this repo already use to extend a URL.
    if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
        return holds_a_base(node.target, node.value)
    if isinstance(node, ast.Call):
        called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if called in {"format", "urljoin", "join"}:
            return holds_a_base(*node.args)
    return False


def _routed_url_without_routed_headers(node, aliases: set[str]) -> bool:
    """A request whose URL came from `vault_plane` and whose headers are a
    hand-built dict.

    **The invariant both reviewers had to find by reading.** `parked_sweep._do_park`
    in the sibling repo took its URL from `vault_plane.url()` and built its
    headers by hand, so it reached the right route and was refused for declaring
    no protocol version — a park failing on every run while unpark kept working,
    logged as a warning, never fatal (convivy/convivy-orient#175). The URL rule
    could not see it: the URL was right.

    **Only a DICT LITERAL with no `headers()` call inside it is flagged.** A
    headers value that is a name, or a dict spreading `vault_plane.headers(...)`
    into a larger one, is left alone — both are ordinary refactors of correct
    code, and a detector that flags those gets adopted by being disabled
    (reviewer, round 2).
    """
    if not isinstance(node, ast.Call):
        return False
    supplied = list(node.args) + [kw.value for kw in node.keywords]
    from_plane = any(
        isinstance(sub, ast.Call)
        and getattr(sub.func, "attr", None) == "url"
        and getattr(getattr(sub.func, "value", None), "id", None) in aliases
        for arg in supplied for sub in ast.walk(arg))
    if not from_plane:
        return False
    for candidate in supplied:
        if not isinstance(candidate, ast.Dict):
            continue
        keys = {k.value for k in candidate.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if not keys & {"Authorization", "Content-Type"}:
            continue
        routed = any(isinstance(sub, ast.Call)
                     and getattr(sub.func, "attr", None) == "headers"
                     for sub in ast.walk(candidate))
        if not routed:
            return True
    return False


def offenders_in(source: str, label: str) -> list[str]:
    """Every place in `source` that decides a plane for itself."""
    found = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found            # not Python this interpreter parses; not a client
    aliases = _plane_aliases(tree)
    tainted = _tainted_names(tree, aliases)
    named = _env_name_constants(tree)
    for node in ast.walk(tree):
        why = ("resolves a plane variable" if _reads_a_plane_variable(node, named)
               else "composes a URL from a base address"
               if _composes_a_base_url(node, aliases, tainted)
               else "takes its URL from vault_plane and its headers by hand"
               if _routed_url_without_routed_headers(node, aliases) else None)
        if why:
            found.append(f"{label}:{getattr(node, 'lineno', '?')}: {why}")
    return found


def source_files() -> list[Path]:
    """Every tracked Python file in the repo."""
    tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO,
                             capture_output=True, text=True,
                             check=True).stdout.split()
    return [REPO / n for n in tracked
            if n not in _SWEEP_EXEMPT and (REPO / n).is_file()]


# **THE POSITIVE CONTROLS, and the reason they are not optional.** A sweep that
# has only ever run against a repo it finds clean cannot tell "the detector works"
# from "the detector matches nothing" — which is what a reviewer proved by
# planting `os.getenv` and a `+` concatenation and watching both tests pass.
# Every shape below was written by a reviewer attacking a version of this file.
_PLANTED = [
    ('BASE = os.getenv("ORIENT_BASE_URL", "http://x")', "os.getenv"),
    ('BASE = os.environ.get("ORIENT_BASE_URL", "http://x")', "os.environ.get"),
    ('BASE = os.environ["LIBRARY_BASE_URL"]', "os.environ[...]"),
    ('from os import environ\nBASE = environ.get("VAULTS_ON_LIBRARY")',
     "a bare environ"),
    ('url = BASE_URL + path', "concatenation"),
    ('url = f"{BASE_URL}{path}"', "an f-string"),
    ('url = f"{sc.BASE_URL}/api/issues"', "an f-string on another module"),
    ('url = "{}{}".format(BASE_URL, path)', ".format"),
    ('url = "%s%s" % (BASE_URL, path)', "percent formatting"),
    ('url = urllib.parse.urljoin(BASE_URL, path)', "urljoin"),
    ('_ORIENT_ENV = "ORIENT_BASE_URL"\nBASE = os.environ.get(_ORIENT_ENV)',
     "the variable name held in a constant"),
    ('_ENV = "LIBRARY_BASE_URL"\nBASE = os.getenv(_ENV, "http://x")',
     "the same, through getenv"),
    ('_ENV: str = "ORIENT_BASE_URL"\nBASE = os.environ.get(_ENV)',
     "the same, annotated"),
    ('url: str = BASE_URL\nurl += path', "an annotated tainted name"),
    ('url = BASE_URL\nurl += path', "augmented assignment"),
    ('import vault_plane\nurl = vault_plane.base_url() + path',
     "composing with the accessor"),
    ('import vault_plane\nurl = f"{vault_plane.base_url()}{path}"',
     "an f-string on the accessor"),
    ('import vault_plane\n'
     'req = Request(vault_plane.url("/api/issues"),\n'
     '              headers={"Authorization": f"Bearer {tok}"})',
     "a routed URL with hand-built headers"),
    ('import vault_plane as vp\n'
     'req = Request(vp.url("/api/issues"),\n'
     '              headers={"Authorization": f"Bearer {tok}"})',
     "the same, through a module alias"),
    ('import vault_plane\n'
     'req = Request(url=vault_plane.url("/api/issues"),\n'
     '              headers={"Authorization": f"Bearer {tok}"})',
     "the same, with the URL passed by keyword"),
    ('import vault_plane\n'
     'req = Request(vault_plane.url("/api/issues"), data,\n'
     '              {"Authorization": f"Bearer {tok}"})',
     "the same, with headers passed positionally"),
]

_ALLOWED = [
    'BASE_URL = vault_plane.ORIENT_BASE_URL',
    'url = vault_plane.url("/api/issues")',
    'headers = vault_plane.headers(token=tok, json_body=True)',
    'import vault_plane\n'
    'req = Request(vault_plane.url("/api/issues"),\n'
    '              headers=vault_plane.headers(token=tok))',
    'import vault_plane\n'
    'hdrs = vault_plane.headers(token=tok)\n'
    'req = Request(vault_plane.url("/api/issues"), headers=hdrs)',
    'import vault_plane\n'
    'req = Request(vault_plane.url("/api/issues"),\n'
    '              headers={**vault_plane.headers(token=tok), "X-Extra": "1"})',
]


@pytest.mark.parametrize("snippet,shape", _PLANTED,
                         ids=[shape for _, shape in _PLANTED])
def test_the_sweep_flags_a_planted_offender(snippet, shape):
    assert offenders_in(snippet, "planted"), (
        f"the sweep does not catch {shape}, so a client written that way would "
        f"keep writing to Orient after the cutover with nothing to say so")


@pytest.mark.parametrize("snippet", _ALLOWED)
def test_the_sweep_does_not_flag_the_approved_shapes(snippet):
    """The other half of a positive control: a detector that flags everything is
    as useless as one that flags nothing, and it gets adopted by being disabled.
    The last two are ordinary refactors of correct code — pulling a keyword into
    a variable, spreading the routed headers into a larger dict — which an
    earlier version flagged (reviewer, round 2)."""
    assert not offenders_in(snippet, "allowed"), snippet


def test_nothing_outside_vault_plane_decides_a_plane_for_itself():
    """**The count becomes a check.** A client that resolves the base URL or
    joins it to a path decides for itself which service holds the vaults, and at
    the cutover that decision is made once, silently, in whichever file nobody
    remembered.

    This fails on the file rather than on the deploy, which is the only place the
    failure is cheap.
    """
    offenders = []
    for path in source_files():
        offenders += offenders_in(path.read_text(errors="replace"),
                                  str(path.relative_to(REPO)))
    assert not offenders, (
        "these decide which service holds the vaults for themselves; call "
        "vault_plane.url(path) and vault_plane.headers(...) instead, so the "
        "write cutover repoints them:\n  " + "\n  ".join(offenders))


# --- the address, which carries a bearer token ----------------------------------

def test_a_remote_plain_http_library_is_refused():
    """**The write path is held to the read path's bar** (reviewer,
    convivy/convivy-orient#175). `library_client._resolve_base_url` refuses at
    import unless the address is loopback or https, because it sends a bearer
    token; these four jobs send one too. Without this, one variable would sit at
    two safety bars and the lower one would be the writes.
    """
    with pytest.raises(RuntimeError) as exc:
        vault_plane._refuse_cleartext("http://library.example.com")
    assert "cleartext" in str(exc.value)


def test_loopback_and_https_are_allowed():
    for allowed in ("http://127.0.0.1:8100", "http://localhost:8100",
                    "https://library.example.com"):
        assert vault_plane._refuse_cleartext(allowed) == allowed


# --- the redirect, which is how the import-time check gets bypassed -------------

def _redirect_to(target: str, origin: str = "https://library.example.com/items"):
    """Ask the installed handler to follow a redirect from `origin` to `target`."""
    import urllib.request
    handler = vault_plane._NoDowngrade()
    return handler.redirect_request(urllib.request.Request(origin), None, 302,
                                    "Found", {}, target)


def test_a_redirect_down_to_cleartext_is_refused():
    """**`_refuse_cleartext` checks the configured address once, at import, and a
    redirect is how that check is bypassed at run time** (security-reviewer,
    convivy/agent-platform#400).

    `urlopen`'s default opener follows 30x and copies every header except
    `Content-Length` and `Content-Type` onto the new request, with no scheme or
    host check — so an https Library answering `Location: http://anywhere`
    resends `Authorization: Bearer <token>` in the clear. Reaching it needs
    control of the Library origin or whatever fronts it, which is exactly the
    actor the guard names.
    """
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as exc:
        _redirect_to("http://attacker.example.com/items")
    assert "cleartext" in str(exc.value)


def test_a_redirect_to_another_host_is_refused_even_over_https():
    """**Refusing only cleartext was not enough** (security-reviewer, round 3).
    `Location: https://attacker.example.com` carried the bearer token intact —
    exfiltration with the transport doing its job. The same actor reaches both,
    so the host has to match as well as the scheme."""
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as exc:
        _redirect_to("https://attacker.example.com/items")
    assert "leaves the origin" in str(exc.value)


def test_a_redirect_to_another_port_on_the_same_host_is_refused():
    """**Comparing the host alone let the token move between loopback services**
    (security-reviewer, round 4). `LIBRARY_BASE_URL` defaults to
    `http://127.0.0.1:8100` and this box runs several services on loopback ports,
    so a host-only check follows `Location: http://127.0.0.1:9999` and hands the
    bearer token to whatever is listening. "Where the request was sent" means the
    origin, which includes the port."""
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        _redirect_to("http://127.0.0.1:9999/collect", "http://127.0.0.1:8100/items")
    with pytest.raises(urllib.error.HTTPError):
        _redirect_to("https://library.example.com:8443/items")


def test_the_scheme_default_port_is_the_same_origin():
    """`https://host` and `https://host:443` are one origin, not two — a guard
    that called them different would refuse an ordinary redirect."""
    assert _redirect_to("https://library.example.com:443/items/") is not None


def test_a_same_host_redirect_is_followed():
    """The other half: a guard that refused every redirect would be discovered
    as an outage rather than as a control. A trailing slash or a moved path on
    the host the request was already sent to is ordinary."""
    assert _redirect_to("https://library.example.com/items/") is not None


def test_the_guard_is_the_process_default():
    """Installed rather than offered. Fourteen call sites reach the stdlib
    directly, and a control that depends on all fourteen adopting a new name is
    one the fifteenth will miss."""
    import urllib.request
    installed = urllib.request._opener
    assert installed is not None, "vault_plane did not install its opener"
    assert any(isinstance(h, vault_plane._NoDowngrade) for h in installed.handlers)
