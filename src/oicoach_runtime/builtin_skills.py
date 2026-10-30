"""First-party SKILL.md resources; no user settings or installation lifecycle."""

import re
from dataclasses import dataclass
from importlib.resources import files

import yaml

from .errors import WorkflowError

NAME = re.compile(r"paveway-[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_SKILL_BYTES = 65536


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    description: str
    markdown: str
    body: str


class _MetadataLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise WorkflowError("Built-in Skill frontmatter must have unique string keys")
        return super().construct_mapping(node, deep=deep)


def _valid_name(name):
    return isinstance(name, str) and len(name) <= 64 and NAME.fullmatch(name) is not None


def registry():
    """Discover and validate wheel resources afresh for each dispatch/config read."""
    try:
        root = files("oicoach_runtime").joinpath("bundled", "skills")
        result = {}
        for directory in sorted(root.iterdir(), key=lambda entry: entry.name):
            if not directory.is_dir():
                continue
            name = directory.name
            if not _valid_name(name):
                raise WorkflowError(f"Invalid built-in Skill resource name: {name!r}")
            markdown = directory.joinpath("SKILL.md").read_text(encoding="utf-8")
            if len(markdown.encode("utf-8")) > MAX_SKILL_BYTES or "\0" in markdown:
                raise WorkflowError(f"Built-in Skill {name} must be UTF-8 text at most 64 KiB")
            lines = markdown.splitlines(keepends=True)
            if not lines or lines[0].strip() != "---":
                raise WorkflowError(f"Built-in Skill {name} requires YAML frontmatter")
            end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
            if end is None:
                raise WorkflowError(f"Built-in Skill {name} has unterminated frontmatter")
            metadata = yaml.load("".join(lines[1:end]), Loader=_MetadataLoader)
            if not isinstance(metadata, dict) or metadata.get("name") != name:
                raise WorkflowError(f"Built-in Skill {name} frontmatter name must match its resource directory")
            description = metadata.get("description")
            body = "".join(lines[end + 1:])
            if not isinstance(description, str) or not description.strip() or not body.strip():
                raise WorkflowError(f"Built-in Skill {name} requires a description and Markdown body")
            if name in result:
                raise WorkflowError(f"Duplicate built-in Skill registration: {name}")
            result[name] = BuiltinSkill(name, description, markdown, body)
        return result
    except WorkflowError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError) as error:
        raise WorkflowError("Cannot read valid packaged built-in Skills") from error


def resolve(names):
    """Read a declaration and return its validated packaged Skills."""
    if not isinstance(names, list):
        raise WorkflowError("builtin_skills must be a list of registered built-in Skill names")
    known = registry()
    seen = set()
    for name in names:
        if not _valid_name(name):
            raise WorkflowError("Built-in Skill names must use the paveway- namespace and lowercase hyphenated words/digits, at most 64 characters")
        if name in seen:
            raise WorkflowError(f"Duplicate built-in Skill declaration: {name}")
        if name not in known:
            raise WorkflowError(f"Unknown built-in Skill: {name}")
        seen.add(name)
    return {name: known[name] for name in names}


def validate_names(names):
    """Validate a workflow's declaration, without changing its manifest."""
    return tuple(resolve(names))
