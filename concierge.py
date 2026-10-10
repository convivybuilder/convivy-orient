"""The Reading Room concierge (ARCH-357 fast-follow) — a member's in-room edit loop.

A member types a request in their room; this turns it into an action plan via one
cheap structured-output call to claude-haiku-4-5, scoped to ONLY the member's
entitled catalog (input-side minimization — the model can reference no doc the
member can't already see, so a prompt injection has nothing out-of-grant to reach
for). The server re-validates every doc_id against the live grant before applying
(room_api), so the model's output is a proposal, never an authority. Grant
WIDENING is impossible in-loop: a request for something outside the grant is
denied and logged (room_api._log_grant_widening_request), not granted live.

The Anthropic key lives in a gitignored concierge.json read per request — NEVER
exported to the process env (ARCH-146). Absent/invalid config → the concierge is
disabled and room_api returns 503. The one model call goes through _call_model,
which tests monkeypatch so the suite never hits the network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from paths import CONFIG_ROOT

CONFIG = Path(os.environ.get("ORIENT_CONCIERGE_CONFIG", str(CONFIG_ROOT / "concierge.json")))

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5"
SNIPPET_CHARS = 200
MAX_CATALOG = 400   # bound the prompt for a very large full-tier grant
TIMEOUT_S = 20.0

# The action ops the concierge may propose. reorder/annotate/add operate on a
# doc_id; set_landing_note carries text only. Every doc_id is re-validated against
# the live grant server-side before anything is applied.
OPS = ("add", "remove", "reorder", "annotate", "set_landing_note")

# Strict structured-output schema (output_config.format). additionalProperties
# false everywhere; the server enforces the shape so the plan always parses.
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "op": {"type": "string", "enum": list(OPS)},
                    "doc_id": {"type": "string"},
                    "section": {"type": "string"},
                    "position": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["op"],
            },
        },
        "reply": {"type": "string"},
        "escalate": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "reason": {"type": "string"},
                        "wanted": {"type": "string"},
                    },
                    "required": ["reason", "wanted"],
                },
            ]
        },
    },
    "required": ["actions", "reply", "escalate"],
}

SYSTEM = (
    "You are the concierge for ONE team member's personal Reading Room — a curated "
    "set of company documents. The member types a request; you return an action plan "
    "that reorganizes their room.\n\n"
    "Rules:\n"
    "- You may ONLY reference documents present in the provided catalog. The catalog "
    "is the complete set of documents this member is allowed to see; never invent a "
    "doc_id or reference a document not in it.\n"
    "- Supported ops: add (doc_id, optional section/position), remove (doc_id), "
    "reorder (doc_id, section, position), annotate (doc_id, text), set_landing_note "
    "(text).\n"
    "- For an 'explain X' request, put the answer — grounded in the catalog snippets "
    "— in the reply field; do not fabricate. Optionally annotate a relevant doc.\n"
    "- If the member asks for a document NOT in the catalog, do NOT invent it: return "
    "empty actions and set escalate {reason, wanted} describing what they asked for. "
    "Tell them in reply that the request was sent for approval.\n"
    "- reply is a short, plain message shown to the member.\n"
    "Return only the structured plan."
)


def config() -> dict | None:
    """The concierge config from the gitignored concierge.json, or None when it's
    absent/invalid (concierge disabled). Read per call — never cached, never env."""
    try:
        raw = json.loads(CONFIG.read_text())
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    key = raw.get("api_key")
    if not isinstance(key, str) or not key.strip():
        return None
    model = raw.get("model")
    return {"api_key": key.strip(), "model": model if isinstance(model, str) and model else DEFAULT_MODEL}


def build_catalog(docs: list[dict]) -> list[dict]:
    """The member's entitled docs as a minimal catalog for the model: id, title,
    kind, project, and a short body snippet. This is the ONLY doc information the
    model sees — input-side minimization is the primary safety property."""
    return [
        {
            "doc_id": d["id"], "title": d["title"], "kind": d["kind"],
            "project": d["project"], "snippet": (d.get("body") or "")[:SNIPPET_CHARS],
        }
        for d in docs[:MAX_CATALOG]
    ]


def _call_model(cfg: dict, system: str, user_payload: str) -> dict:
    """The single Anthropic Messages call (claude-haiku-4-5, structured outputs).
    Monkeypatched in tests so the suite never hits the network."""
    resp = httpx.post(
        API_URL, timeout=TIMEOUT_S,
        headers={
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": cfg["model"], "max_tokens": 1024, "system": system,
            "messages": [{"role": "user", "content": user_payload}],
            "output_config": {"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        },
    )
    resp.raise_for_status()
    data = resp.json()
    text = next(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    return json.loads(text)


def interpret(cfg: dict, catalog: list[dict], room_state: dict, body: str) -> dict:
    """Turn a member request into a validated-shape action plan. Raises on a model
    or transport failure (room_api turns that into a failed RoomRequest + 502)."""
    user_payload = json.dumps(
        {"catalog": catalog, "room": room_state, "request": body}, ensure_ascii=False)
    return _call_model(cfg, SYSTEM, user_payload)
