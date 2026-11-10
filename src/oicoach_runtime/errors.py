class RuntimeContractError(ValueError):
    """A public validation error; messages must not include private payloads."""

    code = "invalid_input"


class ConfigError(RuntimeContractError):
    code = "invalid_config"


class StorageError(RuntimeContractError):
    code = "storage_error"


class WakeupSnapshotError(RuntimeContractError):
    code = "native_wakeup_snapshot_missing"

    def __init__(self):
        super().__init__("Native wakeup snapshot is missing; no prompt was sent. Inspect the event, cancel it if still queued, and schedule a new explicitly authorized request. Mutable current settings cannot reconstruct its original snapshot.")


class WorkflowError(RuntimeContractError):
    code = "invalid_workflow"


class ConflictError(RuntimeContractError):
    code = "conflict"


class NativeSessionError(ConflictError):
    """Fixed session-integrity diagnostics, without filenames or contents."""
    def __init__(self, code):
        self.code = code
        super().__init__({
            "native_session_unsafe": "Native session file is not owned, regular or inside its assigned directory.",
            "native_session_incomplete": "Native session ends with an incomplete JSONL entry.",
            "native_session_entry_limit": "Native session has a JSONL entry over the supported 1 MiB validation limit.",
            "native_session_changed": "Native session changed or was replaced during validation.",
            "native_session_invalid": "Native session is not valid supported JSONL.",
            "native_session_identity_mismatch": "Native session header does not match its recorded identity.",
            "native_session_missing": "The persisted native session is missing; preserve ownership and restore the original file.",
        }[code])


class SessionConflictError(ConflictError):
    """Fixed public diagnostics; never interpolate paths or session contents."""

    REASONS = {
        "session_uncertain": "当前任务上次执行的结果尚未确认，已暂停续聊。输入 /new 创建新任务继续；/recover 查看旧事件，核对副作用后再决定是否恢复。旧事件不会自动重播。",
        "session_blocked": "当前任务的会话需要修复。输入 /new 创建新任务继续，或用 /events 查看旧任务状态；原会话和历史保留。",
        "session_workflow_changed": "当前任务绑定的工作目录或 workflow 版本已改变。输入 /new 创建使用当前配置的新任务，或恢复原来的绑定配置。",
        "session_file_missing": "当前任务绑定的会话文件已丢失。请恢复会话备份，或输入 /new 创建新任务；不会悄悄替换原会话。",
        "session_prompt_changed": "当前会话使用旧提示词架构，或角色绑定已改变。输入 /new 或使用新 task/session 继续；旧会话与历史完整保留，不自动迁移或重播。",
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
