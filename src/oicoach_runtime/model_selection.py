"""Read the selected OMP installation's model catalog without a model turn."""

import json
import subprocess

from . import customization
from .config import valid_inference_token
from .errors import RuntimeContractError
from .omp_installation import resolve_omp
from .workflows import WorkflowProvider


def catalog(config, workflow_id, *, config_path=None):
    workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
    report = customization.show(config, workflow_id)
    argv = [resolve_omp(config.agent.executable)]
    if config.agent.profile:
        argv.extend(["--profile", config.agent.profile])
    argv.extend(["models", "--json", "--no-extensions"])
    try:
        from .proxy import child_environment
        process = subprocess.run(argv, cwd=workflow.workspace, capture_output=True, timeout=20,
                                 env=child_environment(config_path))
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeContractError("Cannot read OMP model catalog; check the executable/profile or enter a model selector directly") from None
    if process.returncode or len(process.stdout) > 8388608:
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
