#!/usr/bin/env python3.11
"""alerts_build.py — the /alerts dashboard (epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW).

Redefines Alerts away from a raw feed of every signal tagged `alert` in the
signal store (the old /engineering/alerts — see ops_reports_build.py's module
docstring for that history) into a policy-threshold view: an alert fires ONLY
when a Dashboards metric crosses the threshold that metric's own page already
colors by. This module invents no new numbers — every check below calls the
classification function the source dashboard already uses for its own
good/warn/bad coloring, so /alerts and the page it points at can never
disagree about what counts as in-policy:

  Code Health   -> code_health_build.ci_status / oldest_pr_status / reverts_status
  Spending      -> dashboard_build.overage_status
  KB Retrieval  -> kb_build.cls_recall / empty_rate_status

Security and Warrant are DELIBERATELY EXCLUDED. Both pages' own docstrings say
their rate coloring is not yet a policy threshold — security_build._blk_cls and
warrant_build._rate_cls are explicitly a "soft read", a measure-first baseline
with no threshold set. This page reuses existing thresholds; it does not invent
one, so there is nothing to alert on there until those pages define one.

No live gh/API calls: every check reads the same persisted snapshot its source
dashboard's own live_html() already reads, so /alerts is as cheap as any other
Dashboards page render (ARCH-182).

ACKNOWLEDGMENT STATE IS NOT SHIPPED HERE — see the PR body for why and what a
follow-up would need. Every alert on this page is a live read of "is this
metric currently past its threshold", so the page is self-clearing: fix the
metric and the alert is gone on the next render. That is a materially weaker
property than an ack — it says "the value recovered", not "a person saw
this" — but it means the page never carries a stale alert for a metric that
has already recovered.

Served full-tier only (server.py FULL_ONLY), same gate as every other
Dashboards page.
"""
from __future__ import annotations

from html import escape

import alert_policy_store
import code_health_build
import dashboard_build
import design_system as ds
import kb_build
import orient_time
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"

_SEVERITY_ORDER = {"bad": 0, "warn": 1}
_SEVERITY_LABEL = {"bad": "Critical", "warn": "Warning"}


def _alert(*, category: str, metric: str, subject: str, severity: str,
           value: str, detail: str, href: str) -> dict:
    return {"category": category, "metric": metric, "subject": subject,
            "severity": severity, "value": value, "detail": detail, "href": href}


# --- per-category checks (pure: take already-loaded snapshot data) ----------


def code_health_alerts(rows: list[dict], thresholds: dict | None = None) -> list[dict]:
    """One alert per (repo, metric) whose status is warn/bad. `rows` is
    code_health_build.load_snapshot()'s per-repo list (the harvest already
    scheduled to produce site/health.html).

    `thresholds` defaults to `alert_policy_store.DEFAULTS` — pure, so a test
    calling this with one argument (every existing caller) evaluates against
    the shipped default with no control_db touched. `gather()` fetches the
    live policy and threads it through."""
    t = thresholds or alert_policy_store.DEFAULTS["code_health.ci_pass_rate"]
    out = []
    for row in rows:
        repo = row.get("repo", "?")
        rate = row.get("ci_pass_rate_main")
        status = code_health_build.ci_status(rate, t)
        if status in ("warn", "bad"):
            out.append(_alert(
                category="Code Health", metric="CI pass rate (main)", subject=repo,
                severity=status, value=f"{rate * 100:.0f}%",
                detail=f"{repo}'s merged-code CI pass rate is {rate * 100:.0f}% "
                       f"(good ≥{t['good_min'] * 100:.0f}%, warn ≥{t['warn_min'] * 100:.0f}%).",
                href="/health"))
        days = row.get("oldest_open_days")
        status = code_health_build.oldest_pr_status(days)
        if status in ("warn", "bad"):
            out.append(_alert(
                category="Code Health", metric="Oldest open PR", subject=repo,
                severity=status, value=f"{days}d",
                detail=f"{repo}'s oldest open PR is {days} days old "
                       "(warn past 7d, bad past 14d).",
                href="/health"))
        n = row.get("reverts_recent")
        status = code_health_build.reverts_status(n)
        if status == "bad":
            out.append(_alert(
                category="Code Health", metric="Reverts", subject=repo,
                severity="bad", value=str(n),
                detail=f"{repo} had {n} revert(s) in the last "
                       f"{code_health_build.WINDOW_DAYS} days.",
                href="/health"))
    return out


def spending_alert(snap: dict, thresholds: dict | None = None) -> dict | None:
    """The account-wide subscription-overage alert, or None. `snap` is the
    parsed agent-cost-report latest.json (dashboard_build.load_latest_snapshot()).

    `thresholds` defaults to `alert_policy_store.DEFAULTS` — pure, so a test
    calling this with one argument (every existing caller) evaluates against
    the shipped default with no control_db touched. `gather()` fetches the
    live policy and threads it through."""
    t = thresholds or alert_policy_store.DEFAULTS["spending.overage_runs"]
    ov = (snap or {}).get("overage")
    if dashboard_build.overage_status(ov, t) != "bad":
        return None
    hit = ov.get("runs_in_overage")
    n = ov.get("runs_reporting")
    return _alert(
        category="Spending", metric="Subscription overage", subject="account-wide",
        severity="bad", value=f"{hit} of {n} runs",
        detail=f"The subscription was drawing extra usage during {hit} of {n} "
               "runs reported today.",
        href="/spending")


def kb_alerts(ev: dict | None, q: dict, thresholds: dict | None = None) -> list[dict]:
    """Recall/MRR alerts from the golden-set eval, plus the null-result rate
    from live query stats. `ev` is kb_build.load_eval()'s result; `q` is
    kb_build.load_query_stats()'s result.

    `thresholds` defaults to `alert_policy_store.DEFAULTS` — pure, so a test
    calling this with two arguments (every existing caller) evaluates against
    the shipped default with no control_db touched. `gather()` fetches the
    live policy and threads it through. (empty_rate_status is out of scope —
    Security/Warrant-style soft reads, not one of the three settled
    thresholds — and keeps its own hardcoded bound.)"""
    t = thresholds or alert_policy_store.DEFAULTS["kb.recall"]
    out = []
    if ev:
        overall = ev.get("overall") or {}
        for key, label in (("recall@1", "recall@1"), ("mrr", "MRR")):
            val = overall.get(key)
            status = kb_build.cls_recall(val, t)
            if status in ("warn", "bad"):
                shown = f"{val:.2f}" if key == "mrr" else f"{val * 100:.0f}%"
                out.append(_alert(
                    category="KB Retrieval", metric=label, subject="golden set",
                    severity=status, value=shown,
                    detail=f"{label} against the golden set is {shown} "
                           f"(good ≥{t['good_min'] * 100:.0f}%, warn ≥{t['warn_min'] * 100:.0f}%).",
                    href="/kb"))
    rate = q.get("empty_rate")
    status = kb_build.empty_rate_status(rate)
    if status in ("warn", "bad"):
        out.append(_alert(
            category="KB Retrieval", metric="Null-result rate", subject="live queries",
            severity=status, value=f"{rate * 100:.0f}%",
            detail=f"{rate * 100:.0f}% of live kb_search calls returned zero hits "
                   "(good ≤5%, warn ≤20%).",
            href="/kb"))
    return out


def freshness_banners() -> str:
    """A `stalled_state` banner for each of this page's three source feeds that
    is currently stalled — /alerts computes nothing of its own, so a stale
    source here is exactly as stale as it is on that source's own dashboard,
    and reads its freshness the same way (issue_01M0TKX6DW2E1TXGN9QT6K61J5
    comment 902). Isolated per source like `gather()`: a broken read degrades
    to no banner for that source rather than breaking the page."""
    banners = []
    try:
        asof, _ = code_health_build.load_snapshot()
        banners.append(ds.stalled_banner_html(
            "Code-health harvest", code_health_build.freshness(asof)))
    except Exception:
        pass
    try:
        banners.append(ds.stalled_banner_html(
            "Spend snapshot", dashboard_build.spending_freshness(dashboard_build.load_latest_snapshot())))
    except Exception:
        pass
    try:
        banners.append(ds.stalled_banner_html("KB golden-set eval", kb_build.freshness(kb_build.load_eval())))
    except Exception:
        pass
    return "".join(banners)


def gather() -> list[dict]:
    """Every current alert, from the same persisted snapshots each source
    dashboard's own live_html() reads — no live gh/API calls. Each source is
    isolated: a bad or missing snapshot degrades to "no alerts from that
    source" rather than breaking the page, mirroring waiting_build.gather's
    per-tile isolation on Home.

    Fetches each of the three settable thresholds fresh on every call (no
    cache) — the same invariant `alert_policy_store.get`'s own docstring
    states: a write made from a source dashboard's own page must reach this
    evaluator on its very next call, or the control on that page changes
    nothing while looking like it works."""
    alerts: list[dict] = []
    try:
        _, health_rows = code_health_build.load_snapshot()
        t = alert_policy_store.get("code_health.ci_pass_rate")
        alerts += code_health_alerts(health_rows, t)
    except Exception:
        pass
    try:
        t = alert_policy_store.get("spending.overage_runs")
        a = spending_alert(dashboard_build.load_latest_snapshot(), t)
        if a:
            alerts.append(a)
    except Exception:
        pass
    try:
        ev = kb_build.load_eval()
        q = kb_build.load_query_stats()
        t = alert_policy_store.get("kb.recall")
        alerts += kb_alerts(ev, q, t)
    except Exception:
        pass
    alerts.sort(key=lambda a: (_SEVERITY_ORDER.get(a["severity"], 9), a["category"], a["subject"]))
    return alerts


# --- rendering ----------------------------------------------------------------

_PAGE_CSS = r"""
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; margin-bottom:16px; max-width:70ch; }
.page-sub a { color:var(--brand-ink); text-decoration:none; }
.page-sub a:hover { text-decoration:underline; }

.statcards { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:16px 0 6px; }
@media (max-width:640px){ .statcards { grid-template-columns:1fr; } }

.data-table th, .data-table td { text-align:right; white-space:nowrap; }
.data-table th:first-child, .data-table td:first-child { text-align:left; }
.data-table td[data-label="Detail"] { text-align:left; white-space:normal; }
td.k { font-weight:600; }
.data-table tbody tr:hover td { background:var(--brand-wash); }
.data-table td a { color:inherit; text-decoration:none; border-bottom:1px dotted var(--line); }
.data-table td a:hover { color:var(--brand-ink); border-bottom-color:var(--brand-ink); }

.pill { font-size:11px; padding:1px 7px; border-radius:20px; border:1px solid var(--line);
        color:var(--ink-2); }
.good { color:var(--ok); font-weight:600; }
.warn { color:var(--warn); font-weight:600; }
.bad  { color:var(--err); font-weight:600; }
.muted { color:var(--ink-2); }

.clean { color:var(--ink-2); padding:20px; border:1px dashed var(--line); border-radius:8px;
         background:var(--surface); margin:8px 0; }

.notes { color:var(--ink-2); font-size:12px; line-height:1.65; margin:22px 0 0;
         padding:12px 14px 12px 26px; border-left:2px solid var(--brand);
         background:var(--surface); border-radius:4px; list-style:none; }
.notes li { margin-bottom:5px; }
.notes b { color:var(--ink); font-weight:650; }
"""


def _rows_html(alerts: list[dict]) -> str:
    out = []
    for a in alerts:
        out.append(
            "<tr>"
            f'<td data-label="Severity"><span class="{escape(a["severity"])}">'
            f'{escape(_SEVERITY_LABEL.get(a["severity"], a["severity"]))}</span></td>'
            f'<td data-label="Category" class="k">{escape(a["category"])}</td>'
            f'<td data-label="Metric">{escape(a["metric"])}</td>'
            f'<td data-label="Subject">{escape(a["subject"])}</td>'
            f'<td data-label="Value">{escape(a["value"])}</td>'
            f'<td data-label="Detail">{escape(a["detail"])} '
            f'<a href="{escape(a["href"], quote=True)}">view →</a></td>'
            "</tr>"
        )
    return "".join(out)


def render(alerts: list[dict], asof: str | None = None, banners: str = "") -> str:
    asof = asof or orient_time.now_label()
    n_bad = sum(1 for a in alerts if a["severity"] == "bad")
    n_warn = sum(1 for a in alerts if a["severity"] == "warn")

    if not alerts:
        body = (
            '<div class="page-head"><h1>Alerts</h1>'
            f'<p class="page-sub">Fires only when a Dashboards metric crosses its own page’s '
            f'policy threshold — <a href="/health">Code Health</a>, '
            f'<a href="/spending">Spending</a>, '
            f'<a href="/kb">KB Retrieval</a>. As of {escape(asof)}.</p></div>'
            + banners
            + '<div class="clean">No alerts — every tracked metric is inside its policy '
            "threshold.</div>"
        )
        return ds.page_shell(title="alerts", nav_html=ds.nav_component("/alerts", "full"),
                             body=body, extra_css=_PAGE_CSS)

    cards = "".join([
        ds.stat_card("Critical", str(n_bad)),
        ds.stat_card("Warning", str(n_warn)),
        ds.stat_card("Total", str(len(alerts))),
    ])
    heads = ["Severity", "Category", "Metric", "Subject", "Value", "Detail"]
    ths = "".join(f"<th>{escape(h)}</th>" for h in heads)
    table = ('<div class="data-table-wrap"><table class="data-table"><thead><tr>'
             + ths + "</tr></thead><tbody>" + _rows_html(alerts) + "</tbody></table></div>")

    body = (
        '<div class="page-head"><h1>Alerts</h1>'
        f'<p class="page-sub">Fires only when a Dashboards metric crosses its own page’s '
        f'policy threshold — <a href="/health">Code Health</a>, '
        f'<a href="/spending">Spending</a>, '
        f'<a href="/kb">KB Retrieval</a>. As of {escape(asof)}.</p></div>'
        + banners
        + f'<div class="statcards">{cards}</div>'
        + table
        + '<ul class="notes">'
        '<li>Every threshold here is read from the source page’s own status logic '
        '(<code>code_health_build.ci_status</code>, '
        '<code>dashboard_build.overage_status</code>, <code>kb_build.cls_recall</code>) — '
        'this page invents no new numbers, and cannot disagree with the page it links to.</li>'
        '<li><b>Security</b> and <b>Warrant</b> are not covered: both pages’ own coloring is '
        'explicitly a soft read, not a policy threshold yet — there is nothing here to alert on '
        'until those pages set one.</li>'
        '<li>This list has no acknowledgment state: an alert clears itself the moment its metric '
        'recovers, not when a person marks it seen — see the PR that shipped this page for why.</li>'
        '</ul>'
    )
    return ds.page_shell(title="alerts", nav_html=ds.nav_component("/alerts", "full"),
                         body=body, extra_css=_PAGE_CSS)


def live_html() -> str:
    """Render /alerts from the current persisted snapshots at request time
    (ARCH-182). Each source dashboard stays the data producer; this only
    re-reads what they already wrote."""
    return render(gather(), banners=freshness_banners())


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    alerts = gather()
    (OUT / "alerts.html").write_text(render(alerts, banners=freshness_banners()), encoding="utf-8")
    print(f"Wrote site/alerts.html ({len(alerts)} alerts).")


if __name__ == "__main__":
    build()
