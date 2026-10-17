"""One bounded versioned JSON request/response per private Unix connection."""

import asyncio
import json
import os
import socket
import stat
import struct
import uuid
from pathlib import Path

from .errors import DaemonError, RuntimeContractError
from .store import canonical_json

MAX_REQUEST = 131072
MAX_RESPONSE = 131072


def request(socket_path, command, arguments=None):
    request_id = str(uuid.uuid4())
    payload = {"api_version": 1, "id": request_id, "command": command, "arguments": arguments or {}}
    encoded = (canonical_json(payload) + "\n").encode()
    if len(encoded) > MAX_REQUEST:
        raise RuntimeContractError("IPC request exceeds 128 KiB")
    try:
        info = Path(socket_path).lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise DaemonError("Control socket is not private and owned by the current user")
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(socket_path))
            client.sendall(encoded)
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = client.recv(min(8192, MAX_RESPONSE + 1 - len(data)))
                if not chunk or len(data) + len(chunk) > MAX_RESPONSE:
                    raise DaemonError("Incomplete or oversized IPC response")
                data.extend(chunk)
        response = json.loads(data)
        if not isinstance(response, dict) or type(response.get("api_version")) is not int or response["api_version"] != 1 or response.get("id") != request_id:
            raise DaemonError("Invalid IPC response identity/version")
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict) or not isinstance(error.get("message"), str) or not isinstance(error.get("code"), str):
                raise DaemonError("Invalid IPC error")
            failure = RuntimeContractError(error["message"])
            failure.code = error["code"]
            raise failure
        if "result" not in response:
            raise DaemonError("Missing IPC result")
        return response["result"]
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise DaemonError("Cannot reach daemon or decode response; start serve with the same config") from None


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {"api_version", "id", "command", "arguments"}:
        raise RuntimeContractError("Invalid IPC request fields")
    if type(value["api_version"]) is not int or value["api_version"] != 1:
        raise RuntimeContractError("Unsupported IPC version")
    if not isinstance(value["id"], str) or not 1 <= len(value["id"]) <= 128:
        raise RuntimeContractError("Invalid request ID")
    if not isinstance(value["command"], str) or not isinstance(value["arguments"], dict):
        raise RuntimeContractError("Invalid IPC command or arguments")


async def handle_connection(reader, writer, dispatch, accepting):
    request_id = None
    try:
        sock = writer.get_extra_info("socket")
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid() or not accepting():
            raise DaemonError("Daemon is stopping or caller is not the runtime owner")
        line = await asyncio.wait_for(reader.readline(), 10)
        if not line.endswith(b"\n") or len(line) > MAX_REQUEST:
            raise RuntimeContractError("Incomplete or oversized IPC request")
        value = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        validate_request(value)
        request_id = value["id"]
        response = {"api_version": 1, "id": request_id, "result": dispatch(value["command"], value["arguments"])}
        if len((canonical_json(response) + "\n").encode()) > MAX_RESPONSE:
            raise RuntimeContractError("Response exceeds 128 KiB; narrow the query")
    except asyncio.CancelledError:
        writer.close()
        raise
    except RuntimeContractError as error:
        response = {"api_version": 1, "id": request_id, "error": {"code": error.code, "message": str(error)}}
    except (ValueError, UnicodeError, TimeoutError, RecursionError):
        response = {"api_version": 1, "id": request_id, "error": {"code": "invalid_input", "message": "Invalid, oversized or timed out IPC frame"}}
    except Exception:
        response = {"api_version": 1, "id": request_id, "error": {"code": "internal_error", "message": "Runtime operation failed; no private diagnostics returned"}}
    try:
        writer.write((canonical_json(response) + "\n").encode())
        await asyncio.wait_for(writer.drain(), 5)
    except (OSError, TimeoutError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
