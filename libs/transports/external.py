"""External DSH Web sessions (``dsh_attach`` / ``dsh_send``).

Speaks the same protocol as the DSH Web browser client:

* ``GET <launch url>`` -> 303 with a session cookie (the URL carries ``?token=``)
* ``POST /api/<method>`` with ``{"type":"client-request", ...}`` -> ``{result}``
* ``ws /api/remote.mux`` -> multiplexed streams opened with
  ``{"type":"open","streamId",endpoint,payload:{args}}`` and delivered as
  ``{"type":"item","streamId","value"}`` frames

Observers delegate by default; explicit per-session interaction mode can answer questions, never grant permissions. Sending is
an explicit user action through ``dsh_send``.
"""

from __future__ import annotations

import asyncio
import os
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..core.policy import KNOWN_PERMISSION_PRESETS
from ..core.platform import private_directory
from ..core.secrets import read_secret_json, write_secret_json
from ..core.net import http_options

try:  # optional at import time so the bridge still runs without WS support
    import websockets
except Exception:  # noqa: BLE001
    websockets = None


class WebAuthError(RuntimeError):
    pass


class WebRemoteRejected(RuntimeError):
    """The server answered and rejected the request (safe to retry/adapt)."""


class WebCredentials:
    def __init__(self, origin: str, cookie: str) -> None:
        self.origin = origin
        self.cookie = cookie

    def to_dict(self) -> dict:
        return {"origin": self.origin, "cookie": self.cookie}

    @classmethod
    def from_dict(cls, data: dict) -> "WebCredentials":
        return cls(data["origin"], data["cookie"])


async def authenticate_web(web_url: str, timeout: float = 30.0) -> WebCredentials:
    """Exchange the launch URL (with ``?token=``) for a session cookie."""
    parsed = urlparse(web_url)
    if parsed.scheme not in ("http", "https"):
        raise WebAuthError("Use an HTTP(S) DSH Web URL")
    if parsed.username or parsed.password:
        raise WebAuthError("Use a DSH Web URL without embedded credentials")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise WebAuthError("Remote DSH Web connections require HTTPS")

    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, **http_options(web_url)) as client:
        response = await client.get(web_url)
        cookies = response.headers.get_list("set-cookie")
        cookie = "; ".join(value.split(";")[0] for value in cookies if value)
        status = response.status_code

    if status != 303 or not cookie:
        raise WebAuthError(
            f"DSH Web authentication failed (HTTP {status}); supply its current launch URL"
        )
    return WebCredentials(f"{parsed.scheme}://{parsed.netloc}", cookie)


class WebClient:
    """Authenticated RPC + multiplexed streams, mirroring web-client.mjs."""

    def __init__(self, credentials: WebCredentials) -> None:
        self.credentials = credentials
        self.streams: dict[str, dict] = {}
        self.socket = None
        self.closed = False
        self.error: Exception | None = None
        self._reader_task: asyncio.Task | None = None

    async def rpc(self, method: str, args: dict) -> dict:
        url = f"{self.credentials.origin}/api/{method}"
        headers = {
            "cookie": self.credentials.cookie,
            "origin": self.credentials.origin,
            "content-type": "application/json",
        }
        body = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": {"args": args},
        }
        async with httpx.AsyncClient(timeout=30.0, **http_options(self.credentials.origin)) as client:
            response = await client.post(url, headers=headers, json=body)
            if response.status_code >= 400:
                raise RuntimeError(f"DSH Web {method}: HTTP {response.status_code}")
            data = response.json()
        result = (data or {}).get("result") or {}
        if not result.get("ok"):
            error = result.get("error") or {}
            raise WebRemoteRejected(
                f"DSH Web {method}: {error.get('code', 'invalid response')}: {error.get('message', '')}"
            )
        return result.get("value")

    async def connect(self, on_disconnect) -> None:
        if websockets is None:
            raise RuntimeError("the 'websockets' package is required for dsh_attach")
        ws_scheme = "wss" if self.credentials.origin.startswith("https") else "ws"
        url = self.credentials.origin.replace("http", ws_scheme, 1) + "/api/remote.mux"
        self.socket = await websockets.connect(
            url,
            extra_headers={
                "cookie": self.credentials.cookie,
                "origin": self.credentials.origin,
            },
            open_timeout=30,
            max_size=None,
        )
        self._reader_task = asyncio.create_task(self._read_loop(on_disconnect))

    async def _read_loop(self, on_disconnect) -> None:
        try:
            async for raw in self.socket:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                stream = self.streams.get(frame.get("streamId"))
                if not stream:
                    continue
                if frame.get("type") == "item":
                    value = frame.get("value")
                    result = stream["on_value"](value)
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                    if not stream.get("resolved"):
                        stream["resolved"] = True
                        stream.get("resolve") and stream["resolve"](value)
                else:
                    error = frame.get("error") or {}
                    stream.get("reject") and stream["reject"](
                        RuntimeError(
                            f"DSH Web stream: {error.get('code')}: {error.get('message')}"
                        )
                    )
            if not self.closed:
                self._fail(RuntimeError("DSH Web observation connection closed"), on_disconnect)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self._fail(error, on_disconnect)

    def _fail(self, error: Exception, on_disconnect=None) -> None:
        self.error = error
        for stream in list(self.streams.values()):
            stream.get("reject") and stream["reject"](error)
        self.streams.clear()
        if on_disconnect is not None:
            on_disconnect(error)
        if self.socket is not None:
            try:
                asyncio.create_task(self.socket.close())
            except Exception:  # noqa: BLE001
                pass

    async def subscribe(self, endpoint: str, args: dict, on_value, stream_id=None) -> dict:
        stream_id = stream_id or str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def resolve(value):
            if not future.done():
                future.set_result(value)

        def reject(error):
            if not future.done():
                future.set_exception(error)

        self.streams[stream_id] = {"on_value": on_value, "resolve": resolve, "reject": reject, "resolved": False}
        await self.socket.send(json.dumps({
            "type": "open",
            "streamId": stream_id,
            "endpoint": endpoint,
            "payload": {"args": args},
        }))
        try:
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            self.streams.pop(stream_id, None)
            raise RuntimeError(f"DSH Web {endpoint} did not open") from None

    async def cancel_stream(self, stream_id: str) -> None:
        stream = self.streams.pop(stream_id, None)
        if stream and stream.get("reject"):
            stream["reject"](RuntimeError("Stream cancelled"))
        if self.socket is not None and not self.closed:
            try:
                await self.socket.send(json.dumps({"type": "cancel", "streamId": stream_id}))
            except Exception:
                pass

    async def close(self) -> None:
        self.closed = True
        self._fail(RuntimeError("DSH Web observer detached"))
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self.socket is not None:
            try:
                await self.socket.close()
            except Exception:  # noqa: BLE001
                pass


# -- external session registry -------------------------------------------------

async def _answer_next(client: WebClient, state: dict, frame: dict) -> None:
    """Decline a waterfall prompt on the observer's behalf (never approve)."""
    try:
        await client.rpc("$events/result", {
            "clientId": state.get("clientId"),
            "eventId": frame.get("eventId"),
            "outcome": {"kind": "next"},
        })
    except Exception:  # noqa: BLE001
        pass


class ExternalSessions:
    """Tracks attached DSH Web sessions and proxies sends/status."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self.live: dict[str, dict] = {}
        self.opening: dict[str, asyncio.Task] = {}
        from .interactions import Interactions
        self.interactions = Interactions(self)

    def _credential_path(self, auth_id: str) -> Path:
        return Path(self.manager.config.database).parent / "web-auth" / f"{auth_id}.json"

    async def _credentials(self, web_url: str) -> WebCredentials:
        """Authenticate via the launch URL; fall back to a stored cookie.

        DSH Web prints a fresh ``?token=`` on every launch, so a URL saved by
        the user goes stale whenever the Web restarts. The auth cookie issued
        by that URL lasts much longer, so when the token login fails (401) we
        retry with a stored cookie of the SAME origin and validate it with a
        cheap ``session/list`` call before using it.
        """
        try:
            return await authenticate_web(web_url)
        except WebAuthError:
            parsed = urlparse(web_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            state = Path(self.manager.config.database).parent / "web-auth"
            for path in state.glob("*.json"):
                try:
                    data = read_secret_json(path)
                except (OSError, ValueError):
                    continue
                if not data or data.get("origin") != origin:
                    continue
                cookie = str(data.get("cookie") or "")
                if not cookie:
                    continue
                candidate = WebCredentials(origin, cookie)
                try:
                    listing = await WebClient(candidate).rpc("session/list", {"_request": {}})
                except Exception:  # noqa: BLE001 - expired/invalid cookie: try the next
                    continue
                if isinstance(listing, dict):
                    return candidate
            raise

    async def create(self, web_url: str, cwd: str | None = None,
                     name: str | None = None, agent_preset: str | None = None,
                     permission: str | None = None, model: str | None = None,
                     provider: str | None = None, effort: str | None = None,
                     workspace_id: str | None = None) -> dict:
        """Create a session in a DSH Web server via session/create (Web token),
        then adopt it as an external agent. The session is natively visible in
        DSH Web — no duplicate subtree, no publish step.
        """
        if workspace_id:
            baseline = await self.web_workspaces(web_url)
            row = next((w for w in baseline.get("workspaces", []) if w.get("workspaceId") == workspace_id), None)
            if not row or not row.get("path"):
                raise ValueError("Unknown workspace_id; session creation refused")
            from ..core.policy import workspace_path
            selected = str(workspace_path(row["path"]))
            if cwd and workspace_path(cwd) != workspace_path(selected):
                raise ValueError("cwd and workspace_id refer to different workspaces")
            cwd = selected
        from ..core.preferences import resolve
        settings = resolve({"cwd": cwd, "preset": agent_preset, "permission": permission,
                            "model": model, "provider": provider, "effort": effort},
                           Path(self.manager.config.database).parent)
        cwd, agent_preset, permission = settings["cwd"], settings["preset"], settings["permission"]
        model, provider, effort = (settings.get(key) for key in ("model", "provider", "effort"))
        if permission not in KNOWN_PERMISSION_PRESETS:
            raise ValueError("Unsupported permission; session creation refused")
        credentials = await self._credentials(web_url)
        client = WebClient(credentials)
        catalog = await client.rpc("session/modelCatalog", {}) or {}
        effective = catalog.get("default") or {}
        for field, requested in (("model", model), ("provider", provider), ("reasoningEffort", effort)):
            if requested and effective.get(field) != requested:
                raise ValueError("Requested {} does not match Web default; no session created or global default changed".format(field))
        request: dict = {"agentPreset": agent_preset or "standard"}
        if workspace_id:
            request["workspaceId"] = workspace_id
        elif cwd:
            request["cwd"] = cwd
        try:
            result = await client.rpc("session/create", {"request": request})
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("Web API session/create failed: {}".format(error)) from error
        # result contains the created session identity
        session_id = (result or {}).get("sessionId") or (result or {}).get("id")             or (result or {}).get("session", {}).get("id")
        if not session_id:
            listing = await client.rpc("session/list", {"_request": {}})
            items = (listing or {}).get("items") or []
            if not items:
                raise RuntimeError("session/create answered but no session is listed")
            session_id = items[0].get("sessionId")
        if not session_id:
            raise RuntimeError("session/create did not yield a session id")
        # rename to the friendly title when asked
        if name:
            try:
                await client.rpc("session/rename", {"request": {"sessionId": session_id, "title": name}})
            except Exception:  # noqa: BLE001
                pass

        existing = next((a for a in self.manager.list() if a["id"] == session_id), None)
        auth_id = (existing or {}).get("web_auth_id") or str(uuid.uuid4())
        path = self._credential_path(auth_id)
        private_directory(path.parent)
        write_secret_json(path, credentials.to_dict())
        try:
            path.chmod(0o600)
        except OSError:
            pass
        agent = {
            **(existing or {}),
            "id": session_id,
            "session_id": session_id,
            "external": True,
            "owner_pid": os.getpid(),
            "web_origin": credentials.origin,
            "web_auth_id": auth_id,
            "cwd": cwd or (existing or {}).get("cwd") or "",
            "name": name or (existing or {}).get("name") or "Web 会话",
            "status": "starting",
            "answer": (existing or {}).get("answer", ""),
            "partial_text": "",
            "created_at": (existing or {}).get("created_at")
            or __import__("libs.core.manager", fromlist=["_now"])._now(),
        }
        self.manager.save(agent)
        try:
            await self._ensure(session_id)
        except Exception as error:  # noqa: BLE001
            current = self.manager.get(session_id)
            current["status"] = "error"
            current["error"] = str(error)
            self.manager.save(current)
            raise
        agent = await self.status(session_id)
        for field, requested in (("model", model), ("provider", provider), ("effort", effort)):
            if requested and agent.get(field) != requested:
                raise ValueError("DSH Web effective {} does not match request; no task sent or global default changed".format(field))
        if agent.get("permission") != permission:
            # Official permission plugin exposes a session-local command, not
            # a permission field on session/create. Never expose arbitrary
            # command execution through MCP.
            state = await self._ensure(session_id)
            await state["client"].rpc("commands/execute", {
                "agentId": session_id, "line": "/permission " + permission,
                "submittedAttachments": [],
            })
            agent = await self.status(session_id)
            if agent.get("permission") != permission:
                raise ValueError("DSH Web effective permission does not match requested permission; no task sent")
        agent["requested_permission"] = permission
        for field, requested in (("model", model), ("provider", provider), ("effort", effort)):
            if requested:
                agent["requested_" + field] = requested
        self.manager.save(agent)
        return agent

    async def attach(self, session_id: str, web_url: str) -> dict:
        credentials = await self._credentials(web_url)
        client = WebClient(credentials)
        listing = await client.rpc("session/list", {"_request": {}})
        row = next(
            (item for item in (listing or {}).get("items", []) if item.get("sessionId") == session_id),
            None,
        )
        if row is None:
            raise ValueError(f"Session not found in this DSH Web server: {session_id}")
        if row.get("origin") == "subagent":
            raise ValueError("This is a DSH-owned child; use its parent session for delivery")

        existing = next((a for a in self.manager.list() if a["id"] == session_id), None)
        if existing and (not existing.get("external") or existing.get("web_origin") != credentials.origin):
            raise ValueError("This session ID already belongs to another bridge runtime or Web server")

        auth_id = (existing or {}).get("web_auth_id") or str(uuid.uuid4())
        path = self._credential_path(auth_id)
        private_directory(path.parent)
        write_secret_json(path, credentials.to_dict())
        try:
            path.chmod(0o600)
        except OSError:
            pass

        await self.interactions.detach(session_id, connected=True)
        live = self.live.pop(session_id, None)
        if live:
            await live["client"].close()

        agent = {
            **(existing or {}),
            "id": session_id,
            "session_id": session_id,
            "external": True,
            "owner_pid": os.getpid(),
            "web_origin": credentials.origin,
            "web_auth_id": auth_id,
            "cwd": row.get("cwd") or "",
            "name": ((row.get("projections") or {}).get("values") or {}).get("title") or "",
            "status": "starting",
            "answer": (existing or {}).get("answer", ""),
            "partial_text": "",
            "created_at": (existing or {}).get("created_at")
            or __import__("libs.core.manager", fromlist=["_now"])._now(),
        }
        self.manager.save(agent)
        try:
            await self._ensure(session_id)
        except Exception as error:  # noqa: BLE001 - record the failure, then surface it
            # A failed attach must not leave the agent stuck in "starting".
            current = self.manager.get(session_id)
            current["status"] = "error"
            current["error"] = str(error)
            self.manager.save(current)
            raise
        return self.manager.get(session_id)

    async def _ensure(self, session_id: str) -> dict:
        # _open publishes its state before authentication/subscription finish.
        # Concurrent readers must join that open, not use a half-ready client.
        if session_id in self.opening:
            return await self.opening[session_id]
        if session_id in self.live:
            return self.live[session_id]
        task = asyncio.create_task(self._open(session_id))
        self.opening[session_id] = task
        try:
            return await task
        except BaseException:
            # A failed/timed-out open must not cache a half-open observer.
            state = self.live.get(session_id)
            if state and not state.get("ready"):
                self.live.pop(session_id, None)
                await state["client"].close()
            raise
        finally:
            self.opening.pop(session_id, None)

    async def _open(self, session_id: str) -> dict:
        agent = self.manager.get(session_id)
        if agent.get("status") == "closed":
            raise ValueError("Agent is closed; attach again to reconnect to the external session")
        credentials = WebCredentials.from_dict(
            read_secret_json(self._credential_path(agent["web_auth_id"]))
        )
        client = WebClient(credentials)
        state = {"client": client, "running": False, "queue": [], "ready": False}
        self.live[session_id] = state

        def on_event(frame: dict) -> None:
            if frame.get("type") == "ready":
                state["clientId"] = frame.get("clientId")
                return
            if frame.get("type") == "cancel":
                asyncio.create_task(self.interactions.cancel(frame.get("eventId")))
                return
            if frame.get("type") == "waterfall":
                asyncio.create_task(self.interactions.handle(session_id, state, frame))
            elif frame.get("type") == "emit" and (frame.get("args") or [None])[0] == session_id:
                if frame.get("event") == "api-session/status":
                    state["running"] = bool(frame["args"][1])
                    self._settle(session_id, state)
                elif frame.get("event") == "api-session/error":
                    self._session_error(session_id, state, str(frame["args"][1]))

        await client.connect(lambda error: self._fail(session_id, error))
        await client.subscribe("$events", {}, on_event)

        def on_history(frame: dict) -> None:
            if frame.get("type") != "snapshot":
                event = frame.get("event") or {}
                self._apply_event(session_id, event)

        snapshot = await client.subscribe(
            "session/follow",
            {"request": {"address": {"kind": "session", "sessionId": session_id},
                         "maxMessages": 2, "assistantStream": True}},
            on_history,
        )
        self._apply_snapshot(session_id, snapshot)
        current = self.manager.get(session_id)
        current.pop("connection_error", None)
        self.manager.save(current)

        listing = await client.rpc("session/list", {"_request": {}})
        row = next((i for i in (listing or {}).get("items", []) if i.get("sessionId") == session_id), None)
        if row is None:
            raise ValueError("External session no longer exists")
        state["running"] = bool(row.get("running"))
        self._apply_listing_row(session_id, row)
        if not row.get("running") and not self.manager.get(session_id).get("finish_reason"):
            current = self.manager.get(session_id)
            current["status"] = "idle"
            self.manager.save(current)
        self._settle(session_id, state)
        state["ready"] = True
        return state

    def _apply_listing_row(self, session_id: str, row: dict) -> None:
        """Fill model/provider/preset/permission from a session/list row.

        The session/follow snapshot does not always carry modelSelection for a
        freshly created session, while session/list does — without this the
        bridge row kept provider/model = None and the panel showed "— / —".
        """
        if not row:
            return
        agent = self.manager.get(session_id)
        values = (row.get("projections") or {}).get("values") or {}
        selection = ((values.get("modelSelection") or {}).get("next")
                     or (values.get("modelSelection") or {}).get("lastUsed"))
        agent["blank"] = bool(row.get("blank"))
        agent["model_selection_explicit"] = bool(selection)
        if selection:
            agent["provider"] = selection.get("provider") or agent.get("provider")
            agent["model"] = selection.get("model") or agent.get("model")
            if selection.get("reasoningEffort"):
                agent["effort"] = selection.get("reasoningEffort")
        if values.get("agentPreset"):
            agent["preset"] = values.get("agentPreset")
        permissions = values.get("permissions") or {}
        if permissions.get("currentValue"):
            agent["permission"] = permissions.get("currentValue")
        if values.get("title"):
            agent["name"] = values.get("title")
        self.manager.save(agent)

    def _apply_snapshot(self, session_id: str, frame: dict) -> None:
        agent = self.manager.get(session_id)
        values = (frame.get("projections") or {}).get("values") or {}
        selection = (values.get("modelSelection") or {}).get("next") or (values.get("modelSelection") or {}).get("lastUsed")
        if frame.get("header"):
            agent["cwd"] = frame["header"].get("cwd") or agent.get("cwd")
        agent["preset"] = values.get("agentPreset")
        agent["permission"] = (values.get("permissions") or {}).get("currentValue")
        if selection:
            agent["model"] = selection.get("model")
            agent["provider"] = selection.get("provider")
            agent["effort"] = selection.get("reasoningEffort")
        agent["name"] = values.get("title") or agent.get("name")
        if agent.get("web_cursor") is None:
            agent["error"] = None
            agent["finish_reason"] = None
            agent["answer"] = ""
        for record in frame.get("records") or []:
            event = record.get("event") or {}
            self._apply_event(session_id, event, save=False)
        self.manager.save(agent)

    def _apply_event(self, session_id: str, event: dict, save: bool = True) -> None:
        agent = self.manager.get(session_id)
        seq = event.get("seq")
        cursor = agent.get("web_cursor")
        if seq is not None and cursor is not None and seq <= cursor:
            return
        kind = event.get("type")
        data = event.get("data") or {}
        if kind in ("approval/asked", "approval/decided"):
            state = self.live.get(session_id)
            if state is not None:
                state["awaiting_approval"] = kind == "approval/asked"
        if kind == "turn/start":
            agent["error"] = None
            agent.pop("settled_at", None)
            agent.pop("settled_at_source", None)
            agent["finish_reason"] = None
            agent["answer"] = ""
            agent["status"] = "running"
        elif kind == "assistant/message":
            blocks = (data.get("message") or {}).get("content") or []
            agent["answer"] = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        elif kind == "turn/end":
            agent["finish_reason"] = data.get("reason")
            from ..core.manager import _now
            from ..core.diagnostics import error_message
            agent["settled_at"] = event.get("time") or _now()
            agent["settled_at_source"] = "native-event" if event.get("time") else "observed-event"
            agent["error"] = error_message(agent)
        agent["last_event"] = kind
        if seq is not None:
            agent["web_cursor"] = seq
            if kind == "assistant/message":
                self.manager.event(session_id, kind, {
                    "seq": seq, "text": agent["answer"], "interrupted": data.get("interrupted"),
                })
            elif kind and (kind.startswith(("tool/", "turn/", "step/", "permission/", "sandbox/", "approval/"))):
                self.manager.event(session_id, kind, {**data, "session_seq": seq})
        if save:
            self.manager.save(agent)
            self._settle(session_id, self.live.get(session_id) or {})

    def _settle(self, session_id: str, state: dict) -> None:
        agent = self.manager.get(session_id)
        previous_status = agent.get("status")
        if agent.get("status") == "closed":
            return
        pending = agent.get("pending_interactions") or []
        if pending and state.get("running"):
            agent["status"] = "waiting_approval" if any(i.get("kind") == "approval" for i in pending) else "waiting_input"
        elif state.get("awaiting_approval") and state.get("running"):
            agent["status"] = "waiting_approval"
        elif state.get("queue"):
            agent["status"] = "running" if state.get("running") else "starting"
        elif state.get("running"):
            agent["status"] = "running"
        elif agent.get("finish_reason"):
            from ..core.manager import settled_status

            agent["status"] = settled_status(agent["finish_reason"])
        elif agent.get("status") not in ("starting", "running", "interrupting") and agent.get("status") != "error":
            agent["status"] = "idle"
        else:
            return
        if agent.get("status") in ("starting", "running", "waiting_approval", "waiting_input", "interrupting"):
            from ..core.manager import _pid_alive
            if not _pid_alive(agent.get("owner_pid")):
                agent["owner_pid"] = os.getpid()
        self.manager.save(agent)
        if agent.get("status") in ("completed", "error", "interrupted") and agent.get("status") != previous_status:
            self.manager._notify_settled(session_id)

    def _session_error(self, session_id: str, state: dict, message: str) -> None:
        agent = self.manager.get(session_id)
        agent["finish_reason"] = {"kind": "error", "error": {"message": message}}
        agent["error"] = message
        self.manager.save(agent)
        self.manager.event(session_id, "error", {"message": message})
        self._settle(session_id, state)

    def _fail(self, session_id: str, error: Exception) -> None:
        state = self.live.pop(session_id, None)
        if state:
            asyncio.create_task(self.interactions.detach(session_id))
            asyncio.create_task(state["client"].close())
        agent = self.manager.get(session_id)
        agent["status"] = "disconnected"
        agent["connection_error"] = str(error)
        self.manager.save(agent)

    async def status(self, session_id: str) -> dict:
        agent = self.manager.get(session_id)
        if agent.get("status") == "closed":
            return agent
        state = await self._ensure(session_id)
        listing = await state["client"].rpc("session/list", {"_request": {}})
        row = next((i for i in (listing or {}).get("items", []) if i.get("sessionId") == session_id), None)
        if row is None:
            raise ValueError("External session no longer exists")
        state["running"] = bool(row.get("running"))
        self._apply_listing_row(session_id, row)
        values = (row.get("projections") or {}).get("values") or {}
        selection = values.get("modelSelection") or {}
        if row.get("blank") and not selection.get("next") and not selection.get("lastUsed"):
            # Official selectionFor(): no pending selection or request header
            # means the deployment's currentSelection, not an unknown model.
            catalog = await state["client"].rpc("session/modelCatalog", {}) or {}
            default = catalog.get("default") or {}
            agent = self.manager.get(session_id)
            agent.update(provider=default.get("provider"), model=default.get("model"),
                         effort=default.get("reasoningEffort"), model_selection_source="web-default-for-blank")
            self.manager.save(agent)
        if not row.get("running") and row.get("blank"):
            agent = self.manager.get(session_id)
            if agent.get("status") not in ("closed", "error"):
                agent["status"] = "idle"
                self.manager.save(agent)
        self._settle(session_id, state)
        return self.manager.get(session_id)

    # auto = dsh-experimental-auto-review 注册的预设（等价 workspace-write + 自动审批）；
    # danger-full-access 也可能是用户特意选择的会话状态：都属于合法会话，不应拒绝发送。
    _SENDABLE_PERMISSIONS = ("read-only", "workspace-write", "danger-full-access", "auto")

    async def _ensure_sendable(self, agent: dict) -> None:
        effective = agent.get("permission")
        if effective not in self._SENDABLE_PERMISSIONS:
            raise ValueError("Unsafe or unknown DSH Web permission; message refused")
        if agent.get("requested_permission") and effective != agent["requested_permission"]:
            raise ValueError("DSH Web permission changed; message refused")
        for field in ("model", "provider", "effort"):
            if agent.get("requested_" + field) and agent.get(field) != agent["requested_" + field]:
                raise ValueError("DSH Web effective {} changed; message refused".format(field))

    async def send(self, session_id: str, task: str, mode: str = "queue", idle_only: bool = False,
                   images: list[dict] | None = None,
                   files: list[dict] | None = None) -> dict:
        if mode not in ("queue", "steer"):
            raise ValueError("Message mode must be queue or steer")
        agent = await self.status(session_id)
        await self._ensure_sendable(agent)

        if agent.get("status") == "closed":
            raise ValueError("Agent is closed")
        from ..core.policy import workspace_path
        workspace_path(str(agent.get("cwd") or ""))
        if idle_only and agent.get("status") in ("starting", "running", "waiting_approval", "waiting_input", "interrupting"):
            raise ValueError("Agent is busy; use dsh_send to queue a message, or wait until idle")
        state = await self._ensure(session_id)
        request_id = str(uuid.uuid4())
        # 网页支持发图片：content 块 = [text, image…]（见 session/prompt schema）
        content: list[dict] = []
        if str(task or "").strip():
            content.append({"type": "text", "text": task})
        for image in images or []:
            media = str(image.get("media_type") or image.get("mediaType") or "image/png")
            data = str(image.get("data") or "")
            if data.startswith("data:"):
                data = data.split(",", 1)[-1]
            if not data:
                raise ValueError("image.data (base64) is required")
            block = {"type": "image", "mediaType": media, "data": data}
            if image.get("name"):
                block["name"] = str(image["name"])
            content.append(block)
        for item in files or []:
            receipt = str(item.get("receipt_id") or item.get("receiptId") or "").strip()
            if not receipt:
                raise ValueError("file.receipt_id is required (upload it first)")
            content.append({"type": "file", "receiptId": receipt})
        if not content:
            raise ValueError("empty message: provide text or an image")
        if agent.get("status") not in ("starting", "running", "waiting_approval", "waiting_input", "interrupting"):
            agent["status"] = "starting"
            agent["finish_reason"] = None
            agent["answer"] = ""
            agent["partial_text"] = ""
        agent["pending_requests"] = [*(agent.get("pending_requests") or []), request_id]
        agent["owner_pid"] = os.getpid()
        self.manager.save(agent)
        try:
            await state["client"].rpc("session/prompt", {
                "request": {
                    "sessionId": session_id,
                    "requestId": request_id,
                    "mode": mode,
                    "content": content,
                }
            })
        except WebRemoteRejected as error:
            current = self.manager.get(session_id)
            current["pending_requests"] = [r for r in (current.get("pending_requests") or []) if r != request_id]
            self.manager.save(current)
            current["error"] = str(error)
            self.manager.save(current)
            raise
        except Exception as error:  # noqa: BLE001
            current = self.manager.get(session_id)
            current["status"] = "disconnected"
            current["connection_error"] = str(error)
            current["delivery_unknown"] = True
            self.manager.save(current)
            raise
        self.manager.event(session_id, "bridge/prompt", {"request_id": request_id, "task": task, "mode": mode})
        current = self.manager.get(session_id)
        current["request_id"] = request_id
        self.manager.save(current)
        return {**current, "delivery": {"accepted": True, "mode": mode, "request_id": request_id}}

    async def interrupt(self, session_id: str, reason: str = "调用方手动中断（面板/API）") -> dict:
        await self.status(session_id)
        state = await self._ensure(session_id)
        if self.manager.get(session_id).get("status") not in ("starting", "running", "interrupting"):
            return self.manager.get(session_id)
        current = self.manager.get(session_id)
        current["interrupted_by"] = reason
        self.manager.save(current)
        await state["client"].rpc("session/cancel", {"request": {"sessionId": session_id}})
        self.manager.event(session_id, "interrupted", {"reason": reason, "timeout": False})
        result = await self.manager.wait(session_id, 30)
        if result.get("wait_outcome") == "timeout":
            raise RuntimeError("DSH Web interruption is not yet confirmed; check status before redirecting")
        return result

    async def rename(self, session_id: str, title: str) -> dict:
        state = await self._ensure(session_id)
        value = await state["client"].rpc("session/rename", {"request": {"sessionId": session_id, "title": title}})
        agent = self.manager.get(session_id)
        agent["name"] = (value or {}).get("title") or title
        self.manager.save(agent)
        return agent

    async def close(self, session_id: str) -> dict:
        await self.interactions.detach(session_id, connected=True)
        agent = self.manager.get(session_id)
        if agent.get("status") == "closed":
            return agent
        state = self.live.pop(session_id, None)
        if state:
            await state["client"].close()
        agent["status"] = "closed"
        self.manager.save(agent)
        path = self._credential_path(agent.get("web_auth_id", ""))
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return agent

    # -- generic DSH Web feature surface (read/send any Web session) -------------
    # These mirror the browser client's own calls, so anything the Web UI can
    # show/do for a session is reachable here (list/detail/history/search/send).

    def _default_web_url(self) -> str:
        """The configured DSH Web launch URL (panel saved / captured)."""
        try:
            record = read_secret_json(Path(self.manager.config.database).parent / "web_launch.json")
            return str(record.get("url") or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _web_client(self, web_url: str = "") -> WebClient:
        url = (web_url or "").strip() or self._default_web_url()
        if not url:
            raise ValueError(
                "未配置 DSH Web：请在面板「服务管理」启动 Web 或保存授权连接"
            )
        return WebClient(await self._credentials(url))

    @staticmethod
    def _normalize_web_row(item: dict) -> dict:
        values = (item.get("projections") or {}).get("values") or {}
        selection = values.get("modelSelection") or {}
        chosen = selection.get("next") or selection.get("lastUsed") or {}
        return {
            "session_id": item.get("sessionId"),
            "title": values.get("title"),
            "cwd": item.get("cwd") or values.get("workingDirectory"),
            "running": bool(item.get("running")),
            "blank": bool(item.get("blank")),
            "updated_at": item.get("updatedAt"),
            "provider": chosen.get("provider"),
            "model": chosen.get("model"),
            "effort": chosen.get("reasoningEffort"),
            "agent_preset": values.get("agentPreset"),
            "permission": (values.get("permissions") or {}).get("currentValue"),
            "goal": values.get("goal"),
            "session_stats": values.get("sessionStats"),
            "cost": values.get("costUsage") or values.get("tokenUsage"),
        }

    async def web_sessions(self, web_url: str = "", limit: int = 200) -> list[dict]:
        """All visible DSH Web sessions with title/model/preset/permission/cwd."""
        client = await self._web_client(web_url)
        listing = await client.rpc("session/list", {"_request": {}})
        items = (listing or {}).get("items", [])
        rows = [self._normalize_web_row(i) for i in items]
        # V2 墓碑：删除的会话在运行中 DSH Web 内存里仍可能短暂可见，统一过滤
        try:
            import json as _json
            marker = Path(self.manager.config.database).parent / "web-deleted.json"
            if marker.is_file():
                deleted = set(_json.loads(marker.read_text(encoding="utf-8")).keys())
                rows = [r for r in rows if r.get("session_id") not in deleted]
        except Exception:  # noqa: BLE001
            pass
        rows.sort(key=lambda r: int(r.get("updated_at") or 0), reverse=True)
        return rows[: int(limit) if limit else len(rows)]

    async def web_detail(self, session_id: str, web_url: str = "") -> dict:
        """Full projection set the Web UI shows for a session (model/preset/
        permissions/goal/usage/context/inbox/todos/plan/…)."""
        client = await self._web_client(web_url)
        value = await client.rpc("session/projections", {"request": {"sessionId": session_id}})
        if not isinstance(value, dict):
            raise RuntimeError("session/projections returned an unexpected payload")
        return {"session_id": session_id, "as_of_seq": value.get("asOfSeq"),
                "values": value.get("values") or {}}

    async def web_history(self, session_id: str, *, before_seq: int | None = None,
                          max_messages: int = 200, web_url: str = "") -> dict:
        """Message-aligned history page (same source the Web UI scrolls back
        through). Pass the returned ``oldest_seq`` as ``before_seq`` to page."""
        client = await self._web_client(web_url)
        projection = await client.rpc("session/projections", {"request": {"sessionId": session_id}})
        through_seq = (projection or {}).get("asOfSeq")
        if not through_seq:
            raise RuntimeError("cannot resolve throughSeq (session/projections has no asOfSeq)")
        request: dict = {
            "address": {"kind": "session", "sessionId": session_id},
            "throughSeq": int(through_seq),
            "maxMessages": max(1, int(max_messages)),
        }
        if before_seq:
            request["beforeSeq"] = int(before_seq)
        page = await client.rpc("session/page", {"request": request})
        records = (page or {}).get("records") or []
        events = []
        for record in records:
            event = record.get("event") or {}
            events.append({
                "seq": event.get("seq"),
                "time": event.get("time"),
                "type": event.get("type"),
                "data": event.get("data"),
            })
        return {
            "session_id": session_id,
            "through_seq": through_seq,
            "has_more": bool((page or {}).get("hasMore")),
            "oldest_seq": next((e["seq"] for e in events if e.get("seq") is not None), None),
            "count": len(events),
            "events": events,
        }

    async def web_search(self, query: str, web_url: str = "") -> dict:
        """Search visible session content (Web UI search box)."""
        client = await self._web_client(web_url)
        try:
            value = await client.rpc("session/search", {"request": {"query": str(query)}})
        except WebRemoteRejected as error:
            if "search is disabled" in str(error) or "openAt" in str(error):
                raise RuntimeError(
                    "当前 DSH Web 未开启会话搜索索引（session-query openAt=never），搜索不可用；"
                    "网页上的搜索框同样不可用。其余 Web 能力不受影响。"
                ) from error
            raise
        return value if isinstance(value, dict) else {"results": value}

    async def web_send(self, session_id: str, task: str, mode: str = "queue",
                       web_url: str = "", images: list[dict] | None = None,
                       files: list[dict] | None = None) -> dict:
        """Send to ANY visible Web session (text and/or images; attaches a
        bridge observer on demand, including previously-closed rows)."""
        try:
            agent = self.manager.get(session_id)
        except KeyError:
            agent = None
        if (not agent or not agent.get("external") or agent.get("status") == "closed"):
            agent = await self.attach(session_id, (web_url or "").strip() or self._default_web_url())
        return await self.send(session_id, task, mode, images=images, files=files)

    async def web_attachment(self, session_id: str, attachment_id: str,
                             web_url: str = "") -> dict:
        """Read one image attached to a session log; save it locally and return
        the path (the MCP client cannot render base64 anyway)."""
        client = await self._web_client(web_url)
        value = await client.rpc("session/attachment", {
            "request": {"sessionId": session_id, "attachmentId": attachment_id}})
        media_type = "image/png"
        data = ""
        if isinstance(value, dict):
            media_type = str(value.get("mediaType") or value.get("mimeType") or media_type)
            data = str(value.get("data") or value.get("base64") or "")
        elif isinstance(value, str):
            data = value
        if data.startswith("data:"):
            header, data = data.split(",", 1)
            if "image/" in header:
                media_type = header.split(";")[0].split(":", 1)[-1]
        if not data:
            raise RuntimeError("session/attachment returned no image data")
        import base64

        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                     "image/gif": "gif"}.get(media_type, "bin")
        out_dir = Path(self.manager.config.database).parent / "web-attachments"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "{}-{}".format(session_id[-12:], attachment_id)[:80]
        path = out_dir / "{}.{}".format(safe, extension)
        try:
            path.write_bytes(base64.b64decode(data))
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("cannot decode attachment image: {}".format(error)) from error
        return {"session_id": session_id, "attachment_id": attachment_id,
                "path": str(path), "media_type": media_type,
                "bytes": path.stat().st_size}

    async def web_model_catalog(self, web_url: str = "") -> dict:
        """模型选择器的数据源（网页模型下拉）：default/routableProviders/groups。"""
        client = await self._web_client(web_url)
        value = await client.rpc("session/modelCatalog", {})
        return value if isinstance(value, dict) else {"catalog": value}

    async def web_select_model(self, session_id: str, provider: str, model: str,
                               reasoning_effort: str | None = None,
                               web_url: str = "") -> dict:
        """切换某个 Web 会话下一轮使用的模型（等价网页模型选择器）。"""
        raise ValueError("Session-local model selection is unavailable: this DSH version also writes the global default; use DSH Web explicitly")

    async def web_control(self, web_url: str = "", timeout: float = 8.0) -> dict:
        """主机级实时控制基线（网页的"实时状态"流）：订阅 session/control，
        取第一帧基线 + 短暂收集后续替换帧，然后断开。"""
        client = await self._web_client(web_url)
        frames: list[dict] = []

        def on_frame(frame: dict) -> None:
            frames.append(frame)

        await client.connect(lambda error: None)
        try:
            try:
                baseline = await asyncio.wait_for(
                    client.subscribe("session/control", {}, on_frame), timeout=timeout)
            except asyncio.TimeoutError:
                baseline = None
            await asyncio.sleep(0.4)  # let replacement frames arrive
            # Host projections contain full goals and conversation outlines.
            # Keep the default control view bounded; detail/history have their
            # own explicit, paginated tools.
            value = (baseline or {}).get("value") or {}
            projections = value.get("projections") or {}
            sessions = []
            for session_id, projection in list(projections.items())[:200]:
                values = projection.get("values") or {}
                selection = values.get("modelSelection") or {}
                sessions.append({"session_id": session_id,
                    "title": str(values.get("title") or "")[:120],
                    "permission": (values.get("permissions") or {}).get("currentValue"),
                    "model": selection.get("next") or selection.get("lastUsed"),
                    "goal_phase": ((values.get("goal") or {}).get("goal") or {}).get("phase")})
            return {"sessions": sessions, "total": len(projections),
                    "truncated": len(projections) > len(sessions), "frame_count": len(frames)}
        finally:
            await client.close()

    async def web_cancel(self, session_id: str, web_url: str = "") -> dict:
        """停止某 Web 会话当前回合（等价网页点停止；需会话在运行/已附加）。"""
        client = await self._web_client(web_url)
        value = await client.rpc("session/cancel", {"request": {"sessionId": session_id}})
        return value if isinstance(value, dict) else {"accepted": True}

    async def web_upload(self, session_id: str, path: str, name: str | None = None,
                         web_url: str = "") -> dict:
        """上传本地文件到某 Web 会话，返回收据（receiptId），随后可用
        session/prompt 的 {type:"file", receiptId} 发送（等价网页拖文件）。"""
        import httpx as _httpx

        source = Path(path)
        if not source.is_absolute():
            raise ValueError("Upload requires an absolute workspace file path")
        source = source.resolve(strict=True)
        rows = await self.web_sessions(web_url)
        row = next((item for item in rows if item.get("session_id") == session_id), None)
        if not row or not row.get("cwd"):
            raise ValueError("Cannot verify session workspace; upload refused")
        from ..core.policy import workspace_path
        root = workspace_path(row["cwd"])
        if not source.is_relative_to(root):
            raise ValueError("Upload outside session workspace is refused")
        if not source.is_file():
            raise ValueError("file not found: {}".format(path))
        credentials = await self._credentials((web_url or "").strip() or self._default_web_url())
        query = {"sessionId": session_id}
        filename = (name or source.name).strip()
        if filename:
            query["name"] = filename
        headers = {"cookie": credentials.cookie, "origin": credentials.origin,
                   "content-type": "application/octet-stream"}
        async with _httpx.AsyncClient(timeout=120.0, **http_options(credentials.origin)) as client:
            response = await client.post(
                credentials.origin + "/api/session/uploadFileBinary",
                params=query, content=source.read_bytes(), headers=headers)
        if response.status_code != 200:
            raise RuntimeError("upload failed: HTTP {} {}".format(
                response.status_code, response.text[:200]))
        value = response.json()
        # 服务端返回 {ok, value:{receiptId,file}} 信封
        if isinstance(value, dict) and value.get("ok") is True and isinstance(value.get("value"), dict):
            value = value["value"]
        receipt = str((value or {}).get("receiptId") or "")
        if not receipt:
            raise RuntimeError("upload returned no receiptId: {}".format(str(value)[:200]))
        return {"session_id": session_id, "receipt_id": receipt,
                "file": value.get("file") or {}, "name": filename,
                "bytes": source.stat().st_size}

    async def web_jobs(self, session_id: str, web_url: str = "", timeout: float = 6.0) -> dict:
        """某会话的后台作业基线（网页"作业"面板）。job/list 是流方法，走 mux 订阅取基线。"""
        client = await self._web_client(web_url)
        frames: list[dict] = []
        await client.connect(lambda error: None)
        try:
            try:
                baseline = await asyncio.wait_for(
                    client.subscribe("job/list", {"request": {"sessionId": session_id}},
                                     lambda f: frames.append(f)),
                    timeout=timeout)
            except asyncio.TimeoutError:
                baseline = None
            await asyncio.sleep(0.3)
            return {"baseline": baseline, "frame_count": len(frames), "frames": frames[:6]}
        finally:
            await client.close()

    async def web_skills(self, session_id: str, web_url: str = "") -> dict:
        if not session_id:
            raise ValueError("Runtime Skills require an authorized session_id")
        client = await self._web_client(web_url)
        return await client.rpc("skills/list", {"request": {"sessionId": session_id}}) or {"skills": []}

    async def web_settings(self, web_url: str = "") -> dict:
        """读 DSH Web 设置描述（writable/文档等，只读）。"""
        client = await self._web_client(web_url)
        return await client.rpc("settings/describe", {}) or {}

    async def web_providers(self, web_url: str = "") -> dict:
        """读 LLM provider 列表（只读）。"""
        client = await self._web_client(web_url)
        return await client.rpc("llm/listProviders", {}) or {}

    async def web_permission_presets(self, web_url: str = "") -> dict:
        """读权限预设目录（网页权限选择器的可选值，只读）。"""
        client = await self._web_client(web_url)
        return await client.rpc("permissionPresets/catalog", {}) or {}

    async def web_inventory(self, web_url: str = "") -> dict:
        client = await self._web_client(web_url)
        try:
            snapshot = await client.rpc("pluginInventory/list", {}) or {}
        except (RuntimeError, WebRemoteRejected) as error:
            return {"supported": False, "reason": str(error), "entries": [], "agent_presets": []}
        def project(row):
            return {"module": row.get("moduleName"), "configured": True,
                    "enabled": row.get("enabled"), "phase": row.get("fiberPhase"),
                    "loaded": row.get("fiberPhase") == "active",
                    "agent_callable": "unverified"}
        entries = [project(row) for row in snapshot.get("entries") or []]
        presets = [{"id": preset.get("id"), "default": preset.get("isDefault"),
                    "plugins": [project(row) for row in preset.get("rows") or []]}
                   for preset in snapshot.get("agentPresets") or []]
        return {"supported": True, "entries": entries, "agent_presets": presets,
                "note": "Loaded plugins are not proof that every Agent tool, MCP server or Skill is callable"}

    async def web_workspaces(self, web_url: str = "", timeout: float = 8.0) -> dict:
        """工作区基线（网页侧栏）：订阅 workspace/follow 取 baseline（工作区 →
        会话分组），然后断开。"""
        client = await self._web_client(web_url)
        frames: list[dict] = []
        await client.connect(lambda error: None)
        try:
            try:
                baseline = await asyncio.wait_for(
                    client.subscribe("workspace/follow", {}, lambda f: frames.append(f)),
                    timeout=timeout)
            except asyncio.TimeoutError:
                baseline = None
            value = ((baseline or {}).get("value") or {}) if isinstance(baseline, dict) else {}
            return {
                "workspaces": value.get("items") or [],
                "archived_session_ids": value.get("archivedSessionIds") or [],
                "pinned_session_ids": value.get("pinnedSessionIds") or [],
                "frame_count": len(frames),
            }
        finally:
            await client.close()

    async def web_workspace_create(self, path: str, web_url: str = "") -> dict:
        """把一个目录登记为工作区（workspace/create）。"""
        from ..core.policy import workspace_path
        path = str(workspace_path(path))
        client = await self._web_client(web_url)
        value = await client.rpc("workspace/create", {"request": {"path": path}}) or {}
        workspace = (value or {}).get("workspace") or {}
        return {"workspace_id": workspace.get("workspaceId"), "workspace": workspace}

    async def web_workspace_rename(self, workspace_id: str, title: str,
                                   web_url: str = "") -> dict:
        """重命名一个工作区。"""
        client = await self._web_client(web_url)
        return await client.rpc("workspace/rename", {
            "request": {"workspaceId": workspace_id, "title": title}}) or {}

    async def web_workspace_delete(self, workspace_id: str, web_url: str = "") -> dict:
        """删除一个工作区登记（保留文件与会话）。"""
        client = await self._web_client(web_url)
        return await client.rpc("workspace/delete", {
            "request": {"workspaceId": workspace_id}}) or {"workspace_id": workspace_id}

    async def web_pin(self, session_id: str, pinned: bool = True, web_url: str = "") -> dict:
        """置顶/取消置顶一个 Web 会话（workspace/pinSession|unpinSession）。"""
        client = await self._web_client(web_url)
        method = "workspace/pinSession" if pinned else "workspace/unpinSession"
        value = await client.rpc(method, {"request": {"sessionId": session_id}})
        return value if isinstance(value, dict) else {"session_id": session_id, "pinned": pinned}

    async def web_archive(self, session_id: str, archived: bool = True,
                          stop_activity: bool = False, web_url: str = "") -> dict:
        """归档/取消归档一个 Web 会话（从分组列表隐藏/恢复；可选先停止其活动）。"""
        client = await self._web_client(web_url)
        method = "workspace/archiveSession" if archived else "workspace/unarchiveSession"
        request: dict = {"sessionId": session_id}
        if archived and stop_activity:
            request["stopActivity"] = True
        value = await client.rpc(method, {"request": request})
        return value if isinstance(value, dict) else {"session_id": session_id, "archived": archived}

    async def web_rename(self, session_id: str, title: str, web_url: str = "") -> dict:
        """重命名任意 Web 会话（等价网页改名）。"""
        client = await self._web_client(web_url)
        return await client.rpc("session/rename", {
            "request": {"sessionId": session_id, "title": title}}) or {"title": title}

    async def web_execution_target(self, session_id: str, web_url: str = "") -> dict:
        rows = await self.web_sessions(web_url)
        row = next((item for item in rows if item.get("session_id") == session_id), None)
        if not row or row.get("permission") not in KNOWN_PERMISSION_PRESETS:
            raise ValueError("Unsafe or unknown session permission")
        from ..core.policy import workspace_path
        workspace_path(str(row.get("cwd") or ""))
        if row.get("blank") and not row.get("model"):
            default = (await self.web_model_catalog(web_url)).get("default") or {}
            row.update(model=default.get("model"), provider=default.get("provider"), effort=default.get("reasoningEffort"))
        return row

    async def web_queue(self, session_id: str, item_id: str, action: str,
                        content: str | None = None, web_url: str = "") -> dict:
        """管理 Web 会话的排队消息（网页 inbox）：edit 改文本 / remove 删除 /
        steer 插入当前步骤（仅运行中、next-turn 项）。"""
        if action in ("edit", "steer"):
            await self.web_execution_target(session_id, web_url)
        client = await self._web_client(web_url)
        act: dict = {"kind": action}
        if action == "edit":
            text = str(content or "")
            if not text.strip():
                raise ValueError("edit 需要非空 content")
            act["content"] = [{"type": "text", "text": text}]
        request: dict = {"sessionId": session_id, "itemId": item_id, "action": act}
        value = await client.rpc("session/updateQueue", {"request": request})
        return value if isinstance(value, dict) else {"accepted": True}

    async def web_fork(self, session_id: str, at_seq: int | None = None,
                       web_url: str = "") -> dict:
        """Fork a session prefix into a new session (creates a session)."""
        await self.web_execution_target(session_id, web_url)
        client = await self._web_client(web_url)
        request: dict = {"sessionId": session_id}
        if at_seq:
            request["atSeq"] = int(at_seq)
        return await client.rpc("session/fork", {"request": request}) or {}

    async def shutdown(self) -> None:
        for state in self.live.values():
            state["interaction_enabled"] = False
        for sid in list(self.live):
            await self.interactions.detach(sid, connected=True)
        for task in list(self.opening.values()):
            if not task.done():
                task.cancel()
        self.opening.clear()
        for state in list(self.live.values()):
            await state["client"].close()
        self.live.clear()
