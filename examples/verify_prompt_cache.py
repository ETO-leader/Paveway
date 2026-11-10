"""Capture real OMP requests against a disposable loopback provider.

Run with the project interpreter; no credentials, user profiles or paid calls
are used. A/B/C/E exercise the integrated native entrypoint; D exercises the
scheduler's OmpRpc lifecycle. --output stores complete synthetic wire bodies
and persisted normalized usage. Fixture cache numbers are NOT real cache proof,
and OMP calculated costs are not an invoice. A failed invariant exits nonzero.
"""

import argparse
from contextlib import contextmanager
from dataclasses import replace
import fcntl
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import pyte

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.native_host import prepare_native_host
from oicoach_runtime.workflow_setup import create

try:
    from .verify_omp_m0 import Probe, Provider, WideScreen
except ImportError:
    from verify_omp_m0 import Probe, Provider, WideScreen


WORKFLOW = "cache-probe"
NOTE_OLD = "PAVEWAY_CACHE_NOTE_VERSION_ONE"
NOTE_NEW = "PAVEWAY_CACHE_NOTE_VERSION_TWO"
REPLY = "PAVEWAY_CACHE_FIXTURE_REPLY"
TIMEOUT = 60


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def system_blocks(body):
    """Keep the actual provider representation, not a reconstructed prompt."""
    return [message for message in body.get("messages", [])
            if message.get("role") in ("system", "developer")]


def history_blocks(body):
    return [message for message in body.get("messages", [])
            if message.get("role") not in ("system", "developer")]

def memory_deltas(body):
    result = []
    for message in body.get("messages", []):
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            block.get("text", "") for block in content or [] if isinstance(block, dict)]
        for text in texts:
            try:
                value = json.loads(text.split("\n")[-1])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("upserts"), list) and isinstance(value.get("withdrawals"), list):
                result.append(value)
    return result

def main_request(body, marker):
    messages = body.get("messages", [])
    if not messages or messages[-1].get("role") != "user":
        return False
    tail = messages[-1].get("content")
    texts = [tail] if isinstance(tail, str) else [
        block.get("text", "") for block in tail or [] if isinstance(block, dict)]
    # Native title/card side channels deliberately reuse the system AND history.
    # A submitted prompt ends with its user text or the staged Memory companion.
    return ("# Paveway Core" in canonical(system_blocks(body)) and
            any(marker in text or text.startswith("Runtime Memory Context —") for text in texts) and
            not any("<title-request>" in text or "Ephemeral side-channel turn" in text for text in texts))


def compare_requests(before, after):
    old, new = history_blocks(before), history_blocks(after)
    left, right = canonical(before).encode(), canonical(after).encode()
    common = 0
    for a, b in zip(left, right):
        if a != b:
            break
        common += 1
    return {"system_equal": system_blocks(before) == system_blocks(after),
            "history_prefix_equal": new[:len(old)] == old,
            "canonical_request_common_prefix_bytes": common,
            "history_messages_before": len(old), "history_messages_after": len(new)}


def read_archive(path, root):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()),
            "OMP archive escaped the disposable fixture")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def normalized_usage(entries):
    """Observe persisted OMP normalization; never infer provenance from zeros."""
    rows = []
    for entry in entries:
        message = entry.get("message", {})
        if entry.get("type") != "message" or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        rows.append({"entry_id": entry.get("id"), "provider": message.get("provider"),
                     "model": message.get("model"), "omp_normalized_usage": usage,
                     "cache_read_evidence": "positive_fixture_value" if usage.get("cacheRead", 0) > 0
                     else "unavailable_zero_or_missing_provenance",
                     "cache_write_evidence": "positive_fixture_value" if usage.get("cacheWrite", 0) > 0
                     else "unavailable_zero_or_missing_provenance",
                     "input_cache_ratio": None,
                     "ratio_reason": "OMP normalized input = uncached input + cacheRead + cacheWrite; "
                     "zero fields lack raw provenance",
                     "cost_label": "OMP calculated; synthetic fixture prices, not actual invoice"})
    return rows


class CacheProvider(Provider):
    """A real HTTP/SSE transport with deliberately synthetic cache usage."""

    def do_POST(self):
        if getattr(self.server, "expose_usage", True) is False:
            return super().do_POST()
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in (({"role": "assistant", "content": REPLY}, None), ({}, "stop")):
            chunk = {"id": "cache-fixture", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        # No cache-write field: the absence must not become a claimed reported zero.
        usage = {"prompt_tokens": 160, "completion_tokens": 8, "total_tokens": 168,
                 "prompt_tokens_details": {"cached_tokens": 96}}
        self.wfile.write(("data: " + json.dumps({"id": "cache-fixture", "choices": [],
                                                "usage": usage}) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


class NativeConversation:
    def __init__(self, fixture):
        self.fixture = fixture
        previous = fixture.api("frontend.list")
        generation = previous[0]["generation"] if previous else 0
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.output = bytearray()
        self.screen = WideScreen(120, 40)
        self.stream = pyte.ByteStream(self.screen)
        self.process = None
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "oicoach_runtime", "native", "--config", str(fixture.path),
                 "--workflow", WORKFLOW], env=fixture.env, stdin=slave, stdout=slave, stderr=slave)
        finally:
            os.close(slave)
        try:
            def ready():
                records = fixture.api("frontend.list")
                return (b"PAVEWAY" in self.output and records and records[0]["status"] == "idle" and
                        records[0]["generation"] > generation)
            self.until(ready)
            self.record = fixture.api("frontend.list")[0]
            # Exercise the live editor without submitting a model request.
            self.send("\x1b[200~CACHE_UI_READY\x1b[201~")
            self.until(lambda: "CACHE_UI_READY" in "\n".join(self.screen.display))
            self.send("\x15")
            self.until(lambda: "CACHE_UI_READY" not in "\n".join(self.screen.display))
        except BaseException:
            stop_process(self.process)
            os.close(self.master)
            raise

    def pump(self):
        if select.select([self.master], [], [], 0.05)[0]:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                data = b""
            self.output.extend(data)
            self.stream.feed(data)
            if b"\x1b[6n" in data:
                os.write(self.master, b"\x1b[1;1R")

    def until(self, predicate):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            self.pump()
            if predicate():
                return
            if self.process.poll() is not None:
                raise RuntimeError(f"native OMP exited ({self.process.returncode}) before the requested fixture state: " +
                                   self.output.decode(errors="replace")[-3000:])
        raise TimeoutError("native OMP did not reach the requested fixture state: " +
                           self.output.decode(errors="replace")[-1500:])

    def send(self, text):
        pending = memoryview(text.encode("utf-8"))
        deadline = time.monotonic() + TIMEOUT
        while pending and time.monotonic() < deadline:
            self.pump()
            if select.select([], [self.master], [], 0.05)[1]:
                pending = pending[os.write(self.master, pending[:4096]):]
        require(not pending, "native OMP did not consume fixture input")

    def prompt(self, marker, padding=""):
        start = len(self.fixture.server.requests)
        self.send("\x1b[200~" + marker + padding + "\x1b[201~\r")

        def settled():
            candidates = self.fixture.server.requests[start:]
            if not any(main_request(body, marker) for body in candidates):
                return False
            record = self.fixture.api("frontend.show", {"conversation_id": self.record["conversation_id"]})
            if record["status"] != "idle":
                return False
            self.record = record
            entries = read_archive(record["session_file"], self.fixture.root)
            messages = [entry.get("message", {}) for entry in entries if entry.get("type") == "message"]
            # The preceding idle state can briefly survive a newly submitted turn.
            return bool(messages and messages[-1].get("role") == "assistant" and
                        any(marker in canonical(message) for message in messages))

        self.until(settled)
        candidates = [body for body in self.fixture.server.requests[start:] if main_request(body, marker)]
        require(len(candidates) == 1, "expected one actual provider request per native prompt")
        entries = read_archive(self.record["session_file"], self.fixture.root)
        raw = marker + padding
        require(any(entry.get("type") == "message" and entry.get("message", {}).get("role") == "user" and
                    (entry["message"].get("content") == raw or
                     any(part.get("text") == raw for part in entry["message"].get("content", [])
                         if isinstance(part, dict))) for entry in entries),
                "native Memory integration changed the persisted raw user message")
        return candidates[0]

    def compact(self):
        path = self.record["session_file"]
        before = sum(entry.get("type") == "compaction" for entry in read_archive(path, self.fixture.root))
        start = len(self.fixture.server.requests)
        self.send("/compact\r")
        self.until(lambda: sum(entry.get("type") == "compaction"
                               for entry in read_archive(path, self.fixture.root)) > before)
        require(len(self.fixture.server.requests) > start,
                "real OMP compaction did not call the loopback summary model")
        return self.fixture.server.requests[start:]

    def clear(self):
        path = self.record["session_file"]
        before = read_archive(path, self.fixture.root)
        boundaries = sum(entry.get("type") == "reset_boundary" for entry in before)
        requests = len(self.fixture.server.requests)
        self.send("/clear\r")
        self.until(lambda: sum(entry.get("type") == "reset_boundary"
                               for entry in read_archive(path, self.fixture.root)) > boundaries)
        after = read_archive(path, self.fixture.root)
        old_messages = {entry["id"]: entry for entry in before if entry.get("type") in ("message", "custom_message")}
        new_messages = {entry["id"]: entry for entry in after if entry.get("type") in ("message", "custom_message")}
        require(all(new_messages.get(key) == value for key, value in old_messages.items()),
                "/clear rewrote persisted message history")
        require(len(self.fixture.server.requests) == requests, "/clear unexpectedly called the model")

    def close(self):
        try:
            if self.process.poll() is None:
                self.send("/exit\r")
                self.until(lambda: self.process.poll() is not None)
            require(self.process.returncode == 0, "integrated native fixture exited unsuccessfully")
            record = self.fixture.api("frontend.show", {"conversation_id": self.record["conversation_id"]})
            require(record["status"] == "released", "native fixture did not release its conversation")
        finally:
            stop_process(self.process)
            os.close(self.master)


class CacheHarness:
    def __init__(self, root, executable=None):
        self.root = Path(root)
        # Native always replaces its executable; consistency evidence must use that same host.
        prepared = prepare_native_host()
        require(executable is None or Path(executable).resolve() == Path(prepared).resolve(),
                "Cache consistency capture requires the prepared Paveway OMP launcher for both Native and RPC")
        executable = prepared
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CacheProvider)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.daemon = None
        self.log = None
        try:
            self.probe = Probe(self.root, executable, self.server)
            self.env = dict(self.probe.env)
            project = Path(__file__).resolve().parents[1]
            self.env["PYTHONPATH"] = str(project / "src")
            # A public patched host build may be reused; no user settings/auth are read.
            self.env["PAVEWAY_OMP_CACHE"] = os.environ.get("PAVEWAY_OMP_CACHE",
                str(Path.home() / ".cache/paveway/omp/18.16.0"))
            self.path = self.root / "runtime.toml"
            initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
            config = load_config(self.path)
            self.config = replace(config,
                server=replace(config.server, poll_interval_seconds=0.05),
                agent=replace(config.agent, executable=executable, model="paveway-m0/probe", thinking="off",
                    tools_enabled=False, skills_enabled=False, rules_enabled=False, extensions_enabled=False,
                    startup_timeout_seconds=30, ack_timeout_seconds=30, turn_timeout_seconds=60,
                    settle_timeout_seconds=20, graceful_stop_seconds=1))
            self.path.write_text(config_text(self.config))
            create(self.config, WORKFLOW, self.probe.workspace, "example")
            manifest = self.config.workflow_root / WORKFLOW / "workflow.toml"
            manifest.write_text(manifest.read_text().replace("memory_enabled = false", "memory_enabled = true"))
            config_path = self.probe.agent / "config.yml"
            omp_config = json.loads(config_path.read_text())
            omp_config.update({"modelRoles": {role: "paveway-m0/probe" for role in ("default", "smol", "slow")},
                               "compaction": {"enabled": True, "methodOrder": ["soft"],
                                              "keepRecentTokens": 128, "reserveTokens": 256}})
            config_path.write_text(json.dumps(omp_config))
            models_path = self.probe.agent / "models.yml"
            models = json.loads(models_path.read_text())
            models["providers"]["paveway-m0"]["models"][0]["cost"] = {
                "input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 1.25}
            models_path.write_text(json.dumps(models))
            self.log = (self.root / "daemon.log").open("w")
            self.start_daemon()
        except BaseException:
            self.close()
            raise

    def api(self, command, arguments=None):
        return request(self.config.server.socket_path, command, arguments)

    def start_daemon(self):
        self.daemon = subprocess.Popen(
            [sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
            env=self.env, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            require(self.daemon.poll() is None, "isolated runtime daemon exited during startup")
            try:
                if self.api("status")["daemon"] == "running":
                    return
            except RuntimeContractError:
                pass
            time.sleep(0.05)
        raise TimeoutError("isolated runtime daemon did not start")

    def note(self, content, expected_version=0):
        return self.api("memory.put", {"workflow_id": WORKFLOW, "key": "cache-note", "title": "Cache probe note",
            "content": content, "expected_version": expected_version, "pinned": True})

    @contextmanager
    def native(self):
        conversation = NativeConversation(self)
        try:
            yield conversation
        finally:
            conversation.close()

    def native_scenarios(self):
        note = self.note(NOTE_OLD)
        with self.native() as native:
            a1 = native.prompt("CACHE_A_FIRST", " synthetic historical text" * 100)
            a2 = native.prompt("CACHE_A_SECOND", " synthetic historical text" * 100)
            unchanged = compare_requests(a1, a2)
            require(unchanged["system_equal"] and unchanged["history_prefix_equal"],
                    "A: unchanged memory rewrote the wire system or prior history")
            require(canonical(a1).count(NOTE_OLD) == canonical(a2).count(NOTE_OLD) == 1,
                    "A: unchanged memory is absent or duplicated on the wire")
            self.note(NOTE_NEW, note["version"])
            b = native.prompt("CACHE_B_CHANGED", " synthetic historical text" * 100)
            changed = compare_requests(a2, b)
            require(changed["system_equal"] and changed["history_prefix_equal"],
                    "B: updating a note rewrote the wire system or earlier history")
            require(canonical(b).count(NOTE_NEW) == 1 and NOTE_NEW not in canonical(system_blocks(b)),
                    "B: updated memory is missing, duplicated, or injected into system blocks")
            require(canonical(history_blocks(a1)).find(NOTE_OLD) <
                    canonical(history_blocks(a1)).find("CACHE_A_FIRST"),
                    "A: Memory data must precede the current user input")
            require(canonical(history_blocks(b)).find(NOTE_NEW) <
                    canonical(history_blocks(b)).find("CACHE_B_CHANGED"),
                    "B: changed Memory data must precede the current user input")
            original = dict(native.record)
        # Closing the complete Paveway process forces the next native launch's --resume path.
        with self.native() as resumed:
            c = resumed.prompt("CACHE_C_RESUMED")
            require(resumed.record["session_file"] == original["session_file"],
                    "C: native restart did not resume the same physical OMP session")
            restart = compare_requests(b, c)
            require(restart["system_equal"] and restart["history_prefix_equal"],
                    "C: --resume rewrote the wire system or earlier history")
            require(canonical(c).count(NOTE_NEW) == 1, "C: restart duplicated unchanged memory")
            summary_requests = resumed.compact()
            e = resumed.prompt("CACHE_E_AFTER_COMPACTION")
            require(system_blocks(e) == system_blocks(c), "E: compaction changed stable system blocks")
            require(canonical(history_blocks(e)).count(NOTE_NEW) == 1,
                    "E: compaction lost or duplicated current selected memory")
            e2 = resumed.prompt("CACHE_E_CONTINUE_UNCHANGED")
            require(compare_requests(e, e2)["history_prefix_equal"],
                    "E: recovery frame changed on an unchanged subsequent turn")
            self.note("PAVEWAY_CACHE_NOTE_VERSION_THREE", note["version"] + 1)
            e3 = resumed.prompt("CACHE_E_CHANGE_AFTER_COMPACTION")
            require(compare_requests(e2, e3)["system_equal"] and compare_requests(e2, e3)["history_prefix_equal"],
                    "E: later Memory change rewrote the already-cached compaction recovery prefix")
            entries = read_archive(resumed.record["session_file"], self.root)
            require(any(entry.get("type") == "compaction" for entry in entries),
                    "E: missing persisted real OMP compaction entry")
            usage = normalized_usage(entries)
            require(usage, "native archive did not persist normalized usage")
            diagnostics = self.api("cache.stats", {"conversation_id": resumed.record["conversation_id"]})
            require(diagnostics["cumulative"]["observed_subtotals"]["cache_read_tokens"] > 0,
                    "native cache.stats did not expose persisted normalized usage")
            resumed.send("/paveway cache\r")
            resumed.until(lambda: b"omp_session_jsonl" in resumed.output)
            return {"A": unchanged, "B": changed, "C": restart,
                    "E": {"persisted_compaction": True, "summary_provider_requests": len(summary_requests),
                          "continued_prefix_equal": compare_requests(e, e2)["history_prefix_equal"],
                          "changed_prefix_equal": compare_requests(e2, e3)["history_prefix_equal"],
                          "history_prefix_equal": compare_requests(c, e)["history_prefix_equal"]},
                    "wire": {"A1": a1, "A2": a2, "B": b, "C": c,
                             "E_summary": summary_requests, "E": e, "E2": e2, "E3": e3},
                    "normalized_usage": usage, "diagnostics": diagnostics}

    def clear_scenarios(self):
        keep, old, changed, added, latest = (
            "CLEAR_CURRENT_KEEP", "CLEAR_OLD_DELETED", "CLEAR_OLD_UPDATE",
            "CLEAR_NEW_ADDED", "CLEAR_UPDATED_CURRENT")
        self.note(keep)

        def put(key, content, version=0):
            return self.api("memory.put", {"workflow_id": WORKFLOW, "key": key, "title": key,
                "content": content, "expected_version": version})

        put("clear-delete", old)
        put("clear-update", changed)
        with self.native() as native:
            before = native.prompt("CLEAR_BEFORE", " historical padding" * 200)
            native.compact()
            native.clear()
            unchanged = native.prompt("CLEAR_UNCHANGED_DATABASE")
            wire = canonical(history_blocks(unchanged))
            require(all(wire.count(text) == 1 for text in (keep, old, changed)),
                    "/clear lost unchanged current Memory or duplicated its snapshot")
            require("CLEAR_BEFORE" not in wire, "/clear resurrected the old conversation")
            require(system_blocks(before) == system_blocks(unchanged), "/clear changed stable system policy")
            steady = native.prompt("CLEAR_UNCHANGED_AGAIN")
            require(compare_requests(unchanged, steady)["history_prefix_equal"] and
                    canonical(history_blocks(steady)).count(keep) == 1,
                    "unchanged post-clear turn resent Memory or rewrote its prefix")
            self.api("memory.delete", {"workflow_id": WORKFLOW, "key": "clear-delete", "expected_version": 1})
            put("clear-update", latest, 1)
            put("clear-add", added)
            mutated = native.prompt("CLEAR_CHANGED_AND_DELETED")
            require(compare_requests(steady, mutated)["history_prefix_equal"],
                    "post-clear changes rewrote the existing prefix")
            require(any(note["key"] == "clear-delete" and note["reason"] == "deleted"
                        for delta in memory_deltas(mutated) for note in delta["withdrawals"]),
                    "deletion has no explicit withdrawal on the actual wire")
            native.clear()
            clean = native.prompt("CLEAR_AFTER_MUTATIONS")
            visible = canonical(history_blocks(clean))
            require(all(visible.count(text) == 1 for text in (keep, latest, added)),
                    "new epoch failed to restore current note versions")
            require(old not in visible and changed not in visible and "CLEAR_UNCHANGED_DATABASE" not in visible,
                    "old receipts crossed reset boundary")
            original = dict(native.record)
        with self.native() as resumed:
            restarted = resumed.prompt("CLEAR_RESTARTED")
            require(resumed.record["session_file"] == original["session_file"] and
                    compare_requests(clean, restarted)["history_prefix_equal"],
                    "post-clear restart did not preserve the effective context prefix")
            require(canonical(history_blocks(restarted)).count(keep) == 1,
                    "restart resent unchanged Memory")
            resumed.compact()
            compacted = resumed.prompt("CLEAR_COMPACTED")
            require(keep in canonical(compacted) and old not in canonical(compacted),
                    "post-clear compaction lost current Memory or revived deleted Memory")
            put("clear-update", "CLEAR_UPDATED_AFTER_COMPACTION", 2)
            self.api("memory.delete", {"workflow_id": WORKFLOW, "key": "clear-add", "expected_version": 1})
            final = resumed.prompt("CLEAR_COMPACTION_UPDATE_DELETE")
            require(compare_requests(compacted, final)["history_prefix_equal"],
                    "post-clear compaction recovery changed after later update/deletion")
            require("CLEAR_UPDATED_AFTER_COMPACTION" in canonical(final),
                    "latest update is missing after compaction")
            require(any(note["key"] == "clear-add" and note["reason"] == "deleted"
                        for delta in memory_deltas(final) for note in delta["withdrawals"]),
                    "post-compaction deletion has no explicit withdrawal")
            resumed.clear()
            final_clean = resumed.prompt("CLEAR_FINAL_EPOCH")
            final_wire = canonical(history_blocks(final_clean))
            require(keep in final_wire and "CLEAR_UPDATED_AFTER_COMPACTION" in final_wire and
                    all(text not in final_wire for text in (old, changed, added, latest)),
                    "old Memory/compaction state crossed the final clear")
        return {"unchanged_reinjected_once": True, "old_history_preserved": True,
                "restart_prefix_equal": True, "compaction_then_changes_prefix_equal": True,
                "wire": {"before": before, "unchanged": unchanged, "mutated": mutated,
                         "clean": clean, "restarted": restarted, "compacted": compacted,
                         "final": final, "final_clean": final_clean}}

    def ask(self, marker):
        start = len(self.server.requests)
        result = subprocess.run([sys.executable, "-m", "oicoach_runtime", "ask", "--config", str(self.path),
            "--workflow", WORKFLOW, "--task", "background-cache", "--message", marker,
            "--wait", "--timeout", str(TIMEOUT), "--json"], env=self.env,
            capture_output=True, text=True, timeout=TIMEOUT + 15)
        require(result.returncode == 0, "background OmpRpc fixture failed: " + result.stderr[-1000:])
        result = json.loads(result.stdout)["result"]
        require(result["event"]["status"] == "succeeded", "background OmpRpc fixture event did not succeed")
        candidates = [body for body in self.server.requests[start:] if marker in canonical(body)]
        require(len(candidates) == 1, "expected one actual provider request per background event")
        event = self.api("events.show", {"event_id": result["event"]["event_id"]})
        run = self.api("runs.show", {"run_id": event["run_id"]})
        directory = self.config.state_dir / "sessions" / run["binding_id"]
        archives = list(directory.rglob("*.jsonl"))
        require(len(archives) == 1, "background fixture must have exactly one physical OMP archive")
        return candidates[0], run, str(archives[0])

    def rpc_scenario(self):
        self.note(NOTE_OLD)
        first, run1, archive1 = self.ask("CACHE_D_FIRST")
        second, run2, archive2 = self.ask("CACHE_D_SECOND")
        comparison = compare_requests(first, second)
        require(comparison["system_equal"] and comparison["history_prefix_equal"],
                "D: fresh OmpRpc dispatch rewrote system or prior history")
        require(run1["agent_pid"] != run2["agent_pid"], "D: scheduler reused a process instead of testing fresh OmpRpc")
        require(archive1 == archive2, "D: scheduler did not resume the same OMP Session")
        require(canonical(second).count(NOTE_OLD) == 1, "D: fresh RPC process duplicated unchanged memory")
        entries = read_archive(archive2, self.root)
        usage = normalized_usage(entries)
        require(usage, "background archive did not persist normalized usage")
        diagnostics = self.api("cache.stats", {"workflow_id": WORKFLOW, "task_id": "background-cache"})
        require(diagnostics["cumulative"]["observed_subtotals"]["cache_read_tokens"] > 0,
                "background cache.stats did not expose persisted normalized usage")
        return {"D": {**comparison, "fresh_process": True, "same_session_file": True},
                "wire": {"D1": first, "D2": second}, "normalized_usage": usage, "diagnostics": diagnostics}

    def close(self):
        stop_process(self.daemon)
        if self.log:
            self.log.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omp", help="Prepared Paveway launcher only; alternate builds are rejected to keep Native/RPC comparable")
    parser.add_argument("--output", type=Path, help="Write complete synthetic capture JSON")
    arguments = parser.parse_args()
    observations = {"provider": "loopback fixture, not real cache proof", "external_calls": False,
                    "cost_label": "OMP calculated, not actual invoice", "scenarios": {}}
    try:
        for name, method in (("native", "native_scenarios"), ("rpc", "rpc_scenario"), ("clear", "clear_scenarios")):
            with tempfile.TemporaryDirectory(prefix="pw-cache-omp-") as directory:
                with CacheHarness(Path(directory), arguments.omp) as fixture:
                    capture = {"all_wire_requests": fixture.server.requests}
                    observations["scenarios"][name] = capture
                    try:
                        capture.update(getattr(fixture, method)())
                    finally:
                        capture["persisted_normalized_usage"] = [
                            {"session_file": str(path.relative_to(fixture.root)),
                             "rows": normalized_usage(read_archive(path, fixture.root))}
                            for subtree in ("native", "sessions")
                            for path in (fixture.config.state_dir / subtree).rglob("*.jsonl")]
    except Exception as error:
        observations["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if arguments.output:
            arguments.output.write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"probe": "prompt-cache", "scenarios": ["A", "B", "C", "D", "E", "clear/restart"],
                      "invariants_passed": True, "real_cache_proof": False,
                      "capture_output": str(arguments.output) if arguments.output else None}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, RuntimeContractError, TimeoutError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"probe": "prompt-cache", "invariants_passed": False,
                          "error_type": type(error).__name__, "error": str(error)}))
        raise SystemExit(1)
