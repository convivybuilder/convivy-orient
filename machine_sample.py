"""What the machine looked like at the instant a Library call timed out
(issue_01M2KW4H2CTXXXM2JKWB6NZG09).

On 2026-09-15 a `library_client` read raised `TimeoutError` for several
minutes. `serve.log` recorded the symptom — the timeout — and nothing about
the machine underneath it, so the cause (memory pressure: 2.45 GB of 3 GB
swap in use, ~75 MB free pages) had to be reconstructed afterwards from
another crew's store, after the fact and from someone else's data. This
module is what `library_client._request` calls on the timeout path only, so
next time the one line already says enough.

**Sampled only on a timeout, never on a successful request.** A healthy read
does none of this — see `library_client._request`'s call site, which invokes
`record_timeout` exclusively from its timeout handlers.

**Every part of the sample is independently best-effort.** `sysctl` or
`vm_stat` failing, hanging, or being absent on a non-Darwin box must not cost
the caller the one field the others still have — each source degrades to
`"unavailable"` on its own rather than taking the whole line down.
`record_timeout` itself never raises: a bug in this module must not replace
the original `TimeoutError` with something worse.

No sqlalchemy/alembic import here on purpose, matching `owner_only_paths.py`'s
rule for the other lightweight launchd-adjacent scripts — this has to stay
cheap to import on a path that is already failing.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from logutil import clean_log_field

_SUBPROCESS_TIMEOUT = 2.0

AGENT_RUNS_ROOT = Path.home() / "Convivy" / "agent-runs"

# A pathological date directory (or a run tree gone wild) must not make the
# scan unbounded — this is generous against the ~220 runs/day observed on
# 2026-09-16, not a tuned production ceiling.
_MAX_RUN_DIRS_SCANNED = 20000

_SWAP_USED_RE = re.compile(r"used\s*=\s*([0-9.]+\w*)")
_FREE_PAGES_RE = re.compile(r"Pages free:\s*(\d+)\.")


def _swap_used() -> str:
    """The `used` field of `sysctl vm.swapusage`, e.g. `"898.38M"` — or
    `"unavailable"` if the subprocess fails, hangs past its timeout, or the
    output doesn't parse."""
    try:
        out = subprocess.run(
            ["sysctl", "vm.swapusage"], capture_output=True, text=True,
            timeout=_SUBPROCESS_TIMEOUT, check=True).stdout
    except Exception:  # noqa: BLE001 — every failure mode degrades the same way
        return "unavailable"
    m = _SWAP_USED_RE.search(out)
    return m.group(1) if m else "unavailable"


def _free_pages() -> str:
    """The `Pages free` count from `vm_stat`, as a string — or
    `"unavailable"` under the same failure modes as `_swap_used`."""
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True,
            timeout=_SUBPROCESS_TIMEOUT, check=True).stdout
    except Exception:  # noqa: BLE001 — every failure mode degrades the same way
        return "unavailable"
    m = _FREE_PAGES_RE.search(out)
    return m.group(1) if m else "unavailable"


def _parse_iso(raw: str | None) -> dt.datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _run_dirs(dates: list[str]):
    """`run-*` directories under each of `dates`, missing dates skipped quietly.

    `_MAX_RUN_DIRS_SCANNED` bounds how many are YIELDED, not how many are
    listed: `glob` plus `sorted` materializes a whole date directory before the
    cap can engage. That is fine against the ~220 runs a day this runs on, and
    it is worth knowing before pointing this at a directory that could actually
    grow without limit, since the cap would not save it."""
    scanned = 0
    for date in dates:
        date_dir = AGENT_RUNS_ROOT / date
        try:
            entries = sorted(date_dir.glob("run-*"))
        except OSError:
            continue
        for entry in entries:
            if scanned >= _MAX_RUN_DIRS_SCANNED:
                return
            scanned += 1
            yield entry


def _pid_alive(pid: object) -> bool:
    """Whether `pid` names a live process. `os.kill(pid, 0)` sends no signal,
    it only asks the kernel. `PermissionError` means the process exists under
    another user, which is still alive — treating it as dead is the classic
    misreading of this call.

    Subject to pid reuse: a long-dead run whose pid has been recycled reads as
    alive. That is acceptable for a diagnostic line and is bounded by the
    timestamp test this runs behind."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001 — anything unexpected reads as not-alive
        return False
    return True


def live_run_counts(now: dt.datetime | None = None) -> tuple[int, int]:
    """`(live, stale)` at the instant `now` (default: this instant, UTC) —
    runs live AT THAT INSTANT, not runs merely started nearby.

    A run is a candidate when `started_at <= now` and `ended_at` is absent or
    `> now`. Measured for the 2026-09-15 stall, a started-in-the-surrounding-
    half-hour count overstated true peak concurrency roughly threefold (22 vs
    the ~8 actually live), because a run that starts a minute after the
    instant in question was never running at that instant at all.

    **A candidate counts as `live` only when its recorded pid is a live
    process; the rest are `stale`.** The first version of this counted how
    many candidates lacked an `ended_at` instead, which is a tautology when
    sampling the present: a run that is genuinely running has not ended, so
    every live run lacks the field by construction and the number carried no
    information. Measured here on 2026-09-16 it read `live=4 live_unended=4`,
    with all four genuinely running. The unended-tail concern is real, but it
    belongs to post-hoc analysis over old directories, which is where it was
    first observed — not to this call site.

    `stale` is therefore the honest version of that worry: a candidate whose
    process is gone, or which recorded no pid at all, so its `ended_at` was
    never written. It is a data-quality tail on the same scan, and a `live`
    that excludes it is the concurrency figure a reader actually wants.

    Scans today's and yesterday's date directories (by UTC date), since a run
    started before midnight can still be live after it. A directory that
    doesn't exist, a `meta.json` that's missing, malformed, or unreadable, and
    an unparsable timestamp are all skipped rather than raised — this
    function is called from a failure path and must not add a failure of its
    own."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    # TWO DAYS, AND `stale` DEPENDS ON THAT — widen this window and the field
    # stops meaning what it says. `stale` merges "the process is gone" with
    # "no pid was recorded", which is safe only while every candidate comes
    # from an era that records one. Measured by the Library crew across the
    # whole agent-runs tree on 2026-09-16: of 40,318 records, 25,488 carry
    # `ended_at`, 65 lack it but carry a pid, and 14,765 lack both. Those
    # 14,765 are uninstrumented history, not dead runs, and reaching back far
    # enough to include them would swamp `stale` with a large constant that
    # reads like abandoned work.
    dates = [now.date().isoformat(), (now - dt.timedelta(days=1)).date().isoformat()]

    live = 0
    stale = 0
    for run_dir in _run_dirs(dates):
        try:
            meta = json.loads((run_dir / "meta.json").read_text())
        except Exception:  # noqa: BLE001, S112 — a missing/malformed meta.json is skipped, not raised
            continue
        started = _parse_iso(meta.get("started_at"))
        if started is None or started > now:
            continue
        ended = _parse_iso(meta.get("ended_at"))
        if ended is not None and ended <= now:
            continue
        if _pid_alive(meta.get("pid")):
            live += 1
        else:
            stale += 1
    return live, stale


def classify_direction(*, method: str | None, json_body: dict | None) -> str:
    """`"read"` for a GET with no body, `"write"` for anything else —
    including a DELETE that carries a JSON body, which is a write even though
    its HTTP verb doesn't create anything."""
    effective = (method or ("POST" if json_body is not None else "GET")).upper()
    return "read" if effective == "GET" else "write"


def sample_machine_state(*, operation: str, direction: str,
                          now: dt.datetime | None = None) -> str:
    """One tab-delimited line describing machine state at a Library-call
    timeout: timestamp, operation, direction, swap used, free pages, the
    live-run count, and the stale tail beside it.

    `operation` is caller-influenced (a request path built from a caller-
    supplied id) and goes through `clean_log_field`, the same rule
    `server.py._log_field` applies to every other caller-influenced field in
    this codebase — an embedded tab or newline could otherwise forge a new
    log line or shift this line's columns.

    Each field is sourced independently and degrades on its own; a failure in
    one does not blank the others. This function does not write anywhere —
    `record_timeout` does that and is what callers outside tests should use.
    """
    ts = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        swap = _swap_used()
    except Exception:  # noqa: BLE001 — one field's bug must not blank the others
        swap = "unavailable"
    try:
        free = _free_pages()
    except Exception:  # noqa: BLE001 — one field's bug must not blank the others
        free = "unavailable"
    try:
        live, stale = live_run_counts(now)
    except Exception:  # noqa: BLE001 — one field's bug must not blank the others
        live, stale = "unavailable", "unavailable"
    fields = [
        ts, "LIBRARY-TIMEOUT", clean_log_field(operation), direction,
        f"swap_used={swap}", f"free_pages={free}",
        f"live={live}", f"stale={stale}",
    ]
    return "\t".join(str(f) for f in fields)


def record_timeout(*, operation: str, direction: str) -> None:
    """Write one `sample_machine_state` line to stderr (`serve.log` under the
    launchd plist). Call this from a Library timeout handler only — see the
    module docstring.

    **Never raises.** A bug here must not cost the caller the `TimeoutError`
    it was already handling; that would replace a real, actionable failure
    with a worse and unrelated one."""
    try:
        print(sample_machine_state(operation=operation, direction=direction),
              file=sys.stderr)
    except Exception:  # noqa: BLE001, S110 — a logging bug must not surface as an error
        pass
