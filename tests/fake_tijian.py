#!/usr/bin/env python3
"""JSON-CLI fixture, never a substitute for real Tijian acceptance."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).parent
if sys.argv[1:] != ["--input-format", "json", "--output-format", "json"]:
    sys.exit(2)
request = json.load(sys.stdin)
if set(request) != {"args"}:
    sys.exit(2)
args = request["args"]
with (root / "trace.jsonl").open("a") as stream:
    stream.write(json.dumps(args, ensure_ascii=False) + "\n")
dry_run = "--dry-run" in args
result = {"message": "ok", "changed": False, "dryRun": dry_run, "data": {"args": args}}
if args == ["help"]:
    result["message"] = "problem get problem status problem refetch problem vector compute history import plan show daily show backup verify article list"
if "HANG" in args:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (root / "child.pid").write_text(str(child.pid))
    time.sleep(60)
if "FLOOD" in args:
    sys.stdout.write("x" * 100000)
    sys.stdout.flush()
    time.sleep(60)
if "STDERR" in args:
    sys.stderr.write("PRIVATE_SECRET" * 100000)
if "FAIL" in args:
    sys.stderr.write("PRIVATE_SECRET")
    sys.exit(1)
if any(value in args for value in ("MISSING", "MISSING_NOISE")):
    sys.stderr.write("命令执行失败: 题目不存在: CF1A\n")
    if "MISSING_NOISE" in args:
        sys.stderr.write("PRIVATE_SECRET")
    sys.exit(1)
if "MALFORMED" in args:
    print("PRIVATE_SECRET")
    sys.exit(0)
if "BAD_ENVELOPE" in args:
    result["changed"] = "yes"
if "WRONG_DRY_RUN" in args:
    result["dryRun"] = not dry_run
state_file = root / "state.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {"status": "unseen", "history": []}
if args[:2] == ["problem", "status"]:
    status = args[args.index("--to") + 1]
    result["changed"] = True
    if not dry_run:
        state["status"] = status
        state["history"].append(status)
        state_file.write_text(json.dumps(state))
if args[:2] == ["problem", "get"]:
    result["data"] = {"problem": {"id": args[2], "statement": "題面 $x$", "status": state["status"]},
                      "statusHistory": state["history"], "translations": [{"language": "en", "text": "A statement"}],
                      "futureUpstreamField": {"preserved": True}}
print(json.dumps(result, ensure_ascii=False, indent=2))
