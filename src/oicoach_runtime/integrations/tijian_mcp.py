"""Optional official MCP SDK adapter. stdout is reserved for stdio protocol."""

import json

from .. import __version__
from ..errors import RuntimeContractError
from .tijian import TijianClient, TijianError
from .tijian_catalog import OPERATIONS


async def serve(config):
    try:
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import CallToolResult, TextContent, Tool
    except ImportError:
        raise TijianError("mcp_not_installed", "Install the optional SDK with pip install 'oicoach-runtime[tijian]'") from None
    if not config.enabled:
        raise TijianError("tijian_disabled", "Enable [tijian] before starting its MCP bridge")
    client = TijianClient(config)
    server = Server("paveway-tijian", version=__version__, instructions=(
        "Tijian problem-library and training-data bridge. Daily plans do not schedule Runtime wakes. "
        "Use dry_run for previews. Never automatically retry a failed write with outcome=uncertain; "
        "first inspect the actual Tijian state. Status explored does not record solution reading."))

    @server.list_tools()
    async def tools():
        return [Tool(**operation.tool()) for operation in OPERATIONS.values()
                if not (config.read_only and operation.mutates)]

    # Validate here to avoid SDK validation errors echoing private argument values.
    @server.call_tool(validate_input=False)
    async def call(name, arguments):
        try:
            result = await client.call(name, arguments)
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                  structuredContent=result, isError=False)
        except RuntimeContractError as error:
            result = {"error": {"code": error.code, "message": str(error),
                                "outcome": getattr(error, "outcome", "not_started")}}
        except Exception:
            result = {"error": {"code": "tijian_adapter_error", "message": "Tijian adapter failed; inspect state before retrying a write",
                                "outcome": "uncertain"}}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result))],
                              structuredContent=result, isError=True)

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
