"""Tailscale whois client (CQ·150, E21).

The tailnet listener's only identity source. `whois(host, port)` asks the
local Tailscale daemon which tailnet login owns the peer at that address. It
is never asked over a request header: the `Tailscale-User-Login` header
Tailscale Serve sets is unsigned, and any local process could forge it.

**The daemon is reached through the `tailscale` CLI (`tailscale whois --json
<addr>`), not by dialling its LocalAPI.** The production host runs the macOS Tailscale app,
whose LocalAPI is not the open-source `tailscaled` Unix socket: neither
`/var/run/tailscaled.socket` nor `/var/run/tailscale/tailscaled.sock` exists
there (checked 2026-10-15). The CLI finds the LocalAPI on either build, and
`tailscale whois --json` answered there for a user's device and for a tagged
node alike.

Four failures collapse into the one return value, `None`, because every
caller refuses the request for each: the CLI cannot be run or times out, the
daemon cannot name the peer ("peer not found"), the answer is malformed, and
the peer is a tagged node. **A tagged node is recognised by `Node.Tags`, not
by its login.** On the production host, `tailscale whois` of the host's own tailnet address
reads `UserProfile.LoginName` as the machine's own name,
`host-a.example.test`, with `Node.Tags` set. Taking that name as an
identity would hand every tagged machine the contractor tier, since any
authenticated identity outside the full list is a contractor.

Answers are cached per peer address for `_CACHE_TTL_SECONDS`. The key is the
address alone: a tailnet address names one node, and the port changes on
every connection, so a key carrying the port would never hit.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import paths

_TAILSCALE_NAME = "tailscale"
_CACHE_TTL_SECONDS = 30
_TIMEOUT_SECONDS = 2.0

# The CLI needs HOME and PATH and nothing else from this process's environment.
_ENV_ALLOWLIST = ("HOME", "PATH")

# {host: (cached_at_monotonic, login_or_None)}
_cache: dict[str, tuple[float, str | None]] = {}


def _request(addr: str, *, timeout: float) -> bytes | None:
    """`tailscale whois --json <addr>`'s stdout on exit 0, or `None` when the
    CLI is missing, fails, times out or cannot name the peer (it exits
    nonzero with "peer not found" on stderr)."""
    tool = paths.resolve_tool(_TAILSCALE_NAME) or _TAILSCALE_NAME
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    try:
        done = subprocess.run([tool, "whois", "--json", addr], capture_output=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def _login_from_response(body: bytes) -> str | None:
    """`UserProfile.LoginName` out of one whois answer, or `None` for a
    malformed answer, an absent profile, or a tagged node."""
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    node = doc.get("Node")
    if isinstance(node, dict) and node.get("Tags"):
        return None
    profile = doc.get("UserProfile")
    login = (profile.get("LoginName") if isinstance(profile, dict) else None) or ""
    login = login.strip()
    if not login or login == "tagged-devices":
        return None
    return login


def whois(host: str, port: int, *, timeout: float = _TIMEOUT_SECONDS) -> str | None:
    """The tailnet login owning the peer at `host`, or `None` when the daemon
    cannot be asked, cannot name the peer, or names a tagged node. `port` is
    the connection's own and plays no part in the answer. Cached per host for
    `_CACHE_TTL_SECONDS`, so a revoked login stops being honoured within one
    cache window."""
    now = time.monotonic()
    cached = _cache.get(host)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    body = _request(host, timeout=timeout)
    login = _login_from_response(body) if body is not None else None
    _cache[host] = (now, login)
    return login
