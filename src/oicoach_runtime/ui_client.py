"""Public CLI subprocess transport for terminal frontends; never opens the runtime DB."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .errors import RuntimeContractError


class CliFailure(RuntimeContractError):
    def __init__(self, message, code="cli_failed"):
        super().__init__(message)
        self.code = code


class CliClient:
    """Owned CLI process groups only; closing never stops the separate daemon."""
    def __init__(self, config_path):
        self.config_path = Path(config_path).absolute()
        self.lock = threading.Lock()
        self.closed = False
        self.process = None

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is not None:
                self._stop(self.process)

    def call(self, *arguments, text_files=None):
        # Draft bodies never appear in argv or reusable state files.
        with tempfile.TemporaryDirectory(prefix="paveway-ui-") as directory:
            values = {"--workflow", "--task", "--dedupe-key", "--mode", "--query", "--key", "--title", "--kind",
                      "--expected-version", "--source-event", "--expires-at", "--workspace", "--at", "--reason", "--offset",
                      "--expected-revision", "--name", "--description", "--repository", "--remote", "--branch",
                      "--interval", "--device", "--run", "--session", "--head"}
            normalized, iterator = [], iter(map(str, arguments))
            for argument in iterator:
                if argument in values:
                    try:
                        normalized.append(argument + "=" + next(iterator))
                    except StopIteration:
                        raise CliFailure("缺少 CLI 参数值") from None
                else:
                    normalized.append(argument)
            argv = [sys.executable, "-m", "oicoach_runtime", *normalized]
            for index, (option, content) in enumerate((text_files or {}).items()):
                path = Path(directory) / str(index)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
                argv.extend([option, str(path)])
            argv.extend(["--config", str(self.config_path), "--json"])
            with self.lock:
                if self.closed:
                    raise CliFailure("界面已经关闭")
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
                self.process = process
            try:
                try:
                    stdout, stderr = process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    self._stop(process)
                    process.communicate()
                    raise CliFailure("CLI 响应超时；写入结果可能已保存，请先刷新核对，勿重复提交", "cli_timeout") from None
            finally:
                with self.lock:
                    if self.process is process:
                        self.process = None
            data = stderr if process.returncode and stderr else stdout
            try:
                if len(data) > 1048576:
                    raise ValueError()
                report = json.loads(data)
                if not isinstance(report, dict) or report.get("api_version") != 1:
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError):
                raise CliFailure("CLI 返回了无法解析的结果；请使用独立命令检查配置/运行状态") from None
            if "error" in report:
                error = report["error"]
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    raise CliFailure(error["message"], str(error.get("code", "cli_failed")))
                raise CliFailure("CLI 返回了无效错误")
            if process.returncode and report.get("ok") is not False:
                raise CliFailure("检查未通过：" + json.dumps(report, ensure_ascii=False))
            return report.get("result", report)
