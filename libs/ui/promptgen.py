"""Generate current-schema, credential-free instructions for an MCP client."""
from pathlib import Path
from typing import Any

from ..core.policy import KNOWN_PERMISSION_PRESETS

def tool_catalog():
    from ..transports.server import _TOOL_DEFS
    return [(tool.name, tool.description or "") for tool in _TOOL_DEFS]

def project_dir_default():
    return str(Path(__file__).resolve().parents[2])

def default_options():
    from ..core.preferences import resolve
    from ..transports.httpd import address
    host,port=address()
    return {**resolve({}),"project_dir":project_dir_default(),"session_id":"","session_title":"",
            "transport":"http","http_url":f"http://{host}:{port}/mcp"}

def resolve_options(options: dict[str,Any], defaults=None):
    from ..core.preferences import resolve
    base=default_options()
    chosen={key:value.strip() for key,value in options.items() if isinstance(value,str) and value.strip()}
    fallback={**(defaults or {}),**chosen}
    launch=resolve(fallback)
    resolved={**base,**launch,**chosen}
    if resolved.get("permission") not in KNOWN_PERMISSION_PRESETS:
        raise ValueError("Unsupported permission")
    return resolved,{key:("本次选择" if key in chosen else "工作区默认") for key in resolved}

def _install_block(project_dir,transport="http",http_url="http://127.0.0.1:8770/mcp"):
    if transport!="http":
        raise ValueError("V2 supports only Streamable HTTP")
    import json
    return f"""## 一、接入（一次性）

业务控制直接调用 DSH Web 的接口，不模拟页面点击。
先在程序目录运行 `python main.py panel`，在面板「服务管理」启动或连接 DSH Web。
MCP 地址：{http_url}。连接认证由客户端保存，任务参数不传 Web token。

Codex 配置示例：

```toml
[mcp_servers.dsh_mcp_bridge]
url = {json.dumps(http_url)}
bearer_token_env_var = "DSH_MCP_TOKEN"
```

由用户在客户端启动环境中安全设置 DSH_MCP_TOKEN，值来自程序自己的受限状态目录中的 http.token。
不要把实际值贴进对话、提示词或日志，也不要覆盖客户端其他 MCP 配置。
其他 MCP 客户端使用 Streamable HTTP 地址和 Bearer 认证即可。

配置格式参考：https://learn.chatgpt.com/docs/extend/mcp?surface=cli
"""

def _tools_block():
    rows=["## 二、工具能力（六类路由）","","| 工具 | 用途 |","|---|---|"]
    rows += [f"| `{name}` | {description} |" for name,description in tool_catalog()]
    return "\n".join(rows)

def _rules_block():
    return """## 三、使用规则

1. 先 dsh_runtime(action="status") 确认 Web 可用，再 dsh_runtime(action="options") 查看真实创建选项。
2. 调用 dsh_runtime(action="skills", skill_name="dsh-mcp-bridge") 获取完整 Skill；按用户授权安装到调用方工作区 .agents/skills 下。服务只提供文件，不自动安装或执行。
3. 提交逻辑消息时提供稳定 request_id；同一次提交重试必须复用 ID 和相同参数。
4. 保存 submit 返回的 agent_id，后续作为 task_id。使用 wait 阻塞等待，timeout 不等于失败。
5. disconnected 时 reconnect 原任务；delivery_unknown 先查状态和历史，不能重新提交造成重复执行。
6. 模型字段是有效模型的匹配要求；当前 DSH 会话模型切换会改全局默认，MCP 不调用它。不匹配时报告原因，不能放宽要求偷偷执行。
7. 默认工作目录是程序目录/workspace，可显式传 cwd 或 workspace_id。只在用户授权范围执行，权限仅 read-only/workspace-write（其他预设须与 DSH 环境一致）。
8. 用 dsh_message.history 读取原生分页历史，用 dsh_event 查询 after 游标后的进度事件，不反复拉取全量历史。
9. 插件、MCP、Skills 复用 DSH 会话实际加载的能力；目录条目不代表所有工具都已实际调用。
10. 不改全局配置、凭据和插件安装，不代替人批准高危请求；验收实际产物，中断不回滚文件。
11. 2.0.2 支持 dsh_session.interactions 查询交互；需接收后续交互时显式 interaction_enable。wait 返回 interaction_needed 时查询请求，普通问题可用 interaction_reply 精确回答；审批只可拒绝或交回 Web，计划授权由人处理。请求过期、已提交或回执不明时不重放，不按第一项自动选择。
12. dsh_message.send 的 mode=queue 为排队，mode=steer 为插话；插话也要等待原生执行边界，提交成功不等于已执行。
"""

def build_prompt(options,defaults=None):
    values,sources=resolve_options(options,defaults)
    task=str(options.get("task") or "<任务目标、范围和验收标准>").strip()
    rows=["# 接入并使用 DSH MCP V2","",_install_block(values["project_dir"],values["transport"],values["http_url"]),
          _tools_block(),"",_rules_block(),"","## 四、本次可用范围","","| 设置 | 要求 | 来源 |","|---|---|---|"]
    for key,label in (("cwd","工作目录"),("permission","权限"),("preset","Agent 预设"),("provider","模型服务商"),("model","模型匹配要求"),("effort","思考强度要求")):
        rows.append(f"| {label} | `{values.get(key) or '沿用实际设置'}` | {sources.get(key,'工作区默认')} |")
    rows += ["","### 任务","",task,"","### 建议调用序列","","```text"]
    sid=values.get("session_id")
    if sid:
        rows += [f"dsh_session(action='detail', session_id={sid!r})",
                 "# 核对会话的实际工作区、模型与权限；不使用新会话默认值覆盖已有会话",
                 f"dsh_message(action='send', session_id={sid!r}, text='<任务>', request_id='<本次消息稳定ID>')",
                 f"dsh_task(action='wait', task_id={sid!r}, seconds=30)"]
    else:
        params=["action='submit'","task='<上面的任务>'","request_id='<本次任务稳定ID>'"]
        for key in ("cwd","provider","model","permission","preset","effort"):
            if values.get(key):
                params.append(f"{key}={values[key]!r}")
        rows += ["receipt = dsh_task("+", ".join(params)+")",
                 "# 保存 receipt.agent_id，作为后续 task_id",
                 "dsh_task(action='wait', task_id='<返回的agent_id>', seconds=30)",
                 "dsh_task(action='result', task_id='<返回的agent_id>')",
                 "dsh_message(action='history', session_id='<返回的agent_id>', max_messages=100)"]
    rows += ["```","","不要因等待超时重建任务。需要恢复观察连接时调用 dsh_task(action='reconnect', task_id=原ID)。"]
    return "\n".join(rows)

def prompt_json(options):
    return {"prompt":build_prompt(options),"tools":[{"name":n,"description":d} for n,d in tool_catalog()]}
