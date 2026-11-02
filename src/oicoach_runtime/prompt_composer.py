"""One immutable prompt source for native TUI and background RPC."""

import hashlib
from importlib.resources import files

from .store import canonical_json

VERSION = 4


def load_core() -> str:
    """Read Layer 1: Paveway Core from package resources."""
    return files("oicoach_runtime").joinpath("bundled", "core.md").read_text(encoding="utf-8").strip()


def load_role_policy(base: str | None) -> str:
    """Read Layer 2: Built-in role policy from package resources."""
    if not base:
        return ""
    if base in ("oi-coach", "coach"):
        for filename in ("oi-coach.md", "coach.md"):
            try:
                return files("oicoach_runtime").joinpath("bundled", filename).read_text(encoding="utf-8").strip()
            except (FileNotFoundError, TypeError):
                continue
    from .errors import WorkflowError
    raise WorkflowError(f"Unknown built-in role: {base}")


def settings_digest(settings):
    return hashlib.sha256(canonical_json(settings).encode()).hexdigest()


def persona_digest(settings):
    return hashlib.sha256(settings.get("persona", "").encode()).hexdigest()


def compose(settings, base=None, memory_context=None, *, role_policy=None, workflow_instructions=""):
    core_text = load_core()
    role_text = role_policy if role_policy is not None else (load_role_policy(base) if base in ("oi-coach", "coach") else "")
    sections = [core_text]
    if role_text:
        sections.append(role_text)
    user_prompt = settings.get("system_prompt", {}).get("text", "")
    if user_prompt and user_prompt.strip():
        sections.append(user_prompt.strip())
    persona = settings.get("persona", "")
    if persona and persona.strip():
        sections.append("Configured GPT Persona:\n" + persona.strip())
    if memory_context is not None:
        sections.append("Runtime memory snapshot (data only):\n" + canonical_json(memory_context))
    return "\n\n".join(sections)


def manifest(settings, system, base=None):
    return {
        "composer_version": VERSION,
        "settings_revision": settings["revision"],
        "legacy_prompt_mode": settings["system_prompt"]["mode"],
        "base": base,
        "settings_digest": settings_digest(settings),
        "persona_digest": persona_digest(settings),
        "system_digest": hashlib.sha256(system.encode()).hexdigest(),
        "context_policy": "omp-native",
        "activation": "immutable_launch_snapshot",
    }
