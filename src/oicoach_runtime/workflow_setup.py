"""Install editable external workflows from package resources, without business logic in the scheduler."""

import json
import os
from importlib.resources import files
from pathlib import Path
import tempfile

from .config import absolute_path
from .errors import ConflictError, WorkflowError
from .models import validate_id
from .workflows import WorkflowProvider


def describe(workflow):
    return {"id": workflow.workflow_id, "version": workflow.revision, "workspace": str(workflow.workspace),
            "session_policy": workflow.session_policy, "memory_enabled": workflow.memory_enabled,
            "instructions_in_system": workflow.instructions_in_system,
            "builtin_skills": list(workflow.builtin_skills)}


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
        prompt = files("oicoach_runtime").joinpath("bundled", template + ".md").read_text(encoding="utf-8")
        prompt_path = destination / "event.md"
        prompt_path.touch(mode=0o600, exist_ok=False)
        prompt_path.write_text(prompt)
        manifest = destination / "workflow.toml"
        manifest.touch(mode=0o600, exist_ok=False)
        manifest.write_text('schema_version = 1\n' + f'id = {json.dumps(workflow_id)}\nversion = "1"\n'
            f'workspace = {json.dumps(str(directory), ensure_ascii=False)}\nsession_policy = "per_task"\n'
            'start_prompt = "event.md"\nwake_prompt = "event.md"\nrecovery_prompt = "event.md"\n'
            + ('memory_enabled = true\ninstructions_in_system = true\nbuiltin_skills = ["paveway-tijian"]\n'
               if template == "coach" else 'memory_enabled = false\n'))
        result = describe(WorkflowProvider(root).describe(workflow_id))
    except BaseException:
        for name in ("event.md", "workflow.toml"):
            (destination / name).unlink(missing_ok=True)
        destination.rmdir()
        raise
    return {"api_version": 1, "workflow": result, "template": template, "path": str(destination)}


def connect_tijian(config, workflow_id, executable, *, read_only=True):
    workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
    executable_path = Path(executable).expanduser().resolve()
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        raise WorkflowError(f"tijian-mcp executable not found or not executable: {executable_path}")
    path = workflow.workspace / ".mcp.json"
    if path.is_symlink():
        raise ConflictError("Refusing to modify a symlink MCP configuration")
    original = None
    if path.exists():
        if not path.is_file() or path.stat().st_size > 65536:
            raise WorkflowError("Existing MCP configuration must be a JSON file at most 64 KiB")
        original = path.read_bytes()
    try:
        raw = json.loads(original) if original is not None else {}
        if not isinstance(raw, dict) or not isinstance(raw.get("mcpServers", {}), dict):
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        raise WorkflowError("Existing MCP configuration is invalid; repair it explicitly") from None
    disabled = raw.get("disabledServers")
    if isinstance(disabled, list) and "tijian" in disabled:
        raise ConflictError("Tijian MCP server is disabled in disabledServers; enable it explicitly in .mcp.json before connecting")
    server = {
        "command": str(executable_path.absolute()),
        "args": ["--read-only"] if read_only else [],
    }
    servers = raw.setdefault("mcpServers", {})
    if "tijian" in servers and servers["tijian"] != server:
        raise ConflictError("An existing tijian MCP server differs; merge it explicitly")
    servers["tijian"] = server
    encoded = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode()
    if len(encoded) > 65536:
        raise WorkflowError("Merged MCP configuration exceeds 64 KiB")
    with tempfile.NamedTemporaryFile(dir=workflow.workspace, prefix=".paveway-mcp-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o600)
            if original is None:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    raise ConflictError("MCP configuration changed concurrently; retry after reviewing it") from None
            else:
                if path.is_symlink() or path.read_bytes() != original:
                    raise ConflictError("MCP configuration changed concurrently; retry after reviewing it")
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    path.chmod(0o600)
    return {"api_version": 1, "path": str(path), "server": "tijian", "availability": "configured_not_verified", "read_only": read_only}
