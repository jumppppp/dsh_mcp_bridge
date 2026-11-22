"""Compact projections of agent and event records for tool responses.

Every MCP tool returns a small, stable JSON object instead of exposing raw
internal rows. The same shapes are used by ``dsh-list`` (array of rows) and the
CLI, so outputs stay consistent across transports.
"""

from __future__ import annotations

import json
from typing import Any

# Fields always present in a compact row. ``external`` tells MCP clients the
# agent is a DSH Web session (visible in the Web UI; use dsh_send semantics).
_COMPACT_FIELDS = (
    "id", "name", "status", "cwd", "provider", "model", "effort", "preset",
    "permission", "created_at", "updated_at", "last_event", "message_id",
    "persisted", "finish_reason", "error", "last_completed_answer",
    "context_tokens", "answer", "partial_text", "external", "interrupted_by",
    "connection_error", "delivery_unknown", "requested_permission",
    "finish_reason_code", "settled_at", "settled_at_source", "pending_interactions",
)

_REDACTED = object()


def compact(agent: dict, full: bool = False) -> dict:
    from .diagnostics import enrich
    agent = enrich(agent)
    if full:
        return dict(agent)
    return {k: agent.get(k) for k in _COMPACT_FIELDS if k in agent}


def status(agent: dict, full: bool = False) -> dict:
    return compact(agent, full=full)


def receipt(agent: dict, action: str) -> dict:
    """Short acknowledgment returned by start/followup etc."""
    return {
        "agent_id": agent.get("id"),
        "action": action,
        "status": agent.get("status"),
        "name": agent.get("name"),
        "external": bool(agent.get("external")),
    }


# -- event projection -------------------------------------------------------

def _budget_text(text: str, limit: int) -> dict:
    """Truncate a long event field, reporting the omitted tail via cursor."""
    if len(text) <= limit:
        return {"text": text}
    return {"text": text[:limit], "truncated": True}


# Event types that always belong to a progress view: turn boundaries, the
# assistant's visible text, and permission/sandbox/approval decisions.
_PROGRESS_PREFIXES = (
    "turn/start", "turn/end", "assistant/message",
    "permission/", "sandbox/", "approval/",
    "error", "bridge/callback", "bridge/notification",
)

# Tool and step activity is verbose and only shown when explicitly requested.
_TOOL_PREFIXES = ("tool/", "step/")


def project_event(row: dict, only_finals: bool, include_tool_events: bool,
                  max_chars: int) -> dict | None:
    kind = row.get("type") or ""
    data = row.get("data") or {}

    if only_finals:
        # Readers of "final answers" only need completed turn/end rows, with the
        # assistant's final text attached from the previous assistant/message.
        if kind == "turn/end" and (data.get("reason") or {}).get("kind") == "completed":
            return {
                "event_id": row["seq"],
                "seq": row["seq"],
                "time": row.get("time"),
                "type": kind,
                "finish_reason": data.get("reason"),
                "text": _budget_text(str(data.get("final_text") or ""), max_chars),
            }
        return None

    if kind.startswith(_PROGRESS_PREFIXES):
        if kind == "assistant/message":
            text = str(data.get("text") or "")
            if not text:
                return None
            return {
                "event_id": row["seq"],
                "seq": row["seq"],
                "time": row.get("time"),
                "type": kind,
                "text": _budget_text(text, max_chars),
                "usage": data.get("usage"),
            }
        return {
            "event_id": row["seq"],
            "seq": row["seq"],
            "time": row.get("time"),
            "type": kind,
            "data": json.dumps(data, ensure_ascii=False, default=str)[:max_chars],
        }

    if include_tool_events and kind.startswith(_TOOL_PREFIXES):
        return {
            "event_id": row["seq"],
            "seq": row["seq"],
            "time": row.get("time"),
            "type": kind,
            "data": json.dumps(data, ensure_ascii=False, default=str)[:max_chars],
        }

    return None


def project_events(rows: list[dict], *, only_finals: bool, include_tool_events: bool,
                   max_chars: int) -> tuple[list[dict], int]:
    """Project DB event rows into response items + next cursor (max seq)."""
    out: list[dict] = []
    last_seq = 0
    for row in rows:
        last_seq = max(last_seq, row.get("seq", 0))
        item = project_event(row, only_finals, include_tool_events, max_chars)
        if item is not None:
            out.append(item)
    return out, last_seq


# -- list projection --------------------------------------------------------

def project_list(agents: list[dict], *, full: bool = False, limit: int = 20,
                 state: str | None = None, cwd: str | None = None,
                 match: str | None = None) -> tuple[list[dict], dict]:
    """Filter + compact agents for ``dsh_list``; returns (rows, row_summary)."""
    matched = agents
    if state:
        matched = [a for a in matched if a.get("status") == state]
    if cwd:
        matched = [a for a in matched if (a.get("cwd") or "") == cwd]
    if match:
        match_lower = match.lower()
        matched = [
            a for a in matched
            if match_lower in (a.get("name") or "").lower()
            or match_lower in (a.get("cwd") or "").lower()
            or (a.get("id") or "").startswith(match)
        ]

    def sort_key(agent: dict) -> str:
        # Newest activity first: order purely by updated_at so the most
        # recently touched task is always on top.
        return agent.get("updated_at") or ""

    rows = [compact(a, full=full) for a in sorted(matched, key=sort_key, reverse=True)]
    omitted = max(0, len(rows) - limit)
    rows = rows[:limit]

    summary = {
        "total_stored": len(agents),
        "matched": len(matched),
        "rows": len(rows),
        "omitted": omitted,
        "limit": limit,
        "filter": {
            "status": state,
            "cwd": cwd,
            "match": match,
            "full": full,
        },
    }
    return rows, summary
