"""Which service holds the vaults, for Orient's own scheduled jobs.

**The Library holds the vaults. The cutover ran at about 04:00 on 2026-08-17**,
moving the Chart House, the Archives and the decision plane into the Library's own
file (Phase 3 §4). This job, `github_capture`, is an HTTP client of that surface
exactly like the fleet's agents are, so it moved with the fleet. It is in this
repo because it runs beside Orient, not because it is part of it. (`market_digest`
was another such client; it retired with the /market page it fed, the operator
2026-08-26. `parked_sweep` retired on 2026-09-12 without ever having run: The operator
ruled that a job judging and writing work items does not belong in a
view-mostly client. `gmail_capture` retired on 2026-09-16, when the operator moved
capture to the Signals product.)

**The failure this exists to prevent is silent, and it happened.** Orient's copy
of the moved tables survives until Stage C, days after the cutover, so a job still
posting to `ORIENT_BASE_URL` gets a `200`, writes to a database nobody reads any
more, and reports success. A capture job that quietly stops delivering is the
worst version of that: nothing errors, and the Archives simply stop growing. Over
the twenty hours after the cutover, 56 rows accumulated in Orient's copy that the
Library has never seen, because the variable below was set on 8 of 83 launchd jobs
and the other 75 kept collecting their `200`s
(`issue_01M0948WHA3BM3D72CX3D7XT0Q`). The default inverted on 2026-08-18 as a
result; see the constant.

**The planes are not the same shape**, which is why pointing `ORIENT_BASE_URL` at
port 8100 does not work: the Library serves no `/api` prefix, calls work items
`items` rather than `issues`, and refuses a request that declares no protocol
version.

**`VAULTS_ON_LIBRARY` unset means the Library**, so a job, a login shell or a git
hook that was never told about the cutover writes to the right store. Set it to
`0` to reach Orient's copy deliberately, which is the only thing that should ever
want it before Stage C drops those tables.

**The switch is distinct from the address, because this repo already has the
address.** `library_client.py` reads `LIBRARY_BASE_URL` with a default of
`http://127.0.0.1:8100` and has pointed there since the read rewire. Reusing that
name as the cutover switch would give one variable two meanings, so moving the
Library to another port would silently move every capture job's writes with it,
days early. Where the Library IS and whether the vaults are THERE are different
questions, and only the second one is the cutover.

**A copy of `agent-platform/lib/agent-platform/vault_plane.py`, deliberately.**
Orient is becoming a CLIENT of the Library (Phase 6), and a client that imported
the platform's library at runtime would point a dependency back at the Keel — the
direction the extraction exists to remove. Forty lines in two repos is the
cheaper of the two couplings, and the mapping it encodes is fixed by the Library's
protocol contract rather than by either repo's taste.
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request

ORIENT_BASE_URL = os.environ.get("ORIENT_BASE_URL", "http://127.0.0.1:8099")

# Where the Library is — the same variable and default `library_client` reads, so
# one address answers for both and neither has to be told about the other.

# The same bar `library_client._resolve_base_url` holds the READ path to, applied
# to the write path, because both read this variable and both attach a bearer
# token (reviewer, convivy/convivy-orient#175). Without it, pointing
# `LIBRARY_BASE_URL` at a remote plain-http host would make `library_client`
# refuse to import while these four jobs happily sent the token in the clear —
# two copies of one address at two different safety bars, which is exactly the
# drift the duplication above has to be watched for.
#
# Duplicated rather than imported for the reason the module is: importing
# `library_client` here would pull Orient's whole read client into four capture
# jobs that need one address.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _refuse_cleartext(raw: str) -> str:
    host = (urllib.parse.urlparse(raw).hostname or "").lower()
    if host not in _LOOPBACK_HOSTS and not raw.startswith("https://"):
        raise RuntimeError(
            f"LIBRARY_BASE_URL={raw!r} is neither loopback nor https — refusing "
            "rather than sending the bearer token in cleartext over the network. "
            "Use an https:// URL for a remote Library, or leave LIBRARY_BASE_URL "
            "unset to use the local default.")
    return raw


LIBRARY_BASE_URL = _refuse_cleartext(
    os.environ.get("LIBRARY_BASE_URL", "http://127.0.0.1:8100").strip())

# Whether the vaults are THERE. THE CUTOVER HAS RUN, SO UNSET NOW MEANS THE
# LIBRARY, and `VAULTS_ON_LIBRARY=0` is the explicit way back to Orient.
#
# It defaulted to Orient until 2026-08-18, deliberately, so that landing this
# module before the cutover window changed nothing (ADR-0016). That default was
# right for exactly as long as Orient was authoritative, and it became the
# failure mode the moment the vaults moved: a variable set in 8 of 83 launchd
# jobs, in no login shell and in no git hook, left every other writer posting to
# a database nobody reads and collecting a `200` for it. Fifty-six rows
# accumulated in Orient's superseded copy over the twenty hours after the
# cutover (`issue_01M0948WHA3BM3D72CX3D7XT0Q`,
# `issue_01M094X927D4S4D1M3RN4YZX9X`).
#
# So the default is inverted rather than the variable being set on the other 75
# jobs: setting it needs 75 correct edits to be safe against one omission to be
# wrong, and it still catches no job and no session created afterwards. What the
# old default had, and lost at the cutover, is that doing nothing is safe. This
# gives that property back.
#
# **The paired copy in `agent-platform` inverts in the same window**
# (agent-platform#406). What has to match across the two files is the SEMANTICS
# of the plane choice rather than their text — they are a deliberate copy, not a
# mirror, and they differ in surrounding code. One side inverting alone would
# leave the two halves disagreeing about which store is authoritative, which
# moves the split rather than closing it.
# A function rather than an inline expression so the rule itself is testable:
# the constant is resolved at import, so a test that sets the variable afterwards
# is testing nothing. `_refuse_cleartext` above is split for the same reason.
def _resolve_on_library() -> bool:
    """Absent means the Library, `1` means the Library, `0` means Orient, and
    anything else raises — **including a value that is present and empty**.

    **Absent and empty are different states and this distinguishes them**, which
    is why the lookup takes no default string. A templated deploy that emits
    `VAULTS_ON_LIBRARY=` instead of omitting the key has produced a value it did
    not mean to; reading that as "unset" accepts a broken template silently, and
    the destination only happens to be safe today. The next plane transition would
    inherit "empty means whatever the default is", which is the shape of the bug
    this whole change exists to remove (agent-platform#406's security review found
    it on the other copy, where empty silently reverted a job to the superseded
    store).

    Whitespace is stripped before comparing, so a plist padding its value to
    `" 1 "` is accepted. A padded value says what it means, and refusing it would
    be strictness carrying no safety. A value that is only whitespace strips to
    empty and raises with the rest.

    **Refusing an unrecognised value is the point, and it beats both of the
    obvious alternatives.** Silently treating everything except `0` as the
    Library sends an operator who typed `false` to the store they meant to leave.
    Accepting a set — `0`, `false`, `no`, `off` — is worse rather than better: it
    is always one synonym short of what someone types, so it narrows that path
    instead of closing it, while teaching a generalisation it does not honour. An
    operator with no theory about synonyms looks the value up; an operator whose
    theory is confidently wrong does not.

    Raising covers both directions. `true` cannot quietly mean Orient, and
    `false` cannot quietly mean the Library. The mistake surfaces where it was
    made rather than in a store that stopped growing.

    This is the treatment `_refuse_cleartext` already gives a misconfigured
    address, for the same reason: a setting this module cannot interpret is not
    one it should guess at. Checked before adopting it — across 83 installed
    launchd jobs the variable is `1` on 8 and absent on the rest, and it appears
    in no shell startup file and no git hook, so nothing deployed trips this.
    """
    raw = os.environ.get("VAULTS_ON_LIBRARY")
    if raw is None:
        return True
    raw = raw.strip()
    if raw in ("0", "1"):
        return raw == "1"
    raise RuntimeError(
        f"VAULTS_ON_LIBRARY={raw!r} is neither '0' nor '1'. Refusing to guess "
        f"which store you meant: guessing the Library would ignore a rollback "
        f"you asked for, and guessing Orient would write into the superseded "
        f"copy, which is the failure this variable exists to prevent. Unset it "
        f"for the Library, or set it to '0' to reach Orient's copy deliberately.")


VAULTS_ON_LIBRARY = _resolve_on_library()

PROTOCOL_HEADER = "Library-Protocol"
PROTOCOL_VERSION = "1"

# Orient's noun for a work item, and the Library's. The rename is the only part
# of the mapping that is not mechanical: `/api/issues` looks like it should
# become `/issues`, which the Library does not serve at all.
_ORIENT_ITEMS = "/api/issues"
_LIBRARY_ITEMS = "/items"


def on_library() -> bool:
    return VAULTS_ON_LIBRARY


def base_url() -> str:
    return LIBRARY_BASE_URL if on_library() else ORIENT_BASE_URL


def translate(path: str) -> str:
    """One of Orient's API paths in the shape the configured plane serves it.

    **Rewriting is now the default behaviour**, since an unset `VAULTS_ON_LIBRARY`
    resolves to the Library. This docstring previously said the opposite — that
    leaving the path untouched "is what makes adopting this a no-op until the
    variable is set" — which described the world before 2026-08-18, when the
    default was Orient and this function did nothing until somebody opted in. A
    reader trusting that line would conclude the rollout changes nothing, which is
    the class of error the flip exists to fix (reviewer, convivy/convivy-orient#176).

    The path is only left untouched on the deliberate `VAULTS_ON_LIBRARY=0`
    opt-out. A query string is carried across unmodified either way: only the path
    is rewritten, so `/api/issues?state=open` keeps its filter, and a filter
    dropped in translation would be a wider answer rather than an error.
    """
    if not on_library():
        return path
    cut = re.search(r"[?#]", path)
    head, sep, query = (path[:cut.start()], path[cut.start()], path[cut.start() + 1:]) \
        if cut else (path, "", "")
    if head == _ORIENT_ITEMS or head.startswith(_ORIENT_ITEMS + "/"):
        head = _LIBRARY_ITEMS + head[len(_ORIENT_ITEMS):]
    elif head.startswith("/api/"):
        head = head[len("/api"):]
    return head + sep + query


def url(path: str) -> str:
    return f"{base_url()}{translate(path)}"


class _NoDowngrade(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to carry the bearer token downhill.

    **`_refuse_cleartext` checks the CONFIGURED address once, at import, and a
    redirect is how that check gets bypassed at run time** (security-reviewer,
    convivy/agent-platform#400). `urlopen`'s default opener follows 301, 302,
    303, 307 and 308, and `HTTPRedirectHandler.redirect_request` copies every
    header except `Content-Length` and `Content-Type` onto the new request, with
    no scheme or host check. So an `https://` Library that answers `Location:
    http://anywhere` gets `Authorization: Bearer <token>` resent in the clear —
    the precise thing the import-time check exists to prevent, defeated by the
    origin it was told to trust.

    Reaching this needs control of the Library origin or whatever fronts it,
    rather than a passive network position. That is the actor the guard names, so
    the guard has to hold against it — and against the same actor sending the
    token somewhere ENCRYPTED that it does not belong. Refusing only cleartext
    left `Location: https://attacker.example.com` carrying
    `Authorization: Bearer <token>` intact, which is exfiltration with the
    transport doing its job (security-reviewer, round 3). So the host has to
    match as well as the scheme.

    **The rule is that a vault request stays where it was sent, and "where" means
    the ORIGIN — scheme, host and port.** A same-origin redirect is ordinary (a
    trailing slash, a path move) and is followed; anything else is refused rather
    than stripped of its token, because a Library that answers somewhere else is a
    configuration change rather than a request to follow, and a silently
    de-authenticated retry would come back 401 and send its operator to the wrong
    layer.

    **Comparing the host alone was not enough, and loopback is why**
    (security-reviewer, round 4). `LIBRARY_BASE_URL` defaults to
    `http://127.0.0.1:8100` and the deployment runs several services side by side
    on loopback ports, so a host-only check follows `Location:
    http://127.0.0.1:9999` and hands the bearer token to whatever is listening
    there. That needs a second bug — an open redirect in the Library — to reach,
    but the docstring claimed an origin guarantee the code did not enforce, which
    is the part that had to be fixed either way.

    The refusal is a `HTTPError` rather than a silent stop, because a client that
    quietly received no answer would report the plane unreachable and send its
    operator to the wrong layer.
    """

    # A URL's origin, in the sense RFC 6454 means it: scheme, host AND port. A
    # missing port is the scheme's default, so `https://host` and
    # `https://host:443` are one origin rather than two.
    _DEFAULT_PORTS = {"http": 80, "https": 443}

    @classmethod
    def _origin(cls, url: str) -> tuple:
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        return (scheme, (parsed.hostname or "").lower(),
                parsed.port or cls._DEFAULT_PORTS.get(scheme))

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        host = (target.hostname or "").lower()
        if host not in _LOOPBACK_HOSTS and not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing a redirect from {req.full_url} to {newurl}: it is "
                f"neither loopback nor https, and following it would resend the "
                f"bearer token in cleartext", headers, fp)
        if self._origin(newurl) != self._origin(req.full_url):
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing a redirect from {req.full_url} to {newurl}: it "
                f"leaves the origin the request was sent to, and following it "
                f"would hand the bearer token to {host or 'another host'}",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# **INSTALLED AS THE PROCESS DEFAULT, rather than handed to callers as a wrapper
# they each have to remember.** Fourteen call sites across seven files call
# `urllib.request.urlopen`, and a control that depends on all fourteen adopting a
# new name is a control the fifteenth will miss — which is the same failure this
# whole module exists to remove, one layer down.
#
# It is also the seam the test suite already owns: `tests/conftest.py` refuses a
# live-plane call by patching `urllib.request.urlopen`, and every client test
# stubs the same name. A wrapper would have moved the seam out from under all of
# them, which is how the first version of this arrived at 96 failing tests.
#
# Process-wide is broader than the vault, and that is acceptable in one
# direction only: this handler can refuse a redirect and can never permit one the
# stdlib would have refused. Nothing else in the process loses an option.
urllib.request.install_opener(urllib.request.build_opener(_NoDowngrade()))


def headers(token: str | None = None, *, json_body: bool = False) -> dict:
    """The headers a request to the configured plane needs. The protocol header
    goes only to the Library, which is the only service that reads it."""
    out: dict[str, str] = {}
    if token:
        out["Authorization"] = f"Bearer {token}"
    if json_body:
        out["Content-Type"] = "application/json"
    if on_library():
        out[PROTOCOL_HEADER] = PROTOCOL_VERSION
    return out
