"""Read the selected OMP installation's model catalog without a model turn."""

import json
import os
import subprocess

from . import customization
from .compatibility import probe_daemon
from .config import valid_inference_token
from .errors import RuntimeContractError
from .omp_installation import OMP_IDENTITY_ENV, OmpInstallation, OmpInstallationUnavailable, load_active_omp
from .workflows import WorkflowProvider


def catalog(config, workflow_id, *, config_path=None):
    workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
    report = customization.show(config, workflow_id)
    installation = _catalog_installation(config)
    argv = []
    if config.agent.profile:
        argv.extend(["--profile", config.agent.profile])
    argv.extend(["models", "--json", "--no-extensions"])
    try:
        from .proxy import child_environment
        process = subprocess.run(installation.command(argv), cwd=workflow.workspace, capture_output=True, timeout=20,
                                 env=child_environment(config_path))
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeContractError("Cannot read OMP model catalog; check the executable/profile or enter a model selector directly") from None
    if process.returncode or len(process.stdout) > 8388608:
        installation.check()
        raise RuntimeContractError("OMP model catalog failed or exceeded 8 MiB; no settings were changed")
    try:
        raw = json.loads(process.stdout)
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
            raise ValueError
        models = []
        for model in raw["models"]:
            if not isinstance(model, dict) or model.get("kind", "chat") != "chat":
                continue
            selector = model.get("selector")
            if not selector or not valid_inference_token(selector):
                continue
            levels = model.get("thinking", [])
            if levels is None:
                levels = []
            if not isinstance(levels, list):
                raise ValueError
            models.append({"selector": selector, "name": str(model.get("name") or selector)[:256],
                           "thinking": list(dict.fromkeys(level for level in levels
                                                          if level and valid_inference_token(level)))})
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeContractError("OMP returned an invalid model catalog; no settings were changed") from None
    return {**report, "models": sorted(models, key=lambda model: model["selector"])}


def _catalog_installation(config):
    if OMP_IDENTITY_ENV in os.environ:
        return OmpInstallation.for_command(config.agent.executable)
    active = load_active_omp(config.state_dir)
    if active is None:
        return OmpInstallation.for_command(config.agent.executable)
    # If a verified handoff failed after the pointer moved, a still-live
    # Runtime remains authoritative for native and control CLI selection.
    status = probe_daemon(config.server.socket_path, timeout=1) if config.server.socket_path else None
    if status is None:
        return active.installed
    omp = status.get("omp", {})
    if omp.get("installation_status") != "selected" or not omp.get("path") or not omp.get("file_identity"):
        raise OmpInstallationUnavailable()
    return OmpInstallation(omp["path"], tuple(omp["file_identity"]))
