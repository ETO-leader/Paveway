"""One foreground dispatch, persistent evidence before every external prompt."""

import asyncio
import json
import os
import signal
import socket
import stat
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

from .adapters.omp_rpc import OmpRpc
from .config import ensure_private_directory
from .db import initialize_database
from .errors import AgentError, ConflictError, RuntimeContractError
from .ipc import MAX_REQUEST, handle_connection
from .models import parse_timestamp, utc_text
from .processes import InstanceLock
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

    def dispatch_api(self, command, raw):
        if command == "status":
            arguments(raw, ())
            return {"daemon": "running", "milestone": "M1", "agent_health": self.agent_health, **self.store.status()}
        if command == "schedule":
            arguments(raw, ("workflow_id", "task_id", "reason", "dedupe_key", "payload", "at", "after_turn", "run_id"),
                      ("workflow_id", "task_id", "reason", "dedupe_key"))
            self.provider.describe(raw["workflow_id"])
            result = self.store.enqueue(**raw)
        elif command == "events.list":
            arguments(raw, ("status", "offset"))
            # Lists are summaries so large private payloads cannot overflow IPC.
            return [{key: event[key] for key in ("event_id", "workflow_id", "task_id", "status", "version", "not_before_utc", "attempt", "run_id", "last_error_code")}
                    for event in self.store.events(**raw)]
        elif command == "events.show":
            arguments(raw, ("event_id",), ("event_id",))
            return self.store.event(**raw)
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
        elif command == "recover.list":
            arguments(raw, ("offset",))
            return [{"event_id": event["event_id"], "status": event["status"], "last_error_code": event["last_error_code"]}
                    for status in ("uncertain", "blocked", "failed", "dead_letter") for event in self.store.events(status, **raw)]
        elif command == "recover.retry":
            arguments(raw, ("event_id", "confirmed"), ("event_id",))
            if "confirmed" in raw and type(raw["confirmed"]) is not bool:
                raise RuntimeContractError("confirmed must be boolean")
            result = self.store.retry(**raw)
        else:
            raise RuntimeContractError("Unknown command")
        self.changed.set()
        return result

    async def dispatch_event(self, event):
        run_id = event["run_id"]
        agent = OmpRpc(self.config, self.lock_fds, self.config_path)
        terminal = False
        try:
            workflow = self.provider.describe(event["workflow_id"])
            binding = self.store.bind(event, workflow)
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
            prompt = workflow.render("wake", envelope)
            self.agent_health = "starting"
            pid, identity = await agent.start(binding, run_id)
            self.store.record_process(run_id, pid, identity)
            await agent.ready()
            state = await agent.state()
            if state.get("isSettled") is not True:
                raise ConflictError("Agent session is not settled before dispatch")
            self.store.record_session(run_id, state)
            # This commit MUST happen before the first write of the prompt frame.
            self.store.dispatch(run_id)
            self.agent_health = "running"
            result = await agent.prompt(run_id, prompt,
                                        lambda: self.store.evidence(run_id, acknowledged=True),
                                        lambda outcome: self.store.evidence(run_id, result=outcome))
            state = await agent.state()
            if state.get("isSettled") is not True:
                raise AgentError("Session became active after completion")
            self.store.record_session(run_id, state)
            path = Path(state["sessionFile"])
            if not path.is_file():
                raise AgentError("Completed session file was not persisted")
            self.store.finish(run_id, "succeeded" if result == "completed" else "failed",
                              result=result, settled=True, error=None if result == "completed" else "agent_turn_failed")
            terminal = True
        except asyncio.CancelledError:
            raise
        except (RuntimeContractError, OSError, TimeoutError, ValueError):
            # Errors contain no prompt/provider text. Unknown post-intent outcome is never retried.
            pass
        finally:
            try:
                await agent.stop(abort=not terminal)
            finally:
                if not terminal:
                    run = self.store.run(run_id)
                    self.store.finish(run_id, "uncertain" if run["dispatch_intent_at"] else "blocked",
                                      error="dispatch_interrupted" if run["dispatch_intent_at"] else "agent_start_or_workflow_failed",
                                      exit_code=agent.process.returncode if agent.process else None)
                elif agent.process:
                    with self.store.transaction() as db:
                        db.execute("UPDATE runs SET agent_exit_code=? WHERE run_id=?", (agent.process.returncode, run_id))
                self.agent_health = "idle" if terminal else "degraded"
                run = self.store.run(run_id)
                print(json.dumps({"timestamp_utc": self.store.timestamp(), "component": "scheduler", "event_id": event["event_id"],
                                  "run_id": run_id, "transition": run["status"], "error_code": run["error_code"]}), flush=True)

    async def schedule_loop(self):
        while not self.stop_event.is_set():
            self.changed.clear()
            event = self.store.claim_due(self.owner)
            if event:
                await self.dispatch_event(event)
                continue
            try:
                await asyncio.wait_for(self.changed.wait(), self.config.server.poll_interval_seconds)
            except TimeoutError:
                pass

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
                waiter.cancel()
                await asyncio.gather(scheduler, waiter, return_exceptions=True)
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
                if socket_path.exists() and socket_path.lstat().st_ino == inode:
                    socket_path.unlink()
