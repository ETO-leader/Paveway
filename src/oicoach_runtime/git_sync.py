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

from . import customization
from .config import absolute_path, ensure_private_directory
from .errors import ConflictError, RuntimeContractError
from .models import validate_id

MAX_FILE = 32 * 1024 * 1024
FORMAT = {"format": "paveway-user-data", "version": 1}


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
    records = {}
    for path in sorted((repo / "profiles").glob("*/*.json")):
        record = read_json(path, maximum=524288)
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


def validate_tree(git, ref):
    listing = git.run("ls-tree", "-rlz", "--full-tree", ref).stdout
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        fields = meta.split()
        name = path.decode("utf-8")
        allowed = name == "format.json" or re.fullmatch(r"profiles/[A-Za-z0-9_.-]+/[0-9a-f]{64}\.json", name) or re.fullmatch(r"devices/[0-9a-f]{32}/(?:runs/[A-Za-z0-9-]+|sessions/[0-9a-f]{64})\.json", name)
        if fields[0] != b"100644" or not allowed or int(fields[3]) > MAX_FILE:
            raise RuntimeContractError("Remote tree contains unexpected paths, modes or oversized files; nothing was applied")


def pull(git, values):
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


def export_history(config, repo, device):
    if not config.database_path.exists():
        return 0
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
                continue
            try:
                archive = export_session(config, binding, device)
            except (OSError, RuntimeContractError):
                continue  # Busy/unsupported native archive never replaces an earlier valid export.
            # The export transaction may predate a newly leased turn. Recheck outside
            # that snapshot after reading the file so an in-flight prefix stays local.
            with sqlite3.connect(config.database_path.as_uri() + "?mode=ro", uri=True) as fresh:
                if fresh.execute("SELECT 1 FROM runs WHERE binding_id=? AND status IN ('leased','running')", (binding["binding_id"],)).fetchone():
                    continue
            write_json(repo / "devices" / device / "sessions" / (binding["binding_id"] + ".json"), archive)
        # Only read text from attested native sessions after the owner exits.
        # Never archive capability tokens, private launch JSON or raw tool logs.
        for row in db.execute("SELECT value FROM runtime_metadata WHERE key LIKE 'frontend:v1:%'"):
            record = json.loads(row[0])
            if record["status"] != "released":
                continue
            for source in record.get("session_files", []):
                binding = {**record, **source, "binding_id": hashlib.sha256(
                    canonical([record["conversation_id"], source["session_id"]])).hexdigest()}
                try:
                    archive = export_session(config, binding, device, root=Path(record["session_dir"]))
                except (OSError, RuntimeContractError):
                    continue
                with sqlite3.connect(config.database_path.as_uri() + "?mode=ro", uri=True) as fresh:
                    current = fresh.execute("SELECT value FROM runtime_metadata WHERE key=?",
                        ("frontend:v1:" + record["conversation_id"],)).fetchone()
                    if not current or json.loads(current[0])["status"] != "released" or json.loads(current[0])["generation"] != record["generation"]:
                        continue
                archive.update(source="omp_native_session", conversation_id=record["conversation_id"])
                write_json(repo / "devices" / device / "sessions" / (binding["binding_id"] + ".json"), archive)
    return len(rows)


def export_session(config, binding, device, *, root=None):
    path = Path(binding["session_file"])
    root = root or config.state_dir / "sessions" / binding["binding_id"]
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ConflictError("Native archive escaped its assigned task")
    messages = []
    header = False
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > 16 * 1024 * 1024:
            raise RuntimeContractError("Native archive must be owned and at most 16 MiB")
        while line := stream.readline(1048577):
            if len(line) > 1048576 or not line.endswith(b"\n"):
                raise ConflictError("Native archive is incomplete or changing")
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                raise RuntimeContractError("Native archive is not supported JSONL") from None
            if not isinstance(entry, dict):
                raise RuntimeContractError("Invalid native archive entry")
            if entry.get("type") == "session":
                if header or entry.get("id") != binding["session_id"] or entry.get("cwd") != binding["workspace"] or type(entry.get("version", 1)) is not int or entry.get("version", 1) not in (1, 2, 3):
                    raise RuntimeContractError("Native archive identity mismatch")
                header = True
                continue
            message = entry.get("message")
            if entry.get("type") != "message" or not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                continue
            if not header:
                raise RuntimeContractError("Native archive header is missing")
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
            if not isinstance(content, str) or not content:
                continue
            if any(value is not None and (not isinstance(value, str) or len(value) > 128) for value in (entry.get("id"), entry.get("parentId"))):
                raise RuntimeContractError("Invalid native message identity")
            messages.append({"role": message["role"], "text": content,
                             "id": entry.get("id"), "parent_id": entry.get("parentId")})
        after = os.fstat(stream.fileno())
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or not header:
            raise ConflictError("Native archive changed during export")
    return {"device_id": device, "binding_id": binding["binding_id"], "workflow_id": binding["workflow_id"],
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
        if path.is_file() and not (relative.as_posix() == "format.json" or
            re.fullmatch(r"profiles/[A-Za-z0-9_.-]+/[0-9a-f]{64}\.json", relative.as_posix()) or
            re.fullmatch(r"devices/[0-9a-f]{32}/(?:runs/[A-Za-z0-9-]+|sessions/[0-9a-f]{64})\.json", relative.as_posix())):
            raise ConflictError("Sync worktree contains unexpected files; only managed user data can be committed")
    if git.run("diff", "--name-only", "--diff-filter=D").stdout:
        raise ConflictError("Tracked sync data was deleted locally; restore or inspect it before automatic sync")
    git.run("add", "--all", "--", "format.json")
    for name in ("profiles", "devices"):
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
            count = export_history(config, repo, values["device_id"])
            write_json(root / "local.json", local)
            commit(git)
            pull(git, values)
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
                    pull(git, values)
                    reconcile_profiles(config, repo, values, local)
                    commit(git)
            result = {"state": "ok", "completed_at": stamp(), "exported_runs": count,
                      "remote_synced": bool(values["remote"]), "conflict_count": len(conflicts(config))}
        except Exception as error:
            result = {"state": "error", "completed_at": stamp(), "error": str(error) if isinstance(error, RuntimeContractError) else "Sync filesystem/database operation failed"}
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
