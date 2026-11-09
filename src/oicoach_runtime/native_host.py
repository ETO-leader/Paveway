"""Select the reproducible UI host without replacing the configured worker OMP."""

import json
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from .errors import RuntimeContractError


def prepare_native_host():
    """Provision before acquiring a frontend lease; never install inside admission."""
    resources = Path(str(files("oicoach_runtime").joinpath("bundled/omp")))
    try:
        prepared = subprocess.run([sys.executable, str(resources / "install.py")],
                                  stdout=subprocess.PIPE, text=True, check=True)
        launcher = Path(json.loads(prepared.stdout)["launcher"])
        if not launcher.is_file() or not os.access(launcher, os.X_OK):
            raise ValueError("Prepared host launcher is not executable")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise RuntimeContractError(
            "Cannot prepare the isolated Paveway OMP host. Run packaging/omp/install.py "
            "to inspect installation errors; no frontend ownership was acquired.") from error
    return str(launcher)
