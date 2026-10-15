#!/usr/bin/env python3.11
"""/engineering/policy — the tool policy in force for one agent/crew/model
combination, and the form to change it (ADR-0048, epic
issue_01M0C91CGDR0YT8T5ERX2HD2HS).

Third of the epic's three items. The first (issue_01M1F1DT9SM5V17BF4AV8FTTN2,
PR #243) built `library_client.resolve_policy`, the read client for `GET
/policy/resolve`. The second (issue_01M1F1E59D38JMZ8WJVKH6W1AY, PR #244) built
this page read-only. This pass adds the write form `_write_form_html` below —
the one bit of markup on this page that can change what an agent may do — and
every security check that gates it lives in `policy_api.py`, not here: the
form's own visibility (`data-can-policy-write`) is cosmetic, matching
`reference_doc_build.py`'s KB editor toggle, and posting to `/api/policy`
directly (bypassing this page entirely) hits the exact same gate.

**A LOOKUP, not a directory.** The Library serves exactly `GET
/policy/version`, `GET /policy/resolve` and `PUT /policy` — there is no route
to list every override on file, at any layer (the Library's `store.py` has
one resolve function and one upsert, both keyed to a single triple). So this
page answers "what applies to THIS agent/crew/model" and cannot show "every
override on file anywhere"; `_SCOPE_NOTE` below says so on the page itself,
the way `/engineering/e2e`'s own scope note does for its own narrower claim.

**Read at request time, always** — matching `resolve_policy` itself (no TTL
cache, no module-level dict): a stale deny-list is exactly the property the
epic exists to prevent, and this page holds no copy of anything it renders.

Handled as a literal query-string route in server.py (like `/crews/runs`),
NOT through `_full_only_routes()`. That dispatcher calls its renderer with
zero arguments — this page needs `agent`/`crew`/`model` off the query string
— and falls back to a static file on a render failure. A static fallback here
would be a second, stale copy of a deny-list decision, which is the one thing
this page is built not to hold; letting the failure become a plain 500
instead is also what makes it visibly different from a successful call that
happens to resolve to no explicit overrides (DoD: the two must not look
alike).

Full-tier only (server.py gates /engineering/policy); a contractor gets 404.
`policy_api.py`'s own capability gate (`policy_write`, held by the admin role;
see authz.py) is the real wall on the write — putting a SECOND, weaker copy of
that gate here would protect nothing a full page-load already exposes and
would only give a reviewer two places to check instead of one.
"""
from __future__ import annotations

from html import escape

import crew_registry
import design_system as ds
import library_client
import model_ids
import roster_build

_SCOPE_NOTE = (
    "This looks up ONE agent/crew/model combination at a time — the Library "
    "has no route to list every override on file, so this page can never "
    "show \"everything denied anywhere,\" only the answer for the triple "
    "entered below."
)


def _known_values(table: dict[str, str] | None, crews: list[str] | None,
                  scope: frozenset[str], whole_fleet: bool,
                  ) -> tuple[list[str], list[str], list[str]]:
    """Agent, crew and model strings this repo already knows about, offered
    as `<datalist>` suggestions — NOT a validation list. The Library's policy
    table has no notion of a valid agent/crew/model, only strings it was
    asked to remember, so a triple outside these lists is not an error;
    asking the running Library is how you find out what it actually holds.

    Sourced from `roster_build.load_roster()` rather than a second,
    hand-maintained list of agents/projects — the mistake `roster_build`'s
    own `PROJECTS` constant made once (a hardcoded five-project list going
    stale) and fixed with `discover_projects()`. Reusing that discovery here
    means a new project or agent shows up in these suggestions the same way
    it shows up on /engineering/roster, with nothing here to fall out of
    sync.

    **A project's own lane agents and its crew are restricted to `scope`**
    (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) the same way `/engineering/
    roster`'s own cards are: a project the registry lists under no project
    in `scope` offers no agent slug and no crew key here. `autonomous`
    (cross-cutting agents in no project's profile) stays unfiltered,
    matching `roster_build`'s own precedent — it names agent slugs and
    models, never a project.

    **Crews are the registry's crew keys** (`crew_registry.public_crews`),
    the value `agentctl` looks a dispatch's policy up under. The roster's
    project slugs were offered here until 2026-09-29, and some are not crew
    keys: a row written for `agent-platform` or `convivy-deck` bound nothing,
    because those projects' crews are `bosun` and `deck`. With no registry
    (`crews` is None) no crew is suggested.

    **Models are offered resolved, not as the roster's raw alias.** The
    roster's `model:` field holds an alias (`sonnet`), but the row this page
    reads and writes is keyed on the id `agentctl` resolves that alias to
    (`claude-sonnet-5`, `model_ids.resolve`) — offering the alias here is
    what made a deny-list entry written from these very suggestions bind
    nothing (issue_01M2HVGKSNFMHZF0FDN8FBT9N6). `model_ids.resolve` passes an
    already-resolved value through unchanged, so an agent whose roster model
    is already an id (as in tests fixturing one directly) still dedupes into
    one suggestion rather than two. With no `table` (agentctl's list is
    unavailable) no model is suggested, since a raw alias is the wrong key.
    """
    roster = roster_build.load_roster()
    repos = None if whole_fleet else crew_registry.repos_in_scope(scope)
    agents: set[str] = set()
    models: set[str] = set()
    for project in roster["projects"]:
        if repos is not None and project.get("slug") not in repos:
            continue
        for lane in project["lanes"]:
            for agent in lane["agents"]:
                slug = agent.get("slug")
                if slug:
                    agents.add(slug)
                model = agent.get("model")
                if model and table is not None:
                    models.add(model_ids.resolve(model, table))
    for agent in roster["autonomous"]:
        slug = agent.get("slug")
        if slug:
            agents.add(slug)
        model = agent.get("model")
        if model and table is not None:
            models.add(model_ids.resolve(model, table))
    crews = crews or []
    if not whole_fleet:
        in_scope_crews = crew_registry.crews_in_scope(scope)
        crews = [c for c in crews if c in in_scope_crews]
    return sorted(agents), list(crews), sorted(models)


def _datalist_html(id_: str, values: list[str]) -> str:
    options = "".join(f'<option value="{escape(v, quote=True)}">' for v in values)
    return f'<datalist id="{escape(id_, quote=True)}">{options}</datalist>'


def _form_html(agent: str, crew: str, model: str, table: dict[str, str] | None,
               crews: list[str] | None, scope: frozenset[str], whole_fleet: bool) -> str:
    known_agents, known_crews, known_models = _known_values(table, crews, scope, whole_fleet)
    return (
        f'<form method="get" action="/engineering/policy" class="pol-form">'
        f'<label>Agent<br><input type="text" name="agent" list="polAgents" '
        f'value="{escape(agent, quote=True)}" placeholder="e.g. scout"></label>'
        f'<label>Crew<br><input type="text" name="crew" list="polCrews" '
        f'value="{escape(crew, quote=True)}" placeholder="e.g. vivy"></label>'
        f'<label>Model<br><input type="text" name="model" list="polModels" '
        f'value="{escape(model, quote=True)}" placeholder="e.g. claude-sonnet-5"></label>'
        f'<button type="submit">Check</button>'
        f'</form>'
        f'{_datalist_html("polAgents", known_agents)}'
        f'{_datalist_html("polCrews", known_crews)}'
        f'{_datalist_html("polModels", known_models)}'
    )


def _policy_rows(resolution: library_client.PolicyResolution,
                 crew_is_public: bool) -> list[tuple[str, str, str]]:
    """One row per target this triple has an opinion about: every explicit
    override plus every member of the two default classes. Each row carries
    (target, decision, why) — "why" is what lets a reader tell an explicit
    override (something a person set) from a class default (something the
    tool's own class carries).

    **An exfiltration-class target on a public crew renders "allow".** The
    Library serves `exfiltration_builtins` as denied for every crew, but the
    decision that binds is agentctl's: it merges only the Library's `explicit`
    rows (`_library_denials`) and adds its own `_EXFIL_DENY` only on a
    private-crew dispatch or one with no repo. Showing the Library's "deny"
    here told the operator WebFetch was denied to reviewer on convivy-orient, and
    run-ec2be7ff fetched example.com under exactly that triple
    (issue_01M1QY3HJTFHYDY3ZJPQ1PNHE5). Connector servers keep the Library's
    answer: how agentctl enforces them has not been traced, so this page has
    no evidence to relabel them with.

    A crew not in the public list, including every crew while the registry is
    unavailable, keeps "deny", which is what a private dispatch gets."""
    targets = sorted(
        set(resolution.explicit)
        | set(resolution.connector_servers)
        | set(resolution.exfiltration_builtins))
    rows = []
    for target in targets:
        decision = resolution.decision(target)
        if target in resolution.explicit:
            why = "explicit override"
        elif target in resolution.connector_servers:
            why = "class default — account connector"
        elif crew_is_public:
            decision = "allow"
            why = ("class default — exfiltration-capable built-in; agentctl "
                   "denies it only on private-crew dispatches, so this public "
                   "crew has it unless denied explicitly")
        else:
            why = ("class default — exfiltration-capable built-in; agentctl "
                   "denies it on private-crew dispatches")
        rows.append((target, decision, why))
    return rows


# The two decision strings the Library's own resolution can produce
# (`store.POLICY_ALLOW` and the class-default `"deny"`). Anything else is a
# value this page does not understand, which is a THIRD state rather than
# either of the two.
_DECISION_PILL = {"deny": "pol-deny", "allow": "pol-allow"}


def _row_html(target: str, decision: str, why: str) -> str:
    # **An unrecognised decision must not render as "allow".** The pill's
    # colour is how this page is actually read — scanned down a column — so
    # the colour, not the label beside it, is what a reader takes away. The
    # first version of this line was `"pol-deny" if decision == "deny" else
    # "pol-allow"`, which put every unexpected value into the green branch:
    # a wrong-cased `"DENY"`, or any string a future writer stores, would
    # have read as permitted on a page whose subject is what is forbidden.
    #
    # It is inert today — the live Library resolves only `"deny"` or
    # `"allow"` — and becomes reachable the moment this epic's third item
    # starts writing `explicit` values that nothing in this display layer
    # validates. Fixed before that route exists rather than after.
    #
    # An unknown value gets its own state rather than being folded into
    # "deny", because calling a value we cannot parse "denied" is its own
    # false claim; this page's whole discipline is that two different
    # situations must not look alike.
    pill_cls = _DECISION_PILL.get(decision, "pol-unknown")
    return (
        f'<div class="pol-row">'
        f'<span class="pol-target">{escape(target)}</span>'
        f'<span class="pill {pill_cls}">{escape(decision)}</span>'
        f'<span class="pol-why">{escape(why)}</span>'
        f'</div>'
    )


def _write_form_html(agent: str, crew: str, model: str, crews: list[str] | None) -> str:
    """The write form for THIS triple — target + decision, posting straight to
    `POST /api/policy`. `data-can-policy-write` is the `__CAN_POLICY_WRITE__`
    substitution `server.py` applies from the viewer's role, matching
    `__CAN_KB_WRITE__`/`__CAN_BLOG_WRITE__`: cosmetic, since `policy_api.py`'s
    own gate (human, CSRF, `policy_write` scoped to `crew`) is the wall
    regardless of what this attribute says — a viewer who lacks the
    capability but posts to it anyway is refused there, not here.

    `agent`/`crew`/`model` travel as data attributes rather than hidden form
    fields, matching the KB editor's own JSON-payload convention, and are
    read by the script below rather than re-typed by whoever is using the
    form — the triple on screen is the triple the write applies to.

    "Every public crew" sends `all_public_crews` in place of `crew`, and
    `policy_api.py` writes the row once per crew in the registry. The box
    lists those crews so the operator sees what one Save will write. It is
    absent when the registry is unavailable."""
    all_crews = (
        f'<label class="pol-write-all"><input type="checkbox" id="polWriteAll"> '
        f'Apply to every public crew ({len(crews)}), not only {escape(crew)}'
        f'<span class="pol-write-crews">{escape(", ".join(crews))}</span></label>'
    ) if crews else ""
    return (
        f'<section class="pol-write" id="polWrite" data-can-policy-write="__CAN_POLICY_WRITE__" '
        f'data-agent="{escape(agent, quote=True)}" data-crew="{escape(crew, quote=True)}" '
        f'data-model="{escape(model, quote=True)}">'
        f'<h2>Set a decision</h2>'
        f'<div class="pol-write-row">'
        f'<label>Target<br><input type="text" id="polWriteTarget" placeholder="e.g. WebFetch"></label>'
        f'<label>Decision<br><select id="polWriteDecision">'
        f'<option value="deny">deny</option><option value="allow">allow</option>'
        f'</select></label>'
        f'<button type="button" id="polWriteBtn">Save</button>'
        f'</div>'
        f'{all_crews}'
        f'<div class="pol-write-note" id="polWriteNote" hidden></div>'
        f'</section>'
        f'<script>{_WRITE_JS}</script>'
    )


_WRITE_JS = r"""
(function() {
  const section = document.getElementById("polWrite");
  if (!section || section.dataset.canPolicyWrite !== "true") return;
  const targetInput = document.getElementById("polWriteTarget");
  const decisionSelect = document.getElementById("polWriteDecision");
  const btn = document.getElementById("polWriteBtn");
  const note = document.getElementById("polWriteNote");
  const allBox = document.getElementById("polWriteAll");

  function setNote(cls, text) {
    note.className = "pol-write-note " + cls;
    note.textContent = text;
    note.hidden = false;
  }

  btn.addEventListener("click", function() {
    const target = targetInput.value.trim();
    if (!target) { setNote("err", "Target is required."); return; }
    const payload = {
      agent: section.dataset.agent, model: section.dataset.model,
      target: target, decision: decisionSelect.value,
    };
    if (allBox && allBox.checked) payload.all_public_crews = true;
    else payload.crew = section.dataset.crew;
    btn.disabled = true;
    fetch("/api/policy", {
      method: "POST", credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function(r) {
      return r.json().catch(function() { return {}; }).then(function(j) {
        if (!r.ok) {
          let msg = (j && j.error) || ("HTTP " + r.status);
          if (j && j.failed_crew) {
            msg += " at crew " + j.failed_crew + "; written before it: " +
              ((j.written && j.written.length) ? j.written.join(", ") : "none") +
              ". Saving again rewrites those with the same value.";
          }
          throw new Error(msg);
        }
        return j;
      });
    }).then(function(j) {
      setNote("ok", (j.crews ? "Saved for " + j.crews.length + " crews. " : "Saved. ") + "Reloading…");
      window.location.reload();
    }).catch(function(err) {
      setNote("err", "Couldn't save: " + err.message);
    }).finally(function() { btn.disabled = false; });
  });
})();
"""


def _result_html(agent: str, crew: str, model: str, crews: list[str] | None,
                 whole_fleet: bool = True) -> str:
    """The resolution for one triple, and the write form under it. The write
    form's "every public crew" box names every crew in the registry, so a
    scoped viewer is given the form without it (E19·4)."""
    resolution = library_client.resolve_policy(agent, crew, model)
    rows = _policy_rows(resolution, crews is not None and crew in crews)
    if not resolution.explicit:
        lead = ds.empty_state(
            f'Nothing is explicitly denied — or allowed — for agent="{agent}" '
            f'crew="{crew}" model="{model}". Account connectors are denied by '
            f'default; exfiltration-capable built-ins are denied only on '
            f'private-crew dispatches; everything else resolves to '
            f'"{resolution.default}".')
    else:
        lead = (
            f'<p class="pol-summary">{len(resolution.explicit)} explicit '
            f'override{"s" if len(resolution.explicit) != 1 else ""} on file '
            f'for this combination.</p>')
    table = (
        "".join(_row_html(*row) for row in rows) if rows
        else ds.empty_state("Neither class carries any targets right now.")
    )
    return (
        f'<div class="pol-result">'
        f'{lead}'
        f'<div class="pol-table">'
        f'<div class="pol-row pol-row-head">'
        f'<span class="pol-target">Target</span>'
        f'<span>Decision</span>'
        f'<span class="pol-why">Why</span>'
        f'</div>'
        f'{table}'
        f'</div>'
        f'<p class="pol-footnote">Everything not listed above resolves to '
        f'"{escape(resolution.default)}". Read from the Library for this '
        f'request only — policy version {resolution.version}, never cached.</p>'
        f'{_write_form_html(agent, crew, model, crews if whole_fleet else None)}'
        f'</div>'
    )


def live_html(query: dict | None = None, scope: frozenset[str] = frozenset(),
              whole_fleet: bool = True) -> str:
    """Render /engineering/policy for the agent/crew/model in `query` (the
    raw `dict(request.query_params)` server.py passes, matching
    `crews_build.runs_html`'s own signature). Raises whatever
    `library_client.resolve_policy` raises on a Library failure — server.py's
    literal-path handler for this route catches that the same way it does for
    /crews/runs, logs it, and answers 500 with no static fallback, which is
    the only way this page is visibly different from an empty result rather
    than silently indistinguishable from one.

    `scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) restrict
    the form's own agent/crew suggestion lists, passed to `_form_html` — see
    `_known_values`'s docstring — and gate the resolve itself: a scoped
    viewer's crew must be one `crew_registry.crews_in_scope(scope)` lists,
    or the Library is never asked. A crew outside their scope and a crew the
    registry has never heard of get the same answer, so the page cannot be
    used to probe whether an out-of-scope crew exists or what it is denied.

    `model` is normalized through `model_ids.resolve` before it is used for
    anything — the lookup, the redisplayed form field, and the write form's
    `data-model` — so a triple entered with an alias (typed, or carried over
    from a bookmarked URL predating this fix) reads and writes the same key
    `policy_api.py`'s write boundary now stores under
    (issue_01M2HVGKSNFMHZF0FDN8FBT9N6). Without this, a page that just wrote
    a row keyed on the resolved id would immediately fail to find it again
    under the alias still sitting in the query string."""
    query = query or {}
    agent = (query.get("agent") or "").strip()
    crew = (query.get("crew") or "").strip()
    model = (query.get("model") or "").strip()
    try:
        table: dict[str, str] | None = model_ids.aliases()
    except model_ids.ModelListUnavailable:
        table = None
    if model and table is not None:
        model = model_ids.resolve(model, table)
    try:
        crews: list[str] | None = crew_registry.public_crews()
    except crew_registry.RegistryUnavailable:
        crews = None

    if (agent and crew and model and not whole_fleet
            and crew not in crew_registry.crews_in_scope(scope)):
        content = ds.empty_state(
            f'No crew "{crew}" in your projects. This lookup answers only for '
            f'the crews of the projects you have access to.')
    elif agent and crew and model and table is None:
        content = ds.empty_state(
            "The model list is unavailable (agentctl models gave no usable "
            "answer), so this lookup cannot be keyed the way a dispatch keys it.")
    elif agent and crew and model:
        content = _result_html(agent, crew, model, crews, whole_fleet)
    else:
        content = ds.empty_state(
            "Enter an agent, crew and model above to see what's denied for "
            "that combination.")

    body = f'''<div class="pol">
  <header class="pol-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / engineering / policy</div>
    <h1>Tool policy</h1>
    <p class="lede">What an agent/crew/model combination is denied right now, read live from the Library.</p>
    <p class="scope-note">{escape(_SCOPE_NOTE)}</p>
  </header>
  <div class="wrap">
    {_form_html(agent, crew, model, table, crews, scope, whole_fleet)}
    {content}
  </div>
</div>'''
    return ds.page_shell(
        title="Tool Policy",
        nav_html=ds.nav_component("/engineering/policy", "full"),
        body=body,
        extra_css=_CSS,
    )


_CSS = r"""
.pol { max-width: 760px; margin: 0 auto; padding: 32px 32px 120px; }
.pol-head h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -.01em; margin: 4px 0 6px; color: var(--ink); }
.pol .lede { color: var(--ink-2); font-size: 14px; max-width: 620px; margin: 0 0 6px; }
.pol .scope-note { color: var(--faint); font-size: .8rem; margin: 0 0 20px; }
.pol-form {
  display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end;
  padding: 16px; margin: 0 0 24px;
  background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
}
.pol-form label { font-size: 12px; color: var(--ink-3); font-weight: 600; }
.pol-form input {
  display: block; margin-top: 4px; font-family: var(--mono); font-size: 13px;
  color: var(--ink); background: var(--bg); border: 1px solid var(--line);
  border-radius: 7px; padding: 7px 9px; min-width: 160px;
}
.pol-form button {
  font-family: var(--sans); font-size: 13px; font-weight: 600; color: var(--bg);
  background: var(--ink); border: none; border-radius: 7px; padding: 8px 16px;
  cursor: pointer;
}
.pol-summary { font-size: 13.5px; color: var(--ink-2); margin: 0 0 14px; }
.pol-table { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.pol-row {
  display: grid; grid-template-columns: 1.4fr .6fr 1.6fr; gap: 10px; align-items: center;
  padding: 9px 14px; border-bottom: 1px solid var(--line); font-size: 13px;
}
.pol-row:last-child { border-bottom: none; }
.pol-row-head { background: var(--surface-2); font-size: 11px; text-transform: uppercase;
  letter-spacing: .03em; color: var(--ink-3); font-weight: 700; }
.pol-target { font-family: var(--mono); color: var(--ink); }
.pol-why { color: var(--ink-3); font-size: 12.5px; }
.pill.pol-deny { background: var(--err-wash); color: var(--err); }
.pill.pol-allow { background: var(--ok-wash); color: var(--ok); }
.pill.pol-unknown { background: var(--warn-wash); color: var(--warn); border: 1px dashed var(--warn); }
.pol-footnote { color: var(--ink-3); font-size: 12px; margin: 14px 0 0; }
.pol-write {
  margin-top: 20px; padding-top: 18px; border-top: 1px solid var(--line);
}
.pol-write[data-can-policy-write="false"] { display: none; }
.pol-write h2 { font-size: 13px; font-weight: 700; color: var(--ink-3);
  text-transform: uppercase; letter-spacing: .03em; margin: 0 0 12px; }
.pol-write-row { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end; }
.pol-write-row label { font-size: 12px; color: var(--ink-3); font-weight: 600; }
.pol-write-row input, .pol-write-row select {
  display: block; margin-top: 4px; font-family: var(--mono); font-size: 13px;
  color: var(--ink); background: var(--bg); border: 1px solid var(--line);
  border-radius: 7px; padding: 7px 9px; min-width: 160px;
}
.pol-write-row button {
  font-family: var(--sans); font-size: 13px; font-weight: 600; color: var(--bg);
  background: var(--ink); border: none; border-radius: 7px; padding: 8px 16px;
  cursor: pointer;
}
.pol-write-all { display: block; margin-top: 12px; font-size: 12.5px; color: var(--ink-2); }
.pol-write-all input { margin-right: 6px; }
.pol-write-crews { display: block; margin: 4px 0 0 22px; color: var(--ink-3);
  font-family: var(--mono); font-size: 12px; }
.pol-write-note { margin-top: 10px; font-size: 12.5px; }
.pol-write-note.ok { color: var(--ok); }
.pol-write-note.err { color: var(--err); }
"""
