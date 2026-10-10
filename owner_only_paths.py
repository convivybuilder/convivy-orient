"""Owner-only directory and file creation (issue_01M2MFGVAW1C8ZQ7G9CT0E1YXZ).

`~/.orient-control/` holds credential-shaped state (push subscriptions,
capture cursors, the control DB) alongside token files that were already
owner-only. `mkdir(mode=...)` is masked by the process umask and is a no-op
on a directory that already exists (`exist_ok=True` skips the mode entirely),
so a `644`/`755` writer anywhere in the chain leaves the mode wherever it
started. These two helpers set the mode explicitly rather than relying on
umask or a create-then-chmod window: `ensure_owner_only_dir` chmods after
every call, and `ensure_owner_only_file` opens with the mode baked into the
`open(2)` call so there is no window where a new file is briefly readable.

No sqlalchemy/alembic import here on purpose — `github_capture.py` is a
lightweight launchd-triggered script and should stay that way.
"""
from __future__ import annotations

import os
from pathlib import Path


def ensure_owner_only_dir(path: Path) -> None:
    """`path` is 0700 on return, whether newly created or already there at a
    looser mode."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def ensure_owner_only_file(path: Path) -> None:
    """Create `path` empty at 0600 if it doesn't exist yet. A no-op on an
    existing file — callers that immediately overwrite it (`write_text`, a
    DB engine's first connect) inherit this mode rather than the umask's,
    since opening an existing file for write never changes its mode bits."""
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
