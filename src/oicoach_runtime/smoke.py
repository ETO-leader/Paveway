"""Reproducible real-OMP verification in a disposable workspace and state store."""

import asyncio
import json
import sqlite3
import tempfile
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sys

from .adapters.omp_rpc import OmpRpc
from .config import ServerConfig, config_text, ensure_private_directory
from .db import initialize_database
from .errors import AgentError, RuntimeContractError
from .ipc import request
from .store import now_utc
from .models import utc_text


def message_text(message):
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")


async def inspect_session(config, binding):
    adapter = OmpRpc(config)
    try:
        await adapter.start(binding, str(uuid.uuid4()))
        await adapter.ready()
        state = await adapter.state()
        path = Path(state.get("sessionFile", "")).resolve()
        if not path.is_relative_to(Path(binding["session_dir"]).resolve()) or not state.get("sessionId") or state.get("isSettled") is not True:
            raise AgentError("OMP returned an invalid session identity or unsettled state")
        if binding["session_file"] and (str(path) != binding["session_file"] or state["sessionId"] != binding["session_id"]):
            raise AgentError("OMP did not resume the bound session")
        if state.get("dumpTools"):
            raise AgentError("Verification requires disabled agent tools")
        messages = (await adapter.command("get_messages")).get("messages", [])
        if not isinstance(messages, list):
            raise AgentError("OMP returned invalid session history")
        return state, messages
    finally:
        await adapter.stop()


async def verify_omp(config, *, with_model=False):
    checks = []
    # Reuse the explicitly selected executable/model/profile, never modify its files.
    # Sessions and all workspace/runtime writes are restricted to this temporary tree.
    with tempfile.TemporaryDirectory(prefix="pw-omp-") as temp:
        root = Path(temp)
        state_dir, workflows = root / "state", root / "workflows"
        for directory in (state_dir, state_dir / "sessions", workflows):
            ensure_private_directory(directory)
        isolated = replace(config, state_dir=state_dir, workflow_root=workflows,
                           server=ServerConfig(str(state_dir / "runtime.sock"), 0.05),
                           agent=replace(config.agent, tools_enabled=False, extensions_enabled=False, skills_enabled=False, rules_enabled=False,
                                         turn_timeout_seconds=min(config.agent.turn_timeout_seconds, 90)))
        config_path = root / "config.toml"
        config_path.write_text(config_text(isolated))
        config_path.chmod(0o600)
        probe_dir = state_dir / "sessions/probe"
        ensure_private_directory(probe_dir)
        binding = {"workspace": str(root), "session_dir": str(probe_dir), "session_file": None}
        try:
            state, _ = await inspect_session(isolated, binding)
            checks.append({"check": "rpc_transport", "status": "PASS", "detail": "Ready v1, correlated get_state/get_messages, settled and isolated session identity"})
            checks.append({"check": "model_selected", "status": "PASS" if state.get("model") else "WARN", "detail": "Model selection present" if state.get("model") else "No default model selected"})
        except AgentError as error:
            code = getattr(error, "code", "agent_protocol_error")
            detail_messages = {
                "agent_context_guard_missing": "OMP is ready, but Context Guard (context-window.mjs) was not loaded or initialized",
                "agent_context_guard_invalid": "Context Guard initialized with invalid token limit or message format",
                "agent_rpc_ready_missing": "Context Guard is ready, but OMP RPC ready frame was not received",
                "agent_protocol_version_unsupported": "OMP returned an unsupported RPC protocol version",
                "agent_rpc_timeout": "Timed out waiting for OMP RPC and Context Guard ready frames",
                "agent_process_exited": "OMP process exited before initialization completed",
                "agent_rpc_eof_or_overflow": "OMP closed RPC stream or produced an oversized frame",
            }
            detail = f"[{code}] {detail_messages.get(code, 'OMP transport/session inspection failed')}"
            return {"api_version": 1, "milestone": "M2", "ok": False, "checks": [{"check": "rpc_transport", "status": "FAIL", "detail": detail}]}
        except (RuntimeContractError, OSError, TimeoutError, ValueError):
            return {"api_version": 1, "milestone": "M2", "ok": False, "checks": [{"check": "rpc_transport", "status": "FAIL", "detail": "OMP transport/session inspection failed; no credentials or stderr exposed"}]}
        if not with_model:
            checks.append({"check": "model_roundtrip", "status": "SKIP", "detail": "Pass --with-model to run three small model turns using configured authentication"})
            return {"api_version": 1, "milestone": "M2", "ok": True, "checks": checks}
        workflow = workflows / "verify"
        workflow.mkdir()
        (workflow / "workflow.toml").write_text(
            'schema_version = 1\nid = "verify"\nversion = "1"\nsession_policy = "per_task"\n'
            f'workspace = {json.dumps(str(root))}\nstart_prompt = "event.md"\nwake_prompt = "event.md"\nrecovery_prompt = "event.md"\n')
        (workflow / "event.md").write_text("Follow the verification instruction in payload.instruction. Do not use tools.\n${event_json}\n")
        initialize_database(isolated.database_path)
        daemon = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(config_path),
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def drain(stream):
            while await stream.read(8192):
                pass
        drains = [asyncio.create_task(drain(daemon.stdout)), asyncio.create_task(drain(daemon.stderr))]
        async def api(command, arguments=None):
            return await asyncio.to_thread(request, isolated.server.socket_path, command, arguments)
        async def turn(task, instruction, key):
            event = await api("schedule", {"workflow_id": "verify", "task_id": task, "reason": "OMP integration verification",
                                           "dedupe_key": key, "payload": {"instruction": instruction}, "at": utc_text(now_utc())})
            async with asyncio.timeout(isolated.agent.startup_timeout_seconds + isolated.agent.turn_timeout_seconds + isolated.agent.settle_timeout_seconds + 10):
                while True:
                    current = await api("events.show", {"event_id": event["event_id"]})
                    if current["status"] in ("succeeded", "failed", "blocked", "uncertain"):
                        if current["status"] != "succeeded":
                            raise AgentError("Model verification did not succeed: " + current["status"])
                        run = await api("runs.show", {"run_id": current["run_id"]})
                        return run
                    await asyncio.sleep(0.05)
        def read_binding(run):
            with closing(sqlite3.connect(isolated.database_path)) as db:
                db.row_factory = sqlite3.Row
                return dict(db.execute("SELECT * FROM session_bindings WHERE binding_id=?", (run["binding_id"],)).fetchone())
        try:
            async with asyncio.timeout(10):
                while True:
                    if daemon.returncode is not None:
                        raise AgentError("Verification daemon failed to start")
                    try:
                        await api("status")
                        break
                    except RuntimeContractError:
                        await asyncio.sleep(0.05)
            marker = "PAVEWAY_" + uuid.uuid4().hex
            first = await turn("a", f"Remember the token {marker}. Reply only READY.", "first")
            first_binding = read_binding(first)
            second = await turn("a", "Repeat the exact PAVEWAY_ token from my earlier message. Reply only with that token.", "second")
            second_binding = read_binding(second)
            if first["binding_id"] != second["binding_id"] or first_binding["session_file"] != second_binding["session_file"] or first_binding["session_id"] != second_binding["session_id"]:
                raise AgentError("Same-task wake changed session identity")
            _, messages = await inspect_session(isolated, second_binding)
            replies = [message_text(m) for m in messages if m.get("role") == "assistant"]
            if len(replies) < 2 or marker not in replies[-1]:
                raise AgentError("Resumed model did not recall the first turn token")
            checks.append({"check": "same_task_resume", "status": "PASS", "detail": "Two daemon wake events, stable session identity and recalled random token"})
            third = await turn("b", "Reply only EMPTY.", "third")
            other_binding = read_binding(third)
            _, other_messages = await inspect_session(isolated, other_binding)
            if other_binding["session_id"] == second_binding["session_id"] or any(marker in message_text(m) for m in other_messages):
                raise AgentError("Different tasks shared session history")
            checks.append({"check": "task_isolation", "status": "PASS", "detail": "Separate session file/ID and no first-task token in second-task history"})
        except (RuntimeContractError, OSError, TimeoutError, ValueError):
            checks.append({"check": "model_roundtrip", "status": "FAIL", "detail": "Model/session verification failed; check model availability and configured authentication; no prompt replay attempted"})
        finally:
            if daemon.returncode is None:
                daemon.terminate()
                try:
                    await asyncio.wait_for(daemon.wait(), min(isolated.agent.graceful_stop_seconds, 30) + 5)
                except TimeoutError:
                    daemon.kill()
                    await daemon.wait()
            for task in drains:
                task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)
    return {"api_version": 1, "milestone": "M2", "ok": not any(c["status"] == "FAIL" for c in checks), "checks": checks}
