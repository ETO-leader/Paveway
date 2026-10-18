"""Internal Linux guardian: parent death terminates only this owned process group."""

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def cleanup_group(group_id=None, exclude_pid=None):
    """Reap surviving tool descendants before releasing inherited runtime locks."""
    group = os.getpgrp() if group_id is None else group_id
    children_path = Path(f"/proc/self/task/{os.getpid()}/children")
    deadline = time.monotonic() + 1
    while True:
        found = False
        for child in children_path.read_text().split():
            pid = int(child)
            if pid == exclude_pid:
                continue
            entry = Path(f"/proc/{pid}")
            descriptor = None
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] == "Z":
                    os.waitpid(pid, os.WNOHANG)
                    continue
                if int(fields[2]) != group:
                    continue
                found = True
                # pidfd pins the process identity, preventing PID-reuse kills.
                descriptor = os.pidfd_open(pid)
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
            if group == os.getpgrp():
                os.killpg(group, signal.SIGKILL)
            raise OSError("Owned process group did not stop")
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
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        return 125
    if os.getppid() != expected_parent:
        return 125
    # The guardian retains inherited flock descriptors while OMP runs.
    # OMP itself receives only the intended standard streams.
    try:
        guardian_pid = os.getpid()
        def protect_child():
            # This dedicated guardian is single-threaded; do not run preexec_fn
            # in the async daemon. Kill OMP if its guardian itself is killed.
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != guardian_pid:
                os._exit(125)
        lock_fds = tuple(int(fd) for fd in os.environ.get("OICOACH_LOCK_FDS", "").split(",") if fd)
        process = subprocess.Popen(sys.argv[2:], preexec_fn=protect_child, pass_fds=lock_fds)
        result = process.wait()
        cleanup_group()
        return result
    except OSError:
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
