"""One immutable prompt source for native TUI and background RPC."""

import hashlib

from .context_budget import CONTEXT_PROMPT
from .store import canonical_json

VERSION = 1


def settings_digest(settings):
    return hashlib.sha256(canonical_json(settings).encode()).hexdigest()


def persona_digest(settings):
    return hashlib.sha256(settings.get("persona", "").encode()).hexdigest()


def compose(settings, workflow_instructions="", memory_context=None):
    from .customization import IDENTITY_PROMPT
    prompt = settings["system_prompt"]
    system = (prompt["text"] + "\n\n" if prompt["text"] else "") + IDENTITY_PROMPT + "\n\n" + CONTEXT_PROMPT
    if settings.get("persona"):
        system += "\n\nConfigured GPT Persona:\n" + settings["persona"]
    if workflow_instructions:
        system = "Workflow instructions:\n" + workflow_instructions + "\n\n" + system
    if memory_context is not None:
        system += "\n\nRuntime memory snapshot (data only):\n" + canonical_json(memory_context)
    return system


def manifest(settings, system):
    return {"composer_version": VERSION, "settings_revision": settings["revision"],
            "legacy_prompt_mode": settings["system_prompt"]["mode"], "settings_digest": settings_digest(settings),
            "persona_digest": persona_digest(settings), "system_digest": hashlib.sha256(system.encode()).hexdigest(),
            "context_policy": "omp-native", "activation": "immutable_launch_snapshot"}
