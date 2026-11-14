"""One foreground dispatch, persistent evidence before every external prompt."""

import asyncio
import json
import os
import signal
import sqlite3
import socket
import stat
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from . import __version__
from .adapters.omp_rpc import OmpRpc
from .config import ensure_private_directory
from .db import initialize_database
from .errors import AgentError, ConflictError, SessionConflictError, RuntimeContractError
from .ipc import MAX_REQUEST, handle_connection
from .models import parse_timestamp, utc_text
from .memory import Memory, History
from .processes import InstanceLock, enable_subreaper, process_identity
from .store import Store, bounded_text
from .compatibility import EXPECTED_LIFECYCLE_PROTOCOL_VERSION, EXPECTED_NATIVE_PROTOCOL_VERSION
from .workflows import WorkflowProvider

LIFECYCLE_TARGET_FIELDS = (
    "runtime_instance", "runtime_build_id", "runtime_version",
    "native_protocol_version", "runtime_pid", "runtime_identity",
)


def arguments(raw, allowed, required=()):
    if set(raw) - set(allowed) or set(required) - set(raw):
        raise RuntimeContractError("Unknown or missing command arguments")


class Runtime:
    def __init__(self, config, config_path=None):
        self.config = config
        self.config_path = config_path
        self.owner = str(uuid.uuid4())
        self.stop_event = asyncio.Event()
        self.changed = asyncio.Event()
        self.store = None
        self.provider = WorkflowProvider(config.workflow_root)
        self.agent_health = "idle"
        self.lock_fds = ()
        self.clients = set()
        self.active_dispatch = None
        self.active_run_id = None
        self.active_scope = None
        self.pending_restart = None
        self.draining = False
        self.stopping = False
        self.stop_timer = None
        self.lifecycle_lock = None
        self.runtime_pid = os.getpid()
        self.runtime_identity = process_identity(self.runtime_pid)
        self.execution_progress = None
        self.sync_wakeup = asyncio.Event()
        self.wake_wakeup = asyncio.Event()
        self.wake_reconciler = None
        self.wake_guard_task = None
        self.bound_task = None
        from .build_id import runtime_build_id
        self.runtime_build_id = runtime_build_id()

    def dispatch_api(self, command, raw):
        if self.draining and command in (
                "schedule", "agent.restart", "frontend.begin", "frontend.started",
                "frontend.attest", "frontend.inference", "frontend.recover"):
            raise ConflictError(
                "Runtime 正在安全收尾（draining）；新的工作已暂时拒绝，已有持久排程和会话保持不变。")
        if command == "cache.stats":
            from .cache_usage import session_usage
            from .frontend import Frontend
            arguments(raw, ("conversation_id", "workflow_id", "task_id"))
            if set(raw) == {"conversation_id"}:
                record = Frontend(self.store).get(raw["conversation_id"])
            elif set(raw) == {"workflow_id", "task_id"}:
                record = self.store.binding_for_task(raw["task_id"], raw["workflow_id"])
            else:
                raise RuntimeContractError("Choose a conversation or workflow/task")
            if not record.get("session_file"):
                return {"source": "omp_session_jsonl", "status": "unavailable", "reason": "session_not_persisted"}
            path = Path(record["session_file"])
            if path.is_symlink() or not path.resolve().is_relative_to(Path(record["session_dir"]).resolve()):
                raise ConflictError("OMP session escaped its assigned directory")
            return session_usage(path, record.get("session_id"))
        if command.startswith("native_memory.") or command == "native_history.search":
            from .frontend import Frontend, alive
            arguments(raw, ("conversation_id", "generation", "owner_token", "parameters"),
                      ("conversation_id", "generation", "owner_token", "parameters"))
            owner = {key: raw[key] for key in ("conversation_id", "generation", "owner_token")}
            record = Frontend(self.store).authenticate(**owner)
            if not alive(record.get("omp_pid"), record.get("omp_identity")) or not isinstance(raw["parameters"], dict):
                raise ConflictError("Native owner is unavailable or parameters are invalid")
            values = dict(raw["parameters"])
            if command == "native_history.search":
                arguments(values, ("source", "query", "offset"), ("source",))
                source = values.pop("source")
                if source == "native":
                    return Frontend(self.store).messages(record["conversation_id"], **values)
                if source == "background":
                    return History(self.store).search(record["workflow_id"], record["task_id"], **values)
                raise RuntimeContractError("Unknown history source")
            if not self.provider.describe(record["workflow_id"]).memory_enabled:
                raise RuntimeContractError("Paveway memory tools are disabled in this workflow")
            operation = command.removeprefix("native_memory.")
            if operation == "context":
                arguments(values, ("known",), ("known",))
                return Memory(self.store).context(record["workflow_id"], record["task_id"], **values)
            contracts = {"list": (("scope", "query", "offset"), ()), "show": (("scope", "key"), ("key",)),
                "put": (("scope", "key", "title", "content", "expected_version", "kind", "pinned", "source_event_id", "expires_at"), ("key", "title", "content", "expected_version")),
                "delete": (("scope", "key", "expected_version"), ("key", "expected_version"))}
            if operation not in contracts:
                raise RuntimeContractError("Unknown native memory operation")
            arguments(values, *contracts[operation])
            scope = values.pop("scope", "profile")
            if scope not in ("profile", "task"):
                raise RuntimeContractError("Choose profile or task memory scope")
            return getattr(Memory(self.store), operation)(record["workflow_id"],
                task_id=record["task_id"] if scope == "task" else None, **values)
        if command in ("coach.state.show", "coach.state.put"):
            from .coaches import state
            fields = ("coach_id", "key") + (("value", "expected_version") if command.endswith("put") else ())
            arguments(raw, fields, fields)
            return state(self.store, **raw)
        if command.startswith("wakeup."):
            from .wakeup import Wakeup
            arguments(raw, ("conversation_id", "generation", "owner_token", "parameters"),
                      ("conversation_id", "generation", "owner_token", "parameters"))
            owner = {key: raw[key] for key in ("conversation_id", "generation", "owner_token")}
            result = Wakeup(self.store).call(command.removeprefix("wakeup."), owner, raw["parameters"])
            self.changed.set()
            return result
        if command.startswith("inbox."):
            from .inbox import Inbox
            if command.startswith("inbox.bound-"):
                from .frontend import Frontend, alive
                arguments(raw, ("conversation_id", "generation", "owner_token", "parameters"),
                          ("conversation_id", "generation", "owner_token", "parameters"))
                owner = {key: raw[key] for key in ("conversation_id", "generation", "owner_token")}
                record = Frontend(self.store).authenticate(**owner)
                if not alive(record.get("omp_pid"), record.get("omp_identity")):
                    raise ConflictError("Native owner is no longer alive")
                parameters = raw["parameters"]
                operation = command.removeprefix("inbox.bound-")
                fields = {"list": ("offset",), "show": ("delivery_id",), "acknowledge": ("delivery_id", "expected_version")}
                if operation not in fields or not isinstance(parameters, dict):
                    raise RuntimeContractError("Unknown bound inbox operation")
                arguments(parameters, fields[operation], () if operation == "list" else fields[operation])
                return getattr(Inbox(self.store), "offer" if operation == "list" else operation)(record["workflow_id"], task_id=record["task_id"], **parameters)
            if command == "inbox.list":
                arguments(raw, ("workflow_id", "task_id", "offset"), ("workflow_id",))
                return Inbox(self.store).list(**raw)
            if command == "inbox.show":
                arguments(raw, ("workflow_id", "task_id", "delivery_id"), ("workflow_id", "delivery_id"))
                return Inbox(self.store).show(**raw)
            if command == "inbox.acknowledge":
                arguments(raw, ("workflow_id", "task_id", "delivery_id", "expected_version"),
                          ("workflow_id", "delivery_id", "expected_version"))
                return Inbox(self.store).acknowledge(**raw)
            raise RuntimeContractError("Unknown inbox operation")
        if command.startswith("frontend."):
            from .frontend import Frontend
            frontend = Frontend(self.store)
            operation = command.removeprefix("frontend.")
            if operation == "begin":
                arguments(raw, ("workflow_id", "task_id", "conversation_id", "new"), ("workflow_id", "task_id"))
                workflow = self.provider.describe(raw["workflow_id"])
                instructions = ""
                return frontend.begin(**raw, workspace=workflow.workspace, settings=workflow.customization,
                                      workflow_revision=workflow.revision, workflow_instructions=instructions,
                                      role_base=workflow.base,
                                      inference_preference={key: workflow.customization.get("inference", {}).get(key) or getattr(self.config.agent, key)
                                                            for key in ("model", "thinking")})
            contracts = {
                "started": ("conversation_id", "generation", "owner_token", "guardian_pid"),
                "attest": ("conversation_id", "generation", "owner_token", "omp_pid", "session_id", "session_file"),
                "observe": ("conversation_id", "generation", "owner_token", "phase"),
                "inference": ("conversation_id", "generation", "owner_token", "model", "thinking"),
                "finish": ("conversation_id", "generation", "owner_token", "exit_code"),
                "show": ("conversation_id",), "recover": ("conversation_id",),
                "messages": ("conversation_id", "query", "offset"),
            }
            if operation == "list":
                arguments(raw, ("workflow_id", "task_id"))
                return [frontend.public(row) for row in frontend.records(**raw)][:100]
            if operation not in contracts:
                raise RuntimeContractError("Unknown frontend operation")
            arguments(raw, contracts[operation], ("conversation_id",) if operation == "messages" else contracts[operation])
            if operation == "show":
                return frontend.public(frontend.get(**raw))
            if operation == "messages":
                return frontend.messages(**raw)
            return getattr(frontend, operation)(**raw)
        if command == "sync.request":
            arguments(raw, ())
            from . import git_sync
            values = git_sync.settings(self.config)
            if not values or not values["enabled"]:
                raise RuntimeContractError("Configure and enable Git sync first")
            before = git_sync.read_json(git_sync.local_root(self.config) / "status.json", {})
            self.sync_wakeup.set()
            return {"requested": True, "previous_completion": before.get("completed_at")}
        if command == "status":
            arguments(raw, ())
            supervisor = self.store.status()
            health = "degraded" if supervisor["supervisor"]["phase"] in ("backoff", "open") else self.agent_health
            return {"daemon": "running", "runtime_version": __version__, "runtime_build_id": self.runtime_build_id,
                    "native_protocol_version": EXPECTED_NATIVE_PROTOCOL_VERSION,
                    "lifecycle_protocol_version": EXPECTED_LIFECYCLE_PROTOCOL_VERSION,
                    "runtime_instance": self.owner, "runtime_pid": self.runtime_pid,
                    "runtime_identity": self.runtime_identity, "draining": self.draining,
                    "stopping": self.stopping,
                    "milestone": "M4", "agent_health": health, **supervisor}
        if command == "schedule":
            arguments(raw, ("workflow_id", "task_id", "reason", "dedupe_key", "payload", "at", "after_turn", "run_id"),
                      ("workflow_id", "task_id", "reason", "dedupe_key"))
            self.provider.describe(raw["workflow_id"])
            result = self.store.enqueue(**raw)
        elif command.startswith("memory."):
            contracts = {
                "put": (("workflow_id", "task_id", "key", "title", "content", "expected_version", "kind", "pinned", "source_event_id", "expires_at"), ("workflow_id", "key", "title", "content")),
                "show": (("workflow_id", "task_id", "key"), ("workflow_id", "key")),
                "list": (("workflow_id", "task_id", "query", "offset"), ("workflow_id",)),
                "delete": (("workflow_id", "task_id", "key", "expected_version"), ("workflow_id", "key", "expected_version")),
                "recall": (("workflow_id", "task_id", "known"), ("workflow_id", "task_id")),
            }
            operation = command.removeprefix("memory.")
            if operation not in contracts:
                raise RuntimeContractError("Unknown memory operation")
            arguments(raw, *contracts[operation])
            if operation == "put":
                self.provider.describe(raw["workflow_id"])
            return getattr(Memory(self.store), "context" if operation == "recall" else operation)(**raw)
        elif command.startswith("history."):
            contracts = {"search": (("workflow_id", "task_id", "query", "offset"), ("workflow_id",)),
                         "show": (("run_id",), ("run_id",)),
                         "messages": (("workflow_id", "task_id", "query", "offset"), ("workflow_id", "task_id"))}
            operation = command.removeprefix("history.")
            if operation not in contracts:
                raise RuntimeContractError("Unknown history operation")
            arguments(raw, *contracts[operation])
            return getattr(History(self.store), operation)(**raw)
        elif command == "events.upcoming":
            arguments(raw, ("workflow_id", "task_id"), ("workflow_id",))
            return self.store.upcoming(**raw)
        elif command == "events.list":
            arguments(raw, ("status", "offset"))
            # Lists are summaries so large private payloads cannot overflow IPC.
            return [{key: event[key] for key in ("event_id", "workflow_id", "task_id", "status", "version", "not_before_utc", "attempt", "run_id", "last_error_code")}
                    for event in self.store.events(**raw)]
        elif command == "events.show":
            arguments(raw, ("event_id",), ("event_id",))
            event = self.store.event(**raw)
            if self.execution_progress and event["run_id"] == self.execution_progress["run_id"] and event["status"] not in ("succeeded", "failed", "blocked", "uncertain", "cancelled", "dead_letter"):
                event["progress"] = dict(self.execution_progress)
            return event
        elif command in ("events.cancel", "events.reschedule"):
            fields = ("event_id", "expected_version") + (("at",) if command.endswith("reschedule") else ())
            arguments(raw, fields, fields)
            result = self.store.modify(**raw, cancel=command.endswith("cancel"))
        elif command == "runs.list":
            arguments(raw, ("task", "offset"))
            return self.store.runs(**raw)
        elif command == "runs.show":
            arguments(raw, ("run_id",), ("run_id",))
            return self.store.run(**raw)
        elif command == "runs.output":
            arguments(raw, ("run_id",), ("run_id",))
            return self.store.output(**raw)
        elif command == "recover.list":
            arguments(raw, ("offset",))
            return [{"event_id": event["event_id"], "status": event["status"], "last_error_code": event["last_error_code"]}
                    for status in ("uncertain", "blocked", "failed", "dead_letter") for event in self.store.events(status, **raw)]
        elif command == "recover.retry":
            arguments(raw, ("event_id", "confirmed"), ("event_id",))
            if "confirmed" in raw and type(raw["confirmed"]) is not bool:
                raise RuntimeContractError("confirmed must be boolean")
            result = self.store.retry(**raw)
        elif command == "agent.restart":
            arguments(raw, ("task_id", "workflow_id", "reason", "reset_circuit"), ("task_id",))
            result = self.store.request_restart(**raw)
            self.pending_restart = result
            if self.active_dispatch and self.active_scope == (result["workflow_id"], result["task_id"]):
                self.active_dispatch.cancel()
        elif command == "agent.show":
            arguments(raw, ("restart_id",), ("restart_id",))
            return self.store.restart_job(raw["restart_id"])
        else:
            raise RuntimeContractError("Unknown command")
        self.changed.set()
        return result

    async def dispatch_async(self, command, raw):
        if command.startswith("lifecycle."):
            return await self.lifecycle_api(command, raw)
        if command.startswith("wakebinding."):
            return await self.wakebinding_api(command, raw)
        return await self.dispatch_control(command, raw)

    def _lifecycle_target(self, raw, *, require_reason):
        fields = (*LIFECYCLE_TARGET_FIELDS, "lifecycle_protocol_version") + (("reason",) if require_reason else ())
        arguments(raw, fields, fields)
        if raw.get("lifecycle_protocol_version") != EXPECTED_LIFECYCLE_PROTOCOL_VERSION:
            raise ConflictError("Runtime 生命周期协议版本不匹配；拒绝自动操作")
        if (raw["runtime_instance"] != self.owner
                or raw["runtime_build_id"] != self.runtime_build_id
                or raw["runtime_version"] != __version__
                or raw["native_protocol_version"] != EXPECTED_NATIVE_PROTOCOL_VERSION
                or type(raw["runtime_pid"]) is not int or raw["runtime_pid"] != self.runtime_pid
                or raw["runtime_identity"] != self.runtime_identity):
            raise ConflictError("Runtime 实例已变化；拒绝在另一个实例上执行生命周期操作")
        if require_reason:
            bounded_text(raw["reason"], 256, "reason")

    def _active_runs(self):
        rows = self.store.connection.execute(
            """SELECT r.run_id, r.status, r.dispatch_intent_at, e.workflow_id, e.task_id, e.event_id
               FROM runs r JOIN events e ON e.event_id=r.event_id
               WHERE r.status IN ('leased','running') ORDER BY r.started_at""").fetchall()
        return [{"run_id": row["run_id"], "status": row["status"],
                 "dispatch_intent_at": row["dispatch_intent_at"],
                 "workflow_id": row["workflow_id"], "task_id": row["task_id"],
                 "event_id": row["event_id"],
                 "safe_to_abort": row["status"] == "leased" and row["dispatch_intent_at"] is None}
                for row in rows]

    def _live_frontends(self):
        from .frontend import Frontend, alive
        rows = []
        for record in Frontend(self.store).records():
            if record.get("status") == "released":
                continue
            guardian_alive = alive(record.get("guardian_pid"), record.get("guardian_identity"))
            omp_alive = alive(record.get("omp_pid"), record.get("omp_identity"))
            # A launch between frontend.begin and the guardian's admitted start
            # has no PID yet. Treat a recent admission window as activity so a
            # restart cannot race it; stale starting records age out.
            starting = False
            if record.get("status") == "starting":
                try:
                    age = (self.store.clock() - parse_timestamp(record.get("updated_at") or record["created_at"])).total_seconds()
                    starting = age < 60
                except (KeyError, TypeError, ValueError):
                    starting = True
            if guardian_alive or omp_alive or starting:
                rows.append({"conversation_id": record["conversation_id"],
                             "workflow_id": record["workflow_id"], "task_id": record["task_id"],
                             "status": record.get("status"), "guardian_alive": guardian_alive,
                             "omp_alive": omp_alive, "starting_window": starting})
        return rows

    def _event_counts(self):
        rows = self.store.connection.execute(
            "SELECT status,count(*) FROM events WHERE status IN ('queued','waiting_anchor') GROUP BY status").fetchall()
        counts = {row[0]: row[1] for row in rows}
        return {"queued_events": counts.get("queued", 0),
                "waiting_anchor_events": counts.get("waiting_anchor", 0)}

    def _lifecycle_blockers(self):
        blockers = []
        for run in self._active_runs():
            if run["safe_to_abort"]:
                continue
            blockers.append({"kind": "background_run", "blocking": "active_dispatch", **run,
                             "safe_handling": "等待该后台运行自然结束；用 events show/status 检查进度，"
                                              "不要强制取消或重发不确定的模型请求。"})
        for frontend in self._live_frontends():
            blockers.append({"kind": "frontend", "blocking": "live_session", **frontend,
                             "safe_handling": "在对应终端正常退出该会话（Ctrl+D 或 /exit），然后重试。"})
        if self.agent_health == "restarting":
            blockers.append({"kind": "agent_restart", "blocking": "agent_restart",
                             "safe_handling": "等待当前 agent restart 探测结束；它不会重发旧 prompt。"})
        return blockers

    def _lifecycle_result(self, accepted, blockers, *, already=False):
        return {"api_version": 1, "accepted": accepted, "draining": self.draining,
                "stopping": self.stopping, "already_draining": already, "blockers": blockers,
                **self._event_counts(),
                "runtime_instance": self.owner, "runtime_build_id": self.runtime_build_id,
                "runtime_version": __version__, "native_protocol_version": EXPECTED_NATIVE_PROTOCOL_VERSION,
                "lifecycle_protocol_version": EXPECTED_LIFECYCLE_PROTOCOL_VERSION}

    async def quiesce(self, raw):
        """Atomically stop admitting work and prove that no execution is in flight."""
        self._lifecycle_target(raw, require_reason=True)
        if self.lifecycle_lock is None:
            self.lifecycle_lock = asyncio.Lock()
        async with self.lifecycle_lock:
            if self.draining:
                return self._lifecycle_result(True, [], already=True)
            self.draining = True
            try:
                blockers = self._lifecycle_blockers()
                if blockers:
                    self.draining = False
                    return self._lifecycle_result(False, blockers)
                task = self.active_dispatch
                active_run_id = self.active_run_id
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
                    except TimeoutError:
                        self.draining = False
                        return self._lifecycle_result(False, [{
                            "kind": "background_run", "blocking": "drain_timeout",
                            "run_id": active_run_id, "status": "cancelling",
                            "safe_handling": "后台任务未在限时内停止；已取消自动重启，请检查 daemon 日志。"}])
                if active_run_id is not None:
                    # Re-check after cancellation: dispatch intent may have been
                    # committed just before the task observed cancellation.
                    raced = self.store.run(active_run_id)
                    if raced["dispatch_intent_at"] is not None or raced["status"] == "uncertain":
                        self.draining = False
                        return self._lifecycle_result(False, [{
                            "kind": "background_run", "blocking": "dispatch_race",
                            "run_id": active_run_id, "status": raced["status"],
                            "dispatch_intent_at": raced["dispatch_intent_at"],
                            "event_id": raced["event_id"],
                            "safe_handling": "停止请求与后台派发发生竞争，结果已标记 uncertain；"
                                             "请检查 events show 的外部副作用，不自动重发。"}])
                # Cancel-safe leased runs are returned to the durable queue, not
                # completed or failed, even if their dispatch task was not owned
                # by this loop iteration.
                for run in self._active_runs():
                    if run["safe_to_abort"]:
                        try:
                            self.store.abort_claim(run["run_id"], "runtime_drain", keep_event=True)
                        except ConflictError:
                            pass
                blockers = self._lifecycle_blockers()
                if blockers:
                    self.draining = False
                    return self._lifecycle_result(False, blockers)
            except BaseException:
                self.draining = False
                raise
            return self._lifecycle_result(True, [])

    async def lifecycle_api(self, command, raw):
        if command == "lifecycle.inspect":
            arguments(raw, ())
            return {"api_version": 1, "draining": self.draining, "stopping": self.stopping,
                    "active_runs": self._active_runs(), "live_frontends": self._live_frontends(),
                    "blockers": self._lifecycle_blockers(), **self._event_counts(),
                    "runtime_instance": self.owner, "runtime_build_id": self.runtime_build_id,
                    "runtime_version": __version__, "native_protocol_version": EXPECTED_NATIVE_PROTOCOL_VERSION,
                    "lifecycle_protocol_version": EXPECTED_LIFECYCLE_PROTOCOL_VERSION}
        if command == "lifecycle.resume":
            self._lifecycle_target(raw, require_reason=True)
            if self.stop_timer is not None:
                self.stop_timer.cancel()
                self.stop_timer = None
            self.stopping = False
            self.draining = False
            return {"api_version": 1, "draining": False, "stopping": False, "resumed": True}
        if command == "lifecycle.drain":
            return await self.quiesce(raw)
        if command == "lifecycle.shutdown":
            result = await self.quiesce(raw)
            if result["accepted"]:
                # Let this response reach the client before the server closes.
                self.stopping = True
                self.stop_timer = asyncio.get_running_loop().call_later(0.2, self.stop_event.set)
                result["stopping"] = True
            return result
        raise RuntimeContractError("Unknown lifecycle command")

    async def wakebinding_api(self, command, raw):
        from .frontend import Frontend, alive
        from .wakebinding import WakeBindings
        operation = command.removeprefix("wakebinding.")
        native = any(key in raw for key in ("conversation_id", "generation", "owner_token"))
        if native:
            arguments(raw, ("conversation_id", "generation", "owner_token", "parameters"),
                      ("conversation_id", "generation", "owner_token", "parameters"))
            owner = {key: raw[key] for key in ("conversation_id", "generation", "owner_token")}
            record = Frontend(self.store).authenticate(**owner)
            if record["status"] not in ("active", "idle") or not alive(record.get("omp_pid"), record.get("omp_identity")):
                raise ConflictError("WakeBinding requires a live, attested native owner")
            if not isinstance(raw["parameters"], dict):
                raise RuntimeContractError("WakeBinding parameters must be an object")
            parameters = dict(raw["parameters"])
            workflow_id, task_id = record["workflow_id"], record["task_id"]
            conversation_id = record["conversation_id"]
            allowed = {
                "create": {"todo_id", "name", "mode", "time_source", "offset_seconds",
                           "eligibility", "reminder_at", "resume_policy", "authorized_by_user"},
                "list": {"status", "offset"}, "show": {"binding_id"},
                "update": {"binding_id", "expected_version", "mode", "time_source", "offset_seconds",
                           "eligibility", "reminder_at", "resume_policy"},
                "cancel": {"binding_id", "expected_version"},
                "sync": {"binding_id"},
                "status": set(),
            }
            if operation not in allowed:
                raise RuntimeContractError("Unknown WakeBinding operation")
            required = {
                "create": {"todo_id"}, "show": {"binding_id"},
                "update": {"binding_id", "expected_version"},
                "cancel": {"binding_id", "expected_version"},
                "list": set(), "sync": set(), "status": set(),
            }[operation]
            arguments(parameters, allowed[operation], required)
            if operation == "create" and parameters.get("authorized_by_user") is not True:
                raise RuntimeContractError("Creating a WakeBinding requires explicit user authorization")
        else:
            allowed = {
                "create": {"workflow_id", "task_id", "todo_id", "name", "mode", "time_source",
                           "offset_seconds", "eligibility", "reminder_at", "resume_policy"},
                "list": {"workflow_id", "task_id", "status", "offset"},
                "show": {"workflow_id", "task_id", "binding_id"},
                "update": {"workflow_id", "task_id", "binding_id", "expected_version", "mode",
                           "time_source", "offset_seconds", "eligibility", "reminder_at",
                           "resume_policy"},
                "cancel": {"workflow_id", "task_id", "binding_id", "expected_version"},
                "sync": {"workflow_id", "task_id", "binding_id"},
                "status": {"workflow_id", "task_id"},
            }
            if operation not in allowed:
                raise RuntimeContractError("Unknown WakeBinding operation")
            required = {
                "create": {"workflow_id", "todo_id"}, "list": {"workflow_id"},
                "show": {"workflow_id", "binding_id"},
                "update": {"workflow_id", "binding_id", "expected_version"},
                "cancel": {"workflow_id", "binding_id", "expected_version"},
                "sync": {"workflow_id"}, "status": {"workflow_id"},
            }[operation]
            arguments(raw, allowed[operation], required)
            parameters = {key: value for key, value in raw.items() if key not in ("workflow_id", "task_id")}
            workflow_id = raw["workflow_id"]
            task_id = raw.get("task_id", "training")
            conversation_id = None
            if operation == "create":
                parameters["authorized_by_user"] = True
        service = WakeBindings(self.store)
        if operation == "create":
            self.wake_reconciler = service
            result = await service.create(workflow_id=workflow_id, task_id=task_id,
                                          owner_conversation_id=conversation_id, **parameters)
        elif operation == "list":
            result = {"api_version": 1, "bindings": await service.list(
                workflow_id, task_id, parameters.get("status"), parameters.get("offset", 0))}
        elif operation == "show":
            result = {"api_version": 1, "binding": await service.show(
                parameters["binding_id"], workflow_id, task_id)}
        elif operation == "update":
            identity = parameters.pop("binding_id")
            version = parameters.pop("expected_version")
            result = {"api_version": 1, "binding": await service.update(
                identity, version, workflow_id=workflow_id, task_id=task_id, **parameters)}
        elif operation == "cancel":
            result = {"api_version": 1, "binding": await service.cancel(
                parameters["binding_id"], parameters["expected_version"],
                workflow_id=workflow_id, task_id=task_id)}
        elif operation == "sync":
            result = await service.sync(workflow_id=workflow_id, task_id=task_id,
                                        binding_id=parameters.get("binding_id"))
        else:
            rows = await service.list(workflow_id, task_id, offset=0)
            result = {"api_version": 1, "bindings": rows, "counts": self.store.wakebinding_counts(),
                      "source": service.source_status(workflow_id)}
        self.wake_wakeup.set()
        self.changed.set()
        return result

    async def sync_loop(self):
        from . import git_sync
        due = 0
        previous_settings = None
        while not self.stop_event.is_set():
            interval = 60
            values = None
            try:
                values = git_sync.settings(self.config)
                if values is None and time.monotonic() >= due:
                    await asyncio.to_thread(git_sync.ensure_local, self.config)
                    values = git_sync.settings(self.config)
                if values != previous_settings:
                    due = 0
                    previous_settings = values
                if values and values["enabled"]:
                    interval = values["interval_seconds"]
                    if time.monotonic() >= due or self.sync_wakeup.is_set():
                        self.sync_wakeup.clear()
                        await asyncio.to_thread(git_sync.cycle, self.config)
                        due = time.monotonic() + interval
            except (RuntimeContractError, OSError):
                # Sync failures remain independent of foreground queue/model execution.
                due = time.monotonic() + interval
                if values is None:
                    try:
                        git_sync.write_json(git_sync.local_root(self.config) / "status.json", {
                            "state": "error", "completed_at": git_sync.stamp(),
                            "error": "Local Git archive initialization failed; check Git availability and the dedicated directory"})
                    except (RuntimeContractError, OSError):
                        pass
            try:
                await asyncio.wait_for(self.sync_wakeup.wait(), timeout=5)
                if not values or not values["enabled"]:
                    self.sync_wakeup.clear()
            except TimeoutError:
                pass

    async def dispatch_event(self, event):
        run_id = event["run_id"]
        payload = event.get("payload") or {}
        if payload.get("native_wakeup") and payload.get("intent") == "reminder":
            # Pure reminders never start a model. claim_due excludes them; if a
            # stale or externally created claim reaches here, release it and let
            # the durable reminder projection deliver without ever creating a
            # prompt. The event stays queued so no schedule is lost.
            try:
                self.store.abort_claim(run_id, "reminder_not_dispatchable", keep_event=True)
            except ConflictError:
                return
            from .inbox import Inbox
            Inbox(self.store).project_due(complete=True)
            return
        if payload.get("wake_binding_id"):
            from .wakebinding import WakeBindings
            service = self.wake_reconciler or WakeBindings(self.store)
            try:
                result = await service.verify(event)
            except (RuntimeContractError, OSError) as error:
                # A CAS/source failure only defers this event. Bindings are put
                # into a bounded reconcile backoff; the Scheduler keeps running.
                code = "binding_verify_conflict" if isinstance(error, ConflictError) else "binding_verify_failed"
                try:
                    self.store.abort_claim(run_id, code, keep_event=True)
                except ConflictError:
                    pass
                service.defer(payload["wake_binding_id"], code, str(error)[:200])
                print(json.dumps({"timestamp_utc": self.store.timestamp(), "component": "scheduler",
                                  "event_id": event["event_id"], "run_id": run_id,
                                  "transition": "blocked", "error_code": code,
                                  "error": type(error).__name__}), flush=True)
                return
            if not result["allowed"]:
                try:
                    self.store.abort_claim(run_id, result["reason"],
                                           keep_event=result["action"] in ("reschedule", "defer"))
                except ConflictError:
                    return
                if result["action"] == "reschedule":
                    try:
                        await service.reconcile_binding(payload["wake_binding_id"], reason="claim_released")
                    except RuntimeContractError:
                        pass
                print(json.dumps({"timestamp_utc": self.store.timestamp(), "component": "scheduler",
                                  "event_id": event["event_id"], "run_id": run_id,
                                  "transition": "blocked", "error_code": result["reason"]}), flush=True)
                return
        agent = OmpRpc(self.config, self.lock_fds, self.config_path)
        terminal = False
        failure_code = "dispatch_interrupted"
        failure_message = None
        retryable = False
        interrupted = False
        stage = "workflow"
        self.execution_progress = {"run_id": run_id, "phase": "starting", "tool_calls": 0,
                                   "started_at": self.store.timestamp(), "last_activity_at": self.store.timestamp()}
        def progress(kind, tool):
            state = self.execution_progress
            state["last_activity_at"] = self.store.timestamp()
            state["phase"] = "tool" if kind == "tool_execution_start" else "model"
            if kind == "tool_execution_start":
                state["tool_calls"] += 1
                state["tool"] = tool if tool in ("read", "write", "bash", "todo", "glob", "grep") else "external-tool"
        try:
            trusted = self.store.metadata("native-wakeup-config:v1:" + event["event_id"]) if event["payload"].get("native_wakeup") else None
            if event["payload"].get("native_wakeup") and not trusted:
                from .errors import WakeupSnapshotError
                raise WakeupSnapshotError()
            # Validate the frozen event settings directly. Loading mutable user
            # settings first can block an authorized event after later edits,
            # even though those settings will never be used by this wakeup.
            workflow = self.provider.describe(event["workflow_id"], settings=trusted["settings"] if trusted else None)
            binding_event = event
            if event["payload"].get("native_wakeup"):
                if trusted["workflow_revision"] != workflow.revision:
                    raise RuntimeContractError("Workflow revision changed after native scheduling; inspect the event and start a new conversation")
                binding_event = {**event, "task_id": trusted["binding_task_id"]}
            binding = self.store.bind(binding_event, workflow)
            policy_key = "prompt-policy:v1:" + binding["binding_id"]
            policy = self.store.metadata(policy_key)
            from .prompt_composer import VERSION
            current_policy = {"composer_version": VERSION, "base": workflow.base}
            if binding["session_file"] and policy != current_policy:
                raise SessionConflictError("session_prompt_changed")
            with self.store.transaction() as db:
                self.store._set_metadata(db, policy_key, current_policy)
            from .prompt_composer import persona_digest
            persona_key = "persona-binding:v1:" + binding["binding_id"]
            original_persona = self.store.metadata(persona_key)
            current_persona = persona_digest(workflow.customization)
            if (original_persona is not None and original_persona != current_persona) or (original_persona is None and binding["session_file"] and workflow.customization.get("persona")):
                raise RuntimeContractError("Persona changed; use a new task/session instead of changing identity inside this conversation")
            with self.store.transaction() as db:
                self.store._set_metadata(db, persona_key, current_persona)
            binding["customization"] = workflow.customization
            binding["builtin_skills"] = workflow.builtin_skills
            binding["base"] = workflow.base
            binding["memory_enabled"] = workflow.memory_enabled
            binding["memory_scope"] = {"workflow_id": event["workflow_id"], "task_id": event["task_id"]}
            envelope = {
                "event_id": event["event_id"], "run_id": run_id, "workflow_id": event["workflow_id"],
                "task_id": event["task_id"], "dedupe_key": event["dedupe_key"],
                "scheduled_at": event["not_before_utc"], "actual_started_at": self.store.timestamp(),
                "reason": event["reason"], "payload": event["payload"],
                "missed_by_seconds": max(0, (self.store.clock() - parse_timestamp(event["not_before_utc"])).total_seconds()),
                "runtime_cli_contract_version": 1,
                "available_tools": [{"name": "oicoach-runtime", "contract_version": 1,
                                     "argv_prefix": [sys.executable, "-m", "oicoach_runtime"],
                                     "config_path": str(self.config_path) if self.config_path else None,
                                     "commands": ["schedule", "events", "runs", "status"]}],
            }
            if workflow.memory_enabled:
                try:
                    binding["memory_context"] = Memory(self.store).context(event["workflow_id"], event["task_id"])
                except (RuntimeContractError, OSError, sqlite3.Error):
                    raise AgentError("Runtime Memory recall failed before dispatch", code="memory_recall_failed") from None
                envelope["memory"] = {key: value for key, value in binding["memory_context"].items() if key != "notes"}
                envelope["memory"]["location"] = "conversation.paveway-memory-context"
                envelope["available_tools"][0]["commands"].extend(["memory", "history"])
            binding["workflow_instructions"], prompt = workflow.render_parts("wake", envelope)
            if trusted:
                binding["workflow_instructions"] = trusted["workflow_instructions"]
            self.agent_health = "starting"
            stage = "startup"
            pid, identity = await agent.start(binding, run_id)
            self.store.record_process(run_id, pid, identity)
            await agent.ready()
            stage = "session"
            state = await agent.state()
            if state.get("isSettled") is not True:
                raise ConflictError("Agent session is not settled before dispatch")
            self.store.record_session(run_id, state)
            stage = "dispatch"
            # This commit MUST happen before the first write of the prompt frame.
            self.store.dispatch(run_id)
            self.agent_health = "running"
            result = await agent.prompt(run_id, prompt,
                                        lambda: self.store.evidence(run_id, acknowledged=True),
                                        lambda outcome: self.store.evidence(run_id, result=outcome), progress)
            state = await agent.state()
            if state.get("isSettled") is not True:
                raise AgentError("Session became active after completion")
            self.store.record_session(run_id, state)
            path = Path(state["sessionFile"])
            if not path.is_file():
                raise AgentError("Completed session file was not persisted")
            output = {"status": "unavailable", "text": None, "truncated": False}
            if result == "completed" and getattr(agent, "agent_invoked", None) is not False:
                output = agent.assistant_output() or output
                try:
                    response = await asyncio.wait_for(agent.command("get_last_assistant_text"), min(5, self.config.agent.ack_timeout_seconds))
                    text = response.get("text")
                    if output["status"] == "unavailable" and isinstance(text, str):
                        encoded = text.encode("utf-8")
                        output = {"status": "available", "text": encoded[:16384].decode("utf-8", errors="ignore"),
                                  "truncated": len(encoded) > 16384}
                except AgentError as error:
                    if error.code != "agent_output_unsupported":
                        raise  # Persistence/transport errors still invalidate success.
                except (TimeoutError, UnicodeError):
                    # A missing optional result never replaces completion evidence.
                    pass
            # Disposal can still fail while flushing a session. Preserve intent until
            # the settled process has exited cleanly, including persistence at EOF.
            await agent.stop()
            if agent.process.returncode != 0:
                raise AgentError("OMP did not exit cleanly after settling", code="agent_shutdown_failed")
            self.store.finish(run_id, "succeeded" if result == "completed" else "failed",
                              result=result, settled=True, error=None if result == "completed" else (getattr(agent, "failure_code", None) or "agent_turn_failed"),
                              error_message=getattr(agent, "failure_message", None),
                              exit_code=agent.process.returncode, record_failure=result != "completed", output=output)
            terminal = True
        except asyncio.CancelledError:
            interrupted = True
            failure_code = "agent_restart_requested" if self.pending_restart and self.active_scope == (self.pending_restart["workflow_id"], self.pending_restart["task_id"]) else "daemon_shutdown"
            raise
        except RuntimeContractError as error:
            failure_code = error.code
            if isinstance(error, SessionConflictError):
                failure_message = str(error)
            retryable = isinstance(error, AgentError) and stage in ("startup", "session")
        except (OSError, TimeoutError, ValueError):
            # Errors contain no prompt/provider text. Unknown post-intent outcome is never retried.
            retryable = stage in ("startup", "session")
            failure_code = "agent_start_failed" if retryable else "dispatch_interrupted"
        finally:
            try:
                await agent.stop(abort=not terminal)
            finally:
                if not terminal:
                    run = self.store.run(run_id)
                    restart_scope = bool(self.pending_restart and self.active_scope == (
                        self.pending_restart["workflow_id"], self.pending_restart["task_id"]))
                    graceful = self.stop_event.is_set() or self.draining or restart_scope
                    if graceful and run["status"] == "leased" and run["dispatch_intent_at"] is None:
                        # No prompt was sent: return the durable event to the queue
                        # instead of reporting a failure or losing the schedule.
                        code = "runtime_drain" if self.draining and not self.stop_event.is_set() else "daemon_shutdown"
                        self.store.abort_claim(run_id, code, keep_event=True)
                        interrupted, retryable = True, False
                        failure_code, failure_message = code, None
                    else:
                        if self.stop_event.is_set() or restart_scope:
                            interrupted, retryable = True, False
                            failure_code = "daemon_shutdown" if self.stop_event.is_set() else "agent_restart_requested"
                            failure_message = None
                        self.store.finish(run_id, "uncertain" if run["dispatch_intent_at"] else ("failed" if retryable else "blocked"),
                                          error=failure_code, error_message=failure_message,
                                          exit_code=agent.process.returncode if agent.process else None,
                                          automatic_retry=retryable and not run["dispatch_intent_at"],
                                          record_failure=not interrupted and (retryable or bool(run["dispatch_intent_at"])))
                self.agent_health = "idle" if terminal else "degraded"
                self.execution_progress = None
                run = self.store.run(run_id)
                print(json.dumps({"timestamp_utc": self.store.timestamp(), "component": "scheduler", "event_id": event["event_id"],
                                  "run_id": run_id, "transition": run["status"], "error_code": run["error_code"]}), flush=True)

    async def _project_bound_reminders(self, service):
        from .inbox import Inbox
        try:
            await Inbox(self.store).project_bound_due(service)
        except Exception as error:
            # One broken binding must not stop the Scheduler. The durable event
            # stays queued and the binding's last_attempt_at backoff applies.
            print(json.dumps({"timestamp_utc": self.store.timestamp(), "component": "scheduler",
                              "transition": "reminder_projection_failed",
                              "error_code": type(error).__name__}), flush=True)

    def _start_bound_projection(self):
        if self.bound_task is not None and not self.bound_task.done():
            return
        service = self.wake_reconciler
        if service is None:
            from .wakebinding import WakeBindings
            service = self.wake_reconciler = WakeBindings(self.store)
        self.bound_task = asyncio.create_task(self._project_bound_reminders(service))

    async def schedule_loop(self):
        while not self.stop_event.is_set():
            if self.draining:
                try:
                    await asyncio.wait_for(self.changed.wait(), self.config.server.poll_interval_seconds)
                except TimeoutError:
                    pass
                continue
            from .inbox import Inbox
            Inbox(self.store).project_due(complete=True)
            self._start_bound_projection()
            Inbox(self.store).collect()
            self.changed.clear()
            if self.pending_restart:
                await self.restart_agent(self.pending_restart)
                self.pending_restart = None
                continue
            service = self.wake_reconciler
            if service is None:
                from .wakebinding import WakeBindings
                service = self.wake_reconciler = WakeBindings(self.store)
            if self.wake_guard_task is None or self.wake_guard_task.done():
                if self.wake_guard_task is not None:
                    try:
                        self.wake_guard_task.result()
                    except Exception:
                        pass
                # Do not block the foreground queue on MCP I/O. The authoritative
                # gate is repeated in dispatch_event immediately before dispatch.
                self.wake_guard_task = asyncio.create_task(service.reconcile_due())
            if self.draining:
                continue
            event = self.store.claim_due(self.owner)
            if event:
                native = self.store.metadata("native-wakeup-config:v1:" + event["event_id"]) if event["payload"].get("native_wakeup") else None
                self.active_scope = (event["workflow_id"], native["binding_task_id"] if native else event["task_id"])
                self.active_run_id = event["run_id"]
                self.active_dispatch = asyncio.create_task(self.dispatch_event(event))
                try:
                    while not self.active_dispatch.done():
                        Inbox(self.store).project_due(complete=True)
                        self._start_bound_projection()
                        await asyncio.wait((self.active_dispatch,), timeout=self.config.server.poll_interval_seconds)
                    await asyncio.shield(self.active_dispatch)
                except asyncio.CancelledError:
                    if self.stop_event.is_set() or self.draining:
                        if not self.active_dispatch.done() and not self.active_dispatch.cancelling():
                            self.active_dispatch.cancel()
                        await asyncio.gather(self.active_dispatch, return_exceptions=True)
                        if self.stop_event.is_set():
                            raise
                        continue
                    if self.pending_restart is None:
                        raise
                finally:
                    self.active_dispatch = None
                    self.active_scope = None
                    self.active_run_id = None
                continue
            try:
                await asyncio.wait_for(self.changed.wait(), self.config.server.poll_interval_seconds)
            except TimeoutError:
                pass

    async def wakebinding_loop(self):
        from .wakebinding import WakeBindings
        service = self.wake_reconciler or WakeBindings(self.store)
        self.wake_reconciler = service
        while not self.stop_event.is_set():
            self.wake_wakeup.clear()
            try:
                await service.reconcile_all(limit=10, reason="periodic")
            except Exception:
                # Reconciliation is independent of the foreground queue. A broken
                # binding must not stop the runtime or other reminders.
                pass
            try:
                await asyncio.wait_for(self.wake_wakeup.wait(), 60)
            except TimeoutError:
                pass

    async def restart_agent(self, job):
        adapter = OmpRpc(self.config, self.lock_fds, self.config_path)
        status, error = "failed", "agent_restart_failed"
        self.store.update_restart(job["restart_id"], "running")
        self.agent_health = "restarting"
        try:
            binding = self.store.binding_for_task(job["task_id"], job["workflow_id"])
            workflow = self.provider.describe(job["workflow_id"])
            if binding["workspace"] != str(workflow.workspace) or binding["workflow_revision"] != workflow.revision:
                raise ConflictError("Bound workflow or workspace changed")
            if binding["session_file"] and not Path(binding["session_file"]).is_file():
                raise ConflictError("Bound session is missing; restart cannot create a replacement")
            from .prompt_composer import VERSION
            if binding["session_file"] and self.store.metadata("prompt-policy:v1:" + binding["binding_id"]) != {
                    "composer_version": VERSION, "base": workflow.base}:
                raise SessionConflictError("session_prompt_changed")
            binding["customization"] = workflow.customization
            binding["builtin_skills"] = workflow.builtin_skills
            binding["base"] = workflow.base
            await adapter.start(binding, job["restart_id"])
            await adapter.ready()
            state = await adapter.state()
            path = self.store._session_file(state.get("sessionFile"), Path(binding["session_dir"]))
            if state.get("isSettled") is not True or not isinstance(state.get("sessionId"), str):
                raise AgentError("Restarted agent did not report a settled session")
            if binding["session_file"] and (str(path) != binding["session_file"] or state["sessionId"] != binding["session_id"]):
                raise ConflictError("Restarted agent returned another session identity")
            await adapter.stop()
            if adapter.process.returncode != 0:
                raise AgentError("Restarted agent failed during disposal")
            if job["reset_circuit"]:
                self.store.reset_supervisor()
            status, error = "succeeded", None
        except asyncio.CancelledError:
            status, error = "interrupted", "daemon_shutdown"
            raise
        except (RuntimeContractError, OSError, TimeoutError, ValueError):
            self.store.supervisor_failure(error)
        finally:
            try:
                await adapter.stop(abort=status != "succeeded")
            finally:
                self.store.update_restart(job["restart_id"], status, error)
                self.agent_health = "idle" if status == "succeeded" else "degraded"

    async def dispatch_control(self, command, raw):
        if command in ("frontend.finish", "frontend.recover"):
            fields = ("conversation_id", "generation", "owner_token", "exit_code") if command == "frontend.finish" else ("conversation_id",)
            arguments(raw, fields, fields)
            def release():
                from .frontend import Frontend
                store = Store(self.config, clock=self.store.clock)
                try:
                    return getattr(Frontend(store), command.split(".")[1])(**raw)
                finally:
                    store.close()
            return await asyncio.to_thread(release)
        return self.dispatch_api(command, raw)

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        if len(self.clients) >= 64:
            writer.close()
            await writer.wait_closed()
            return
        self.clients.add(task)
        try:
            await handle_connection(reader, writer, self.dispatch_async, lambda: not self.stop_event.is_set())
        finally:
            self.clients.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def serve(self):
        enable_subreaper()
        ensure_private_directory(self.config.state_dir)
        ensure_private_directory(self.config.state_dir / "sessions")
        socket_path = Path(self.config.server.socket_path)
        ensure_private_directory(socket_path.parent)
        with ExitStack() as stack:
            state_lock = InstanceLock(self.config.state_dir / "runtime.lock")
            stack.callback(state_lock.close)
            socket_lock = InstanceLock(socket_path.with_name(socket_path.name + ".lock"))
            stack.callback(socket_lock.close)
            self.lock_fds = (state_lock.fd, socket_lock.fd)
            initialize_database(self.config.database_path)
            self.store = Store(self.config)
            stack.callback(self.store.close)
            self.store.reconcile()
            from .frontend import Frontend
            Frontend(self.store).reconcile()
            from .wakebinding import WakeBindings
            self.wake_reconciler = WakeBindings(self.store)
            if socket_path.exists() or socket_path.is_symlink():
                info = socket_path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise ConflictError("Refusing to remove a non-socket or unowned control path")
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(str(socket_path))
                    except (ConnectionRefusedError, FileNotFoundError):
                        socket_path.unlink(missing_ok=True)
                    else:
                        raise ConflictError("Control socket already has a live listener")
            previous_umask = os.umask(0o077)
            try:
                server = await asyncio.start_unix_server(self.connection, path=socket_path, limit=MAX_REQUEST)
                socket_path.chmod(0o600)
            finally:
                os.umask(previous_umask)
            inode = socket_path.stat().st_ino
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.stop_event.set)
            scheduler = asyncio.create_task(self.schedule_loop())
            synchronizer = asyncio.create_task(self.sync_loop())
            reconciler = asyncio.create_task(self.wakebinding_loop())
            waiter = asyncio.create_task(self.stop_event.wait())
            print(json.dumps({"component": "daemon", "transition": "ready", "api_version": 1}), flush=True)
            try:
                done, _ = await asyncio.wait((scheduler, waiter), return_when=asyncio.FIRST_COMPLETED)
                if scheduler in done:
                    scheduler.result()  # Unexpected failure must fail serve, not leave a dead scheduler behind.
            finally:
                self.stop_event.set()
                server.close()
                await server.wait_closed()
                for client in list(self.clients):
                    client.cancel()
                await asyncio.gather(*self.clients, return_exceptions=True)
                scheduler.cancel()
                synchronizer.cancel()
                reconciler.cancel()
                waiter.cancel()
                if self.wake_guard_task is not None:
                    self.wake_guard_task.cancel()
                if self.bound_task is not None:
                    self.bound_task.cancel()
                await asyncio.gather(scheduler, synchronizer, reconciler, waiter,
                                     *([self.wake_guard_task] if self.wake_guard_task else []),
                                     *([self.bound_task] if self.bound_task else []),
                                     return_exceptions=True)
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
                if socket_path.exists() and socket_path.lstat().st_ino == inode:
                    socket_path.unlink()
