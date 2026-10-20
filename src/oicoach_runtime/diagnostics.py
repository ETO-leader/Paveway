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
