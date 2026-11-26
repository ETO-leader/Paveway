"""Explicit test-only acceptance for the stock RPC fake, never real probes."""

import sys
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.omp_installation import OmpInstallation, omp_version, prepare_omp

FAKE_OMP = Path(__file__).with_name("fake_omp.py").resolve()


def admit_fake(config):
    source = OmpInstallation.select(str(FAKE_OMP), environment={})
    assert Path(config.agent.executable).resolve() == FAKE_OMP
    with patch("oicoach_runtime.omp_probe.probe_omp", return_value="0.0.1-test"):
        with prepare_omp(config.state_dir, source) as activation:
            activation.commit()


def stale_fake_runtime(config_path):
    # Deliberately emulate a previously admitted legacy daemon, without making
    # its emulated old build fingerprint replace the current fixture admission.
    script = '''
import asyncio, sys
from pathlib import Path
from oicoach_runtime import build_id
from oicoach_runtime.config import load_config
from oicoach_runtime.omp_installation import OmpInstallation
from oicoach_runtime.scheduler import Runtime
build_id._CURRENT_BUILD_ID = '0' * 64
path = Path(sys.argv[1])
config = load_config(path)
asyncio.run(Runtime(config, path, omp_installation=OmpInstallation.select(config.agent.executable),
                    probe_version='0.0.1-test').serve())
'''
    return [sys.executable, "-c", script, str(config_path)]


def fixture_version(installation):
    return omp_version(installation.path, installation=installation)
