"""Prepare a fresh fake-OMP demo; no model, credentials, MCP or services involved."""

import argparse
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config.absolute()
    if path.exists() or path.is_symlink():
        parser.error("Choose a new demo config path; existing configurations are never overwritten")
    repo = Path(__file__).resolve().parents[1]
    fake = repo / "tests/fake_omp.py"
    if not fake.is_file() or not os.access(fake, os.X_OK):
        parser.error("Run from a source checkout with executable tests/fake_omp.py")
    initialize(path, str(path.parent / "state"), str(path.parent / "workflows"))
    config = load_config(path)
    directory = config.workflow_root / "example"
    shutil.copytree(repo / "workflows/example", directory)
    manifest = directory / "workflow.toml"
    manifest.write_text(manifest.read_text().replace('"REPLACE_WITH_ABSOLUTE_WORKSPACE"', json.dumps(str(path.parent), ensure_ascii=False)))
    config = replace(config, agent=replace(config.agent, executable=str(fake)))
    path.write_text(config_text(config))
    print(json.dumps({"demo_only": True, "config": str(path), "agent": "fake OMP", "workflow": "example"}))


if __name__ == "__main__":
    main()
