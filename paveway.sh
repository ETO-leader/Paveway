#!/usr/bin/env bash
# Start from any working directory; default private data uses ignored .runtime/.
set -euo pipefail
umask 077

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
config_path="$project_dir/.runtime/config.toml"
ui_args=()

usage() {
    cat <<'EOF'
用法：./paveway.sh [--config PATH] [--workflow ID] [--task ID]

检查/安装本地 .venv，初始化缺少的配置，启动 Runtime 并进入终端 UI。
默认使用项目 .runtime/config.toml；已有设置、会话和活动请求继续沿用。
退出 UI 不会停止后台 Runtime。OMP 需要事先安装并完成模型认证。
EOF
}

while (($#)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --config|--workflow|--task)
            if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
                printf '缺少 %s 的值。\n' "$1" >&2
                exit 2
            fi
            if [[ "$1" == --config ]]; then
                config_path="$2"
            else
                ui_args+=("$1" "$2")
            fi
            shift 2 ;;
        *) printf '未知参数：%s；使用 --help 查看用法。\n' "$1" >&2; exit 2 ;;
    esac
done

if [[ ! -t 0 || ! -t 1 ]]; then
    printf 'Paveway UI 需要交互终端，请在终端中运行此脚本。\n' >&2
    exit 1
fi

# Resolve user-relative paths before changing to the project directory.
if [[ "$config_path" != /* ]]; then config_path="$PWD/$config_path"; fi
cd -- "$project_dir"
python="$project_dir/.venv/bin/python"
if [[ ! -x "$python" ]]; then
    command -v python3 >/dev/null || { printf '需要 Python 3.11+。\n' >&2; exit 1; }
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11+。")'
    printf '正在建立项目 .venv…\n'
    python3 -m venv "$project_dir/.venv"
fi
if ! "$python" -c 'import oicoach_runtime, prompt_toolkit, tiktoken; from pathlib import Path; assert Path(oicoach_runtime.__file__).resolve().parent == Path("src/oicoach_runtime").resolve()' 2>/dev/null; then
    printf '正在安装 Paveway 依赖…\n'
    "$python" -m pip install -e .
fi

"$python" - "$config_path" <<'PY'
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from oicoach_runtime import service
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import ensure_private_directory, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.workflow_setup import create

path = Path(sys.argv[1]).absolute()
try:
    if not path.exists():
        initialize(path, str(path.parent / "state"), str(path.parent / "workflows"))
    config = load_config(path)
    if not shutil.which(config.agent.executable):
        raise RuntimeContractError("找不到配置的 OMP；请安装 OMP 或修改配置中的 agent.executable。")
    if not any(config.workflow_root.glob("*/workflow.toml")):
        workspace = path.parent / "workspace"
        ensure_private_directory(workspace)
        create(config, "gpt", workspace)

    def online():
        try:
            return request(config.server.socket_path, "status").get("daemon") == "running"
        except RuntimeContractError as error:
            if error.code != "daemon_unavailable":
                raise
            return False

    if not online():
        if service.status(path).get("managed"):
            print("正在启动已有的 systemd 用户服务…", flush=True)
            service.control(path, "start")
        else:
            ensure_private_directory(config.state_dir)
            log_path = config.state_dir / "startup.log"
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab") as log:
                info = os.fstat(log.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise RuntimeContractError("后台启动日志必须是当前用户拥有的私密普通文件。")
                subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(path)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
                    env={**os.environ, **service.read_environment(path)})
            print("正在启动后台 Runtime…", flush=True)
        deadline = time.monotonic() + 20
        while not online():
            if time.monotonic() >= deadline:
                raise RuntimeContractError("Runtime 启动未就绪；请使用 doctor 或 service logs 检查，不会自动重发请求。")
            time.sleep(0.1)
except RuntimeContractError as error:
    print(f"启动失败：{error}", file=sys.stderr)
    sys.exit(1)
except OSError:
    print("启动失败：请检查 Python、文件权限及本机服务环境。", file=sys.stderr)
    sys.exit(1)
PY

exec "$python" -m oicoach_runtime ui --config "$config_path" "${ui_args[@]}"
