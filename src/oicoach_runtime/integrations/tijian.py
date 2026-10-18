"""Bounded JSON process adapter for Tijian's canonical application CLI."""

import asyncio
import json
import os
import shutil
import signal
import sys
from pathlib import Path

from ..config import TijianConfig
from ..errors import RuntimeContractError
from .tijian_catalog import OPERATIONS, UPSTREAM_REVISION, UPSTREAM_URL


class TijianError(RuntimeContractError):
    def __init__(self, code, message, *, outcome="not_started"):
        super().__init__(message)
        self.code = code
        self.outcome = outcome


def catalog():
    return {"api_version": 1, "upstream": UPSTREAM_URL, "upstream_revision": UPSTREAM_REVISION,
            "transport": "tijian-json-cli", "tools": [item.tool() for item in OPERATIONS.values()]}


def mcp_configuration(config_path: Path):
    return {"mcpServers": {"tijian": {"command": sys.executable,
            "args": ["-m", "oicoach_runtime", "tijian", "mcp", "--config", str(config_path.absolute())]}}}


class TijianClient:
    def __init__(self, config: TijianConfig):
        self.config = config
        # MCP tools are concurrent by default; serialize mutations and queries together.
        self.lock = asyncio.Lock()

    async def call(self, name, arguments):
        if not isinstance(name, str):
            raise RuntimeContractError("Tijian operation name must be a string")
        if name.startswith("tijian_"):
            name = name[len("tijian_"):]
        operation = OPERATIONS.get(name)
        if operation is None:
            raise RuntimeContractError("Unknown Tijian operation; use tijian tools")
        args = operation.arguments(arguments)
        if len(json.dumps({"args": args}, ensure_ascii=False, allow_nan=False).encode()) > 65536:
            raise RuntimeContractError("Tijian request exceeds 64 KiB")
        if not self.config.enabled:
            raise TijianError("tijian_disabled", "Enable [tijian] in the Runtime configuration")
        if self.config.read_only and operation.mutates:
            raise TijianError("tijian_read_only", "This operation is unavailable in Tijian read_only mode")
        executable = shutil.which(self.config.executable)
        if not executable:
            raise TijianError("tijian_unavailable", "Tijian executable not found; install Tijian or configure its absolute path")
        outcome = "uncertain" if operation.mutates and not arguments.get("dry_run") else "no_business_mutation"
        async with self.lock:
            return await self._execute(executable, args, outcome, bool(arguments.get("dry_run")))

    async def _execute(self, executable, args, outcome, dry_run):
        process = None
        tasks = []
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "oicoach_runtime.child", str(os.getpid()),
                    executable, "--input-format", "json", "--output-format", "json",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True,
                    # The bridge does not own the daemon's inherited descriptor numbers.
                    env=dict(os.environ, OICOACH_LOCK_FDS=""))
            except OSError:
                raise TijianError("tijian_unavailable", "Cannot start Tijian executable") from None

            async def read_stdout():
                chunks, size = [], 0
                while data := await process.stdout.read(65536):
                    size += len(data)
                    if size > self.config.max_output_bytes:
                        raise TijianError("tijian_output_limit", "Tijian output exceeds the configured bound; narrow the query", outcome=outcome)
                    chunks.append(data)
                return b"".join(chunks)

            async def drain_stderr():
                # Drain, but never retain or relay credentials, URLs, model text or user data.
                while await process.stderr.read(65536):
                    pass

            async def exchange():
                process.stdin.write(json.dumps({"args": args}, ensure_ascii=False, allow_nan=False).encode())
                try:
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                process.stdin.close()
                return await asyncio.gather(*tasks)

            tasks = [asyncio.create_task(read_stdout()), asyncio.create_task(drain_stderr()),
                     asyncio.create_task(process.wait())]
            try:
                stdout, _, exit_code = await asyncio.wait_for(exchange(), self.config.timeout_seconds)
            except TimeoutError:
                raise TijianError("tijian_timeout", "Tijian timed out; no automatic retry was performed", outcome=outcome) from None
            except OSError:
                raise TijianError("tijian_transport_error", "Tijian process communication failed; no automatic retry was performed", outcome=outcome) from None
            if exit_code != 0:
                raise TijianError("tijian_command_failed", "Tijian command failed; check Tijian locally. Batch operations may have partially completed", outcome=outcome)
            try:
                result = json.loads(stdout, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                raise TijianError("tijian_protocol_error", "Tijian did not return one valid JSON result", outcome=outcome) from None
            if (not isinstance(result, dict) or not isinstance(result.get("message"), str) or
                    type(result.get("changed")) is not bool or type(result.get("dryRun")) is not bool or
                    "data" not in result or result["dryRun"] != dry_run):
                raise TijianError("tijian_protocol_error", "Tijian returned an incompatible JSON result envelope", outcome=outcome)
            # Preserve upstream metadata, status distinctions and future fields verbatim.
            return result
        finally:
            if process is not None:
                cleanup = asyncio.create_task(self._cleanup(process, tasks))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Complete ownership cleanup even when a caller cancels a tool call.
                    await cleanup
                    raise

    @staticmethod
    async def _cleanup(process, tasks):
        # Only the new process session created above; no payload-supplied PID/group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await process.wait()

    async def diagnose(self, with_data=False):
        checks = []
        try:
            help_result = await self.call("help", {})
            required = ("problem get", "problem status", "problem refetch", "problem vector compute",
                        "history import", "plan show", "daily show", "backup verify", "article list")
            compatible = all(word in help_result["message"] for word in required)
            checks.append({"check": "tijian_json_cli", "status": "PASS" if compatible else "FAIL",
                           "detail": "JSON envelope and required command syntax " + ("recognized" if compatible else "incompatible")})
            if compatible and with_data:
                for name in ("problem_stats", "plan_show", "article_list"):
                    await self.call(name, {})
                    checks.append({"check": name, "status": "PASS", "detail": "Read query completed; contents omitted"})
        except TijianError as error:
            checks.append({"check": "tijian", "status": "FAIL", "code": error.code, "detail": str(error)})
        return {"api_version": 1, "ok": all(c["status"] == "PASS" for c in checks),
                "upstream_revision": UPSTREAM_REVISION, "checks": checks,
                "model_verified": False, "mutations_verified": False}
