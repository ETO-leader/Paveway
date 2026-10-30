"""One foreground dispatch, persistent evidence before every external prompt."""

import asyncio
import json
import os
import signal
import socket
import stat
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from .adapters.omp_rpc import OmpRpc
from .config import ensure_private_directory
from .db import initialize_database
from .errors import AgentError, ConflictError, SessionConflictError, RuntimeContractError
from .ipc import MAX_REQUEST, handle_connection
from .models import parse_timestamp, utc_text
from .memory import Memory, History
from .processes import InstanceLock, enable_subreaper
from .store import Store
from .workflows import WorkflowProvider


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
        self.active_scope = None
        self.pending_restart = None
        self.execution_progress = None
        self.sync_wakeup = asyncio.Event()

    def dispatch_api(self, command, raw):
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
            return {"daemon": "running", "milestone": "M4", "agent_health": health, **supervisor}
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
                "recall": (("workflow_id", "task_id"), ("workflow_id", "task_id")),
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
            workflow = self.provider.describe(event["workflow_id"])
            binding = self.store.bind(event, workflow)
            binding["customization"] = workflow.customization
            binding["builtin_skills"] = workflow.builtin_skills
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
                binding["memory_context"] = Memory(self.store).context(event["workflow_id"], event["task_id"])
                envelope["memory"] = {key: value for key, value in binding["memory_context"].items() if key != "notes"}
                envelope["memory"]["location"] = "system_prompt.runtime_memory_snapshot"
                envelope["available_tools"][0]["commands"].extend(["memory", "history"])
            binding["workflow_instructions"], prompt = workflow.render_parts("wake", envelope)
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
                    if self.stop_event.is_set() or (self.pending_restart and self.active_scope == (self.pending_restart["workflow_id"], self.pending_restart["task_id"])):
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

    async def schedule_loop(self):
        while not self.stop_event.is_set():
            self.changed.clear()
            if self.pending_restart:
                await self.restart_agent(self.pending_restart)
                self.pending_restart = None
                continue
            event = self.store.claim_due(self.owner)
            if event:
                self.active_scope = (event["workflow_id"], event["task_id"])
                self.active_dispatch = asyncio.create_task(self.dispatch_event(event))
                try:
                    await asyncio.shield(self.active_dispatch)
                except asyncio.CancelledError:
                    if self.stop_event.is_set():
                        if not self.active_dispatch.done() and not self.active_dispatch.cancelling():
                            self.active_dispatch.cancel()
                        await asyncio.gather(self.active_dispatch, return_exceptions=True)
                        raise
                    if self.pending_restart is None:
                        raise
                finally:
                    self.active_dispatch = None
                    self.active_scope = None
                continue
            try:
                await asyncio.wait_for(self.changed.wait(), self.config.server.poll_interval_seconds)
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
            binding["customization"] = workflow.customization
            binding["builtin_skills"] = workflow.builtin_skills
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

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        if len(self.clients) >= 64:
            writer.close()
            await writer.wait_closed()
            return
        self.clients.add(task)
        try:
            await handle_connection(reader, writer, self.dispatch_api, lambda: not self.stop_event.is_set())
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
                waiter.cancel()
                await asyncio.gather(scheduler, synchronizer, waiter, return_exceptions=True)
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
                if socket_path.exists() and socket_path.lstat().st_ino == inode:
                    socket_path.unlink()
