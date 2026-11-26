"""Transparent PTY supervision for the original OMP TUI; no chat renderer."""

import errno
import fcntl
import json
import os
import pty
import selectors
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty

from .child import cleanup_group
from .diagnostics import NativeStartupFailure
from .errors import RuntimeContractError
from .processes import enable_subreaper


def run_terminal(argv, environment, *, ready, timeout=30, started=lambda process: None, lock_fds=()):
    """Input is forwarded only after attestation and Extension initialization."""
    if not os.isatty(0) or not os.isatty(1):
        raise RuntimeContractError("OMP native UI requires an interactive terminal")
    enable_subreaper()
    master, slave = pty.openpty()
    channel, inherited = socket.socketpair()
    descriptor = inherited.fileno()
    inherited_fds = (descriptor, *lock_fds)
    environment = dict(environment, PAVEWAY_NATIVE_FD=str(descriptor), OICOACH_LOCK_FDS=",".join(map(str, lock_fds)))
    process = None
    original = termios.tcgetattr(0)
    previous_signals = {}
    selector = selectors.DefaultSelector()
    admitted = False
    frames = bytearray()
    pending = bytearray()
    terminal_open = True

    def resize(*_):
        fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGWINCH)

    def emit_terminal(data):
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]

    def check_exit(code):
        if not admitted:
            # The control FD can close before select delivers the last PTY bytes.
            try:
                while chunk := os.read(master, 65536):
                    emit_terminal(chunk)
            except BlockingIOError:
                pass
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
            raise NativeStartupFailure("host_exit",
                f"OMP exited before Paveway Extension admission (exit {code}); "
                "check the OMP startup output and required Extension capabilities")
        return code

    try:
        resize()
        process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime.child", str(os.getpid()), *argv],
            stdin=slave, stdout=slave, stderr=slave, env=environment, start_new_session=True,
            pass_fds=inherited_fds)
        os.close(slave)
        slave = None
        inherited.close()
        started(process)
        tty.setraw(0)
        os.set_blocking(master, False)
        os.set_blocking(channel.fileno(), False)
        for signum in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP):
            previous_signals[signum] = signal.getsignal(signum)
            signal.signal(signum, resize if signum == signal.SIGWINCH else
                          lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        selector.register(master, selectors.EVENT_READ, "terminal")
        selector.register(channel, selectors.EVENT_READ, "control")
        deadline = time.monotonic() + timeout
        while True:
            if not admitted and time.monotonic() >= deadline:
                raise NativeStartupFailure("timeout", "Native OMP admission timed out; no user prompt was sent")
            events_ready = selector.select(0.05)
            # Receive a queued rejection before a concurrent PTY hangup.
            for key, events in sorted(events_ready, key=lambda pair: pair[0].data != "control"):
                if key.data == "control":
                    chunk = channel.recv(4097)
                    if not chunk:
                        # Normal native /exit closes inherited fds just before
                        # the guardian reports exit. An idle living process is
                        # never treated as a successful release.
                        try:
                            code = process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            if not admitted:
                                raise NativeStartupFailure("channel", "Native Extension closed before admission") from None
                            raise RuntimeContractError("Native integration closed unexpectedly; inspect history before retrying") from None
                        return check_exit(code)
                    frames.extend(chunk)
                    if len(frames) > 4096:
                        raise NativeStartupFailure("attestation", "Native attestation exceeds its size limit")
                    if b"\n" in frames:
                        line, _, remainder = frames.partition(b"\n")
                        if remainder:
                            raise NativeStartupFailure("attestation", "Unexpected native attestation data")
                        try:
                            frame = json.loads(line)
                        except (ValueError, UnicodeError):
                            raise NativeStartupFailure("attestation", "Invalid native attestation; no automatic retry") from None
                        if not isinstance(frame, dict):
                            raise NativeStartupFailure("attestation", "Invalid native attestation object")
                        initialized = ready(frame, process)
                        channel.sendall(b"A")
                        frames.clear()
                        if initialized and not admitted:
                            admitted = True
                            selector.register(0, selectors.EVENT_READ, "input")
                elif key.data == "input":
                    data = os.read(0, 65536)
                    if data:
                        pending.extend(data)
                    else:
                        selector.unregister(0)
                        pending.extend(b"\x04")
                else:
                    if events & selectors.EVENT_READ:
                        try:
                            data = os.read(master, 65536)
                        except BlockingIOError:
                            data = None
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            data = b""
                        if data == b"":
                            if admitted:
                                process.wait(timeout=5)
                                return check_exit(process.returncode)
                            selector.unregister(master)
                            terminal_open = False
                            continue
                        if data:
                            emit_terminal(data)
                    if events & selectors.EVENT_WRITE and pending:
                        try:
                            written = os.write(master, pending[:4096])
                            del pending[:written]
                        except BlockingIOError:
                            pass
            if terminal_open:
                selector.modify(master, selectors.EVENT_READ | (selectors.EVENT_WRITE if pending else 0), "terminal")
            if process.poll() is not None and not selector.select(0):
                return check_exit(process.returncode)
    except KeyboardInterrupt:
        return 130
    finally:
        selector.close()
        termios.tcsetattr(0, termios.TCSADRAIN, original)
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            # This native CLI is a dedicated subreaper (unlike the shared
            # Runtime). After guardian death, its adopted tools are still our
            # children even when they changed process group/session.
            cleanup_group()
        channel.close()
        inherited.close()
        os.close(master)
        if slave is not None:
            os.close(slave)
