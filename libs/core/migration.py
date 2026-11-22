"""跨机迁移校验与安全修复。

目标：换机后「还原备份 → 跨机校验 → 修复可自动修复项 → 直接运行」。
只做本地安全操作：建目录、重置桥接 token、清理不可解密的 DPAPI 凭据；
安装 DSH/Node 等系统级操作只给出命令，不自动执行。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import zipfile
from pathlib import Path

from . import dsh_pack


def _current_env() -> dict:
    return dsh_pack.environment()


def _read_manifest(archive: Path) -> dict:
    with zipfile.ZipFile(Path(archive)) as bundle:
        return json.loads(bundle.read("manifest.json").decode("utf-8"))


def _registration_state() -> dict:
    """桥接注册配置：serverName / Authorization / token 文件是否一致。"""
    home = dsh_pack.dsh_home()
    patch = home / "profiles" / "web" / "cordis.patch.yml"
    token_file = dsh_pack.state_dir() / "http.token"
    token = token_file.read_text(encoding="utf-8").strip() if token_file.is_file() else ""
    text = patch.read_text(encoding="utf-8") if patch.is_file() else ""
    registered = "serverName: dsh_mcp_bridge" in text
    match = re.search(r"Authorization:\s*'Bearer\s+([^']+)'", text)
    configured = match.group(1) if match else ""
    return {"patch": str(patch), "patch_exists": patch.is_file(),
            "token_file_exists": token_file.is_file(), "token": token,
            "token_configured": configured, "registered": registered,
            "matches": bool(token) and token == configured}


def _credential_state() -> dict:
    from .secrets import read_secret_json

    launch = dsh_pack.state_dir() / "web_launch.json"
    if not launch.is_file():
        return {"present": False, "readable": False}
    try:
        data = read_secret_json(launch)
        return {"present": True, "readable": isinstance(data, dict) and bool(data)}
    except Exception:  # noqa: BLE001
        return {"present": True, "readable": False}


def verify(archive: Path | None = None) -> dict:
    """对比备份环境与当前机器，返回检查项与是否就绪。"""
    backup_env = {}
    if archive and Path(archive).is_file():
        try:
            backup_env = _read_manifest(Path(archive)).get("environment") or {}
        except Exception:  # noqa: BLE001
            backup_env = {}
    env = _current_env()
    home = dsh_pack.dsh_home()
    state = dsh_pack.state_dir()
    reg = _registration_state()
    cred = _credential_state()
    checks: list[dict] = []

    def add(key, label, ok, detail, fix="", command=""):
        checks.append({"key": key, "label": label, "ok": bool(ok),
                       "detail": detail, "fix": fix, "command": command})

    add("dsh_home", "DSH_HOME 目录", home.is_dir(),
        str(home), fix="dirs" if not home.is_dir() else "")
    add("state", "桥接状态目录", state.is_dir(), str(state),
        fix="dirs" if not state.is_dir() else "")
    add("profiles", "Web profile 配置",
        (home / "profiles" / "web" / "cordis.patch.yml").is_file(),
        str(home / "profiles" / "web"))
    deps = home / "profiles" / "web" / "node_modules"
    add("plugin_deps", "插件依赖 node_modules", deps.is_dir(),
        str(deps))
    backup_version = (backup_env.get("dsh_version") or "").strip()
    current_version = (env.get("dsh_version") or "").strip()
    version_ok = bool(current_version) and (not backup_version or current_version == backup_version)
    add("dsh_version", "DSH 版本", version_ok,
        (current_version or "未检测到") + ("（备份：" + backup_version + "）" if backup_version else ""),
        command="" if version_ok else ("npm install -g @deepseek-ai/dsh@" + (backup_version or "latest")))
    node_backup = (backup_env.get("node_version") or "").strip()
    node_current = (env.get("node_version") or "").strip()
    node_ok = bool(node_current)
    if node_backup and node_current:
        node_ok = node_current.split(".")[0] == node_backup.split(".")[0]
    add("node_version", "Node 运行时", node_ok,
        (node_current or "未检测到") + ("（备份：" + node_backup + "）" if node_backup else ""),
        command="" if node_ok else "安装与备份同主版本的 Node.js")
    add("token", "桥接 token", reg["token_file_exists"] and bool(reg["token"]),
        reg["token_file_exists"] and "已存在" or "缺失", fix="token" if not reg["token_file_exists"] else "")
    add("registration", "DSH 注册与 token 一致", reg["registered"] and reg["matches"],
        ("serverName 已注册" if reg["registered"] else "未注册") +
        ("；Authorization 匹配" if reg["matches"] else "；Authorization 不匹配"),
        fix="token" if not (reg["registered"] and reg["matches"]) else "")
    if cred["present"]:
        add("credentials", "DPAPI 凭据可读", cred["readable"],
            "可在本机解密" if cred["readable"] else "换机后无法解密（需重新粘贴 DSH Web 启动链接）",
            fix="creds" if not cred["readable"] else "")
    ready = all(item["ok"] for item in checks)
    return {"ready": ready, "checks": checks, "environment": env,
            "backup_environment": backup_env,
            "archive": str(archive) if archive else ""}


def fix(actions: list[str]) -> dict:
    """执行安全修复：dirs / token / creds。返回结果并重跑校验。"""
    done: list[str] = []
    skipped: list[str] = []
    home = dsh_pack.dsh_home()
    state = dsh_pack.state_dir()

    if "dirs" in actions:
        home.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        done.append("创建 DSH_HOME / 状态目录")

    if "token" in actions:
        token = secrets.token_urlsafe(32)
        (state / "http.token").write_text(token, encoding="utf-8")
        patch = home / "profiles" / "web" / "cordis.patch.yml"
        if patch.is_file():
            text = patch.read_text(encoding="utf-8")
            backup = patch.with_name(patch.name + ".bak-token")
            shutil.copy2(patch, backup)
            text = re.sub(r"Authorization:\s*'Bearer\s+[^']+'",
                          "Authorization: 'Bearer " + token + "'", text)
            if "serverName: dsh_mcp_bridge" not in text:
                skipped.append("注册块不存在，未改名（请检查 cordis.patch.yml）")
            patch.write_text(text, encoding="utf-8")
            done.append("已重置桥接 token 并写入注册配置")
        else:
            skipped.append("cordis.patch.yml 不存在，仅生成 token")
        done.append("注意：需重启 HTTP(MCP) 服务使新 token 生效")

    if "creds" in actions:
        for name in ("web_launch.json", "web-auth"):
            target = state / name
            if target.exists():
                target.rename(state / (name + ".unusable-" + dsh_pack.now_stamp()))
                done.append("已移走不可用凭据：" + name)
        done.append("请在面板粘贴新的 DSH Web 启动链接")

    return {"done": done, "skipped": skipped, "verify": verify()}