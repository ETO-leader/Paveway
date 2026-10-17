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


class NotFoundError(RuntimeContractError):
    code = "not_found"


class DaemonError(RuntimeContractError):
    code = "daemon_unavailable"


class AgentError(RuntimeContractError):
    code = "agent_protocol_error"
