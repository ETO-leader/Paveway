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
                # A guardian is a dedicated subreaper: every child it adopts
                # belongs to its OMP, even if a tool changed session/group.
                # A shared launcher/daemon must retain the explicit group filter.
                if group_id is not None and int(fields[2]) != group:
                    continue
                if fields[0] == "Z":
                    os.waitpid(pid, os.WNOHANG)
                    continue
                found = True
                # pidfd pins the process identity, preventing PID-reuse kills.
                descriptor = os.pidfd_open(pid)
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[1]) == os.getpid() and (group_id is None or int(fields[2]) == group):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except (OSError, IndexError, ValueError):
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if not found:
            return
        if time.monotonic() >= deadline:
            raise OSError("Owned descendants did not stop")
        time.sleep(0.01)


def main():
    expected_parent = int(sys.argv[1])

    def terminate_group(signum, _frame):
        # Parent death/termination must leave the subreaper alive long enough
        # to collect tools that escaped the group, while it still holds locks.
        cleanup_group()
        raise SystemExit(128 + signum)

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
        # Locks stop here. Only the native attestation channel crosses into OMP;
        # worker/RPC children need no additional descriptors at all.
        native_fd = os.environ.get("PAVEWAY_NATIVE_FD")
        communication_fds = (int(native_fd),) if native_fd else ()
        child_environment = dict(os.environ)
        child_environment.pop("OICOACH_LOCK_FDS", None)
        process = subprocess.Popen(sys.argv[2:], preexec_fn=protect_child,
                                   pass_fds=communication_fds, env=child_environment)
        result = process.wait()
        cleanup_group()
        return result
    except OSError:
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
