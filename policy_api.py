#!/usr/bin/env python3.11
"""`POST /api/policy` — the agent tool deny-list write route (ADR-0048, epic
issue_01M0C91CGDR0YT8T5ERX2HD2HS, issue_01M1F1ENN8P6RD4W3SJJ04HR45). Third of
the epic's three items, and the one carrying its whole security burden: the
first two built a read client (`library_client.resolve_policy`, PR #243) and a
read-only page (`policy_build.py`, PR #244). Nothing before this module could
change what an agent may do; this one can, so it is reviewed alone.

**The security property is the point, not the UI: an agent cannot widen its
own permissions when the only route to the field is a surface no agent holds
credentials for.** Three checks below, all required, none sufficient alone —
matching the Library's own `PUT /policy`, which draws the same line a second
time on its own side of the wire:

1. `ident.kind != "human"` refuses UNCONDITIONALLY, before anything else runs.
   Orient's own identity model genuinely has a `human` kind
   (`signal_capture_api.py:114`, `server.py:482`) and this is the Orient-side
   half of ADR-0048's two-boundary design — see (3) for the other half. No
   exception, whatever else the identity holds: an agent token that somehow
   carried every other capability in `authz.ROLE_CAPS` still stops here.
2. `same_origin(request)` — CSRF, matching `room_api.py`'s pattern exactly:
   a state change reachable via the ambient CF Access cookie must not be
   forgeable by a cross-site POST.
3. `authorize(ident, "policy.write", ...)` — this route's OWN capability
   (`policy_write`, granted to the admin role and no other, in
   `authz.ROLE_CAPS`; see that file), never a reuse of a general admin one,
   so it can be granted and re-checked on its own terms rather than riding
   along with unrelated admin rights.
   Checked TWICE: once before the body is even read (matching every other
   admin-write route in this repo — `room_api`, `kb_write_api`), and again
   after parsing, with the CREW THE BODY ACTUALLY RESOLVED TO passed as the
   scope. The second check is not redundant — it reuses `authorize`'s
   existing product-scope machinery (`in_scope`) to catch a human granted
   `policy_write` but scoped to one product writing a deny-list entry for a
   crew outside it, and checking the CALLER's right against what the request
   ACTUALLY RESOLVES TO rather than what it CLAIMS is exactly the shape
   sibling PR #179 spent five review rounds re-finding (epic body, epic
   comment 744). A missing capability and an out-of-scope crew answer
   identically: **404**, not 403 — the epic's own DoD wording — so a
   full-tier member, whose role does not hold `policy_write`, is not even told
   this route exists. (Every admin holds it since PR #265.)

Once all three hold, `acting_human` is `ident.actor` — **never read off the
request body**, matching `signal_capture_api.py`'s rule for the same field and
for the same reason: a caller-supplied value would let the attestation assert
whatever the caller typed, which is the one thing `identity.attested_human`
exists not to do on the Library side.

**Type-check to reject, strip as a separate step** (`_clean_field` below) —
carried over from the Library's own `PUT /policy`, whose first fix for this
exact route let `isinstance` decide only whether to strip a value, which let a
truthy non-string reach `len()` uncaught. This module is a second place the
same mistake could be made independently, so the same discipline is applied
here rather than assumed inherited from the far side of an HTTP call.

**No second copy of the deny-list.** Every write goes straight to the
Library via `library_client.write_policy`; nothing here is stored, cached, or
read back locally.
"""
from __future__ import annotations

import json
import urllib.error

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import crew_registry
import library_client
import model_ids
from api_common import _json, api_guard as _api, read_json_body, resolve_identity, same_origin
from authz import authorize

router = APIRouter(prefix="/api")

# Four short identifiers and a decision word; generous relative to the
# Library's own `store.MAX_POLICY_FIELD` (64) so an oversized field 400s from
# there with its own message rather than being silently truncated here.
MAX_BODY = 4 * 1024
MAX_FIELD = 500


def _clean_field(payload: dict, name: str) -> tuple[str | None, JSONResponse | None]:
    """`payload[name]`, type-checked to REFUSE before it is stripped — never
    the reverse (Library `PUT /policy` review, round 2: letting `isinstance`
    decide only whether to strip let a truthy non-string reach `len()`
    uncaught). Returns `(value, None)` or `(None, error-response)`."""
    value = payload.get(name)
    if not isinstance(value, str):
        return None, _json(400, {"error": f"{name} is required"})
    value = value.strip()
    if not value:
        return None, _json(400, {"error": f"{name} is required"})
    if len(value) > MAX_FIELD:
        return None, _json(400, {"error": f"{name} is too long"})
    return value, None


@router.post("/policy")
@_api
async def write_policy(request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    # UNCONDITIONAL — no capability, however broad, substitutes for this.
    # Checked FIRST, ahead of CSRF and the capability gate below, so an agent
    # identity never reaches either: the whole point of this route is that
    # the surface it protects has no agent-shaped door at all.
    if ident.kind != "human":
        return _json(403, {"error": "only a human may change the tool policy"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    # THIS ROUTE'S OWN CAPABILITY — checked here, ahead of the body, matching
    # every other admin-write route in this repo (room_api._api routes,
    # kb_write_api._authorized): a caller who cannot use this surface at all
    # learns that before anything about the shape it expects. 404, not 403 —
    # the epic's own DoD wording — so a full-tier member, whose role does not
    # hold `policy_write`, is not even told this route exists.
    if not authorize(ident, "policy.write", None):
        return _json(404, {"error": "not found"})

    payload, err = await read_json_body(request, MAX_BODY)
    if err:
        return err
    if "acting_human" in payload:
        return _json(400, {"error": "acting_human is not a caller-supplied field; "
                                    "it is the verified identity of the submitting human"})

    # `all_public_crews: true` in place of `crew` writes the same row once per
    # public crew in the project registry (crew_registry.py): the Library's
    # table is exact-match, so "every crew" has to be that many rows.
    all_public = payload.get("all_public_crews", False)
    if all_public is not True and all_public is not False:
        return _json(400, {"error": "all_public_crews must be true or false"})
    if all_public and "crew" in payload:
        return _json(400, {"error": "send crew or all_public_crews, not both"})

    fields: dict[str, str] = {}
    for name in ("agent", "model", "target") if all_public else ("agent", "crew", "model", "target"):
        value, err = _clean_field(payload, name)
        if err:
            return err
        fields[name] = value
    # NORMALIZED HERE, ahead of every use below (the crew-scope check, the
    # write itself) — an operator who types the alias the page's own
    # datalist offers (`sonnet`) must write the key `agentctl` resolves
    # (`claude-sonnet-5`), or the deny-list entry binds nothing while
    # displaying as if it does (issue_01M2HVGKSNFMHZF0FDN8FBT9N6).
    # With no table from agentctl there is no way to know the key a dispatch
    # will look up, so the write is refused rather than stored under a guess.
    try:
        fields["model"] = model_ids.resolve(fields["model"], model_ids.aliases())
    except model_ids.ModelListUnavailable:
        return _json(503, {"error": "the model list is unavailable (agentctl models gave "
                                    "no usable answer), so the model cannot be resolved"})
    decision = payload.get("decision")
    if decision not in library_client.POLICY_DECISIONS:
        return _json(400, {"error": "decision must be one of "
                                    f"{sorted(library_client.POLICY_DECISIONS)}",
                           "decision": decision})

    # THE SAME CAPABILITY, RE-CHECKED AGAINST THE CREW THE BODY ACTUALLY
    # RESOLVED TO — not "holds policy_write" in the abstract. The check above
    # only proved this identity holds policy_write AT ALL; a human scoped to
    # one product must not be able to write a deny-list entry for a crew
    # outside it, and checking the caller's right against what the request
    # CLAIMS rather than what it ACTUALLY RESOLVES TO is the exact bug shape
    # sibling PR #179 spent five review rounds re-finding (epic body, epic
    # comment 744). Answers the same 404 as the coarse check above — a
    # missing capability and an out-of-scope crew must not be
    # distinguishable from the response.
    if all_public:
        try:
            crews = crew_registry.public_crews()
        except crew_registry.RegistryUnavailable:
            return _json(503, {"error": "the project registry is unavailable, so the "
                                        "public crews cannot be listed"})
    else:
        crews = [fields["crew"]]
    # Every crew is checked before anything is written, so a writer scoped to
    # some of them is refused whole rather than left with a partial grant.
    if not all(authorize(ident, "policy.write", crew) for crew in crews):
        return _json(404, {"error": "not found"})

    written: list[str] = []
    result: dict = {}
    for crew in crews:
        try:
            result = library_client.write_policy(
                agent=fields["agent"], crew=crew, model=fields["model"],
                target=fields["target"], decision=decision, acting_human=ident.actor)
        except Exception as e:
            err = _write_error(e)
            if all_public:
                # The Library has no transaction across rows. Say which crews
                # were written; resubmitting rewrites them with the same value.
                body = json.loads(err.body)
                body.update({"written": written, "failed_crew": crew})
                return _json(err.status_code, body)
            return err
        written.append(crew)

    if all_public:
        return _json(200, {
            "agent": result.get("agent"), "crews": written,
            "model": result.get("model"), "target": result.get("target"),
            "decision": result.get("decision"), "acting_human": result.get("acting_human"),
            "version": result.get("version"),
        })
    return _json(200, {
        "agent": result.get("agent"), "crew": result.get("crew"),
        "model": result.get("model"), "target": result.get("target"),
        "decision": result.get("decision"), "acting_human": result.get("acting_human"),
        "version": result.get("version"),
    })


def _write_error(e: Exception) -> JSONResponse:
    """The response for a failed `library_client.write_policy` call."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 403:
            # NOT THE CALLER'S FAULT. Every gate above already passed, so a
            # 403 here means the Library rejected Orient's approved-client
            # credential itself (stale or unregistered in its
            # client-tokens.json) — a deployment fault dressed as a
            # permission one, the same reasoning `signal_capture_api.py`
            # applies to its own attested write.
            return _json(502, {"error": "the Library refused Orient's "
                                        "approved-client credential"})
        if e.code in (400, 422):
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            return _json(400, {"error": "the Library refused this write", "detail": detail[:500]})
        return _json(502, {"error": "the Library refused the write"})
    return _json(502, {"error": "the Library was unreachable"})
