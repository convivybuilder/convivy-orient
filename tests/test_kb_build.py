"""Tests for kb_build.py — the KB retrieval dashboard's data layer (ARCH-121).

The IO (running kb-eval, reading the logs) is impure and excluded; these pin the
aggregation, where a wrong null-result rate, latency percentile, or
latest-per-profile pick would mislead the dashboard.

The one piece of IO pinned here is `refresh_eval`'s tool resolution
(issue_01M1DKJYHSH9FT1SBP9068MTT1): `KB_EVAL_BIN` used to be a literal
`HOME / ".local" / "bin" / "kb-eval"` — the DEVELOPER symlink farm into the
agent-platform working checkout, reachable from this deploy-pinned launchd
job. It must go through `paths.resolve_tool` instead, so a future edit that
reintroduces the literal fails.
"""
import datetime as dt
import os
import subprocess
import urllib.error

import kb_build as kb
import library_client


def test_pctile():
    assert kb._pctile([], 0.5) is None
    assert kb._pctile([5.0], 0.95) == 5.0
    assert kb._pctile([1, 2, 3, 4], 0.5) == 2.5          # median interpolated
    assert kb._pctile([1, 2, 3, 4], 1.0) == 4


def test_query_stats_counts_and_rates():
    rows = [
        {"tool": "kb_search", "empty": False, "hit_limit": True, "latency_ms": 2.0, "run_id": "r1", "ts": "2026-06-08T01:00:00Z"},
        {"tool": "kb_search", "empty": True, "hit_limit": False, "latency_ms": 4.0, "query": "nope", "ts": "2026-06-08T02:00:00Z"},
        {"tool": "kb_get", "latency_ms": 1.0, "ts": "2026-06-08T03:00:00Z"},
        {"tool": "kb_list_topics", "latency_ms": 6.0, "ts": "2026-06-08T04:00:00Z"},
    ]
    s = kb.query_stats(rows)
    assert s["total"] == 4 and s["searches"] == 2 and s["gets"] == 1 and s["topics"] == 1
    assert s["empty_rate"] == 0.5 and s["hitlimit_rate"] == 0.5
    assert s["orchestrated"] == 1 and s["interactive"] == 3   # only r1 has a run_id
    assert s["top_nulls"] == [("nope", 1)]
    assert s["first"] == "2026-06-08T01:00:00Z" and s["last"] == "2026-06-08T04:00:00Z"


def test_query_stats_empty():
    s = kb.query_stats([])
    assert s["total"] == 0 and s["empty_rate"] is None and s["p50"] is None and s["top_nulls"] == []


def test_mat_stats_latest_per_profile_wins():
    rows = [
        {"project": "vivy", "profile": "daytime", "agents": 5, "skills": 8, "total_docs": 30, "outcome": "written", "ts": "2026-06-08T01:00:00Z"},
        {"project": "vivy", "profile": "daytime", "agents": 6, "skills": 9, "total_docs": 33, "outcome": "noop", "ts": "2026-06-08T05:00:00Z"},
    ]
    m = kb.mat_stats(rows)
    assert m["total"] == 2 and m["by_outcome"] == {"written": 1, "noop": 1}
    assert len(m["profiles"]) == 1
    p = m["profiles"][0]
    assert p["agents"] == 6 and p["skills"] == 9 and p["individuated"] == 15  # newest ts wins
    assert p["live_only"] == 33 - 15


def _sample_ev():
    return {"overall": {"recall@1": 0.8, "recall@5": 0.9, "recall@10": 0.95, "mrr": 0.85},
            "k": 10, "cases": 20, "generated": "2026-06-10T00:00:00Z",
            "by_style": {"keyword": {"n": 10, "recall@1": 0.8, "recall@5": 0.9,
                                     "recall@10": 0.95, "mrr": 0.85}},
            "by_kind": {"adr": {"n": 5, "recall@10": 1.0, "mrr": 0.9}},
            "misses": [{"style": "keyword", "query": "x", "expect": ["d"]}]}


def _sample_q():
    return kb.query_stats([{"tool": "kb_search", "query": "x", "empty": True,
                            "latency_ms": 5, "ts": "2026-06-09T00:00:00Z"}])


def _sample_m():
    return kb.mat_stats([{"project": "vivy", "profile": "daytime", "outcome": "ok",
                          "agents": 3, "skills": 2, "total_docs": 9, "ts": "t1"}])


def test_render_has_metric_tooltips():
    html = kb.render(_sample_ev(), _sample_q(), _sample_m())
    # every panel heading, card, and table header carries hover copy
    for needle in ('data-tip="', "kb-golden.jsonl", "kb-queries.jsonl",
                   "kb-materializations.jsonl", "Mean reciprocal rank",
                   "zero hits", "result cap", ".claude/agents/"):
        assert needle in html, needle
    # the tooltip CSS ships inside the page (stays self-contained)
    assert ".tip::after" in html and "attr(data-tip)" in html
    # the dynamic recall@k tip carries the eval's actual k
    assert "top 10 — the eval cutoff" in html


def test_nav_is_styled():
    """After migrating to ds.nav_component, the old topnav class is gone.
    The Atelier nav renders class="atelier-nav" with aria-current="page" on
    the Dashboards primary, and ds tokens (e.g. --brand) replace the old
    per-page --grad literal. KB Retrieval left the Dashboards sub-strip in
    the 2026-08-24 trim (the operator's pinned five) — it keeps its route and
    full-tier gating, reachable through the grouped Overview index
    (nav-reshape spec §4), so it has no sub-strip link to assert on here."""
    html = kb.render(_sample_ev(), _sample_q(), _sample_m())
    assert 'class="atelier-nav"' in html          # ds nav rendered
    assert 'href="/overview" aria-current="page"' in html  # Dashboards primary lit
    assert ">KB Retrieval<" not in html            # trimmed from the sub-strip
    assert "--brand" in html                       # ds token present
    assert "topnav" not in html                    # old nav class absent


def test_panel1_recall_cards_have_status_cue():
    """Recall StatCards emit a non-color qualifier (.statcard-delta) so the
    good/warn status is not carried by color alone (WCAG 1.4.1)."""
    html = kb.render(_sample_ev(), _sample_q(), _sample_m())
    # at least one status cue phrase must appear alongside the color class
    assert "on target" in html or "below target" in html


def test_kb_tabs_have_aria_controls():
    """Each KB area tab must carry aria-controls="kbNavlist" to complete the
    tab → panel relationship required by WCAG 4.1.2 and ARIA authoring practice."""
    import build
    assert 'aria-controls="kbNavlist"' in build.HTML_TEMPLATE
    # All three tabs (company, products, engineering) must carry the attribute.
    assert build.HTML_TEMPLATE.count('aria-controls="kbNavlist"') >= 3


def test_orientation_library_sanitizes_doc_body_before_innerhtml():
    """ARCH-244: the Orientation doc library renders doc bodies via marked →
    innerHTML; the output must be DOMPurify-sanitized and the library inlined,
    so a <script> in a doc body can't execute for a full-tier viewer."""
    import build
    doc = {"project": "shared", "kind": "reference", "status": "current",
           "title": "Sample", "body": "## Sample\n\nbody\n"}
    html = build.render([doc], tier="full")
    assert "Cure53" in html, "DOMPurify library not inlined into the orientation bundle"
    assert "DOMPurify.sanitize(marked.parse(d.body))" in html
    assert html.count("marked.parse(d.body)") == html.count("DOMPurify.sanitize(marked.parse(d.body))")


def test_orientation_library_escapes_meta_fields_in_innerhtml():
    """ARCH-244: the scalar frontmatter fields (project/kind/status) the doc
    viewer interpolates into innerHTML are esc()'d — they don't pass through
    DOMPurify like the body does, so they need their own escape."""
    import build
    html = build.render([{"project": "shared", "kind": "reference",
                          "status": "current", "title": "Sample", "body": "x"}], tier="full")
    assert "function esc(" in html, "no esc() helper in the orientation bundle"
    assert "esc(d.kind)" in html
    assert "esc(d.status)" in html
    assert "esc(DATA.projectLabel[d.project] || d.project)" in html
    # the bare (unescaped) interpolations are gone
    assert "${d.kind}" not in html
    assert "${d.status}" not in html


def test_cls_recall_thresholds():
    assert kb.cls_recall(0.9) == "good"
    assert kb.cls_recall(0.85) == "good"    # boundary is inclusive
    assert kb.cls_recall(0.7) == "warn"
    assert kb.cls_recall(0.6) == "warn"     # boundary is inclusive
    assert kb.cls_recall(0.4) == "bad"
    assert kb.cls_recall(None) == "muted"


def test_empty_rate_status_thresholds():
    """Mirrors cls_recall's bad/warn/good tiers but on the null-result rate,
    and — unlike the inline StatCard, which shows 0% for missing data — returns
    None on a missing rate rather than reading "no traffic" as "healthy"."""
    assert kb.empty_rate_status(0.3) == "bad"
    assert kb.empty_rate_status(0.21) == "bad"
    assert kb.empty_rate_status(0.2) == "warn"    # boundary is not bad
    assert kb.empty_rate_status(0.1) == "warn"
    assert kb.empty_rate_status(0.05) == "good"   # boundary is not warn
    assert kb.empty_rate_status(0.0) == "good"
    assert kb.empty_rate_status(None) is None


def test_load_query_stats_reads_the_configured_log(tmp_path, monkeypatch):
    log = tmp_path / "kb-queries.jsonl"
    log.write_text('{"tool": "kb_search", "empty": true}\n')
    monkeypatch.setattr(kb, "QUERY_LOG", log)
    assert kb.load_query_stats()["total"] == 1


def test_panel1_has_four_cards_no_golden_cases_card():
    """The golden cases StatCard was removed (the count shows in the panel
    header badge); Panel 1 should contain exactly 4 recall-metric StatCards."""
    html = kb.render(_sample_ev(), _sample_q(), _sample_m())
    # the panel header badge still carries the count
    assert "20 cases" in html or "cases" in html
    # the standalone "golden cases" card label is gone from the statcard row
    assert 'statcard-label">golden cases' not in html and \
           ">golden cases<" not in html


# --- freshness (owns the /kb <-> /overview stalled agreement) --------------


def test_freshness_stalled_past_30h():
    fresh = kb.freshness({"generated": "2020-01-01T00:00:00Z"})
    assert fresh.stalled is True
    assert fresh.cadence == "daily (07:48 MST)"


def test_freshness_not_stalled_when_recent():
    import orient_time
    fresh = kb.freshness({"generated": orient_time.now().isoformat()})
    assert fresh.stalled is False


def test_freshness_no_judgment_without_an_eval():
    fresh = kb.freshness(None)
    assert fresh.stalled is None


def test_render_shows_stalled_banner_for_an_old_eval():
    ev = _sample_ev()
    ev["generated"] = "2020-01-01T00:00:00Z"
    html = kb.render(ev, _sample_q(), _sample_m())
    assert 'class="stalled-banner"' in html


def test_render_no_stalled_banner_for_a_fresh_eval():
    import orient_time
    ev = _sample_ev()
    ev["generated"] = orient_time.now().isoformat()
    html = kb.render(ev, _sample_q(), _sample_m())
    assert 'class="stalled-banner"' not in html


# --- KB_EVAL_BIN resolves kb-eval through paths.resolve_tool ---------------


def test_kb_eval_bin_matches_a_live_resolve_tool_call():
    """Pins that KB_EVAL_BIN is whatever `paths.resolve_tool("kb-eval")`
    yields, not a reconstructed literal — it fails if kb_build.py ever
    hardcodes a path instead of delegating to resolve_tool. No reload, no
    monkeypatch: `importlib.reload(kb)` inside a `finally` does NOT restore
    the real module state, because monkeypatch's teardown runs only after
    this test function returns — the second reload re-executes kb_build.py's
    module top level under the still-active stub, leaving a fabricated
    KB_EVAL_BIN for the rest of the pytest session. Calling resolve_tool
    live and comparing needs neither reload nor patch, and holds in any
    environment (deploy area present or not) because both sides observe the
    same process state at the same moment."""
    assert kb.KB_EVAL_BIN == kb.paths.resolve_tool("kb-eval")


def test_refresh_eval_runs_the_resolved_path_when_it_exists(monkeypatch):
    monkeypatch.setattr(kb, "KB_EVAL_BIN", __file__)  # any real, existing path
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    kb.refresh_eval()
    assert calls == [[__file__]]


def test_refresh_eval_skips_the_subprocess_when_kb_eval_bin_is_none(monkeypatch):
    monkeypatch.setattr(kb, "KB_EVAL_BIN", None)

    def fail_if_called(*a, **k):
        raise AssertionError("refresh_eval must not run a subprocess when KB_EVAL_BIN is None")

    monkeypatch.setattr(kb.subprocess, "run", fail_if_called)
    kb.refresh_eval()  # must not raise


def test_refresh_eval_logs_stderr_and_reports_no_refresh_on_nonzero_exit(monkeypatch, tmp_path, capsys):
    """issue_01M1S3YSKJV3GCB8ZN4MQ7W16N: a harness that exits non-zero must not
    read as success. The captured stderr reaches the log, and the job itself
    must not raise or fail — the dashboard's other two panels are independent
    and must still render."""
    monkeypatch.setattr(kb, "KB_EVAL_BIN", __file__)  # any real, existing path
    eval_json = tmp_path / "kb-eval.json"
    monkeypatch.setattr(kb, "KB_EVAL_JSON", eval_json)  # file never created

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"golden set missing\n")

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    refreshed = kb.refresh_eval()
    assert refreshed is False
    captured = capsys.readouterr()
    assert "golden set missing" in captured.err
    assert "exited 1" in captured.err


def test_refresh_eval_reports_refreshed_when_the_harness_updates_the_json(monkeypatch, tmp_path):
    """The check the issue names: comparing KB_EVAL_JSON's mtime across the
    call is what tells a live eval apart from a stale file left on disk by a
    harness that's been failing for days."""
    monkeypatch.setattr(kb, "KB_EVAL_BIN", __file__)  # any real, existing path
    eval_json = tmp_path / "kb-eval.json"
    eval_json.write_text('{"stale": true}')
    monkeypatch.setattr(kb, "KB_EVAL_JSON", eval_json)
    old_mtime = eval_json.stat().st_mtime - 5
    os.utime(eval_json, (old_mtime, old_mtime))

    def fake_run(cmd, **kw):
        eval_json.write_text('{"fresh": true}')  # simulates the harness's own write
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    assert kb.refresh_eval() is True


def test_refresh_eval_reports_not_refreshed_when_the_json_is_untouched(monkeypatch, tmp_path):
    """A harness that exits 0 without writing kb-eval.json (or that never ran
    at all) must not be reported as a refresh — only the mtime actually
    moving counts."""
    monkeypatch.setattr(kb, "KB_EVAL_BIN", __file__)  # any real, existing path
    eval_json = tmp_path / "kb-eval.json"
    eval_json.write_text('{"stale": true}')
    monkeypatch.setattr(kb, "KB_EVAL_JSON", eval_json)

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0)  # exits clean but touches nothing

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    assert kb.refresh_eval() is False


def test_refresh_eval_does_not_raise_when_the_mtime_stat_fails(monkeypatch, capsys):
    """issue_01M1S3YSKJV3GCB8ZN4MQ7W16N round 2: an OSError from the mtime
    stat (the scorecard directory going away, a permission change) must not
    propagate out of refresh_eval. build() has no handler of its own, so an
    uncaught exception here would exit the whole script non-zero and none of
    the dashboard's three panels would render, not just the eval one."""
    monkeypatch.setattr(kb, "KB_EVAL_BIN", __file__)  # any real, existing path

    class _StatRaises:
        def stat(self):
            raise OSError("scorecard directory gone")

    monkeypatch.setattr(kb, "KB_EVAL_JSON", _StatRaises())

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    assert kb.refresh_eval() is False  # must not raise
    assert "stat failed" in capsys.readouterr().err


# --- panel 4: the weekly KB write audit (kb-audit stream) -------------------

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _audit_sig(kind: str, body: str, produced_at: dt.datetime, sid: str) -> library_client.SignalRecord:
    return library_client.SignalRecord(
        id=sid, source="platform", stream="kb-audit", kind=kind, actor="platform",
        ingested_by=None, product="substrate", body=body, refs={}, meta={},
        privacy_class="internal", produced_at=produced_at, ingested_at=produced_at)


_DIGEST_BODY = ("KB write audit — last 8.days\n"
                "  32 via PR · 57 via the cage · 9 via legacy scribe · 0 unsigned")
_ALERT_BODY = (
    "KB write audit — last 8.days\n"
    "  32 via PR · 57 via the cage · 9 via legacy scribe · 1 unsigned\n"
    "\n"
    "BYPASSED BOTH SANCTIONED PATHS (1) — neither a cage write nor a PR merge:\n"
    "  f783f4b6d  2026-08-17  the operator  docs: file forecast-backtest restore runbook\n"
    "  Each of these reached main without review or provenance. The ARCH-395 hook "
    "does not catch them.\n"
    "\n"
    "WRITTEN ON ATTESTATION (0) — nothing to review.")


def test_audit_digest_week_reads_clean():
    rows = [_audit_sig("digest", _DIGEST_BODY, _now() - dt.timedelta(days=1), "sig_d")]
    html = kb.render(_sample_ev(), _sample_q(), _sample_m(), audit=rows)
    assert "KB write audit" in html
    assert "Clean: no commit reached main" in html
    assert "Findings:" not in html and "unknown, not clean" not in html


def test_audit_alert_week_names_its_commit_and_does_not_read_clean():
    rows = [_audit_sig("alert", _ALERT_BODY, _now() - dt.timedelta(days=1), "sig_a")]
    html = kb.render(_sample_ev(), _sample_q(), _sample_m(), audit=rows)
    # The offending commit is named by sha and subject, not reduced to a count.
    assert "f783f4b6d" in html
    assert "docs: file forecast-backtest restore runbook" in html
    assert "Findings:" in html
    assert "Clean: no commit reached main" not in html


_STORED_ALERT_BODY = (
    "KB write audit — last 8.days 32 via PR · 57 via the cage · 9 via legacy scribe · "
    "1 unsigned BYPASSED BOTH SANCTIONED PATHS (1) — neither a cage write nor a PR merge: "
    "f783f4b6d 2026-08-17 the operator docs: file forecast-backtest restore runbook + "
    "portfolio-masking PRD ( Each of these reached main without review or provenance. "
    "The ARCH-395 hook does not catch them: it blocks committing ON main, not pushing TO "
    "it from a branch. WRITTEN ON ATTESTATION (54) — an agent asserted these were agreed "
    "in session; skim for anything you do not recognise: a8e9dd9d4 2026-08-22 docs: update "
    "projects/deck/bead/deck.md [session:operator]")


def test_audit_alert_names_its_commit_in_the_stored_flattened_body():
    """The Library stores the alert with its line breaks flattened to spaces
    (signal sig_01M0QA5GJA4Q0FYXFA9TPCE1GN, read back 2026-10-03). The commit must
    still come out as a table row, and the attested commits below the bypass
    list must not."""
    rows = [_audit_sig("alert", _STORED_ALERT_BODY, _now() - dt.timedelta(days=1), "sig_a")]
    html = kb.render(_sample_ev(), _sample_q(), _sample_m(), audit=rows)
    assert '<td data-label="Commit" class="k">f783f4b6d</td>' in html
    assert "the operator docs: file forecast-backtest restore runbook" in html
    assert "a8e9dd9d4" not in html


def test_audit_alert_week_shows_the_failed_run_text_when_no_commit_is_named():
    body = ("The weekly KB write audit could not run (exit 2). No week was examined; "
            "treat this as unknown, not clean.")
    rows = [_audit_sig("alert", body, _now() - dt.timedelta(days=1), "sig_x")]
    html = kb.render(_sample_ev(), _sample_q(), _sample_m(), audit=rows)
    assert "could not run (exit 2)" in html
    assert "Clean: no commit reached main" not in html


def test_audit_no_signal_in_window_reads_unknown_and_shows_last_run():
    old = _now() - dt.timedelta(days=40)
    rows = [_audit_sig("digest", _DIGEST_BODY, old, "sig_old")]
    html = kb.render(_sample_ev(), _sample_q(), _sample_m(), audit=rows)
    assert "did not run in the last 8 days" in html
    assert "unknown, not clean" in html
    assert "Clean: no commit reached main" not in html
    assert 'class="empty-state"' in html
    assert kb.orient_time.fmt(old.isoformat(), assume_utc=True) in html


def test_audit_never_captured_reads_unknown():
    html = kb.render(_sample_ev(), _sample_q(), _sample_m(), audit=[])
    assert "No audit has ever been captured" in html
    assert "Clean: no commit reached main" not in html


def test_audit_failed_fetch_says_unavailable(monkeypatch, capsys):
    def boom(**kw):
        raise urllib.error.URLError("library down")

    monkeypatch.setattr(kb.library_client, "list_signals", boom)
    assert kb.load_audit() is None
    assert "could not read the kb-audit stream (URLError)" in capsys.readouterr().err

    html = kb.render(_sample_ev(), _sample_q(), _sample_m(), audit=None)
    assert "The audit results are unavailable" in html
    assert "Clean: no commit reached main" not in html


def test_load_audit_asks_the_library_for_the_kb_audit_stream(monkeypatch):
    seen = {}

    def fake_list_signals(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(kb.library_client, "list_signals", fake_list_signals)
    assert kb.load_audit() == []
    assert seen == {"stream": ["kb-audit"]}
