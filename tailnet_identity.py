"""Tailnet listener configuration + identity resolution (CQ·150, E21).

A second listener is off by default: with no `tailnet.json` (or one with
`"enabled": false` or a blank `"bind"`), `tailnet_config()` reports
disabled, `server.py` binds loopback only, and no request is ever
identified by `login_for_peer` — the install behaves exactly as it does
today. Re-read per call, the same no-restart-to-change convention
`access_identity.jwt_config`/`TIERS` already use. Only `server._serve`
reads it for the listener, at startup: which socket a request arrived on is
decided from the sockets actually bound (`listener_for`), so a change to
tailnet.json takes effect on restart and never reclassifies an open socket.
"""
from __future__ import annotations

import json

from paths import CONFIG_ROOT
import tailscale_whois

TAILNET_CONFIG = CONFIG_ROOT / "tailnet.json"
DEFAULT_PORT = 8099


def tailnet_config() -> dict:
    """{"enabled": bool, "bind": str, "port": int}. Absent, unreadable or
    malformed `tailnet.json`, or a blank `"bind"`, all resolve to disabled —
    the same fail-closed default `access_identity.jwt_config` uses: a bad
    config file must shrink what the site serves, never silently widen it
    to a bind nobody reviewed."""
    try:
        c = json.loads(TAILNET_CONFIG.read_text())
    except Exception:
        return {"enabled": False, "bind": "", "port": DEFAULT_PORT}
    if not isinstance(c, dict):
        return {"enabled": False, "bind": "", "port": DEFAULT_PORT}
    bind = (c.get("bind") or "").strip()
    port = c.get("port", DEFAULT_PORT)
    if not isinstance(port, int):
        port = DEFAULT_PORT
    return {"enabled": bool(c.get("enabled", False)) and bool(bind), "bind": bind, "port": port}


LOOPBACK = "loopback"
TAILNET = "tailnet"

# {(host, port): LOOPBACK | TAILNET} for every socket `server._serve` bound at
# startup. The listener a request arrived on is decided from this record, never
# from tailnet.json as it reads now: editing or disabling tailnet.json without a
# restart leaves the tailnet socket open, and a socket reclassified as loopback
# would trust the Cloudflare email header from a tailnet peer (security review,
# PR #381).
_bound: dict[tuple[str, int], str] = {}


def record_listener(addr: tuple[str, int], kind: str) -> None:
    """Note a socket `server._serve` has bound, and which listener it is."""
    _bound[(addr[0], addr[1])] = kind


def listener_for(server: tuple | None) -> str | None:
    """LOOPBACK or TAILNET for the local address a request landed on (the ASGI
    scope's "server", which uvicorn sets from the accepted socket), or `None`
    for a socket `_serve` never bound; every caller refuses `None`.

    With nothing recorded, the process was not started through `_serve` (the
    test suite's in-process client), so only the loopback path exists and
    every request takes it, as before CQ·150."""
    if not _bound:
        return LOOPBACK
    if not server or len(server) < 2:
        return None
    return _bound.get((server[0], server[1]))


def login_for_peer(host: str, port: int) -> str | None:
    """The tailnet login owning the peer at `host:port` — `tailscale_whois.
    whois`'s own three collapsed no-identity cases (daemon unreachable,
    peer unnamed, tagged node with no user) all return `None` here too, and
    `server.py`'s tailnet gate treats every one of them as an unidentified
    request.

    Lowercased, as the Cloudflare path lowercases its email: `tier_for`
    compares against tiers.json lowercased, so a login in another case
    would miss the full tier (review, PR #381)."""
    login = tailscale_whois.whois(host, port)
    return login.lower() if login else None
