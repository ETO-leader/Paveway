"""Read an explicitly selected Skill file as data; never extract or execute it."""

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile
import zlib

from .errors import RuntimeContractError

MAX_FILE = 1024 * 1024
MAX_SOURCE = 256 * 1024
MAX_ENTRIES = 64


def read_source(filename, *, allow_multiple=False):
    value = str(filename).strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if re.match(r"^[A-Za-z]:[\\/]", value) and Path("/mnt", value[0].lower()).is_dir():
        value = "/mnt/" + value[0].lower() + "/" + value[3:].replace("\\", "/")
    path = Path(value).expanduser()
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
                raise RuntimeContractError("Select a regular Skill file at most 1 MiB")
            data = stream.read(MAX_FILE + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_FILE or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeContractError("Skill file changed while reading; select it again")
    except RuntimeContractError:
        raise
    except (OSError, ValueError):
        raise RuntimeContractError("Cannot read the selected Skill file; check its path and permissions") from None
    documents, omitted = [], []
    total = 0
    def document(name, payload):
        nonlocal total
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeError:
            return False
        if "\0" in text:
            return False
        total += len(text.encode("utf-8"))
        if total > MAX_SOURCE:
            raise RuntimeContractError("Skill text exceeds 256 KiB; split the source before analysis")
        documents.append({"path": name, "text": text})
        return True
    if zipfile.is_zipfile(io.BytesIO(data)):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_SOURCE:
                    raise RuntimeContractError("Skill package exceeds 64 entries or 256 KiB unpacked")
                names = set()
                for entry in entries:
                    name = entry.filename
                    relative = PurePosixPath(name)
                    if (relative.is_absolute() or ".." in relative.parts or "\\" in name or
                            len(name) > 512 or any(ord(c) < 32 for c in name) or
                            stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1 or name in names):
                        raise RuntimeContractError("Skill package has unsafe, duplicate, linked or encrypted entries")
                    names.add(name)
                    if entry.is_dir():
                        continue
                    with archive.open(entry) as stream:
                        payload = stream.read(MAX_SOURCE + 1)
                    if len(payload) > MAX_SOURCE:
                        raise RuntimeContractError("Skill package entry exceeds the source limit")
                    if not document(name, payload):
                        omitted.append(name)
                count = sum(PurePosixPath(item["path"]).name.upper() == "SKILL.MD" for item in documents)
                if count < 1 or (not allow_multiple and count != 1):
                    raise RuntimeContractError("Skill ZIP must contain exactly one UTF-8 SKILL.md; import separate Skills individually")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, zlib.error):
            raise RuntimeContractError("Cannot decode the Skill ZIP package") from None
    elif not document(path.name, data):
        raise RuntimeContractError("Skill source must be UTF-8 text or a Skill ZIP package")
    if not documents or not any(item["text"].strip() for item in documents):
        raise RuntimeContractError("Skill source is empty")
    return {"filename": path.name, "sha256": hashlib.sha256(data).hexdigest(),
            "documents": documents, "omitted_binary_resources": omitted, "text_bytes": total}


def local_skills(filename, settings):
    """Parse standard frontmatter; body characters and line endings stay exact."""
    import yaml
    from .customization import validate
    source = read_source(filename, allow_multiple=True)
    is_package = len(source["documents"]) != 1 or any(PurePosixPath(d["path"]).name.upper() == "SKILL.MD" for d in source["documents"])
    documents = [d for d in source["documents"] if PurePosixPath(d["path"]).name.upper() == "SKILL.MD"] if is_package else source["documents"]
    existing = {s["name"]: s for s in settings["skills"]}
    records, seen = [], set()
    for document in documents:
        lines = document["text"].splitlines(keepends=True)
        closing = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
        if not lines or lines[0].rstrip("\r\n") != "---" or closing is None:
            raise RuntimeContractError("Local import requires YAML frontmatter with name and description")
        try: metadata = yaml.safe_load("".join(lines[1:closing]))
        except yaml.YAMLError:
            raise RuntimeContractError("Invalid Skill YAML frontmatter") from None
        if not isinstance(metadata, dict): raise RuntimeContractError("Skill frontmatter must be an object")
        name = metadata.get("name")
        if not isinstance(name, str) or name in seen:
            raise RuntimeContractError("Skill names must be present and unique within the import")
        seen.add(name)
        old = existing.get(name)
        skill = {"name": name, "description": metadata.get("description"), "content": "".join(lines[closing + 1:]),
            "category": metadata.get("category", "personal-skill"), "enabled": old["enabled"] if old else metadata.get("enabled", True)}
        validate({"schema_version": 1, "revision": 0, "system_prompt": {"mode": "append", "text": ""}, "skills": [skill]})
        records.append({**skill, "source": document["path"], "conflict": old is not None})
    return {"api_version": 1, "revision": settings["revision"], "skills": records,
        "resources_not_installed": [d["path"] for d in source["documents"] if d not in documents] + source["omitted_binary_resources"]}
