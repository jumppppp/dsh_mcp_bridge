"""Explicit, session-scoped native interaction handling. No permission grants."""
from __future__ import annotations
import asyncio
import copy
import os
import time
import uuid


def validate_answers(questions, answers):
    if not isinstance(questions, list) or not questions or not isinstance(answers, list):
        raise ValueError("Questions and answers must be non-empty structured batches")
    if any(q.get("intent") for q in questions):
        raise ValueError("Tagged decisions (including plan approval) require the DSH Web user")
    by_id = {q.get("id"): q for q in questions}
    if len(by_id) != len(questions) or len(answers) != len(questions):
        raise ValueError("Answer every question exactly once")
    result, seen = [], set()
    for answer in answers:
        if not isinstance(answer, dict) or set(answer) - {"id", "selected", "custom"}:
            raise ValueError("Invalid answer fields")
        qid, selected = answer.get("id"), answer.get("selected")
        if qid not in by_id or qid in seen or not isinstance(selected, list):
            raise ValueError("Invalid or duplicate question id")
        q = by_id[qid]
        if any(not isinstance(v, str) for v in selected) or len(set(selected)) != len(selected):
            raise ValueError("Selected labels must be unique strings")
        labels = {o.get("label") for o in q.get("options", [])}
        if any(v not in labels for v in selected):
            raise ValueError("Answer must use the offered option labels")
        custom = answer.get("custom", "")
        if not isinstance(custom, str) or len(custom) > 8000:
            raise ValueError("Invalid custom answer")
        if not q.get("multiSelect") and len(selected) + bool(custom.strip()) > 1:
            raise ValueError("Single-select accepts one choice or custom text")
        seen.add(qid)
        result.append({"id": qid, "selected": selected, **({"custom": custom} if custom else {})})
    return result


class Interactions:
    def __init__(self, external):
        self.external = external
        self.pending = {}
        self.receipts = {}

    def _public(self, entry):
        return copy.deepcopy({k: entry[k] for k in ("interaction_id", "session_id", "kind", "request", "expires_at")})

    def publish(self, sid):
        agent = self.external.manager.get(sid)
        agent["pending_interactions"] = [self._public(e) for e in self.pending.values() if e["session_id"] == sid]
        state = self.external.live.get(sid)
        if state and state.get("running"):
            if agent["pending_interactions"]:
                agent["status"] = "waiting_approval" if any(i["kind"] == "approval" for i in agent["pending_interactions"]) else "waiting_input"
            else:
                agent["status"] = "waiting_approval" if state.get("awaiting_approval") else "running"
        self.external.manager.save(agent, bump=False)
        if state and state.get("ready"):
            self.external._settle(sid, state)

    async def configure(self, sid, enabled):
        state = await self.external._ensure(sid)
        state["interaction_enabled"] = enabled
        if not enabled:
            for key in [k for k, v in self.pending.items() if v["session_id"] == sid]:
                await self._finish(key, {"kind": "next"})
        return {"session_id": sid, "enabled": enabled, "permission_grants": False}

    async def handle(self, sid, state, frame):
        # Each observer gets global forwarded events; never claim another session.
        if frame.get("agentId") != sid or not state.get("interaction_enabled") or frame.get("event") not in ("approval/request", "user-questions/request"):
            from .external import _answer_next
            await _answer_next(state["client"], state, frame)
            return
        if self.external.live.get(sid) is not state:
            return
        key = frame.get("eventId")
        if not isinstance(key, str) or not state.get("clientId") or key in self.pending:
            return
        request = copy.deepcopy(frame.get("request") or {})
        kind = "approval" if frame["event"] == "approval/request" else "question"
        if kind == "question" and (not isinstance(request.get("questions"), list) or not request["questions"]):
            from .external import _answer_next
            await _answer_next(state["client"], state, frame)
            return
        entry = {"interaction_id": key, "session_id": sid, "kind": kind, "request": request,
                 "state": state, "deadline": time.monotonic() + 120, "expires_at": time.time() + 120}
        self.pending[key] = entry
        self.publish(sid)
        try:
            wait = request.get("wait") or {}
            if kind == "question" and wait.get("timed"):
                stream = str(uuid.uuid4())
                entry["claim_stream"] = stream
                value = await asyncio.wait_for(state["client"].subscribe("userQuestions/attachWait",
                    {"agentId": sid, "callId": wait["callId"]}, lambda value: None, stream_id=stream), 5)
                if key not in self.pending:
                    await state["client"].cancel_stream(stream)
                    return
                remaining = min(max(float(value["remainingMs"]) / 1000, 0), 600)
                entry.update(deadline=time.monotonic() + remaining, expires_at=time.time() + remaining)
                self.publish(sid)
            if key in self.pending:
                entry["timer"] = asyncio.create_task(self._expire(key, entry["deadline"]))
        except Exception:
            await self._finish(key, {"kind": "next"})

    async def _expire(self, key, deadline):
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        try:
            await self._finish(key, {"kind": "next"})
        except Exception:
            pass  # An expired/disconnected request is never replayed.

    def forget(self, key):
        entry = self.pending.pop(key, None)
        if entry:
            timer = entry.get("timer")
            if timer and timer is not asyncio.current_task():
                timer.cancel()
            self.publish(entry["session_id"])
        return entry

    async def cancel(self, key):
        entry = self.forget(key)
        if entry and entry.get("claim_stream"):
            await entry["state"]["client"].cancel_stream(entry["claim_stream"])

    async def _finish(self, key, outcome):
        entry = self.forget(key)
        if not entry:
            return
        try:
            await entry["state"]["client"].rpc("$events/result", {
                "clientId": entry["state"]["clientId"], "eventId": key, "outcome": outcome})
        finally:
            if entry.get("claim_stream"):
                await entry["state"]["client"].cancel_stream(entry["claim_stream"])

    async def listing(self, sid):
        await self.external._ensure(sid)
        detail = await self.external.web_detail(sid)
        durable = []
        for item in (detail.get("values", {}).get("userQuestions") or {}).get("active", []):
            if any(e["session_id"] == sid and (e["request"].get("wait") or {}).get("callId") == item.get("callId") for e in self.pending.values()):
                continue
            durable.append({"interaction_id": "question:" + item["callId"], "session_id": sid,
                            "kind": "question", "state": item["state"], "request": {"questions": item["questions"]},
                            "reply_available": item["state"] == "continued"})
        items = [self._public(e) for e in self.pending.values() if e["session_id"] == sid] + durable
        agent = self.external.manager.get(sid)
        agent["pending_interactions"] = items
        self.external.manager.save(agent, bump=False)
        return {"session_id": sid, "enabled": bool(self.external.live[sid].get("interaction_enabled")),
                "items": items, "permission_grants": False}

    async def _check_target(self, sid):
        row = await self.external.web_execution_target(sid)
        agent = self.external.manager.get(sid)
        for field in ("permission", "model", "provider", "effort"):
            requested = agent.get("requested_" + field)
            if requested and row.get(field) != requested:
                raise ValueError("Native session settings changed; review before answering")

    async def reply(self, sid, key, decision, answers=None, confirm=False):
        if confirm is not True:
            raise ValueError("Explicit confirm=true is required")
        if decision not in ("answer", "reject", "delegate"):
            raise ValueError("Permission grants are unavailable; use DSH Web for approval")
        async with self.external.manager.lock("interaction:" + sid):
            receipt_key = (sid, key)
            if receipt_key in self.receipts:
                # The first reply may have reached DSH even if its HTTP ack was lost.
                raise ValueError("Interaction already submitted or delivery unknown; do not replay")
            entry = self.pending.get(key)
            if entry:
                if entry["session_id"] != sid or self.external.live.get(sid) is not entry["state"]:
                    raise ValueError("Interaction belongs to another session or expired connection")
                if time.monotonic() >= entry["deadline"]:
                    await self._finish(key, {"kind": "next"})
                    raise ValueError("Interaction expired; refresh pending interactions")
                if decision == "answer":
                    if entry["kind"] != "question":
                        raise ValueError("Approvals cannot be answered with question data")
                    checked = validate_answers(entry["request"]["questions"], answers)
                    await self._check_target(sid)
                    outcome = {"kind": "result", "value": {"answers": checked}}
                elif decision == "reject" and entry["kind"] == "approval":
                    outcome = {"kind": "result", "value": "rejected"}
                elif decision == "delegate":
                    outcome = {"kind": "next"}
                else:
                    raise ValueError("Use delegate to return a question to DSH Web")
                if self.pending.get(key) is not entry:
                    raise ValueError("Interaction was cancelled while validating; refresh")
                self._reserve(receipt_key)
                await self._finish(key, outcome)
                return {"interaction_id": key, "submitted": True, "decision": decision, "delivery": "rpc-acknowledged"}
            if not key.startswith("question:") or decision != "answer":
                raise ValueError("Interaction no longer pending; refresh before replying")
            current = await self.listing(sid)
            item = next((i for i in current["items"] if i["interaction_id"] == key), None)
            if not item or not item.get("reply_available"):
                raise ValueError("Question is no longer continued/answerable")
            checked = validate_answers(item["request"]["questions"], answers)
            await self._check_target(sid)
            state = await self.external._ensure(sid)
            self._reserve(receipt_key)
            accepted = await state["client"].rpc("userQuestions/answer", {
                "agentId": sid, "callId": key[len("question:"):], "answer": {"answers": checked}})
            if accepted:
                agent = self.external.manager.get(sid)
                agent["owner_pid"] = os.getpid()
                self.external.manager.save(agent, bump=False)
            return {"interaction_id": key, "submitted": bool(accepted), "delivery": "queued" if accepted else "stale",
                    "note": "Queued answer is not proof of model execution"}

    def _reserve(self, key):
        self.receipts[key] = True
        if len(self.receipts) > 512:
            self.receipts.pop(next(iter(self.receipts)))

    async def detach(self, sid, connected=False):
        for key in [k for k, e in self.pending.items() if e["session_id"] == sid]:
            if connected:
                try:
                    await self._finish(key, {"kind": "next"})
                except Exception:
                    pass
            else:
                await self.cancel(key)
