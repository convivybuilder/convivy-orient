"""Unit tests for machine_sample.py — the best-effort machine-state sample
`library_client` writes on a Library-call timeout
(issue_01M2KW4H2CTXXXM2JKWB6NZG09).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess

import pytest

import machine_sample

# --- subprocess-sourced fields degrade independently ------------------------


def test_swap_used_parses_sysctl_output(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="vm.swapusage: total = 2048.00M  used = 898.38M  "
                            "free = 1149.62M  (encrypted)\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert machine_sample._swap_used() == "898.38M"


def test_swap_used_is_unavailable_when_the_subprocess_fails(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("no sysctl")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert machine_sample._swap_used() == "unavailable"


def test_swap_used_is_unavailable_when_the_subprocess_times_out(monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert machine_sample._swap_used() == "unavailable"


def test_swap_used_is_unavailable_on_unparsable_output(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="garbage\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert machine_sample._swap_used() == "unavailable"


def test_free_pages_parses_vm_stat_output(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Mach Virtual Memory Statistics: (page size of "
                            "16384 bytes)\nPages free:  4523.\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert machine_sample._free_pages() == "4523"


def test_free_pages_is_unavailable_when_the_subprocess_fails(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("no vm_stat")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert machine_sample._free_pages() == "unavailable"


# --- read/write classification -----------------------------------------------


def test_classify_direction_a_plain_get_is_a_read():
    assert machine_sample.classify_direction(method=None, json_body=None) == "read"


def test_classify_direction_a_json_body_is_a_write():
    assert machine_sample.classify_direction(method=None, json_body={"a": 1}) == "write"


def test_classify_direction_an_explicit_post_is_a_write():
    assert machine_sample.classify_direction(method="POST", json_body=None) == "write"


def test_classify_direction_an_explicit_delete_is_a_write():
    assert machine_sample.classify_direction(method="DELETE", json_body=None) == "write"


def test_classify_direction_a_delete_with_a_body_is_a_write():
    """The one shape neither a plain GET nor a plain POST covers: the ack-
    withdrawal route sends DELETE with a JSON body."""
    assert machine_sample.classify_direction(
        method="DELETE", json_body={"acting_human": "operator@example.com"}) == "write"


def test_classify_direction_an_explicit_get_is_still_a_read():
    assert machine_sample.classify_direction(method="GET", json_body=None) == "read"


# --- pid liveness ------------------------------------------------------------


def test_pid_alive_is_true_for_this_very_process():
    assert machine_sample._pid_alive(os.getpid()) is True


@pytest.mark.parametrize("bogus", [None, 0, -1, "1234", 1.5, True])
def test_pid_alive_is_false_for_anything_that_is_not_a_positive_int(bogus):
    """`True` is an `int` in Python and would otherwise be probed as pid 1."""
    assert machine_sample._pid_alive(bogus) is False


def test_pid_alive_is_false_when_the_process_is_gone(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(machine_sample.os, "kill", gone)
    assert machine_sample._pid_alive(4242) is False


def test_pid_alive_is_true_when_the_process_belongs_to_another_user(monkeypatch):
    """A PermissionError means the process EXISTS but is not ours to signal.
    Reading that as dead is the classic misuse of `kill(pid, 0)`."""
    def not_ours(pid, sig):
        raise PermissionError

    monkeypatch.setattr(machine_sample.os, "kill", not_ours)
    assert machine_sample._pid_alive(4242) is True


# --- live-run counting: point-in-time, and pid-verified ----------------------


_ALIVE_PID = 1234
_DEAD_PID = 5678


def _write_run(root, date, run_id, *, started_at, ended_at=None, pid=_ALIVE_PID):
    run_dir = root / date / run_id
    run_dir.mkdir(parents=True)
    meta = {"run_id": run_id, "started_at": started_at,
            "ended_at": ended_at, "pid": pid}
    (run_dir / "meta.json").write_text(json.dumps(meta))


T = dt.datetime(2026, 9, 16, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def fake_pids(monkeypatch):
    """Liveness is decided by pid VALUE here, so the counting logic is tested
    without depending on what is really running on this machine.
    `_pid_alive` itself is covered against real pids above."""
    monkeypatch.setattr(machine_sample, "_pid_alive",
                        lambda pid: pid == _ALIVE_PID)


def test_live_run_counts_point_in_time(tmp_path, monkeypatch, fake_pids):
    monkeypatch.setattr(machine_sample, "AGENT_RUNS_ROOT", tmp_path)

    # Live at T: started before T, still running, process alive.
    _write_run(tmp_path, "2026-09-16", "run-live",
              started_at="2026-09-16T11:30:00Z", ended_at=None)
    # Ended before T: not live.
    _write_run(tmp_path, "2026-09-16", "run-ended",
              started_at="2026-09-16T10:00:00Z", ended_at="2026-09-16T11:00:00Z")
    # Started after T: not live at T even though it exists in the same directory.
    _write_run(tmp_path, "2026-09-16", "run-future",
              started_at="2026-09-16T12:30:00Z", ended_at=None)
    # Spans midnight from yesterday's directory: started yesterday, still live.
    _write_run(tmp_path, "2026-09-15", "run-overnight",
              started_at="2026-09-15T23:00:00Z", ended_at=None)

    live, stale = machine_sample.live_run_counts(T)

    assert live == 2  # run-live, run-overnight
    assert stale == 0


def test_a_candidate_whose_process_is_gone_is_stale_not_live(
        tmp_path, monkeypatch, fake_pids):
    """The case `live_unended` was meant to catch and could not: by the clock
    this run is still going, but nothing is running. Counting it as live is
    what would overstate concurrency on a machine that had shed work."""
    monkeypatch.setattr(machine_sample, "AGENT_RUNS_ROOT", tmp_path)
    _write_run(tmp_path, "2026-09-16", "run-alive",
              started_at="2026-09-16T11:00:00Z", pid=_ALIVE_PID)
    _write_run(tmp_path, "2026-09-16", "run-abandoned",
              started_at="2026-09-16T09:00:00Z", pid=_DEAD_PID)

    assert machine_sample.live_run_counts(T) == (1, 1)


def test_a_candidate_with_no_recorded_pid_is_stale(tmp_path, monkeypatch, fake_pids):
    """No pid means liveness cannot be confirmed, so it does not inflate the
    figure a reader trusts."""
    monkeypatch.setattr(machine_sample, "AGENT_RUNS_ROOT", tmp_path)
    _write_run(tmp_path, "2026-09-16", "run-no-pid",
              started_at="2026-09-16T11:00:00Z", pid=None)

    assert machine_sample.live_run_counts(T) == (0, 1)


def test_live_run_counts_does_not_overstate_like_a_started_in_window_count(
        tmp_path, monkeypatch, fake_pids):
    """The item's own worked example: a started-in-the-surrounding-window
    count overstates true concurrency, because a run that starts after the
    instant in question was never running at that instant."""
    monkeypatch.setattr(machine_sample, "AGENT_RUNS_ROOT", tmp_path)
    _write_run(tmp_path, "2026-09-16", "run-a", started_at="2026-09-16T11:59:00Z")
    _write_run(tmp_path, "2026-09-16", "run-b", started_at="2026-09-16T12:00:01Z")

    live, _ = machine_sample.live_run_counts(T)
    assert live == 1  # only run-a; run-b starts one second after T


def test_live_run_counts_skips_a_missing_or_malformed_meta(
        tmp_path, monkeypatch, fake_pids):
    monkeypatch.setattr(machine_sample, "AGENT_RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "2026-09-16" / "run-broken"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text("not json")

    other_dir = tmp_path / "2026-09-16" / "run-no-meta"
    other_dir.mkdir(parents=True)

    assert machine_sample.live_run_counts(T) == (0, 0)


def test_live_run_counts_handles_no_date_directory_at_all(
        tmp_path, monkeypatch, fake_pids):
    monkeypatch.setattr(machine_sample, "AGENT_RUNS_ROOT", tmp_path)
    assert machine_sample.live_run_counts(T) == (0, 0)


# --- the assembled line ------------------------------------------------------


def test_sample_machine_state_shape(monkeypatch):
    monkeypatch.setattr(machine_sample, "_swap_used", lambda: "898.38M")
    monkeypatch.setattr(machine_sample, "_free_pages", lambda: "4523")
    monkeypatch.setattr(machine_sample, "live_run_counts", lambda now=None: (8, 1))

    line = machine_sample.sample_machine_state(operation="/items", direction="read", now=T)
    fields = line.split("\t")

    assert fields[0] == "2026-09-16T12:00:00Z"
    assert fields[1] == "LIBRARY-TIMEOUT"
    assert fields[2] == "/items"
    assert fields[3] == "read"
    assert "swap_used=898.38M" in fields
    assert "free_pages=4523" in fields
    assert "live=8" in fields
    assert "stale=1" in fields


def test_sample_machine_state_cleans_a_caller_influenced_operation(monkeypatch):
    """`operation` is built from a caller-supplied id upstream (a request
    path); an embedded tab or newline must not forge a new log line or shift
    this line's columns — the same rule `server.py._log_field` applies."""
    monkeypatch.setattr(machine_sample, "_swap_used", lambda: "unavailable")
    monkeypatch.setattr(machine_sample, "_free_pages", lambda: "unavailable")
    monkeypatch.setattr(machine_sample, "live_run_counts", lambda now=None: (0, 0))

    line = machine_sample.sample_machine_state(
        operation="/items/evil\tid\nwith-control-chars", direction="read", now=T)

    assert "\t" not in line.split("\t")[2]
    assert line.count("\n") == 0
    assert len(line.split("\t")) == 8


def test_sample_machine_state_survives_live_run_counts_raising(monkeypatch):
    monkeypatch.setattr(machine_sample, "_swap_used", lambda: "898.38M")
    monkeypatch.setattr(machine_sample, "_free_pages", lambda: "4523")

    def boom(now=None):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(machine_sample, "live_run_counts", boom)

    line = machine_sample.sample_machine_state(operation="/items", direction="read", now=T)
    assert "live=unavailable" in line
    assert "stale=unavailable" in line


# --- record_timeout: never raises, writes exactly one line ------------------


def test_record_timeout_writes_one_line_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(machine_sample, "_swap_used", lambda: "898.38M")
    monkeypatch.setattr(machine_sample, "_free_pages", lambda: "4523")
    monkeypatch.setattr(machine_sample, "live_run_counts", lambda now=None: (8, 1))

    machine_sample.record_timeout(operation="/items", direction="read")

    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line]
    assert len(lines) == 1
    assert "LIBRARY-TIMEOUT" in lines[0]
    assert "/items" in lines[0]


def test_record_timeout_never_raises_even_if_sample_machine_state_blows_up(monkeypatch):
    def boom(**kw):
        raise RuntimeError("sampler bug")

    monkeypatch.setattr(machine_sample, "sample_machine_state", boom)
    machine_sample.record_timeout(operation="/items", direction="read")  # must not raise
