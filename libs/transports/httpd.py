"""Standard MCP **Streamable HTTP** transport.

Runs the six classified tools behind ``POST/GET/DELETE
/mcp`` so clients (DSH's mcp-client, Codex, custom agents) connect by URL
instead of spawning a Python process.

Authentication: ``Authorization: Bearer <token>`` (or ``?token=`` / ``X-Mcp-Token``).
The token is read from ``DSH_MCP_TOKEN`` or generated once into
``state/http.token`` (user-only ACL).

Usage::

    python -m libs http --port 8770
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import urllib.request
from pathlib import Path

import uvicorn

from ..core.config import runtime_config
from ..core.manager import Manager
from ..core.platform import locations
from .server import build_server

try:
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
except Exception:  # pragma: no cover - SDK too old
    StreamableHTTPSessionManager = None  # type: ignore


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("DSH_MCP_HTTP_PORT", "8770"))


def address() -> tuple[str, int]:
    try:
        record = json.loads((locations().state / "http-address.json").read_text(encoding="utf-8"))
        return str(record["host"]), int(record["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_HOST, DEFAULT_PORT


def token_file() -> Path:
    return locations().state / "http.token"


def load_or_create_token(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("DSH_MCP_TOKEN", "").strip()
    if env:
        return env
    path = token_file()
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _scope_authorized(scope: dict, token: str) -> bool:
    """Bearer/X-Mcp-Token/query auth straight from the ASGI scope."""
    supplied = ""
    for key, value in scope.get("headers", []):
        name = key.decode("latin-1").lower()
        if name == "authorization":
            text = value.decode("latin-1")
            if text.lower().startswith("bearer "):
                supplied = text[7:].strip()
        elif name == "x-mcp-token" and not supplied:
            supplied = value.decode("latin-1").strip()
    if not supplied:
        query = scope.get("query_string", b"").decode("latin-1")
        for part in query.split("&"):
            if part.startswith("token="):
                supplied = part[6:].strip()
                break
    return bool(supplied) and secrets.compare_digest(supplied, token)


async def _send_json(send, status: int, payload: dict, extra_headers=None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    if extra_headers:
        headers += extra_headers
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def make_app(session_manager, token: str, manager=None):
    """Pure-ASGI app: dispatches /mcp and /health, no path redirects."""

    async def app(scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            # StreamableHTTPSessionManager must be run() while the server is up.
            async with session_manager.run():
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        break
            return

        if scope["type"] != "http":
            return

        path = (scope.get("path") or "").rstrip("/")
        if path == "/mcp":
            if not _scope_authorized(scope, token):
                await _send_json(
                    send, 401,
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32001, "message": "unauthorized"}},
                    extra_headers=[(b"www-authenticate", b"Bearer")],
                )
                return
            await session_manager.handle_request(scope, receive, send)
            return
        if path == "/health":
            from ..core.platform import APP_VERSION
            recovery = getattr(manager, "_recovery_task", None)
            await _send_json(send, 200, {"ok": True, "transport": "streamable-http",
                                       "version": APP_VERSION,
                                       "observation_recovery": bool(recovery and not recovery.done())})
            return
        if path == "/admin/drain":
            if not _scope_authorized(scope, token):
                await _send_json(send, 401, {"error": "unauthorized"})
                return
            if scope["method"] != "POST":
                await _send_json(send, 405, {"error": "POST required"})
                return
            # Graceful stop: interrupt every active turn and wait for idle so a
            # service restart does not orphan tasks.
            if manager is None:
                await _send_json(send, 503, {"error": "manager unavailable"})
                return
            try:
                result = await manager.stop_active(timeout=60.0)
            except Exception as error:  # noqa: BLE001
                await _send_json(send, 500, {"error": str(error)[:200]})
                return
            await _send_json(send, 200, result)
            return
        await _send_json(send, 404, {"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32601, "message": "not found"}})

    return app


async def serve(host: str, port: int, token: str, *, stateless: bool = True) -> None:
    if StreamableHTTPSessionManager is None:
        raise RuntimeError("installed 'mcp' package lacks StreamableHTTPSessionManager; upgrade mcp")
    manager = Manager(runtime_config())
    manager._start_watchdog()
    try:
        recoverable = [a for a in manager.list() if a.get("status") == "interrupted"]
        if recoverable:
            print(
                "注意：{} 个任务处于 interrupted，可在面板点「恢复」或用 dsh_resume 继续：{}".format(
                    len(recoverable),
                    ", ".join((a.get("name") or a["id"])[:18] for a in recoverable[:3]),
                ),
                flush=True,
            )
    except Exception:  # noqa: BLE001
        pass
    # Web-only recovery is observational; Manager.auto_resume never replays work.
    asyncio.create_task(manager.auto_resume())
    session_manager = StreamableHTTPSessionManager(
        app=build_server(manager),
        stateless=stateless,
        json_response=False,
    )
    app = make_app(session_manager, token, manager)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await manager.shutdown()


def main(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
         token: str | None = None, stateless: bool = True) -> int:
    token = load_or_create_token(token)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid HTTP port")
    (locations().state / "http-address.json").write_text(json.dumps({"host": host, "port": port}), encoding="utf-8")
    print(f"MCP Streamable HTTP: http://{host}:{port}/mcp", flush=True)
    print("Authentication: Bearer token loaded (value hidden)", flush=True)
    print(f"Health: http://{host}:{port}/health", flush=True)
    try:
        asyncio.run(serve(host, port, token, stateless=stateless))
    except KeyboardInterrupt:
        pass
    return 0


def status() -> dict:
    path = token_file()
    host, port = address()
    return {
        "transport": "streamable-http",
        "endpoint": f"http://{host}:{port}/mcp",
        "token_file": str(path),
        "token_exists": path.is_file(),
    }


# ---------------------------------------------------------------------------
# Service control (used by the panel and the CLI)
# ---------------------------------------------------------------------------

def pid_file() -> Path:
    return locations().state / "http.pid"


def _pid_is_http(pid: int) -> bool:
    """Alive AND actually our HTTP transport (guards against pid reuse)."""
    from ..core.platform import process_command_line
    command = process_command_line(pid).replace("\\", "/").lower()
    root = Path(__file__).resolve().parents[2].as_posix().lower()
    return root in command and ("-m libs http" in command or "main.py" in command) and " http" in command


def _pid_alive(pid: int) -> bool:
    import sys

    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_pid() -> int | None:
    path = pid_file()
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return None
    if _pid_alive(pid) and _pid_is_http(pid):
        return pid
    return None


def service_status() -> dict:
    pid = running_pid()
    token_path = token_file()
    host, port = address()
    return {
        "running": pid is not None,
        "pid": pid,
        "endpoint": f"http://{host}:{port}/mcp",
        "health": f"http://{host}:{port}/health",
        "token_file": str(token_path),
        "token_exists": token_path.is_file(),
    }


def start_service(host: str | None = None, port: int | None = None) -> dict:
    """Start the HTTP transport detached (no console window on Windows)."""
    import subprocess
    import sys
    import time
    configured_host, configured_port = address()
    host = configured_host if host is None else host
    port = configured_port if port is None else port

    existing = running_pid()
    if existing:
        return {"started": False, "already_running": True, **service_status()}

    locations().state.mkdir(parents=True, exist_ok=True)
    log = open(locations().state / "http.log", "a", encoding="utf-8")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x08000000 | 0x00000200  # DETACHED|NO_WINDOW|NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-m", "libs", "http", "--host", host, "--port", str(port),
         "--bridge-root", str(Path(__file__).resolve().parents[2])],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    pid_file().write_text(str(proc.pid), encoding="utf-8")

    # wait for readiness (health endpoint) with backoff
    import urllib.request

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if not _pid_alive(proc.pid):
            return {"started": False, "error": "process exited; see state/http.log"}
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return {"started": True, **service_status()}
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return {"started": False, "error": "health check timed out; see state/http.log",
            **service_status()}


def _active_task_count() -> int | None:
    """Shared-store activity; None means activity could not be verified."""
    try:
        db = runtime_config().database
        import sqlite3 as _sqlite

        con = _sqlite.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            rows = [json.loads(r[0]) for r in con.execute("SELECT data FROM agents").fetchall()]
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None
    from ..core.manager import ACTIVE
    return sum(1 for a in rows if a.get("status") in ACTIVE)


def _drain_service() -> dict:
    """Ask the running service to interrupt its active turns (best effort)."""
    token_path = token_file()
    if not token_path.is_file():
        return {"ok": False, "error": "no token"}
    token = token_path.read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/admin/drain",
        data=b"{}", method="POST",
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return {"ok": True, **json.loads(response.read().decode("utf-8"))}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": str(error)}


def stop_service(force: bool = False) -> dict:
    """Stop only a verifiably owned bridge; never cancel native Web tasks."""
    import sys
    pid = running_pid()
    if pid is None:
        return {"stopped": False, "already_stopped": True, **service_status()}
    active = _active_task_count()
    if (active is None or active > 0) and not force:
        return {"stopped": False, "blocked": True, "active": active,
                "message": ("Task activity could not be verified; service was not stopped" if active is None
                            else "Tasks remain active; wait or explicitly stop only this observer transport"),
                **service_status()}
    try:
        if sys.platform == "win32":
            from ..core.platform import hidden_run
            result = hidden_run(["taskkill", "/PID", str(pid), "/F"], timeout=15)
            if result.returncode and _pid_alive(pid):
                return {"stopped": False, "error": "Owned transport did not stop", **service_status()}
        else:
            os.kill(pid, 15)
    except Exception as error:
        return {"stopped": False, "error": str(error), **service_status()}
    pid_file().unlink(missing_ok=True)
    return {"stopped": True, "native_tasks_cancelled": False, **service_status()}
