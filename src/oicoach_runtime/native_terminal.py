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
from .errors import RuntimeContractError
from .processes import enable_subreaper


def run_terminal(argv, environment, *, ready, timeout=30, started=lambda process: None):
    """Input is forwarded only after this owned child's validated attestation."""
    if not os.isatty(0) or not os.isatty(1):
        raise RuntimeContractError("OMP native UI requires an interactive terminal")
    enable_subreaper()
    master, slave = pty.openpty()
    channel, inherited = socket.socketpair()
    descriptor = inherited.fileno()
    environment = dict(environment, PAVEWAY_NATIVE_FD=str(descriptor), OICOACH_LOCK_FDS=str(descriptor))
    process = None
    original = termios.tcgetattr(0)
    previous_signals = {}
    selector = selectors.DefaultSelector()
    admitted = False
    frames = bytearray()
    pending = bytearray()

    def resize(*_):
        fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))

    try:
        resize()
        process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime.child", str(os.getpid()), *argv],
            stdin=slave, stdout=slave, stderr=slave, env=environment, start_new_session=True,
            pass_fds=(descriptor,))
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
                raise RuntimeContractError("Native OMP admission timed out; no user prompt was sent")
            for key, events in selector.select(0.05):
                if key.data == "control":
                    chunk = channel.recv(4097)
                    if not chunk:
                        # Normal native /exit closes inherited fds just before
                        # the guardian reports exit. An idle living process is
                        # never treated as a successful release.
                        try:
                            code = process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            raise RuntimeContractError("Native integration closed unexpectedly; inspect history before retrying") from None
                        return code
                    frames.extend(chunk)
                    if len(frames) > 4096:
                        raise RuntimeContractError("Native attestation exceeds its size limit")
                    if b"\n" in frames:
                        line, _, remainder = frames.partition(b"\n")
                        if remainder:
                            raise RuntimeContractError("Unexpected native attestation data")
                        ready(json.loads(line), process)
                        channel.sendall(b"A")
                        frames.clear()
                        if not admitted:
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
                            process.wait(timeout=5)
                            return process.returncode
                        if data:
                            view = memoryview(data)
                            while view:
                                view = view[os.write(1, view):]
                    if events & selectors.EVENT_WRITE and pending:
                        try:
                            written = os.write(master, pending[:4096])
                            del pending[:written]
                        except BlockingIOError:
                            pass
            selector.modify(master, selectors.EVENT_READ | (selectors.EVENT_WRITE if pending else 0), "terminal")
            if process.poll() is not None:
                return process.returncode
    finally:
        selector.close()
        termios.tcsetattr(0, termios.TCSADRAIN, original)
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            cleanup_group(process.pid)
        channel.close()
        inherited.close()
        os.close(master)
        if slave is not None:
            os.close(slave)
