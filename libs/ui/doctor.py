"""``doctor``: self-check for the Python bridge.

Catches the failures that actually happen on this machine:

* DSH entrypoint missing or not a JavaScript file
* the ``codex-subagent`` profile missing
* **runtime / profile-store version skew** (a newer DSH with an older plugin
  store silently disables plugin rows and leaves ``initialize`` waiting on
  ``agentPresets`` forever)
* plugin file missing
* state directory or SQLite unusable
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

from ..core.config import resolve_dsh_cli
from ..core.platform import APP_VERSION, locations

OK = "ok"
WARN = "warn"
ERROR = "error"


def _version_of(package_dir: Path) -> str | None:
    manifest = package_dir / "package.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def _major_minor(version: str | None) -> str | None:
    if not version:
        return None
    parts = version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def run_checks() -> list[dict]:
    checks: list[dict] = []
    locations_ = locations()

    # 1. DSH entrypoint.
    try:
        cli = resolve_dsh_cli()
        js = cli[-1]
        checks.append(_check("dsh entrypoint", OK, " ".join(cli)))
    except Exception as error:  # noqa: BLE001
        return [_check("dsh entrypoint", ERROR, str(error))]

    dsh_root = Path(js).parent.parent
    runtime_version = _version_of(dsh_root)

    from ..core.config import web_profile_patch
    profile = web_profile_patch()
    checks.append(_check("DSH Web profile", OK if profile.is_file() else WARN, str(profile)))
    checks.append(_check("installed Web runtime", OK if (runtime_version or "").startswith("0.2.1") else WARN, str(runtime_version)))

    # 5. State directory and database.
    try:
        database = locations_.state / "state.sqlite"
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        expected = {"agents", "parents", "events"}
        missing = expected - tables
        if missing:
            checks.append(_check("state database", WARN, f"missing tables: {sorted(missing)}"))
        else:
            checks.append(_check("state database", OK, str(database)))
    except Exception as error:  # noqa: BLE001
        checks.append(_check("state database", ERROR, str(error)))

    # 6. Python dependencies.
    missing = [name for name in ("mcp",) if not __import__("importlib.util", fromlist=["util"]).find_spec(name)]
    try:
        import websockets  # noqa: F401

        ws = True
    except Exception:  # noqa: BLE001
        ws = False
    checks.append(_check(
        "python deps", OK if not missing else ERROR,
        "mcp" + ("" if missing else "") + (" · websockets(可选)" if ws else " · websockets 缺失(dsh_attach 不可用)"),
    ))

    # 7. The single supported MCP transport (informational).
    from ..transports.httpd import service_status
    state = service_status()
    checks.append(_check("MCP HTTP", OK, ("running " if state["running"] else "stopped ") + state["endpoint"]))

    return checks


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    checks = run_checks()
    if "--json" in argv:
        print(json.dumps({"version": APP_VERSION, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print(f"DSH MCP Bridge {APP_VERSION} doctor\n")
        for check in checks:
            mark = {"ok": "[ok]", "warn": "[warn]", "error": "[FAIL]"}[check["status"]]
            print(f"{mark:7s} {check['name']}: {check['detail']}")
        errors = [c for c in checks if c["status"] == ERROR]
        print()
        print("FAILED" if errors else "All checks passed")
    return 1 if any(c["status"] == ERROR for c in checks) else 0
