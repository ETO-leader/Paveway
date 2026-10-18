import json
import math
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .errors import ConfigError


@dataclass(frozen=True)
class ServerConfig:
    socket_path: str = ""
    poll_interval_seconds: float = 1
    max_concurrent_runs: int = 1


@dataclass(frozen=True)
class AgentConfig:
    adapter: str = "omp-rpc"
    executable: str = "omp"
    profile: str = ""
    model: str = ""
    tools_enabled: bool = True
    extensions_enabled: bool = True
    skills_enabled: bool = True
    rules_enabled: bool = True
    startup_timeout_seconds: float = 30
    ack_timeout_seconds: float = 30
    turn_timeout_seconds: float = 3600
    settle_timeout_seconds: float = 900
    graceful_stop_seconds: float = 15


@dataclass(frozen=True)
class RestartConfig:
    max_failures_per_window: int = 5
    window_seconds: float = 300
    backoff_initial_seconds: float = 2
    backoff_max_seconds: float = 120
    cooldown_seconds: float = 60
    jitter_ratio: float = 0.2


@dataclass(frozen=True)
class QueueConfig:
    default_max_attempts: int = 3
    expired_event_policy: str = "keep"
    uncertain_event_policy: str = "manual_review"


@dataclass(frozen=True)
class TijianConfig:
    enabled: bool = False
    executable: str = "tijian"
    timeout_seconds: float = 120
    max_output_bytes: int = 8388608
    read_only: bool = False


@dataclass(frozen=True)
class Config:
    state_dir: Path
    workflow_root: Path
    schema_version: int = 1
    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    restart: RestartConfig = field(default_factory=RestartConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    tijian: TijianConfig = field(default_factory=TijianConfig)

    @property
    def database_path(self) -> Path:
        return self.state_dir / "runtime.sqlite3"


def xdg_path(variable: str, fallback: str) -> Path:
    value = Path(os.environ.get(variable) or (Path.home() / fallback))
    if not value.is_absolute():
        raise ConfigError(f"{variable} must be absolute")
    return value


def default_config_path() -> Path:
    return xdg_path("XDG_CONFIG_HOME", ".config") / "oicoach-runtime/config.toml"


def defaults() -> Config:
    state = xdg_path("XDG_STATE_HOME", ".local/state") / "oicoach-runtime"
    return Config(state, xdg_path("XDG_CONFIG_HOME", ".config") / "oicoach-runtime/workflows",
                  server=ServerConfig(socket_path=str(state / "runtime.sock")))


def absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ConfigError(f"{name} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path (no ~ expansion)")
    return path


def _section(cls, raw: object):
    if not isinstance(raw, dict):
        raise ConfigError(f"{cls.__name__} must be a TOML table")
    expected = {f.name for f in fields(cls)}
    if set(raw) - expected:
        raise ConfigError(f"Unknown keys in {cls.__name__}")
    baseline = cls()
    for key, value in raw.items():
        default = getattr(baseline, key)
        if isinstance(default, bool):
            valid = type(value) is bool
        elif isinstance(default, str):
            valid = isinstance(value, str) and "\0" not in value
        elif key == "max_output_bytes":
            valid = type(value) is int and 1024 <= value <= 67108864
        elif isinstance(default, int) and not key.endswith("_seconds"):
            valid = type(value) is int and 1 <= value <= 1_000_000
        elif key == "jitter_ratio":
            valid = type(value) in (int, float) and 0 <= value <= 1
        else:
            valid = type(value) in (int, float) and 0 < value <= 31_536_000 and math.isfinite(value)
        if not valid:
            raise ConfigError(f"Invalid value for {cls.__name__}.{key}")
    return cls(**raw)


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigError("Cannot read configuration as TOML; run init or check the file") from None
    allowed = {f.name for f in fields(Config)}
    if set(raw) - allowed:
        raise ConfigError("Unknown top-level configuration keys")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ConfigError("Only config schema_version = 1 is supported")
    state = absolute_path(raw.get("state_dir"), "state_dir")
    root = absolute_path(raw.get("workflow_root"), "workflow_root")
    server_raw = raw.get("server", {})
    server = _section(ServerConfig, server_raw)
    if not server.socket_path:
        server = ServerConfig(str(state / "runtime.sock"), server.poll_interval_seconds, server.max_concurrent_runs)
    socket_path = absolute_path(server.socket_path, "server.socket_path")
    if len(os.fsencode(socket_path)) > 107:
        raise ConfigError("server.socket_path exceeds the Linux Unix socket path limit")
    if server.max_concurrent_runs != 1:
        raise ConfigError("Only one concurrent run is supported")
    agent = _section(AgentConfig, raw.get("agent", {}))
    if agent.adapter != "omp-rpc" or not agent.executable or (not Path(agent.executable).is_absolute() and any(c.isspace() for c in agent.executable)):
        raise ConfigError("agent must use omp-rpc and an executable name or absolute path, not a shell command")
    if "/" in agent.executable and not Path(agent.executable).is_absolute():
        raise ConfigError("agent.executable cannot be a relative path")
    if any(value.startswith("-") or len(value) > 256 for value in (agent.model, agent.profile)):
        raise ConfigError("agent.model/profile must be bounded selectors, not CLI flags")
    restart = _section(RestartConfig, raw.get("restart", {}))
    if restart.backoff_initial_seconds > restart.backoff_max_seconds:
        raise ConfigError("Initial backoff exceeds maximum backoff")
    if restart.max_failures_per_window > 1000:
        raise ConfigError("restart.max_failures_per_window cannot exceed 1000")
    queue = _section(QueueConfig, raw.get("queue", {}))
    if queue.expired_event_policy != "keep" or queue.uncertain_event_policy != "manual_review":
        raise ConfigError("Only keep/manual_review queue policies are supported")
    tijian = _section(TijianConfig, raw.get("tijian", {}))
    if not tijian.executable or (not Path(tijian.executable).is_absolute() and
                                ("/" in tijian.executable or any(c.isspace() for c in tijian.executable))):
        raise ConfigError("tijian.executable must be an executable name or absolute path")
    return Config(state, root, server=server, agent=agent, restart=restart, queue=queue, tijian=tijian)


def config_text(config: Config) -> str:
    # JSON string quoting is compatible with TOML basic strings for filesystem paths.
    lines = ["schema_version = 1", f"state_dir = {json.dumps(str(config.state_dir), ensure_ascii=False)}",
             f"workflow_root = {json.dumps(str(config.workflow_root), ensure_ascii=False)}"]
    for name in ("server", "agent", "restart", "queue", "tijian"):
        section = getattr(config, name)
        lines.extend(["", f"[{name}]"])
        for f in fields(section):
            lines.append(f"{f.name} = {json.dumps(getattr(section, f.name), ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def ensure_private_directory(path: Path) -> None:
    """Create only owned runtime directories; refuse an existing unsafe target."""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError("Runtime directories must be owned by the current user, not symlinks, and have mode 0700")
