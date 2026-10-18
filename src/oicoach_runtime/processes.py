"""Linux process ownership and locks. Lock files are never unlinked."""

import fcntl
import os
import stat
import ctypes
from pathlib import Path

from .errors import ConflictError


def enable_subreaper():
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise ConflictError("Cannot enable owned-child supervision on this Linux host")


class InstanceLock:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ConflictError("Lock file must be privately owned and regular")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise ConflictError("Another daemon or managed agent owns the runtime/socket lock") from None

    def close(self):
        # close rather than LOCK_UN: a guardian may still hold the shared open file description.
        os.close(self.fd)


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
    except (OSError, IndexError):
        return None
