"""Mountain Standard Time formatting for Convivy internal surfaces.

Convivy standardizes every internal operational timestamp on **Mountain STANDARD
Time** — `America/Phoenix`, a fixed **UTC-7** year-round because Arizona does not
observe daylight saving. This is deliberately NOT `America/Denver` ("Mountain
Time" / "Mountain Prevailing Time"), which shifts to UTC-6 in summer. Every
rendered time carries an explicit `MST` label so the zone is never ambiguous, and
the offset never drifts with DST or the host machine's locale.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MST = ZoneInfo("America/Phoenix")  # fixed UTC-7, no DST
LABEL = "MST"


def now() -> datetime:
    """Current instant as an MST-aware datetime."""
    return datetime.now(timezone.utc).astimezone(MST)


def now_label() -> str:
    """Current time formatted for a page header, e.g. ``2026-06-15 22:31 MST``."""
    return now().strftime(f"%Y-%m-%d %H:%M {LABEL}")


def to_mst(value, *, assume_utc: bool = False) -> datetime | None:
    """A datetime or ISO-8601 string → an MST-aware datetime, or None.

    This is the conversion `fmt` does, without `fmt`'s fixed output format.
    It exists because `fmt` renders exactly one layout (``%Y-%m-%d %H:%M MST``),
    so every surface wanting a different one — "Jul 18", "Jul 18, 2026" — had
    nowhere to go and formatted the raw value itself, silently rendering UTC.
    That is how five builders drifted off the house zone. Reach for this
    whenever you need a custom strftime; never call strftime on a value the
    store handed you.

    `assume_utc` matters more here than the parameter name suggests. The control
    DB stores NAIVE UTC by convention (models.utcnow strips the tzinfo because
    SQLite has no native timezone), so anything read from a model column needs
    `assume_utc=True`. Naive input without it is read as MST already, which is
    the same seven-hour error in the opposite direction — and harder to spot,
    because the call site looks like it did the right thing.

    Returns None for an empty, date-only, or unparseable value: a caller with a
    custom format needs to decide its own placeholder, and a date-only string
    has no clock time to convert.
    """
    if not value:
        return None
    if isinstance(value, str):
        s = value.strip()
        if len(s) == 10 and s[4] == "-" and s[7] == "-":  # date-only, no time
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if assume_utc else MST)
    return dt.astimezone(MST)


def hours_since(value, *, assume_utc: bool = False) -> float | None:
    """Hours between now and `value` (an ISO timestamp or datetime), or None
    if it can't be parsed into a clock time — a date-only string has no age
    to compute, so a caller judging staleness skips the check rather than
    guessing at an age. Shared by every dashboard's own freshness judgment
    (design_system.Freshness) so "how stale is this" is computed the same
    way everywhere it's asked.
    """
    parsed = to_mst(value, assume_utc=assume_utc)
    if parsed is None:
        return None
    return (now() - parsed).total_seconds() / 3600


def fmt(value, *, assume_utc: bool = False, with_label: bool = True) -> str:
    """Format a datetime or ISO-8601 string in MST with an ``MST`` label.

    - tz-aware input is converted to MST.
    - naive input is assumed to be MST already (the producers run on the
      Phoenix host) unless ``assume_utc=True`` (for sources that write UTC).
    - a date-only string (``YYYY-MM-DD``) is returned unchanged — a calendar day
      has no clock time to relabel.
    - an empty or unparseable value is returned unchanged, so a render never
      breaks on a malformed timestamp.
    """
    if not value:
        return ""
    if isinstance(value, str):
        s = value.strip()
        if len(s) == 10 and s[4] == "-" and s[7] == "-":  # date-only, no time
            return s
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if assume_utc else MST)
    out = dt.astimezone(MST).strftime("%Y-%m-%d %H:%M")
    return f"{out} {LABEL}" if with_label else out
