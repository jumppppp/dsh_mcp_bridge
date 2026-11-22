"""Optional Codex App Server callback (fourth completion channel).

Why: the original dsh-subagent-mcp could wake an idle Codex session by posting
tool output into its thread through the App Server JSON-RPC API. MCP itself is
client-driven (a server cannot call the client), so that wake-up needs the
App Server.

Protocol (Codex app-server, JSON-RPC 2.0 over stdio, matching `codex app-server`):
  -> {"method":"initialize","id":0,"params":{"clientInfo":{...}}}
  <- {"id":0,"result":...}
  -> {"method":"initialized","params":{}}
  -> {"method":"turn/start","id":1,"params":{
        "threadId": "<parent thread>",
        "input": [{"type":"text","text": "..."}]}}
`turn/start` accepts standalone tool output for a thread, which is exactly what
wakes the parent agent.

Configuration (``state/codex.json``, all optional):
  {"enabled": true,
   "command": ["codex", "app-server"],   # OR "url": "ws://..." for a running server
   "threadId": "<thread to notify>",
   "timeout": 20}

Environment overrides: DSH_CODEX_CALLBACK=0 disables, DSH_CODEX_THREAD sets the
thread id. Everything is best-effort: a callback failure never affects tasks.

Compatibility note: this protocol is Codex-specific and versioned with the CLI.
`codex --version` is recorded in the config; if the JSON-RPC surface changes,
`turn/start` will fail and the channel silently degrades (MCP notification /
webhook / file channels keep working). Re-check this file when upgrading Codex.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path


def config_file(state: Path) -> Path:
    return state / "codex.json"


def load_config(state: Path) -> dict:
    path = config_file(state)
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            data = {}
    if os.environ.get("DSH_CODEX_CALLBACK", "1").strip() == "0":
        data["enabled"] = False
    if os.environ.get("DSH_CODEX_THREAD", "").strip():
        data["threadId"] = os.environ["DSH_CODEX_THREAD"].strip()
    return data


def save_config(state: Path, payload: dict) -> dict:
    clean = {
        "enabled": bool(payload.get("enabled", False)),
        "command": payload.get("command") or ["codex", "app-server"],
        "threadId": (payload.get("threadId") or "").strip(),
        "timeout": int(payload.get("timeout") or 20),
        "codexVersion": payload.get("codexVersion") or "",
    }
    config_file(state).write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


class _WsOverStdio:
    """Tiny RFC6455 client speaking to `codex app-server proxy` (WS tunnel)."""

    def __init__(self, proc, timeout: float = 15.0):
        self.proc = proc
        self.timeout = timeout

    def handshake(self, key=b"dsh-mcp-bridge-callback-key"):
        import base64
        import hashlib

        accept = base64.b64encode(hashlib.sha1(
            key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n".format(key.decode())
        ).encode()
        self.proc.stdin.write(request)
        self.proc.stdin.flush()
        head = b""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            chunk = self.proc.stdout.read(1)
            if not chunk:
                raise RuntimeError("proxy closed during handshake")
            head += chunk
            if b"\r\n\r\n" in head:
                break
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket handshake failed: " + head[:80].decode(errors="replace"))
        return True

    @staticmethod
    def _frame(payload: bytes, opcode: int = 0x1) -> bytes:
        mask_key = b"\x37\xfa\x21\x3d"
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return header + mask_key + masked

    def send_text(self, text: str) -> None:
        self.proc.stdin.write(self._frame(text.encode("utf-8")))
        self.proc.stdin.flush()

    def read_message(self, timeout: float = 15.0) -> str | None:
        import struct

        deadline = time.monotonic() + timeout
        data = b""

        def read(n: int) -> bytes:
            nonlocal data
            while len(data) < n and time.monotonic() < deadline:
                chunk = self.proc.stdout.read(n - len(data))
                if not chunk:
                    raise RuntimeError("proxy closed mid-frame")
                data += chunk
            return data

        read(2)
        b0, b1 = data[0], data[1]
        data = data[2:]
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        masked = bool(b1 & 0x80)
        if length == 126:
            length = struct.unpack(">H", read(2))[0]
            data = data[2:]
        elif length == 127:
            length = struct.unpack(">Q", read(8))[0]
            data = data[8:]
        mask_key = read(4) if masked else None
        data = data[:0]
        payload = read(length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:  # close
            return None
        if opcode == 0x1:
            return payload.decode("utf-8", "replace")
        return None


def codex_command() -> list[str]:
    """Locate the codex CLI (PATH shim or desktop install)."""
    import shutil

    found = shutil.which("codex")
    if found:
        return [found]
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "OpenAI" / "Codex" / "bin")
    candidates.append(Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin")
    for desktop in candidates:
        if desktop.is_dir():
            bins = sorted(desktop.glob("*/codex.exe"), key=lambda x: x.stat().st_mtime, reverse=True)
            if bins:
                return [str(bins[0])]
    return []


def daemon_socket() -> Path | None:
    """The app-server control socket (fixed location under the Codex caches)."""
    candidates = [
        Path("R:/Caches/.codex/app-server-control/app-server-control.sock"),
        Path.home() / ".codex" / "app-server-control" / "app-server-control.sock",
    ]
    env_value = os.environ.get("CODEX_APP_SERVER_CONTROL_SOCKET", "").strip()
    if env_value:
        candidates.insert(0, Path(env_value))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


_DETECT_CACHE: dict = {"at": 0.0, "info": None}


def detect_backend(state: Path, probe_timeout: float = 3.0, ttl: float = 60.0) -> dict:
    """Decide which completion mode fits the caller.

    ChatGPT GUI keeps its own sessions and does NOT attach them to the headless
    app-server daemon, so ``turn/start`` has no thread to target; the sync mode
    (dsh_wait) is the right channel there. CLI/IDE app-server clients do create
    threads on the daemon, which enables the callback mode.
    """
    now = time.monotonic()
    cached = _DETECT_CACHE.get("info")
    if cached is not None and now - float(_DETECT_CACHE.get("at") or 0) < ttl:
        return dict(cached)

    info = {"mode": "sync", "codex": False, "daemon": False, "thread": False}

    command = codex_command()
    info["codex"] = bool(command)
    socket = daemon_socket()
    info["daemon"] = socket is not None
    if not (command and socket):
        _DETECT_CACHE["at"] = time.monotonic()
        _DETECT_CACHE["info"] = dict(info)
        return info

    import json as _json
    import subprocess as _sp

    try:
        proc = _sp.Popen(
            [command[0], "app-server", "proxy"],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.DEVNULL,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001
        return info
    try:
        ws = _WsOverStdio(proc, timeout=probe_timeout)
        ws.handshake()
        ws.send_text(_json.dumps({
            "method": "initialize", "id": 0,
            "params": {"clientInfo": {"name": "dsh-mcp-bridge", "version": "0.1.0"},
                       "capabilities": {}},
        }))
        deadline = time.monotonic() + probe_timeout
        while time.monotonic() < deadline:
            message = ws.read_message(max(0.2, deadline - time.monotonic()))
            if message is None:
                break
            try:
                msg = _json.loads(message)
            except ValueError:
                continue
            if msg.get("id") == 0:
                ws.send_text(_json.dumps({"method": "initialized", "params": {}}))
                ws.send_text(_json.dumps({"method": "thread/list", "id": 1, "params": {}}))
            elif msg.get("id") == 1:
                threads = (msg.get("result") or {}).get("threads") or (
                    (msg.get("result") or {}).get("data") or [])
                info["thread"] = bool(threads)
                info["mode"] = "callback" if threads else "sync"
                _DETECT_CACHE["at"] = time.monotonic()
                _DETECT_CACHE["info"] = dict(info)
                return info
        _DETECT_CACHE["at"] = time.monotonic()
        _DETECT_CACHE["info"] = dict(info)
        return info
    except Exception:  # noqa: BLE001
        _DETECT_CACHE["at"] = time.monotonic()
        _DETECT_CACHE["info"] = dict(info)
        return info
    finally:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def codex_version() -> str:
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr or "").strip()[:80]
    except Exception:  # noqa: BLE001
        return ""


def _message(agent: dict) -> str:
    status = agent.get("status")
    name = agent.get("name") or agent.get("id")
    answer = str(agent.get("answer") or agent.get("last_completed_answer") or "")
    error = agent.get("error")
    if status == "completed":
        body = answer[:4000] or "（无输出）"
        return f"[dsh-subagent] 任务「{name}」已完成。\n\n{body}"
    if status == "interrupted":
        return f"[dsh-subagent] 任务「{name}」已中断（{error or '无原因'}）。可用 dsh_resume 恢复。"
    return f"[dsh-subagent] 任务「{name}」失败：{error or '未知错误'}"


async def notify_codex(state: Path, agent: dict) -> bool:
    """Push one settled task into a Codex thread via `turn/start` (best effort)."""
    config = load_config(state)
    if not config.get("enabled") or not config.get("threadId"):
        return False
    # GUI sessions live outside the daemon; without a live thread this channel
    # would hang, and the sync mode (dsh_wait) already covers that caller.
    try:
        probe = detect_backend(state)
        if probe.get("mode") != "callback":
            return False
    except Exception:  # noqa: BLE001
        return False
    command = config.get("command") or ["codex", "app-server"]
    timeout = float(config.get("timeout") or 20)
    # Original delivery shape: standalone tool output that wakes the parent.
    payload = {
        "method": "turn/start",
        "id": 1,
        "params": {
            "threadId": config["threadId"],
            "input": [],
            "toolOutput": {
                "name": "dsh_completion",
                "namespace": None,
                "output": json.dumps(_message(agent), ensure_ascii=False),
            },
        },
    }

    def _run() -> bool:
        try:
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001 - codex not installed / wrong command
            return False
        try:
            ws = _WsOverStdio(proc, timeout=timeout)
            ws.handshake()
            ws.send_text(json.dumps({
                "method": "initialize", "id": 0,
                "params": {"clientInfo": {"name": "dsh-mcp-bridge", "version": "0.1.0"},
                           "capabilities": {}},
            }))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                message = ws.read_message(timeout)
                if message is None:
                    return False
                try:
                    msg = json.loads(message)
                except ValueError:
                    continue
                if msg.get("id") == 0:
                    ws.send_text(json.dumps({"method": "initialized", "params": {}}))
                    # Original shape: toolOutput, not a text input item.
                    ws.send_text(json.dumps(payload))
                elif msg.get("id") == 1:
                    return "result" in msg
            return False
        except Exception:  # noqa: BLE001
            try:
                err = proc.stderr.read() if hasattr(proc, "stderr") and proc.stderr else ""
                print(f"[codex-callback] {err[:200]}", flush=True)
            except Exception:  # noqa: BLE001
                pass
            return False
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    return await asyncio.to_thread(_run)