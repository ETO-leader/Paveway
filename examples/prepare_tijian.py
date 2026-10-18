"""Prepare a new isolated OMP workspace with the Tijian MCP bridge (not a coaching workflow)."""

import argparse
import json
import shutil
from dataclasses import replace
from pathlib import Path

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import TijianConfig, config_text, ensure_private_directory, load_config
from oicoach_runtime.integrations.tijian import mcp_configuration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="New directory; existing paths are rejected")
    parser.add_argument("--tijian-executable", required=True, help="Installed Tijian path or executable name")
    parser.add_argument("--omp-executable", default="omp")
    args = parser.parse_args()
    root = args.directory.absolute()
    if root.exists() or root.is_symlink():
        parser.error("Choose a new directory; existing workspaces/configurations are never overwritten")
    executable = shutil.which(args.tijian_executable)
    omp = shutil.which(args.omp_executable)
    if not executable or not omp:
        parser.error("Both Tijian and OMP executables must be installed")
    root.mkdir(mode=0o700, parents=True)
    workspace = root / "workspace"
    ensure_private_directory(workspace)
    path = root / "config.toml"
    initialize(path, str(root / "state"), str(root / "workflows"))
    config = load_config(path)
    config = replace(config, agent=replace(config.agent, executable=str(Path(omp).absolute())),
                     tijian=TijianConfig(enabled=True, executable=str(Path(executable).absolute()), read_only=True))
    path.write_text(config_text(config))
    workflow = config.workflow_root / "tijian"
    workflow.mkdir(mode=0o700)
    (workflow / "workflow.toml").write_text(
        'schema_version = 1\nid = "tijian"\nversion = "1"\nsession_policy = "per_task"\n'
        f'workspace = {json.dumps(str(workspace))}\nstart_prompt = "event.md"\nwake_prompt = "event.md"\nrecovery_prompt = "event.md"\n')
    (workflow / "event.md").write_text(
        "這是 Tijian 接口驗證，不是教練策略。請讀取事件 payload.problem 指定的題目；未指定則使用 P1001。\n"
        "只呼叫 Tijian 的 problem_get MCP 工具，簡要回報題目名稱及當前狀態。\n"
        "若 MCP 無法使用，可依 available_tools 用 Runtime tijian call problem_get。\n"
        "不可修改狀態、補抓題目或用其他網路來源代替；讀取失敗時清楚說明。\n"
        "事件資料：${event_json}\n")
    mcp_path = workspace / ".mcp.json"
    mcp_path.write_text(json.dumps(mcp_configuration(path), ensure_ascii=False, indent=2) + "\n")
    mcp_path.chmod(0o600)
    print(json.dumps({"integration_example_only": True, "config": str(path), "workspace": str(workspace),
                      "workflow": "tijian", "read_only": True, "daemon_started": False}))


if __name__ == "__main__":
    main()
