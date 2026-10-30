"""GPT profiles map reusable workflow templates to isolated private runtimes."""

import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from . import customization
from .config import ensure_private_directory
from .errors import ConflictError, NotFoundError
from .models import validate_id
from .workflows import WorkflowProvider


def profiles(config):
    result = []
    for directory in sorted(config.workflow_root.iterdir()):
        if directory.name.startswith(".") or not (directory / "workflow.toml").is_file():
            continue
        workflow = WorkflowProvider(config.workflow_root).describe(directory.name)
        binding = workflow.customization.get("coach", {"id": directory.name, "workflow_source": directory.name})
        result.append({"coach_id": binding["id"], "workflow_source": binding["workflow_source"],
            "runtime_workflow_id": directory.name, "revision": workflow.customization["revision"],
            "has_persona": bool(workflow.customization.get("persona")), "activation": "new_conversation"})
    return result


def resolve(config, coach_id):
    validate_id(coach_id)
    matches = [row for row in profiles(config) if row["coach_id"] == coach_id]
    if not matches:
        raise NotFoundError("GPT profile does not exist")
    if len(matches) != 1:
        raise ConflictError("GPT profile ID is ambiguous; inspect private bindings")
    return matches[0]


def create(config, coach_id, workflow_id, persona=""):
    validate_id(coach_id)
    if any(row["coach_id"] == coach_id for row in profiles(config)):
        raise ConflictError("GPT profile already exists")
    source = WorkflowProvider(config.workflow_root).describe(workflow_id)
    alias = "gpt-" + hashlib.sha256(coach_id.encode()).hexdigest()[:32]
    target = config.workflow_root / alias
    if target.exists():
        raise ConflictError("Private GPT runtime already exists")
    ensure_private_directory(config.workflow_root)
    temporary = Path(tempfile.mkdtemp(prefix=".profile-", dir=config.workflow_root))
    try:
        import tomllib
        import json
        manifest = tomllib.loads((config.workflow_root / workflow_id / "workflow.toml").read_text())
        manifest["id"] = alias
        for kind in ("start", "wake", "recovery"):
            manifest[kind + "_prompt"] = kind + ".md"
            path = temporary / (kind + ".md")
            path.write_text(source.templates[kind], encoding="utf-8")
            path.chmod(0o600)
        lines = [key + " = " + json.dumps(value, ensure_ascii=False) for key, value in manifest.items()]
        (temporary / "workflow.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (temporary / "workflow.toml").chmod(0o600)
        settings = {"schema_version": 1, "revision": 1, "system_prompt": {"mode": "append", "text": ""},
            "skills": [], "persona": persona, "coach": {"id": coach_id, "workflow_source": workflow_id}}
        customization.validate(settings)
        (temporary / "customization.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (temporary / "customization.json").chmod(0o600)
        # Do not implicitly share another profile's Skills, prompt or memory.
        os.rename(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return resolve(config, coach_id)


def state(store, coach_id, key, *, value=None, expected_version=None):
    """Generic interaction state uses existing scoped fact/CAS storage.

    No emotion policy or automatic Persona rewrite is inferred here.
    """
    from .memory import Memory
    from .store import canonical_json
    from .errors import RuntimeContractError
    validate_id(key)
    if len(key) > 64:
        raise RuntimeContractError("GPT state keys are at most 64 characters")
    profile = resolve(store.config, coach_id)
    memory = Memory(store)
    workflow_id = profile["runtime_workflow_id"]
    if expected_version is None:
        return memory.show(workflow_id, "state-" + key)
    return memory.put(workflow_id, "state-" + key, "GPT state: " + key, canonical_json(value),
                      expected_version=expected_version, pinned=True, kind="fact")
