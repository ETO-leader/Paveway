"""Prove checkpoint delivery with real OMP RPC and isolated loopback requests.

The first 63 receipts come from actual CAS Memory writes and completed model
turns. The 64th is prepared, then lost to a process crash and an RPC abort before
one successful retry. No production hook results, user data or paid APIs change.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
from importlib.resources import files
import json
import os
from pathlib import Path
import signal
import tempfile

try:
    from .verify_prompt_cache import CacheHarness, TIMEOUT, canonical, compare_requests, read_archive, require
    from .verify_rpc_memory_boundaries import RpcConversation
except ImportError:
    from verify_prompt_cache import CacheHarness, TIMEOUT, canonical, compare_requests, read_archive, require
    from verify_rpc_memory_boundaries import RpcConversation


SCOPE = {"workflow_id": "cache-probe", "task_id": "training"}
MEMORY_TYPE = "paveway-memory-context"
NOTE_64 = "PAVEWAY_CHECKPOINT_NOTE_VERSION_64"


def _private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False)
    require(path.stat().st_mode & 0o777 == 0o600, "checkpoint control data must be mode 0600")


def _extension(fixture, mode_path):
    path = fixture.root / "observe-memory-checkpoint.mjs"
    production = str(files("oicoach_runtime").joinpath("bundled/paveway-rpc-memory.mjs"))
    source = '''import memory from __MEMORY__;
import {readFileSync, writeSync} from "node:fs";
const emit = frame => writeSync(1, JSON.stringify(frame) + "\\n");
export default async function(pi) {
    await memory(new Proxy(pi, {
        get(target, key, receiver) {
            if (key !== "on") return Reflect.get(target, key, receiver);
            return (event, handler) => target.on(event, async (...args) => {
                const result = await handler(...args);
                if (event === "before_agent_start" && result?.message?.details?.state)
                    emit({type: "test_checkpoint_prepared", state: result.message.details.state,
                        delta: result.message.details.delta});
                return result;
            });
        },
    }));
    pi.on("before_agent_start", async () => {
        const mode = JSON.parse(readFileSync(__MODE__, "utf8"));
        if (mode === "crash") process.exit(23);
        if (mode === "pause") {
            await new Promise(resolve => {
                process.once("SIGUSR1", resolve);
                emit({type: "test_checkpoint_paused"});
            });
        }
    });
}
'''
    path.write_text(source.replace("__MEMORY__", json.dumps(production)).replace(
        "__MODE__", json.dumps(str(mode_path))), encoding="utf-8")
    return path


def _receipts(fixture, session_file):
    return [entry for entry in read_archive(session_file, fixture.root)
            if entry.get("type") == "custom_message" and entry.get("customType") == MEMORY_TYPE]


def _checkpoints(receipts):
    return [entry for entry in receipts if entry.get("details", {}).get("state") is not None]


def _assert_receipts(receipts, count):
    require(len(receipts) == count, f"expected {count} accepted Memory receipts, got {len(receipts)}")
    versions = []
    for entry in receipts:
        delta = entry.get("details", {}).get("delta", {})
        upserts = delta.get("upserts", [])
        require(delta.get("scope") == SCOPE and len(upserts) == 1 and not delta.get("withdrawals"),
                "journal receipt is not the actual scoped single-note update")
        note = upserts[0]
        require(note.get("key") == "cache-note" and note.get("workflow_id") == SCOPE["workflow_id"],
                "journal receipt contains an unexpected Memory note")
        versions.append(note.get("version"))
    require(versions == list(range(1, count + 1)), "accepted journal receipts skipped or duplicated a CAS version")


def _one_request(fixture, start, marker):
    requests = fixture.server.requests[start:]
    require(len(requests) == 1, f"{marker}: expected exactly one normal loopback model request, got {len(requests)}")
    request = requests[0]
    messages = request.get("messages", [])
    require(messages and messages[-1].get("role") == "user" and marker in canonical(messages[-1]),
            f"{marker}: request was not the submitted normal user turn")
    return request


@asynccontextmanager
async def _conversation(fixture, extension, environment, stage, session_file=None):
    conversation = RpcConversation(fixture, extension, environment=environment,
                                   session_file=session_file, name=f"memory-checkpoint-{stage}")
    try:
        async with conversation:
            yield conversation
    except Exception as error:
        stderr = ""
        stderr_task = getattr(conversation, "stderr_task", None)
        if stderr_task is not None and stderr_task.done():
            output = stderr_task.result()
            stderr = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        raise RuntimeError(f"Memory checkpoint {stage}: {error}\nOMP stderr:\n{stderr}") from error


async def _terminal(conversation, identifier):
    predicate = lambda frame: frame.get("type") == "prompt_result" and frame.get("id") == identifier
    return next((frame for frame in conversation.frames if predicate(frame)), None) or await conversation.until(predicate)


async def verify_memory_checkpoint(fixture):
    """Return observations derived from actual accepted journal rows and wire bodies."""
    data_path = fixture.root / "checkpoint-memory.json"
    mode_path = fixture.root / "checkpoint-mode.json"
    _private_json(mode_path, "pass")
    extension = _extension(fixture, mode_path)
    environment = {**fixture.env, "PAVEWAY_MEMORY_DATA": str(data_path)}

    def update_snapshot(version):
        content = f"PAVEWAY_CHECKPOINT_NOTE_VERSION_{version}"
        written = fixture.note(content, expected_version=version - 1)
        require(written["version"] == version, "Memory CAS write returned the wrong version")
        snapshot = fixture.api("memory.recall", SCOPE)
        require(any(note.get("key") == "cache-note" and note.get("version") == version and
                    note.get("content") == content for note in snapshot.get("notes", [])),
                "actual memory.recall did not return the current CAS note")
        _private_json(data_path, {"run_id": "checkpoint-probe", "scope": SCOPE,
                                  "enabled": True, "socket_path": None, "snapshot": snapshot})
        return snapshot

    update_snapshot(1)
    async with _conversation(fixture, extension, environment, "seed") as conversation:
        for version in range(1, 64):
            if version > 1:
                update_snapshot(version)
            marker = f"CHECKPOINT_SEED_TURN_{version}"
            start = len(fixture.server.requests)
            result = await conversation.prompt(marker)
            require(result.get("status") == "completed", f"seed version {version} did not complete")
            _one_request(fixture, start, marker)
        state = await conversation.state()
        require(state.get("isSettled") is True, "seed session did not settle")
        session_file = Path(state["sessionFile"])
        require(not any(frame.get("type") == "test_checkpoint_prepared" for frame in conversation.frames),
                "checkpoint was prepared before the 64th changed receipt")
    original = _receipts(fixture, session_file)
    _assert_receipts(original, 63)
    require(not _checkpoints(original), "seed journal already contains a checkpoint")
    require(len(fixture.server.requests) == 63, "seed did not make exactly 63 loopback requests")

    snapshot64 = update_snapshot(64)
    expected_state = {"schema": 2, "scope": SCOPE, "notes": snapshot64["notes"]}
    _private_json(mode_path, "crash")
    start = len(fixture.server.requests)
    async with _conversation(fixture, extension, environment, "crash", session_file) as conversation:
        require((await conversation.state()).get("sessionFile") == str(session_file),
                "crash attempt did not resume the actual seed session")
        await conversation.send({"type": "prompt", "id": "checkpoint-crash", "message": "CHECKPOINT_CRASH_V64"})
        crash_prepared = await conversation.until(lambda frame: frame.get("type") == "test_checkpoint_prepared")
        require(crash_prepared.get("state") == expected_state, "crash did not stage the real version-64 checkpoint")
        exit_code = await asyncio.wait_for(conversation.process.wait(), TIMEOUT)
        require(exit_code == 23, f"checkpoint crash exited with {exit_code}, expected test exit 23")
    require(len(fixture.server.requests) == start, "crashed checkpoint reached the model")
    crash_requests = len(fixture.server.requests) - start
    after_crash = _receipts(fixture, session_file)
    _assert_receipts(after_crash, 63)
    require(after_crash == original and not _checkpoints(after_crash),
            "crash durably accepted a receipt/checkpoint or changed original receipts")

    _private_json(mode_path, "pause")
    start = len(fixture.server.requests)
    async with _conversation(fixture, extension, environment, "cancel", session_file) as conversation:
        require((await conversation.state()).get("sessionFile") == str(session_file),
                "cancel attempt did not resume the actual seed session")
        await conversation.send({"type": "prompt", "id": "checkpoint-cancel", "message": "CHECKPOINT_CANCEL_V64"})
        cancel_prepared = await conversation.until(lambda frame: frame.get("type") == "test_checkpoint_prepared")
        require(cancel_prepared.get("state") == expected_state, "cancel did not stage the real version-64 checkpoint")
        paused = await conversation.until(lambda frame: frame.get("type") == "test_checkpoint_paused")
        require(_receipts(fixture, session_file) == original, "paused preparation already wrote a durable receipt")
        require(len(fixture.server.requests) == start, "paused checkpoint reached the model before abort")
        # The RPC response proves abort has cancelled prompt setup. Only then may
        # the unmodified production handler's staged result finish unwinding.
        abort_response = await conversation.command("abort")
        require(abort_response.get("success") is True, "real RPC abort was not acknowledged")
        conversation.process.send_signal(signal.SIGUSR1)
        cancelled_result = await _terminal(conversation, "checkpoint-cancel")
        require(cancelled_result.get("status") == "aborted", "cancelled setup was not reported as aborted")
        require((await conversation.state()).get("isSettled") is True, "aborted setup did not settle")
        require(len(fixture.server.requests) == start, "aborted checkpoint reached the model after release")
        require(_receipts(fixture, session_file) == original, "abort committed a false Memory receipt/checkpoint")
    after_cancel = _receipts(fixture, session_file)
    cancel_requests = len(fixture.server.requests) - start
    _assert_receipts(after_cancel, 63)
    require(after_cancel == original and not _checkpoints(after_cancel),
            "cancel changed durable receipts during shutdown")

    _private_json(mode_path, "pass")
    async with _conversation(fixture, extension, environment, "recover", session_file) as conversation:
        require((await conversation.state()).get("sessionFile") == str(session_file),
                "recovery did not resume the actual seed session")
        start = len(fixture.server.requests)
        result = await conversation.prompt("CHECKPOINT_RECOVER_V64")
        require(result.get("status") == "completed", "checkpoint recovery did not complete")
        recovered_wire = _one_request(fixture, start, "CHECKPOINT_RECOVER_V64")
        recovery_requests = len(fixture.server.requests) - start
        prepared = [frame for frame in conversation.frames if frame.get("type") == "test_checkpoint_prepared"]
        require(len(prepared) == 1 and prepared[0].get("state") == expected_state,
                "recovery did not prepare exactly one correct checkpoint")
        recovered = _receipts(fixture, session_file)
        _assert_receipts(recovered, 64)
        checkpoints = _checkpoints(recovered)
        require(recovered[:63] == original and len(checkpoints) == 1 and checkpoints[0] == recovered[-1],
                "recovery changed old receipts or committed more than one checkpoint")
        checkpoint = checkpoints[0]
        require(checkpoint["details"]["state"] == expected_state and
                checkpoint["details"]["delta"] == prepared[0].get("delta"),
                "accepted checkpoint differs from the actual production preparation")
        require(checkpoint["details"]["delta"]["upserts"][0]["content"] == NOTE_64 and
                canonical(recovered_wire).count(NOTE_64) == 1,
                "version-64 current note was missing or duplicated on the model wire")
        start = len(fixture.server.requests)
        unchanged_result = await conversation.prompt("CHECKPOINT_UNCHANGED_V64")
        require(unchanged_result.get("status") == "completed", "unchanged follow-up did not complete")
        unchanged_wire = _one_request(fixture, start, "CHECKPOINT_UNCHANGED_V64")
        unchanged_requests = len(fixture.server.requests) - start
        stable = compare_requests(recovered_wire, unchanged_wire)
        require(stable["system_equal"] and stable["history_prefix_equal"],
                "unchanged follow-up rewrote the stable system/history prefix")
        require(canonical(unchanged_wire).count(NOTE_64) == 1,
                "unchanged follow-up duplicated current Memory")
        require(sum(frame.get("type") == "test_checkpoint_prepared" for frame in conversation.frames) == 1,
                "unchanged follow-up prepared another checkpoint")
        require(_receipts(fixture, session_file) == recovered,
                "unchanged follow-up persisted another Memory receipt/checkpoint")
        require((await conversation.state()).get("isSettled") is True, "recovery follow-up did not settle")
    final = _receipts(fixture, session_file)
    require(final == recovered, "graceful recovery shutdown changed accepted Memory receipts")
    _assert_receipts(final, 64)
    return {"session_file": str(session_file), "seed_receipts": len(original),
            "seed_checkpoints": len(_checkpoints(original)),
            "crash": {"prepared": crash_prepared, "exit_code": exit_code,
                      "accepted_receipts": len(after_crash), "model_requests": crash_requests,
                      "checkpoints": len(_checkpoints(after_crash)), "original_receipts_unchanged": after_crash == original},
            "cancel": {"prepared": cancel_prepared, "paused": paused["type"] == "test_checkpoint_paused",
                       "abort_acknowledged": abort_response["success"],
                       "status": cancelled_result["status"], "accepted_receipts": len(after_cancel),
                       "model_requests": cancel_requests, "checkpoints": len(_checkpoints(after_cancel)),
                       "original_receipts_unchanged": after_cancel == original},
            "recovery": {"accepted_receipts": len(final), "committed_checkpoints": len(_checkpoints(final)),
                         "checkpoint": checkpoint, "normal_model_requests": recovery_requests,
                         "current_note_version": checkpoint["details"]["delta"]["upserts"][0]["version"],
                         "current_note_occurrences": canonical(recovered_wire).count(NOTE_64)},
            "unchanged": {**stable, "accepted_receipts": len(final),
                          "normal_model_requests": unchanged_requests,
                          "committed_checkpoints": len(_checkpoints(final)),
                          "current_note_occurrences": canonical(unchanged_wire).count(NOTE_64)},
            "total_loopback_requests": len(fixture.server.requests)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write synthetic checkpoint observations as JSON")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pw-memory-checkpoint-") as directory:
        with CacheHarness(Path(directory)) as fixture:
            result = asyncio.run(verify_memory_checkpoint(fixture))
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if arguments.output:
        arguments.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
