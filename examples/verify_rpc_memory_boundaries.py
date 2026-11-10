"""Isolated real OMP RPC Memory boundary proof; loopback usage is synthetic.

The administrative commands run in raw RPC mode, not scheduler lifecycle D.
No user profile, external provider, or fabricated Memory receipt is used.
"""

import argparse
import asyncio
from importlib.resources import files
import json
import os
from pathlib import Path
import sys
import tempfile

try:
    from .verify_prompt_cache import (
        CacheHarness, TIMEOUT, WORKFLOW, canonical, compare_requests,
        history_blocks, memory_deltas, read_archive, require, system_blocks,
    )
except ImportError:
    from verify_prompt_cache import (
        CacheHarness, TIMEOUT, WORKFLOW, canonical, compare_requests,
        history_blocks, memory_deltas, read_archive, require, system_blocks,
    )


class RpcConversation:
    """Small JSONL transport over one actual OMP process, without history state."""

    def __init__(self, fixture, extension, *, environment=None, session_file=None,
                 name="rpc-memory-boundary"):
        self.fixture = fixture
        self.extension = extension
        self.environment = fixture.env if environment is None else environment
        self.session_file = session_file
        self.name = name
        self.process = None
        self.frames = []
        self.stderr_task = None
        self._sequence = 0

    async def __aenter__(self):
        argv = [*self.fixture.probe.argv(self.name, [self.extension]), "--mode", "rpc", "--no-ui"]
        if self.session_file is not None:
            argv.extend(["--resume", str(self.session_file)])
        self.process = await asyncio.create_subprocess_exec(
            *argv, env=self.environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=1048576, start_new_session=True)
        self.stderr_task = asyncio.create_task(self.process.stderr.read())
        try:
            await self.until(lambda frame: frame.get("type") == "ready")
        except BaseException:
            await self.__aexit__(*sys.exc_info())
            raise
        return self

    async def until(self, predicate):
        async with asyncio.timeout(TIMEOUT):
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    await self.process.wait()
                    errors = (await self.stderr_task).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"{self.name}: OMP exited ({self.process.returncode}) before the expected RPC frame: {errors[-2000:]}")
                frame = json.loads(line)
                self.frames.append(frame)
                if predicate(frame):
                    return frame

    async def send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def command(self, type, **fields):
        self._sequence += 1
        command_id = f"{self.name}-{self._sequence}"
        await self.send({**fields, "type": type, "id": command_id})
        response = await self.until(
            lambda frame: frame.get("type") == "response" and frame.get("id") == command_id)
        require(response.get("success") is True,
                f"{self.name}: {type} failed: {canonical(response)}")
        return response

    async def prompt(self, message):
        response = await self.command("prompt", message=message)
        # Source-supported local slash commands return only a response, not a
        # prompt_result. Call command('prompt', ...) for those commands.
        require(response.get("data", {}).get("agentInvoked") is not False,
                "local-only RPC command has no terminal prompt_result; use command()")
        return await self.until(lambda frame: frame.get("type") == "prompt_result"
                                and frame.get("id") == response["id"])

    async def state(self):
        return (await self.command("get_state"))["data"]

    async def __aexit__(self, exc_type, _exc, _traceback):
        if self.process is None:
            return
        already_exited = self.process.returncode is not None
        forced = False
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 10)
        except TimeoutError:
            forced = True
            self.process.kill()
            await self.process.wait()
        errors = await self.stderr_task
        # Checkpoint fault probes deliberately observe a killed process before
        # leaving this context; do not turn that expected failure into another.
        if exc_type is None and not already_exited and not forced:
            require(self.process.returncode == 0,
                    f"{self.name}: OMP exited ({self.process.returncode}): "
                    + errors.decode("utf-8", errors="replace")[-2000:])


def memory_environment(fixture, *, name="rpc-memory-data.json"):
    """Configure the production extension to recall through the real IPC socket."""
    path = fixture.root / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"enabled": True,
                   "scope": {"workflow_id": WORKFLOW, "task_id": "training"},
                   "run_id": "test", "socket_path": str(fixture.config.server.socket_path)}, stream)
    return {**fixture.env, "PAVEWAY_MEMORY_DATA": str(path)}


def build_labels(fixture):
    manifest_path = Path(fixture.env["PAVEWAY_OMP_CACHE"]) / "runtime.json"
    runtime = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"executable": str(fixture.probe.executable),
            **{key: runtime[key] for key in ("source", "commit", "patch", "bun")}}


def current_request(body, marker):
    """Match raw OMP requests without requiring the Native Paveway Core system."""
    messages = body.get("messages", [])
    if not messages or messages[-1].get("role") != "user":
        return False
    content = messages[-1].get("content")
    texts = [content] if isinstance(content, str) else [
        block.get("text", "") for block in content or [] if isinstance(block, dict)]
    return any(marker in text or text.startswith("Runtime Memory Context —") for text in texts) and not any(
        "<title-request>" in text or "Ephemeral side-channel turn" in text for text in texts)


async def capture_prompt(conversation, marker, padding=""):
    fixture = conversation.fixture
    start = len(fixture.server.requests)
    terminal = await conversation.prompt(marker + padding)
    require(terminal.get("status") == "completed" and terminal.get("sessionSettled") is True,
            f"RPC prompt did not complete and settle: {canonical(terminal)}")
    state = await conversation.state()
    require(state.get("isSettled") is True and not state.get("isStreaming") and not state.get("isCompacting"),
            "RPC session remains active after terminal prompt_result")
    candidates = [body for body in fixture.server.requests[start:] if current_request(body, marker)]
    require(len(candidates) == 1, "expected one actual main provider request for " + marker)
    require(marker in canonical(history_blocks(candidates[0])), "current input missing from actual request")
    return candidates[0]


def effective_notes(body):
    """Read the version/withdrawal semantics from the actual model-visible data."""
    notes = {}
    for delta in memory_deltas(body):
        if delta.get("restore"):
            catalogue = {note["key"] for note in delta["current_keys_at_compaction"]}
            notes = {key: note for key, note in notes.items() if key in catalogue}
        for note in delta["withdrawals"]:
            notes.pop(note["key"], None)
        for note in delta["upserts"]:
            notes[note["key"]] = note["content"]
    return notes


async def verify_rpc_memory_boundaries(fixture):
    """Prove supported raw administrative RPC semantics on the prepared host.

    In pinned OMP /clear is TUI-only. RPC forwards it as ordinary user input;
    it must not be represented as a reset proof. Native covers real /clear.
    """
    keep = "RPC_BOUNDARY_CURRENT_KEEP"
    old = "RPC_BOUNDARY_DELETED_OLD"
    previous = "RPC_BOUNDARY_UPDATE_OLD"
    updated = "RPC_BOUNDARY_UPDATE_CURRENT"
    added = "RPC_BOUNDARY_ADDED_CURRENT"
    latest = "RPC_BOUNDARY_UPDATE_AFTER_COMPACTION"
    extension = Path(str(files("oicoach_runtime").joinpath("bundled/paveway-rpc-memory.mjs")))
    environment = memory_environment(fixture)
    scope = {"workflow_id": WORKFLOW, "task_id": "training"}

    def put(key, content, version=0):
        return fixture.api("memory.put", {"workflow_id": WORKFLOW, "key": key,
            "title": key, "content": content, "expected_version": version, "pinned": True})

    def delete(key, version):
        return fixture.api("memory.delete", {"workflow_id": WORKFLOW, "key": key,
                                             "expected_version": version})

    def receipt_count(path):
        return sum(entry.get("type") == "custom_message"
                   and entry.get("customType") == "paveway-memory-context"
                   for entry in read_archive(path, fixture.root))

    def assert_current(body, expected):
        require(effective_notes(body) == expected,
                "actual RPC Memory catalogue differs from current fixture facts")

    def stable(before, after, label):
        comparison = compare_requests(before, after)
        require(comparison["system_equal"] and comparison["history_prefix_equal"],
                label + ": stable system or prior history prefix was rewritten")
        return comparison

    def withdrawal(body, key):
        return any(note["key"] == key and note["reason"] == "deleted"
                   for delta in memory_deltas(body) for note in delta["withdrawals"])

    fixture.note(keep)
    deleted_note = put("rpc-delete", old)
    updated_note = put("rpc-update", previous)
    initial = {"cache-note": keep, "rpc-delete": old, "rpc-update": previous}
    recalled = fixture.api("memory.recall", scope)
    require({note["key"]: note["content"] for note in recalled["notes"]} == initial,
            "real IPC recall did not return all seeded notes")
    wires, comparisons = {}, {}
    # Padding is synthetic history, sufficient to exercise a real soft summary.
    padding = " synthetic RPC boundary history" * 300

    async with RpcConversation(fixture, extension, environment=environment) as rpc:
        wires["initial"] = await capture_prompt(rpc, "RPC_BOUNDARY_BEFORE", padding)
        state = await rpc.state()
        session_file = Path(state["sessionFile"])
        require(session_file.is_relative_to(fixture.root) and session_file.is_file(),
                "OMP did not create an isolated actual session archive")
        session_id = state["sessionId"]
        require(any(frame.get("type") == "paveway_memory_ready" and frame.get("runId") == "test"
                    for frame in rpc.frames), "production RPC Memory extension did not initialize")
        assert_current(wires["initial"], initial)
        require(all(canonical(history_blocks(wires["initial"])).count(text) == 1 for text in initial.values()),
                "initial real Memory notes were missing or duplicated")
        wires["appended"] = await capture_prompt(rpc, "RPC_BOUNDARY_APPEND", padding)
        comparisons["append"] = stable(wires["initial"], wires["appended"], "append")

        disk_prefix = session_file.read_bytes()
        receipts_before_clear = receipt_count(session_file)
        boundaries_before = sum(entry.get("type") == "reset_boundary"
                                for entry in read_archive(session_file, fixture.root))
        request_start = len(fixture.server.requests)
        clear_result = await rpc.prompt("/clear")
        require(clear_result.get("status") == "completed" and clear_result.get("agentInvoked") is True
                and clear_result.get("sessionSettled") is True,
                "pinned raw RPC /clear should complete as ordinary model input, not a local reset")
        require((await rpc.state())["isSettled"] is True, "RPC /clear input did not settle")
        clear_wires = [body for body in fixture.server.requests[request_start:] if current_request(body, "/clear")]
        require(len(clear_wires) == 1, "raw RPC /clear did not reach exactly one loopback model request")
        wires["clear_input"] = clear_wires[0]
        comparisons["clear_is_ordinary_input"] = stable(wires["appended"], wires["clear_input"], "RPC /clear")
        require("RPC_BOUNDARY_BEFORE" in canonical(history_blocks(wires["clear_input"])),
                "raw RPC /clear unexpectedly discarded prior user input")
        require(session_file.read_bytes().startswith(disk_prefix),
                "raw RPC /clear rewrote the persisted disk prefix")
        entries_after_clear = read_archive(session_file, fixture.root)
        boundaries_after = sum(entry.get("type") == "reset_boundary" for entry in entries_after_clear)
        require(boundaries_after == boundaries_before == 0,
                "pinned raw RPC /clear unexpectedly appended a reset_boundary")
        require(receipt_count(session_file) == receipts_before_clear,
                "raw RPC /clear resent an unchanged Memory receipt")
        wires["steady"] = await capture_prompt(rpc, "RPC_BOUNDARY_UNCHANGED")
        comparisons["unchanged"] = stable(wires["clear_input"], wires["steady"], "unchanged")
        receipts_after_unchanged = receipt_count(session_file)
        require(receipts_after_unchanged == receipts_before_clear,
                "unchanged RPC input appended a redundant Memory receipt")
        assert_current(wires["steady"], initial)
        require(all(canonical(history_blocks(wires["steady"])).count(text) == 1 for text in initial.values()),
                "unchanged RPC turns duplicated current notes")

        delete("rpc-delete", deleted_note["version"])
        updated_note = put("rpc-update", updated, updated_note["version"])
        added_note = put("rpc-add", added)
        current = {"cache-note": keep, "rpc-update": updated, "rpc-add": added}
        wires["mutated"] = await capture_prompt(rpc, "RPC_BOUNDARY_MUTATED", padding)
        comparisons["mutation"] = stable(wires["steady"], wires["mutated"], "update/delete/new")
        require(withdrawal(wires["mutated"], "rpc-delete"), "wire has no explicit deleted-note withdrawal")
        assert_current(wires["mutated"], current)
        receipts_before_resume = receipt_count(session_file)
        first_pid = rpc.process.pid

    async with RpcConversation(fixture, extension, environment=environment,
                               session_file=session_file, name="rpc-memory-resume") as resumed:
        state = await resumed.state()
        require(Path(state["sessionFile"]) == session_file and state["sessionId"] == session_id,
                "--resume did not reopen the same physical session")
        require(resumed.process.pid != first_pid, "resume did not use a fresh OMP process")
        wires["resumed"] = await capture_prompt(resumed, "RPC_BOUNDARY_RESUMED", padding)
        comparisons["resume"] = stable(wires["mutated"], wires["resumed"], "--resume")
        require(receipt_count(session_file) == receipts_before_resume,
                "fresh RPC process duplicated unchanged Memory")
        assert_current(wires["resumed"], current)
        before_compact = session_file.read_bytes()
        summary_start = len(fixture.server.requests)
        compact_response = await resumed.command("compact")
        summary_requests = fixture.server.requests[summary_start:]
        require(summary_requests, "actual session.compact made no loopback summarizer request")
        state = await resumed.state()
        require(state["isSettled"] is True and state["isCompacting"] is False,
                "compact command returned before the session settled")
        compact_entries = [entry for entry in read_archive(session_file, fixture.root)
                           if entry.get("type") == "compaction"]
        require(len(compact_entries) == 1, "compact did not persist exactly one actual compaction entry")
        compaction = compact_entries[0]
        preserved = compaction.get("preserveData", {}).get("pavewayMemory", {})
        require({note["key"]: note["content"] for note in preserved.get("notes", [])} == current,
                "real session.compacting did not preserve current Memory")
        require(session_file.read_bytes().startswith(before_compact),
                "real compaction rewrote the original archive prefix")
        wires["compacted"] = await capture_prompt(resumed, "RPC_BOUNDARY_COMPACTED")
        require(system_blocks(wires["compacted"]) == system_blocks(wires["resumed"]),
                "actual compact changed stable system blocks")
        assert_current(wires["compacted"], current)
        restores = [delta for delta in memory_deltas(wires["compacted"]) if delta.get("restore")]
        require(len(restores) == 1, "actual compact lacks its one frozen Memory recovery frame")
        require(all(canonical(history_blocks(wires["compacted"])).count(text) == 1 for text in current.values()),
                "actual compact lost or duplicated a current fact")
        compact_receipts = receipt_count(session_file)
        wires["compacted_steady"] = await capture_prompt(resumed, "RPC_BOUNDARY_COMPACT_UNCHANGED")
        comparisons["compacted_unchanged"] = stable(
            wires["compacted"], wires["compacted_steady"], "unchanged compaction recovery")
        require(receipt_count(session_file) == compact_receipts,
                "unchanged compaction recovery appended a redundant Memory receipt")
        updated_note = put("rpc-update", latest, updated_note["version"])
        delete("rpc-add", added_note["version"])
        final_current = {"cache-note": keep, "rpc-update": latest}
        wires["after_compaction_changes"] = await capture_prompt(resumed, "RPC_BOUNDARY_COMPACT_CHANGE_DELETE")
        comparisons["frozen_restore_after_changes"] = stable(
            wires["compacted_steady"], wires["after_compaction_changes"], "postcompact update/delete")
        require([delta for delta in memory_deltas(wires["after_compaction_changes"]) if delta.get("restore")] == restores,
                "a later update/deletion changed the frozen compaction recovery frame")
        require(withdrawal(wires["after_compaction_changes"], "rpc-add"),
                "postcompact deletion has no explicit actual-wire withdrawal")
        assert_current(wires["after_compaction_changes"], final_current)
        final_prefix = session_file.read_bytes()
        final_receipts = receipt_count(session_file)
        wires["final_clear_input"] = await capture_prompt(resumed, "/clear")
        comparisons["final_clear_is_ordinary_input"] = stable(
            wires["after_compaction_changes"], wires["final_clear_input"], "final RPC /clear")
        assert_current(wires["final_clear_input"], final_current)
        require(receipt_count(session_file) == final_receipts,
                "another ordinary /clear input resent superseded Memory")
        require(session_file.read_bytes().startswith(final_prefix),
                "final RPC input changed persisted history")
        state = await resumed.state()

    final_entries = read_archive(session_file, fixture.root)
    final_boundaries = sum(entry.get("type") == "reset_boundary" for entry in final_entries)
    require(final_boundaries == 0, "raw RPC path falsely produced a Native reset boundary")
    archive_text = session_file.read_text(encoding="utf-8")
    require(all(marker in archive_text for marker in
                ("RPC_BOUNDARY_BEFORE", "RPC_BOUNDARY_APPEND", "RPC_BOUNDARY_MUTATED", "/clear")),
            "administrative RPC operations discarded original persisted user input")
    return {
        "build": build_labels(fixture),
        "transport": "real OMP JSONL RPC + real Runtime IPC + loopback HTTP/SSE",
        "usage": "synthetic loopback provider fixture; not paid-provider cache evidence",
        "clear": {"rpc_behavior": "ordinary model input; /clear is Native/TUI-only",
                  "agent_invoked": clear_result["agentInvoked"],
                  "status": clear_result["status"], "reset_boundaries": final_boundaries,
                  "unchanged_receipts_before": receipts_before_clear,
                  "unchanged_receipts_after": receipts_after_unchanged},
        "comparisons": comparisons,
        "resume": {"same_session_file": Path(state["sessionFile"]) == session_file,
                   "same_session_id": state["sessionId"] == session_id, "fresh_process": resumed.process.pid != first_pid},
        "compaction": {"persisted_entries": len(compact_entries), "id": compaction["id"],
                       "summary_provider_requests": len(summary_requests),
                       "response": compact_response, "frozen_restore": restores},
        "archive": {"session_file": str(session_file), "original_disk_prefix_preserved":
                    session_file.read_bytes().startswith(disk_prefix),
                    "original_user_input_retained": "RPC_BOUNDARY_BEFORE" in archive_text,
                    "memory_receipts": receipt_count(session_file)},
        "current_notes": effective_notes(wires["final_clear_input"]),
        "wire": wires,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="paveway-rpc-memory-boundaries-") as root:
        with CacheHarness(root) as fixture:
            result = asyncio.run(verify_rpc_memory_boundaries(fixture))
            print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
