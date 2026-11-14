"""Follow Tijian Todos as durable Paveway reminders.

Tijian remains authoritative for Todo facts. Paveway owns the WakeBinding, its
scheduled event, delivery, deduplication and recovery. The reconciler and the
delivery gates read the native MCP contract instead of copying Todo storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ConflictError, NotFoundError, RuntimeContractError
from .models import parse_timestamp, utc_text, validate_id
from .store import canonical_json
from .workflow_setup import tijian_server
from .workflows import WorkflowProvider

MODES = ("independent", "linked", "follow")
TIME_SOURCES = ("scheduled", "due")
ELIGIBILITY = ("open", "open_and_incomplete")
RESUME_POLICIES = ("manual", "when_eligible")
MAX_OFFSET_SECONDS = 31 * 86400
MCP_TIMEOUT_SECONDS = 10.0
MCP_MAX_LINE = 2 * 1024 * 1024
TODO_ID_PATTERN = re.compile(r"[^\s]+")
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

ERROR_MESSAGES = {
    "todo_unavailable": "Tijian Todo data is temporarily unavailable; delivery is deferred",
    "todo_not_found": "Todo no longer exists; the binding is no longer eligible",
    "todo_ineligible": "Todo is no longer open under the binding eligibility policy",
    "todo_time_missing": "The selected Todo time field has no unambiguous absolute time",
    "todo_time_invalid": "The Todo time field is invalid or outside the supported range",
    "todo_source_missing": "Tijian MCP is not configured for this workflow",
    "todo_source_unavailable": "Tijian MCP configuration or executable is unavailable",
    "todo_source_mismatch": "The configured Tijian source no longer matches the binding",
    "todo_source_invalid": "Tijian MCP returned an unexpected or oversized Todo projection",
    "binding_conflict": "WakeBinding changed concurrently; reload and retry",
    "event_conflict": "The bound reminder event requires manual inspection",
    "reconcile_deferred": "The bound reminder is being dispatched; reconciliation was deferred",
}


class WakeBindingError(RuntimeContractError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or ERROR_MESSAGES.get(code, "WakeBinding operation failed"))


class TodoNotFound(WakeBindingError):
    def __init__(self, message=None):
        super().__init__("todo_not_found", message)


class TodoUnavailable(WakeBindingError):
    def __init__(self, message=None):
        super().__init__("todo_unavailable", message)


class TodoSourceInvalid(WakeBindingError):
    def __init__(self, code="todo_source_invalid", message=None):
        super().__init__(code, message)


def _safe_message(value, limit=512):
    if not isinstance(value, str):
        return ERROR_MESSAGES.get("todo_unavailable")
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value).strip()
    return value[:limit] or ERROR_MESSAGES.get("todo_unavailable")


def _epoch_millis(value, field):
    if type(value) is not int:
        raise TodoSourceInvalid("todo_time_invalid", f"{field} is not an integer UTC millisecond value")
    if value < -62135596800000 or value > 253402300799999:
        raise TodoSourceInvalid("todo_time_invalid", f"{field} is outside the supported timestamp range")
    return datetime.fromtimestamp(value // 1000, timezone.utc) + timedelta(milliseconds=value % 1000)


def _source_fingerprint(server):
    environment = os.environ
    material = {
        "executable": str(Path(server["executable"]).resolve()),
        "args": list(server["args"]),
        "runtime_mode": environment.get("TIJIAN_RUNTIME_MODE"),
        "profile_root": environment.get("TIJIAN_PROFILE_ROOT"),
        "xdg_data_home": environment.get("XDG_DATA_HOME"),
        "home": environment.get("HOME"),
    }
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()


@dataclass(frozen=True)
class TodoFacts:
    todo_id: str
    version: int
    updated_at: datetime | None
    decision: str
    scheduled_kind: str | None
    scheduled_value: object
    scheduled_at: datetime | None
    due_at: datetime | None
    credit_from: datetime | None
    manual_done: bool
    automatic_achieved: bool
    achieved_at: datetime | None
    description: str
    description_truncated: bool
    related_label: str | None

    @property
    def fingerprint(self):
        return hashlib.sha256(canonical_json({
            "version": self.version,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "decision": self.decision,
            "scheduledFor": [self.scheduled_kind, self.scheduled_value],
            "dueAt": self.due_at.isoformat() if self.due_at else None,
            "creditFrom": self.credit_from.isoformat() if self.credit_from else None,
            "manualDone": self.manual_done,
            "automaticAchieved": self.automatic_achieved,
            "achievedAt": self.achieved_at.isoformat() if self.achieved_at else None,
        }).encode()).hexdigest()

    @property
    def title(self):
        value = self.description.strip() or (self.related_label or "").strip() or self.todo_id
        return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)[:512]

    def public(self):
        return {"todo_id": self.todo_id, "version": self.version, "decision": self.decision,
                "scheduled_for": {"kind": self.scheduled_kind, "value": self.scheduled_value}
                if self.scheduled_kind else None,
                "due_at": utc_text(self.due_at) if self.due_at else None,
                "credit_from": utc_text(self.credit_from) if self.credit_from else None,
                "manual_done": self.manual_done, "automatic_achieved": self.automatic_achieved,
                "title": self.title, "description_truncated": self.description_truncated}


class TijianTodoSource:
    """Minimal stdio MCP client for the native Tijian Todo read tools."""

    def __init__(self, server, workspace):
        self.server = server
        self.workspace = Path(workspace)
        self.process = None
        self.buffer = b""
        self.next_id = 0
        self.lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        process, self.process = self.process, None
        self.buffer = b""
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        executable = Path(self.server["executable"])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise TodoUnavailable(ERROR_MESSAGES["todo_source_unavailable"])
        if not self.workspace.is_dir():
            raise TodoUnavailable(ERROR_MESSAGES["todo_source_unavailable"])
        try:
            self.process = subprocess.Popen(
                [str(executable), *self.server["args"]],
                cwd=str(self.workspace), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=os.environ.copy(), bufsize=0)
        except OSError:
            raise TodoUnavailable(ERROR_MESSAGES["todo_source_unavailable"]) from None
        self.buffer = b""
        try:
            self._request("initialize", {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "paveway-runtime", "version": "1"}})
            self._notify("notifications/initialized", {})
        except BaseException:
            self.close()
            raise

    def _send(self, message):
        try:
            data = memoryview((canonical_json(message) + "\n").encode())
            while data:
                written = self.process.stdin.write(data)
                if not written:
                    raise OSError("short write")
                data = data[written:]
            self.process.stdin.flush()
        except (AttributeError, OSError, ValueError):
            self.close()
            raise TodoUnavailable(ERROR_MESSAGES["todo_unavailable"]) from None

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_message(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise TodoUnavailable(ERROR_MESSAGES["todo_unavailable"])
            try:
                ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            except (OSError, ValueError):
                self.close()
                raise TodoUnavailable(ERROR_MESSAGES["todo_unavailable"]) from None
            if not ready:
                continue
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except OSError:
                self.close()
                raise TodoUnavailable(ERROR_MESSAGES["todo_unavailable"]) from None
            if not chunk:
                self.close()
                raise TodoUnavailable(ERROR_MESSAGES["todo_unavailable"])
            self.buffer += chunk
            if len(self.buffer) > MCP_MAX_LINE:
                self.close()
                raise TodoSourceInvalid()
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            return json.loads(line)
        except (ValueError, UnicodeError):
            self.close()
            raise TodoSourceInvalid() from None

    def _request(self, method, params):
        self.next_id += 1
        identity = self.next_id
        self._send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
        while True:
            message = self._read_message(MCP_TIMEOUT_SECONDS)
            if not isinstance(message, dict) or message.get("id") != identity:
                continue
            if "error" in message:
                error = message.get("error") if isinstance(message.get("error"), dict) else {}
                if method == "tools/call" and error.get("code") in (-32601, -32602):
                    raise TodoSourceInvalid("todo_source_invalid",
                                            "This tijian-mcp build does not expose the required Todo read tool")
                raise TodoUnavailable(ERROR_MESSAGES["todo_unavailable"])
            return message.get("result")

    def _call(self, name, arguments):
        with self.lock:
            self._start()
            result = self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise TodoSourceInvalid()
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise TodoSourceInvalid()
        if structured.get("ok") is not True:
            error = structured.get("error") if isinstance(structured.get("error"), dict) else {}
            code = error.get("code")
            message = _safe_message(error.get("message"))
            if code == "TODO_NOT_FOUND":
                raise TodoNotFound(message)
            if code in ("CONFIGURATION_REQUIRED", "REMOTE_UNAVAILABLE", "OUTCOME_UNCERTAIN", "INTERNAL"):
                raise TodoUnavailable(message)
            raise TodoSourceInvalid("todo_source_invalid", message)
        data = structured.get("data")
        if not isinstance(data, dict):
            raise TodoSourceInvalid()
        return data

    def get(self, todo_id):
        data = self._call("tijian_todo_get", {"todo_id": todo_id, "view": "metadata"})
        return self._facts(todo_id, data)

    def _field(self, todo_id, metadata, name, max_bytes=65536):
        descriptor = metadata.get("fields", {}).get(name)
        if not isinstance(descriptor, dict):
            return None, False
        if "value" in descriptor:
            return descriptor["value"], False
        page = self._call("tijian_todo_get", {"todo_id": todo_id, "view": "field",
                                             "field": name, "max_bytes": max_bytes})
        content = page.get("content")
        if not isinstance(content, str):
            raise TodoSourceInvalid()
        truncated = bool(page.get("hasMore"))
        if descriptor.get("encoding") == "json":
            try:
                return json.loads(content), False
            except (ValueError, RecursionError):
                raise TodoSourceInvalid() from None
        return content, truncated

    def _facts(self, todo_id, metadata):
        if metadata.get("todoId") != todo_id:
            raise TodoSourceInvalid()
        version = metadata.get("version")
        if type(version) is not int or version < 1:
            raise TodoSourceInvalid()
        decision = self._field(todo_id, metadata, "decision")[0]
        if decision not in ("open", "done", "dropped"):
            raise TodoSourceInvalid("todo_source_invalid", "Todo decision is missing or unknown")
        scheduled = self._field(todo_id, metadata, "scheduledFor")[0]
        scheduled_kind = scheduled_value = scheduled_at = None
        if scheduled is not None:
            if not isinstance(scheduled, dict) or scheduled.get("kind") not in ("day", "at") or "value" not in scheduled:
                raise TodoSourceInvalid("todo_time_invalid")
            scheduled_kind, scheduled_value = scheduled["kind"], scheduled["value"]
            if scheduled_kind == "at":
                scheduled_at = _epoch_millis(scheduled_value, "scheduledFor")
            elif not isinstance(scheduled_value, str):
                raise TodoSourceInvalid("todo_time_invalid")
        due_value = self._field(todo_id, metadata, "dueAt")[0]
        due_at = _epoch_millis(due_value, "dueAt") if due_value is not None else None
        credit_value = self._field(todo_id, metadata, "creditFrom")[0]
        credit_from = _epoch_millis(credit_value, "creditFrom") if credit_value is not None else None
        evaluation = metadata.get("evaluation")
        if not isinstance(evaluation, dict):
            raise TodoSourceInvalid()
        description, truncated = self._field(todo_id, metadata, "description", max_bytes=1024)
        if not isinstance(description, str):
            description = ""
        related = metadata.get("relatedLabel")
        if isinstance(related, dict):
            related = related.get("value")
        if not isinstance(related, str):
            related = None
        updated = metadata.get("updatedAt")
        return TodoFacts(
            todo_id=todo_id, version=version,
            updated_at=_epoch_millis(updated, "updatedAt") if updated is not None else None,
            decision=decision, scheduled_kind=scheduled_kind, scheduled_value=scheduled_value,
            scheduled_at=scheduled_at, due_at=due_at, credit_from=credit_from,
            manual_done=bool(evaluation.get("manualDone")),
            automatic_achieved=bool(evaluation.get("automaticAchieved")),
            achieved_at=_epoch_millis(evaluation["achievedAt"], "achievedAt")
            if evaluation.get("achievedAt") is not None else None,
            description=description, description_truncated=truncated, related_label=related)


def eligible(facts, policy):
    if facts.decision != "open":
        return False
    if policy == "open":
        return True
    if policy == "open_and_incomplete":
        return not (facts.manual_done or facts.automatic_achieved)
    raise RuntimeContractError("Unknown eligibility policy")


def _validate_todo_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or "\0" in value or not TODO_ID_PATTERN.fullmatch(value):
        raise RuntimeContractError("Todo ID must be 1-512 non-whitespace characters")
    return value


def _validate_name(value):
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise RuntimeContractError("Binding name must be 1-64 ASCII letters, digits, dots, underscores or hyphens")
    return value


def _validate_mode(mode):
    if mode not in MODES:
        raise RuntimeContractError("Choose independent, linked or follow")
    return mode


def _validate_time_source(value):
    if value not in TIME_SOURCES:
        raise RuntimeContractError("Choose scheduled or due")
    return value


def _validate_eligibility(value):
    if value not in ELIGIBILITY:
        raise RuntimeContractError("Choose open or open_and_incomplete")
    return value


def _validate_resume(value):
    if value not in RESUME_POLICIES:
        raise RuntimeContractError("Choose manual or when_eligible")
    return value


def _validate_offset(value):
    if type(value) is not int or not -MAX_OFFSET_SECONDS <= value <= MAX_OFFSET_SECONDS:
        raise RuntimeContractError("offset_seconds must be an integer within +/-31 days")
    return value


def _binding_id(workflow_id, task_id, todo_id, name):
    return hashlib.sha256(f"{workflow_id}\0{task_id}\0{todo_id}\0{name}".encode()).hexdigest()


def _compute_due(facts, binding, now):
    mode = binding["mode"]
    if mode in ("independent", "linked"):
        base = parse_timestamp(binding["reminder_at_utc"])
    elif binding["time_source"] == "scheduled":
        if facts.scheduled_kind != "at" or facts.scheduled_at is None:
            raise WakeBindingError("todo_time_missing",
                "scheduledFor is a logical training day, not an unambiguous absolute instant; choose due_at or an at schedule")
        base = facts.scheduled_at
    else:
        if facts.due_at is None:
            raise WakeBindingError("todo_time_missing", "Todo has no dueAt value")
        base = facts.due_at
    try:
        due = base + timedelta(seconds=binding["offset_seconds"])
    except (OverflowError, ValueError):
        raise WakeBindingError("todo_time_invalid") from None
    if due.year > 9999 or due.year < 1:
        raise WakeBindingError("todo_time_invalid")
    return due


class WakeBindings:
    def __init__(self, store, provider=None, source_factory=None):
        self.store = store
        self.provider = provider or WorkflowProvider(store.config.workflow_root)
        self.source_factory = source_factory or TijianTodoSource

    def _now(self):
        return self.store.clock()

    def _source(self, workflow_id):
        try:
            workflow = self.provider.describe(workflow_id)
            server = tijian_server(self.store.config, workflow_id)
        except ConflictError as error:
            raise WakeBindingError("todo_source_missing", str(error)) from None
        except RuntimeContractError as error:
            code = "todo_source_unavailable" if error.code == "invalid_workflow" else "todo_source_missing"
            raise WakeBindingError(code, _safe_message(str(error))) from None
        except OSError:
            raise WakeBindingError("todo_source_unavailable") from None
        if server is None:
            raise WakeBindingError("todo_source_missing")
        executable = Path(server["executable"])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise WakeBindingError("todo_source_unavailable")
        source = self.source_factory(server, workflow.workspace)
        return source, _source_fingerprint(server), f"{executable.name}:{server['permission']}"

    async def _fetch(self, source, todo_id):
        try:
            return await asyncio.to_thread(source.get, todo_id)
        except WakeBindingError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise TodoUnavailable(_safe_message(str(error))) from None
        except Exception:
            raise TodoUnavailable() from None

    def _scope(self, workflow_id, task_id):
        validate_id(workflow_id)
        validate_id(task_id)
        return workflow_id, task_id

    def _ensure_scope(self, binding, workflow_id, task_id):
        if (binding["workflow_id"], binding["task_id"]) != (workflow_id, task_id):
            raise ConflictError("WakeBinding belongs to another workflow or task")

    def _public(self, binding, include_event=True):
        result = dict(binding)
        if include_event and binding.get("event_id"):
            try:
                event = self.store.event(binding["event_id"])
            except NotFoundError:
                result["event"] = None
            else:
                result["event"] = {key: event[key] for key in (
                    "event_id", "status", "version", "not_before_utc", "attempt", "run_id",
                    "last_error_code", "updated_at")}
        elif include_event:
            result["event"] = None
        return result

    async def create(self, *, workflow_id, task_id, todo_id, name="default", mode="follow",
                     time_source="due", offset_seconds=0, eligibility="open_and_incomplete",
                     reminder_at=None, resume_policy="manual", authorized_by_user=False,
                     owner_conversation_id=None):
        if authorized_by_user is not True:
            raise RuntimeContractError("Creating a WakeBinding requires explicit user authorization")
        workflow_id, task_id = self._scope(workflow_id, task_id)
        todo_id = _validate_todo_id(todo_id)
        name = _validate_name(name)
        mode = _validate_mode(mode)
        time_source = _validate_time_source(time_source)
        offset_seconds = _validate_offset(offset_seconds)
        eligibility = _validate_eligibility(eligibility)
        resume_policy = _validate_resume(resume_policy)
        if mode != "follow" and offset_seconds != 0:
            raise RuntimeContractError("Independent and linked bindings do not apply an offset")
        reminder_at_utc = None if reminder_at is None else utc_text(parse_timestamp(reminder_at))
        if mode != "follow" and reminder_at_utc is None:
            raise RuntimeContractError("Independent and linked bindings require reminder_at")
        source, source_id, source_label = self._source(workflow_id)
        try:
            facts = await self._fetch(source, todo_id)
        finally:
            close = getattr(source, "close", None)
            if close:
                close()
        if not eligible(facts, eligibility):
            raise WakeBindingError("todo_ineligible")
        candidate = {"mode": mode, "time_source": time_source, "offset_seconds": offset_seconds,
                     "reminder_at_utc": reminder_at_utc}
        due = _compute_due(facts, candidate, self._now())
        binding_id = _binding_id(workflow_id, task_id, todo_id, name)
        binding, created = self.store.create_wakebinding(
            binding_id=binding_id, workflow_id=workflow_id, task_id=task_id,
            owner_conversation_id=owner_conversation_id, todo_source_id=source_id,
            todo_source_label=source_label, todo_id=todo_id, name=name, mode=mode,
            time_source=time_source, offset_seconds=offset_seconds, reminder_at_utc=reminder_at_utc,
            eligibility=eligibility, resume_policy=resume_policy, todo_version=facts.version,
            todo_fingerprint=facts.fingerprint, todo_title=facts.title,
            due_at_utc=utc_text(due))
        if not created:
            if binding["todo_source_id"] != source_id:
                raise WakeBindingError("todo_source_mismatch")
            same = (binding["mode"], binding["time_source"], binding["offset_seconds"],
                    binding["reminder_at_utc"], binding["eligibility"], binding["resume_policy"]) == (
                    mode, time_source, offset_seconds, reminder_at_utc, eligibility, resume_policy)
            if not same:
                raise ConflictError("A WakeBinding already exists with different settings; update it explicitly")
            binding = await self.reconcile_binding(binding_id, reason="create-existing", facts=facts)
        return {"api_version": 1, "created": created, "binding": self._public(binding),
                "todo": facts.public()}

    async def list(self, workflow_id, task_id=None, status=None, offset=0):
        validate_id(workflow_id)
        if task_id is not None:
            validate_id(task_id)
        rows = self.store.wakebindings(workflow_id=workflow_id, task_id=task_id, status=status,
                                       offset=offset, limit=20)
        return [self._public(row) for row in rows]

    def source_status(self, workflow_id):
        try:
            source, source_id, label = self._source(workflow_id)
        except WakeBindingError as error:
            return {"configured": False, "error_code": error.code,
                    "message": _safe_message(str(error))}
        close = getattr(source, "close", None)
        if close:
            close()
        return {"configured": True, "source_id": source_id, "label": label,
                "live_check": "not_probed"}

    async def show(self, binding_id, workflow_id=None, task_id=None):
        binding = self.store.wakebinding(binding_id)
        if workflow_id is not None:
            self._scope(workflow_id, task_id or binding["task_id"])
            self._ensure_scope(binding, workflow_id, task_id or binding["task_id"])
        return self._public(binding)

    async def update(self, binding_id, expected_version, *, workflow_id, task_id, **changes):
        binding = self.store.wakebinding(binding_id)
        self._ensure_scope(binding, workflow_id, task_id)
        allowed = {"mode", "time_source", "offset_seconds", "eligibility", "reminder_at", "resume_policy"}
        if "name" in changes:
            raise RuntimeContractError(
                "WakeBinding name is part of its stable identity and cannot be changed after creation; create a separate binding instead")
        if set(changes) - allowed:
            raise RuntimeContractError("Unknown WakeBinding update field")
        if not changes:
            raise RuntimeContractError("No WakeBinding changes supplied")
        fields = {}
        if "mode" in changes:
            fields["mode"] = _validate_mode(changes["mode"])
        if "time_source" in changes:
            fields["time_source"] = _validate_time_source(changes["time_source"])
        if "offset_seconds" in changes:
            fields["offset_seconds"] = _validate_offset(changes["offset_seconds"])
        if "eligibility" in changes:
            fields["eligibility"] = _validate_eligibility(changes["eligibility"])
        if "resume_policy" in changes:
            fields["resume_policy"] = _validate_resume(changes["resume_policy"])
        if "reminder_at" in changes:
            fields["reminder_at_utc"] = utc_text(parse_timestamp(changes["reminder_at"]))
        merged = {**binding, **fields}
        if merged["mode"] != "follow" and merged["offset_seconds"] != 0:
            raise RuntimeContractError("Independent and linked bindings do not apply an offset")
        if merged["mode"] != "follow" and merged["reminder_at_utc"] is None:
            if binding.get("event_id"):
                fields["reminder_at_utc"] = self.store.event(binding["event_id"])["not_before_utc"]
            else:
                raise RuntimeContractError("Independent and linked bindings require reminder_at")
        updated = self.store.update_wakebinding(binding_id, expected_version, fields)
        updated = await self.reconcile_binding(binding_id, reason="update", reactivate=True)
        return self._public(updated)

    async def cancel(self, binding_id, expected_version, *, workflow_id, task_id):
        binding = self.store.wakebinding(binding_id)
        self._ensure_scope(binding, workflow_id, task_id)
        return self._public(self.store.disable_wakebinding(binding_id, expected_version))

    async def sync(self, *, workflow_id, task_id, binding_id=None, reactivate=True):
        if binding_id is not None:
            binding = self.store.wakebinding(binding_id)
            self._ensure_scope(binding, workflow_id, task_id)
            binding = await self.reconcile_binding(binding_id, reason="manual_sync", reactivate=reactivate)
            return {"api_version": 1, "bindings": [self._public(binding)]}
        rows = self.store.wakebindings(workflow_id=workflow_id, task_id=task_id, limit=5)
        results = []
        for row in rows:
            if row["status"] == "disabled":
                results.append(self._public(row))
                continue
            try:
                results.append(self._public(await self.reconcile_binding(
                    row["binding_id"], reason="manual_sync", reactivate=reactivate)))
            except WakeBindingError as error:
                results.append({**self._public(self.store.wakebinding(row["binding_id"])),
                                "sync_error": {"code": error.code, "message": _safe_message(str(error))}})
        return {"api_version": 1, "bindings": results}

    async def reconcile_binding(self, binding_id, *, reason="periodic", facts=None, reactivate=None):
        binding = self.store.wakebinding(binding_id)
        if binding["status"] == "disabled":
            return binding
        if reactivate is None:
            reactivate = binding["resume_policy"] == "when_eligible"
        try:
            source, source_id, _ = self._source(binding["workflow_id"])
        except WakeBindingError as error:
            return self._mark(binding, status="pending" if error.code.startswith("todo_source") else "conflict",
                              error=error.code, message=str(error))
        if source_id != binding["todo_source_id"]:
            return self._mark(binding, status="conflict", error="todo_source_mismatch",
                              message=ERROR_MESSAGES["todo_source_mismatch"], event_action="cancel")
        try:
            if facts is None:
                facts = await self._fetch(source, binding["todo_id"])
        except TodoNotFound as error:
            return self._mark(binding, status="ineligible", error="todo_not_found",
                              message=str(error), event_action="cancel")
        except TodoUnavailable as error:
            return self._mark(binding, status="pending", error="todo_unavailable",
                              message=str(error))
        except TodoSourceInvalid as error:
            return self._mark(binding, status="conflict", error=error.code,
                              message=str(error), event_action="cancel")
        finally:
            close = getattr(source, "close", None)
            if close:
                close()
        if not eligible(facts, binding["eligibility"]):
            return self._mark(binding, status="ineligible", error="todo_ineligible", facts=facts,
                              message=ERROR_MESSAGES["todo_ineligible"], event_action="cancel")
        try:
            due = _compute_due(facts, binding, self._now())
        except WakeBindingError as error:
            return self._mark(binding, status="pending", error=error.code, facts=facts,
                              message=str(error), event_action="cancel")
        try:
            event = self.store.event(binding["event_id"]) if binding["event_id"] else None
        except NotFoundError:
            event = None
        due_text = utc_text(due)
        observation = {"status": "active", "last_error_code": None, "last_error_message": None}
        if event is None:
            return self._replace(binding, due_text, facts, observation)
        if event["status"] in ("queued", "waiting_anchor"):
            if event["not_before_utc"] == due_text:
                return self._mark(binding, facts=facts, fields=observation, reconciled=True)
            return self._reschedule(binding, due_text, facts, observation)
        if event["status"] in ("leased", "running"):
            return self._mark(binding, status="active", error="reconcile_deferred", facts=facts,
                              message=ERROR_MESSAGES["reconcile_deferred"], reconciled=True)
        if event["status"] == "succeeded":
            if event["not_before_utc"] == due_text:
                return self._mark(binding, facts=facts, fields=observation, reconciled=True)
            return self._replace(binding, due_text, facts, observation)
        if event["status"] == "cancelled":
            repairable = (binding["status"] == "pending"
                          and binding["last_error_code"] in ("todo_time_missing", "todo_time_invalid"))
            if binding["status"] == "ineligible" or reactivate or repairable:
                if binding["status"] == "ineligible" and not reactivate:
                    return self._mark(binding, status="ineligible", error="todo_ineligible", facts=facts)
                return self._replace(binding, due_text, facts, observation)
            return self._mark(binding, status="disabled", error="cancelled_by_user",
                              message="Reminder was cancelled by the user; no automatic recreation")
        return self._mark(binding, status="pending", error="event_conflict", facts=facts,
                          message=ERROR_MESSAGES["event_conflict"])

    def _mark(self, binding, *, status=None, error=None, message=None, facts=None,
              fields=None, event_action="keep", due=None, generation=None, reconciled=False):
        stamp = self.store.timestamp()
        values = {"last_attempt_at": stamp}
        if reconciled:
            values["last_reconciled_at"] = stamp
        if facts is not None:
            values.update(todo_version=facts.version, todo_fingerprint=facts.fingerprint,
                          todo_title=facts.title, observed_at_utc=stamp)
        if status is not None:
            values["status"] = status
        if error is not None:
            values["last_error_code"] = error
            values["last_error_message"] = _safe_message(message or ERROR_MESSAGES.get(error, error))
        elif status == "active":
            values["last_error_code"] = None
            values["last_error_message"] = None
        if fields:
            values.update(fields)
        semantic = event_action != "keep" or any(
            key in values and values[key] != binding.get(key)
            for key in ("status", "last_error_code", "last_error_message"))
        if not semantic:
            diagnostic = {key: value for key, value in values.items() if key in {
                "todo_version", "todo_fingerprint", "todo_title", "observed_at_utc",
                "last_reconciled_at", "last_attempt_at"}}
            return self.store.touch_wakebinding(binding["binding_id"], diagnostic)
        for attempt in range(2):
            current = self.store.wakebinding(binding["binding_id"]) if attempt else binding
            if attempt and (current["status"] != binding["status"]
                            or current["last_error_code"] != binding["last_error_code"]
                            or current["event_id"] != binding["event_id"]
                            or current["schedule_generation"] != binding["schedule_generation"]):
                raise WakeBindingError("binding_conflict")
            try:
                return self.store.apply_wakebinding(current["binding_id"], current["version"],
                    fields=values, event_action=event_action, due=due, generation=generation)
            except ConflictError:
                if attempt:
                    raise WakeBindingError("binding_conflict")
        raise WakeBindingError("binding_conflict")

    def _replace(self, binding, due, facts, observation):
        return self._mark(binding, facts=facts, fields=observation, event_action="replace",
                          due=due, generation=binding["schedule_generation"] + 1, reconciled=True)

    def _reschedule(self, binding, due, facts, observation):
        return self._mark(binding, facts=facts, fields=observation, event_action="reschedule",
                          due=due, reconciled=True)

    async def reconcile_due(self, limit=20):
        rows = self.store.due_wakebindings(self._now(), limit=limit)
        blocked = set()
        for row in rows:
            try:
                result = await self.reconcile_binding(row["binding_id"], reason="due")
            except Exception:
                blocked.add(row["event_id"])
                continue
            if result["status"] != "active" or result["event_id"] is None:
                blocked.add(row["event_id"])
        return blocked

    async def reconcile_all(self, limit=20, *, reason="periodic"):
        rows = self.store.reconcile_wakebindings(self._now(), limit=limit)
        results = []
        for row in rows:
            try:
                results.append(await self.reconcile_binding(row["binding_id"], reason=reason,
                                                            reactivate=row["resume_policy"] == "when_eligible"))
            except WakeBindingError:
                results.append(self.store.wakebinding(row["binding_id"]))
            except Exception:
                results.append(self.store.wakebinding(row["binding_id"]))
        return results

    def local_gate(self, event):
        payload = event.get("payload") or {}
        binding_id = payload.get("wake_binding_id")
        if binding_id is None:
            return True
        try:
            binding = self.store.wakebinding(binding_id)
        except (RuntimeContractError, NotFoundError):
            return False
        if binding["status"] != "active" or binding["event_id"] != event["event_id"]:
            return False
        if payload.get("todo_source_id") != binding["todo_source_id"] or payload.get("todo_id") != binding["todo_id"]:
            return False
        return event["status"] in ("queued", "leased", "running", "succeeded")

    async def verify(self, event):
        """Check a leased event against Tijian without mutating its event row."""
        payload = event.get("payload") or {}
        binding_id = payload.get("wake_binding_id")
        if binding_id is None:
            return {"allowed": True, "action": "none", "reason": None}
        if not self.local_gate(event):
            return {"allowed": False, "action": "cancel", "reason": "wake_binding_inactive"}
        binding = self.store.wakebinding(binding_id)
        try:
            source, source_id, _ = self._source(binding["workflow_id"])
        except WakeBindingError as error:
            self._mark(binding, status="pending", error=error.code, message=str(error))
            return {"allowed": False, "action": "defer", "reason": error.code}
        if source_id != binding["todo_source_id"]:
            self._mark(binding, status="conflict", error="todo_source_mismatch",
                       message=ERROR_MESSAGES["todo_source_mismatch"])
            return {"allowed": False, "action": "cancel", "reason": "todo_source_mismatch"}
        try:
            facts = await self._fetch(source, binding["todo_id"])
        except TodoNotFound as error:
            self._mark(binding, status="ineligible", error="todo_not_found", message=str(error))
            return {"allowed": False, "action": "cancel", "reason": "todo_not_found"}
        except TodoUnavailable as error:
            self._mark(binding, status="pending", error="todo_unavailable", message=str(error))
            return {"allowed": False, "action": "defer", "reason": "todo_unavailable"}
        except TodoSourceInvalid as error:
            self._mark(binding, status="conflict", error=error.code, message=str(error))
            return {"allowed": False, "action": "cancel", "reason": error.code}
        finally:
            close = getattr(source, "close", None)
            if close:
                close()
        if not eligible(facts, binding["eligibility"]):
            self._mark(binding, status="ineligible", error="todo_ineligible", facts=facts,
                       message=ERROR_MESSAGES["todo_ineligible"])
            return {"allowed": False, "action": "cancel", "reason": "todo_ineligible"}
        try:
            due = _compute_due(facts, binding, self._now())
        except WakeBindingError as error:
            self._mark(binding, status="pending", error=error.code, facts=facts,
                       message=str(error))
            return {"allowed": False, "action": "cancel", "reason": error.code}
        due_text = utc_text(due)
        self._mark(binding, status="active", facts=facts,
                   fields={"last_error_code": None, "last_error_message": None}, reconciled=True)
        if event["not_before_utc"] != due_text:
            return {"allowed": False, "action": "reschedule", "reason": "todo_time_changed",
                    "due": due_text}
        return {"allowed": True, "action": "none", "reason": None}

    async def gate(self, event):
        if not self.local_gate(event):
            return False
        payload = event.get("payload") or {}
        binding_id = payload.get("wake_binding_id")
        if binding_id is None:
            return True
        if event["status"] in ("leased", "running"):
            result = await self.verify(event)
            return result["allowed"]
        try:
            binding = await self.reconcile_binding(binding_id, reason="delivery_gate")
        except Exception:
            return False
        if binding["status"] != "active" or binding["event_id"] != event["event_id"]:
            return False
        current = self.store.event(event["event_id"])
        return (current["status"] in ("queued", "leased", "running", "succeeded")
                and current["not_before_utc"] is not None
                and current["not_before_utc"] <= self.store.timestamp()
                and self.local_gate(current))

    def defer(self, binding_id, code, message=None):
        """Bounded backoff after a pre-dispatch verification failure.

        The binding becomes pending so claim/projection stop retrying it until
        the periodic reconciler re-reads Tijian. CAS races are not retried here.
        """
        try:
            binding = self.store.wakebinding(binding_id)
            if binding["status"] == "disabled":
                return False
            self._mark(binding, status="pending", error=code,
                       message=message or ERROR_MESSAGES.get(code, code))
            return True
        except (RuntimeContractError, NotFoundError):
            return False
