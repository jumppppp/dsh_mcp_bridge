"""Workspace boundaries shared by panel and MCP; no prompt-based enforcement."""
import json
import os
from pathlib import Path


# DSH 权限预设（read-only/workspace-write/danger-full-access + auto-review 的 auto）。
# 显示与校验以此为准；实际可用项由 DSH 的 permissionPresets/catalog 动态决定。
KNOWN_PERMISSION_PRESETS = ("read-only", "workspace-write", "danger-full-access", "auto")


def workspace_path(value: str) -> Path:
    from .platform import dsh_web_home, locations
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Workspace must be an existing absolute directory")
    path = path.resolve(strict=True)
    home = Path.home().resolve()
    if path == Path(path.anchor) or path == home:
        raise ValueError("Filesystem and personal-home roots cannot be execution workspaces")
    from .runtime021 import dsh_root
    protected = [dsh_web_home(), locations().state, locations().config, locations().codex, dsh_root()]
    for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(key):
            protected.append(Path(os.environ[key]))
    for item in protected:
        boundary = item.resolve()
        if path.is_relative_to(boundary) or boundary.is_relative_to(path):
            raise ValueError("Workspace overlaps protected runtime/configuration data")
    configured = os.environ.get("DSH_MCP_ALLOWED_WORKSPACES", "").strip()
    if configured:
        roots = json.loads(configured)
        if not isinstance(roots, list) or not roots or not all(isinstance(root, str) for root in roots):
            raise ValueError("Invalid allowed workspace configuration")
        if not any(path.is_relative_to(Path(root).resolve()) for root in roots):
            raise ValueError("Workspace is outside the configured allowed roots")
    return path
