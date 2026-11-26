"""Shared native OMP configuration probes; no model prompt is submitted."""

import asyncio
import json

from .errors import AgentError
from .omp_installation import OmpInstallationUnavailable

MAX_CONFIG = 1048576


def disabled_tool_args(overlay, environment):
    """Launch an empty built-in list with the managed extension still loaded."""
    # Both hosts accept a comma-only --tools value as an explicit empty list.
    # --no-tools on 19.2 also excludes the required Paveway extension.
    overlay.update(dev={"autoqa": False}, autolearn={"enabled": False},
                   goal={"enabled": False}, plan={"enabled": False, "defaultOnStartup": False},
                   memory={"backend": "off"}, externalThinking=False,
                   mcp={"enableProjectConfig": False, "shared": False})
    environment["PI_AUTO_QA"] = "0"
    return ["--tools", ",", "--no-lsp", "--no-prewalk"]


async def skill_directories(config, installation, workspace, environment):
    """Preserve native directories: OMP overlays replace list settings."""
    process = None
    try:
        query_environment = dict(environment)
        if config.agent.profile:
            query_environment["OMP_PROFILE"] = config.agent.profile
        async with asyncio.timeout(config.agent.startup_timeout_seconds):
            process = await asyncio.create_subprocess_exec(
                *installation.command(["config", "get", "skills.customDirectories", "--json"]),
                cwd=workspace, env=query_environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=MAX_CONFIG)
            try:
                await process.stdout.readexactly(MAX_CONFIG + 1)
            except asyncio.IncompleteReadError as error:
                data = error.partial
            else:
                raise ValueError("Oversized skill configuration")
            if await process.wait() != 0:
                installation.check()
                raise ValueError("Native configuration query failed")
            result = json.loads(data)
            directories = result.get("value") if isinstance(result, dict) else None
            if not isinstance(directories, list) or any(not isinstance(path, str) for path in directories):
                raise ValueError("Invalid native skill directories")
            return tuple(directories)
    except OmpInstallationUnavailable:
        raise
    except (OSError, TimeoutError, ValueError, RecursionError):
        raise AgentError("Cannot load native OMP skill directories", code="agent_skill_config_error") from None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
