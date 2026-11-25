"""Process/RPC boundary observations for the real, isolated Relay fixture only."""

import asyncio
import json
from pathlib import Path
import sys
import time
from functools import wraps


def emit(path, stage, **facts):
    data = json.dumps({"ns": time.monotonic_ns(), "stage": stage, **facts}).encode() + b"\n"
    with open(path, "ab", buffering=0) as stream:
        stream.write(data)


def describe_children(pid):
    def inspect(parent):
        entries = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
        return [int(entry) for entry in entries]

    try:
        return {child: inspect(child) for child in inspect(pid)}
    except (FileNotFoundError, ProcessLookupError):
        return {}


def serve(config_path, trace):
    from oicoach_runtime.adapters import omp_rpc
    from oicoach_runtime.config import load_config
    from oicoach_runtime.scheduler import Runtime
    from oicoach_runtime.store import Store

    runtime = Runtime(load_config(config_path), config_path)
    record = lambda stage, **facts: emit(trace, stage, runtime=runtime.owner, **facts)
    start, frame, stop = omp_rpc.OmpRpc.start, omp_rpc.OmpRpc.frame, omp_rpc.OmpRpc._stop
    finish, reap, cleanup = Store.finish, runtime._reap, omp_rpc.cleanup_group
    guardians = {}

    @wraps(start)
    async def observe_start(agent, binding, run_id):
        agent.relay_run_id = run_id
        pid, identity = await start(agent, binding, run_id)
        guardians[pid] = agent.process, run_id
        record("guardian_started", run=run_id, guardian=pid, task=binding["task_id"])
        return pid, identity

    @wraps(frame)
    async def observe_frame(agent, timeout):
        row = await frame(agent, timeout)
        kind = row["type"]
        if kind in ("ready", "prompt_result", "session_settled", "tool_execution_end", "agent_end") or (
            kind == "response" and row.get("command") in ("get_state", "get_last_assistant_text", "prompt")
        ):
            record(kind, run=agent.relay_run_id, guardian=agent.process.pid,
                   command=row.get("command"), status=row.get("status"),
                   settled=row.get("sessionSettled", (row.get("data") or {}).get("isSettled") if isinstance(row.get("data"), dict) else None),
                   children=describe_children(agent.process.pid))
        return row

    @wraps(stop)
    async def observe_stop(agent, abort):
        process = agent.process
        def phase(stage, **facts):
            record(stage, run=agent.relay_run_id, guardian=process.pid, **facts)
        phase("stop_enter", abort=abort, children=describe_children(process.pid))
        close = process.stdin.close
        def observe_close():
            process.stdin.close = close
            close()
            phase("stdin_closed")

        process.stdin.close = observe_close
        try:
            await stop(agent, abort)
        finally:
            process.stdin.close = close
            guardians.pop(process.pid, None)
            phase("stop_return", code=process.returncode, children=describe_children(process.pid))

    @wraps(cleanup)
    def observe_cleanup(pid, exclude):
        process, run_id = guardians[pid]
        record("cleanup_begin", guardian=pid, run=run_id, guardian_returncode=process.returncode,
               children=describe_children(pid))
        cleanup(pid, exclude)
        record("cleanup_complete", guardian=pid, run=run_id)

    @wraps(finish)
    def observe_finish(store, run_id, status, **kwargs):
        result = finish(store, run_id, status, **kwargs)
        record("run_persisted", run=run_id, status=status, exit_code=kwargs.get("exit_code"))
        return result

    @wraps(reap)
    async def observe_reap(run_ids):
        await reap(run_ids)
        for run in run_ids:
            record("slot_released", run=run)

    omp_rpc.OmpRpc.start, omp_rpc.OmpRpc.frame, omp_rpc.OmpRpc._stop = observe_start, observe_frame, observe_stop
    omp_rpc.cleanup_group, Store.finish, runtime._reap = observe_cleanup, observe_finish, observe_reap
    asyncio.run(runtime.serve())


if __name__ == "__main__":
    serve(Path(sys.argv[1]), Path(sys.argv[2]))
