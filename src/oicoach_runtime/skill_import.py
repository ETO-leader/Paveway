"""Install complete native OMP Skill directories from a user-selected file.

Two distinct operations share this module:

* :func:`read_source` collects UTF-8 text for the opt-in GPT analysis draft.
* :func:`preview`/:func:`install` install the package exactly as authored.

Nothing here executes packaged files, resolves references, installs
dependencies, requests the network, or rewrites ``SKILL.md``.
"""

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile
import zlib

from . import skill_packages
from .errors import ConflictError, RuntimeContractError

MAX_FILE = 1024 * 1024
MAX_SOURCE = 256 * 1024
MAX_ENTRIES = 64

# Package installation bounds are independent of the analysis text limits.
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2048
MAX_SKILLS = 32
MAX_COMPRESSION_RATIO = 200
MIN_RATIO_BYTES = 1024 * 1024
JUNK_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")


def _sanitize(value):
    if len(value) > 1 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if re.match(r"^[A-Za-z]:[\\/]", value) and Path("/mnt", value[0].lower()).is_dir():
        value = "/mnt/" + value[0].lower() + "/" + value[3:].replace("\\", "/")
    return Path(value).expanduser()


def _read_path(filename, maximum, label="Skill file"):
    path = _sanitize(str(filename).strip())
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise RuntimeContractError("Select a regular %s at most %d MiB" % (label, maximum // (1024 * 1024)))
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        if len(data) > maximum or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeContractError("%s changed while reading; select it again" % label.capitalize())
    except RuntimeContractError:
        raise
    except (OSError, ValueError):
        raise RuntimeContractError("Cannot read the selected Skill file; check its path and permissions") from None
    return path, data


def read_source(filename, *, allow_multiple=False):
    path, data = _read_path(filename, MAX_FILE)
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


def _junk(name):
    parts = name.split("/")
    return parts[0] == "__MACOSX" or parts[-1] in JUNK_NAMES


def _owner(path, roots):
    """Deepest Skill root containing ``path``; nested packages stay separate."""
    best = None
    for root in roots:
        prefix = root + "/" if root else ""
        if path.startswith(prefix) and (best is None or len(root) > len(best)):
            best = root
    return best


def _skill_name(metadata, root, filename):
    name = metadata.get("name")
    if name is None:
        name = Path(root).name if root else Path(filename).stem
    if not isinstance(name, str) or len(name) > 64 or not skill_packages.NAME.fullmatch(name):
        raise RuntimeContractError("Skill name must be lowercase words/digits separated by hyphens, at most 64 characters")
    return name


def _entry(root, metadata, name, files):
    """One installable package, without exposing its bytes to the caller."""
    files = skill_packages.normalize(files)
    category = metadata.get("category")
    enabled = metadata.get("enabled")
    return {"name": name, "description": metadata["description"],
            "category": category if isinstance(category, str) and category in ("oi-introduce", "user-information", "personal-skill") else "personal-skill",
            "enabled": enabled if type(enabled) is bool else True,
            "source": root or "SKILL.md", "digest": skill_packages.digest(files),
            "file_count": len(files), "bytes": sum(len(payload) for payload in files.values()),
            "resources": [{"path": path, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
                          for path, payload in files.items()],
            "files": files}


def _from_zip(data):
    files, roots = {}, set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise RuntimeContractError("Skill ZIP exceeds %d entries" % MAX_ARCHIVE_ENTRIES)
            names, folded, total = set(), set(), 0
            for entry in entries:
                name = entry.filename
                if _junk(name):
                    continue
                if entry.is_dir():
                    skill_packages.safe_path(name.rstrip("/"))
                    continue
                skill_packages.safe_path(name)
                kind = (entry.external_attr >> 16) & 0o170000
                if kind not in (0, stat.S_IFREG):
                    raise RuntimeContractError("Skill ZIP may only contain regular files")
                if entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise RuntimeContractError("Skill ZIP uses encryption or an unsupported compression method")
                if name in names or name.casefold() in folded:
                    raise RuntimeContractError("Skill ZIP has duplicate or case-conflicting resource paths")
                if entry.file_size > skill_packages.MAX_FILE_BYTES:
                    raise RuntimeContractError("A Skill resource must be at most 4 MiB")
                if entry.compress_size and entry.file_size > max(MIN_RATIO_BYTES, entry.compress_size * MAX_COMPRESSION_RATIO):
                    raise RuntimeContractError("Skill ZIP entry has an implausible compression ratio (possible bomb)")
                total += entry.file_size
                if total > MAX_BUNDLE_BYTES:
                    raise RuntimeContractError("Skill ZIP exceeds 32 MiB unpacked")
                with archive.open(entry) as stream:
                    payload = stream.read(skill_packages.MAX_FILE_BYTES + 1)
                if len(payload) != entry.file_size or len(payload) > skill_packages.MAX_FILE_BYTES:
                    raise RuntimeContractError("Skill ZIP resource size does not match its directory entry")
                names.add(name)
                folded.add(name.casefold())
                files[name] = payload
                if name.rsplit("/", 1)[-1] == "SKILL.md":
                    roots.add(name.rsplit("/", 1)[0] if "/" in name else "")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, zlib.error):
        raise RuntimeContractError("Cannot decode the Skill ZIP package") from None
    if not files:
        raise RuntimeContractError("Skill ZIP is empty")
    if not roots:
        raise RuntimeContractError("Skill ZIP must contain at least one SKILL.md")
    if len(roots) > MAX_SKILLS:
        raise RuntimeContractError("Skill ZIP contains more than %d Skills" % MAX_SKILLS)
    grouped = {root: {} for root in roots}
    for name in sorted(files):
        root = _owner(name, roots)
        if root is None:
            raise RuntimeContractError(
                "Skill ZIP entry %r is outside every Skill directory; its ownership is ambiguous"
                % name[:200])
        grouped[root][name[len(root) + 1:] if root else name] = files[name]
    result, seen = [], set()
    for root in sorted(roots):
        members = grouped[root]
        document = members.get("SKILL.md")
        if document is None:
            raise RuntimeContractError("Skill ZIP has files outside every Skill directory")
        metadata = skill_packages.metadata(document)
        name = _skill_name(metadata, root, "")
        if name in seen:
            raise RuntimeContractError("Skill names must be unique within one ZIP")
        seen.add(name)
        result.append(_entry(root, metadata, name, members))
    return result


def _from_markdown(filename, data):
    metadata = skill_packages.metadata(data)
    name = _skill_name(metadata, "", filename)
    return [_entry("", metadata, name, {"SKILL.md": data})]


def discover(filename):
    """Parse one user-selected Skill file or ZIP into complete package trees."""
    path, data = _read_path(filename, MAX_ARCHIVE_BYTES, "Skill package")
    skills = _from_zip(data) if zipfile.is_zipfile(io.BytesIO(data)) else _from_markdown(path.name, data)
    for skill in skills:
        if skill["file_count"] > skill_packages.MAX_FILES or skill["bytes"] > skill_packages.MAX_PACKAGE_BYTES:
            raise RuntimeContractError("Skill %r exceeds %d resources or 16 MiB" % (skill["name"], skill_packages.MAX_FILES))
    return {"path": str(path.absolute()), "filename": path.name, "sha256": hashlib.sha256(data).hexdigest(),
            "skills": skills}


def preview(config, workflow_id, filename):
    """Describe an import without writing anything; the source is re-read on install."""
    from . import customization
    settings = customization.show(config, workflow_id)["settings"]
    existing = {skill["name"]: skill for skill in settings["skills"]}
    discovered = discover(filename)
    skills = []
    for skill in discovered["skills"]:
        old = existing.get(skill["name"])
        skills.append({key: value for key, value in skill.items() if key != "files"} | {
            "enabled": old["enabled"] if old else skill["enabled"],
            "conflict": old is not None,
            "replaces": ("package" if "package" in old else "inline") if old else None})
    return {"api_version": 1, "revision": settings["revision"], "file": discovered["path"],
            "filename": discovered["filename"], "sha256": discovered["sha256"], "mode": "package",
            "skill_limit": customization.MAX_SKILLS,
            "exceeds_limit": len(existing) + sum(not skill["conflict"] for skill in skills) > customization.MAX_SKILLS,
            "skills": skills}


def install(config, workflow_id, expected_revision, batch):
    """Atomically install the selected packages, then commit their references."""
    from . import customization
    if not isinstance(batch, dict) or set(batch) != {"file", "sha256", "skills", "replace"}:
        raise RuntimeContractError("Skill import requires file, sha256, skills and replace")
    if (not isinstance(batch["file"], str) or not isinstance(batch["sha256"], str) or
            not re.fullmatch(r"[0-9a-f]{64}", batch["sha256"]) or
            not isinstance(batch["skills"], list) or not batch["skills"] or
            not isinstance(batch["replace"], list) or
            any(not isinstance(name, str) for name in batch["replace"])):
        raise RuntimeContractError("Skill import batch is invalid")
    discovered = discover(batch["file"])
    if discovered["sha256"] != batch["sha256"]:
        raise ConflictError("Skill package changed after preview; preview it again before installing")
    packages = {skill["name"]: skill for skill in discovered["skills"]}
    settings = customization.show(config, workflow_id)["settings"]
    existing = {skill["name"]: skill for skill in settings["skills"]}
    records, selected = [], set()
    for item in batch["skills"]:
        if not isinstance(item, dict) or set(item) - {"name", "category", "enabled"} or "name" not in item:
            raise RuntimeContractError("Each selected Skill requires a name and optional category/enabled")
        name = item["name"]
        if not isinstance(name, str) or name not in packages or name in selected:
            raise RuntimeContractError("Selected Skills must be existing, unique names from this package")
        selected.add(name)
        package = packages[name]
        old = existing.get(name)
        enabled = item.get("enabled", old["enabled"] if old else package["enabled"])
        if type(enabled) is not bool:
            raise RuntimeContractError("Skill enabled must be a boolean")
        category = item.get("category") or (customization.skill_category(old) if old else package["category"])
        records.append({"name": name, "description": package["description"], "enabled": enabled,
                        "category": category, "package": skill_packages.reference(package["files"])})
    conflicts = sorted(selected & existing.keys())
    if sorted(set(batch["replace"])) != conflicts:
        raise ConflictError("Every conflicting Skill requires explicit replacement confirmation")
    if len(existing) + len(selected - existing.keys()) > customization.MAX_SKILLS:
        raise RuntimeContractError("At most %d user skills are supported per workflow" % customization.MAX_SKILLS)
    # Immutable objects land first; an interrupted install leaves only garbage,
    # never a configuration that references a missing tree.
    for name in sorted(selected):
        skill_packages.store(config, packages[name]["files"])
    report = customization.update(config, workflow_id, expected_revision, "skills-import",
                                  skills=records, replace=conflicts)
    report["installed"] = [{"name": record["name"], "digest": record["package"]["digest"],
                            "files": record["package"]["files"], "bytes": record["package"]["bytes"],
                            "replaced": "package" if "package" in existing.get(record["name"], {})
                                        else "inline" if record["name"] in existing else None}
                           for record in records]
    return report
