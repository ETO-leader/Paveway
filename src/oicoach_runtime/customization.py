"""Workflow-scoped user settings and immutable inputs for native OMP loading."""

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

from .errors import ConflictError, NotFoundError, WorkflowError

MAX_SETTINGS = 262144
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
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "system_prompt", "skills"}:
        raise WorkflowError("Invalid workflow customization schema")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or type(raw["revision"]) is not int or raw["revision"] < 0:
        raise WorkflowError("Unsupported customization schema or revision")
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
    return {"api_version": 1, "workflow_id": workflow_id, "settings": load(directory_for(config, workflow_id)),
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


def snapshot(settings, session_dir, run_id, skills_enabled):
    """Snapshot before spawn: later UI edits cannot alter an in-flight round."""
    validate(settings)
    if not settings["revision"]:
        return []
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
    system_text = prompt["text"]
    if active:
        sections = ["# User Skills · loaded together every turn"]
        for category, label in SKILL_CATEGORIES.items():
            group = sorted((s for s in active if skill_category(s) == category), key=lambda s: s["name"])
            if group:
                sections.append("## " + label)
                for skill in group:
                    sections.append("### " + skill["name"] + "\n" + skill["description"] + "\n\n" + skill["content"])
        system_text = (system_text + "\n\n" if system_text else "") + "\n\n".join(sections)
    if system_text:
        flag = "--system-prompt" if prompt["mode"] == "replace" else "--append-system-prompt"
        arguments += [flag, write("system.md", system_text)]
    if active:
        root = destination / "skills"
        root.mkdir(mode=0o700)
        for skill in active:
            (root / skill["name"]).mkdir(mode=0o700)
            write("skills/" + skill["name"] + "/SKILL.md", "---\nname: " + json.dumps(skill["name"]) +
                  "\ndescription: " + json.dumps(skill["description"], ensure_ascii=False) + "\n---\n\n" + skill["content"])
        arguments += ["--config", write("omp.yml", json.dumps({"skills": {"customDirectories": [str(root)]}}))]
    return arguments
