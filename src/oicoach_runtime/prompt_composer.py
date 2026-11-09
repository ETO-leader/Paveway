"""One immutable prompt source for native TUI and background RPC."""

import hashlib
from importlib.resources import files

from .store import canonical_json

VERSION = 6

DYNAMIC_TEMPLATE_SECTIONS = """{{#if skills.length}}
# Skills
Matching skill → MUST read `skill://<name>` first.

<skills>
{{#each skills}}
- {{name}}: {{description}}
{{/each}}
</skills>
{{/if}}

{{#if alwaysApplyRules.length}}
<generic-rules>
{{#each alwaysApplyRules}}
{{content}}
{{/each}}
</generic-rules>
{{/if}}

{{#if rules.length}}
<domain-rules>
{{#each rules}}
- {{name}}: {{description}}
{{/each}}
</domain-rules>
{{/if}}

{{#if internalUrls.length}}
# Internal URLs
Most FS/bash tools resolve these; path selectors: `read` docs.
{{#each internalUrls}}
- {{this}}
{{/each}}
{{/if}}

{{#if toolInfo.length}}
{{#if toolListMode}}
# Tool Inventory
{{#each toolInfo}}
- {{#if label}}{{label}}: `{{name}}`{{else}}`{{name}}`{{/if}}
{{/each}}
{{else}}
{{toolInventory}}
{{/if}}
{{/if}}

{{#if xdevTools.length}}
# xd:// Tool Devices

Write JSON args as `content` to `xd://<tool>` via `{{toolRefs.write}}`. Invalid args return schema in error → fix/retry.

{{xdevDocs}}
{{/if}}"""


def load_core() -> str:
    """Read Layer 1: Paveway Core from package resources."""
    return files("oicoach_runtime").joinpath("bundled", "core.md").read_text(encoding="utf-8").strip()


def load_role_policy(base: str | None) -> str:
    """Read Layer 2: Built-in role policy from package resources."""
    if base is None or base == "":
        return ""
    if base == "oi-coach":
        return files("oicoach_runtime").joinpath("bundled", "oi-coach.md").read_text(encoding="utf-8").strip()
    from .errors import WorkflowError
    raise WorkflowError(f"Unknown built-in role: {base}")

def settings_digest(settings):
    return hashlib.sha256(canonical_json(settings).encode()).hexdigest()


def persona_digest(settings):
    return hashlib.sha256(settings.get("persona", "").encode()).hexdigest()


def compose(settings, base=None, memory_context=None, *, role_policy=None):
    """Compose static settings only; memory_context is accepted but intentionally ignored."""
    core_text = load_core()
    role_text = role_policy if role_policy is not None else load_role_policy(base)
    sections = [core_text]
    if role_text:
        sections.append(role_text)
    user_prompt = settings.get("system_prompt", {}).get("text", "")
    if user_prompt and user_prompt.strip():
        sections.append(user_prompt.strip())
    persona = settings.get("persona", "")
    if persona and persona.strip():
        sections.append("Configured GPT Persona:\n" + persona.strip())
    return "\n\n".join(sections)

def escape_for_handlebars(text: str) -> str:
    """Escape double braces in static text so OMP's Handlebars compiler emits them literally."""
    return text.replace("{{", '{{default "{{" ""}}')


def compose_template(settings, base=None, memory_context=None, *, role_policy=None) -> str:
    """Append native OMP sections; memory_context is compatibility-only, never system data."""
    static_text = compose(settings, base=base, role_policy=role_policy)
    escaped = escape_for_handlebars(static_text)
    if escaped:
        return f"{escaped}\n\n{DYNAMIC_TEMPLATE_SECTIONS}\n"
    return f"{DYNAMIC_TEMPLATE_SECTIONS}\n"


def manifest(settings, system, base=None, template=None):
    template_content = template if template is not None else compose_template(settings, base=base)
    return {
        "composer_version": VERSION,
        "settings_revision": settings["revision"],
        "legacy_prompt_mode": settings["system_prompt"]["mode"],
        "base": base,
        "settings_digest": settings_digest(settings),
        "persona_digest": persona_digest(settings),
        "system_digest": hashlib.sha256(system.encode()).hexdigest(),
        "template_digest": hashlib.sha256(template_content.encode()).hexdigest(),
        "template_mode": "omp-handlebars",
        "context_policy": "omp-native",
        "memory_policy": "excluded_from_system",
        "activation": "immutable_launch_snapshot",
    }
