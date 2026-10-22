class RuntimeContractError(ValueError):
    """A public validation error; messages must not include private payloads."""

    code = "invalid_input"


class ConfigError(RuntimeContractError):
    code = "invalid_config"


class StorageError(RuntimeContractError):
    code = "storage_error"


class WorkflowError(RuntimeContractError):
    code = "invalid_workflow"


class ConflictError(RuntimeContractError):
    code = "conflict"


class SessionConflictError(ConflictError):
    """Fixed public diagnostics; never interpolate paths or session contents."""

    REASONS = {
        "session_uncertain": "当前任务上次执行的结果尚未确认，已暂停续聊。输入 /new 创建新任务继续；/recover 查看旧事件，核对副作用后再决定是否恢复。旧事件不会自动重播。",
        "session_blocked": "当前任务的会话需要修复。输入 /new 创建新任务继续，或用 /events 查看旧任务状态；原会话和历史保留。",
        "session_workflow_changed": "当前任务绑定的工作目录或 workflow 版本已改变。输入 /new 创建使用当前配置的新任务，或恢复原来的绑定配置。",
        "session_file_missing": "当前任务绑定的会话文件已丢失。请恢复会话备份，或输入 /new 创建新任务；不会悄悄替换原会话。",
    }

    def __init__(self, code):
        super().__init__(self.REASONS[code])
        self.code = code


class NotFoundError(RuntimeContractError):
    code = "not_found"


class DaemonError(RuntimeContractError):
    code = "daemon_unavailable"


class AgentError(RuntimeContractError):
    code = "agent_protocol_error"

    def __init__(self, message, *, code="agent_protocol_error"):
        super().__init__(message)
        self.code = code
