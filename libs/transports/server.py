"""MCP server exposing the agent management tools.

Every tool returns a JSON string (matching the Node bridge's ``JSON.stringify``
behaviour) unless it fails, in which case ``CallToolResult.isError`` is set with
the message as the text.

The six classified tools are served over authenticated Streamable HTTP.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server

from ..core.config import runtime_config
from ..core.manager import Manager
from ..core.policy import KNOWN_PERMISSION_PRESETS
from ..core.platform import APP_NAME, APP_VERSION
from ..core.projection import project_list, receipt, status as project_status

INSTRUCTIONS = (
    "DSH MCP V2（6 个分类工具，参数路由）："
    "dsh_session=会话创建/列表/详情/恢复/命名/接管/关闭/停止/分叉/置顶/归档/队列；"
    "dsh_task=任务提交(可建会话)/列表/状态/等待/结果/中断/关闭/释放/注册回调；"
    "dsh_message=发消息(文本/图片/文件)/历史分页/上传/附件；"
    "dsh_runtime=运行状态/模型/provider/权限预设/后台作业/Skills 文件/创建选项；"
    "dsh_event=任务事件增量读取；"
    "dsh_workspace=工作区列表/新建/改名/删除。"
    "默认权限 workspace-write；MCP 不提供安装/删除插件、改全局配置、改凭据等危险操作；"
    "交互默认交回 Web；可显式启用单会话提问回答，权限审批仅可拒绝或交回 Web。"
)

_ID = {
    "type": "object",
    "properties": {"agent_id": {"type": "string", "minLength": 1}},
    "required": ["agent_id"],
    "additionalProperties": False,
}

_TOOL_DEFS: list[types.Tool] = [
    types.Tool(
        name="dsh_session",
        description=(
            "会话类操作（DSH Web 原生会话）："
            "create 新建空会话（cwd/workspace_id/name/preset/model/provider/permission）；"
            "list 列出 Web 会话；search 搜索历史（需原生索引启用）；detail 会话完整详情；"
            "resume 恢复会话（桥接任务 resume / Web 会话 attach）；attach 接管 Web 会话；"
            "rename 改名；close 关闭桥接记录；"
            "cancel 停止当前回合；fork 分叉；pin/unpin 置顶；archive/unarchive 归档/恢复；"
            "queue_edit/queue_remove/queue_steer 管理排队消息；interactions 查看交互，interaction_enable/interaction_disable 控制本连接接收，interaction_reply 回答普通问题或拒绝/交回审批；模型沿用 Web 实际选择。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "search", "detail", "resume", "attach", "rename", "close", "cancel", "fork", "pin", "unpin", "archive", "unarchive", "queue_edit", "queue_remove", "queue_steer", "interactions", "interaction_enable", "interaction_disable", "interaction_reply"]},
                "session_id": {"type": "string"},
                "cwd": {"type": "string"},
                "workspace_id": {"type": "string"},
                "name": {"type": "string"},
                "title": {"type": "string"},
                "query": {"type": "string", "maxLength": 2000},
                "effort": {"type": "string"},
                "preset": {"type": "string"},
                "model": {"type": "string"},
                "provider": {"type": "string"},
                "permission": {"type": "string", "enum": list(KNOWN_PERMISSION_PRESETS)},
                "at_seq": {"type": "integer"},
                "item_id": {"type": "string"},
                "content": {"type": "string"},
                "interaction_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["answer", "reject", "delegate"]},
                "confirm": {"type": "boolean"},
                "answers": {"type": "array", "maxItems": 50, "items": {"type": "object", "properties": {"id": {"type": "string"}, "selected": {"type": "array", "items": {"type": "string"}}, "custom": {"type": "string", "maxLength": 8000}}, "required": ["id", "selected"], "additionalProperties": False}},
                "stop_activity": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="dsh_task",
        description=(
            "任务类操作：submit 提交任务（session_id 省略=新建 Web 会话并执行；可带 name/cwd/workspace_id/model/provider/preset/permission）；"
            "list 任务列表；status 状态；wait 阻塞等待；result 最终结果；"
            "interrupt 中断；close 关闭；release 释放；watch/unwatch 完成结果文件（文件生成不等于客户端已收到主动推送）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["submit", "list", "status", "wait", "result", "interrupt", "close", "release", "watch", "unwatch", "reconnect"]},
                "task_id": {"type": "string", "description": "任务/会话 id"},
                "task": {"type": "string", "description": "submit 的任务描述 / result 不需要"},
                "request_id": {"type": "string", "maxLength": 128, "description": "提交的稳定去重 ID；重试必须复用同一值和相同参数"},
                "session_id": {"type": "string", "description": "submit 到已有会话"},
                "name": {"type": "string"},
                "cwd": {"type": "string"},
                "workspace_id": {"type": "string"},
                "model": {"type": "string"},
                "provider": {"type": "string"},
                "effort": {"type": "string"},
                "preset": {"type": "string"},
                "permission": {"type": "string", "enum": list(KNOWN_PERMISSION_PRESETS)},
                "seconds": {"type": "number", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "status": {"type": "string"},
                "cwd_match": {"type": "string"},
                "match": {"type": "string"},
                "output_dir": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="dsh_message",
        description=(
            "消息类操作：send 向会话发送消息（文本 text / base64 图片 images / 已上传文件 files[].receipt_id；mode queue|steer）；"
            "history 会话历史分页（before_seq/max_messages）；upload 上传本地文件取收据；attachment 读取会话内图片附件。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["send", "history", "upload", "attachment"]},
                "session_id": {"type": "string"},
                "text": {"type": "string"},
                "request_id": {"type": "string", "maxLength": 128},
                "mode": {"type": "string", "enum": ["queue", "steer"], "default": "queue"},
                "images": {"type": "array", "items": {"type": "object", "properties": {"media_type": {"type": "string", "enum": ["image/png", "image/jpeg", "image/webp", "image/gif"], "default": "image/png"}, "data": {"type": "string"}, "name": {"type": "string"}}, "required": ["data"], "additionalProperties": True}},
                "files": {"type": "array", "items": {"type": "object", "properties": {"receipt_id": {"type": "string"}}, "required": ["receipt_id"], "additionalProperties": True}},
                "path": {"type": "string", "description": "upload 的本地文件绝对路径"},
                "name": {"type": "string"},
                "attachment_id": {"type": "string"},
                "before_seq": {"type": "integer", "minimum": 1},
                "max_messages": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="dsh_runtime",
        description=(
            "运行环境查询（只读）：status 桥接/Web 运行状态（不含 token）；models 模型目录+provider；"
            "capabilities 权限预设/工作区/能力概览；control 主机级实时控制基线；jobs 某会话后台作业；"
            "skills 获取配套 Skill 完整文件供调用方安装，或 skills_source=runtime 查询授权会话的原生 Skill 目录；options 创建参数与本地默认设置。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "models", "capabilities", "control", "jobs", "skills", "options"]},
                "session_id": {"type": "string"},
                "skills_source": {"type": "string", "enum": ["bundled", "runtime"], "default": "bundled"},
                "skill_name": {"type": "string", "enum": ["dsh-mcp-bridge", "dsh-mcp-bridge-maintenance"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="dsh_event",
        description="事件查询：读取某任务的事件（增量 after；progress 含进度；tools 含工具事件；默认只最终答复）。",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "after": {"type": "integer", "minimum": 0},
                "progress": {"type": "boolean"},
                "tools": {"type": "boolean"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="dsh_workspace",
        description="工作区管理：list 基线（含会话分组/置顶/归档）；create 登记目录；rename 改名；delete 删除登记（保留文件与会话）；defaults 查询 V2 本地工作区默认值。",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "rename", "delete", "defaults"]},
                "path": {"type": "string"},
                "workspace_id": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
]


def _web_url(args: dict) -> str:
    return ""  # Credentials and instance selection belong to the connection layer.


async def _dispatch_impl(manager: Manager, name: str, args: dict) -> Any:
    web_url = _web_url(args)
    if name == "dsh_session":
        action = args.get("action")
        sid = str(args.get("session_id") or "").strip()
        if action == "create":
            url = web_url or manager.external._default_web_url()
            if not url:
                raise ValueError("需要先在面板连接 DSH Web")
            agent = await manager.external.create(
                url, str(args.get("cwd") or "").strip() or None,
                (args.get("name") or "").strip() or None, args.get("preset"),
                permission=args.get("permission"), model=args.get("model"),
                provider=args.get("provider"), effort=args.get("effort"),
                workspace_id=args.get("workspace_id"))
            manager.save(agent)
            manager.event(agent["id"], "bridge/created", {"via": "web-api"})
            value = project_status(agent)
        elif action == "list":
            rows = await manager.external.web_sessions(web_url, int(args.get("limit", 300)))
            bridged = {a.get("id") for a in manager.list() if a.get("external")}
            for row in rows:
                row["bridged"] = row.get("session_id") in bridged
            value = {"sessions": rows}
        elif action == "search":
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("search requires query")
            value = await manager.external.web_search(query, web_url)
        elif action == "detail":
            if not sid:
                raise ValueError("detail 需要 session_id")
            value = await manager.external.web_detail(sid, web_url)
        elif action in ("interactions", "interaction_enable", "interaction_disable", "interaction_reply"):
            if not sid:
                raise ValueError("interaction action requires session_id")
            try:
                agent = manager.get(sid)
            except KeyError:
                agent = await manager.attach(sid, manager.external._default_web_url())
            if not agent.get("external") or agent.get("status") == "closed":
                raise ValueError("Attach a live native session first")
            broker = manager.external.interactions
            if action == "interactions":
                value = await broker.listing(sid)
            elif action in ("interaction_enable", "interaction_disable"):
                value = await broker.configure(sid, action == "interaction_enable")
            else:
                value = await broker.reply(sid, str(args.get("interaction_id") or ""), args.get("decision"),
                                           args.get("answers"), args.get("confirm", False))
        elif action == "resume":
            if not sid:
                raise ValueError("resume 需要 session_id")
            try:
                agent = manager.get(sid)
            except KeyError:
                agent = None
            if agent is not None and not agent.get("external"):
                value = project_status(await manager.resume(sid, args.get("name") or ""))
            else:
                value = project_status(await manager.attach(sid, web_url or manager.external._default_web_url()))
        elif action == "attach":
            value = project_status(await manager.attach(
                sid, web_url or manager.external._default_web_url()))
        elif action == "rename":
            if not sid:
                raise ValueError("rename 需要 session_id + title")
            title = args.get("title") or args.get("name") or ""
            if not title:
                raise ValueError("rename 需要 title")
            try:
                agent = manager.get(sid)
            except KeyError:
                agent = None
            if agent is not None and not agent.get("external"):
                value = project_status(await manager.rename(sid, title))
            else:
                value = await manager.external.web_rename(sid, title, web_url)
        elif action == "close":
            value = project_status(await manager.close(sid))
        elif action == "cancel":
            if not sid:
                raise ValueError("cancel 需要 session_id")
            try:
                agent = manager.get(sid)
            except KeyError:
                agent = None
            if agent is not None and not agent.get("external"):
                value = project_status(await manager.interrupt(sid))
            else:
                value = await manager.external.web_cancel(sid, web_url)
        elif action == "fork":
            value = await manager.external.web_fork(sid, args.get("at_seq"), web_url)
        elif action == "pin":
            value = await manager.external.web_pin(sid, True, web_url)
        elif action == "unpin":
            value = await manager.external.web_pin(sid, False, web_url)
        elif action == "archive":
            value = await manager.external.web_archive(
                sid, True, bool(args.get("stop_activity", False)), web_url)
        elif action == "unarchive":
            value = await manager.external.web_archive(sid, False, False, web_url)
        elif action in ("queue_edit", "queue_remove", "queue_steer"):
            kind = {"queue_edit": "edit", "queue_remove": "remove", "queue_steer": "steer"}[action]
            value = await manager.external.web_queue(
                sid, args["item_id"], kind, args.get("content"), web_url)
        else:
            raise ValueError(f"unknown dsh_session.action={action}")
    elif name == "dsh_task":
        action = args.get("action")
        tid = str(args.get("task_id") or "").strip()
        if action == "submit":
            task = str(args.get("task") or "")
            if not task.strip():
                raise ValueError("submit 需要 task")
            target = str(args.get("session_id") or "").strip() or tid
            if target:
                row = await manager.external.web_execution_target(target, web_url)
                for field in ("permission", "model", "provider", "effort"):
                    if args.get(field) and row.get(field) != args[field]:
                        raise ValueError("Existing session {} does not match request".format(field))
                try:
                    agent = manager.get(target)
                except KeyError:
                    agent = None
                if agent is not None and not agent.get("external"):
                    value = await manager.followup(target, task)
                else:
                    value = await manager.external.web_send(target, task, "queue", web_url)
            else:
                params: dict = {"task": task}
                for key in ("name", "cwd", "workspace_id", "model", "provider", "effort", "preset", "permission"):
                    if args.get(key) not in (None, ""):
                        params[key] = args[key]
                agent = await manager.start(params)
                value = receipt(agent, "submit")
        elif action == "list":
            rows, summary = project_list(
                manager.list(), full=False,
                limit=int(args.get("limit", 20)), state=args.get("status"),
                cwd=args.get("cwd_match") or args.get("cwd"), match=args.get("match"))
            value = {"tasks": rows, "summary": summary}
        elif action == "status":
            value = project_status(manager.get(tid))
        elif action == "wait":
            value = await manager.wait(tid, args.get("seconds"))
        elif action == "result":
            value = manager.public_events(tid)
        elif action == "interrupt":
            value = project_status(await manager.interrupt(tid))
        elif action == "close":
            value = project_status(await manager.close(tid))
        elif action == "release":
            value = await manager.release(tid)
        elif action == "reconnect":
            # 连接恢复（≠执行恢复）：MCP 重启后重新关联正在运行的 Web 任务
            if not tid:
                raise ValueError("reconnect 需要 task_id")
            try:
                agent = manager.get(tid)
            except KeyError:
                agent = None
            if agent is not None and not agent.get("external"):
                value = project_status(agent)
            else:
                value = project_status(await manager.attach(
                    tid, web_url or manager.external._default_web_url()))
        elif action == "watch":
            value = manager.watch(tid)
        elif action == "unwatch":
            value = await manager.unwatch(tid, args.get("output_dir"))
        else:
            raise ValueError(f"unknown dsh_task.action={action}")
    elif name == "dsh_message":
        action = args.get("action")
        sid = str(args.get("session_id") or "").strip()
        if action == "send":
            if not sid:
                raise ValueError("send 需要 session_id")
            value = await manager.external.web_send(
                sid, args.get("text") or "", args.get("mode", "queue"),
                web_url, args.get("images"), args.get("files"))
        elif action == "history":
            value = await manager.external.web_history(
                sid, before_seq=args.get("before_seq"),
                max_messages=int(args.get("max_messages", 200)), web_url=web_url)
        elif action == "upload":
            value = await manager.external.web_upload(
                sid, args["path"], args.get("name"), web_url)
        elif action == "attachment":
            value = await manager.external.web_attachment(
                sid, args["attachment_id"], web_url)
        else:
            raise ValueError(f"unknown dsh_message.action={action}")
    elif name == "dsh_runtime":
        action = args.get("action")
        if action == "status":
            from ..core.platform import APP_VERSION, locations
            from ..core import runtime021
            from . import web_launch
            try:
                web_status = web_launch.status(locations().state, probe=True)
            except Exception:  # noqa: BLE001
                web_status = {}
            web = {
                "connected": bool(web_url or manager.external._default_web_url()),
                "running": web_status.get("running"),
                "managed": web_status.get("managed"),
                "port": web_status.get("port"),
                "token_valid": web_status.get("token_valid"),
                "dsh_version": web_status.get("version"),
            }
            value = {"version": APP_VERSION, "web": web, "runtime_mode": runtime021.mode(),
                     "web_format": runtime021.web_session_format(),
                     "interaction_support": {"ordinary_questions": True, "permission_grants": False,
                                             "explicit_session_enable": True, "message_modes": ["queue", "steer"]}}
        elif action == "models":
            value = {"catalog": await manager.external.web_model_catalog(web_url),
                     "providers": await manager.external.web_providers(web_url)}
        elif action == "capabilities":
            value = {"permissions": await manager.external.web_permission_presets(web_url),
                     "workspaces": await manager.external.web_workspaces(web_url),
                     "inventory": await manager.external.web_inventory(web_url)}
        elif action == "control":
            value = await manager.external.web_control(web_url)
        elif action == "jobs":
            value = await manager.external.web_jobs(args["session_id"], web_url)
        elif action == "skills":
            if args.get("skills_source", "bundled") == "runtime":
                value = {"source": "runtime", **await manager.external.web_skills(args.get("session_id") or "", web_url)}
            else:
                from ..core.skill_catalog import catalog
                value = catalog(args.get("skill_name"))
        elif action == "options":
            from ..core.preferences import snapshot
            value = {"fields": ["cwd", "workspace_id", "name", "preset", "permission", "provider", "model", "effort"],
                     "precedence": ["explicit", "workspace defaults", "V2 defaults"],
                     "allowed_permissions": list(KNOWN_PERMISSION_PRESETS),
                     "defaults": snapshot(state=Path(manager.config.database).parent),
                     "permissions": await manager.external.web_permission_presets(web_url),
                     "models": await manager.external.web_model_catalog(web_url),
                     "presets": (await manager.external.web_inventory(web_url)).get("agent_presets", []),
                     "model_selection": "Required effective match; never changes the Web global default"}

        else:
            raise ValueError(f"unknown dsh_runtime.action={action}")
    elif name == "dsh_event":
        tid = str(args.get("task_id") or "")
        value = manager.public_events(
            tid, after=int(args.get("after", 0)),
            include_progress=bool(args.get("progress")),
            include_tool_events=bool(args.get("tools")))
    elif name == "dsh_workspace":
        action = args.get("action")
        if action == "defaults":
            from ..core.preferences import snapshot
            value = snapshot(args.get("path"), state=Path(manager.config.database).parent)
        elif action == "list":
            value = await manager.external.web_workspaces(web_url)
        elif action == "create":
            value = await manager.external.web_workspace_create(args["path"], web_url)
        elif action == "rename":
            value = await manager.external.web_workspace_rename(
                args["workspace_id"], args["title"], web_url)
        elif action == "delete":
            value = await manager.external.web_workspace_delete(args["workspace_id"], web_url)
        else:
            raise ValueError(f"unknown dsh_workspace.action={action}")
    else:
        raise KeyError(f"Unknown tool: {name}")
    return value


def _log_mcp_call(manager: Manager, name: str, args: dict, ok: bool = True, error: str = "") -> None:
    """轻量 MCP 调用日志（本地审计，供面板「调用记录」展示；静默失败）。"""
    try:
        from datetime import datetime, timezone
        state = Path(manager.config.database).parent
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": name,
            "session_id": str(args.get("session_id") or args.get("task_id") or args.get("agent_id") or ""),
            "ok": bool(ok),
            "error": str(error)[:160],
        }
        with (state / "mcp_calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + chr(10))
    except Exception:  # noqa: BLE001
        pass


async def _dispatch_inner(manager: Manager, name: str, args: dict) -> Any:
    """Serialize session commands and persist explicit delivery identities."""
    if name not in {tool.name for tool in _TOOL_DEFS}:
        raise KeyError("Unknown tool: " + name)
    request_id = str(args.get("request_id") or "").strip()
    mutating = (name == "dsh_task" and args.get("action") == "submit") or (name == "dsh_message" and args.get("action") == "send")
    if name in ("dsh_runtime", "dsh_event") or args.get("action") in ("list", "detail", "status", "wait", "result", "history", "attachment"):
        return await _dispatch_impl(manager, name, args)
    sid = str(args.get("session_id") or args.get("task_id") or "create")
    async with manager.lock("mcp-session:" + sid):
        if not request_id or not mutating:
            return await _dispatch_impl(manager, name, args)
        if len(request_id) > 128:
            raise ValueError("request_id exceeds 128 characters")
        payload = {k: v for k, v in args.items() if k not in ("request_id", "web_url")}
        digest = hashlib.sha256(json.dumps([name, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        manager.db.execute("CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT)")
        inserted = manager.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?, NULL)", (request_id, digest)).rowcount
        manager.db.commit()
        if not inserted:
            old_digest, response = manager.db.execute("SELECT digest,response FROM deliveries WHERE id=?", (request_id,)).fetchone()
            if old_digest != digest:
                raise ValueError("request_id was already used with different arguments")
            if response is None:
                raise ValueError("Previous delivery outcome is unknown; inspect/reconnect the original session before resubmitting")
            return json.loads(response)
        value = await _dispatch_impl(manager, name, args)
        manager.db.execute("UPDATE deliveries SET response=? WHERE id=?", (json.dumps(value, ensure_ascii=False), request_id))
        manager.db.commit()
        return value


async def _dispatch(manager: Manager, name: str, args: dict) -> Any:
    """带审计日志的 dispatch 入口（成功/失败都记录，失败仍原样抛出）。"""
    try:
        value = await _dispatch_inner(manager, name, args)
    except Exception as error:  # noqa: BLE001
        _log_mcp_call(manager, name, args, ok=False, error=str(error))
        raise
    _log_mcp_call(manager, name, args, ok=True)
    return value


async def _push_notification(session, payload: dict, related_request_id=None) -> None:
    """Route HTTP notices to the active request stream, not an absent GET stream."""
    from ..core import notify
    message = notify.mcp_message(payload)
    await session.send_log_message(
        level=message["level"], logger=message["logger"], data=message["data"],
        related_request_id=str(related_request_id) if related_request_id is not None else None,
    )


def _record_call(manager: Manager, name: str, agent_id: str, args: dict) -> None:
    """Record a tool invocation for the panel's calls view (best effort)."""
    try:
        manager.event(agent_id or "bridge", "tool/invoke", {
            "tool": name,
            "args": {k: str(v)[:120] for k, v in (args or {}).items()},
        })
    except Exception:  # noqa: BLE001
        pass


def build_server(manager: Manager) -> Server:
    server = Server(APP_NAME, version=APP_VERSION, instructions=INSTRUCTIONS)
    import weakref
    notified_sessions = weakref.WeakSet()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _TOOL_DEFS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        # P0-1: remember this stdio session so settled tasks can be pushed as
        # MCP ``notifications/message`` instead of relying on polling.
        context = None
        is_http = False
        try:
            context = server.request_context
            session = context.session
            is_http = getattr(context, "request", None) is not None or getattr(context, "close_sse_stream", None) is not None
            if not is_http and session not in notified_sessions:
                reference = weakref.ref(session)
                async def on_settled(payload):
                    current = reference()
                    if current is not None:
                        await _push_notification(current, payload)
                manager.register_notifier(on_settled)
                notified_sessions.add(session)
        except Exception:  # noqa: BLE001 - notifications are best effort
            pass
        try:
            args = dict(arguments or {})
            # Stateless HTTP has no standing downstream GET stream. An active
            # wait receives its own completion notice before the tool response.
            wait_active = False
            if is_http and name == "dsh_task" and args.get("action") == "wait":
                from ..core.manager import ACTIVE
                wait_active = manager.get(str(args.get("task_id") or "")).get("status") in ACTIVE
            value = await _dispatch(manager, name, args)
            if wait_active and isinstance(value, dict) and value.get("wait_outcome") == "settled" and value.get("status") in ("completed", "error", "interrupted", "context_exhausted"):
                import asyncio
                notice_status = "not-sent"
                try:
                    await asyncio.wait_for(_push_notification(context.session, value, context.request_id), timeout=1.0)
                    notice_status = "sent-to-request-stream"
                except Exception:
                    pass
                # Telemetry must never turn a completed wait into a tool error.
                try:
                    manager.event(value["id"], "bridge/notification", {"channel": "mcp-http", "status": notice_status})
                except Exception:
                    pass
            text = json.dumps(value, ensure_ascii=False, default=str)
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=False)
        except Exception as error:  # noqa: BLE001 - tools report their own failures
            import re
            message = re.sub(r"(?i)(token=)[^&\s]+", r"\1[REDACTED]", str(error))
            message = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~-]+", r"\1[REDACTED]", message)
            code = "invalid_argument" if isinstance(error, (ValueError, KeyError)) else "operation_failed"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False))],
                isError=True,
            )

    return server
