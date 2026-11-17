"""Immutable content-addressed storage for native OMP Skill directory trees.

Paveway installs, verifies, snapshots and synchronizes complete Skill
directories. Resource bytes and relative paths are preserved exactly; nothing
here interprets a Skill body, executes a packaged file or rewrites frontmatter.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath

import yaml

from .config import ensure_private_directory
from .errors import RuntimeContractError

FORMAT = "paveway-skill-package"
VERSION = 1

# One installed Skill directory tree.
MAX_FILES = 512
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
MAX_PATH = 1024
MAX_PART = 255
MAX_METADATA_BYTES = 65536

DIGEST = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def safe_path(value):
    """Accept one normalized POSIX relative file path; return its parts."""
    if (not isinstance(value, str) or not value or len(value) > MAX_PATH or "\0" in value or
            "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value)):
        raise RuntimeContractError("Skill package resource path is unsafe")
    parts = value.split("/")
    for part in parts:
        if (part in ("", ".", "..") or len(part) > MAX_PART or
                any(ord(character) < 32 or 127 <= ord(character) <= 159 or 0xD800 <= ord(character) <= 0xDFFF
                    for character in part)):
            raise RuntimeContractError("Skill package resource path is unsafe")
    return tuple(parts)


def normalize(files):
    """Validate one package tree and return it sorted by path."""
    if not isinstance(files, dict) or not files:
        raise RuntimeContractError("Skill package must contain at least one resource")
    if len(files) > MAX_FILES:
        raise RuntimeContractError("Skill package exceeds %d resources" % MAX_FILES)
    folded = set()
    total = 0
    for path, payload in files.items():
        safe_path(path)
        if not isinstance(payload, bytes) or len(payload) > MAX_FILE_BYTES:
            raise RuntimeContractError("Each Skill resource must be bytes of at most 4 MiB")
        collapsed = path.casefold()
        if collapsed in folded:
            raise RuntimeContractError("Skill package has duplicate or case-conflicting resource paths")
        folded.add(collapsed)
        total += len(payload)
    if total > MAX_PACKAGE_BYTES:
        raise RuntimeContractError("Skill package exceeds 16 MiB unpacked")
    for path in files:
        parts = path.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                raise RuntimeContractError("Skill package has a resource that is both a file and a directory")
    return {path: files[path] for path in sorted(files)}


def manifest(files):
    return {"format": FORMAT, "version": VERSION,
            "files": [{"path": path, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
                      for path, payload in files.items()]}


def digest(files):
    """Content digest of a normalized tree; independent of storage layout."""
    return hashlib.sha256(canonical(manifest(normalize(files)))).hexdigest()


def reference(files):
    """The bounded customization.json record referencing this exact tree."""
    files = normalize(files)
    return {"digest": digest(files), "files": len(files),
            "bytes": sum(len(payload) for payload in files.values())}


def metadata(markdown):
    """Read only the identifying frontmatter; never rewrite the document."""
    try:
        text = markdown.decode("utf-8-sig")
    except UnicodeError:
        raise RuntimeContractError("SKILL.md must be UTF-8 text") from None
    if "\0" in text:
        raise RuntimeContractError("SKILL.md must be UTF-8 text without NUL bytes")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise RuntimeContractError("SKILL.md requires YAML frontmatter")
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == "---"), None)
    if end is None:
        raise RuntimeContractError("SKILL.md has unterminated YAML frontmatter")
    try:
        raw = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError:
        raise RuntimeContractError("SKILL.md frontmatter is not valid YAML") from None
    if not isinstance(raw, dict):
        raise RuntimeContractError("SKILL.md frontmatter must be an object")
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise RuntimeContractError("SKILL.md name must be text")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip() or len(description.encode("utf-8")) > MAX_METADATA_BYTES:
        raise RuntimeContractError("SKILL.md requires a description of at most 64 KiB")
    return {"name": name, "description": description, "category": raw.get("category"), "enabled": raw.get("enabled")}


def root(value):
    """Resolve the store for runtime settings or an explicit store directory."""
    if hasattr(value, "state_dir"):
        return Path(value.state_dir) / "skill-packages"
    return Path(value)


def _private_root(config):
    directory = root(config)
    ensure_private_directory(directory)
    return directory


def _write_file(path, payload, *, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _write_tree(target, files):
    target.mkdir(mode=0o700)
    for path, payload in files.items():
        destination = target / path
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_file(destination, payload)
    os.chmod(target, 0o700)


def store(config, files, *, expected=None):
    """Install one immutable tree; identical content is stored only once."""
    files = normalize(files)
    value = digest(files)
    if expected is not None and value != expected:
        raise RuntimeContractError("Skill package content does not match its declared digest")
    directory = _private_root(config)
    target = directory / value
    if target.exists() or target.is_symlink():
        verify(config, value)
        return value
    temporary = Path(tempfile.mkdtemp(dir=directory, prefix=".new-"))
    try:
        os.chmod(temporary, 0o700)
        _write_tree(temporary / "files", files)
        _write_file(temporary / "manifest.json", canonical(manifest(files)) + b"\n")
        try:
            os.rename(temporary, target)
        except OSError:
            # Another writer won the race with the same content-addressed name.
            if not target.exists():
                raise
            verify(config, value)
        return value
    finally:
        if temporary.exists():
            import shutil
            shutil.rmtree(temporary, ignore_errors=True)


def verify(config, value):
    """Fail closed unless the stored tree is intact, private and byte-exact."""
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise RuntimeContractError("Skill package reference must be a lowercase SHA256 digest")
    directory = root(config)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeContractError("Skill package store is missing or unsafe")
    target = directory / value
    try:
        info = target.lstat()
    except OSError:
        raise RuntimeContractError("Skill package is missing from the local store") from None
    if target.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeContractError("Skill package store object is unsafe")
    manifest_path = target / "manifest.json"
    try:
        metadata_info = manifest_path.lstat()
        if (manifest_path.is_symlink() or not stat.S_ISREG(metadata_info.st_mode) or
                metadata_info.st_uid != os.getuid() or metadata_info.st_mode & 0o077 or
                metadata_info.st_size > MAX_METADATA_BYTES):
            raise RuntimeContractError("Skill package manifest is unsafe")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise RuntimeContractError("Skill package manifest is missing or invalid") from None
    if (not isinstance(raw, dict) or set(raw) != {"format", "version", "files"} or raw["format"] != FORMAT or
            raw["version"] != VERSION or not isinstance(raw["files"], list) or not raw["files"] or
            len(raw["files"]) > MAX_FILES):
        raise RuntimeContractError("Skill package manifest is not a supported package")
    entries, folded, total = {}, set(), 0
    for item in raw["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise RuntimeContractError("Skill package manifest has an invalid resource entry")
        path = item["path"]
        safe_path(path)
        if not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"]) or type(item["bytes"]) is not int:
            raise RuntimeContractError("Skill package manifest has an invalid resource entry")
        if item["bytes"] < 0 or item["bytes"] > MAX_FILE_BYTES or path.casefold() in folded:
            raise RuntimeContractError("Skill package manifest has an invalid resource entry")
        folded.add(path.casefold())
        total += item["bytes"]
        entries[path] = item
    if total > MAX_PACKAGE_BYTES:
        raise RuntimeContractError("Skill package store object exceeds 16 MiB")
    files_root = target / "files"
    try:
        files_info = files_root.lstat()
    except OSError:
        raise RuntimeContractError("Skill package store object has no resource tree") from None
    if files_root.is_symlink() or not stat.S_ISDIR(files_info.st_mode) or files_info.st_mode & 0o077:
        raise RuntimeContractError("Skill package store object is unsafe")
    stored = {}
    for path, item in entries.items():
        source = files_root.joinpath(*path.split("/"))
        try:
            info = source.lstat()
        except OSError:
            raise RuntimeContractError("Skill package resource is missing from the local store") from None
        if (source.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size != item["bytes"]):
            raise RuntimeContractError("Skill package resource is unsafe or has an unexpected size")
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise RuntimeContractError("Skill package resource hash does not match its manifest")
        stored[path] = payload
    if digest(stored) != value:
        raise RuntimeContractError("Skill package store object is not content-addressed correctly")
    return [dict(entries[path]) for path in sorted(entries)]


def read(config, value):
    """Return the verified resource tree for one package digest."""
    entries = verify(config, value)
    directory = root(config) / value / "files"
    return {item["path"]: directory.joinpath(*item["path"].split("/")).read_bytes() for item in entries}


def require(config, skills):
    """Fail closed when any referenced package is missing or damaged."""
    for skill in skills:
        reference_value = skill.get("package")
        if reference_value is None:
            continue
        try:
            verify(config, reference_value["digest"])
        except RuntimeContractError as error:
            raise RuntimeContractError(
                "Skill %r references an unavailable package (%s); configuration was not applied"
                % (skill.get("name"), str(error))) from None


def materialize(config, value, destination):
    """Copy one verified tree into a fresh snapshot directory; never execute it."""
    entries = verify(config, value)
    source_root = root(config) / value / "files"
    destination.mkdir(mode=0o700)
    for item in entries:
        relative = PurePosixPath(item["path"])
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = source_root.joinpath(*relative.parts).read_bytes()
        if len(payload) != item["bytes"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise RuntimeContractError("Skill package resource changed while creating the launch snapshot")
        _write_file(target, payload)
    return {"files": len(entries), "bytes": sum(item["bytes"] for item in entries)}


def external_skills(directories):
    """Names of Skills already installed in OMP's own configured directories.

    Only the directories reported by OMP itself are inspected, and only their
    ``*/SKILL.md`` frontmatter: Paveway never imports, copies or edits them.
    """
    found = {}
    if not isinstance(directories, (list, tuple)):
        return found
    for value in directories:
        if not isinstance(value, str) or not value:
            continue
        base = Path(value)
        try:
            entries = sorted(base.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries[:MAX_FILES]:
            path = entry / "SKILL.md"
            try:
                info = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
                    continue
                name = metadata(path.read_bytes())["name"] or entry.name
            except (OSError, RuntimeContractError, UnicodeError, ValueError, RecursionError):
                continue
            if isinstance(name, str) and NAME.fullmatch(name) and len(name) <= 64:
                found.setdefault(name, str(entry))
    return found
