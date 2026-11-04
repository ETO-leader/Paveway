"""Install editable external workflows from package resources, without business logic in the scheduler."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .config import absolute_path
from .errors import ConflictError, WorkflowError
from .models import validate_id
from .workflows import WorkflowProvider


def describe(workflow):
    return {"id": workflow.workflow_id, "version": workflow.revision, "workspace": str(workflow.workspace),
            "session_policy": workflow.session_policy, "memory_enabled": workflow.memory_enabled,
            "instructions_in_system": workflow.instructions_in_system,
            "builtin_skills": list(workflow.builtin_skills), "base": workflow.base}


def create(config, workflow_id, workspace, template="coach"):
    validate_id(workflow_id)
    directory = absolute_path(str(workspace), "workspace")
    if not directory.is_dir() or template not in ("coach", "example"):
        raise WorkflowError("Choose an existing absolute workspace and a bundled coach/example template")
    root = config.workflow_root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = root / workflow_id
    # Exclusive mkdir preserves every existing workflow, including empty dirs/symlinks.
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError:
        raise ConflictError("Workflow already exists; edit its external files and bump its version") from None
    try:
        prompt_path = destination / "event.md"
        prompt_path.touch(mode=0o600, exist_ok=False)
        prompt_path.write_text("${event_json}\n", encoding="utf-8")
        manifest = destination / "workflow.toml"
        manifest.touch(mode=0o600, exist_ok=False)
        manifest_text = ('schema_version = 1\n' + f'id = {json.dumps(workflow_id)}\nversion = "1"\n'
            f'workspace = {json.dumps(str(directory), ensure_ascii=False)}\nsession_policy = "per_task"\n'
            'start_prompt = "event.md"\nwake_prompt = "event.md"\nrecovery_prompt = "event.md"\n')
        if template == "coach":
            manifest_text += 'base = "oi-coach"\nmemory_enabled = true\nbuiltin_skills = ["paveway-tijian"]\n'
        else:
            manifest_text += 'memory_enabled = false\n'
        manifest.write_text(manifest_text, encoding="utf-8")
        result = describe(WorkflowProvider(root).describe(workflow_id))
    except BaseException:
        for name in ("event.md", "workflow.toml"):
            (destination / name).unlink(missing_ok=True)
        destination.rmdir()
        raise
    return {"api_version": 1, "workflow": result, "template": template, "path": str(destination)}


@dataclass
class _McpConfiguration:
    directory: Path
    path: Path
    source: Path
    original: bytes | None
    raw: dict
    directory_identity: tuple | None

    @property
    def revision(self):
        content = b"absent" if self.original is None else b"present\0" + self.original
        return hashlib.sha256(content).hexdigest()


def _mcp_directory_identity(directory):
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ConflictError("OMP configuration directory must be owned and not writable by others or a symlink")
    return info.st_dev, info.st_ino


def _mcp_bytes(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ConflictError("Refusing to modify a symlink MCP configuration")
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise WorkflowError("Existing MCP configuration must be a JSON file at most 64 KiB")
    # Never follow a symlink swapped in between lstat and open.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ConflictError("MCP configuration changed concurrently; retry after reviewing it")
        content = stream.read(65537)
    if len(content) > 65536:
        raise WorkflowError("Existing MCP configuration must be a JSON file at most 64 KiB")
    return content


def _read_mcp(config, workflow_id, *, import_legacy=False):
    workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
    directory = workflow.workspace / ".omp"
    identity = _mcp_directory_identity(directory)
    path = directory / "mcp.json"
    source = path
    original = _mcp_bytes(path) if identity is not None else None
    if original is None and import_legacy:
        legacy = workflow.workspace / ".mcp.json"
        legacy_bytes = _mcp_bytes(legacy)
        if legacy_bytes is not None:
            source, original = legacy, legacy_bytes
    try:
        raw = json.loads(original) if original is not None else {}
        if not isinstance(raw, dict) or not isinstance(raw.get("mcpServers", {}), dict):
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        raise WorkflowError("Existing MCP configuration is invalid; repair it explicitly") from None
    disabled = raw.get("disabledServers")
    if isinstance(disabled, list) and "tijian" in disabled:
        raise ConflictError("Tijian MCP server is disabled in disabledServers; enable it explicitly before configuring")
    return _McpConfiguration(directory, path, source, original, raw, identity)


def _write_mcp(current, raw):
    # A semantic no-op preserves even hand-formatted JSON, inode and timestamps.
    if current.source == current.path and current.original is not None and raw == current.raw:
        return False
    encoded = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode()
    if len(encoded) > 65536:
        raise WorkflowError("Merged MCP configuration exceeds 64 KiB")
    current.directory.mkdir(mode=0o700, exist_ok=True)
    identity = _mcp_directory_identity(current.directory)
    if current.directory_identity is not None and identity != current.directory_identity:
        raise ConflictError("OMP configuration directory changed concurrently; review before configuring")
    with tempfile.NamedTemporaryFile(dir=current.directory, prefix=".paveway-mcp-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o600)
            if _mcp_directory_identity(current.directory) != identity:
                raise ConflictError("OMP configuration directory changed concurrently; review before configuring")
            if _mcp_bytes(current.source) != current.original:
                raise ConflictError("MCP configuration changed concurrently; retry after reviewing it")
            if current.original is None or current.source != current.path:
                try:
                    os.link(temporary, current.path)
                except FileExistsError:
                    raise ConflictError("MCP configuration changed concurrently; retry after reviewing it") from None
            else:
                os.replace(temporary, current.path)
        finally:
            temporary.unlink(missing_ok=True)
    current.original = encoded
    current.source = current.path
    current.raw = raw
    return True


def _tijian_server(current):
    servers = current.raw.get("mcpServers", {})
    if "tijian" not in servers:
        return None, None
    server = servers["tijian"]
    if (not isinstance(server, dict) or set(server) != {"command", "args"}
            or not isinstance(server["command"], str) or not server["command"]
            or "\0" in server["command"] or not Path(server["command"]).is_absolute()
            or not isinstance(server["args"], list)):
        raise WorkflowError("Existing tijian MCP server is unrecognized; repair it explicitly")
    args = server["args"]
    if args == []:
        permission = "full"
    elif args == ["--read-only"]:
        permission = "none"
    elif args in (["--write-policy=none"], ["--write-policy=managed"], ["--write-policy=full"]):
        permission = args[0].split("=", 1)[1]
    else:
        raise WorkflowError("Existing tijian MCP arguments are unrecognized; repair them explicitly")
    return server, permission


def _tijian_report(current, server, permission):
    return {"api_version": 1, "path": str(current.path), "configured": server is not None,
            "executable": server["command"] if server is not None else None, "permission": permission,
            "revision": current.revision,
            "availability": "configured_not_verified" if server is not None else "not_configured"}


def show_tijian(config, workflow_id):
    """Inspect native OMP configuration only; never create files or probe the executable."""
    current = _read_mcp(config, workflow_id)
    return _tijian_report(current, *_tijian_server(current))


def _tijian_executable(executable):
    executable_path = Path(executable).expanduser().resolve()
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        raise WorkflowError(f"tijian-mcp executable not found or not executable: {executable_path}")
    return str(executable_path)


def set_tijian(config, workflow_id, *, expected_revision, executable=None, permission=None, confirm_full=False):
    """Edit a recognized native server using an optimistic content revision."""
    current = _read_mcp(config, workflow_id)
    server, old_permission = _tijian_server(current)
    if expected_revision != current.revision:
        raise ConflictError("MCP configuration changed since it was shown; reload before saving")
    if permission is not None and permission not in ("none", "managed", "full"):
        raise WorkflowError("Tijian permission must be none, managed or full")
    if executable is None and permission is None:
        raise WorkflowError("Choose a Tijian executable or permission to save")
    if server is None and executable is None:
        raise WorkflowError("Configure a Tijian executable before changing permissions")
    new_permission = permission if permission is not None else old_permission or "managed"
    if new_permission == "full" and old_permission != "full" and not confirm_full:
        raise ConflictError("Full Tijian access requires explicit confirmation with --confirm-full")
    command = _tijian_executable(executable) if executable is not None else server["command"]
    # Path-only edits retain legacy flags exactly, rather than silently migrating policy.
    args = (server["args"] if server is not None and permission in (None, old_permission)
            else [f"--write-policy={new_permission}"])
    updated = {"command": command, "args": args}
    raw = {**current.raw, "mcpServers": {**current.raw.get("mcpServers", {}), "tijian": updated}}
    changed = _write_mcp(current, raw)
    return {**_tijian_report(current, updated, new_permission), "changed": changed}


def connect_tijian(config, workflow_id, executable, *, read_only=True):
    # The legacy command keeps its exact flags, conflict policy and explicit import.
    executable_path = _tijian_executable(executable)
    current = _read_mcp(config, workflow_id, import_legacy=True)
    server = {"command": executable_path, "args": ["--read-only"] if read_only else []}
    servers = current.raw.get("mcpServers", {})
    if "tijian" in servers and servers["tijian"] != server:
        raise ConflictError("An existing tijian MCP server differs; merge it explicitly")
    imported = current.source != current.path
    _write_mcp(current, {**current.raw, "mcpServers": {**servers, "tijian": server}})
    return {"api_version": 1, "path": str(current.path), "server": "tijian",
            "availability": "configured_not_verified", "read_only": read_only, "legacy_imported": imported}
