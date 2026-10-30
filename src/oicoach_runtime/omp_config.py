"""Shared native OMP configuration probes; no model prompt is submitted."""

import asyncio
import json

from .errors import AgentError

MAX_CONFIG = 1048576


async def skill_directories(config, workspace, environment):
    """Preserve native directories: OMP overlays replace list settings."""
    process = None
    try:
        query_environment = dict(environment)
        if config.agent.profile:
            query_environment["OMP_PROFILE"] = config.agent.profile
        async with asyncio.timeout(config.agent.startup_timeout_seconds):
            process = await asyncio.create_subprocess_exec(
                config.agent.executable, "config", "get", "skills.customDirectories", "--json",
                cwd=workspace, env=query_environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=MAX_CONFIG)
            try:
                await process.stdout.readexactly(MAX_CONFIG + 1)
            except asyncio.IncompleteReadError as error:
                data = error.partial
            else:
                raise ValueError("Oversized skill configuration")
            if await process.wait() != 0:
                raise ValueError("Native configuration query failed")
            result = json.loads(data)
            directories = result.get("value") if isinstance(result, dict) else None
            if not isinstance(directories, list) or any(not isinstance(path, str) for path in directories):
                raise ValueError("Invalid native skill directories")
            return tuple(directories)
    except (OSError, TimeoutError, ValueError, RecursionError):
        raise AgentError("Cannot load native OMP skill directories", code="agent_skill_config_error") from None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
