"""M0 actual-child admission experiment, not a managed application launcher.

Uses real native OMP over a transparently driven PTY and the M0 loopback
provider. No real credentials, model billing, production state, or RPC UI.
Exit 2 reports a demonstrated provider request bypassing the admitted Guard;
exit 1 is an inconclusive/failed probe; exit 0 certifies only the cases run.
"""

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import secrets
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import ThreadingHTTPServer

from verify_omp_m0 import Probe, Provider, WideScreen, emit, pyte
from oicoach_runtime.child import cleanup_group
from oicoach_runtime.processes import enable_subreaper


CASES = ("normal", "missing-entry", "missing-guard", "factory-error", "wrong-digest",
         "foreign-handshake", "wrong-version", "no-handshake", "deny-context", "stall-context",
         "ipc-close", "guard-error", "rename", "compact")

# These are counterexamples, not certified safe native features. Keep a normal
# guarded request as a control before invoking each independent native path.
SIDE_COMMANDS = {"rename": b"/rename\r", "compact": b"/compact soft\r"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AdmissionProbe:
    def __init__(self, base):
        self.base = base
        self.restrictions = base.root / "admission-restrictions.yml"
        self.restrictions.write_text(json.dumps({
            "autoResume": False, "goal": {"enabled": False}, "memory": {"backend": "off"},
            "providers": {"openaiWebsockets": "off", "openaiLiveSteering": False, "cacheWarming": "off"},
            "composer": {"predictions": False}, "recap": {"enabled": False},
            "compaction": {"enabled": False, "idleEnabled": False, "methodOrder": []},
            "collab": {"autoStart": "off"}, "advisor": {"enabled": False},
            "autolearn": {"enabled": False, "autoContinue": False},
            "title": {"icons": "boring"}, "thinkingLevel": "off",
            "modelRoles": {"tiny": "paveway-m0/probe", "smol": "paveway-m0/probe"},
        }))

    def run(self, case):
        root = self.base.root / ("gate-" + case)
        root.mkdir(mode=0o700)
        guard, entry = root / "context-window.mjs", root / "admission.mjs"
        guard.write_bytes(self.base.guard.read_bytes())
        entry.write_bytes(Path(__file__).with_name("omp_admission.mjs").read_bytes())
        if case == "guard-error":
            guard.write_text(guard.read_text().replace("windowMessages(event.messages, count)",
                'windowMessages((() => {throw new Error("M0_GUARD_FAULT");})(), count)'))
        if case == "factory-error":
            entry.write_text('export default function() {throw new Error("M0_FACTORY_FAULT");}')
        if case in ("foreign-handshake", "wrong-version"):
            entry.write_text('import {writeSync, readSync} from "node:fs";\n'
                'export default function() {const fd=Number(process.env.PAVEWAY_ADMISSION_FD);'
                'writeSync(fd, JSON.stringify({apiVersion:1,phase:"guard",nonce:"foreign",pid:process.pid,'
                'sequence:0,guardDigest:process.env.PAVEWAY_ADMISSION_GUARD_SHA256,'
                'entryDigest:process.env.PAVEWAY_ADMISSION_ENTRY_SHA256,tokenLimit:10000})+"\\n");'
                'readSync(fd,Buffer.alloc(1),0,1,null);}')
            if case == "wrong-version":
                entry.write_text(entry.read_text().replace('apiVersion:1', 'apiVersion:2').replace(
                    'nonce:"foreign"', 'nonce:process.env.PAVEWAY_ADMISSION_NONCE'))
        if case == "no-handshake":
            entry.write_text('export default function() {}')
        guard_digest, entry_digest = sha(guard), sha(entry)
        expected_guard_digest = "0" * 64 if case == "wrong-digest" else guard_digest
        if case == "missing-guard":
            guard.unlink()
        if case == "missing-entry":
            entry.unlink()
        restrictions = self.restrictions
        if case == "compact":
            # A short single turn legitimately has nothing to compact. Seed a
            # real oversized native turn, keeping automatic compaction disabled.
            # The small retained tail makes the manual soft path exercisable.
            settings = json.loads(self.restrictions.read_text())
            settings["compaction"]["keepRecentTokens"] = 128
            restrictions = root / "compact-restrictions.yml"
            restrictions.write_text(json.dumps(settings))
        nonce = secrets.token_hex(32)
        control, inherited = socket.socketpair()
        control.setblocking(False)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.set_blocking(master, False)
        env = dict(self.base.env, PAVEWAY_ADMISSION_FD=str(inherited.fileno()),
            PAVEWAY_ADMISSION_NONCE=nonce, PAVEWAY_ADMISSION_GUARD_SHA256=guard_digest,
            PAVEWAY_ADMISSION_ENTRY_SHA256=entry_digest, OICOACH_LOCK_FDS=str(inherited.fileno()))
        # The existing guardian retains/passes this control descriptor exactly
        # as it retains/passes runtime lock descriptors. No queue slot is used.
        argv = [*self.base.argv("gate-" + case, []), "--trusted-extension", str(entry),
                "--config", str(restrictions), "--no-lsp", "--no-prewalk", "--thinking", "off"]
        before = len(self.base.server.requests)
        guardian = None
        selector = selectors.DefaultSelector()
        screen = WideScreen(120, 40)
        stream = pyte.ByteStream(screen)
        raw = bytearray()
        channel_buffer = bytearray()
        terminal_out = bytearray()
        sequence = 0
        phase = "starting"
        root_pid = None
        session_id = None
        contexts = 0
        grants = 0
        input_bytes = 0
        released = False
        rejected = None
        quit_sent = False
        side_command_sent = False
        timed_out = False
        baseline_requests = None
        baseline_grants = None
        message = "Investigate the Paveway native admission boundary and preserve verified evidence."
        if case == "compact":
            message += (" 漢字 filler " * 6000 + " M0_COMPACTION_MIDDLE " +
                        " 漢字 filler " * 6000 + " M0_COMPACTION_TAIL")
        try:
            guardian = subprocess.Popen([sys.executable, "-m", "oicoach_runtime.child", str(os.getpid()), *argv],
                stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
                pass_fds=(inherited.fileno(),))
            os.close(slave)
            slave = None
            inherited.close()
            selector.register(master, selectors.EVENT_READ, "terminal")
            selector.register(control, selectors.EVENT_READ, "control")
            deadline = time.monotonic() + (10 if case == "no-handshake" else 40)
            while time.monotonic() < deadline:
                for key, events in selector.select(0.05):
                    if key.data == "control":
                        try:
                            chunk = control.recv(4097)
                        except ConnectionResetError:
                            chunk = b""
                        if not chunk:
                            selector.unregister(control)
                            control.close()
                            continue
                        channel_buffer.extend(chunk)
                        if len(channel_buffer) > 4096:
                            rejected = "oversized-control-frame"
                            break
                        while b"\n" in channel_buffer:
                            line, _, rest = channel_buffer.partition(b"\n")
                            channel_buffer = bytearray(rest)
                            try:
                                frame = json.loads(line)
                                valid = (isinstance(frame, dict) and type(frame.get("apiVersion")) is int and
                                    frame["apiVersion"] == 1 and frame.get("nonce") == nonce and
                                    type(frame.get("sequence")) is int and frame["sequence"] == sequence and
                                    frame.get("guardDigest") == expected_guard_digest and frame.get("entryDigest") == entry_digest and
                                    frame.get("tokenLimit") == 10000 and type(frame.get("pid")) is int)
                                if not valid:
                                    raise ValueError("identity")
                                if root_pid is None:
                                    root_pid = frame["pid"]
                                    fields = Path(f"/proc/{root_pid}/stat").read_text().rsplit(")", 1)[1].split()
                                    if int(fields[1]) != guardian.pid:
                                        raise ValueError("not-guardian-child")
                                if frame["pid"] != root_pid:
                                    raise ValueError("changed-process")
                                incoming = frame.get("phase")
                                if incoming == "guard" and phase == "starting":
                                    phase = "guard-ready"
                                elif incoming == "session" and phase == "guard-ready":
                                    session_id = frame.get("sessionId")
                                    if not isinstance(session_id, str) or not session_id:
                                        raise ValueError("session")
                                    phase = "admitted"
                                elif incoming == "context" and phase == "admitted" and frame.get("sessionId") == session_id:
                                    contexts += 1
                                else:
                                    raise ValueError("phase")
                                sequence += 1
                            except (ValueError, OSError, KeyError, TypeError):
                                rejected = "invalid-child-attestation"
                                break
                            if case == "stall-context" and incoming == "context":
                                deadline = time.monotonic() + 2
                                continue
                            control.sendall(b"N" if case == "deny-context" and incoming == "context" else b"A")
                            if incoming == "context" and case != "deny-context":
                                grants += 1
                            if phase == "admitted" and not released:
                                if case == "ipc-close":
                                    selector.unregister(control)
                                    control.close()
                                # User bytes are queued from the beginning but
                                # forwarded only after this child's final ack.
                                # Bracketed paste avoids treating the large fixture
                                # as thousands of independent composer keypresses.
                                # Keep the established key-input path for short
                                # controls instead of testing paste batching there.
                                if case == "compact":
                                    terminal_out.extend(b"\x1b[200~" + message.encode() + b"\x1b[201~\r")
                                else:
                                    terminal_out.extend(message.encode() + b"\r")
                                released = True
                    else:
                        if events & selectors.EVENT_READ:
                            try:
                                chunk = os.read(master, 65536)
                            except BlockingIOError:
                                chunk = None
                            except OSError as error:
                                if error.errno != errno.EIO:
                                    raise
                                chunk = b""
                            if chunk == b"":
                                selector.unregister(master)
                            elif chunk:
                                raw.extend(chunk)
                                stream.feed(chunk)
                                if b"\x1b[6n" in chunk:
                                    os.write(master, b"\x1b[1;1R")
                        if events & selectors.EVENT_WRITE and terminal_out:
                            try:
                                written = os.write(master, terminal_out[:4096])
                                input_bytes += written
                                del terminal_out[:written]
                            except BlockingIOError:
                                pass
                if rejected:
                    break
                try:
                    selector.modify(master, selectors.EVENT_READ | (selectors.EVENT_WRITE if terminal_out else 0), "terminal")
                except KeyError:
                    pass
                requests = len(self.base.server.requests) - before
                visible = "\n".join(screen.display)
                if (case == "normal" or case in SIDE_COMMANDS) and requests >= 1 and "M0_REPLY" in visible:
                    if case in SIDE_COMMANDS and not side_command_sent:
                        baseline_requests, baseline_grants = requests, grants
                        terminal_out.extend(SIDE_COMMANDS[case])
                        side_command_sent = True
                    elif not quit_sent and (case == "normal" or requests > baseline_requests):
                        terminal_out.extend(b"/exit\r")
                        quit_sent = True
                if guardian.poll() is not None:
                    break
            else:
                timed_out = True
        finally:
            selector.close()
            if guardian is not None:
                if guardian.poll() is None:
                    os.killpg(guardian.pid, signal.SIGKILL)
                guardian.wait(timeout=10)
                cleanup_group(guardian.pid)
            control.close()
            inherited.close()
            os.close(master)
            if slave is not None:
                os.close(slave)
        requests = len(self.base.server.requests) - before
        if root_pid:
            logs = list(self.base.root.rglob(f"omp.*.{root_pid}.log"))
            diagnostics = b"\n".join(path.read_bytes() for path in logs)
        else:
            diagnostics = b""
        result = dict(case=case, admitted=released, input_bytes=input_bytes, contexts=contexts, grants=grants,
            requests=requests, guardian_exit=guardian.returncode, timeout=timed_out, rejected=rejected,
            rpc_ready_on_tui=b"paveway_context_ready" in raw,
            guard_failed=b"paveway_context_failed" in raw + diagnostics,
            admission_failed=b"paveway_admission_failed" in raw + diagnostics,
            native_reply="M0_REPLY" in "\n".join(screen.display),
            screen="\n".join(line.rstrip() for line in screen.display).strip())
        if case in SIDE_COMMANDS:
            result.update(baseline_requests=baseline_requests, baseline_grants=baseline_grants,
                unguarded_side_request=(baseline_requests is not None and requests > baseline_requests and grants == baseline_grants))
        if case == "compact":
            wires = [json.dumps(row.get("messages", []), ensure_ascii=False)
                     for row in self.base.server.requests[before:]]
            result.update(guarded_input_omitted=bool(wires) and "M0_COMPACTION_MIDDLE" not in wires[0]
                          and "older conversation or oversized content was omitted" in wires[0],
                          side_request_contains_omitted_input=any("M0_COMPACTION_MIDDLE" in wire for wire in wires[1:]),
                          request_message_chars=[len(wire) for wire in wires],
                          original_input_retained=any(message in path.read_text(encoding="utf-8")
                              for path in root.rglob("*.jsonl")))
        if case == "normal":
            result["passed"] = (released and grants >= 1 and requests == grants and guardian.returncode == 0
                                and not timed_out and result["native_reply"] and not result["rpc_ready_on_tui"])
        elif case in SIDE_COMMANDS:
            result["passed"] = (released and result["unguarded_side_request"] and
                baseline_requests == baseline_grants == 1 and guardian.returncode == 0 and not timed_out)
            if case == "compact":
                result["passed"] = (result["passed"] and result["guarded_input_omitted"] and
                    result["side_request_contains_omitted_input"] and result["original_input_retained"])
        elif case == "stall-context":
            result["passed"] = (released and input_bytes > 0 and contexts == 1 and grants == requests == 0
                                and timed_out and guardian.returncode == -9)
        elif case in ("deny-context", "ipc-close", "guard-error"):
            reached = released and input_bytes > 0
            if case == "deny-context":
                reached = reached and contexts == 1 and grants == 0 and result["admission_failed"]
            elif case == "guard-error":
                reached = reached and contexts == grants == 1 and result["guard_failed"]
            else:
                reached = reached and grants == 0 and result["admission_failed"]
            result["passed"] = reached and requests == 0 and guardian.returncode != 0 and not timed_out
        else:
            result["passed"] = (requests == 0 and guardian.returncode != 0 and
                ((case == "no-handshake" and timed_out and input_bytes == 0) or not timed_out) and
                not released and input_bytes == 0 and contexts == grants == 0)
        if result["passed"] and case != "normal" and case not in SIDE_COMMANDS:
            del result["screen"]
        emit("native-admission", **result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, action="append", dest="cases")
    parser.add_argument("--admission-only", action="store_true", help="Exclude the auxiliary-model bypass probe")
    arguments = parser.parse_args()
    executable = shutil.which("omp")
    if not executable:
        raise RuntimeError("omp is not installed")
    enable_subreaper()
    with tempfile.TemporaryDirectory(prefix="pw-admission-") as directory:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            probe = AdmissionProbe(Probe(Path(directory), executable, server))
            version = subprocess.run([executable, "--version"], env=probe.base.env,
                capture_output=True, text=True, check=True, timeout=30)
            emit("environment", omp=version.stdout.strip(), platform=sys.platform,
                 provider="loopback fixture; no external model or credentials")
            cases = arguments.cases or [case for case in CASES if not arguments.admission_only or case not in SIDE_COMMANDS]
            results = [probe.run(case) for case in cases]
            passed = all(result["passed"] for result in results)
            bypass = any(result.get("unguarded_side_request") for result in results)
            emit("admission-decision", cases_passed=passed, managed_tui="disabled",
                 provider_bypass_observed=bypass, full_d4="BLOCKED" if bypass else "NOT_CERTIFIED")
            return 1 if not passed else (2 if bypass else 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        emit("probe-error", error_type=type(error).__name__)
        raise SystemExit(1)
