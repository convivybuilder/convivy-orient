"""Config and state roots for convivy-orient (ADR-0059 rule 3).

`platform/decisions/0059` moves Orient's code to a deploy area that an install
overwrites wholesale on every deploy. Config, credentials, state and output
must not travel with the code: an install that copies them in either
overwrites a working configuration with a stale copy, or destroys accumulated
state, and both failures are silent. So they are resolved from their own
roots, independent of the code's own directory, rather than as `DIR / "..."`
paths anchored on `Path(__file__).resolve().parent`.

Each root defaults to today's location — the checkout directory — so this
module is a no-op until an install sets the environment variable. Nothing
moves on disk here.

CONFIG_ROOT holds the config/credential files read as `<name>.json`:
tiers.json, jwt.json, api-tokens.json, roles.json, concierge.json.

STATE_ROOT holds output and log state: the built `site/` bundle (both the
live-render fallback pages and the static KB bundles) and `access.log`.

A relative value in either environment variable would resolve against the
process's current working directory at import time — reintroducing the
working-directory dependence this module exists to remove, in the one place
nobody would look for it, since nothing before this pointed a launchd job's
`WorkingDirectory` at anything but the code (platform/decisions/0059's
security review, carried into the bin/install-orient cutover). So a relative
value is refused here rather than silently accepted; this is the only reader
of either variable, so it is the one place capable of catching it regardless
of which future caller sets it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def _resolve_root(env_var: str) -> Path:
    value = os.environ.get(env_var)
    if value is None:
        return _DIR
    root = Path(value)
    if not root.is_absolute():
        raise ValueError(
            f"{env_var}={value!r} must be an absolute path, not a path resolved "
            "against the current working directory (ADR-0059 rule 3)."
        )
    return root


CONFIG_ROOT = _resolve_root("ORIENT_CONFIG_ROOT")
STATE_ROOT = _resolve_root("ORIENT_STATE_ROOT")

# The production surface for a deployed CLI tool a launchd job invokes, as
# opposed to `~/.local/bin` — the DEVELOPER symlink farm into the
# agent-platform working checkout (93 entries, deliberately unmanaged by that
# repo's own installer). A production job that resolves a tool through the
# developer surface makes a branch checked out there live to Orient, which is
# the exact failure ADR-0059's deploy-area cutover exists to prevent
# (issue_01M1DKJYHSH9FT1SBP9068MTT1: two of Orient's deploy-pinned jobs did
# this, one of them blocking every /memory save because its check for `agentctl`
# never found it under the plist's production PATH).
#
# Anchored on the running account's home, the root `bin/install-orient` deploys
# under (`$HOME/.local/deploy`). It once named the operator's home literally, and a second
# account's install died at import: stat on another person's home raises
# EACCES, which `is_file()` does not swallow (Shipwright's rehearsal on macmini,
# 2026-10-02). Where that home is readable, the same literal ran the operator's tools
# from inside someone else's install.
_DEPLOY_TOOL_BIN = Path.home() / ".local" / "deploy" / "agent-platform-shared" / "bin"


def resolve_tool(name: str) -> str | None:
    """Resolve `name` (e.g. `"agentctl"`, `"kb-eval"`) to the path a
    subprocess should invoke it at.

    Prefers `_DEPLOY_TOOL_BIN / name` when it exists and is executable, so a
    deploy-pinned launchd job never reaches into `~/.local/bin`. Falls back to
    `shutil.which(name)` — ordinary PATH resolution — so a developer checkout
    or CI, where the deploy area does not exist, is unaffected. Returns
    `None` if neither resolves, matching `shutil.which`'s own contract.

    A stat call, not a subprocess or network call — safe for a caller that
    must answer cheaply on every request.

    Every caller resolving the same tool name within one logical operation
    (e.g. `kb_commit.commit_document`'s checkout/apply/release sequence) must
    resolve once and reuse the result — not call this again per subprocess —
    so a single operation can't invoke two different binaries if the deploy
    area changes mid-request.
    """
    deployed = _DEPLOY_TOOL_BIN / name
    if deployed.is_file() and os.access(deployed, os.X_OK):
        return str(deployed)
    return shutil.which(name)
