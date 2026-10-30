"""Versioned frontend ownership records in the existing transactional store.

Native frontends never acquire a background Run slot or share its session.
Heartbeats are observations, not death, completion, or release evidence.
"""

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import uuid

from .errors import ConflictError, NotFoundError, RuntimeContractError
from .models import validate_id
from .processes import InstanceLock, process_identity
from .store import canonical_json

PREFIX = "frontend:v1:"


def alive(pid, identity):
    if not pid or not identity or process_identity(pid) != identity:
        return False
    try:
        # A zombie has exited and holds no file descriptors; a stale PID alone
        # must not prevent explicit recovery after its lock has been released.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except OSError:
        return False


class Frontend:
    def __init__(self, store):
        self.store = store

    def records(self, workflow_id=None, task_id=None):
        if workflow_id is not None:
            validate_id(workflow_id)
        if task_id is not None:
            validate_id(task_id)
        rows = self.store.connection.execute("SELECT value FROM runtime_metadata WHERE key LIKE ?", (PREFIX + "%",)).fetchall()
        import json
        records = [json.loads(row[0]) for row in rows]
        return sorted((row for row in records if (workflow_id is None or row["workflow_id"] == workflow_id)
                       and (task_id is None or row["task_id"] == task_id)), key=lambda row: row["updated_at"], reverse=True)

    @staticmethod
    def public(record):
        return {**{key: value for key, value in record.items() if key not in ("token_hash", "session_files")},
                "session_count": len(record.get("session_files", []))}

    def get(self, conversation_id):
        validate_id(conversation_id)
        record = self.store.metadata(PREFIX + conversation_id)
        if record is None:
            raise NotFoundError("Native conversation does not exist")
        return record

    def save(self, db, record):
        record["updated_at"] = self.store.timestamp()
        self.store._set_metadata(db, PREFIX + record["conversation_id"], record)

    def authenticate(self, conversation_id, generation, owner_token, *, uncertain=False):
        record = self.get(conversation_id)
        if not isinstance(owner_token, str) or len(owner_token) != 64 or type(generation) is not int or \
                not hmac.compare_digest(record["token_hash"], hashlib.sha256(owner_token.encode()).hexdigest()) or record["generation"] != generation:
            raise ConflictError("Native owner generation changed; stale operation refused")
        if record["status"] == "released" or (record["status"] == "uncertain" and not uncertain):
            raise ConflictError("Native session is released or uncertain; no automatic replay")
        return record

    def begin(self, workflow_id, task_id, workspace, conversation_id=None, new=False, settings=None,
              workflow_revision="", workflow_instructions=""):
        validate_id(workflow_id)
        validate_id(task_id)
        if type(new) is not bool or (new and conversation_id):
            raise RuntimeContractError("Choose a conversation or create a new one")
        if not conversation_id and not new:
            previous = self.records(workflow_id, task_id)
            conversation_id = previous[0]["conversation_id"] if previous else None
        conversation_id = conversation_id or str(uuid.uuid4())
        validate_id(conversation_id)
        owner_token = secrets.token_hex(32)
        from .prompt_composer import settings_digest, persona_digest
        settings = settings or {"schema_version": 1, "revision": 0, "system_prompt": {"mode": "append", "text": ""}, "skills": []}
        with self.store.transaction() as db:
            old = self.store.metadata(PREFIX + conversation_id)
            if old and (old["workflow_id"] != workflow_id or old["task_id"] != task_id or old["workspace"] != str(workspace)):
                raise ConflictError("Native conversation belongs to another scope or workspace")
            if old and old["status"] != "released":
                raise ConflictError("Native conversation has an active or uncertain owner; use --new or inspect frontend status")
            if old and old.get("persona_digest", persona_digest({})) != persona_digest(settings):
                raise ConflictError("Persona changed; start --new (or a new task). The old conversation and original settings remain intact")
            if old and old.get("workflow_revision", workflow_revision) != workflow_revision:
                raise ConflictError("Workflow revision changed; start --new and preserve the old conversation")
            key = hashlib.sha256(canonical_json([workflow_id, task_id, conversation_id]).encode()).hexdigest()
            directory = self.store.config.state_dir / "native" / key
            record = {"contract_version": 1, "conversation_id": conversation_id, "workflow_id": workflow_id,
                "coach_id": settings.get("coach", {}).get("id", workflow_id), "task_id": task_id, "workspace": str(workspace), "binding_id": key,
                "session_dir": str(directory), "session_id": old.get("session_id") if old else None,
                "session_file": old.get("session_file") if old else None,
                "session_files": old.get("session_files", []) if old else [],
                "generation": old["generation"] + 1 if old else 1, "status": "starting",
                "token_hash": hashlib.sha256(owner_token.encode()).hexdigest(),
                "parent_pid": os.getpid(), "guardian_pid": None, "guardian_identity": None,
                "omp_pid": None, "omp_identity": None, "last_heartbeat_at": None,
                "observation": None, "created_at": old["created_at"] if old else self.store.timestamp()}
            record.update(configuration_digest=settings_digest(settings), persona_digest=persona_digest(settings),
                          workflow_revision=workflow_revision,
                          config_key="frontend-config:v1:" + conversation_id + ":" + str(record["generation"]))
            self.store._set_metadata(db, record["config_key"], {"settings": settings,
                "workflow_instructions": workflow_instructions, "workflow_revision": workflow_revision})
            self.save(db, record)
        return {**self.public(record), "owner_token": owner_token}

    def started(self, conversation_id, generation, owner_token, guardian_pid):
        with self.store.transaction() as db:
            record = self.authenticate(conversation_id, generation, owner_token)
            if record["status"] != "starting" or type(guardian_pid) is not int or guardian_pid < 1:
                raise ConflictError("Native process start does not match its lease")
            identity = process_identity(guardian_pid)
            if identity is None:
                raise ConflictError("Native guardian is no longer alive")
            record.update(guardian_pid=guardian_pid, guardian_identity=identity)
            self.save(db, record)
        return self.public(record)

    def attest(self, conversation_id, generation, owner_token, omp_pid, session_id, session_file):
        with self.store.transaction() as db:
            record = self.authenticate(conversation_id, generation, owner_token)
            if type(omp_pid) is not int or omp_pid < 1 or process_identity(record["guardian_pid"]) != record["guardian_identity"]:
                raise ConflictError("Native guardian identity is unavailable")
            fields = Path(f"/proc/{omp_pid}/stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) != record["guardian_pid"]:
                raise ConflictError("Native process is not the owned guardian child")
            validate_id(session_id)
            path = Path(session_file or "")
            if not path.is_absolute() or not path.resolve().is_relative_to(Path(record["session_dir"]).resolve()) or path.is_symlink():
                raise ConflictError("Native session escaped its assigned directory")
            if record["session_id"] is not None and record["session_id"] != session_id:
                record["generation"] += 1
            record.update(session_id=session_id, session_file=str(path), status="idle", omp_pid=omp_pid,
                          omp_identity=process_identity(omp_pid), last_heartbeat_at=self.store.timestamp())
            if not any(item["session_id"] == session_id for item in record["session_files"]):
                if len(record["session_files"]) >= 256:
                    raise ConflictError("Native conversation session index is full; create a new conversation; old sources are preserved")
                record["session_files"].append({"session_id": session_id, "session_file": str(path)})
            self.save(db, record)
        return self.public(record)

    def observe(self, conversation_id, generation, owner_token, phase="heartbeat"):
        if phase not in ("heartbeat", "agent_start", "agent_end", "session_shutdown"):
            raise RuntimeContractError("Unsupported native lifecycle observation")
        with self.store.transaction() as db:
            record = self.authenticate(conversation_id, generation, owner_token)
            record["last_heartbeat_at"] = self.store.timestamp()
            record["observation"] = phase
            if phase in ("agent_start", "agent_end"):
                record["status"] = "active" if phase == "agent_start" else "idle"
                if phase == "agent_start":
                    record["frontend_turn_id"] = str(uuid.uuid4())
                # Observed loop end is deliberately NOT strict settled proof.
                record["turn_evidence"] = "observed_loop_only"
            self.save(db, record)
        return self.public(record)

    def finish(self, conversation_id, generation, owner_token, exit_code):
        if exit_code is not None and type(exit_code) is not int:
            raise RuntimeContractError("Native exit code must be an integer or unknown")
        with self.store.transaction() as db:
            record = self.authenticate(conversation_id, generation, owner_token, uncertain=True)
            living = any(alive(record.get(kind + "_pid"), record.get(kind + "_identity")) for kind in ("guardian", "omp"))
            if living:
                raise ConflictError("Native owner is still alive; refusing release")
            valid = exit_code == 0
            if valid and record["session_file"]:
                if Path(record["session_file"]).exists():
                    try:
                        self.archive(record)
                    except (OSError, RuntimeContractError):
                        valid = False
                else:
                    record.update(session_file=None, session_id=None)
            record["status"] = "released" if valid else "uncertain"
            record["exit_code"] = exit_code
            self.save(db, record)
        return self.public(record)

    def recover(self, conversation_id):
        record = self.get(conversation_id)
        if record["status"] != "uncertain":
            raise ConflictError("Only an uncertain native conversation needs recovery")
        for kind in ("guardian", "omp"):
            if alive(record.get(kind + "_pid"), record.get(kind + "_identity")):
                raise ConflictError("Native process is still alive; heartbeat loss does not permit takeover")
        lock = InstanceLock(Path(record["session_dir"]) / "owner.lock")
        try:
            if record["session_file"]:
                self.archive(record)
            with self.store.transaction() as db:
                fresh = self.get(conversation_id)
                if fresh["generation"] != record["generation"] or fresh["status"] != "uncertain":
                    raise ConflictError("Native owner changed during recovery")
                fresh["status"] = "released"
                self.save(db, fresh)
        finally:
            lock.close()
        return self.public(fresh)

    def archive(self, record):
        from .git_sync import export_session
        return export_session(self.store.config, record, "local", root=Path(record["session_dir"]))

    def messages(self, conversation_id, query="", offset=0):
        from .memory import clip, text
        self.store.validate_offset(offset)
        text(query, 256, "history query", empty=True)
        record = self.get(conversation_id)
        messages = []
        for item in record["session_files"]:
            if not Path(item["session_file"]).exists():
                continue
            archive = self.archive({**record, **item})
            messages.extend({**message, "session_id": item["session_id"]} for message in archive["messages"]
                            if query.lower() in message["text"].lower())
        selected = []
        for message in messages[offset:offset + 20]:
            excerpt, truncated = clip(message["text"], 2048)
            selected.append({**message, "text": excerpt, "truncated": truncated})
        return {"conversation_id": conversation_id, "messages": selected, "has_more": len(messages) > offset + 20,
                "archive_mode": "all_branches", "source": "omp_native_session"}

    def reconcile(self):
        with self.store.transaction() as db:
            for record in self.records():
                if record["status"] not in ("released", "uncertain"):
                    record["status"] = "uncertain"
                    self.save(db, record)
