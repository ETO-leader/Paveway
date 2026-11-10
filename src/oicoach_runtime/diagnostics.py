"""Safe failure summaries: never persist arbitrary provider response/request text."""

import re


def prompt_failure(status, detail):
    if status == "completed":
        return None, None
    if status == "aborted":
        return "agent_aborted", "OMP 回合已中止，没有生成最终回复。"
    detail = detail if isinstance(detail, dict) else {}
    message = detail.get("message", "")
    message = message[:8192].lower() if isinstance(message, str) else ""
    http = detail.get("httpStatus")
    if type(http) is not int:
        match = re.search(r"\b(401|403|404|408|429|5\d\d)\b", message)
        http = int(match[1]) if match else None
    if http == 401 or "invalid api key" in message or "invalid_api_key" in message:
        return "agent_auth_failed", "模型认证失败（HTTP 401 / API key 无效或缺失）。检查 daemon 使用的模型凭证；systemd 服务不会自动继承终端环境，更新凭证后需重启服务。"
    if http == 403:
        return "agent_access_denied", "模型服务拒绝访问（HTTP 403）。检查账号、模型及接口权限。"
    if http == 429:
        return "agent_rate_limited", "模型服务限流或额度不足（HTTP 429）。检查额度和调用限制。"
    if http == 404:
        return "agent_model_not_found", "模型或接口不存在（HTTP 404）。检查模型名称与接口地址。"
    if http == 408 or "timeout" in message or "timed out" in message:
        return "agent_provider_timeout", "模型请求超时。检查网络和模型服务状态。"
    if http is not None and 500 <= http <= 599:
        return "agent_provider_unavailable", f"模型服务暂时不可用（HTTP {http}）。检查供应商服务状态。"
    return "agent_turn_failed", "OMP 回合失败，没有生成最终回复。请检查模型配置、凭证和原生 OMP 会话；供应商原始错误不写入公共日志。"



def failure_hint(code, dispatched=False, status=None):
    if code == "native_wakeup_snapshot_missing":
        stage, action = "configuration", "Inspect the event; cancel if queued and schedule a new authorized request. Do not reconstruct from current settings."
    elif code and ("auth" in code or "api_key" in code):
        stage, action = "model_authentication", "Check model credentials on this device; inspect execution evidence before any retry."
    elif code in ("memory_recall_failed", "memory_projection_failed"):
        stage, action = "memory", "Check Runtime Memory availability; use explicit recovery. Native UI can explicitly disable Memory context for a new turn."
    elif code and (code.startswith("session_") or code == "conflict"):
        stage, action = "binding", "Inspect binding and session ownership; continue in a new task if needed. Preserve the old session."
    elif code and ("persist" in code or code in ("storage_error", "agent_shutdown_failed")):
        stage, action = "persistence", "Check storage and saved session; verify side effects before explicit recovery."
    elif code and ("ready" in code or code in ("agent_start_failed", "agent_cli_unsupported")):
        stage, action = "host_startup", "Check the configured OMP executable, pinned host installation and RPC compatibility."
    else:
        stage, action = ("dispatch" if dispatched else "pre_dispatch"), "Inspect event and Run evidence, then choose an existing explicit recovery operation."
    return {"stage": stage, "code": code, "dispatch_intent": bool(dispatched),
            "outcome_unknown": status == "uncertain", "automatic_replay": False, "next_action": action}


def report(store):
    from .frontend import Frontend
    groups = {}
    # One row per event, not every historical attempt. Preserve the underlying
    # Runs via their identifiers instead of changing/coalescing durable events.
    rows = store.connection.execute("""SELECT e.event_id,e.workflow_id,e.task_id,e.status,
        e.last_error_code,r.run_id,coalesce(r.binding_id,b.binding_id) AS binding_id,r.dispatch_intent_at
        FROM events e LEFT JOIN runs r ON r.run_id=e.run_id
        LEFT JOIN runtime_metadata n ON n.key='native-wakeup-config:v1:' || e.event_id
        LEFT JOIN session_bindings b ON b.workflow_id=e.workflow_id
            AND b.task_id=coalesce(json_extract(n.value,'$.binding_task_id'),e.task_id)
        WHERE e.status IN ('blocked','uncertain','failed','dead_letter')
        ORDER BY e.updated_at DESC,e.event_id LIMIT 100""").fetchall()
    for row in rows:
        key = row["binding_id"] or (row["workflow_id"], row["task_id"])
        if key not in groups:
            groups[key] = {"binding_id": row["binding_id"], "workflow_id": row["workflow_id"],
                           "task_id": row["task_id"], "events": []}
        groups[key]["events"].append({"event_id": row["event_id"], "run_id": row["run_id"],
            "status": row["status"], **failure_hint(row["last_error_code"], row["dispatch_intent_at"], row["status"])})
    native = [{"conversation_id": record["conversation_id"], "workflow_id": record["workflow_id"],
               "task_id": record["task_id"], "status": record["status"], "automatic_replay": False,
               "next_action": "Exit the live UI normally; frontend recover requires verified dead owners and a released lock."}
              for record in Frontend(store).records() if record["status"] == "uncertain"][:100]
    return {"binding_groups": list(groups.values()), "native_uncertain": native,
            "event_limit": 100, "truncated": len(rows) == 100}
