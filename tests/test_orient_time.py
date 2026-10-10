"""Tests for orient_time — Convivy's Mountain STANDARD Time (fixed UTC-7,
America/Phoenix) formatting for dashboard timestamps."""
from datetime import datetime, timezone

import orient_time as ot


def test_now_label_shape():
    s = ot.now_label()
    assert s.endswith(" MST")
    # "YYYY-MM-DD HH:MM MST"
    assert len(s) == len("2026-06-15 22:31 MST")


def test_fixed_utc_minus_7_year_round_not_denver():
    # The whole point: America/Phoenix is UTC-7 in BOTH winter and summer (no DST).
    # 12:00 UTC -> 05:00 MST in July; America/Denver would be 06:00 MDT.
    winter = ot.fmt("2026-01-15T12:00:00+00:00")
    summer = ot.fmt("2026-07-15T12:00:00+00:00")
    assert winter == "2026-01-15 05:00 MST"
    assert summer == "2026-07-15 05:00 MST"  # NOT 06:00 — no daylight shift


def test_naive_is_treated_as_local_mst():
    # agent-cost-report writes generated_at as naive local time on the Phoenix host.
    assert ot.fmt("2026-06-15T20:00:00") == "2026-06-15 20:00 MST"
    assert ot.fmt("2026-06-15T20:00") == "2026-06-15 20:00 MST"


def test_assume_utc_converts_naive():
    # the kb-eval "generated" field is written in UTC (was labeled "UTC").
    assert ot.fmt("2026-06-15T20:00:00", assume_utc=True) == "2026-06-15 13:00 MST"


def test_aware_input_converted_and_z_suffix():
    assert ot.fmt("2026-06-15T20:00:00Z") == "2026-06-15 13:00 MST"
    dt = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)
    assert ot.fmt(dt) == "2026-06-15 13:00 MST"


def test_date_only_passthrough_no_label():
    assert ot.fmt("2026-06-15") == "2026-06-15"


def test_empty_and_unparseable_passthrough():
    assert ot.fmt("") == ""
    assert ot.fmt(None) == ""
    assert ot.fmt("not a date") == "not a date"


def test_with_label_false():
    assert ot.fmt("2026-06-15T20:00:00", with_label=False) == "2026-06-15 20:00"


# --- to_mst: the conversion without fmt's fixed layout -------------------------
#
# `fmt` renders exactly one layout, so every surface wanting a different one —
# "Jul 18", "Jul 18, 2026" — had nowhere to go and formatted the raw value
# itself, rendering UTC. Five builders drifted that way before `to_mst` existed.


def test_to_mst_converts_aware_utc():
    got = ot.to_mst("2026-07-15T12:00:00+00:00")
    assert got.strftime("%b %-d %H:%M") == "Jul 15 05:00"
    assert got.tzinfo is not None


def test_to_mst_is_fixed_utc_minus_7_in_both_seasons():
    """The Phoenix-not-Denver invariant, restated for to_mst: a July timestamp
    must convert at UTC-7, not the UTC-6 that America/Denver would give."""
    assert ot.to_mst("2026-01-15T12:00:00+00:00").hour == 5
    assert ot.to_mst("2026-07-15T12:00:00+00:00").hour == 5


def test_to_mst_late_utc_is_the_previous_mst_day():
    """The bug this was written for. A PR merged at 03:00 UTC on Jul 28 happened
    at 20:00 MST on Jul 27, and the merge-queue page showed the UTC day — so
    everything merged after 17:00 MST, which is most of them, was dated tomorrow."""
    got = ot.to_mst("2026-07-28T03:00:00Z")
    assert got.strftime("%b %-d") == "Jul 27"


def test_to_mst_naive_defaults_to_mst_and_assume_utc_overrides():
    """The subtle half. The control DB stores NAIVE UTC (models.utcnow strips
    tzinfo), so a caller reading a model column must pass assume_utc=True.
    Without it the value reads as MST already — the same seven-hour error in the
    other direction, and harder to spot because the call site looks correct."""
    naive = datetime(2026, 7, 28, 3, 0, 0)
    assert ot.to_mst(naive).hour == 3                      # taken as MST as-is
    assert ot.to_mst(naive, assume_utc=True).hour == 20    # 03:00 UTC -> 20:00 MST
    assert ot.to_mst(naive, assume_utc=True).day == 27


def test_to_mst_returns_none_for_unusable_input():
    """A caller with a custom format has to pick its own placeholder, so these
    return None rather than a string fmt would have passed through."""
    assert ot.to_mst("") is None
    assert ot.to_mst(None) is None
    assert ot.to_mst("2026-07-15") is None      # date-only: no clock time to convert
    assert ot.to_mst("not a timestamp") is None


def test_to_mst_agrees_with_fmt():
    """to_mst must not become a second, subtly different conversion. Whatever fmt
    renders, to_mst + the same layout must match."""
    for iso in ("2026-01-15T12:00:00+00:00", "2026-07-28T03:00:00Z"):
        assert ot.to_mst(iso).strftime("%Y-%m-%d %H:%M") == ot.fmt(iso, with_label=False)


# --- the drift guard ----------------------------------------------------------


def test_no_module_picks_america_denver():
    """America/Denver is UTC-6 in summer while Convivy's zone is a fixed UTC-7,
    so the two disagree by an hour for half the year while both calling
    themselves "Mountain". Two builders had picked Denver in inline JS — invisible
    to any Python-level check and wrong every summer.

    This guard is the point of the whole fix: correcting five call sites is
    worthless if the sixth surface picks its own zone again. orient_time.py is
    exempt because its docstring names Denver to explain what we are NOT using.

    Matched as a QUOTED STRING, because that is how a zone is used as a value.
    Prose mentioning Denver to explain why we avoid it is exactly what the
    comments at the corrected call sites do, and flagging those would make the
    guard punish the documentation that prevents the mistake.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    as_a_value = re.compile(r"""['"]America/Denver['"]""")
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name == "orient_time.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if as_a_value.search(text):
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} use America/Denver; Convivy is America/Phoenix (fixed UTC-7). "
        "Use orient_time.MST in Python, or the literal 'America/Phoenix' in inline JS."
    )
