"""Read trusted external manifests and templates as data, never executable code."""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from string import Template

from .errors import WorkflowError
from .models import validate_id

MAX_TEMPLATE_BYTES = 65536


def contained_file(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise WorkflowError("Template path must be a nonempty relative path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise WorkflowError("Template path must stay inside its workflow directory")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise WorkflowError("Template is missing or escapes its workflow directory")
    return resolved


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    revision: str
    workspace: Path
    session_policy: str
    templates: dict[str, str]

    def render(self, kind: str, envelope: dict) -> str:
        if kind not in self.templates:
            raise WorkflowError("Unknown workflow event kind")
        # Substitute once; dollar signs or placeholders inside payload are never evaluated.
        return Template(self.templates[kind]).substitute(
            event_json=json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )


class WorkflowProvider:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def describe(self, workflow_id: str) -> Workflow:
        try:
            validate_id(workflow_id)
            directory = (self.root / workflow_id).resolve()
            if not directory.is_relative_to(self.root):
                raise WorkflowError("Workflow directory escapes configured root")
            manifest = contained_file(directory, "workflow.toml")
            if manifest.stat().st_size > MAX_TEMPLATE_BYTES:
                raise WorkflowError("Workflow manifest is too large")
            raw = tomllib.loads(manifest.read_text(encoding="utf-8"))
            expected = {"schema_version", "id", "version", "workspace", "session_policy",
                        "start_prompt", "wake_prompt", "recovery_prompt"}
            if set(raw) != expected or type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
                raise WorkflowError("Workflow must use the complete v1 manifest schema")
            if raw["id"] != workflow_id or not isinstance(raw["version"], str) or not raw["version"] or len(raw["version"]) > 128:
                raise WorkflowError("Workflow ID or revision is invalid")
            if raw["session_policy"] != "per_task":
                raise WorkflowError("Only per_task sessions are supported in v1")
            if not isinstance(raw["workspace"], str) or not Path(raw["workspace"]).is_absolute():
                raise WorkflowError("Workspace must be absolute")
            workspace = Path(raw["workspace"])
            if not workspace.is_dir():
                raise WorkflowError("Workspace directory is missing")
            templates = {}
            for kind in ("start", "wake", "recovery"):
                path = contained_file(directory, raw[f"{kind}_prompt"])
                if path.stat().st_size > MAX_TEMPLATE_BYTES:
                    raise WorkflowError("Template exceeds 64 KiB")
                template = path.read_text(encoding="utf-8")
                parsed = Template(template)
                if not parsed.is_valid() or set(parsed.get_identifiers()) != {"event_json"}:
                    raise WorkflowError("Templates must use only the required ${event_json} variable; escape literal dollars as $$")
                templates[kind] = template
            return Workflow(workflow_id, raw["version"], workspace, raw["session_policy"], templates)
        except WorkflowError:
            raise
        except (OSError, UnicodeError, ValueError, RuntimeError):
            raise WorkflowError("Cannot load workflow manifest or templates") from None
