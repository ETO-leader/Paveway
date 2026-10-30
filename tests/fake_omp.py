#!/usr/bin/env python3
"""Test-only RPC peer; never use this executable as a real OI coach."""

import argparse
import hashlib
import json
import os
import sys
import subprocess
import time
import uuid
import re
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
    if sys.argv[1:] == ["config", "get", "skills.customDirectories", "--json"]:
        print(json.dumps({"key": "skills.customDirectories", "value": []}))
        return
    if "models" in sys.argv:
        print(json.dumps({"models": [{"selector": "fake/coach", "name": "Fake Coach", "reasoning": True,
                                      "thinking": ["low", "medium", "high"]},
                                     {"selector": "fake/fast", "name": "Fake Fast", "reasoning": False, "thinking": None}]}))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--mode")
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--model")
    parser.add_argument("--thinking")
    parser.add_argument("--system-prompt", type=Path)
    parser.add_argument("--append-system-prompt", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--extension", type=Path)
    for option in ("tools", "extensions", "skills", "rules"):
        parser.add_argument("--no-" + option, action="store_true")
    args = parser.parse_args()
    with (args.cwd / "fake-starts.jsonl").open("a") as launches:
        launches.write(json.dumps({"pid": os.getpid(), "session_dir": str(args.session_dir), "resumed": bool(args.resume),
                                  "model": args.model, "thinking": args.thinking,
                                  "https_proxy_hash": hashlib.sha256(os.environ.get("HTTPS_PROXY", "").encode()).hexdigest()}) + "\n")
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
    exit_code = 0
    if startup_mode != "missing-context-guard":
        emit({"type": "paveway_context_ready", "tokenLimit": 10000})
    emit({"type": "ready", "protocolVersion": 1}, split=True)
    for line in sys.stdin:
        request = json.loads(line)
        request_id = request["id"]
        if request["type"] == "get_state":
            emit({"type": "response", "command": "get_state", "id": request_id, "success": True,
                  "data": {"sessionFile": str(session_file), "sessionId": session["session_id"], "isSettled": settled}})
        elif request["type"] == "get_last_assistant_text":
            last = session["prompts"][-1]["payload"] if session["prompts"] else {}
            if last.get("fake_mode") == "output-persistence-notice":
                emit({"type": "notice", "source": "session-persistence", "level": "error"})
                continue
            emit({"type": "response", "command": "get_last_assistant_text", "id": request_id, "success": True,
                  "data": {"text": last.get("fake_reply", "fake completion")}})
        elif request["type"] == "get_messages":
            messages = []
            marker = ""
            for envelope in session["prompts"]:
                instruction = envelope["payload"].get("instruction", "")
                match = re.search(r"PAVEWAY_[0-9a-f]+", instruction)
                if match:
                    marker = match.group()
                answer = marker if instruction.startswith("Repeat the exact") else ("READY" if match else "EMPTY")
                messages.extend([{"role": "user", "content": json.dumps(envelope)}, {"role": "assistant", "content": [{"type": "text", "text": answer}]}])
            emit({"type": "response", "command": "get_messages", "id": request_id, "success": True, "data": {"messages": messages}})
        elif request["type"] == "prompt":
            envelope = json.loads(request["message"].splitlines()[-1])
            session["prompts"].append(envelope)
            session_file.write_text(json.dumps(session))
            mode = envelope["payload"].get("fake_mode", startup_mode)
            if mode == "persistence-error":
                exit_code = 15
            if mode == "persistence-notice":
                emit({"type": "notice", "source": "session-persistence", "level": "error", "message": "private failure detail"})
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
            if mode in ("memory-tool", "memory-tool-crash"):
                result = subprocess.run([*envelope["available_tools"][0]["argv_prefix"], "memory", "put",
                    "--workflow", envelope["workflow_id"], "--task", envelope["task_id"],
                    "--key", "turn-note", "--title", "Progress", "--content", "口胡未编码",
                    "--source-event", envelope["event_id"], "--json"], capture_output=True, timeout=5)
                if result.returncode:
                    return 17
                if mode == "memory-tool-crash":
                    return 18
            if mode == "needs-user":
                emit({"type": "extension_ui_request", "id": "permission", "method": "confirm"})
                continue
            if mode in ("hold", "hold-with-tool"):
                emit({"type": "tool_execution_start", "toolName": "bash", "args": {"command": "PRIVATE_PROGRESS_COMMAND"}})
                if mode == "hold-with-tool":
                    tool = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                    (args.cwd / "fake-tool-pid").write_text(str(tool.pid))
                (args.cwd / "fake-entered").write_text(str(os.getpid()))
                while not (args.cwd / "fake-release").exists():
                    time.sleep(0.01)
                emit({"type": "tool_execution_end", "toolName": "bash", "result": "PRIVATE_PROGRESS_RESULT"})
            if mode == "ack-only":
                continue
            if mode == "local-only":
                settled = True
                continue
            result = "error" if mode in ("provider-error", "auth-error") else "completed"
            if mode == "report-then-postscript":
                emit({"type": "message_end", "message": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "PRIVATE_HIDDEN_REASONING"},
                    {"type": "text", "text": envelope["payload"].get("fake_reply", "FULL_REPORT_BODY")},
                    {"type": "toolCall", "id": "private", "name": "bash", "arguments": {"secret": "PRIVATE_TOOL_ARGUMENT"}}]}})
                emit({"type": "message_end", "message": {"role": "toolResult", "content": [{"type": "text", "text": "PRIVATE_TOOL_RESULT"}]}})
                emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "SHORT_POSTSCRIPT"}]}})
            emit({"type": "prompt_result", "id": "unrelated", "status": "completed", "sessionSettled": True})
            delayed = mode in ("delayed-settle", "crash-before-settled")
            detail = {"error": {"httpStatus": 401, "message": "Invalid API key: sk-private-test-value Authorization: Bearer private-token", "retryable": False}} if mode == "auth-error" else {}
            emit({"type": "prompt_result", "id": request_id, "status": result, "agentInvoked": True, "sessionSettled": not delayed, **detail}, split=True)
            if mode == "crash-before-settled":
                return 12
            if delayed:
                time.sleep(0.15)
                emit({"type": "session_settled"})
            settled = True
        elif request["type"] == "abort":
            settled = True
            emit({"type": "response", "id": request_id, "command": "abort", "success": True})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
