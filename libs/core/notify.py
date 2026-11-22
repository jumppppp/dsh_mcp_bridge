"""Completion notifications (P0-1): MCP notification, webhook, and file sinks.

The original dsh-subagent-mcp could only signal completion through the Codex
App Server. This module makes completion delivery transport-agnostic:

* **MCP** — completion notices use the active HTTP wait request stream;
  memory/persistent sessions can receive a session notice (server.py).
* **Webhook** — any HTTP endpoint can be configured in ``state/webhook.json``
  (``{"url": "...", "enabled": true, "secret": "..."}``).
* **File** — ``dsh_task(action="watch")`` writes a result file; that alone
  is not evidence that a remote client received an active push.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path
from typing import Any, Callable

WEBHOOK_FILE_NAME = "webhook.json"


def webhook_file(state: Path) -> Path:
    return state / WEBHOOK_FILE_NAME


def load_webhook(state: Path) -> dict:
    path = webhook_file(state)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("url"), str):
                return {
                    "url": data["url"],
                    "enabled": bool(data.get("enabled", True)),
                    "secret": data.get("secret") or "",
                    "events": data.get("events") or ["completed", "error", "interrupted"],
                }
        except (OSError, ValueError):
            pass
    return {"url": "", "enabled": False, "secret": "", "events": ["completed", "error", "interrupted"]}


def save_webhook(state: Path, url: str, enabled: bool = True, secret: str = "") -> dict:
    payload = {"url": url, "enabled": bool(enabled), "secret": secret,
               "events": ["completed", "error", "interrupted"]}
    webhook_file(state).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_payload(agent: dict) -> dict:
    """One notification body shared by every sink."""
    from .diagnostics import error_message
    return {
        "event": "task.settled",
        "agent_id": agent.get("id"),
        "name": agent.get("name"),
        "status": agent.get("status"),
        "answer": (agent.get("answer") or agent.get("last_completed_answer") or "")[:4000],
        "error": error_message(agent),
        "finish_reason": agent.get("finish_reason"),
        "task_completed_at": agent.get("settled_at"),
        "task_completed_at_source": agent.get("settled_at_source"),
        "cwd": agent.get("cwd"),
        "model": agent.get("model"),
        "updated_at": agent.get("updated_at"),
    }


async def post_webhook(state: Path, agent: dict) -> bool:
    """Deliver one settled task to the configured webhook (best effort)."""
    config = load_webhook(state)
    if not config.get("enabled") or not config.get("url"):
        return False
    if agent.get("status") not in (config.get("events") or []):
        return False
    body = json.dumps(build_payload(agent), ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json; charset=utf-8"}
    if config.get("secret"):
        headers["x-dsh-secret"] = config["secret"]

    def _send() -> bool:
        try:
            request = urllib.request.Request(config["url"], data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=8) as response:
                return 200 <= response.status < 300
        except Exception:  # noqa: BLE001 - webhook failures never affect tasks
            return False

    return await asyncio.to_thread(_send)


def mcp_message(agent: dict) -> dict:
    """Payload shape for MCP ``notifications/message``."""
    return {
        "level": "info" if agent.get("status") == "completed" else "warning",
        "logger": "dsh-subagent",
        "data": build_payload(agent),
    }


def register_notifier(notifiers: list[Callable[[dict], Any]], fn: Callable[[dict], Any]) -> None:
    """Register once (identity-based) — one stdio session must not double-deliver."""
    for existing in notifiers:
        if existing is fn:
            return
    notifiers.append(fn)