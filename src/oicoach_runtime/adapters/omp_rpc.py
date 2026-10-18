"""OMP protocol v1 over bounded NDJSON, with explicit completion and settlement."""

import asyncio
import json
import os
import signal
import sys
import uuid

from ..errors import AgentError
from ..processes import process_identity
from ..child import cleanup_group

MAX_FRAME = 1048576


class OmpRpc:
    def __init__(self, config, lock_fds=(), config_path=None):
        self.config = config
        self.lock_fds = tuple(lock_fds)
        self.config_path = config_path
        self.process = None
        self.stderr_task = None
        self.stderr_bytes = 0
        self.exit_event = asyncio.Event()
        self.exit_task = None
        self.process_fd = None
        self.stop_task = None

    async def start(self, binding, run_id):
        if self.process is not None and self.process.returncode is None:
            raise AgentError("Managed process is already running")
        self.exit_event.clear()
        self.stop_task = None
        agent = self.config.agent
        argv = [agent.executable, "--cwd", binding["workspace"], "--session-dir", binding["session_dir"],
                "--mode", "rpc", "--no-ui"]
        if binding["session_file"]:
            argv.extend(["--resume", binding["session_file"]])
        if agent.profile:
            argv.extend(["--profile", agent.profile])
        if agent.model:
            argv.extend(["--model", agent.model])
        for option in ("tools", "extensions", "skills", "rules"):
            if not getattr(agent, option + "_enabled"):
                argv.append("--no-" + option)
        environment = dict(os.environ, OICOACH_RUN_ID=run_id, OICOACH_LOCK_FDS=",".join(str(fd) for fd in self.lock_fds))
        if self.config_path:
            environment["OICOACH_CONFIG"] = str(self.config_path)
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "oicoach_runtime.child", str(os.getpid()), *argv,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, pass_fds=self.lock_fds, env=environment, limit=MAX_FRAME)
        try:
            self.process_fd = os.pidfd_open(self.process.pid)
        except ProcessLookupError:
            self.process_fd = None
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        self.exit_task = asyncio.create_task(self._watch_exit())
        return self.process.pid, process_identity(self.process.pid)

    async def _watch_exit(self):
        # Process.wait can wait for pipes retained by orphaned tools. returncode
        # is set by the child watcher as soon as the guardian actually exits.
        while self.process.returncode is None:
            await asyncio.sleep(0.05)
        self.exit_event.set()

    async def _drain_stderr(self):
        # Drain continuously without retaining secrets or unbounded diagnostics in memory.
        while data := await self.process.stderr.read(8192):
            self.stderr_bytes += len(data)

    async def frame(self, timeout):
        reader = asyncio.create_task(self.process.stdout.readline())
        exited = asyncio.create_task(self.exit_event.wait())
        try:
            async with asyncio.timeout(timeout):
                await asyncio.wait((reader, exited), return_when=asyncio.FIRST_COMPLETED)
                if not reader.done():
                    raise AgentError("Managed agent process exited", code="agent_process_exited")
                line = reader.result()
            if not line or not line.endswith(b"\n") or len(line) > MAX_FRAME:
                raise AgentError("Agent EOF, incomplete or oversized RPC frame", code="agent_rpc_eof_or_overflow")
            value = json.loads(line)
            if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                raise AgentError("Invalid agent RPC frame")
            if value["type"] == "rpc_frame_error":
                raise AgentError("Agent reported a transport overflow")
            if value["type"] == "extension_ui_request":
                raise AgentError("Headless run requires human interaction")
            if value["type"] == "notice" and value.get("source") == "session-persistence" and value.get("level") == "error":
                raise AgentError("OMP reported a session persistence failure", code="agent_session_persistence_error")
            return value
        except AgentError:
            raise
        except TimeoutError:
            raise AgentError("Agent RPC timeout", code="agent_rpc_timeout") from None
        except (ValueError, UnicodeError, RecursionError):
            raise AgentError("Invalid JSON frame", code="agent_invalid_frame") from None
        finally:
            reader.cancel()
            exited.cancel()
            await asyncio.gather(reader, exited, return_exceptions=True)

    async def write(self, value):
        encoded = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_FRAME:
            raise AgentError("Outbound RPC frame is too large")
        try:
            self.process.stdin.write(encoded)
            await asyncio.wait_for(self.process.stdin.drain(), self.config.agent.ack_timeout_seconds)
        except (OSError, TimeoutError):
            raise AgentError("Cannot write agent RPC command") from None

    async def ready(self):
        async with asyncio.timeout(self.config.agent.startup_timeout_seconds):
            while True:
                frame = await self.frame(self.config.agent.startup_timeout_seconds)
                if frame["type"] == "ready":
                    if type(frame.get("protocolVersion")) is not int or frame["protocolVersion"] != 1:
                        raise AgentError("Unsupported OMP RPC protocol version")
                    return

    async def state(self):
        return await self.command("get_state")

    async def command(self, command):
        if command not in ("get_state", "get_messages", "get_last_assistant_text"):
            raise AgentError("Unsupported adapter inspection command")
        request_id = str(uuid.uuid4())
        await self.write({"id": request_id, "type": command})
        async with asyncio.timeout(self.config.agent.ack_timeout_seconds):
            while True:
                frame = await self.frame(self.config.agent.ack_timeout_seconds)
                if frame["type"] == "response" and frame.get("id") == request_id:
                    if command == "get_last_assistant_text" and frame.get("command") == command and frame.get("success") is False:
                        raise AgentError("Optional assistant output query is unsupported", code="agent_output_unsupported")
                    if frame.get("command") != command or frame.get("success") is not True or not isinstance(frame.get("data"), dict):
                        raise AgentError("Agent state request failed")
                    return frame["data"]

    async def prompt(self, run_id, message, on_ack, on_result):
        self.agent_invoked = None
        await self.write({"id": run_id, "type": "prompt", "message": message})
        loop = asyncio.get_running_loop()
        ack_deadline = loop.time() + self.config.agent.ack_timeout_seconds
        turn_deadline = loop.time() + self.config.agent.turn_timeout_seconds
        settle_deadline = None
        acknowledged = False
        result = None
        settled = False
        while True:
            deadline = min(turn_deadline, ack_deadline) if not acknowledged else (settle_deadline or turn_deadline)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AgentError("Agent ack, turn or settle timeout")
            frame = await self.frame(remaining)
            if frame["type"] == "response" and frame.get("id") == run_id:
                if frame.get("command") != "prompt" or frame.get("success") is not True:
                    raise AgentError("Agent rejected prompt; review completion state")
                if not acknowledged:
                    on_ack()
                    acknowledged = True
                data = frame.get("data")
                if isinstance(data, dict) and data.get("agentInvoked") is False:
                    self.agent_invoked = False
                    result = "completed"
                    on_result(result)
                    # Local-only completion still needs explicit settled state.
                    state = await self.state()
                    settled = state.get("isSettled") is True
                    if not settled:
                        settle_deadline = loop.time() + self.config.agent.settle_timeout_seconds
            elif frame["type"] == "prompt_result" and frame.get("id") == run_id:
                self.agent_invoked = frame.get("agentInvoked", True)
                if frame.get("status") not in ("completed", "error", "aborted") or type(frame.get("sessionSettled")) is not bool:
                    raise AgentError("Invalid prompt completion evidence")
                if result is not None:
                    raise AgentError("Duplicate prompt completion evidence")
                result = frame["status"]
                on_result(result)
                settled = frame["sessionSettled"]
                settle_deadline = loop.time() + self.config.agent.settle_timeout_seconds
            elif frame["type"] == "session_settled" and result is not None:
                settled = True
            # Unknown events and unrelated request IDs are forward-compatible, not completion.
            if acknowledged and result is not None and settled:
                return result

    async def stop(self, abort=False):
        if self.process is None:
            return
        if self.stop_task is None:
            self.stop_task = asyncio.create_task(self._stop(abort))
        # A restart can cancel a dispatch while it is already disposing OMP.
        # Keep one cleanup task alive; later callers await the same bounded stop.
        await asyncio.shield(self.stop_task)

    async def _stop(self, abort):
        grace = min(self.config.agent.graceful_stop_seconds, 30)
        try:
            if self.process.returncode is None:
                if abort:
                    try:
                        await self.write({"id": str(uuid.uuid4()), "type": "abort"})
                    except AgentError:
                        pass
                self.process.stdin.close()
                # Keep reading stdout until the process exits; OMP may drain a final frame.
                async def drain():
                    while await self.process.stdout.read(8192):
                        pass
                reader = asyncio.create_task(drain())
                try:
                    await asyncio.wait_for(self.process.wait(), grace)
                except TimeoutError:
                    try:
                        if self.process_fd is not None:
                            signal.pidfd_send_signal(self.process_fd, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(self.process.wait(), 2)
                    except TimeoutError:
                        if self.process_fd is not None:
                            try:
                                signal.pidfd_send_signal(self.process_fd, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        try:
                            await asyncio.wait_for(self.process.wait(), 2)
                        except TimeoutError:
                            raise AgentError("Owned process did not stop after SIGKILL", code="agent_stop_timeout") from None
                finally:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
        finally:
            # The daemon subreaper adopts a killed guardian's tool descendants.
            # Reap only children in this owned group, never arbitrary host PIDs.
            try:
                await asyncio.to_thread(cleanup_group, self.process.pid, self.process.pid)
            finally:
                if self.stderr_task:
                    self.stderr_task.cancel()
                    await asyncio.gather(self.stderr_task, return_exceptions=True)
                if self.exit_task:
                    self.exit_task.cancel()
                    await asyncio.gather(self.exit_task, return_exceptions=True)
                if self.process_fd is not None:
                    os.close(self.process_fd)
                    self.process_fd = None
