#!/usr/bin/env python3.11
"""Dashboard alert-threshold policy — Orient's own store
(issue_01M17W6XGXVHNDWCFRTQ38Y2RK; see `models.AlertPolicy`).

**THE ONE STORE BOTH SIDES OF THE WRITE READ.** `alerts_policy_api.py`'s write
route calls `write()`; `code_health_build.ci_status`, `dashboard_build.
overage_status` and `kb_build.cls_recall` — plus alerts_build.py's /alerts
feed, which calls the same three functions — call `get()`. There is no second
copy of a threshold anywhere in this repo; a value written here is the value
every evaluator reads on its very next call, with no cache and no restart
(matching `library_client.resolve_policy`'s own no-cache rule, for the same
reason: a stale threshold here is exactly the "control that appears to work
and silently governs nothing" failure this work exists to avoid).

**DEFAULTS ARE THE OLD HARDCODED NUMBERS, UNCHANGED.** A key nobody has ever
written behaves exactly as main did before this store existed — `get()` on a
missing row returns `DEFAULTS[key]` verbatim, not a re-derived approximation.

Only three keys exist, matching the three thresholds
`issue_01M17W6XGXVHNDWCFRTQ38Y2RK` scoped the first pass to. Security and
Warrant carry no key: both pages' own coloring is a soft read with no policy
threshold yet, so there is nothing here for them to override.
"""
from __future__ import annotations

import math

import control_db
from models import AlertPolicy, utcnow

# key -> the threshold fields it carries, and the exact values that were
# hardcoded in code_health_build.ci_status / dashboard_build.overage_status /
# kb_build.cls_recall before this store existed. FIELDS is the one definition
# of what a threshold needs — a write route validates against it rather than
# inventing its own field list, and a display form's inputs are exactly this
# set.
DEFAULTS: dict[str, dict[str, float]] = {
    "code_health.ci_pass_rate": {"good_min": 0.9, "warn_min": 0.7},
    # A run count, not a rate: `overage_status` was a bare "any run in
    # overage is bad" before this store existed, which is the same rule as
    # max_runs_in_overage=0 — the default below reproduces that exactly.
    "spending.overage_runs": {"max_runs_in_overage": 0},
    "kb.recall": {"good_min": 0.85, "warn_min": 0.6},
}

FIELDS: dict[str, tuple[str, ...]] = {
    key: tuple(sorted(fields)) for key, fields in DEFAULTS.items()
}


def get(key: str) -> dict[str, float]:
    """The threshold in force for `key` — the stored override if one exists,
    else the hardcoded default. Read at call time, no cache: the same
    invariant `library_client.resolve_policy` holds for the deny-list, and for
    the same reason."""
    if key not in DEFAULTS:
        raise ValueError(f"unknown alert-policy key: {key!r}")
    with control_db.session() as s:
        row = s.get(AlertPolicy, key)
        if row is not None:
            return dict(row.values)
    return dict(DEFAULTS[key])


def validate(key: str, values: dict) -> str | None:
    """An error message, or None if `values` is a usable threshold for `key`.
    Checked here — the one place FIELDS and the value rules are defined —
    rather than re-derived in the write route."""
    if key not in DEFAULTS:
        return f"unknown alert-policy key: {key!r}"
    fields = FIELDS[key]
    extra = set(values) - set(fields)
    if extra:
        return f"unexpected field(s): {sorted(extra)}"
    missing = set(fields) - set(values)
    if missing:
        return f"missing field(s): {sorted(missing)}"
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name} must be a number"
    if "good_min" in values and "warn_min" in values:
        good, warn = values["good_min"], values["warn_min"]
        if not (0 <= warn <= good <= 1):
            return "warn_min must be between 0 and good_min, and good_min at most 1"
        # A rate is never negative, so good_min == 0 makes every input >=
        # good_min — the evaluator returns "good" unconditionally and the
        # alert category is silently disabled rather than merely lenient.
        if good <= 0:
            return "good_min must be greater than 0 — 0 makes every rate register as good"
    if "max_runs_in_overage" in values:
        n = values["max_runs_in_overage"]
        if not math.isfinite(n):
            return "max_runs_in_overage must be a finite number"
        if n < 0 or n != int(n):
            return "max_runs_in_overage must be a non-negative whole number"
    return None


def write(key: str, values: dict[str, float], actor: str) -> dict[str, float]:
    """Upsert the threshold for `key`, attested to `actor` — the server's
    resolved human identity, never a request-body claim
    (`alerts_policy_api.py`'s own rule). Raises ValueError on an invalid
    key or shape; callers validate() first to turn that into a clean 400
    rather than a 500."""
    why = validate(key, values)
    if why:
        raise ValueError(why)
    with control_db.session() as s:
        row = s.get(AlertPolicy, key)
        if row is None:
            row = AlertPolicy(key=key, values=dict(values), updated_by=actor)
            s.add(row)
        else:
            row.values = dict(values)
            row.updated_by = actor
            row.updated_at = utcnow()
        s.flush()
        s.expunge(row)
        return dict(row.values)
