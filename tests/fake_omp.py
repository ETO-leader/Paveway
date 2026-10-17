#!/usr/bin/env python3
"""Test-only RPC peer; never use this executable as a real OI coach."""

import argparse
import json
import os
import sys
import subprocess
import time
import uuid
from pathlib import Path


def emit(value, split=False):
    data = (json.dumps(value) + "\n").encode()
    if split:
        os.write(1, data[:5])
        os.write(1, data[5:])
    else:
        os.write(1, data)


def main():
    if "--version" in sys.argv:
        print("omp/0.0.1-test")
        return
    if "--help" in sys.argv:
        print("--cwd --session-dir --resume --mode --no-ui --profile")
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--mode")
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--profile")
    args = parser.parse_args()
    mode_file = args.cwd / "fake-mode"
    startup_mode = mode_file.read_text().strip() if mode_file.exists() else "normal"
    if startup_mode == "crash-before-ready":
        return 9
    if startup_mode == "hang-before-ready":
        time.sleep(30)
    args.session_dir.mkdir(parents=True, exist_ok=True)
    session_file = args.resume or args.session_dir / "session.json"
    if args.resume:
        session = json.loads(session_file.read_text())
    else:
        session = {"session_id": str(uuid.uuid4()), "prompts": []}
        session_file.write_text(json.dumps(session))
    settled = True
    emit({"type": "ready", "protocolVersion": 1}, split=True)
    for line in sys.stdin:
        request = json.loads(line)
        request_id = request["id"]
        if request["type"] == "get_state":
            emit({"type": "response", "command": "get_state", "id": request_id, "success": True,
                  "data": {"sessionFile": str(session_file), "sessionId": session["session_id"], "isSettled": settled}})
        elif request["type"] == "prompt":
            envelope = json.loads(request["message"])
            session["prompts"].append(envelope)
            session_file.write_text(json.dumps(session))
            mode = envelope["payload"].get("fake_mode", startup_mode)
            settled = False
            if mode == "crash-before-ack":
                return 10
            if mode == "crash-with-tool":
                tool = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                (args.cwd / "fake-tool-pid").write_text(str(tool.pid))
                return 13
            emit({"type": "future_event", "detail": "ignored"})
            if mode == "stderr-flood":
                os.write(2, b"private diagnostic " * 10000)
            if mode == "malformed":
                os.write(1, b"invalid-json\n")
                continue
            emit({"type": "response", "command": "prompt", "id": request_id, "success": True,
                  "data": {"agentInvoked": mode != "local-only"}})
            if mode == "crash-after-ack":
                return 11
            if mode == "schedule-child":
                result = subprocess.run([*envelope["available_tools"][0]["argv_prefix"], "schedule",
                                         "--workflow", envelope["workflow_id"], "--task", envelope["task_id"],
                                         "--after-turn", "0.1", "--reason", "tool child", "--dedupe-key", "tool-child", "--json"],
                                        capture_output=True, timeout=5)
                if result.returncode:
                    return 14
            if mode == "needs-user":
                emit({"type": "extension_ui_request", "id": "permission", "method": "confirm"})
                continue
            if mode == "hold":
                (args.cwd / "fake-entered").write_text(str(os.getpid()))
                while not (args.cwd / "fake-release").exists():
                    time.sleep(0.01)
            if mode == "ack-only":
                continue
            if mode == "local-only":
                settled = True
                continue
            result = "error" if mode == "provider-error" else "completed"
            emit({"type": "prompt_result", "id": "unrelated", "status": "completed", "sessionSettled": True})
            delayed = mode in ("delayed-settle", "crash-before-settled")
            emit({"type": "prompt_result", "id": request_id, "status": result, "agentInvoked": True, "sessionSettled": not delayed}, split=True)
            if mode == "crash-before-settled":
                return 12
            if delayed:
                time.sleep(0.15)
                emit({"type": "session_settled"})
            settled = True
        elif request["type"] == "abort":
            settled = True
            emit({"type": "response", "id": request_id, "command": "abort", "success": True})


if __name__ == "__main__":
    main()
