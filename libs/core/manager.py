"""Native Web session observations, durable events, waits and completion sinks.

Execution belongs to the existing DSH Web Runtime. V2 does not spawn Agent
processes, reap external processes or automatically replay unknown deliveries.
Disconnected observation and waiting approval are distinct from execution failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import RuntimeConfig, context_limit_tokens
from .policy import KNOWN_PERMISSION_PRESETS
from ..transports.external import ExternalSessions

ACTIVE = ("queued", "starting", "running", "waiting_approval", "waiting_input", "interrupting")

# The session DSH Web shows in place of the individual bridge agents.
PARENT_TITLE = "Claude Code / Codex 子代理"

_INTERESTING_EVENT = re.compile(r"^(tool/|turn/|step/|permission/|sandbox/|approval/)")


def settled_status(reason: dict | None) -> str:
    """Map a DSH turn-end reason onto an agent status."""
    if not reason:
        return "error"
    kind = reason.get("kind")
    if kind == "completed":
        return "completed"
    # DSH Web reports user cancellation as kind=aborted with
    # reason.kind=user (session/cancel); both spell interrupted.
    if kind in ("cancelled", "interrupted"):
        return "interrupted"
    if kind == "aborted" and ((reason.get("reason") or {}).get("kind") == "user"):
        return "interrupted"
    error = reason.get("error") or {}
    if error.get("code") == "CONTEXT_WINDOW_EXCEEDED":
        return "context_exhausted"
    return "error"


def _defaults() -> dict:
    """Lazy access to the configured default provider/model."""
    from .config import default_model

    return default_model()


def title_from_task(task: str) -> str:
    for line in str(task).splitlines():
        cleaned = " ".join(line.split()).strip()
        if cleaned:
            return cleaned if len(cleaned) <= 60 else cleaned[:59] + "…"
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")




def _process_alive(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True



def _norm_ws_path(value: str) -> str:
    return str(value).replace("\\", "/").rstrip("/").lower()


def _with_registry_lock(fn, timeout: float = 10.0):
    """Run ``fn`` while holding a cross-process lock on the registry.

    workspace.json is read-modify-written by several bridge processes (HTTP,
    panel, daemon, stdio MCP sessions). Without a process-wide lock two
    concurrent publishes can each read the old table and overwrite the other,
    losing or duplicating rows.
    """
    import os
    import time as _time

    from .platform import workspace_registry

    lock_path = workspace_registry().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = _time.monotonic() + timeout
    fd = None
    while _time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # Stale lock from a crashed process: drop it after 30s.
            try:
                if _time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            _time.sleep(0.05)
    if fd is None:
        raise TimeoutError("timed out waiting for the workspace registry lock")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return fn()
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _register_published_session(agent_id: str, cwd: str | None = None) -> None:
    """Add a published session to the REAL workspace registry.

    DSH Web renders the session list from ``storages/workspace.json``; copying
    the session log alone is not enough. Look the workspace up by path (case
    preserved compare) and append the id, creating a row when missing.
    """
    import datetime
    import uuid

    from .platform import storages_root, workspace_registry

    # Resolve the session cwd OUTSIDE the lock closure; rebinding it inside
    # ``_apply`` made Python treat it as a closure-local and raised
    # "cwd referenced before assignment".
    if not cwd:
        db = Path(self.config.database)
        if db.is_file():
            try:
                import sqlite3 as _sqlite

                con = _sqlite.connect(str(db))
                try:
                    row = con.execute("SELECT data FROM agents WHERE id=?", (agent_id,)).fetchone()
                    if row:
                        cwd = json.loads(row[0]).get("cwd")
                finally:
                    con.close()
            except Exception:  # noqa: BLE001
                pass
    if not cwd:
        return

    def _apply() -> None:
        registry = workspace_registry()
        payload = {}
        if registry.is_file():
            try:
                payload = json.loads(registry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
        workspaces = payload.setdefault("tables", {}).setdefault("workspaces", {})

        target = None
        for wid, entry in workspaces.items():
            if _norm_ws_path(entry.get("path") or "") == _norm_ws_path(cwd):
                target = wid
                break
        if target is None:
            target = str(uuid.uuid4())
            # Store Windows-native separators. DSH resolves workspaces via
            # realpath() (backslashes) and compares strictly, so writing a
            # forward-slash path creates a SECOND workspace for the same
            # directory and later fails validation.
            canonical = str(cwd or "").replace("/", "\\")
            workspaces[target] = {
                "path": canonical,
                "title": Path(canonical).name or canonical,
                "sessionIds": [],
                "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        session_ids = workspaces[target].setdefault("sessionIds", [])
        if agent_id not in session_ids:
            session_ids.append(agent_id)
        workspaces[target]["updatedAt"] = (
            datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        )
        glob = payload.setdefault("global", {})
        order = [w for w in (glob.get("workspaceIds") or []) if w in workspaces]
        order += [w for w in workspaces if w not in order]
        glob["workspaceIds"] = order
        tmp = registry.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(registry)

    _with_registry_lock(_apply)


def _pid_alive(pid) -> bool:
    """Best-effort process-alive check (Windows + POSIX, no psutil)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    import sys as _sys

    if _sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        import os as _os

        _os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _publish_enabled() -> bool:
    """Tests/tools set DSH_SUBAGENT_PUBLISH=0 so their sessions never land in
    the real DSH sessions tree (that would pollute the user's workspace)."""
    import os as _os

    return _os.environ.get("DSH_SUBAGENT_PUBLISH", "1").strip() != "0"


def _auto_migrate_enabled() -> bool:
    """把已完结会话发布（复制+登记）到 R:/dsh-data/sessions，让 DSH Web 可打开。

    V2 固定 021：子进程直接写 v4，任务完结后复制登记即可，无需迁移。
    """
    import os as _os

    return _os.environ.get("DSH_SUBAGENT_AUTOMIGRATE", "1").strip() != "0"


class _LockedConnection:
    """线程安全 + 自动提交的 SQLite 连接代理。

    面板进程里同一个连接会被 Flask 请求线程与异步循环（watchdog）并发使用；
    原生 sqlite3 连接并发不安全，且写操作若在 commit 前抛异常会留下未结束的
    隐式事务——之后所有 SELECT 都被钉在旧快照上（面板显示陈旧状态），同时
    阻塞 WAL checkpoint（WAL 无限膨胀）。

    autocommit（isolation_level=None）从根上消除隐式事务；锁把语句串行化；
    每次执行后若仍有事务则立即回滚（守卫），保证连接永远不会卡在旧快照。
    """

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock):
        self._conn = connection
        self._lock = lock

    def execute(self, sql: str, params=()):
        with self._lock:
            cursor = self._conn.execute(sql, params)
            # autocommit 下不应有事务；守卫一次，防止未来改动引入长事务
            if self._conn.in_transaction:
                self._conn.rollback()
            return cursor

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    def commit(self) -> None:
        with self._lock:
            if self._conn.in_transaction:
                self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            if self._conn.in_transaction:
                self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            if self._conn.in_transaction:
                self._conn.rollback()
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class Manager:
    def __init__(self, config: RuntimeConfig, runtime_class=None) -> None:
        self.config = config
        self.RuntimeClass = runtime_class
        self.live: dict = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, list] = {}
        self.watchers: dict[str, asyncio.Task] = {}
        self.external = ExternalSessions(self)
        self._shutdown_task: asyncio.Task | None = None
        # P0-1 completion notifications: MCP sessions and webhooks subscribe here.
        self._notifiers: list = []
        # P0-2 watchdog: marks long-idle running tasks as stalled.
        self._watchdog_task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        # P0-3 concurrency gate: tasks beyond the limit wait in this FIFO queue.
        self._queue: list[str] = []
        self._launching: set[str] = set()
        # P0: pending runtime releases; a resume must wait for them or the new
        # writer hits "already owned by an active write handle".
        self._releasing: dict[str, asyncio.Task] = {}

        self._db_raw = sqlite3.connect(
            str(config.database), check_same_thread=False, timeout=15.0,
            isolation_level=None,  # 自动提交：不存在隐式长事务
        )
        self._db_lock = threading.RLock()
        self._db_closed = False
        self.db = _LockedConnection(self._db_raw, self._db_lock)
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS parents(cwd TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                                               seeded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                                              agent TEXT NOT NULL, time TEXT NOT NULL,
                                              type TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_agent_seq ON events(agent, seq);
            """
        )
        self.db.commit()
        self._start_db_maintenance()

        # Reap grandchildren left by a hard-killed previous process: they keep
        # per-session write locks that would make resume fail.
        # Web-only V2 never reaps processes belonging to another runtime.

        # Work that was running when the service died cannot be replayed —
        # but ONLY when the OWNING process is this process (or is gone).
        # The database is shared across transports (panel / http / stdio), so
        # unconditionally interrupting every active row killed tasks that other
        # processes were still running.
        my_pid = os.getpid()
        for agent in self.list():
            if agent.get("external"):
                owner = agent.get("owner_pid")
                if agent.get("status") in ACTIVE and (
                    owner is None or owner == my_pid or not _pid_alive(owner)
                ):
                    agent["status"] = "disconnected"
                    agent.pop("pending_interactions", None)
                    agent["connection_error"] = "Observer restarted; reconnect to verify native task state."
                    self.save(agent)
                continue
            if agent.get("status") in ACTIVE:
                owner = agent.get("owner_pid")
                # Only claim rows whose owner is THIS process or provably dead.
                # A missing owner (rows written before ownership was tracked)
                # may still be running under another process — never touch it.
                if owner is not None and (owner == my_pid or not _pid_alive(owner)):
                    agent["status"] = "interrupted"
                    agent["error"] = "Service restarted; work was not automatically replayed."
                    agent["interrupted_by"] = "service start (pid {})".format(my_pid)
                    self.save(agent)
            elif agent.get("status") == "error" and settled_status(agent.get("finish_reason")) == "context_exhausted":
                agent["status"] = "context_exhausted"
                self.save(agent)

    # -- store -------------------------------------------------------------
    def save(self, agent: dict, bump: bool = True) -> None:
        if bump:
            agent["updated_at"] = _now()
        self.db.execute(
            "INSERT OR REPLACE INTO agents VALUES (?, ?)",
            (agent["id"], json.dumps(agent, ensure_ascii=False)),
        )
        self.db.commit()
        self._notify_state(agent)

    def get(self, agent_id: str) -> dict:
        row = self.db.execute("SELECT data FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown agent: {agent_id}")
        return json.loads(row[0])

    def list(self) -> list[dict]:
        rows = self.db.execute("SELECT data FROM agents ORDER BY rowid DESC").fetchall()
        return [json.loads(r[0]) for r in rows]

    def event(self, agent_id: str, event_type: str, data: dict) -> None:
        self.db.execute(
            "INSERT INTO events(agent, time, type, data) VALUES (?,?,?,?)",
            (agent_id, _now(), event_type, json.dumps(data, ensure_ascii=False)),
        )
        self.db.commit()

    def events(self, agent_id: str, after: int = 0, limit: int = 50, latest: bool = False,
               before: int | None = None) -> dict:
        self.get(agent_id)
        if before is not None and before > 0:
            # Cursor for "load earlier": the newest `limit` events STRICTLY
            # older than `before`, returned in chronological order. Long
            # sessions need this; `latest` alone always kept only the tail.
            rows = self.db.execute(
                "SELECT seq, time, type, data FROM events WHERE agent=? AND seq<? "
                "ORDER BY seq DESC LIMIT ?",
                (agent_id, before, limit),
            ).fetchall()
            rows = list(reversed(rows))
        elif latest and after == 0:
            # Newest `limit` events in chronological order. A busy task easily
            # produces >500 events; returning the OLDEST ones made chat/trace
            # stop at the beginning of the day ("看不到最近的").
            rows = self.db.execute(
                "SELECT seq, time, type, data FROM events WHERE agent=? ORDER BY seq DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = self.db.execute(
                "SELECT seq, time, type, data FROM events WHERE agent=? AND seq>? ORDER BY seq LIMIT ?",
                (agent_id, after, limit),
            ).fetchall()
        items = [
            {"seq": r[0], "time": r[1], "type": r[2], "data": json.loads(r[3])}
            for r in rows
        ]
        return {"events": items, "next_cursor": items[-1]["seq"] if items else after}

    def count_events(self, agent_id: str) -> int:
        """Total event rows for an agent (for "显示最近 N / 共 M 条" labels)."""
        self.get(agent_id)
        row = self.db.execute(
            "SELECT COUNT(*) FROM events WHERE agent=?", (agent_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def has_events_before(self, agent_id: str, seq: int) -> bool:
        """Whether any OLDER event exists than ``seq`` (load-earlier cursor).

        Event seq is a global rowid, NOT per-agent starting at 1, so "seq > 1"
        is the wrong test for "more history". A short session whose whole event
        window already fits must show no 加载更早 button.
        """
        if not seq or seq <= 0:
            return False
        row = self.db.execute(
            "SELECT 1 FROM events WHERE agent=? AND seq<? LIMIT 1", (agent_id, seq)
        ).fetchone()
        return row is not None

    def public_events(
        self,
        agent_id: str,
        after: int = 0,
        limit: int = 30,
        *,
        before: int | None = None,
        include_progress: bool = False,
        include_descendants: bool = False,
        include_tool_events: bool = False,
        event_id: int | None = None,
        max_chars: int = 12000,
    ) -> dict:
        """Projected event view used by ``dsh_events``.

        Defaults to completed root-turn final answers. ``turn/end`` rows get the
        preceding assistant message text attached as ``final_text`` so a reader
        can accept one consolidated artifact.
        """
        from .projection import project_events

        self.get(agent_id)
        only_finals = not include_progress and not include_tool_events

        if event_id is not None:
            rows = self.db.execute(
                "SELECT seq, time, type, data FROM events WHERE agent=? AND seq=?",
                (agent_id, event_id),
            ).fetchall()
        elif before is not None and before > 0:
            if only_finals:
                rows = self.db.execute(
                    "SELECT seq, time, type, data FROM events WHERE agent=? AND seq<? "
                    "AND type='turn/end' ORDER BY seq DESC LIMIT ?",
                    (agent_id, before, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT seq, time, type, data FROM events WHERE agent=? AND seq<? "
                    "ORDER BY seq DESC LIMIT ?",
                    (agent_id, before, limit),
                ).fetchall()
            rows = list(reversed(rows))
        elif only_finals:
            rows = self.db.execute(
                "SELECT seq, time, type, data FROM events WHERE agent=? AND seq>? "
                "AND type='turn/end' ORDER BY seq LIMIT ?",
                (agent_id, after, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT seq, time, type, data FROM events WHERE agent=? AND seq>? "
                "ORDER BY seq LIMIT ?",
                (agent_id, after, limit),
            ).fetchall()

        items: list[dict] = []
        for r in rows:
            data = json.loads(r[3])
            kind = r[2]
            if kind == "turn/end" and (data.get("reason") or {}).get("kind") == "completed":
                prev = self.db.execute(
                    "SELECT type, data FROM events WHERE agent=? AND seq<? "
                    "AND type IN ('assistant/message','turn/start') ORDER BY seq DESC LIMIT 1",
                    (agent_id, r[0]),
                ).fetchone()
                if prev and prev[0] == "assistant/message":
                    try:
                        prev_data = json.loads(prev[1])
                        data = {**data, "final_text": prev_data.get("text") or ""}
                    except ValueError:
                        pass
            items.append({"seq": r[0], "time": r[1], "type": kind, "data": data})

        projected, last_seq = project_events(
            items,
            only_finals=only_finals,
            include_tool_events=include_tool_events,
            max_chars=max_chars,
        )
        return {
            "events": projected,
            "next_cursor": last_seq or after,
            "has_more": len(rows) >= limit,
            # 原始窗口内最早一条的 seq（用于精确判断"还有没有更早"，rowid 非从 1 开始）
            "oldest_seq": rows[0][0] if rows else 0,
        }

    # -- waiters -----------------------------------------------------------
    def _notify_state(self, agent: dict) -> None:
        for callback in list(self._waiters.get(agent["id"], [])):
            try:
                callback(agent)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _active(agent: dict) -> bool:
        return agent.get("status") in ACTIVE

    @staticmethod
    def _wait_result(agent: dict, outcome: str) -> dict:
        status = agent.get("status")
        if status == "disconnected":
            next_action = "reconnect"
            outcome = "disconnected"
        elif agent.get("pending_interactions") or status in ("waiting_approval", "waiting_input"):
            next_action = "handle_interaction"
            outcome = "interaction_needed"
        elif Manager._active(agent):
            next_action = "continue_waiting"
        elif status == "completed":
            next_action = "review_and_continue"
        elif status == "context_exhausted":
            next_action = "start_new_agent"
        elif status == "error":
            next_action = "handle_error"
        else:
            next_action = "respect_stop"
        from .diagnostics import enrich
        return {**enrich(agent), "wait_outcome": outcome, "next_action": next_action}

    async def wait(self, agent_id: str, seconds: float | None = None) -> dict:
        # Timed questions may continue after the native turn settled. Refresh
        # their native projection when an observation is already connected.
        if agent_id in self.external.live:
            try:
                await asyncio.wait_for(self.external.interactions.listing(agent_id), timeout=3)
            except Exception:
                pass  # Keep the observed state; failed reads never replay work.
        initial = self.get(agent_id)
        if initial.get("pending_interactions") or initial.get("status") in ("waiting_approval", "waiting_input"):
            return self._wait_result(initial, "interaction_needed")
        if not self._active(initial):
            return self._wait_result(initial, "settled")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def on_state(agent: dict) -> None:
            if (not self._active(agent) or agent.get("pending_interactions") or agent.get("status") in ("waiting_approval", "waiting_input")) and not future.done():
                future.set_result(agent)

        self._waiters.setdefault(agent_id, []).append(on_state)
        try:
            if seconds is None:
                settled = await future
                return self._wait_result(settled, "settled")
            try:
                settled = await asyncio.wait_for(asyncio.shield(future), timeout=seconds)
                return self._wait_result(settled, "settled")
            except asyncio.TimeoutError:
                return self._wait_result(self.get(agent_id), "timeout")
        finally:
            try:
                self._waiters.get(agent_id, []).remove(on_state)
            except ValueError:
                pass

    # -- serialisation -----------------------------------------------------
    def lock(self, key: str) -> asyncio.Lock:
        if key not in self.locks:
            self.locks[key] = asyncio.Lock()
        return self.locks[key]

    # -- workspace mount point --------------------------------------------
    def parent_for(self, cwd: str) -> str:
        row = self.db.execute("SELECT session_id FROM parents WHERE cwd=?", (cwd,)).fetchone()
        if row:
            return row[0]
        session_id = "session-" + str(uuid.uuid4())
        self.db.execute("INSERT INTO parents(cwd, session_id) VALUES (?,?)", (cwd, session_id))
        self.db.commit()
        return session_id

    def _mount_enabled(self) -> bool:
        """Whether to create the per-workspace parent mount point.

        The mount point only helps when DSH Web can read the child sessions.
        V2 固定 021：子进程直接写 v4 且 Web 只读 v4，默认跳过挂载点，
        避免父目录缺失时报错：

        ``cannot validate session …: session persistence holds no such session``.
        Set ``DSH_SUBAGENT_MOUNT=1`` to enable it on a matching runtime.
        """
        return os.environ.get("DSH_SUBAGENT_MOUNT", "").strip() == "1"

    async def ensure_parent(self, agent: dict) -> str | None:
        if not self._mount_enabled():
            return None
        parent = self.parent_for(agent["cwd"])
        row = self.db.execute("SELECT seeded FROM parents WHERE cwd=?", (agent["cwd"],)).fetchone()
        if row and row[0]:
            return parent
        runtime = await self.RuntimeClass.spawn(agent, self.config)
        try:
            await runtime.request("initialize", {
                "cwd": agent["cwd"], "provider": agent.get("provider"),
                "model": agent.get("model"),
                **({"reasoningEffort": agent["effort"]} if agent.get("effort") else {}),
                "permission": agent.get("permission"), "preset": agent.get("preset"),
                "resume": False,
            })
            await runtime.request(
                "parent/seed",
                {"cwd": agent["cwd"], "parent": parent, "parentTitle": PARENT_TITLE},
                timeout=120.0,
            )
            self.db.execute("UPDATE parents SET seeded=1 WHERE cwd=?", (agent["cwd"],))
            self.db.commit()
        finally:
            await runtime.close()
        return parent

    # -- runtime -----------------------------------------------------------
    async def runtime(self, agent: dict):
        raise ValueError("V2 does not launch independent Agent runtimes; connect the native Web session")


    def _notification(self, agent_id: str, method: str, params) -> None:
        if params and params.get("sessionId") and params["sessionId"] != agent_id:
            self.event(agent_id, "descendant/" + method, params)
            return

        agent = self.get(agent_id)

        if method == "session.text":
            chunk = params.get("chunk") or {}
            text = chunk.get("text") or chunk.get("delta") or ""
            agent["partial_text"] = (agent.get("partial_text") or "")[-16000:] + text
            self.save(agent)
            return

        if method == "session.event":
            event = params.get("event") or {}
            kind = event.get("type")
            data = event.get("data") or {}
            if kind == "turn/start":
                agent["status"] = "running"
                agent["answer"] = ""
                agent["partial_text"] = ""
                agent["finish_reason"] = None
                # A new turn supersedes any earlier interrupt marker; otherwise a
                # later "completed" task still shows a stale interrupted_by.
                agent["interrupted_by"] = None
                self.event(agent_id, kind, {**data, "session_seq": event.get("seq")})
            elif kind == "assistant/message":
                blocks = (data.get("message") or {}).get("content") or []
                agent["answer"] = "\n".join(
                    b.get("text", "") for b in blocks if b.get("type") == "text"
                )
                self.event(agent_id, kind, {
                    "seq": event.get("seq"),
                    "text": agent["answer"],
                    "usage": data.get("usage"),
                    "interrupted": data.get("interrupted"),
                })
                usage = data.get("usage") or {}
                if usage:
                    # P1-5: accumulate token usage across turns for cost reporting.
                    cost = agent.setdefault("cost", {"input": 0, "output": 0, "total": 0, "messages": 0})
                    cost["input"] += int(usage.get("inputTokens") or 0)
                    cost["output"] += int(usage.get("outputTokens") or 0)
                    cost["total"] += int(usage.get("totalTokens") or 0)
                    cost["messages"] += 1
                if usage.get("totalTokens"):
                    agent["context_tokens"] = usage["totalTokens"]
            elif kind == "turn/end":
                agent["finish_reason"] = data.get("reason")
                self.event(agent_id, kind, data)
            elif _INTERESTING_EVENT.match(kind or ""):
                self.event(agent_id, kind, {**data, "session_seq": event.get("seq")})
            agent["last_event"] = kind
            self.save(agent)
            return

        if method == "session.status":
            status = params.get("status")
            finish = agent.get("finish_reason") or {}
            if status == "running":
                agent["status"] = "running"
                # A new turn clears the previous failure text so the panel never
                # shows "error + running" at the same time.
                agent["error"] = None
            elif agent.get("status") == "interrupting" or finish.get("kind") in ("cancelled", "interrupted"):
                agent["status"] = "interrupted"
            else:
                agent["status"] = settled_status(finish)
            if agent["status"] == "completed":
                agent["last_completed_answer"] = agent.get("answer")
            # A completed/idle task must not keep the error text left over from a
            # previous service restart, or the panel shows "interrupted" forever.
            if agent["status"] in ("completed", "idle"):
                agent["error"] = None
            self.save(agent)
            self.event(agent_id, method, params)
            if agent.get("status") in ("completed", "error", "interrupted"):
                self._notify_settled(agent_id)
                self._pump_queue()
            if status == "idle":
                # Release the process; the session stays resumable.
                self._schedule_release(agent_id)
                # 021 writes v4 natively (resume still works), so publish as soon
                # as the task settles — waiting for close meant Codex-driven tasks
                # never appeared in DSH Web.
                from . import runtime021

                if _auto_migrate_enabled():
                    asyncio.create_task(self._auto_migrate(agent_id))

            return

    async def _auto_migrate(self, agent_id: str) -> None:
        """Make a settled session readable by DSH Web.

        * V2 固定 021：子进程已在桥接自己的 DSH_HOME 写好 v4；把它复制到真实的
          ``R:/dsh-data/sessions`` 并登记，DSH Web 即可直接打开（不触碰用户环境）。
        """
        import shutil

        from . import runtime021

        from .platform import sessions_root

        if not _publish_enabled():
            return
        root = sessions_root()
        try:
            produced = list(
                (runtime021.HOME / "sessions").glob(
                    "**/{}/session.v4.jsonl.zstd".format(agent_id)
                )
            )
            for src in produced:
                workspace = src.parent.parent.name  # --encoded-cwd--
                dst = root / workspace / agent_id / "session.v4.jsonl.zstd"
                if dst.is_file():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            # DSH Web lists sessions from storages/workspace.json; without
            # this registration the file is present but the UI never shows it.
            try:
                _register_published_session(agent_id)
            except Exception as error:  # noqa: BLE001
                self._soft_error(agent_id, "register-session", error)
        except Exception as error:  # noqa: BLE001 - never break the agent lifecycle
            self._soft_error(agent_id, "publish-session", error)

    async def _release_quietly(self, agent_id: str) -> None:
        try:
            await self.release(agent_id)
        except Exception as error:  # noqa: BLE001
            self.event(agent_id, "runtime/release-error", {"message": str(error)})
        finally:
            # The concurrency slot only really frees once the child is gone.
            self._pump_queue()

    # -- operations --------------------------------------------------------
    def _web_api_url(self, params: dict) -> str:
        """Where to create sessions through the DSH Web API (Web-API-first)."""
        value = str(params.get("web_url") or "").strip()
        if value:
            return value
        value = str(os.environ.get("DSH_SUBAGENT_WEB_URL", "")).strip()
        if value:
            return value
        return self.external._default_web_url()

    async def start(self, params: dict) -> dict:
        if params.get("no_web"):
            raise ValueError("V2 executes only through DSH Web")
        task = str(params.get("task") or "").strip()
        cwd = str(params.get("cwd") or "").strip()
        workspace_id = str(params.get("workspace_id") or "").strip()
        web_url = self._web_api_url(params)
        if not web_url:
            raise ValueError("DSH Web is not configured; independent execution is unavailable")
        if workspace_id:
            baseline = await self.external.web_workspaces(web_url)
            row = next((w for w in baseline.get("workspaces", []) if w.get("workspaceId") == workspace_id), None)
            if not row or not row.get("path"):
                raise ValueError("Unknown workspace_id")
            if cwd and Path(cwd).resolve() != Path(row["path"]).resolve():
                raise ValueError("cwd and workspace_id refer to different workspaces")
            cwd = row["path"]
        from .preferences import resolve
        params = resolve({**params, "cwd": cwd}, Path(self.config.database).parent)
        cwd, permission = params["cwd"], params["permission"]
        if permission not in KNOWN_PERMISSION_PRESETS:
            raise ValueError("Unsupported permission")
        try:
            created = await self.external.create(
                web_url, cwd, (params.get("name") or "").strip() or title_from_task(task),
                params.get("preset") or "standard", permission=permission,
                model=params.get("model"), provider=params.get("provider"), effort=params.get("effort"),
                workspace_id=workspace_id or None)
        except ValueError:
            raise
        except Exception as error:
            raise RuntimeError("DSH Web session creation failed; independent execution was not started") from error
        self.event(created["id"], "bridge/created", {"via": "web-api"})
        if not params.get("no_preamble"):
            from .preamble import apply
            task = apply(task, permission, cwd)
        await self.external.send(created["id"], task, "queue")
        return self.get(created["id"])

    async def resume(self, agent_id: str, note: str = "") -> dict:
        """Resume an interrupted/error/closed task through MCP.

        Equivalent to ``followup`` with a sensible default prompt, so a client
        that only sees ``status: interrupted`` can bring the task back without
        inventing a new instruction.
        """
        agent = self.get(agent_id)
        if agent.get("status") == "closed":
            agent["status"] = "idle"
            self.save(agent)
        task = (note or "").strip() or (
            "继续完成上次未完成的工作；如果已经完成，简要说明结果并列出后续可选项。"
        )
        return await self.followup(agent_id, task)

    # -- adopt existing sessions (P1-4, cross-platform) --------------------
    def adopt(self, params: dict) -> dict:
        """Register an existing DSH session as a bridge task (no process yet).

        Unlike the original project's Linux-only ``adopt`` (flock takeover), this
        works on any platform: it only records the session so ``dsh_followup``
        can resume it. ``cwd`` is required because the ``--encoded-path--``
        directory name is ambiguous when the path itself contains hyphens.
        """
        session_id = (params.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id must not be blank")
        cwd = (params.get("cwd") or "").strip()
        if not cwd:
            raise ValueError("cwd must be provided (the encoded directory name is ambiguous)")
        if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError("cwd must be an existing absolute directory")

        existing = self.get(session_id)
        if existing:
            # Adopting a session we already know means "take it back": closed or
            # failed records become idle again so followup can resume them.
            if existing.get("status") in ("closed", "error", "interrupted"):
                existing["status"] = "idle"
                existing["error"] = None
                existing["cwd"] = cwd
                existing["adopted"] = True
                self.save(existing)
                self.event(existing["id"], "adopted", {"cwd": cwd, "reopened": True})
            return existing
        from .platform import sessions_root

        hits = [p for p in sessions_root().glob(
            "**/{}/session.*.jsonl.zstd".format(session_id))]
        if not hits:
            raise ValueError('session "{}" not found under the DSH sessions root'.format(session_id))
        agent = {
            "id": session_id,
            "name": (params.get("name") or "").strip() or "adopted-" + session_id[:8],
            "cwd": cwd,
            "model": params.get("model") or _defaults()["model"],
            "provider": params.get("provider") or _defaults()["provider"],
            "effort": None,
            "permission": params.get("permission") or "workspace-write",
            "preset": params.get("preset") or "standard",
            "status": "idle",
            "created_at": _now(),
            "answer": "",
            "partial_text": "",
            "persisted": True,
            "adopted": True,
            "owner_pid": os.getpid(),
        }
        self.save(agent)
        self.event(agent["id"], "adopted", {"cwd": cwd, "log": str(hits[0])})
        return agent

    # -- concurrency gate (P0-3) ------------------------------------------
    def _max_concurrent(self) -> int:
        raw = os.environ.get("DSH_SUBAGENT_MAX_CONCURRENT", "0").strip()
        try:
            return max(0, int(raw))
        except ValueError:
            return 0  # 0 = unlimited (the permissive default)

    def _active_count(self) -> int:
        """Tasks THIS Manager actually holds a child for.

        Count from ``self.live`` (per-process) instead of the shared database:
        DB rows include tasks owned by other transports (e.g. the HTTP service
        runs its own Manager), which would block this transport's queue forever.
        """
        return len(self.live) + len(self._launching)

    def _pump_queue(self) -> None:
        limit = self._max_concurrent()
        while self._queue:
            if limit and self._active_count() >= limit:
                return
            agent_id = self._queue.pop(0)
            try:
                agent = self.get(agent_id)
            except KeyError:
                continue  # the queued task was deleted before its turn came
            if not agent or agent.get("status") != "queued":
                continue
            task = agent.pop("queued_task", None) or ""
            agent["status"] = "starting"
            self.save(agent)
            self.event(agent_id, "dequeued", {"remaining": len(self._queue)})
            self._launching.add(agent_id)
            asyncio.create_task(self._launch(agent_id, task))

    async def _launch(self, agent_id: str, task: str) -> None:
        raise ValueError("Independent execution was retired; attach the native Web session")

    async def submit(self, agent_id: str, task: str) -> dict:
        async with self.lock(agent_id):
            agent = self.get(agent_id)
            agent["status"] = "starting"
            agent["answer"] = ""
            agent["partial_text"] = ""
            agent["finish_reason"] = None
            agent["error"] = None
            agent["interrupted_by"] = None  # new turn: previous reason no longer current
            agent["owner_pid"] = os.getpid()
            self.save(agent)

            try:
                runtime = await self.runtime(agent)
            except Exception as error:  # noqa: BLE001
                current = self.get(agent_id)
                current["status"] = "error"
                current["error"] = str(error)
                self.save(current)
                raise

            agent = self.get(agent_id)
            agent["status"] = "running"
            self.save(agent)

            # Give the DSH session a real title, otherwise DSH Web shows
            # "未命名" for every bridged task (the name only lived in our DB).
            if agent.get("name"):
                try:
                    await runtime.request("session/rename",
                                          {"sessionId": agent_id, "title": agent["name"]})
                except Exception:  # noqa: BLE001 - title is cosmetic
                    pass

            try:
                receipt = await runtime.request(
                    "session/prompt",
                    {"sessionId": agent_id, "contentBlocks": [{"type": "text", "text": task}]},
                )
            except Exception as error:  # noqa: BLE001
                current = self.get(agent_id)
                current["status"] = "error"
                current["error"] = str(error)
                self.save(current)
                try:
                    await runtime.close()
                finally:
                    self.live.pop(agent_id, None)
                raise

            agent = self.get(agent_id)
            agent["persisted"] = True
            agent["message_id"] = receipt.get("messageId") if isinstance(receipt, dict) else None
            self.save(agent)
            self.event(agent_id, "bridge/prompt", {"message_id": agent["message_id"], "task": task})
            return agent

    async def followup(self, agent_id: str, task: str, mode: str = "queue", idle_only: bool = True) -> dict:
        # A resume racing a pending release is the classic
        # "already owned by an active write handle" case.
        await self._await_release(agent_id)
        self._ensure_session_local(agent_id)
        async with self.lock(agent_id):
            agent = self.get(agent_id)
            # 前置提示词作用范围：默认只在新对话注入；可配置为每条请求都注入。
            try:
                from .preamble import SCOPE_EVERY, apply as apply_preamble, load_preamble

                if (load_preamble().get("scope") == SCOPE_EVERY
                        and not task.startswith("[系统前置约束")):
                    task = apply_preamble(task, agent.get("permission"), agent.get("cwd") or "")
            except Exception:  # noqa: BLE001
                pass
            if agent.get("external"):
                return await self.external.send(agent_id, task, mode, idle_only)
            if agent["status"] == "closed":
                raise ValueError("Agent is closed")
            if agent["status"] in ACTIVE:
                raise ValueError("Agent is busy; interrupt it before redirecting, or wait until idle.")
            if agent["status"] == "context_exhausted":
                raise ValueError(
                    "Agent exhausted its model context; start a new agent with a "
                    "self-contained handoff (last_completed_answer is in dsh_status)."
                )
            limit = context_limit_tokens(agent.get("provider"))
            if agent.get("preset") == "minimal" and limit and (agent.get("context_tokens") or 0) >= limit * 0.75:
                raise ValueError(
                    f"Agent context is {agent.get('context_tokens')} of {limit} tokens and the "
                    "minimal preset does not compact; start a new agent for this task."
                )
        return await self.submit(agent_id, task)

    async def interrupt(self, agent_id: str, reason: str = "用户请求中断") -> dict:
        async with self.lock(agent_id):
            agent = self.get(agent_id)
            if agent.get("external"):
                return await self.external.interrupt(agent_id, reason)
            runtime = self.live.get(agent_id)
            if runtime is None or agent["status"] not in ACTIVE:
                return agent
            # Record WHY before cancelling so the panel/details can explain the
            # interruption ("错误" is only for real errors, not for this).
            agent["interrupted_by"] = reason
            agent["status"] = "interrupting"
            self.save(agent)
            timed_out = False
            try:
                # A stuck child (e.g. mid tool-call) would otherwise leave the
                # task in "interrupting" forever. Bound the cancel and force-
                # terminate the process when it does not answer.
                await asyncio.wait_for(
                    runtime.request("session/cancel", {"sessionId": agent_id}),
                    timeout=30.0,
                )
            except (asyncio.TimeoutError, RuntimeError):
                timed_out = True
                try:
                    await runtime.close()
                finally:
                    self.live.pop(agent_id, None)
            current = self.get(agent_id)
            current["status"] = "interrupted"
            current["interrupted_by"] = reason
            if timed_out or agent_id not in self.live:
                current["error"] = (
                    "中断超时：子进程卡在工具调用（未响应取消），已被强制终止。"
                    "该任务不会自动恢复，可按需重新发起。"
                )
            self.event(agent_id, "interrupted", {"reason": reason, "timeout": timed_out})
            self.save(current)
            return current

    async def close(self, agent_id: str) -> dict:
        async with self.lock(agent_id):
            agent = self.get(agent_id)
            if agent.get("external"):
                return await self.external.close(agent_id)
            runtime = self.live.get(agent_id)
            if runtime is not None:
                await runtime.close()
            self.live.pop(agent_id, None)
            agent["status"] = "closed"
            self.save(agent)
            if _auto_migrate_enabled() and agent.get("cwd"):
                # Closed agents are not resumed, so making the log readable in
                # DSH Web (v4) no longer costs anything.
                asyncio.create_task(self._auto_migrate(agent_id))
            return agent

    async def release(self, agent_id: str) -> dict:
        async with self.lock(agent_id):
            agent = self.get(agent_id)
            if agent.get("external"):
                raise ValueError("External Web sessions own their runtimes; use close to detach.")
            if agent["status"] in ACTIVE:
                return {"agent_id": agent_id, "released": False, "status": agent["status"]}
            runtime = self.live.get(agent_id)
            if runtime is not None:
                try:
                    await runtime.request("session/checkpoint", {"sessionId": agent_id}, timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    try:
                        await runtime.close()
                    finally:
                        self.live.pop(agent_id, None)
                self.event(agent_id, "runtime/released", {})
            return {
                "agent_id": agent_id,
                "released": runtime is not None,
                "status": self.get(agent_id)["status"],
            }

    async def rename(self, agent_id: str, name: str) -> dict:
        async with self.lock(agent_id):
            title = (name or "").strip()
            if not title:
                raise ValueError("name must not be blank")
            agent = self.get(agent_id)
            if agent.get("external"):
                return await self.external.rename(agent_id, title)
            agent["name"] = title
            self.save(agent)
            if not agent.get("persisted"):
                return agent
            runtime = self.live.get(agent_id)
            if runtime is not None:
                await runtime.request("session/rename", {"sessionId": agent_id, "title": title})
            elif Path(agent["cwd"]).is_dir():
                runtime = await self.runtime(agent)
                if self.get(agent_id)["status"] not in ACTIVE:
                    await runtime.close()
                    self.live.pop(agent_id, None)
            else:
                raise ValueError(
                    "Stored name updated, but the session cwd no longer exists so DSH was not renamed"
                )
            return self.get(agent_id)

    def _soft_error(self, agent_id: str, where: str, error) -> None:
        """Record a non-fatal failure so it is visible instead of swallowed.

        Decorative failures (titles, webhooks) may still be ignored by callers,
        but the event stream and logs keep a trace, which is how the title bug
        stayed hidden for so long.
        """
        message = str(error)[:300]
        try:
            if agent_id:
                self.event(agent_id, "soft-error", {"where": where, "message": message})
        except Exception:  # noqa: BLE001
            pass
        try:
            print(f"[soft-error] {where}: {message}", flush=True)
        except Exception:  # noqa: BLE001
            pass

    # -- completion notifications (MCP notification / webhook) -------------
    def register_notifier(self, fn) -> None:
        from .notify import register_notifier as _register

        _register(self._notifiers, fn)

    def _notify_settled(self, agent_id: str) -> None:
        """Fan out a settled task to every sink; failures never break tasks."""
        agent = self.get(agent_id)
        if not agent or agent.get("status") not in ("completed", "error", "interrupted"):
            return
        payload = dict(agent)

        async def fanout() -> None:
            from . import notify

            for fn in list(self._notifiers):
                try:
                    result = fn(payload)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001
                    pass
            try:
                await notify.post_webhook(self.config.database.parent, payload)
            except Exception:  # noqa: BLE001
                pass
            try:
                from . import codex_callback

                await codex_callback.notify_codex(self.config.database.parent, payload)
            except Exception:  # noqa: BLE001
                pass

        asyncio.create_task(fanout())

    # -- release serialization (write-handle race) -------------------------
    def _schedule_release(self, agent_id: str) -> None:
        task = self._releasing.get(agent_id)
        if task is not None and not task.done():
            return
        self._releasing[agent_id] = asyncio.create_task(self._release_quietly(agent_id))

    def _ensure_session_local(self, agent_id: str) -> None:
        """Bring a published session log back into the child's DSH_HOME.

        Published sessions live in the real DSH sessions root; the 0.2.1 child
        only reads its own home, so resuming such a task needs the log copied
        back first (otherwise DSH reports "session not found").
        """
        from . import runtime021

        local_root = runtime021.HOME / "sessions"
        if list(local_root.glob("**/{}/session.*.jsonl.zstd".format(agent_id))):
            return
        from .platform import sessions_root as _sessions_root

        real_root = _sessions_root()
        for src in real_root.glob("**/{}/session.*.jsonl.zstd".format(agent_id)):
            workspace = src.parent.parent.name
            dst_dir = local_root / workspace / agent_id
            dst_dir.mkdir(parents=True, exist_ok=True)
            try:
                import shutil

                shutil.copy2(src, dst_dir / src.name)
            except OSError:
                pass

    async def _await_release(self, agent_id: str) -> None:
        """Wait for a pending release so the old write handle is gone first."""
        task = self._releasing.pop(agent_id, None)
        if task is not None and not task.done():
            try:
                await task
            except Exception:  # noqa: BLE001
                pass

    # -- automatic recovery (service restart must not lose work) -----------
    def _auto_resume_enabled(self) -> bool:
        return os.environ.get("DSH_SUBAGENT_AUTO_RESUME", "1").strip() != "0"

    async def _recover_observers_once(self, limit: int = 2) -> dict:
        """Restore observation only; never prompt, cancel, take over or replay."""
        recovered, failed = [], []
        attempts = getattr(self, "_recovery_attempts", {})
        self._recovery_attempts = attempts
        now = asyncio.get_running_loop().time()
        candidates = []
        for agent in self.list():
            if not agent.get("external") or agent.get("status") != "disconnected":
                continue
            owner = agent.get("owner_pid")
            if owner and owner != os.getpid() and _pid_alive(owner):
                continue
            due, failures = attempts.get(agent["id"], (0, 0))
            if due <= now:
                candidates.append((agent["id"], failures))
        # Old failures cannot starve new tasks: retry eligibility has backoff.
        for sid, failures in candidates[:max(1, limit)]:
            try:
                await asyncio.wait_for(self.external.status(sid), timeout=12)
                current = self.get(sid)
                if current.get("status") == "disconnected":
                    raise RuntimeError("Native state remains unverified")
                current["owner_pid"] = os.getpid()
                current.pop("connection_error", None)
                self.save(current, bump=False)
                attempts.pop(sid, None)
                recovered.append(sid)
            except asyncio.CancelledError:
                raise
            except Exception:
                current = self.get(sid)
                if current.get("status") != "closed":
                    current["status"] = "disconnected"
                    current["connection_error"] = "Automatic observation reconnect failed; retrying without replay."
                    self.save(current, bump=False)
                # Preserve unknown state; neither raw credentials nor exceptions
                # are recorded here. A failed observation is not task failure.
                delay = min(5 * (2 ** min(failures, 6)), 300)
                attempts[sid] = (asyncio.get_running_loop().time() + delay, failures + 1)
                failed.append(sid)
        return {"reconnected": recovered, "unverified": failed}

    async def auto_resume(self, limit: int = 2) -> dict:
        if self._auto_resume_enabled() and (self._recovery_task is None or self._recovery_task.done()):
            async def observe():
                while True:
                    try:
                        await self._recover_observers_once(limit)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass  # Retry a transient store/runtime failure on the next tick.
                    await asyncio.sleep(2)
            self._recovery_task = asyncio.create_task(observe())
        return {"enabled": False, "resumed": [], "observation_recovery": self._auto_resume_enabled(),
                "reason": "Only disconnected observations reconnect; Web tasks are never replayed"}

    # -- stall watchdog (P0-2) ----------------------------------------------
    def _tool_timeout_seconds(self) -> float:
        value = os.environ.get("DSH_SUBAGENT_TOOL_TIMEOUT", "900").strip()
        try:
            return max(60.0, float(value))
        except ValueError:
            return 900.0

    def _stall_threshold(self) -> float:
        value = os.environ.get("DSH_SUBAGENT_STALL", "180").strip()
        try:
            # Allow short thresholds (e.g. 10s) for testing/tuning; the old 30s
            # floor silently ignored smaller configured values.
            return max(10.0, float(value))
        except ValueError:
            return 180.0

    def _stall_action(self) -> str:
        # Env wins; then the persisted panel choice (state/stall.json) so the
        # setting survives manual service starts (start-panel.bat, http start).
        value = os.environ.get("DSH_SUBAGENT_STALL_ACTION", "").strip().lower()
        if value in ("interrupt", "warn"):
            return value
        try:
            import json as _json

            from .platform import locations

            path = locations().state / "stall.json"
            if path.is_file():
                choice = str(_json.loads(path.read_text(encoding="utf-8")).get("action") or "").strip().lower()
                if choice in ("interrupt", "warn"):
                    return choice
        except Exception:  # noqa: BLE001
            pass
        return "warn"

    def _watchdog_tick(self) -> None:
        """Mark running tasks that emitted nothing for too long as stalled.

        Only the transport that OWNS a live child can safely interrupt it; the
        watchdog here simply flags and (optionally) enqueues an interrupt via
        the same code path an API call would use.
        """
        # Module-level name is `from datetime import datetime` (the class), so
        # import the MODULE here. It must come before any use of datetime.* —
        # an inner import further down made the earlier tool/call branch raise
        # UnboundLocalError and silently kill the whole tick.
        import datetime

        if getattr(self, "_watchdog_stopped", False):
            return
        threshold = self._stall_threshold()
        now = _now()
        for agent in self.list():
            if agent.get("status") not in ("running", "starting"):
                continue
            # Progress = the newest EVENT. updated_at is refreshed by ordinary
            # saves, so it must not be the staleness base or a stuck task looks
            # active forever.
            # Newest REAL progress event. Our own "stalled" markers must NOT become the
            # next anchor: doing so chain-fired a fresh stall event every threshold
            # (the 停滞 row every 3 minutes the user reported).
            last_event = ""
            last_type = ""
            try:
                row = self.db.execute(
                    "SELECT time, type FROM events WHERE agent=? AND type<>'stalled' "
                    "ORDER BY seq DESC LIMIT 1",
                    (agent["id"],),
                ).fetchone()
                if row:
                    last_event, last_type = row[0], row[1]
            except Exception:  # noqa: BLE001
                last_event, last_type = "", ""
            if last_type == "tool/call":
                tool_seconds = self._tool_timeout_seconds()
                try:
                    since = (datetime.datetime.fromisoformat(now.replace("Z", "+00:00"))
                             - datetime.datetime.fromisoformat(
                                 (last_event or agent.get("created_at") or now).replace("Z", "+00:00"))
                             ).total_seconds()
                except Exception:  # noqa: BLE001 - a bad timestamp must not break the tick
                    since = 0
                if since < tool_seconds:
                    # A running tool IS progress: never flag, and make sure a
                    # stale "stalled" flag is cleared too (a plain `continue`
                    # here left the flag stuck forever).
                    if agent.get("stalled"):
                        agent["stalled"] = False
                        agent.pop("stalled_since", None)
                        self.save(agent)
                    continue  # normal tool execution; not stalled
            anchor = last_event or agent.get("updated_at") or agent.get("created_at") or ""
            updated = anchor
            try:
                stale_seconds = (datetime.datetime.fromisoformat(now.replace("Z", "+00:00"))
                                 - datetime.datetime.fromisoformat(updated.replace("Z", "+00:00"))).total_seconds()
            except Exception:  # noqa: BLE001
                stale_seconds = 0
            if stale_seconds >= threshold:
                if not agent.get("stalled"):
                    agent["stalled"] = True
                    agent["stalled_since"] = updated
                    # bump=False keeps updated_at at the last REAL progress, so
                    # the flag is not self-reset on the next tick and the
                    # "activity time updated" illusion disappears.
                    self.save(agent, bump=False)
                    # Emit ONE stall event per progress gap even if another
                    # process's save cleared our flag in the meantime.
                    prev = self.db.execute(
                        "SELECT data FROM events WHERE agent=? AND type='stalled' "
                        "ORDER BY seq DESC LIMIT 1",
                        (agent["id"],),
                    ).fetchone()
                    same_gap = False
                    if prev:
                        try:
                            same_gap = json.loads(prev[0]).get("since") == updated
                        except (ValueError, TypeError):
                            same_gap = False
                    if not same_gap:
                        self.event(agent["id"], "stalled", {"since": updated, "seconds": int(stale_seconds)})
                    if self._stall_action() == "interrupt":
                        asyncio.create_task(self._interrupt_from_watchdog(
                            agent["id"], int(stale_seconds), last_type))
            else:
                if agent.get("stalled") and agent["status"] == "running":
                    agent["stalled"] = False
                    agent.pop("stalled_since", None)
                    self.save(agent)

    async def _interrupt_from_watchdog(self, agent_id: str, stale_seconds: int = 0,
                                       last_type: str = "") -> None:
        try:
            reason = "系统自动中断（长时间无响应）"
            await self.interrupt(agent_id, reason=reason)
        except Exception as error:  # noqa: BLE001
            self._soft_error(agent_id, "watchdog-interrupt", error)

    def _start_watchdog(self) -> None:
        if self._watchdog_task is not None:
            return
        # Never create the coroutine without a running loop (the panel constructs
        # managers on a Flask thread and would leak an un-awaited coroutine).
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._watchdog_stopped = False

        async def loop() -> None:
            while not self._watchdog_stopped:
                try:
                    self._watchdog_tick()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(20.0)

        self._watchdog_task = asyncio.create_task(loop())

    def _stop_watchdog(self) -> None:
        self._watchdog_stopped = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    # -- completion callbacks (dsh_watch / dsh_unwatch) ---------------------
    def _callbacks_dir(self) -> Path:
        return self.config.database.parent / "callbacks"

    async def _callback_settlement(self, agent_id: str) -> dict:
        """Watch true terminal states; an observer disconnect is not completion."""
        terminal = {"completed", "error", "interrupted", "context_exhausted", "closed"}
        initial = self.get(agent_id)
        if initial.get("status") in terminal:
            return self._wait_result(initial, "settled")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def changed(agent):
            if agent.get("status") in terminal and not future.done():
                future.set_result(agent)
        self._waiters.setdefault(agent_id, []).append(changed)
        try:
            return self._wait_result(await future, "settled")
        finally:
            listeners = self._waiters.get(agent_id, [])
            if changed in listeners:
                listeners.remove(changed)

    def watch(self, agent_id: str) -> dict:
        """Register a completion callback for an agent.

        Without the Codex App Server plumbing of the Node bridge, completion is
        delivered as a result file written under ``state/callbacks`` plus a
        ``bridge/callback`` event. Clients that need busy-wakeup can poll the
        result file once; everything else should use ``dsh_wait``.
        """
        self.get(agent_id)
        output_dir = self._callbacks_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        result_path = output_dir / f"{agent_id}.json"

        existing = self.watchers.pop(agent_id, None)
        if existing is not None and not existing.done():
            existing.cancel()

        async def run() -> None:
            try:
                settled = await self._callback_settlement(agent_id)
            except asyncio.CancelledError:
                return
            except Exception as error:  # noqa: BLE001
                self.event(agent_id, "bridge/callback-error", {"message": str(error)})
                return
            from .diagnostics import error_message
            written_at = _now()
            task_completed_at = settled.get("settled_at")
            task_time_source = settled.get("settled_at_source")
            if not task_completed_at and settled.get("status") != "closed":
                ending = self.db.execute(
                    "SELECT time FROM events WHERE agent=? AND type IN ('turn/end','error') ORDER BY seq DESC LIMIT 1",
                    (agent_id,),
                ).fetchone()
                if ending:
                    task_completed_at, task_time_source = ending[0], "observed-event"
            payload = {
                "agent_id": agent_id,
                "name": settled.get("name"),
                "status": settled.get("status"),
                "wait_outcome": settled.get("wait_outcome"),
                "next_action": settled.get("next_action"),
                "answer": settled.get("answer") or "",
                "finish_reason": settled.get("finish_reason"),
                "error": error_message(settled),
                # completed_at retains the historical file-write semantics.
                "completed_at": written_at,
                "callback_written_at": written_at,
                "task_completed_at": task_completed_at,
                "task_completed_at_source": task_time_source,
                "delivery": {"file": "written", "mcp": "not_recorded", "webhook": "not_recorded", "codex": "not_recorded"},
            }
            tmp = result_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(result_path)
            self.event(agent_id, "bridge/callback", {"result_path": str(result_path), "channel": "file", "written_at": written_at})

        self.watchers[agent_id] = asyncio.create_task(run())
        return {
            "agent_id": agent_id,
            "watching": True,
            "delivery_mode": "file",
            "output_dir": str(output_dir),
            "result_path": str(result_path),
        }

    async def unwatch(self, agent_id: str, output_dir: str | None = None) -> dict:
        task = self.watchers.pop(agent_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return {
            "agent_id": agent_id,
            "unwatched": True,
            "output_dir": output_dir or str(self._callbacks_dir()),
        }

    # -- external DSH Web sessions ------------------------------------------
    async def attach(self, session_id: str, web_url: str) -> dict:
        async with self.lock(session_id):
            return await self.external.attach(session_id, web_url)

    async def send_external(self, agent_id: str, task: str, mode: str = "queue") -> dict:
        agent = self.get(agent_id)
        if not agent.get("external"):
            raise ValueError("dsh_send requires an attached external Web session; use dsh_followup for bridge agents")
        async with self.lock(agent_id):
            return await self.external.send(agent_id, task, mode)

    async def delete(self, agent_id: str) -> dict:
        """Permanently remove an agent record and its events.

        Live runtime/watcher/external observer are torn down first; a running
        task is interrupted implicitly. Files the task already changed are NOT
        rolled back.
        """
        async with self.lock(agent_id):
            agent = self.get(agent_id)
            watcher = self.watchers.pop(agent_id, None)
            if watcher is not None and not watcher.done():
                watcher.cancel()
            if agent.get("external"):
                try:
                    await self.external.close(agent_id)
                except Exception:  # noqa: BLE001
                    pass
            runtime = self.live.pop(agent_id, None)
            if runtime is not None:
                try:
                    await runtime.close()
                except Exception:  # noqa: BLE001
                    pass
            self.db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            self.db.execute("DELETE FROM events WHERE agent=?", (agent_id,))
            self.db.commit()
            return {
                "deleted": True,
                "agent_id": agent_id,
                "name": agent.get("name"),
                "status": agent.get("status"),
            }

    async def delete_many(self, agent_ids: list[str]) -> dict:
        results = []
        for agent_id in agent_ids:
            try:
                results.append(await self.delete(agent_id))
            except KeyError:
                results.append({"deleted": False, "agent_id": agent_id, "error": "unknown agent"})
        return {"deleted": sum(1 for r in results if r.get("deleted")), "results": results}

    async def stop_active(self, timeout: float = 60.0) -> dict:
        """Interrupt every active agent and wait until DSH reports idle.

        Used by graceful shutdown / "safe restart" so a panel restart does not
        silently orphan running turns.
        """
        active = [a for a in self.list()
                  if a.get("status") in ("starting", "running", "interrupting")]
        if not active:
            return {"interrupted": 0, "names": []}
        names = [a.get("name") or a["id"] for a in active]
        # Mark the cause BEFORE interrupting so a later auto_resume can pick
        # these rows up (otherwise a graceful drain looks like a user interrupt).
        for agent in active:
            agent["interrupted_by"] = "http stop (drain, pid {})".format(os.getpid())
            self.save(agent)
        for agent in active:
            try:
                await self.interrupt(agent["id"])
            except Exception as error:  # noqa: BLE001
                self._soft_error(agent["id"], "drain-interrupt", error)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = [a for a in self.list()
                         if a.get("status") in ("starting", "running", "interrupting")]
            if not remaining:
                break
            await asyncio.sleep(0.5)
        return {"interrupted": len(names), "names": names}

    # -- SQLite/WAL maintenance -------------------------------------------
    def _wal_path(self) -> Path:
        return Path(str(self.config.database) + "-wal")

    def _db_checkpoint(self, truncate: bool = False) -> None:
        mode = "TRUNCATE" if truncate else "PASSIVE"
        try:
            self.db.execute("PRAGMA wal_checkpoint({})".format(mode)).fetchone()
        except Exception:  # noqa: BLE001 - 维护失败不可影响业务
            pass

    def _start_db_maintenance(self) -> None:
        """启动时回收 WAL；之后每 5 分钟维护一次（读快照不会再被钉住）。"""

        def loop() -> None:
            while not getattr(self, "_db_closed", False):
                try:
                    wal = self._wal_path()
                    size = wal.stat().st_size if wal.is_file() else 0
                    # 超过 256MB 才做 TRUNCATE（会短暂阻塞写入）；否则 PASSIVE
                    self._db_checkpoint(truncate=size > 256 * 1024 * 1024)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(300)

        # 启动时立即尝试回收（旧进程的长快照在重启后已消失）
        self._db_checkpoint(truncate=True)
        threading.Thread(target=loop, name="dsh-db-maintenance", daemon=True).start()

    async def shutdown(self) -> None:
        self._stop_watchdog()
        if self._shutdown_task is not None:
            await self._shutdown_task
            return

        async def stop() -> None:
            if self._recovery_task is not None:
                self._recovery_task.cancel()
                await asyncio.gather(self._recovery_task, return_exceptions=True)
                self._recovery_task = None
            for task in list(self.watchers.values()):
                if not task.done():
                    task.cancel()
            self.watchers.clear()
            await self.external.shutdown()
            for agent_id in list(self.live):
                agent = self.get(agent_id)
                if agent["status"] in ACTIVE:
                    agent["status"] = "interrupted"
                    agent["error"] = "Service stopped; follow up explicitly to resume."
                    # Let auto_resume bring these back on the next start.
                    agent["interrupted_by"] = "service shutdown (pid {})".format(os.getpid())
                    self.save(agent)
            await asyncio.gather(
                *(runtime.close() for runtime in list(self.live.values())),
                return_exceptions=True,
            )
            self.live.clear()
            self._db_closed = True
            self.db.close()

        self._shutdown_task = asyncio.create_task(stop())
        await self._shutdown_task
