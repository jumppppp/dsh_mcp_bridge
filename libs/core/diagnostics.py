"""Human-readable terminal reasons without changing the underlying task status."""
MESSAGES={
    "max-tokens":"达到模型输出长度上限，本轮未正常完成。请核对已有产物后决定是否手动续接。",
    "blocked":"任务被执行策略阻止，请查看原生审批或事件记录。",
    "interrupted":"本轮被中断，未正常完成。中断不会回滚已有文件。",
    "cancelled":"本轮已取消，中断不会回滚已有文件。",
    "forked":"此轮在会话分叉边界关闭，不代表原任务成功完成。",
}

def error_message(agent):
    reason=agent.get("finish_reason") or {}
    if not isinstance(reason,dict):
        reason={"kind":str(reason)}
    kind=reason.get("kind")
    if kind=="completed" and agent.get("status")=="completed":
        return None
    error=agent.get("error") or reason.get("error")
    if isinstance(error,dict):
        error=error.get("message") or error.get("code")
    if error:
        return str(error)
    if kind in MESSAGES:
        return MESSAGES[kind]
    if kind=="aborted":
        return "本轮已停止，请查看停止原因；已改文件不会回滚。"
    if agent.get("status") in ("error","context_exhausted"):
        if kind:
            return "本轮以 " + str(kind) + " 结束，运行时未提供更详细的错误说明。"
        return "任务记录为错误，但运行时未提供详细原因；请核对事件与原生历史。"
    return None

def enrich(agent):
    result=dict(agent)
    message=error_message(agent)
    if message:
        result["error"]=message
    reason=agent.get("finish_reason") or {}
    if isinstance(reason,dict) and reason.get("kind"):
        result["finish_reason_code"]=reason["kind"]
    return result
