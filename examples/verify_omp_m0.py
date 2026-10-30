"""M0 native OMP probes; isolated HOME, no credentials or external model calls.

Run with the project interpreter. JSONL observations are not a managed-TUI
certification. Exit 2 means a required fail-closed property was disproved;
exit 1 means the probe itself failed. The loopback provider is a fixture,
not evidence that a real model, Browser, Subagent or Tijian works.
"""

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

import pyte

# This checkout-only acceptance harness already runs the repository's tests.
# Use their strict wide-cell decoder for the same PTY byte-boundary semantics.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from terminal_screen import WideScreen


def emit(name, **values):
    print(json.dumps({"probe": name, **values}, ensure_ascii=False), flush=True)


class Provider(BaseHTTPRequestHandler):
    """Capture the actual provider request after OMP's context transforms."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        response = self.server.response_factory(body) if hasattr(self.server, "response_factory") else (
            ({"role": "assistant", "content": "M0_REPLY"}, None), ({}, "stop"))
        for delta, finish in response:
            chunk = {"id": "m0", "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path != "/paveway-browser-fixture":
            self.send_error(404)
            return
        data = b"<!doctype html><title>PAVEWAY_BROWSER_OK</title><h1>Local native browser fixture</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Probe:
    def __init__(self, root, executable, server):
        self.root, self.executable, self.server = root, executable, server
        self.agent, self.workspace = root / "agent", root / "workspace"
        self.agent.mkdir()
        self.workspace.mkdir()
        # An allowlist avoids inheriting auth, model overrides, proxy values or
        # parent-agent configuration. Never load the user's real profile.
        self.env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(root),
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "xterm-256color",
                    "PI_CODING_AGENT_DIR": str(self.agent)}
        for kind in ("CONFIG", "STATE", "DATA", "CACHE"):
            self.env[f"XDG_{kind}_HOME"] = str(root / kind.lower())
        (self.agent / "config.yml").write_text(json.dumps({
            "startup": {"setupWizard": False, "showSplash": False}}))
        (self.agent / "models.yml").write_text(json.dumps({"providers": {"paveway-m0": {
            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "auth": "none",
            "api": "openai-completions", "models": [{"id": "probe", "name": "probe",
                "reasoning": False, "input": ["text"], "contextWindow": 128000, "maxTokens": 64}]}}}))
        self.failure = root / "throwing-context.mjs"
        self.failure.write_text('export default function(pi) { pi.on("context", () => { '
                                'throw new Error("M0_CONTEXT_FAILURE"); }); }')
        self.guard = Path(str(files("oicoach_runtime").joinpath("bundled/context-window.mjs")))
        self.guard_faults = []
        for stage in ("registration", "context"):
            extension = root / f"guard-{stage}-fault.mjs"
            callback = ('throw new Error("M0_GUARD_REGISTRATION_FAULT");' if stage == "registration" else
                'pi.on(event, original => handler({get messages() { '
                'throw new Error("M0_GUARD_CONTEXT_FAULT"); }}));')
            extension.write_text(f"import guard from {json.dumps(str(self.guard))};\n"
                f"export default async function(pi) {{ await guard({{on(event, handler) {{ {callback} }}}}); }}")
            self.guard_faults.append((stage, extension))

    def argv(self, name, extensions):
        return [self.executable, "--cwd", str(self.workspace), "--session-dir", str(self.root / name),
                "--no-tools", "--no-skills", "--no-rules", "--no-extensions", "--no-title",
                "--model", "paveway-m0/probe",
                *[arg for extension in extensions for arg in ("--extension", str(extension))]]

    async def rpc(self, name, extensions, message):
        before = len(self.server.requests)
        process = await asyncio.create_subprocess_exec(
            *self.argv(name, extensions), "--mode", "rpc", "--no-ui", env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=1048576, start_new_session=True)
        stderr = asyncio.create_task(process.stderr.read())
        frames = []

        async def until(predicate):
            async with asyncio.timeout(40):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        raise RuntimeError(f"{name}: OMP closed RPC before the expected frame")
                    frame = json.loads(line)
                    frames.append(frame)
                    if predicate(frame):
                        return frame

        async def send(value):
            process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()

        try:
            await until(lambda f: f.get("type") == "ready")
            await send({"type": "get_state", "id": "state"})
            state = await until(lambda f: f.get("id") == "state")
            if not state.get("success"):
                raise RuntimeError(f"{name}: get_state failed")
            await send({"type": "prompt", "id": "turn", "message": message})
            result = await until(lambda f: f.get("type") == "prompt_result" and f.get("id") == "turn")
            await send({"type": "get_state", "id": "after"})
            after = await until(lambda f: f.get("id") == "after")
            requests = self.server.requests[before:]
            wire = json.dumps([r.get("messages", []) for r in requests], ensure_ascii=False)
            session_file = Path(after.get("data", {}).get("sessionFile", ""))
            persisted = session_file.is_relative_to(self.root) and session_file.is_file()
            original_retained = persisted and message in session_file.read_text()
            observation = {"requests": len(requests), "wire_chars": len(wire),
                "context_error": any(f.get("type") == "extension_error" and f.get("event") == "context" for f in frames),
                "guard_ready": any(f.get("type") == "paveway_context_ready" and f.get("tokenLimit") == 10000 for f in frames),
                "status": result.get("status"), "is_settled": after.get("data", {}).get("isSettled"),
                "original_retained": original_retained, "omission_visible": "older conversation or oversized content was omitted" in wire}
            emit(name, **observation)
            return observation
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
            await stderr

    async def guard_failure(self, name, extension):
        before = len(self.server.requests)
        process = await asyncio.create_subprocess_exec(
            *self.argv(name, [extension]), "--mode", "rpc", "--no-ui", env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=1048576, start_new_session=True)
        stderr = asyncio.create_task(process.stderr.read())
        result_status = None
        timed_out = False
        frame_types = []
        try:
            async with asyncio.timeout(40):
                while line := await process.stdout.readline():
                    frame = json.loads(line)
                    frame_types.append(frame.get("type"))
                    if frame.get("type") == "ready":
                        process.stdin.write(b'{"type":"prompt","id":"fault","message":"M0_GUARD_FAULT_INPUT"}\n')
                        await process.stdin.drain()
                    if frame.get("type") == "prompt_result":
                        result_status = frame.get("status")
                        break
                if process.returncode is None and result_status is None:
                    await process.wait()
        except TimeoutError:
            timed_out = True
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
            errors = await stderr
        observation = {"requests": len(self.server.requests) - before,
                       "status": result_status, "exit_code": process.returncode,
                       "timed_out": timed_out, "frames": frame_types,
                       "guard_failure_reported": b"paveway_context_failed" in errors}
        emit(name, **observation)
        return observation

    def tui(self, name, extensions):
        before = len(self.server.requests)
        master, slave = pty.openpty()
        process = None
        output = bytearray()
        stream_closed = False
        screen = WideScreen(120, 40)
        stream = pyte.ByteStream(screen)
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            process = subprocess.Popen([*self.argv(name, extensions), "M0_TUI_INPUT"],
                stdin=slave, stdout=slave, stderr=slave, env=self.env, start_new_session=True)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        stream_closed = True
                        break
                    if not data:
                        stream_closed = True
                        break
                    output.extend(data)
                    stream.feed(data)
                    if b"\x1b[6n" in data:
                        os.write(master, b"\x1b[1;1R")
                if len(self.server.requests) > before and "M0_REPLY" in "\n".join(screen.display):
                    break
            if stream_closed:
                process.wait(timeout=5)
            terminal_diagnostic = b"paveway_context_failed" in output
            # OMP redirects fd 2 to its per-process log while owning the TUI.
            # Read only this child's logs under the disposable HOME/XDG tree.
            log_diagnostic = any(b"paveway_context_failed" in path.read_bytes()
                                 for path in self.root.rglob(f"omp.*.{process.pid}.log"))
            observation = {"requests": len(self.server.requests) - before,
                "context_error_visible": b"M0_CONTEXT_FAILURE" in output,
                "guard_failure_reported": terminal_diagnostic or log_diagnostic,
                "diagnostic_source": "terminal" if terminal_diagnostic else ("omp-log" if log_diagnostic else None),
                "exit_code": process.poll(),
                "reply_visible": "M0_REPLY" in "\n".join(screen.display),
                "screen": "\n".join(line.rstrip() for line in screen.display).strip()}
            emit(name, **observation)
            return observation
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if slave is not None:
                os.close(slave)
            os.close(master)

    def existing_native_checks(self):
        from oicoach_runtime.config import AgentConfig, Config, config_text
        config = self.root / "runtime.toml"
        config.write_text(config_text(Config(self.root / "runtime", self.root / "workflows",
                                            agent=AgentConfig(executable=self.executable))))
        project = Path(__file__).resolve().parents[1]
        env = dict(self.env, OICOACH_TEST_REAL_OMP_CONFIG=str(config),
                   PYTHONPATH=os.pathsep.join(str(project / name) for name in ("src", "tests")))
        checks = ["test_omp_verification.RealTransportTests.test_real_omp_transport",
                  "test_customization_omp.RealCustomizationTests.test_native_template_expands_dynamic_capabilities_without_model",
                  "test_customization_omp.RealCustomizationTests.test_native_system_prompt_append_replace_and_skill_discovery_without_model",
                  "test_customization_omp.RealCustomizationTests.test_native_builtin_and_user_skills_read_without_model",
                  "test_customization_omp.RealCustomizationTests.test_disabled_tools_or_skills_do_not_inject_a_fallback_body"]
        result = subprocess.run([sys.executable, "-m", "unittest", *checks, "-v"],
                                env=env, cwd=project, capture_output=True, text=True, timeout=180)
        # Do not print a failing assertion's full system-prompt/config snapshot.
        emit("native-transport-prompt-skills", exit_code=result.returncode, checks=checks)
        return result.returncode == 0


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guard-faults-only", action="store_true",
                        help="Exercise actual Paveway Guard failures, not generic OMP Hook failures")
    arguments = parser.parse_args()
    executable = shutil.which("omp")
    if not executable:
        raise RuntimeError("omp is not installed")
    with tempfile.TemporaryDirectory(prefix="pw-m0-") as directory:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            probe = Probe(Path(directory), executable, server)
            version = subprocess.run([executable, "--version"], env=probe.env,
                                     capture_output=True, text=True, check=True, timeout=30)
            emit("environment", omp=version.stdout.strip(), platform=sys.platform,
                 provider="loopback fixture; no external model or credentials")
            failures = []
            for stage, extension in probe.guard_faults:
                failures.append(await probe.guard_failure(f"rpc-guard-{stage}-fault", extension))
                failures.append(probe.tui(f"tui-guard-{stage}-fault", [extension]))
            guarded = all(row["requests"] == 0 and row["exit_code"] == -9 and row["guard_failure_reported"]
                          for row in failures)
            emit("guard-failure-boundary", fail_closed=guarded,
                 managed_tui="disabled; required-extension removal remains unverified")
            if arguments.guard_faults_only:
                return 0 if guarded else 1
            message = "M0_INPUT " + "漢字 filler " * 12000
            failed_hook = await probe.rpc("rpc-throwing-context", [probe.failure], message)
            guard = await probe.rpc("rpc-guard", [probe.guard], message)
            missing = await probe.rpc("rpc-missing-extension", [probe.root / "absent.mjs"], "M0_INPUT")
            tui_failed_hook = probe.tui("tui-throwing-context", [probe.failure])
            tui_missing = probe.tui("tui-missing-extension", [probe.root / "absent.mjs"])
            native = probe.existing_native_checks()
            blocked = any(row["requests"] for row in (failed_hook, missing, tui_failed_hook, tui_missing))
            controls = guard["guard_ready"] and guard["omission_visible"] and guard["original_retained"] and native and guarded
            emit("decision", managed_tui="disabled", d4="BLOCKED" if blocked else "NOT_CERTIFIED",
                 controls_passed=controls, remaining="D1/D2/D3/D6/D7 require further probes; do not enable M1")
            return 1 if not controls else (2 if blocked else 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (OSError, RuntimeError, TimeoutError, ValueError, subprocess.SubprocessError) as error:
        emit("probe-error", error_type=type(error).__name__)
        raise SystemExit(1)
