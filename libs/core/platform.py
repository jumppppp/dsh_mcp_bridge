"""Platform layer: user directories, private permissions, JSON helpers.

Mirrors the layout used by dsh-subagent-mcp so the two can be compared, but
keeps its own directory prefix (``dsh-mcp-bridge``) so it never steps on the
npm installation while both are installed.

Windows notes
-------------
Windows mode bits do not restrict other users, so private directories get an
explicit DACL via ``icacls`` before we write credentials or IPC tokens.
"""

from __future__ import annotations

import json
import locale
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_DARWIN = sys.platform == "darwin"

APP_NAME = "dsh-mcp-bridge"
APP_VERSION = "2.0.2"
# 默认把数据放在项目内 v2/data（state/config/backups），可用 DSH_SUBAGENT_DATA/STATE 覆盖。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

# DEEPSEEK_API_KEY="sk-..." or DEEPSEEK_BASE_URL="https://..."
_ENV_LINE = re.compile(r'^(DEEPSEEK_API_KEY|DEEPSEEK_BASE_URL)="(.*)"$')


def _env_path(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Locations:
    """All per-user paths the bridge uses."""

    data: Path
    config: Path
    state: Path
    codex: Path


def locations() -> Locations:
    home = Path.home()
    env = os.environ

    base = Path(_env_path("DSH_SUBAGENT_DATA") or DEFAULT_DATA_DIR)
    data = base
    config = Path(_env_path("DSH_SUBAGENT_CONFIG") or (base / "config"))
    state = Path(_env_path("DSH_SUBAGENT_STATE") or (base / "state"))

    codex = Path(env.get("CODEX_HOME") or (home / ".codex"))
    return Locations(data=data, config=config, state=state, codex=codex)


def hidden_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a short console tool WITHOUT flashing a new console window.

    The panel may run detached (no console of its own); a plain subprocess call
    would spawn a visible PowerShell/node window on EVERY page refresh (HTTP
    status probing runs ``powershell Get-CimInstance`` to match the pid).
    ``CREATE_NO_WINDOW`` keeps those probes invisible.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if kwargs.get("text"):
        kwargs.setdefault("encoding", "mbcs" if IS_WINDOWS else "utf-8")
        kwargs.setdefault("errors", "replace")
    if IS_WINDOWS:
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(argv, **kwargs)


def _run_text(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a Windows tool whose output is in the system ANSI code page.

    ``text=True`` alone would decode as UTF-8 and raise on GBK consoles, so the
    encoding is pinned to the locale and replacement is allowed.
    """
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False) or "utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=0x08000000 if IS_WINDOWS else 0,  # CREATE_NO_WINDOW
    )


def private_directory(path: Path) -> Path:
    """Create ``path`` and restrict it to the current user (+ SYSTEM on Windows)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if IS_WINDOWS:
        try:
            user = _run_text(["whoami.exe"], timeout=15).stdout.strip()
            if user:
                _run_text(
                    [
                        "icacls.exe", str(path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{user}:(OI)(CI)F",
                        "*S-1-5-18:(OI)(CI)F",
                    ]
                )
        except (OSError, subprocess.SubprocessError):
            # A failed ACL tightening must not take the whole service down; the
            # parent directory already inherited the user's profile ACL.
            pass
    else:
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def read_json(path: Path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError):
        return fallback


def write_json(path: Path, value, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        tmp.chmod(mode)
    except OSError:
        pass
    os.replace(tmp, path)


def encode_workspace(path: str) -> str:
    """DSH's workspace directory encoding: D:/x/y -> --D-x-y--.

    The drive separator+backslash collapses into ONE dash; replacing the colon
    and the backslash separately yields D--x which never matches on disk. Kept
    in one place so every caller shares the same rule.
    """
    norm = str(path).replace("\\", "/")
    # D:/x -> D-x : the drive colon and the NEXT separator merge into one dash.
    body = norm.replace(":/", "-").replace("/", "-").strip("-")
    return "--" + body + "--"


def dsh_web_home() -> Path:
    """The user's real DSH home (R:/dsh-data by default, DSH_WEB_HOME overrides)."""
    import os

    return Path(os.environ.get("DSH_WEB_HOME", "R:/dsh-data"))


def sessions_root() -> Path:
    return dsh_web_home() / "sessions"


def storages_root() -> Path:
    return dsh_web_home() / "storages"


def workspace_registry() -> Path:
    return storages_root() / "workspace.json"


def temp_workspace() -> Path:
    """The single scratch workspace for otherwise directory-less work.

    Every ad-hoc MCP task lands here instead of a fresh ``mkdtemp`` folder, so
    DSH shows one predictable workspace rather than a new one per test run.
    """
    path = Path(tempfile.gettempdir()) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def process_command_line(pid: int) -> str:
    """Best-effort Windows command line for a pid ('' when unknown)."""
    if not pid or pid <= 0:
        return ""
    if IS_WINDOWS:
        try:
            out = hidden_run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                timeout=15,
            )
            return (out.stdout or "").strip()
        except Exception:  # noqa: BLE001
            return ""
    try:
        return Path(f"/proc/{pid}/cmdline").read_text(encoding="utf-8", errors="replace").replace("\x00", " ")
    except OSError:
        return ""


def pid_matches(pid: int, marker: str) -> bool:
    """True when ``pid`` is alive and its command line contains ``marker``.

    Pid reuse is common after crashes; daemon and HTTP services must not
    mistake each other (or an unrelated Python process) for themselves.
    """
    if not pid or pid <= 0:
        return False
    import ctypes

    if IS_WINDOWS:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
    else:
        try:
            import os as _os

            _os.kill(pid, 0)
        except OSError:
            return False
    command = process_command_line(pid)
    if not command:
        # Unverifiable identity never grants permission to manage a process.
        return False
    return marker in command


def temporary_directory() -> Path:
    return Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")


def installation_file() -> Path:
    return locations().config / "installation.json"


def installation() -> dict | None:
    return read_json(installation_file())

