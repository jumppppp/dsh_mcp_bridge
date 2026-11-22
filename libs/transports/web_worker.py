"""Persistent, detached output supervisor for one owned native DSH Web."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

from ..core.secrets import write_secret_json
from .web_launch import _dsh_cli, _LAUNCH_URL_RE


def run(state: Path, port: int, home: str, extra_args: list[str] | None = None):
    state.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    credential = secrets.token_urlsafe(32)
    class Control(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            supplied = self.headers.get("X-Worker-Token", "")
            if self.path != "/stop" or not secrets.compare_digest(supplied, credential):
                self.send_error(403)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"stopping":true}')
            stop.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Control)
    server.daemon_threads = True
    record = {"worker_pid": os.getpid(), "control_port": server.server_port,
              "control_token": credential, "port": port, "pid": None, "url": "",
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    # Secure control metadata exists before any child can be created.
    write_secret_json(state / "web_launch.json", record)
    control = threading.Thread(target=server.serve_forever, daemon=True)
    control.start()
    child = None
    reader = None
    try:
        command = [*_dsh_cli(), "web", "--port", str(port), "--no-open", *(extra_args or [])]
        display = "dsh web --port {} --no-open{}".format(
            port, (" " + " ".join(extra_args or [])) if extra_args else "")
        record["command"] = display
        log_file = state / "web-launch.log"
        if log_file.is_file() and log_file.stat().st_size > 2 * 1024 * 1024:
            try:
                log_file.rename(state / "web-launch.log.1")
            except OSError:
                pass
        with log_file.open("w", encoding="utf-8") as _header:
            _header.write("=== started " + time.strftime("%Y-%m-%dT%H:%M:%S")
                          + " :: " + display + " ===\n")
        child = subprocess.Popen(command,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "DSH_HOME": home},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        record["pid"] = child.pid
        def drain():
            try:
                with log_file.open("a", encoding="utf-8") as log:
                    for line in child.stdout:
                        match = _LAUNCH_URL_RE.search(line)
                        if match and not record["url"]:
                            record["url"] = "http://127.0.0.1:{}/?token={}".format(match.group(1), match.group(2))
                            write_secret_json(state / "web_launch.json", record)
                        line = re.sub(r"(?i)(token=)[^&\s]+", r"\1[REDACTED]", line)
                        line = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~-]+", r"\1[REDACTED]", line)
                        log.write(line)
                        log.flush()
            except Exception:
                stop.set()
            finally:
                child.stdout.close()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        delay = 0.05
        while child.poll() is None and not stop.wait(delay):
            delay = min(delay * 2, 1)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if reader:
            reader.join(timeout=5)
        if child is not None:
            exit_payload = {"exit_code": child.returncode,
                            "exited_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "command": record.get("command", ""),
                            "pid": record.get("pid"),
                            "started_at": record.get("started_at", "")}
            try:
                (state / "web-exit.json").write_text(
                    json.dumps(exit_payload, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            try:
                with log_file.open("a", encoding="utf-8") as _tail:
                    _tail.write("=== exited " + exit_payload["exited_at"]
                                + " code " + str(child.returncode) + " ===\n")
            except OSError:
                pass
        server.shutdown()
        server.server_close()
        control.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--home", required=True)
    parser.add_argument("--extra-args", default="[]", help="JSON list of extra dsh web args")
    args = parser.parse_args()
    try:
        extra = json.loads(args.extra_args)
    except ValueError:
        extra = []
    run(Path(args.state), args.port, args.home, extra if isinstance(extra, list) else [])
