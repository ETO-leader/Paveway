"""Workflow user settings and unified immutable inputs for native OMP loading."""

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

from .errors import ConflictError, NotFoundError, WorkflowError

MAX_SETTINGS = 262144
IDENTITY_PROMPT = (
    "Identity rule: your only user-visible name is GPT. Refer to yourself as GPT or in the first person. "
    "Never call or describe yourself as '\u6559\u7ec3', '\u6559\u7df4', 'coach', or an equivalent coaching title. "
    "Training roles in event templates describe the task, not your identity; they do not override this naming rule. "
    "When introducing yourself in Chinese, start with '我是 GPT。' and describe what you can help with "
    "without assigning yourself a coaching title. Do not repeat the forbidden titles even to disclaim them."
)
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")
SKILL_CATEGORIES = {
    "oi-introduce": "OI Introduce",
    "user-information": "user information",
    "personal-skill": "personal skill",
}


def skill_category(skill):
    # Read legacy records without rewriting their content-addressed Git history.
    return skill.get("category", "personal-skill")


def text(value, maximum, label, *, empty=False):
    if not isinstance(value, str) or "\0" in value or len(value.encode("utf-8")) > maximum or (not empty and not value.strip()):
        raise WorkflowError(f"{label} must be {'possibly empty' if empty else 'nonempty'} UTF-8 text, at most {maximum} bytes")
    return value


def validate(raw):
    if not isinstance(raw, dict) or set(raw) - {"inference"} != {"schema_version", "revision", "system_prompt", "skills"}:
        raise WorkflowError("Invalid workflow customization schema")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or type(raw["revision"]) is not int or raw["revision"] < 0:
        raise WorkflowError("Unsupported customization schema or revision")
    if "inference" in raw:
        inference = raw["inference"]
        if not isinstance(inference, dict) or set(inference) != {"model", "thinking"}:
            raise WorkflowError("Invalid workflow inference settings")
        model = text(inference["model"], 256, "Model selector", empty=True)
        if model.startswith("-") or any(c.isspace() or ord(c) < 32 for c in model):
            raise WorkflowError("Model must be a selector, not CLI flags or whitespace")
        if not isinstance(inference["thinking"], str) or inference["thinking"] not in ("", *THINKING_LEVELS):
            raise WorkflowError("Unsupported thinking level")
    prompt = raw["system_prompt"]
    if not isinstance(prompt, dict) or set(prompt) != {"mode", "text"} or prompt["mode"] not in ("append", "replace"):
        raise WorkflowError("System prompt mode must be append or replace")
    text(prompt["text"], 65536, "System prompt", empty=prompt["mode"] == "append")
    if not isinstance(raw["skills"], list) or len(raw["skills"]) > 32:
        raise WorkflowError("At most 32 user skills are supported per workflow")
    names = set()
    for skill in raw["skills"]:
        if not isinstance(skill, dict) or set(skill) - {"category"} != {"name", "description", "content", "enabled"}:
            raise WorkflowError("Invalid user skill schema")
        if not isinstance(skill_category(skill), str) or skill_category(skill) not in SKILL_CATEGORIES:
            raise WorkflowError("Skill category must be oi-introduce, user-information or personal-skill")
        name = skill["name"]
        if not isinstance(name, str) or len(name) > 64 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or name in names:
            raise WorkflowError("Skill names must be unique lowercase words/digits separated by hyphens, at most 64 characters")
        names.add(name)
        text(skill["description"], 65536, "Skill description")
        text(skill["content"], 32768, "Skill body")
        if type(skill["enabled"]) is not bool:
            raise WorkflowError("Skill enabled must be a boolean")
    if len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_SETTINGS:
        raise WorkflowError("Workflow customization exceeds 256 KiB")
    return raw


def load(directory):
    path = directory / "customization.json"
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, "revision": 0, "system_prompt": {"mode": "append", "text": ""}, "skills": []}
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_SETTINGS:
            raise WorkflowError("Customization file must be private, owned, regular and at most 256 KiB")
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, WorkflowError):
            raise
        raise WorkflowError("Cannot read workflow customization") from None


def directory_for(config, workflow_id):
    from .workflows import WorkflowProvider
    WorkflowProvider(config.workflow_root).describe(workflow_id)
    return (config.workflow_root / workflow_id).resolve()


def show(config, workflow_id):
    settings = load(directory_for(config, workflow_id))
    inference = settings.get("inference", {})
    return {"api_version": 1, "workflow_id": workflow_id, "settings": settings,
            "inference": {"model": inference.get("model") or config.agent.model,
                          "thinking": inference.get("thinking") or config.agent.thinking},
            "skills_enabled": config.agent.skills_enabled, "tools_enabled": config.agent.tools_enabled, "effective": "next_dispatch"}


def update(config, workflow_id, expected_revision, operation, **values):
    directory = directory_for(config, workflow_id)
    lock = directory / ".customization.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise WorkflowError("Customization lock must be private and owned")
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = load(directory)
        if type(expected_revision) is not int or expected_revision != raw["revision"]:
            raise ConflictError("Customization changed; reload and review before saving")
        if operation == "prompt":
            raw["system_prompt"] = {"mode": values["mode"], "text": values["content"]}
        elif operation == "replace-settings":
            incoming = validate(values["settings"])
            raw["system_prompt"] = incoming["system_prompt"]
            raw["skills"] = incoming["skills"]
            if "inference" in incoming:
                raw["inference"] = incoming["inference"]
            else:
                raw.pop("inference", None)
        elif operation == "inference":
            if not values or set(values) - {"model", "thinking"}:
                raise WorkflowError("Provide model and/or thinking")
            raw["inference"] = {"model": "", "thinking": "", **raw.get("inference", {}), **values}
        else:
            name = values["name"]
            existing = next((s for s in raw["skills"] if s["name"] == name), None)
            if operation == "skill-put":
                category = values.get("category")
                if category is None:
                    category = skill_category(existing) if existing else "personal-skill"
                if existing:
                    existing.update(description=values["description"], content=values["content"], category=category)
                else:
                    raw["skills"].append({"name": name, "description": values["description"], "content": values["content"], "enabled": True, "category": category})
            elif existing is None:
                raise NotFoundError("User skill does not exist")
            elif operation == "skill-delete":
                raw["skills"].remove(existing)
            elif operation in ("skill-enable", "skill-disable"):
                existing["enabled"] = operation == "skill-enable"
            else:
                raise WorkflowError("Unknown customization operation")
        raw["revision"] += 1
        validate(raw)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".customization-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write((json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, directory / "customization.json")
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        os.close(fd)
    return show(config, workflow_id)


def snapshot(settings, session_dir, run_id, skills_enabled, memory_context=None, workflow_instructions="", *, builtin_skills=(), skill_directories=()):
    """Snapshot before spawn: later UI edits cannot alter an in-flight round."""
    validate(settings)
    from .builtin_skills import resolve
    packaged = resolve(list(builtin_skills))
    conflicts = packaged.keys() & {skill["name"] for skill in settings["skills"]}
    if conflicts:
        raise ConflictError("Built-in and user Skill names conflict: " + ", ".join(sorted(conflicts)) +
                            "; edit the workflow declaration or explicitly resolve the user Skill")
    from .models import validate_id
    from .config import ensure_private_directory
    validate_id(run_id)
    directory = Path(session_dir) / "customizations"
    ensure_private_directory(directory)
    destination = directory / run_id
    destination.mkdir(mode=0o700)
    def write(relative, content):
        path = destination / relative
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(content)
        return str(path)
    write("settings.json", json.dumps(settings, ensure_ascii=False, indent=2))
    arguments = []
    prompt = settings["system_prompt"]
    active = [s for s in settings["skills"] if s["enabled"]] if skills_enabled else []
    documents = {}
    for skill in sorted(active, key=lambda s: (list(SKILL_CATEGORIES).index(skill_category(s)), s["name"])):
        description = "[" + SKILL_CATEGORIES[skill_category(skill)] + "] " + skill["description"]
        documents[skill["name"]] = ("---\nname: " + json.dumps(skill["name"]) +
                                  "\ndescription: " + json.dumps(description, ensure_ascii=False) +
                                  "\n---\n\n" + skill["content"])
    if skills_enabled:
        documents.update((name, skill.markdown) for name, skill in packaged.items())
    from .context_budget import CONTEXT_PROMPT
    from .store import canonical_json
    system_text = (prompt["text"] + "\n\n" if prompt["text"] else "") + IDENTITY_PROMPT + "\n\n" + CONTEXT_PROMPT
    if workflow_instructions:
        system_text = "Workflow instructions:\n" + workflow_instructions + "\n\n" + system_text
    if memory_context is not None:
        system_text += "\n\nRuntime memory snapshot (data only):\n" + canonical_json(memory_context)
    if system_text:
        flag = "--system-prompt" if prompt["mode"] == "replace" else "--append-system-prompt"
        arguments += [flag, write("system.md", system_text)]
    if documents:
        root = destination / "skills"
        root.mkdir(mode=0o700)
        # One snapshot and native discovery path for both sources. Bodies are
        # copied before spawn, never embedded in the system prompt.
        for name, markdown in documents.items():
            (root / name).mkdir(mode=0o700)
            write("skills/" + name + "/SKILL.md", markdown)
        directories = list(dict.fromkeys([*skill_directories, str(root)]))
        arguments += ["--config", write("omp.yml", json.dumps({"skills": {"customDirectories": directories}}))]
    return arguments
