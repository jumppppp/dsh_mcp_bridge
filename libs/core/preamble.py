"""Preamble injected in front of every delegated task.

Why: MCP-driven sessions are unattended. DSH rejects approval-requiring actions
when no answerer exists (``ask`` fails closed), so the model must be told up
front to avoid asking, to prefer safe alternatives, and — under
``danger-full-access`` — to never run system-destructive commands.

The template is user-editable from the panel and stored in
``state/preamble.json``; ``{placeholders}`` are filled per request.
"""

from __future__ import annotations

import json
from pathlib import Path

SCOPE_NEW = "new_session"      # 只在 dsh_start（新对话）注入
SCOPE_EVERY = "every_request"  # 每次请求（含 followup/resume）都注入
DEFAULT_SCOPE = SCOPE_NEW

DEFAULT_TEMPLATE = """[系统前置约束 · MCP 无人值守会话]
1. 本会话由 MCP 自动调用，没有交互界面：不要请求审批、不要等待用户确认、不要使用需要人机交互的工具。
2. 需要审批（approval）的操作会被自动拒绝；请改用安全替代方案，或在最终答复中说明"需要人工执行"及其命令与回滚方式。
3. 当前权限：{permission}。{permission_note}
4. 不要执行任何破坏系统或不可逆的操作（删除系统文件、改注册表/启动项、格式化、批量删除、关闭安全软件等）。
5. 任务结束时，在答复末尾给出「后续可选项」（最多 3 条），供调用方选择；不要在过程中提问。

[任务]
"""

NOTE_DANGER = ("本会话为高危权限 danger-full-access：任何影响系统安全与稳定性的高危命令"
               "（删除系统目录、修改注册表/启动项、终止关键进程、修改防火墙、批量删除、格式化等）"
               "一律禁止自行执行；如确有必要，只能在答复中说明命令、影响与回滚方式，等待人工授权。")
NOTE_WRITE = "只能修改工作目录（{cwd}）内的文件；不要写入系统目录或工作目录之外的位置。"
NOTE_READ = "只读权限：不得修改或删除任何文件，也不要尝试提权。"
NOTE_DEFAULT = "请遵守当前权限边界，不要越权操作。"


def preamble_file() -> Path:
    from .platform import locations

    return locations().state / "preamble.json"


def load_preamble() -> dict:
    path = preamble_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("template"), str):
                scope = data.get("scope") or DEFAULT_SCOPE
                if scope not in (SCOPE_NEW, SCOPE_EVERY):
                    scope = DEFAULT_SCOPE
                return {
                    "template": data["template"],
                    "enabled": bool(data.get("enabled", True)),
                    "scope": scope,
                }
        except (OSError, ValueError):
            pass
    return {"template": DEFAULT_TEMPLATE, "enabled": True, "scope": DEFAULT_SCOPE}


def save_preamble(template: str, enabled: bool = True, scope: str = DEFAULT_SCOPE) -> dict:
    from .platform import write_json

    if scope not in (SCOPE_NEW, SCOPE_EVERY):
        scope = DEFAULT_SCOPE
    payload = {"template": template, "enabled": bool(enabled), "scope": scope}
    write_json(preamble_file(), payload)
    return payload


def render(permission: str | None, cwd: str, template: str | None = None) -> str:
    """Fill placeholders for one request."""
    data = load_preamble()
    if not data.get("enabled", True):
        return ""
    body = template if template is not None else data["template"]
    if permission == "danger-full-access":
        note = NOTE_DANGER
    elif permission == "read-only":
        note = NOTE_READ
    elif permission == "workspace-write":
        note = NOTE_WRITE.format(cwd=cwd)
    else:
        note = NOTE_DEFAULT
    return body.replace("{permission}", permission or "workspace-write").replace(
        "{permission_note}", note
    ).replace("{cwd}", cwd)


def apply(task: str, permission: str | None, cwd: str) -> str:
    """Return ``preamble + task`` (or the bare task when disabled)."""
    prefix = render(permission, cwd)
    if not prefix.strip():
        return task
    return prefix + task