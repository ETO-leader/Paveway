"""Internal Linux guardian: parent death terminates only this owned process group."""

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def cleanup_group():
    """Reap surviving tool descendants before releasing inherited runtime locks."""
    group = os.getpgrp()
    deadline = time.monotonic() + 1
    while True:
        found = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal() or int(entry.name) == os.getpid():
                continue
            descriptor = None
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] == "Z" or int(fields[2]) != group:
                    continue
                found = True
                # pidfd pins the process identity, preventing PID-reuse kills.
                descriptor = os.pidfd_open(int(entry.name))
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == group:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except (OSError, IndexError, ValueError):
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if not found:
            return
        if time.monotonic() >= deadline:
            os.killpg(group, signal.SIGKILL)
        time.sleep(0.01)


def main():
    expected_parent = int(sys.argv[1])

    def terminate_group(*_):
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, terminate_group)
    signal.signal(signal.SIGINT, terminate_group)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        return 125
    if os.getppid() != expected_parent:
        return 125
    # The guardian retains inherited flock descriptors while OMP runs.
    # OMP itself receives only the intended standard streams.
    try:
        process = subprocess.Popen(sys.argv[2:])
        result = process.wait()
        cleanup_group()
        return result
    except OSError:
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
