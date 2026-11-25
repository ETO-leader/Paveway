"""Independent, append-only Git user-data sync; never imports an execution queue."""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import customization, skill_packages
from .config import absolute_path, ensure_private_directory
from .errors import ConflictError, RuntimeContractError
from .models import validate_id

MAX_FILE = 32 * 1024 * 1024
FORMAT = {"format": "paveway-user-data", "version": 1}
PROFILE_PATH = re.compile(r"profiles/[A-Za-z0-9_.-]+/[0-9a-f]{64}\.json")
DEVICE_PATH = re.compile(r"devices/[0-9a-f]{32}/(?:runs/[A-Za-z0-9-]+|sessions/[0-9a-f]{64})\.json")
PACKAGE_MANIFEST = re.compile(r"packages/([0-9a-f]{64})/manifest\.json")
PACKAGE_RESOURCE = re.compile(r"packages/([0-9a-f]{64})/files/(.+)")
PACKAGE_MANIFEST_BYTES = 65536


def stamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path, default=None, maximum=MAX_FILE):
    if not path.exists() and not path.is_symlink():
        return default
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise RuntimeContractError("Sync data must be a bounded regular file, not a symlink")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeContractError("Invalid sync JSON; preserve the repository for inspection") from None


def write_json(path, value):
    data = canonical(value) + b"\n"
    if len(data) > MAX_FILE:
        raise RuntimeContractError("A sync record exceeds 32 MiB")
    ensure_private_directory(path.parent)
    if path.exists() and path.read_bytes() == data:
        return
    if path.is_symlink():
        raise ConflictError("Refusing to replace a symlink sync record")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def local_root(config):
    return config.state_dir / "git-sync"


@contextmanager
def locked(config):
    root = local_root(config)
    ensure_private_directory(root)
    fd = os.open(root / "sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConflictError("A Git sync operation is already running") from None
        yield root
    finally:
        os.close(fd)


class Git:
    def __init__(self, repository, budget=25):
        self.repository = Path(repository)
        self.deadline = time.monotonic() + budget

    def run(self, *args, allowed=(0,)):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeContractError("Git sync time budget exhausted; changes remain local for retry")
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no")
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=8")
        try:
            result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                "-c", "core.autocrlf=false", "-C", str(self.repository), *args], env=env,
                stdin=subprocess.DEVNULL, capture_output=True, timeout=min(10, remaining))
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeContractError("Git is unavailable or timed out; check connectivity and noninteractive Git authentication") from None
        if result.returncode not in allowed:
            # Git stderr can contain URLs, credentials or private paths. Do not publish it.
            raise RuntimeContractError("Git operation failed (" + args[0] + "); check remote access, authentication and repository state")
        return result


def validate_remote(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value) or value.startswith("-"):
        raise RuntimeContractError("Use a Git SSH/HTTPS address or an absolute local bare-repository path")
    if not value:
        return
    if Path(value).is_absolute():
        return
    if re.fullmatch(r"[\w.-]+@[\w.-]+:[^\s]+", value):
        return
    parsed = urlsplit(value)
    if parsed.scheme not in ("https", "ssh") or not parsed.hostname or parsed.password or parsed.query or parsed.fragment or (parsed.scheme == "https" and parsed.username):
        raise RuntimeContractError("Use SSH/HTTPS without embedded credentials, query tokens or fragments")


def settings(config):
    raw = read_json(local_root(config) / "settings.json", maximum=65536)
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"repository", "remote", "branch", "interval_seconds", "device_id", "enabled"} or type(raw["enabled"]) is not bool or type(raw["interval_seconds"]) is not int or not 10 <= raw["interval_seconds"] <= 86400:
        raise RuntimeContractError("Invalid local Git sync settings")
    absolute_path(raw["repository"], "sync.repository")
    validate_remote(raw["remote"])
    if not isinstance(raw["branch"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}", raw["branch"]) or not isinstance(raw["device_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", raw["device_id"]):
        raise RuntimeContractError("Invalid sync branch or device identity")
    return raw


def owned_repository(config, values):
    repo = absolute_path(values["repository"], "sync.repository")
    ensure_private_directory(repo)
    if (repo / ".git").is_symlink() or not (repo / ".git").is_dir():
        raise ConflictError("Sync requires its own initialized Git repository")
    marker = read_json(repo / ".git/paveway-sync.json", maximum=65536)
    if marker != {"format": FORMAT, "device_id": values["device_id"]}:
        raise ConflictError("Repository is not owned by this device's Paveway sync configuration")
    return repo


def configure(config, repository, remote="", branch="main", interval=60, *, _if_missing=False):
    validate_remote(remote)
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}", branch):
        raise RuntimeContractError("Invalid sync branch")
    if type(interval) is not int or not 10 <= interval <= 86400:
        raise RuntimeContractError("Sync interval must be 10 to 86400 seconds")
    repository = absolute_path(str(repository), "sync.repository")
    if config.state_dir.resolve().is_relative_to(repository.resolve()) or repository.resolve().is_relative_to(config.workflow_root.resolve()) or config.workflow_root.resolve().is_relative_to(repository.resolve()):
        raise ConflictError("The sync repository must be separate from live state/workflow roots")
    with locked(config) as root:
        previous = settings(config)
        if previous and _if_missing:
            return status(config)
        if previous and Path(previous["repository"]) != repository:
            raise ConflictError("Keep this device's existing sync repository; configure another runtime for a separate identity")
        device = previous["device_id"] if previous else uuid.uuid4().hex
        values = {"repository": str(repository), "remote": remote, "branch": branch,
                  "interval_seconds": interval, "device_id": device, "enabled": True}
        ensure_private_directory(repository)
        git = Git(repository)
        git.run("check-ref-format", "--branch", branch)
        if (repository / ".git").exists():
            owned_repository(config, values)
            if previous and branch != previous["branch"]:
                raise ConflictError("Changing a populated sync branch requires a separate runtime configuration")
        else:
            if list(repository.iterdir()):
                raise ConflictError("Use an empty directory dedicated to user-data sync, not the source repository")
            git.run("init", "--initial-branch=" + branch)
            (repository / ".git").chmod(0o700)
            write_json(repository / ".git/paveway-sync.json", {"format": FORMAT, "device_id": device})
            git.run("config", "user.name", "Paveway User Sync")
            git.run("config", "user.email", "paveway-sync@localhost")
        names = git.run("remote").stdout.decode().splitlines()
        if remote:
            git.run("remote", "set-url" if "origin" in names else "add", "origin", remote)
        elif "origin" in names:
            git.run("remote", "remove", "origin")
        write_json(root / "settings.json", values)
    return status(config)


def ensure_local(config):
    """Create one runtime-wide local archive without changing existing settings."""
    if settings(config) is not None:
        return
    configure(config, config.state_dir / "user-data-git", _if_missing=True)


def set_remote(config, remote):
    validate_remote(remote)
    ensure_local(config)
    with locked(config) as root:
        values = settings(config)
        git = Git(owned_repository(config, values))
        names = git.run("remote").stdout.decode().splitlines()
        if remote:
            git.run("remote", "set-url" if "origin" in names else "add", "origin", remote)
        elif "origin" in names:
            git.run("remote", "remove", "origin")
        values["remote"] = remote
        write_json(root / "settings.json", values)
    return status(config)


def status(config):
    values = settings(config)
    last = read_json(local_root(config) / "status.json", {})
    return {"api_version": 1, "configured": bool(values), "settings": values,
            "default_repository": str(config.state_dir / "user-data-git"),
            "last": last, "conflicts": conflicts(config) if values and last.get("state") != "syncing" else []}


def set_enabled(config, enabled):
    with locked(config) as root:
        values = settings(config)
        if not values:
            raise RuntimeContractError("Configure a sync repository first")
        values["enabled"] = enabled
        write_json(root / "settings.json", values)
    return status(config)


def profile_content(raw):
    return {key: raw[key] for key in ("system_prompt", "skills", "inference", "persona", "coach") if key in raw}


def read_profiles(repo):
    return validate_profiles((path, read_json(path, maximum=524288)) for path in sorted((repo / "profiles").glob("*/*.json")))


def validate_profiles(entries):
    records = {}
    for path, record in entries:
        if not isinstance(record, dict) or set(record) != {"workflow_id", "parents", "content", "device_id", "created_at"} or digest(record) != path.stem:
            raise RuntimeContractError("Invalid content-addressed profile record")
        validate_id(record["workflow_id"])
        if path.parent.name != record["workflow_id"] or not isinstance(record["parents"], list) or len(record["parents"]) > 128 or any(not isinstance(p, str) or not re.fullmatch(r"[0-9a-f]{64}", p) for p in record["parents"]):
            raise RuntimeContractError("Invalid profile identity or parents")
        if not isinstance(record["content"], dict) or set(record["content"]) - {"inference", "persona", "coach"} != {"system_prompt", "skills"}:
            raise RuntimeContractError("Invalid portable user configuration")
        if not isinstance(record["device_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", record["device_id"]) or not isinstance(record["created_at"], str) or len(record["created_at"]) > 64:
            raise RuntimeContractError("Invalid profile provenance")
        customization.validate({"schema_version": 1, "revision": 0, **record["content"]})
        records.setdefault(record["workflow_id"], {})[path.stem] = record
    for group in records.values():
        for key, record in group.items():
            if key in record["parents"] or any(parent not in group for parent in record["parents"]):
                raise RuntimeContractError("Profile ancestry is missing or invalid")
    return records


def heads(group):
    parents = {p for record in group.values() for p in record["parents"]}
    return sorted(set(group) - parents)


def add_profile(repo, workflow, content, parents, device):
    record = {"workflow_id": workflow, "content": content, "parents": sorted(parents), "device_id": device, "created_at": stamp()}
    key = digest(record)
    write_json(repo / "profiles" / workflow / (key + ".json"), record)
    return key, record


def reconcile_profiles(config, repo, values, local):
    records = read_profiles(repo)
    baselines = local.setdefault("baselines", {})
    for directory in sorted(config.workflow_root.iterdir()):
        if not directory.is_dir():
            continue
        workflow = directory.name
        report = customization.show(config, workflow)
        raw = report["settings"]
        content = profile_content(raw)
        content_hash = digest(content)
        baseline = baselines.get(workflow)
        group = records.setdefault(workflow, {})
        # A new device's untouched defaults do not compete with an existing profile.
        modified = (baseline and content_hash != baseline["content_hash"]) or (not baseline and raw["revision"] > 0)
        current = heads(group)
        if len(current) == 1 and digest(group[current[0]]["content"]) == content_hash:
            modified = False
        if modified:
            parents = [baseline["head"]] if baseline and baseline["head"] in group else []
            key, record = add_profile(repo, workflow, content, parents, values["device_id"])
            group[key] = record
            baselines[workflow] = {"head": key, "content_hash": content_hash}
        current = heads(group)
        if len(current) == 1:
            key = current[0]
            incoming = group[key]["content"]
            if digest(incoming) != content_hash:
                customization.update(config, workflow, raw["revision"], "replace-settings",
                    settings={"schema_version": 1, "revision": 0, **incoming})
            baselines[workflow] = {"head": key, "content_hash": digest(incoming)}
        write_json(local_root(config) / "local.json", local)
    return records


def conflicts(config):
    values = settings(config)
    if not values:
        return []
    repo = owned_repository(config, values)
    records = read_profiles(repo)
    return [{"workflow_id": workflow, "heads": [{"head": key, "device_id": group[key]["device_id"],
              "created_at": group[key]["created_at"], "prompt_preview": group[key]["content"]["system_prompt"]["text"][:160],
              "skill_names": [s["name"] for s in group[key]["content"]["skills"]]} for key in heads(group)],
              "needs_binding": not (config.workflow_root / workflow).is_dir()}
            for workflow, group in sorted(records.items()) if len(heads(group)) > 1 or not (config.workflow_root / workflow).is_dir()]


def resolve(config, workflow, head=None, keep_local=False):
    validate_id(workflow)
    with locked(config) as root:
        values = settings(config)
        if not values:
            raise RuntimeContractError("Sync is not configured")
        repo = owned_repository(config, values)
        group = read_profiles(repo).get(workflow, {})
        current = heads(group)
        if not current or (not keep_local and head not in current):
            raise ConflictError("Choose one of the current profile heads")
        local = read_json(root / "local.json", {"baselines": {}})
        raw = customization.show(config, workflow)["settings"]
        content = profile_content(raw) if keep_local else group[head]["content"]
        if profile_content(raw) != content:
            customization.update(config, workflow, raw["revision"], "replace-settings", settings={"schema_version": 1, "revision": 0, **content})
        key, _ = add_profile(repo, workflow, content, current, values["device_id"])
        local["baselines"][workflow] = {"head": key, "content_hash": digest(content)}
        write_json(root / "local.json", local)
    return {"api_version": 1, "resolved": workflow, "head": key, "next": "sync now"}


def safe_resource(path):
    """Accept one normalized relative Skill resource path from the remote tree."""
    try:
        skill_packages.safe_path(path)
    except RuntimeContractError:
        return False
    return True


def package_tree_path(name):
    """True only for a managed Skill package manifest or resource path."""
    if PACKAGE_MANIFEST.fullmatch(name):
        return True
    match = PACKAGE_RESOURCE.fullmatch(name)
    return bool(match) and safe_resource(match.group(2))


def read_blob(git, oid, maximum, *, value=False):
    data = git.run("cat-file", "blob", oid.decode()).stdout
    if len(data) > maximum:
        raise RuntimeContractError("Remote record exceeds its size limit; HEAD unchanged")
    if not value:
        return data
    try:
        return json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeContractError("Remote configuration contains invalid JSON; HEAD unchanged") from None


def manifest_entries(raw):
    """Validate one Skill package manifest read from the remote tree."""
    if (not isinstance(raw, dict) or set(raw) != {"format", "version", "files"} or
            raw["format"] != skill_packages.FORMAT or raw["version"] != skill_packages.VERSION or
            not isinstance(raw["files"], list) or not raw["files"] or len(raw["files"]) > skill_packages.MAX_FILES):
        raise RuntimeContractError("Remote Skill package manifest is invalid; HEAD unchanged")
    entries, folded, total = {}, set(), 0
    for item in raw["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise RuntimeContractError("Remote Skill package manifest has an invalid entry; HEAD unchanged")
        path = item["path"]
        if (not safe_resource(path) or not isinstance(item["sha256"], str) or
                not skill_packages.DIGEST.fullmatch(item["sha256"]) or type(item["bytes"]) is not int or
                not 0 <= item["bytes"] <= skill_packages.MAX_FILE_BYTES or path.casefold() in folded):
            raise RuntimeContractError("Remote Skill package manifest has an invalid entry; HEAD unchanged")
        folded.add(path.casefold())
        total += item["bytes"]
        entries[path] = item
    if total > skill_packages.MAX_PACKAGE_BYTES:
        raise RuntimeContractError("Remote Skill package exceeds 16 MiB; HEAD unchanged")
    return entries


def validate_tree(git, ref):
    listing = git.run("ls-tree", "-rlz", "--full-tree", ref).stdout
    entries, packages, totals = [], {}, {}
    rejected = "Remote tree contains unexpected paths, modes or oversized files; nothing was applied"
    for chunk in listing.split(b"\0"):
        if not chunk:
            continue
        try:
            meta, encoded = chunk.split(b"\t", 1)
            fields = meta.split()
            name = encoded.decode("utf-8")
            size = int(fields[3])
        except (UnicodeError, IndexError, ValueError):
            raise RuntimeContractError(rejected) from None
        manifest_match, resource_match = PACKAGE_MANIFEST.fullmatch(name), PACKAGE_RESOURCE.fullmatch(name)
        if name == "format.json":
            maximum = 65536
        elif PROFILE_PATH.fullmatch(name):
            maximum = 524288
        elif DEVICE_PATH.fullmatch(name):
            maximum = MAX_FILE
        elif manifest_match:
            maximum = PACKAGE_MANIFEST_BYTES
        elif resource_match:
            maximum = skill_packages.MAX_FILE_BYTES
        else:
            raise RuntimeContractError(rejected)
        if fields[0] != b"100644" or size > maximum:
            raise RuntimeContractError(rejected)
        if manifest_match:
            packages.setdefault(manifest_match.group(1), {"manifest": None, "files": {}})["manifest"] = (fields[2], size)
        elif resource_match:
            digest, relative = resource_match.group(1), resource_match.group(2)
            if not safe_resource(relative):
                raise RuntimeContractError("Remote tree contains an unsafe Skill resource path; nothing was applied")
            group = packages.setdefault(digest, {"manifest": None, "files": {}})
            totals[digest] = totals.get(digest, 0) + size
            if totals[digest] > skill_packages.MAX_PACKAGE_BYTES or len(group["files"]) >= skill_packages.MAX_FILES:
                raise RuntimeContractError("Remote Skill package exceeds its size limit; nothing was applied")
            group["files"][relative] = (fields[2], size)
        entries.append((name, fields[2], size))

    # Read blobs by object ID; nothing has been checked out or merged yet.
    candidates = []
    format_seen = False
    for name, oid, size in entries:
        if name != "format.json" and not PROFILE_PATH.fullmatch(name):
            continue
        maximum = 65536 if name == "format.json" else 524288
        if size > maximum:
            raise RuntimeContractError("Remote configuration record exceeds its size limit; HEAD unchanged")
        value = read_blob(git, oid, maximum, value=True)
        if name == "format.json":
            format_seen = True
            if not isinstance(value, dict) or value != FORMAT or type(value.get("version")) is not int:
                raise RuntimeContractError("Remote format.json is incompatible; expected paveway-user-data version 1; HEAD unchanged")
        else:
            candidates.append((Path(name), value))
    if not format_seen:
        raise RuntimeContractError("Remote format.json is missing; HEAD unchanged")
    try:
        records = validate_profiles(candidates)
    except RuntimeContractError as error:
        raise RuntimeContractError("Remote profile validation failed: " + str(error) + "; HEAD unchanged") from None
    validate_remote_packages(git, packages, records)


def validate_remote_packages(git, packages, records):
    """Every referenced package must be complete, and every possible Profile
    head must describe the package it references.

    Historical (non-head) records keep their content and hashes: they can never
    be applied again, so a stricter future contract cannot lock the repository.
    """
    referenced, applicable = {}, {}
    for group in records.values():
        current = set(heads(group))
        for key, record in group.items():
            for skill in record["content"]["skills"]:
                if "package" not in skill:
                    continue
                digest = skill["package"]["digest"]
                referenced.setdefault(digest, []).append(skill)
                if key in current:
                    applicable.setdefault(digest, []).append(skill)
    for digest in sorted(set(referenced) - packages.keys()):
        raise RuntimeContractError("Remote configuration references a missing Skill package; HEAD unchanged")
    verified = {}
    for digest, group in sorted(packages.items()):
        if group["manifest"] is None:
            raise RuntimeContractError("Remote Skill package is missing its manifest; HEAD unchanged")
        declared = manifest_entries(read_blob(git, group["manifest"][0], PACKAGE_MANIFEST_BYTES, value=True))
        if set(declared) != set(group["files"]):
            raise RuntimeContractError("Remote Skill package tree does not match its manifest; HEAD unchanged")
        for path, item in declared.items():
            if group["files"][path][1] != item["bytes"]:
                raise RuntimeContractError("Remote Skill package resource size does not match its manifest; HEAD unchanged")
        if digest not in referenced:
            continue
        files = {}
        for path, item in declared.items():
            payload = read_blob(git, group["files"][path][0], skill_packages.MAX_FILE_BYTES)
            if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                raise RuntimeContractError("Remote Skill package content does not match its digest; HEAD unchanged")
            files[path] = payload
        if skill_packages.digest(files) != digest:
            raise RuntimeContractError("Remote Skill package is not content-addressed correctly; HEAD unchanged")
        verified[digest] = ([declared[path] for path in sorted(declared)], files)
    # A correct content hash does not prove the Profile describes this package.
    for digest, skills in applicable.items():
        entries, files = verified[digest]
        for skill in skills:
            try:
                skill_packages.bind(skill, entries, files)
            except RuntimeContractError as error:
                raise RuntimeContractError(
                    "Remote Skill %r package binding is invalid (%s); HEAD unchanged"
                    % (skill.get("name"), error)) from None


def write_package_bytes(path, payload):
    ensure_private_directory(path.parent)
    if path.is_symlink():
        raise ConflictError("Refusing to replace a symlink Skill package resource")
    if path.exists():
        if path.read_bytes() == payload:
            return
        if not path.is_file():
            raise ConflictError("Sync repository contains an unexpected Skill package entry")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_package_directory(directory):
    """Read one immutable package tree from the sync worktree, never a symlink."""
    files_root = Path(directory) / "files"
    if files_root.is_symlink() or not files_root.is_dir():
        raise RuntimeContractError("Synced Skill package has no resource directory; nothing was applied")
    files, total = {}, 0
    for base, directories, names in os.walk(files_root, followlinks=False):
        for name in sorted(directories):
            if (Path(base) / name).is_symlink():
                raise RuntimeContractError("Synced Skill package contains a symlink; nothing was applied")
        for name in sorted(names):
            path = Path(base) / name
            relative = path.relative_to(files_root).as_posix()
            if path.is_symlink() or not path.is_file() or not safe_resource(relative):
                raise RuntimeContractError("Synced Skill package contains an unsafe resource; nothing was applied")
            payload = path.read_bytes()
            if len(payload) > skill_packages.MAX_FILE_BYTES:
                raise RuntimeContractError("Synced Skill package resource exceeds 4 MiB; nothing was applied")
            total += len(payload)
            if total > skill_packages.MAX_PACKAGE_BYTES or len(files) >= skill_packages.MAX_FILES:
                raise RuntimeContractError("Synced Skill package exceeds its size limit; nothing was applied")
            files[relative] = payload
    if not files:
        raise RuntimeContractError("Synced Skill package is empty; nothing was applied")
    return files


def export_packages(config, repo):
    """Copy every published or locally referenced package into the sync tree."""
    digests = set()
    for path in (Path(repo) / "profiles").glob("*/*.json"):
        record = read_json(path, maximum=524288)
        for skill in record.get("content", {}).get("skills", []) if isinstance(record, dict) else []:
            reference = skill.get("package") if isinstance(skill, dict) else None
            if isinstance(reference, dict) and isinstance(reference.get("digest"), str):
                digests.add(reference["digest"])
    for directory in sorted(Path(config.workflow_root).iterdir()):
        if not directory.is_dir():
            continue
        for skill in customization.load(directory)["skills"]:
            if "package" in skill:
                digests.add(skill["package"]["digest"])
    exported = {"packages": 0, "bytes": 0}
    for value in sorted(digests):
        files = skill_packages.read(config, value)
        target = Path(repo) / "packages" / value
        write_package_bytes(target / "manifest.json", canonical(skill_packages.manifest(files)) + b"\n")
        for path, payload in files.items():
            write_package_bytes(target / "files" / Path(path), payload)
        exported["packages"] += 1
        exported["bytes"] += sum(len(payload) for payload in files.values())
    return exported


def import_packages(config, repo):
    """Stage verified remote packages locally before any profile is applied."""
    directory = Path(repo) / "packages"
    if not directory.exists() and not directory.is_symlink():
        return {"packages": 0}
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeContractError("Synced Skill package root is unsafe; nothing was applied")
    imported = 0
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not skill_packages.DIGEST.fullmatch(entry.name):
            raise RuntimeContractError("Synced Skill package directory is invalid; nothing was applied")
        skill_packages.store(config, read_package_directory(entry), expected=entry.name)
        imported += 1
    return {"packages": imported}


def pull(config, git, values):
    if not values["remote"]:
        return
    git.run("fetch", "--no-tags", "origin")
    ref = "refs/remotes/origin/" + values["branch"]
    if git.run("show-ref", "--verify", "--quiet", ref, allowed=(0, 1)).returncode:
        return  # Empty remote; first push will create the selected branch.
    validate_tree(git, ref)
    if git.run("rev-parse", "--verify", "HEAD", allowed=(0, 128)).returncode:
        git.run("checkout", "-B", values["branch"], ref)
    else:
        git.run("merge", "--no-edit", "--allow-unrelated-histories", ref)
    # Stage packages before reconcile_profiles applies any remote profile.
    import_packages(config, Path(values["repository"]))


def export_history(config, repo, device):
    sessions = {"exported": 0, "skipped": 0, "deferred": 0, "attention": 0, "reasons": {}}
    report = {"exported_runs": 0, "session_archives": sessions}
    def skip(reason):
        sessions["skipped"] += 1
        sessions["deferred" if reason in ("active", "changed") else "attention"] += 1
        sessions["reasons"][reason] = sessions["reasons"].get(reason, 0) + 1
    def failed(error):
        reason = {"native_session_export_limit": "size_limit", "native_session_changed": "changed",
                  "native_session_unsafe": "unsafe", "native_session_entry_limit": "entry_limit"}.get(getattr(error, "code", None))
        skip(reason or ("missing" if isinstance(error, FileNotFoundError) else "unavailable" if isinstance(error, OSError) else "invalid_or_unsupported"))
    if not config.database_path.exists():
        return report
    with sqlite3.connect(config.database_path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = db.execute("""SELECT r.run_id,r.binding_id,r.status,r.started_at,r.completed_at,r.error_code,r.error_message,
            e.event_id,e.workflow_id,e.task_id,e.reason,e.payload_json,m.value AS output
            FROM runs r JOIN events e ON e.event_id=r.event_id LEFT JOIN runtime_metadata m ON m.key='output:' || r.run_id
            WHERE r.status IN ('succeeded','failed','blocked','uncertain')""").fetchall()
        for row in rows:
            item = dict(row)
            payload = json.loads(item.pop("payload_json"))
            encoded_output = item.pop("output")
            output = json.loads(encoded_output) if encoded_output else {}
            item.update(device_id=device, user_message=payload.get("message", item["reason"]),
                        reply=output.get("text"), truncated=output.get("truncated", False))
            write_json(repo / "devices" / device / "runs" / (item["run_id"] + ".json"), item)
        for binding in db.execute("SELECT * FROM session_bindings WHERE session_file IS NOT NULL"):
            if db.execute("SELECT 1 FROM runs WHERE binding_id=? AND status IN ('leased','running')", (binding["binding_id"],)).fetchone():
                skip("active")
                continue
            export_error = None
            try:
                archive = export_session(config, dict(binding), device)
            except (OSError, RuntimeContractError) as error:
                export_error = error
            # The export transaction may predate a newly leased turn. Recheck outside
            # that snapshot after reading the file so an in-flight prefix stays local.
            with sqlite3.connect(config.database_path.as_uri() + "?mode=ro", uri=True) as fresh:
                if fresh.execute("SELECT 1 FROM runs WHERE binding_id=? AND status IN ('leased','running')", (binding["binding_id"],)).fetchone():
                    skip("active")
                    continue
            if export_error:
                failed(export_error)
                continue  # Never replace an earlier valid export with a busy/unsupported archive.
            write_json(repo / "devices" / device / "sessions" / (binding["binding_id"] + ".json"), archive)
            sessions["exported"] += 1
        # Only read text from attested native sessions after the owner exits.
        # Never archive capability tokens, private launch JSON or raw tool logs.
        for row in db.execute("SELECT value FROM runtime_metadata WHERE key LIKE 'frontend:v1:%'"):
            record = json.loads(row[0])
            for source in record.get("session_files", []):
                if record["status"] != "released":
                    skip("active" if record["status"] in ("starting", "idle", "active") else "owner_unreleased")
                    continue
                binding = {**record, **source, "binding_id": hashlib.sha256(
                    canonical([record["conversation_id"], source["session_id"]])).hexdigest()}
                export_error = None
                try:
                    archive = export_session(config, binding, device, root=Path(record["session_dir"]))
                except (OSError, RuntimeContractError) as error:
                    export_error = error
                with sqlite3.connect(config.database_path.as_uri() + "?mode=ro", uri=True) as fresh:
                    current = fresh.execute("SELECT value FROM runtime_metadata WHERE key=?",
                        ("frontend:v1:" + record["conversation_id"],)).fetchone()
                    if not current or json.loads(current[0])["status"] != "released" or json.loads(current[0])["generation"] != record["generation"]:
                        skip("changed")
                        continue
                if export_error:
                    failed(export_error)
                    continue
                archive.update(source="omp_native_session", conversation_id=record["conversation_id"])
                write_json(repo / "devices" / device / "sessions" / (binding["binding_id"] + ".json"), archive)
                sessions["exported"] += 1
    report["exported_runs"] = len(rows)
    return report


def archive_timestamp(value):
    """Normalize source timestamps, never substitute export or file modification time."""
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
        elif type(value) in (int, float):
            parsed = datetime.fromtimestamp(value / 1000, timezone.utc)  # OMP message epoch milliseconds
        else:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _read_session(binding, root, consume, *, maximum=None, header_only=False):
    """Stream integrity validation independently of bounded text collection."""
    from .errors import NativeSessionError
    path = Path(binding["session_file"])
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise NativeSessionError("native_session_unsafe")
    header = title = False
    expected_cwd = binding.get("cwd", binding["workspace"])
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise NativeSessionError("native_session_unsafe")
        if maximum is not None and before.st_size > maximum:
            raise NativeSessionError("native_session_export_limit")
        while line := stream.readline(1048577):
            if len(line) > 1048576:
                raise NativeSessionError("native_session_entry_limit")
            if not line.endswith(b"\n"):
                raise NativeSessionError("native_session_incomplete")
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                raise NativeSessionError("native_session_invalid") from None
            if not isinstance(entry, dict):
                raise NativeSessionError("native_session_invalid")
            if entry.get("type") == "session":
                if header or entry.get("id") != binding["session_id"] or entry.get("cwd") != expected_cwd or type(entry.get("version", 1)) is not int or entry.get("version", 1) not in (1, 2, 3):
                    raise NativeSessionError("native_session_identity_mismatch")
                header = True
                if header_only:
                    break
            elif header_only:
                if not title and entry.get("type") == "title":
                    title = True
                    continue
                raise NativeSessionError("native_session_identity_mismatch")
            elif entry.get("type") == "message":
                if not header:
                    raise NativeSessionError("native_session_identity_mismatch")
                if any(value is not None and (not isinstance(value, str) or len(value) > 128)
                       for value in (entry.get("id"), entry.get("parentId"))):
                    raise NativeSessionError("native_session_invalid")
            consume(entry)
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        if not header:
            raise NativeSessionError("native_session_identity_mismatch")
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or
                (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
            raise NativeSessionError("native_session_changed")
    return before.st_size


def validate_session(config, binding, *, root=None):
    """Bounded-memory validation for release/resume; no aggregate export limit."""
    root = root or config.state_dir / "sessions" / binding["binding_id"]
    return _read_session(binding, root, lambda _entry: None)


def validate_session_header(binding, root):
    """Fence OMP resume before its native loader can adopt another project."""
    from .errors import NativeSessionError
    try:
        _read_session(binding, root, None, header_only=True)
    except FileNotFoundError:
        raise NativeSessionError("native_session_missing") from None


def conversation_message(entry):
    """Visible user/assistant text from one Session entry.

    Thinking blocks, tool arguments and tool results are never conversation
    text, even though they live in the same JSONL file.
    """
    if entry.get("type") != "message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
        return None
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    if not isinstance(content, str) or not content:
        return None
    return {"role": message["role"], "text": content,
            "id": entry.get("id"), "parent_id": entry.get("parentId"),
            "timestamp": archive_timestamp(entry.get("timestamp")) or archive_timestamp(message.get("timestamp"))}


def search_session(binding, root, query="", offset=0, limit=20, project=None):
    """Stream one local Session and return at most ``limit`` matches.

    The whole file is read once with the same integrity rules as the archive
    path but without the 16 MiB export budget. Only the requested page is
    retained; matches outside it are counted, never buffered.
    """
    needle = query.lower()
    state = {"matched": 0}
    page = []

    def consume(entry):
        message = conversation_message(entry)
        if message is None or needle not in message["text"].lower():
            return
        position = state["matched"]
        state["matched"] = position + 1
        if position < offset or len(page) >= limit:
            return
        page.append(project(message) if project else message)

    _read_session(binding, root, consume)
    return page, state["matched"]


def export_session(config, binding, device, *, root=None):
    root = root or config.state_dir / "sessions" / binding["binding_id"]
    messages = []
    created_at = None
    def collect(entry):
        nonlocal created_at
        if entry.get("type") == "session":
            created_at = archive_timestamp(entry.get("timestamp"))
            return
        message = conversation_message(entry)
        if message is not None:
            messages.append(message)
    _read_session(binding, root, collect, maximum=16 * 1024 * 1024)
    times = [m["timestamp"] for m in messages if m["timestamp"]]
    return {"device_id": device, "binding_id": binding["binding_id"], "workflow_id": binding["workflow_id"],
            "created_at": created_at or (min(times) if times else None),
            "updated_at": max(times) if times else created_at,
            "task_id": binding["task_id"], "archive_mode": "all_branches", "messages": messages}


def commit(git):
    if git.run("ls-files", "--unmerged").stdout:
        raise ConflictError("Git has unresolved conflicts; preserve both versions and resolve them before retrying")
    for path in git.repository.rglob("*"):
        relative = path.relative_to(git.repository)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise ConflictError("Sync worktree contains a symlink")
        name = relative.as_posix()
        if path.is_file() and not (name == "format.json" or PROFILE_PATH.fullmatch(name) or
                DEVICE_PATH.fullmatch(name) or package_tree_path(name)):
            raise ConflictError("Sync worktree contains unexpected files; only managed user data can be committed")
    if git.run("diff", "--name-only", "--diff-filter=D").stdout:
        raise ConflictError("Tracked sync data was deleted locally; restore or inspect it before automatic sync")
    git.run("add", "--all", "--", "format.json")
    for name in ("profiles", "devices", "packages"):
        if any((git.repository / name).rglob("*.json")):
            git.run("add", "--all", "--", name)
    if git.run("diff", "--cached", "--quiet", allowed=(0, 1)).returncode:
        git.run("commit", "-m", "Sync Paveway user configuration and history")


def cycle(config):
    with locked(config) as root:
        values = settings(config)
        if not values or not values["enabled"]:
            return {"state": "paused", "completed_at": stamp()}
        previous = read_json(root / "status.json", {})
        write_json(root / "status.json", {**previous, "state": "syncing", "started_at": stamp()})
        archive_report = None
        try:
            repo = owned_repository(config, values)
            git = Git(repo)
            if values["remote"] and git.run("config", "--get", "remote.origin.url").stdout.decode().strip() != values["remote"]:
                raise ConflictError("The managed origin changed; review and reconfigure it before syncing")
            local = read_json(root / "local.json", {"baselines": {}})
            # Complete any local export from a prior failed push before fetching.
            for name in ("profiles", "devices"):
                ensure_private_directory(repo / name)
            if not (repo / "format.json").exists():
                write_json(repo / "format.json", FORMAT)
            if read_json(repo / "format.json") != FORMAT:
                raise RuntimeContractError("Unsupported user-data repository format")
            validate_tree(git, "HEAD") if not git.run("rev-parse", "--verify", "HEAD", allowed=(0, 128)).returncode else None
            commit(git)
            # Offline work is committed locally even when the following fetch fails.
            reconcile_profiles(config, repo, values, local)
            package_report = export_packages(config, repo)
            archive_report = export_history(config, repo, values["device_id"])
            write_json(root / "local.json", local)
            commit(git)
            pull(config, git, values)
            if read_json(repo / "format.json") != FORMAT:
                raise RuntimeContractError("Unsupported user-data repository format")
            # Git checks out files with ordinary modes; keep the independent clone private.
            for path in repo.rglob("*"):
                if path.is_symlink():
                    raise RuntimeContractError("Sync repository contains an unexpected symlink")
                if ".git" not in path.relative_to(repo).parts:
                    path.chmod(0o700 if path.is_dir() else 0o600)
            reconcile_profiles(config, repo, values, local)
            write_json(root / "local.json", local)
            commit(git)
            if values["remote"]:
                for attempt in range(3):
                    result = git.run("push", "origin", "HEAD:refs/heads/" + values["branch"], allowed=(0, 1, 128))
                    if result.returncode == 0:
                        break
                    if attempt == 2:
                        raise RuntimeContractError("Remote moved or push was rejected; local commits are retained for retry")
                    pull(config, git, values)
                    reconcile_profiles(config, repo, values, local)
                    commit(git)
            result = {"state": "ok", "completed_at": stamp(), **archive_report,
                      "skill_packages": package_report,
                      "remote_synced": bool(values["remote"]), "conflict_count": len(conflicts(config))}
        except Exception as error:
            result = {"state": "error", "completed_at": stamp(), "error": str(error) if isinstance(error, RuntimeContractError) else "Sync filesystem/database operation failed"}
            if archive_report is not None:
                result.update(archive_report)
        write_json(root / "status.json", result)
        return result


def validate_history(item, device, run):
    if not isinstance(item, dict) or item.get("run_id") != run or item.get("device_id") != device:
        raise RuntimeContractError("Invalid synced history identity")
    for key in ("workflow_id", "task_id"):
        validate_id(item.get(key))
    if item.get("status") not in ("succeeded", "failed", "blocked", "uncertain") or not isinstance(item.get("started_at"), str) or len(item["started_at"]) > 64:
        raise RuntimeContractError("Invalid synced history status or timestamp")
    for key in ("reply", "error_code", "error_message"):
        if item.get(key) is not None and not isinstance(item[key], str):
            raise RuntimeContractError("Invalid synced history text")
    return item


def history(config, query="", workflow=None, offset=0):
    if not isinstance(query, str) or len(query) > 256 or type(offset) is not int or not 0 <= offset <= 1000000:
        raise RuntimeContractError("Invalid sync history query or offset")
    values = settings(config)
    if not values:
        raise RuntimeContractError("Sync is not configured")
    repo = owned_repository(config, values)
    rows = []
    for path in (repo / "devices").glob("*/runs/*.json"):
        item = validate_history(read_json(path, maximum=262144), path.parent.parent.name, path.stem)
        if (workflow is None or item.get("workflow_id") == workflow) and query.casefold() in canonical(item).decode().casefold():
            rows.append(item)
    rows.sort(key=lambda r: (r.get("started_at", ""), r["device_id"], r["run_id"]), reverse=True)
    summaries = []
    for row in rows[offset:offset + 20]:
        summary = {k: row[k] for k in ("device_id", "run_id", "workflow_id", "task_id", "status", "started_at")}
        summary["snippet"] = (str(row.get("user_message", "")) + "\n" + str(row.get("reply") or ""))[:400]
        summaries.append(summary)
    return {"api_version": 1, "records": summaries, "has_more": len(rows) > offset + 20, "offset": offset}


def history_show(config, device, run):
    if not re.fullmatch(r"[0-9a-f]{32}", device) or not re.fullmatch(r"[A-Za-z0-9-]+", run):
        raise RuntimeContractError("Invalid synced history identity")
    values = settings(config)
    if not values:
        raise RuntimeContractError("Sync is not configured")
    repo = owned_repository(config, values)
    item = read_json(repo / "devices" / device / "runs" / (run + ".json"), maximum=262144)
    validate_history(item, device, run)
    return {"api_version": 1, "record": item}


def messages(config, device, session, query="", offset=0):
    if not re.fullmatch(r"[0-9a-f]{32}", device) or not re.fullmatch(r"[0-9a-f]{64}", session) or type(offset) is not int or not 0 <= offset <= 1000000 or not isinstance(query, str) or len(query) > 256:
        raise RuntimeContractError("Invalid synced archive query")
    values = settings(config)
    if not values:
        raise RuntimeContractError("Sync is not configured")
    item = read_json(owned_repository(config, values) / "devices" / device / "sessions" / (session + ".json"))
    if not isinstance(item, dict) or item.get("device_id") != device or item.get("binding_id") != session or not isinstance(item.get("messages"), list):
        raise RuntimeContractError("Synced native text archive is missing or invalid")
    selected = []
    for message in item["messages"]:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant") or not isinstance(message.get("text"), str):
            raise RuntimeContractError("Invalid synced text message")
        if any(value is not None and (not isinstance(value, str) or len(value) > 128) for value in (message.get("id"), message.get("parent_id"))):
            raise RuntimeContractError("Invalid synced message identity")
        if query.casefold() in message["text"].casefold():
            selected.append(message)
    page = [{**m, "text": m["text"][:4096], "truncated": len(m["text"]) > 4096} for m in selected[offset:offset + 20]]
    return {"api_version": 1, "archive_mode": "all_branches", "messages": page,
            "has_more": len(selected) > offset + 20, "offset": offset}


def sessions(config, workflow=None, offset=0):
    """Discover both RPC and native-TUI text archives without importing sessions."""
    if workflow is not None:
        validate_id(workflow)
    if type(offset) is not int or not 0 <= offset <= 1000000:
        raise RuntimeContractError("Invalid archive offset")
    values = settings(config)
    if not values:
        raise RuntimeContractError("Sync is not configured")
    repository = owned_repository(config, values)
    rows = []
    for path in repository.glob("devices/*/sessions/*.json"):
        item = read_json(path)
        device, session = path.parent.parent.name, path.stem
        if not re.fullmatch(r"[0-9a-f]{32}", device) or not re.fullmatch(r"[0-9a-f]{64}", session) or \
                not isinstance(item, dict) or item.get("device_id") != device or item.get("binding_id") != session or not isinstance(item.get("messages"), list):
            raise RuntimeContractError("Invalid native text archive identity")
        validate_id(item.get("workflow_id"))
        validate_id(item.get("task_id"))
        if workflow is None or workflow == item["workflow_id"]:
            rows.append({"device_id": device, "session": session, "workflow_id": item["workflow_id"],
                "task_id": item["task_id"], "source": item.get("source", "omp_rpc"), "message_count": len(item["messages"]),
                "created_at": archive_timestamp(item.get("created_at")),
                "updated_at": archive_timestamp(item.get("updated_at"))})
    rows.sort(key=lambda row: (row["updated_at"] or row["created_at"] or "", row["session"]), reverse=True)
    return {"api_version": 1, "records": rows[offset:offset + 20], "has_more": len(rows) > offset + 20, "offset": offset}
