"""CLI for the native DSH Web bridge and Streamable HTTP service."""

from __future__ import annotations

import json
import sys

USAGE = """DSH MCP V2

Usage:
  python main.py panel [--port 8765] [--http-port 8770]
  python main.py http [--host 127.0.0.1] [--port 8770]
  python main.py http start|stop|status
  python main.py doctor [--json]
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "http"
    rest = argv[1:]

    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if command in ("-V", "--version"):
        from .core.platform import APP_VERSION

        print(APP_VERSION)
        return 0

    if command == "mcp":
        print("stdio MCP 已移除：仅提供 Streamable HTTP (python main.py http [start])。", file=sys.stderr)
        return 2

    if command == "doctor":
        from .ui import doctor
        return doctor.main(rest)

    if command == "http":
        from .transports import httpd
        from .transports.httpd import DEFAULT_HOST, DEFAULT_PORT, main as http_main

        action = rest[0] if rest and rest[0] in ("start", "stop", "status") else None
        if action:
            if action == "start":
                print(json.dumps(httpd.start_service(), ensure_ascii=False))
            elif action == "stop":
                # Force detaches only the observer transport; native tasks are not cancelled.
                force = "--force" in rest
                print(json.dumps(httpd.stop_service(force=force), ensure_ascii=False))
            else:
                print(json.dumps(httpd.service_status(), ensure_ascii=False))
            return 0

        host, port, token = DEFAULT_HOST, DEFAULT_PORT, None
        index = 0
        while index < len(rest):
            value = rest[index]
            if value == "--host" and index + 1 < len(rest):
                host = rest[index + 1]; index += 2; continue
            if value == "--port" and index + 1 < len(rest):
                port = int(rest[index + 1]); index += 2; continue
            if value == "--token" and index + 1 < len(rest):
                token = rest[index + 1]; index += 2; continue
            index += 1
        return http_main(host, port, token)

    if command == "panel":
        from .ui.panel_app import main as panel_main

        return panel_main(rest)

    print(f"Unknown command: {command}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())