"""Installed DSH Web version/path metadata; no independent Profile bootstrap."""
from pathlib import Path
import os
import shutil

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOME = PROJECT_ROOT / "data" / "dsh-home-bridge"  # Historical diagnostics only; never created.

def web_session_format() -> str:
    """V2 固定写 v4（DSH 0.2.1 Web 读 v4）。"""
    return "v4"

def mode() -> str:
    """V2 唯一运行时：021（写 v4）。"""
    return "021"

def dsh_root() -> Path:
    """The installed 0.2.1 dsh package (JS entry + plugin set)."""
    for name in ("DSH_021_ROOT", "DSH_INSTALL_ROOT"):
        explicit = os.environ.get(name, "").strip()
        if explicit:
            return Path(explicit)
    found = shutil.which("dsh")
    if found:
        for parent in Path(found).resolve().parents:
            candidate = parent / "@deepseek-ai" / "dsh"
            if (candidate / "package.json").is_file():
                return candidate
    return Path.home() / ".dsh" / "install"

def entry() -> Path:
    return dsh_root() / "lib" / "bin.js"
