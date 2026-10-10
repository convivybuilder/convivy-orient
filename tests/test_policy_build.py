"""Render tests for /engineering/policy (issue_01M1F1E59D38JMZ8WJVKH6W1AY,
epic issue_01M0C91CGDR0YT8T5ERX2HD2HS).

Load-bearing: an incomplete triple never calls the Library at all; a
zero-explicit-overrides result states that plainly and still shows the class
defaults that apply, rather than reading like `design_system.awaiting_state`
(a slot for a capability that doesn't exist yet — this one works); an explicit
override is labeled distinctly from a class default; and a Library failure
propagates (this module must not catch it, matching `library_client`'s own
"failure is not caught" rule) so server.py's route handler is what turns it
into a visibly different 500.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error

import pytest

import crew_registry
import library_client
import model_catalog
import policy_build


_CATALOG = [
    model_catalog.Model(model="claude-opus-5-5", alias="opus", provider="anthropic"),
    model_catalog.Model(model="claude-sonnet-5", alias="sonnet", provider="anthropic"),
]


@pytest.fixture(autouse=True)
def listed_models(monkeypatch):
    """agentctl's model list, which `model_ids` now reads. CI has no agentctl,
    so every test here sees this list unless it replaces it."""
    catalog = {"value": list(_CATALOG)}
    monkeypatch.setattr(model_catalog, "load", lambda: catalog["value"])
    return catalog


def _resolution(*, explicit=None, connectors=None, builtins=None,
                default="allow", version=0, agent="scout", crew="test",
                model="claude-sonnet-5"):
    return library_client.PolicyResolution(
        agent=agent, crew=crew, model=model,
        explicit=explicit if explicit is not None else {},
        connector_servers=connectors if connectors is not None else [
            "claude_ai_Gmail", "claude_ai_Slack"],
        exfiltration_builtins=builtins if builtins is not None else [
            "DesignSync", "RemoteTrigger", "WebFetch"],
        default=default, version=version)


@pytest.fixture
def no_roster(monkeypatch):
    """Most tests here don't care what the datalist suggestions are; stub
    roster discovery to an empty roster so a test failure in that unrelated
    path can't masquerade as a policy-rendering bug."""
    monkeypatch.setattr(policy_build.roster_build, "load_roster",
                        lambda: {"projects": [], "autonomous": []})


# --- an incomplete triple asks the Library nothing ---------------------------


def test_no_query_shows_the_prompt_and_never_calls_the_library(monkeypatch, no_roster):
    def boom(agent, crew, model):
        raise AssertionError("must not resolve policy with no triple given")
    monkeypatch.setattr(policy_build.library_client, "resolve_policy", boom)
    html = policy_build.live_html({})
    assert "Enter an agent, crew and model" in html


@pytest.mark.parametrize("query", [
    {"agent": "scout"},
    {"agent": "scout", "crew": "vivy"},
    {"agent": " ", "crew": "vivy", "model": "claude-sonnet-5"},
])
def test_partial_triple_shows_the_prompt_and_never_calls_the_library(
        query, monkeypatch, no_roster):
    def boom(agent, crew, model):
        raise AssertionError("must not resolve policy with an incomplete triple")
    monkeypatch.setattr(policy_build.library_client, "resolve_policy", boom)
    html = policy_build.live_html(query)
    assert "Enter an agent, crew and model" in html


# --- the empty-explicit state is a real answer, not `awaiting_state` --------


def test_zero_explicit_overrides_states_plainly_and_keeps_class_defaults(
        monkeypatch, no_roster):
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution())
    html = policy_build.live_html({"agent": "scout", "crew": "test", "model": "claude-sonnet-5"})
    assert "Nothing is explicitly denied" in html
    assert "claude_ai_Gmail" in html and "WebFetch" in html
    assert 'class="pill pol-deny"' in html
    # Never the awaiting_state idiom — the capability exists, there's just no
    # override. (The shared stylesheet always defines `.awaiting-state`'s CSS
    # rules regardless of use, so the rendered marker to check for is the div
    # itself / the badge text, not the class name's mere presence anywhere.)
    assert '<div class="awaiting-state' not in html
    assert "WAITING ON" not in html


def test_zero_explicit_overrides_names_the_fallback_default(monkeypatch, no_roster):
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution(default="allow"))
    html = policy_build.live_html({"agent": "scout", "crew": "test", "model": "claude-sonnet-5"})
    assert 'resolves to "allow"' in html


# --- explicit vs. class default are visually and textually distinct --------


def test_explicit_override_is_labeled_explicit_not_class_default(monkeypatch, no_roster):
    monkeypatch.setattr(
        policy_build.library_client, "resolve_policy",
        lambda agent, crew, model: _resolution(explicit={"WebFetch": "allow"}))
    html = policy_build.live_html({"agent": "scout", "crew": "test", "model": "claude-sonnet-5"})
    assert "explicit override" in html
    assert 'class="pill pol-allow"' in html  # the carve-out renders as allowed


def test_class_default_target_with_no_override_is_labeled_class_default(monkeypatch, no_roster):
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution())
    html = policy_build.live_html({"agent": "scout", "crew": "test", "model": "claude-sonnet-5"})
    assert "class default" in html


def test_explicit_target_outside_either_class_still_renders(monkeypatch, no_roster):
    monkeypatch.setattr(
        policy_build.library_client, "resolve_policy",
        lambda agent, crew, model: _resolution(explicit={"gh": "deny"},
                                                connectors=[], builtins=[]))
    html = policy_build.live_html({"agent": "scout", "crew": "test", "model": "claude-sonnet-5"})
    assert ">gh<" in html
    assert "explicit override" in html


# --- escaping ----------------------------------------------------------------


def test_query_values_are_escaped(monkeypatch, no_roster):
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution())
    html = policy_build.live_html({"agent": "<script>x</script>", "crew": "t", "model": "m"})
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


# --- failure is not caught here (library_client's own rule) -----------------


def test_a_library_failure_propagates_rather_than_rendering_empty(monkeypatch, no_roster):
    def boom(agent, crew, model):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(policy_build.library_client, "resolve_policy", boom)
    with pytest.raises(urllib.error.URLError):
        policy_build.live_html({"agent": "scout", "crew": "test", "model": "claude-sonnet-5"})


# --- suggestions come from roster discovery, not a second hand list --------


def test_datalist_suggestions_are_sourced_from_roster_discovery(monkeypatch):
    monkeypatch.setattr(policy_build.roster_build, "load_roster", lambda: {
        "projects": [{
            "slug": "vivy", "label": "Vivy", "repo": "/x",
            "lanes": [{"name": "daytime", "label": "Daytime",
                       "agents": [{"slug": "vivy-coder", "model": "claude-opus-5"}]}],
        }],
        "autonomous": [{"slug": "consultant", "model": "claude-sonnet-5"}],
    })
    html = policy_build.live_html({})
    assert 'id="polAgents"' in html
    assert '<option value="vivy-coder">' in html
    assert '<option value="consultant">' in html
    assert '<option value="claude-opus-5">' in html
    assert '<option value="claude-sonnet-5">' in html


def test_datalist_offers_resolved_model_ids_not_roster_aliases(monkeypatch):
    """The failure this item is about: the roster's `model:` field holds an
    alias (`sonnet`), but the row the page reads and writes is keyed on the
    resolved id (`claude-sonnet-5`). Offering the alias in the datalist is
    what led an operator straight into writing a key nothing reads
    (issue_01M2HVGKSNFMHZF0FDN8FBT9N6); the suggestion must be the id."""
    monkeypatch.setattr(policy_build.roster_build, "load_roster", lambda: {
        "projects": [{
            "slug": "convivy-orient", "label": "Orient", "repo": "/x",
            "lanes": [{"name": "daytime", "label": "Daytime",
                       "agents": [{"slug": "orient-coder", "model": "sonnet"}]}],
        }],
        "autonomous": [],
    })
    html = policy_build.live_html({})
    assert '<option value="claude-sonnet-5">' in html
    assert '<option value="sonnet">' not in html


# --- the model is normalized to the id agentctl resolves, both directions --


def test_a_lookup_by_alias_resolves_to_the_stored_id(monkeypatch, no_roster):
    """A row is written under the resolved id (`policy_api.py`'s write
    boundary). A page still queried with the alias — typed, or a bookmarked
    URL predating this fix — must resolve the same way, or the page cannot
    find the row it, or a fixed write path, just wrote
    (issue_01M2HVGKSNFMHZF0FDN8FBT9N6)."""
    seen = {}

    def fake_resolve(agent, crew, model):
        seen["model"] = model
        return _resolution(model=model)

    monkeypatch.setattr(policy_build.library_client, "resolve_policy", fake_resolve)
    policy_build.live_html({"agent": "scout", "crew": "test", "model": "sonnet"})
    assert seen["model"] == "claude-sonnet-5"


def test_the_write_form_carries_the_normalized_model(monkeypatch, no_roster):
    """`data-model` is what the page's own write-form script posts to
    `/api/policy` — if it still carried the alias, saving through the page
    would write the wrong key even with `policy_api.py`'s own normalization
    in place, since that normalization is idempotent and only helps if the
    posted value is inspectable here."""
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution(model=model))
    html = policy_build.live_html({"agent": "scout", "crew": "test", "model": "sonnet"})
    assert 'data-model="claude-sonnet-5"' in html
    assert 'data-model="sonnet"' not in html


# --- nothing here reaches Orient's own control database ---------------------


def test_module_imports_no_control_db():
    import policy_build as mod
    assert not hasattr(mod, "control_db")


def test_route_registered_in_the_nav_not_full_only_routes():
    """This page needs `agent`/`crew`/`model` off the query string, so it is
    a literal path in server.py's handle_get (like /crews/runs) rather than
    an entry in `_full_only_routes()`, which calls its renderer with zero
    arguments and falls back to a stale static file on failure — exactly the
    second copy of a deny-list decision this page must not hold."""
    import server
    assert "/engineering/policy" not in server._full_only_routes()
    assert server.policy_build.live_html is policy_build.live_html


def test_the_rendered_page_says_it_is_a_lookup_not_a_directory():
    """The Library has no route to list the policy table — only `resolve` for
    one triple and `upsert` for one row, at every layer. So this page can
    never show "everything denied anywhere", and a reader who assumed it did
    would read "nothing denied" as fleet-wide when it only ever answered for
    the triple they typed.

    Asserted on the RENDERED OUTPUT rather than on `_SCOPE_NOTE`, because a
    constant can exist while nothing interpolates it — and pinning the
    constant would pass in exactly that case. Checked without a triple, since
    the landing state is where a reader forms their idea of what the page
    covers.

    Added after the note was removed by hand and all fourteen existing tests
    still passed: the copy is a definition-of-done bullet with no test behind
    it, which is how a prose pass silently turns an honest page into a
    misleading one."""
    html = policy_build.live_html({})
    assert "no route to list every override" in html
    assert "ONE agent/crew/model combination" in html


def test_an_unrecognised_decision_never_renders_as_allow():
    """The pill's colour is how this page is read — scanned down a column —
    so an unexpected decision value landing in the green "allow" branch would
    tell a reader a target is permitted on a page whose subject is what is
    forbidden.

    The first version was `"pol-deny" if decision == "deny" else "pol-allow"`,
    which put every value that is not exactly `"deny"` — a wrong-cased
    `"DENY"`, or anything this epic's write route later stores — into allow.

    Unrecognised gets its OWN state rather than being folded into deny:
    calling a value we cannot parse "denied" is its own false claim, and this
    module's whole discipline is that two different situations must not look
    alike. Asserts all three branches, so collapsing any two of them fails."""
    assert 'class="pill pol-deny"' in policy_build._row_html("t", "deny", "w")
    assert 'class="pill pol-allow"' in policy_build._row_html("t", "allow", "w")
    for weird in ("DENY", "Deny", "forbidden", "", "allow "):
        html = policy_build._row_html("t", weird, "w")
        assert 'class="pill pol-unknown"' in html, f"{weird!r} should be unknown"
        assert "pol-allow" not in html, f"{weird!r} must never read as allow"


# --- the alias table comes from agentctl (issue_01M2HVGKSNFMHZF0FDN8FBT9N6) --


def _one_agent_roster(model):
    return lambda: {
        "projects": [{
            "slug": "convivy-orient", "label": "Orient", "repo": "/x",
            "lanes": [{"name": "daytime", "label": "Daytime",
                       "agents": [{"slug": "planner", "model": model}]}],
        }],
        "autonomous": [],
    }


def test_an_opus_agent_is_offered_the_id_agentctl_dispatches_today(monkeypatch):
    monkeypatch.setattr(policy_build.roster_build, "load_roster", _one_agent_roster("opus"))
    html = policy_build.live_html({})
    assert '<option value="claude-opus-5-5">' in html
    assert '<option value="claude-opus-5">' not in html


def test_an_unavailable_list_suggests_no_model(monkeypatch, listed_models):
    listed_models["value"] = None
    monkeypatch.setattr(policy_build.roster_build, "load_roster", _one_agent_roster("opus"))
    html = policy_build.live_html({})
    assert '<option value="opus">' not in html
    assert '<option value="claude-opus-5' not in html


def test_an_unavailable_list_refuses_to_key_a_lookup(monkeypatch, no_roster, listed_models):
    """Looking up under an unresolved alias would report "nothing denied" for
    a row that exists under the resolved id."""
    listed_models["value"] = None

    def boom(*a, **k):
        raise AssertionError("looked up a policy key with no model table")

    monkeypatch.setattr(library_client, "resolve_policy", boom)
    html = policy_build.live_html({"agent": "planner", "crew": "vivy", "model": "opus"})
    assert "model list is unavailable" in html


# --- crews come from the registry, and one Save can cover all of them -------


@pytest.fixture
def public_crews(monkeypatch):
    crews = {"value": ["bosun", "deck", "vivy"]}

    def load():
        if crews["value"] is None:
            raise crew_registry.RegistryUnavailable("absent")
        return list(crews["value"])

    monkeypatch.setattr(crew_registry, "public_crews", load)
    return crews


def test_crew_suggestions_are_registry_crew_keys_not_project_slugs(monkeypatch, public_crews):
    """`agent-platform` is a project slug whose crew is `bosun`; a row written
    under the slug bound nothing."""
    monkeypatch.setattr(policy_build.roster_build, "load_roster", lambda: {
        "projects": [{"slug": "agent-platform", "label": "Bosun", "repo": "/x", "lanes": []}],
        "autonomous": [],
    })
    html = policy_build.live_html({})
    assert '<option value="bosun">' in html
    assert '<option value="agent-platform">' not in html


def test_the_write_form_lists_every_crew_one_save_would_write(monkeypatch, no_roster,
                                                              public_crews):
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution())
    html = policy_build.live_html({"agent": "scout", "crew": "vivy", "model": "claude-sonnet-5"})
    assert 'id="polWriteAll"' in html
    assert "every public crew (3)" in html
    assert "bosun, deck, vivy" in html


def test_no_every_crew_option_while_the_registry_is_unavailable(monkeypatch, no_roster,
                                                                public_crews):
    public_crews["value"] = None
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution())
    html = policy_build.live_html({"agent": "scout", "crew": "vivy", "model": "claude-sonnet-5"})
    assert 'id="polWriteAll"' not in html
    assert 'id="polWriteBtn"' in html


# --- exfiltration-class defaults bind only on private crews ------------------


def _row_for(html, target):
    start = html.index(f'<span class="pol-target">{target}</span>')
    return html[start:html.index("</div>", start)]


def test_an_exfiltration_builtin_on_a_public_crew_renders_allow(monkeypatch, no_roster,
                                                                public_crews):
    """agentctl adds `_EXFIL_DENY` only on a private dispatch; run-ec2be7ff
    fetched with WebFetch as reviewer on a public crew while this row read
    "deny" (issue_01M1QY3HJTFHYDY3ZJPQ1PNHE5)."""
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution(crew="vivy"))
    html = policy_build.live_html({"agent": "reviewer", "crew": "vivy", "model": "claude-sonnet-5"})
    row = _row_for(html, "WebFetch")
    assert 'class="pill pol-allow"' in row
    assert "only on private-crew dispatches" in row
    # Connector servers are denied on every dispatch, public crew included.
    assert 'class="pill pol-deny"' in _row_for(html, "claude_ai_Gmail")


def test_an_exfiltration_builtin_on_a_crew_outside_the_public_list_renders_deny(
        monkeypatch, no_roster, public_crews):
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution(crew="cfo"))
    html = policy_build.live_html({"agent": "reviewer", "crew": "cfo", "model": "claude-sonnet-5"})
    row = _row_for(html, "WebFetch")
    assert 'class="pill pol-deny"' in row
    assert "denies it on private-crew dispatches" in row


def test_an_exfiltration_builtin_renders_deny_while_the_registry_is_unavailable(
        monkeypatch, no_roster, public_crews):
    public_crews["value"] = None
    monkeypatch.setattr(policy_build.library_client, "resolve_policy",
                        lambda agent, crew, model: _resolution(crew="vivy"))
    html = policy_build.live_html({"agent": "reviewer", "crew": "vivy", "model": "claude-sonnet-5"})
    assert 'class="pill pol-deny"' in _row_for(html, "WebFetch")


def test_an_explicit_deny_on_a_public_crew_still_renders_deny(monkeypatch, no_roster,
                                                              public_crews):
    monkeypatch.setattr(
        policy_build.library_client, "resolve_policy",
        lambda agent, crew, model: _resolution(crew="vivy", explicit={"WebFetch": "deny"}))
    html = policy_build.live_html({"agent": "reviewer", "crew": "vivy", "model": "claude-sonnet-5"})
    row = _row_for(html, "WebFetch")
    assert 'class="pill pol-deny"' in row
    assert "explicit override" in row


def _run_write_js(all_checked):
    """Run the page's real write script in node with a stub DOM, click Save,
    and return the JSON body it posted."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to exercise the policy page JS")
    stub = """
const posted = [];
function el(extra) { return Object.assign({ dataset: {}, value: "", hidden: true,
  className: "", textContent: "", disabled: false, _h: null,
  addEventListener(ev, h) { this._h = h; } }, extra); }
const els = {
  polWrite: el({ dataset: { canPolicyWrite: "true", agent: "scout", crew: "vivy",
                            model: "claude-sonnet-5" } }),
  polWriteTarget: el({ value: "WebFetch" }),
  polWriteDecision: el({ value: "allow" }),
  polWriteBtn: el({}),
  polWriteNote: el({}),
  polWriteAll: el({ checked: __CHECKED__ }),
};
global.document = { getElementById(id) { return els[id] || null; } };
global.window = { location: { reload() {} } };
global.fetch = function(url, opts) {
  posted.push(JSON.parse(opts.body));
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
};
""".replace("__CHECKED__", "true" if all_checked else "false")
    driver = """
els.polWriteBtn._h();
setTimeout(() => console.log(JSON.stringify(posted[0])), 0);
"""
    full = f"'use strict';\n{stub}\n{policy_build._WRITE_JS}\n{driver}"
    r = subprocess.run([node, "-e", full], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_script_sends_all_public_crews_instead_of_crew_when_ticked():
    body = _run_write_js(all_checked=True)
    assert body["all_public_crews"] is True
    assert "crew" not in body
    assert (body["agent"], body["model"], body["target"], body["decision"]) == (
        "scout", "claude-sonnet-5", "WebFetch", "allow")


def test_the_script_sends_the_looked_up_crew_when_not_ticked():
    body = _run_write_js(all_checked=False)
    assert body["crew"] == "vivy"
    assert "all_public_crews" not in body
