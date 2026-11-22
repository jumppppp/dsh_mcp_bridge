"""Launch the DSH Web from the panel and capture its launch URL (token).

DSH prints ``dsh web: http://127.0.0.1:PORT/?token=...`` on stdout and does not
persist the token anywhere, so the only automation path is to start the process
ourselves, capture stdout, and remember the URL. ``dsh_attach``/``dsh_send``
then reuse it without the user pasting a URL by hand.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from ..core.secrets import read_secret_json, write_secret_json
from ..core.platform import dsh_web_home
from ..core.net import http_options

_LAUNCH_URL_RE = re.compile(r"http://[^\s:/]+:(\d+)/\?token=([A-Za-z0-9_-]+)")


_LAUNCH_CONFIG_NAME = "web-launch-config.json"
DEFAULT_LAUNCH_COMMAND = "dsh web"


def _config_file(state: Path) -> Path:
    return Path(state) / _LAUNCH_CONFIG_NAME


def read_launch_command(state: Path) -> str:
    """用户保存的 DSH Web 启动命令（默认 ``dsh web``）。"""
    try:
        data = json.loads(_config_file(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_LAUNCH_COMMAND
    return str(data.get("command") or DEFAULT_LAUNCH_COMMAND)


def read_autostart(state: Path) -> bool:
    """面板启动时是否自动拉起 DSH Web（默认关闭，仅用户显式开启才自动启动）。"""
    try:
        data = json.loads(_config_file(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("autostart", False))


def parse_launch_command(text: str) -> list[str]:
    """把 ``dsh web --host 0.0.0.0`` 解析为传给 dsh 的额外参数。

    仅接受 dsh 的参数：可执行文件固定为面板选定的 dsh 入口，用户不能换成任意
    命令（防注入）。``--port`` 与 ``--no-open`` 由面板统一注入，避免与状态/
    端口探测冲突。返回 ``["web", <extra args...>]`` 之后的额外参数（不含 web）。
    """
    import shlex

    raw = (text or "").strip()
    if not raw:
        raise ValueError("启动命令不能为空；默认：dsh web")
    try:
        parts = shlex.split(raw, posix=True)
    except ValueError as error:
        raise ValueError("启动命令解析失败：{}".format(error)) from None
    if not parts:
        raise ValueError("启动命令不能为空；默认：dsh web")
    first = parts[0].lower()
    if first.endswith("dsh") or first.endswith("dsh.exe"):
        parts = parts[1:]
    if not parts:
        raise ValueError("缺少 web 子命令；示例：dsh web --host 0.0.0.0")
    if parts[0] == "node" or parts[0].lower().endswith("node.exe"):
        raise ValueError("只支持 dsh 命令，例如：dsh web --host 0.0.0.0（不支持自定义 node 入口）")
    if parts[0] == "web":
        parts = parts[1:]
    elif not parts[0].startswith("-"):
        raise ValueError("启动命令必须以 web 开头；示例：dsh web --host 0.0.0.0")
    extras: list[str] = []
    skip_next = False
    for item in parts:
        if skip_next:
            skip_next = False
            continue
        if item in ("--port", "-p"):
            skip_next = True  # 端口由面板注入
            continue
        if item.startswith("--port="):
            continue
        if item == "--no-open":
            continue  # 面板统一注入
        extras.append(item)
    return extras


def launch_command_line(state: Path) -> str:
    """归一化后的展示命令（用于面板显示）。"""
    try:
        extras = parse_launch_command(read_launch_command(state))
    except ValueError:
        return read_launch_command(state)
    return " ".join(["dsh", "web", *extras])


def write_launch_command(state: Path, text: str, autostart: bool | None = None) -> dict:
    """校验并保存启动命令（可选同时保存自启开关）；返回归一化结果。"""
    extras = parse_launch_command(text)
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    enabled = read_autostart(state) if autostart is None else bool(autostart)
    payload = {"command": " ".join(["dsh", "web", *extras]), "extras": extras,
               "autostart": enabled}
    path = _config_file(state)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return payload


def read_web_exit(state: Path) -> dict | None:
    """面板托管的 DSH Web 最近一次退出信息（退出码/时间/命令）。"""
    try:
        data = json.loads((Path(state) / "web-exit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _file(state: Path) -> Path:
    return state / "web_launch.json"


@contextmanager
def _launch_lock(state: Path):
    with (state / "web-launch.lock").open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 65
        delay = 0.05
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another owned launch is still in progress")
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def stop_managed(state: Path) -> dict:
    """Authenticated stop of the specific supervisor; never a PID-wide reap."""
    import urllib.request
    from ..core.platform import pid_matches, process_command_line
    state = Path(state).resolve()
    record = read_secret_json(_file(state))
    worker_pid = int(record.get("worker_pid") or 0)
    if not worker_pid or not pid_matches(worker_pid, "libs.transports.web_worker") or str(state) not in process_command_line(worker_pid):
        raise RuntimeError("Owned supervisor identity cannot be verified; stop refused")
    port = int(record["control_port"])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid supervisor control port")
    request = urllib.request.Request("http://127.0.0.1:{}/stop".format(port), data=b"{}",
        headers={"X-Worker-Token": record["control_token"], "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return {"stopping": response.status == 200}


def _dsh_cli() -> list[str]:
    import shutil

    from ..core.runtime021 import dsh_root

    candidate = dsh_root() / "lib" / "bin.js"
    if candidate.is_file():
        return ["node", str(candidate)]
    found = shutil.which("dsh")
    if found:
        return [found]
    raise RuntimeError(
        "cannot locate the DSH CLI; set DSH_INSTALL_ROOT or ensure `dsh` is on PATH")


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _dsh_version() -> str:
    """从本机 DSH 包读取版本（只读，不起进程）。"""
    try:
        candidate = _dsh_cli()[1]
        pkg = Path(candidate).parent.parent / "package.json"
        if pkg.is_file():
            import json as _json
            return str(_json.loads(pkg.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError, IndexError):
        pass
    return ""


def probe_token(url: str, timeout: float = 3.0) -> dict:
    """A valid launch link answers 303; do not follow without its cookie."""
    import httpx
    if not url:
        return {"ok": False, "reason": "no-url"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, **http_options(url)) as client:
            response = client.get(url)
        if response.status_code == 303 and response.headers.get("set-cookie"):
            return {"ok": True, "status": 303, "reason": "redirect"}
        return {"ok": False, "status": response.status_code,
                "reason": "token-invalid" if response.status_code == 401 else "unexpected-response"}
    except Exception:
        return {"ok": False, "reason": "unreachable"}


def probe_cookie(state: Path, origin: str, timeout: float = 3.0) -> dict:
    """用已存储的 dsh-auth cookie 验证连接（token 失效时的回退凭据）。"""
    import httpx
    for path in (state / "web-auth").glob("*.json"):
        try:
            data = read_secret_json(path)
        except (OSError, ValueError):
            continue
        if not data or (data.get("origin") or "").rstrip("/") != (origin or "").rstrip("/")                 or not data.get("cookie"):
            continue
        body = {"type": "client-request", "rpcId": "probe", "method": "session/list",
                "payload": {"args": {"_request": {}}}}
        try:
            with httpx.Client(timeout=timeout, **http_options(origin)) as client:
                response = client.post(origin + "/api/session/list", json=body,
                                       headers={"cookie": str(data["cookie"]), "origin": origin,
                                                "content-type": "application/json"})
            if response.status_code == 200:
                result = response.json().get("result") or {}
                if result.get("ok"):
                    return {"ok": True, "reason": "cookie"}
        except Exception:  # noqa: BLE001
            continue
    return {"ok": False, "reason": "no-valid-cookie"}


def status(state: Path, probe: bool = False) -> dict:
    record = {}
    if _file(state).is_file():
        try:
            record = read_secret_json(_file(state))
        except (OSError, ValueError):
            record = {}
    port = int(record.get("port") or 3080)
    running = _port_in_use(port)
    pid = record.get("pid")
    pid_alive = False
    if running and pid:
        try:
            from ..core.platform import process_command_line
            command = process_command_line(int(pid))
            pid_alive = _dsh_cli()[1] in command and " web" in command
        except (OSError, RuntimeError, ValueError):
            pid_alive = False
    url = record.get("url") or ""
    token_probe = probe_token(url) if (probe and url) else None
    cookie_probe = None
    if probe and not (token_probe or {}).get("ok"):
        origin = (url.split("?", 1)[0] if url else "http://127.0.0.1:{}".format(port)).rstrip("/")
        cookie_probe = probe_cookie(state, origin)
    auth = None
    if (token_probe or {}).get("ok"):
        auth = "token"
    elif (cookie_probe or {}).get("ok"):
        auth = "cookie"
    return {
        "running": running,
        "managed": bool(pid_alive),  # V2 自己启动；False = 外部已有实例
        "port": port,
        "pid": pid if pid_alive else None,
        "url": url,
        "origin": url.split("?", 1)[0] if url else "",
        "token_known": bool(url),
        "token_valid": bool((token_probe or {}).get("ok") or (cookie_probe or {}).get("ok")) if probe else None,
        "auth": auth,
        "started_at": record.get("started_at"),
        "version": _dsh_version(),
    }


def launch(state: Path, port: int = 3080, on_progress=None) -> dict:
    """Start a detached supervisor; the panel never owns the Web output pipe."""
    import sys
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    def emit(phase):
        if on_progress is not None:
            try:
                on_progress(phase)
            except Exception:
                pass
    emit("checking")
    with _launch_lock(state):
        if _port_in_use(port):
            existing = status(state)
            if existing.get("url") and existing.get("port") == port:
                return {**existing, "note": "Existing Web instance retained"}
            raise RuntimeError("Port is occupied by an external instance; connect its authorized launch URL")
        emit("starting_worker")
        try:
            extras = parse_launch_command(read_launch_command(state))
        except ValueError:
            extras = []
        worker = subprocess.Popen(
            [sys.executable, "-m", "libs.transports.web_worker", "--state", str(state),
             "--port", str(port), "--home", str(dsh_web_home()),
             "--extra-args", json.dumps(extras)],
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(0x00000008 | 0x08000000 | 0x00000200) if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
        # Retain and reap the owned process handle without binding its lifetime
        # or standard streams to the launching panel.
        threading.Thread(target=worker.wait, daemon=True, name="dsh-worker-reaper").start()
        deadline = time.monotonic() + 60
        delay = 0.05
        while time.monotonic() < deadline:
            try:
                record = read_secret_json(_file(state))
            except (OSError, ValueError, RuntimeError):
                record = {}
            if record.get("worker_pid") == worker.pid:
                emit("waiting_address")
            if record.get("worker_pid") == worker.pid and record.get("url"):
                return {key: record[key] for key in ("url", "port", "pid", "started_at")}
            if worker.poll() is not None:
                raise RuntimeError("DSH Web supervisor exited before readiness; inspect redacted web-launch.log")
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
        try:
            stop_managed(state)
        except Exception:
            pass
        raise TimeoutError("DSH Web did not become ready within 60 seconds")


def save_url(state: Path, url: str) -> dict:
    match = _LAUNCH_URL_RE.search(url)
    if not match:
        raise ValueError("URL 不是 DSH Web 启动链接（应含 ?token=...）")
    record = {
        "url": "http://127.0.0.1:{}/?token={}".format(match.group(1), match.group(2)),
        "port": int(match.group(1)),
        "pid": None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "手动记录",
    }
    try:
        previous = read_secret_json(_file(state))
    except (OSError, ValueError, RuntimeError):
        previous = {}
    if previous.get("worker_pid") and previous.get("port") == record["port"]:
        for key in ("worker_pid", "control_port", "control_token", "pid"):
            record[key] = previous.get(key)
    assert _file(state).parent.is_dir() or _file(state).parent.mkdir(parents=True, exist_ok=True)
    write_secret_json(_file(state), record)
    try:
        os.chmod(_file(state), 0o600)
    except OSError:
        pass
    return {key: record[key] for key in ("url", "port", "pid", "started_at", "note")}
