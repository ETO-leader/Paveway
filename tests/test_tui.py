import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.document import Document
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput
try:
    import pyte
except ImportError:
    pyte = None

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.tui import CliClient, CliFailure, TerminalUI, SlashCompleter, visible, run
from oicoach_runtime.workflow_setup import create


def collect(ui):
    deadline = time.monotonic() + 5
    while (ui.busy or ui.restore_pending) and time.monotonic() < deadline:
        ui.collect()
        if ui.restore_pending and not ui.busy:
            ui.restore_pending = False
            ui.restore()
        time.sleep(0.01)
    if ui.busy:
        raise AssertionError("UI worker did not finish")


def make_ui(client, workflow="echo", task="task"):
    return TerminalUI(client, workflow, task, input=DummyInput(), output=DummyOutput())


class ConversationTests(unittest.TestCase):
    def test_reenter_running_conversation_keeps_polling_and_shows_completion_once(self):
        event = {"event_id": "existing", "workflow_id": "echo", "task_id": "task", "run_id": "existing-run",
                 "status": "running", "created_at": "2026-10-20T15:33:00Z", "reason": "request", "payload": {"message": "check QOJ"}}
        class Client:
            def call(self, *args):
                if args[:2] == ("history", "search"):
                    return [{"run_id": "existing-run"}]
                if args[:2] == ("history", "show"):
                    return {"run": {"run_id": "existing-run", "status": event["status"]}, "event": dict(event), "output": {"text": None}}
                if args[:2] == ("events", "show"):
                    return dict(event)
                if args[:2] == ("runs", "output"):
                    return {"text": "finished QOJ check"}
                raise AssertionError(args)
        ui = make_ui(Client())
        ui.restore()
        collect(ui)
        self.assertIsNotNone(ui.pending)
        self.assertNotIn("existing-run", ui.seen)
        self.assertIn("已等待", ui.status_text())
        self.assertNotIn("没有已保存的最终回复", ui.transcript.text)
        ui.editor.text = "NEXT_DRAFT"
        event["status"] = "succeeded"
        ui.poll()
        collect(ui)
        self.assertIsNone(ui.pending)
        self.assertIn("finished QOJ check", ui.transcript.text)
        ui.restore()
        collect(ui)
        self.assertEqual(ui.transcript.text.count("finished QOJ check"), 1)
        self.assertEqual(ui.transcript.text.count("check QOJ"), 1)
        self.assertEqual(ui.editor.text, "NEXT_DRAFT")

    def test_periodic_history_reconciliation_discovers_finished_external_requests(self):
        class Client:
            def __init__(self): self.visible = False
            def call(self, *args):
                if args[:2] == ("history", "search"):
                    return [{"run_id": "external-run"}] if self.visible else []
                if args[:2] == ("history", "show"):
                    return {"run": {"run_id": "external-run", "status": "succeeded"},
                        "event": {"event_id": "external-event", "status": "succeeded", "reason": "external",
                                  "payload": {"message": "scheduled followup"}}, "output": {"text": "external reply"}}
                raise AssertionError(args)
        client = Client()
        ui = make_ui(client)
        ui.restore()
        collect(ui)
        ui.editor.text = "KEEP_DRAFT"
        client.visible = True
        ui.last_restore = 0
        ui.reconcile_history()
        collect(ui)
        self.assertIn("external reply", ui.transcript.text)
        ui.last_restore = 0
        ui.reconcile_history()
        collect(ui)
        self.assertEqual(ui.transcript.text.count("external reply"), 1)
        self.assertEqual(ui.editor.text, "KEEP_DRAFT")

    def test_model_failure_reason_is_visible_on_arrival_and_history_restore(self):
        event = {"event_id": "failed", "workflow_id": "echo", "task_id": "task", "run_id": "run", "status": "failed",
                 "last_error_code": "agent_auth_failed", "last_error_message": "模型认证失败（HTTP 401）。检查服务凭证。",
                 "payload": {"message": "test"}, "reason": "test"}
        class Client:
            def call(self, *args):
                if args[:2] == ("events", "show"):
                    return event
                if args[:2] == ("runs", "output"):
                    return {"text": None}
                if args[:2] == ("history", "search"):
                    return [{"run_id": "run"}]
                return {"run": {"run_id": "run", "status": "failed"}, "event": event, "output": {"text": None}}
        ui = make_ui(Client())
        ui.pending = event
        ui.poll()
        collect(ui)
        self.assertIn("401", ui.transcript.text)
        self.assertIn("agent_auth_failed", ui.transcript.text)
        self.assertIn("未自动重发", ui.transcript.text)
        reopened = make_ui(Client())
        reopened.restore()
        collect(reopened)
        self.assertIn("401", reopened.transcript.text)

    def test_non_utf8_locale_is_rejected_before_application_start(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
             patch.dict(os.environ, {"TERM": "xterm"}), patch("locale.setlocale"), \
             patch("locale.nl_langinfo", return_value="ANSI_X3.4-1968"), patch.object(TerminalUI, "run") as start:
            with self.assertRaises(RuntimeContractError):
                run(Path("/tmp/ui-config.toml"))
            start.assert_not_called()

    def test_safe_output_and_descriptive_slash_completion(self):
        self.assertNotIn("\x1b", visible("中文\x1b[2J\u202e"))
        self.assertNotIn("\u202e", visible("中文\x1b[2J\u202e"))
        completer = SlashCompleter()
        self.assertEqual([c.text for c in completer.get_completions(Document("/eve"), None)], ["/events"])
        self.assertEqual([c.text for c in completer.get_completions(Document("1qrs"), None)], [])
        self.assertIn("/mode checkin", [c.text for c in completer.get_completions(Document("/mode "), None)])

    def test_unknown_submit_preserves_key_and_pending_replies_preserve_draft(self):
        class Client:
            def __init__(self): self.calls = []
            def call(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                if args[0] == "events":
                    return {"event_id": "same-event", "workflow_id": "echo", "task_id": "task", "status": "succeeded", "run_id": "run"}
                if args[0] == "runs":
                    return {"status": "available", "text": "model reply", "truncated": True}
                if len(self.calls) == 1:
                    raise CliFailure("timeout", "cli_timeout")
                return {"event_id": "same-event", "workflow_id": "echo", "task_id": "task", "status": "queued"}
        client = Client()
        ui = make_ui(client)
        self.assertTrue(ui.send("1qrs 中文 $(echo literal) `id`"))
        collect(ui)
        self.assertIn("/retry-send", ui.notice + "\n".join(ui.system))
        ui.editor.text = "第二条草稿 q123"
        ui.accept()
        self.assertEqual(ui.editor.text, "第二条草稿 q123")
        self.assertEqual(len(client.calls), 1)
        ui.execute("/retry-send")
        collect(ui)
        self.assertEqual(client.calls[0], client.calls[1])
        self.assertNotIn("中文", " ".join(client.calls[0][0]))
        ui.poll()
        collect(ui)
        self.assertIsNone(ui.pending)
        self.assertIn("model reply", "\n".join(ui.chat[ui.scope]))
        self.assertIn("截断", "\n".join(ui.chat[ui.scope]))
        self.assertEqual(ui.editor.text, "第二条草稿 q123")

    def test_nonblocking_memory_wizard_preserves_scope_source_and_version(self):
        calls = []
        class Client:
            def call(self, *args, **kwargs):
                calls.append((args, kwargs))
                return {}
        ui = make_ui(Client(), "coach", "training")
        ui.shared = False
        ui.editor.text = "保留原草稿"
        note = {"key": "progress", "title": "旧标题", "content": "旧摘要", "kind": "summary",
                "pinned": True, "expires_at": None, "source_event_id": "original-event", "version": 7}
        ui.memory_edit(note=note)
        for value in ("更正", "口胡未编码", "summary", "yes", ""):
            ui.editor.text = value
            ui.accept()
        collect(ui)
        self.assertIsNone(ui.wizard)
        self.assertEqual(ui.editor.text, "保留原草稿")
        args, kwargs = calls[0]
        self.assertEqual(args[args.index("--task") + 1], "training")
        self.assertEqual(args[args.index("--expected-version") + 1], "7")
        self.assertEqual(args[args.index("--source-event") + 1], "original-event")
        self.assertIn("--pinned", args)
        self.assertEqual(kwargs["text_files"]["--content-file"], "口胡未编码")

    def test_scope_switch_discards_stale_history_without_losing_input(self):
        release = threading.Event()
        class Client:
            def call(self, *args):
                if args[:2] == ("history", "show"):
                    release.wait(2)
                    return {"run": {"run_id": "old-run", "status": "succeeded"}, "event": {
                        "event_id": "old-event", "payload": {"message": "OLD_PRIVATE"}, "reason": "old"},
                        "output": {"text": "OLD_REPLY"}}
                return [{"run_id": "old-run"}] if "old-task" in args else []
        ui = make_ui(Client(), "coach", "old-task")
        ui.editor.text = "尚未发送的输入"
        ui.restore()
        ui.choose_scope("coach", "new-task")
        release.set()
        collect(ui)
        self.assertNotIn("OLD_PRIVATE", ui.transcript.text)
        self.assertEqual(ui.editor.text, "尚未发送的输入")

    def test_picker_escape_and_wizard_cancel_restore_draft(self):
        ui = make_ui(None)
        ui.editor.text = "q123 是普通文字"
        selected = []
        ui.open_picker("模式", [("安排", lambda: selected.append("plan")), ("复盘", lambda: selected.append("review"))])
        ui.editor.text = "复盘"
        ui.select()
        self.assertEqual(selected, ["review"])
        self.assertEqual(ui.editor.text, "q123 是普通文字")
        ui.memory_edit()
        ui.escape()
        self.assertEqual(ui.editor.text, "q123 是普通文字")
        self.assertIsNone(ui.wizard)

    def test_archive_paging_keeps_task_and_query(self):
        calls = []
        class Client:
            def call(self, *args):
                calls.append(args)
                return {"messages": [], "archive_mode": "all_branches", "has_more": False}
        ui = make_ui(Client(), "coach", "today")
        ui.execute('/history messages yesterday "线性基"')
        collect(ui)
        ui.execute("/next")
        collect(ui)
        self.assertEqual(calls[1][calls[1].index("--task") + 1], "yesterday")
        self.assertEqual(calls[1][calls[1].index("--query") + 1], "线性基")
        self.assertEqual(calls[1][calls[1].index("--offset") + 1], "20")

    def test_validation_never_silently_truncates_or_clears_draft(self):
        ui = make_ui(None)
        for draft in ("汉" * 11000, "/unknown", '/history "unfinished'):
            ui.editor.text = draft
            ui.accept()
            self.assertEqual(ui.editor.text, draft)
        ui.busy = "正在读取"
        ui.editor.text = "q123"
        ui.accept()
        self.assertEqual(ui.editor.text, "q123")

    def test_wizard_keeps_collecting_background_reply_and_escaping_does_not_cancel_event(self):
        class Client:
            def call(self, *args):
                if args[0] == "events":
                    return {"event_id": "event", "workflow_id": "echo", "task_id": "task", "status": "succeeded", "run_id": "run"}
                return {"status": "available", "text": "completed while editing", "truncated": False}
        ui = make_ui(Client())
        ui.pending = {"event_id": "event", "status": "running"}
        ui.memory_edit()
        ui.editor.text = "unsaved-key"
        ui.poll()
        collect(ui)
        self.assertIsNotNone(ui.wizard)
        self.assertEqual(ui.editor.text, "unsaved-key")
        self.assertIn("completed while editing", ui.transcript.text)
        self.assertIsNone(ui.pending)

    def test_failed_save_keeps_all_memory_fields_for_correction(self):
        class Client:
            def call(self, *args, **kwargs):
                raise CliFailure("another client changed the note", "conflict")
        ui = make_ui(Client())
        ui.memory_edit("remember")
        for value in ("remember", "标题", "必须保留的草稿", "summary", "no", ""):
            ui.editor.text = value
            ui.accept()
        collect(ui)
        self.assertIsNotNone(ui.wizard)
        self.assertFalse(ui.wizard.get("saving"))
        self.assertEqual(ui.wizard["answers"]["content"], "必须保留的草稿")

    def test_reports_and_replies_render_in_chronological_order(self):
        ui = make_ui(None)
        ui.info("操作说明", "long help")
        ui.chat.setdefault(ui.scope, []).append("教练\nnewest reply")
        ui.draw()
        self.assertLess(ui.transcript.text.index("long help"), ui.transcript.text.index("newest reply"))

    def test_command_entered_during_save_ack_is_deferred_and_executed(self):
        release = threading.Event()
        calls = []
        class Client:
            def call(self, *args, **kwargs):
                calls.append(args)
                if args[0] == "memory":
                    release.wait(3)
                return {}
        ui = make_ui(Client())
        ui.memory_edit("note")
        for value in ("note", "标题", "笔记内容", "fact", "no", ""):
            ui.editor.text = value
            ui.accept()
        self.assertTrue(ui.wizard["saving"])
        ui.editor.text = "/status"
        ui.accept()
        self.assertEqual(ui.deferred["text"], "/status")
        release.set()
        collect(ui)
        self.assertEqual([args[0] for args in calls], ["memory", "status"])
        self.assertIsNone(ui.wizard)
        self.assertIsNone(ui.deferred)


class TerminalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(self.path)
        self.config = replace(config, server=replace(config.server, poll_interval_seconds=0.02),
            agent=replace(config.agent, executable=str(Path(__file__).with_name("fake_omp.py").absolute()),
                startup_timeout_seconds=3, ack_timeout_seconds=1, turn_timeout_seconds=10,
                settle_timeout_seconds=2, graceful_stop_seconds=1))
        self.path.write_text(config_text(self.config))
        create(self.config, "echo", self.root, "example")
        self.log = (self.root / "daemon.log").open("w")
        self.addCleanup(self.log.close)
        self.daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
            stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop_daemon)
        self.client = CliClient(self.path)
        self.addCleanup(self.client.close)
        self.until(lambda: self.api("status").get("daemon") == "running")

    def stop_daemon(self):
        if self.daemon.poll() is None:
            self.daemon.terminate()
            try: self.daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
                self.daemon.wait(timeout=5)

    def api(self, command, args=None):
        return request(self.config.server.socket_path, command, args)

    def until(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = condition()
                if result: return result
            except RuntimeContractError:
                pass
            time.sleep(0.03)
        self.fail("Condition did not become true")

    def test_public_cli_backend_message_memory_and_duplicate_request(self):
        body = "中文 UI literal $(echo secret) `id` ${event_json}"
        event = self.client.call("ask", "--workflow", "echo", "--task", "ui-task", "--dedupe-key", "ui-idempotent",
                                 text_files={"--message-file": body})
        self.assertEqual(self.client.call("events", "show", event["event_id"])["payload"]["message"], body)
        self.until(lambda: self.api("events.show", {"event_id": event["event_id"]})["status"] == "succeeded")
        again = self.client.call("ask", "--workflow", "echo", "--task", "ui-task", "--dedupe-key", "ui-idempotent",
                                 text_files={"--message-file": body})
        self.assertEqual(again["event_id"], event["event_id"])
        self.assertEqual(len(self.api("runs.list")), 1)
        note = self.client.call("memory", "put", "--workflow", "echo", "--key", "ui-preference", "--title", "提示偏好",
                                text_files={"--content-file": "先提示"})
        self.assertEqual(note["version"], 1)
        self.assertEqual(self.client.call("memory", "put", "--workflow", "echo", "--key", "leading-hyphen",
            "--title", "--literal title", text_files={"--content-file": "literal"})["title"], "--literal title")
        with self.assertRaises(CliFailure) as failure:
            self.client.call("memory", "put", "--workflow", "echo", "--key", "ui-preference", "--title", "不覆盖",
                             text_files={"--content-file": "new"})
        self.assertEqual(failure.exception.code, "conflict")
        self.assertNotIn(body, (self.root / "daemon.log").read_text())

    def test_customization_conflict_preserves_prompt_draft(self):
        ui = make_ui(self.client)
        ui.execute("/prompt edit")
        collect(ui)
        self.assertIsNotNone(ui.wizard)
        self.client.call("workflow", "customize", "prompt", "echo", "--expected-revision", "0", "--mode", "append",
                         text_files={"--content-file": "另一个窗口已修改"})
        ui.accept()
        ui.replace_draft("我的未保存草稿")
        ui.accept()
        collect(ui)
        self.assertIsNotNone(ui.wizard)
        self.assertEqual(ui.wizard["answers"]["content"], "我的未保存草稿")
        self.assertIn("conflict", ui.transcript.text)
        actual = self.client.call("workflow", "customize", "show", "echo")["settings"]
        self.assertEqual(actual["system_prompt"]["text"], "另一个窗口已修改")

    def test_non_terminal_json_rejection_and_closed_client(self):
        result = subprocess.run([sys.executable, "-m", "oicoach_runtime", "ui", "--config", str(self.path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("交互终端", result.stderr)
        result = subprocess.run([sys.executable, "-m", "oicoach_runtime", "ui", "--json", "--config", str(self.path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "invalid_input")
        self.client.close()
        with self.assertRaises(CliFailure):
            self.client.call("status")

    def test_diagnostic_failure_remains_viewable(self):
        # No OMP model is needed; a nonexistent selected executable produces doctor ok=false.
        bad = replace(self.config, agent=replace(self.config.agent, executable="/missing-paveway-omp"))
        self.path.write_text(config_text(bad))
        report = self.client.call("doctor")
        self.assertFalse(report["ok"])
        self.assertTrue(report["checks"])

    def open_terminal(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        before = termios.tcgetattr(slave)
        process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "ui", "--config", str(self.path),
            "--workflow", "echo", "--task", "pty-task"], stdin=slave, stdout=slave, stderr=slave,
            env=dict(os.environ, TERM="xterm-256color", LC_ALL="C.UTF-8"), start_new_session=True, close_fds=True)
        def stop():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.addCleanup(stop)
        screen = pyte.Screen(120, 32)
        stream = pyte.ByteStream(screen)
        def read_until(needle, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                rendered = "\n".join(screen.display)
                if needle in rendered:
                    return rendered
                self.assertIsNone(process.poll(), rendered)
                if select.select([master], [], [], 0.1)[0]:
                    stream.feed(os.read(master, 65536))
            self.fail("Terminal did not render " + needle + ":\n" + "\n".join(screen.display))
        read_until("已恢复最近对话")
        return master, slave, process, screen, stream, read_until, before

    @unittest.skipUnless(pyte, "install .[test] to verify actual terminal screen cells")
    def test_pty_git_sync_setup_remote_push_history_and_pause(self):
        remote = self.root / "user-data.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], capture_output=True, check=True)
        master, slave, process, screen, stream, read_until, before = self.open_terminal()
        os.write(master, b"/sync setup\r")
        read_until("专用本地 Git 数据目录")
        os.write(master, b"\r")
        read_until("SSH/HTTPS remote")
        os.write(master, (str(remote) + "\r").encode())
        read_until("同步分支")
        os.write(master, b"\r")
        read_until("同步间隔秒数")
        os.write(master, b"\r")
        read_until("Git 同步配置已保存")
        os.write(master, b"sync UI history request\r")
        read_until("fake completion")
        os.write(master, b"/sync now\r")
        read_until("Git 同步已请求")
        report = self.until(lambda: (lambda r: r if r["last"].get("exported_runs") == 1 and r["last"].get("state") == "ok" else None)(self.client.call("sync", "status")))
        read_until("Git 同步完成")
        names = subprocess.check_output(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"], text=True)
        self.assertIn("devices/" + report["settings"]["device_id"] + "/runs/", names)
        os.write(master, b"/sync history\r")
        read_until("跨设备历史 · ↑↓ Enter")
        os.write(master, b"\r")
        read_until("同步历史 ·")
        os.write(master, b"/sync pause\r")
        read_until("Git 同步已暂停")
        self.assertFalse(self.client.call("sync", "status")["settings"]["enabled"])
        identity = report["settings"]["device_id"]
        os.write(master, b"/sync remote\r")
        read_until("清空后停止上传")
        os.write(master, b"\x01\x0b\r")
        read_until("remote 已保存")
        self.until(lambda: self.client.call("sync", "status")["settings"]["remote"] == "")
        settings = self.client.call("sync", "status")["settings"]
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["device_id"], identity)
        self.assertEqual(subprocess.check_output(["git", "-C", settings["repository"], "remote"], text=True), "")
        os.write(master, b"/quit\r")
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    @unittest.skipUnless(pyte, "install .[test] to verify actual terminal screen cells")
    def test_pty_custom_prompt_and_skill_crud_reach_native_dispatch(self):
        master, slave, process, screen, stream, read_until, before = self.open_terminal()
        def settings():
            return self.client.call("workflow", "customize", "show", "echo")["settings"]
        os.write(master, b"/prompt edit\r")
        read_until("append 追加 / replace 替换")
        os.write(master, b"\r")
        read_until("系统提示词正文")
        os.write(master, "先给提示\n不要直接给答案 ${literal}\r".encode())
        self.until(lambda: settings()["revision"] == 1)
        read_until("自定义设置已保存 · v1")
        os.write(master, b"/skills add\r")
        read_until("选择 Skill 分类")
        read_until("user information")
        read_until("personal skill")
        os.write(master, b"\x1b[B\r")
        read_until("Skill 名称")
        os.write(master, b"training-skill\r")
        read_until("用途描述")
        os.write(master, "复盘时使用\r".encode())
        read_until("Skill Markdown 正文")
        os.write(master, "先检查边界情况\n再检查复杂度\r".encode())
        self.until(lambda: settings()["revision"] == 2)
        read_until("自定义设置已保存 · v2")
        self.assertEqual(settings()["skills"][0]["category"], "user-information")
        os.write(master, b"/skills edit training-skill\r")
        read_until("编辑用户 Skill")
        os.write(master, b"\r")
        read_until("Skill Markdown 正文")
        os.write(master, "，保留原文\r".encode())
        self.until(lambda: settings()["revision"] == 3)
        read_until("自定义设置已保存 · v3")
        self.assertEqual(settings()["skills"][0]["content"], "先检查边界情况\n再检查复杂度，保留原文")
        os.write(master, b"test customized dispatch\r")
        read_until("fake completion")
        runs = self.api("runs.list")
        self.assertEqual(len(runs), 1)
        snapshots = list((self.config.state_dir / "sessions").glob("*/customizations/*/settings.json"))
        self.assertEqual(len(snapshots), 1)
        saved = json.loads(snapshots[0].read_text())
        self.assertEqual(saved["system_prompt"]["text"], "先给提示\n不要直接给答案 ${literal}")
        self.assertEqual(saved["revision"], 3)
        system = snapshots[0].with_name("system.md").read_text()
        self.assertIn("## user information", system)
        self.assertIn("先检查边界情况\n再检查复杂度，保留原文", system)
        os.write(master, b"/skills category training-skill\r")
        read_until("移动 Skill 分类")
        os.write(master, b"\x1b[B\x1b[B\r")
        self.until(lambda: settings()["revision"] == 4)
        read_until("自定义设置已保存 · v4")
        self.assertEqual(settings()["skills"][0]["category"], "personal-skill")
        self.assertIn("## user information", snapshots[0].with_name("system.md").read_text())
        os.write(master, b"/skills disable training-skill\r")
        self.until(lambda: settings()["revision"] == 5)
        read_until("自定义设置已保存 · v5")
        self.assertFalse(settings()["skills"][0]["enabled"])
        os.write(master, b"/prompt reset\r")
        read_until("恢复默认系统提示词")
        os.write(master, b"yes\r")
        self.until(lambda: settings()["revision"] == 6)
        read_until("自定义设置已保存 · v6")
        self.assertEqual(settings()["system_prompt"]["text"], "")
        self.assertEqual(len(settings()["skills"]), 1)
        os.write(master, b"/skills delete training-skill\r")
        read_until("删除用户 Skill")
        os.write(master, b"yes\r")
        self.until(lambda: settings()["revision"] == 7)
        read_until("自定义设置已保存 · v7")
        self.assertEqual(settings()["skills"], [])
        os.write(master, b"/quit\r")
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    @unittest.skipUnless(pyte, "install .[test] to verify actual terminal screen cells")
    def test_pty_plain_typing_bracketed_paste_resize_and_detached_lifecycle(self):
        (self.root / "fake-mode").write_text("hold")
        master, slave, process, screen, stream, read_until, before = self.open_terminal()
        marker = "1qrs 中文_" + uuid.uuid4().hex[:8]
        os.write(master, marker.encode())
        rendered = read_until(marker)
        self.assertNotIn("1:概览", rendered)
        self.assertNotIn("Ctrl+D 提交", rendered)
        pasted = "\n第二行 q123 /quit"
        os.write(master, b"\x1b[200~" + pasted.encode() + b"\x1b[201~")
        read_until("第二行 q123 /quit")
        self.assertEqual(self.api("events.list"), [])
        # Editing survives terminal resize, with the composer still at the bottom.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 60, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        time.sleep(0.15)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        os.write(master, b"\r")
        expected = marker + pasted
        def submitted():
            for row in self.api("events.list"):
                event = self.api("events.show", {"event_id": row["event_id"]})
                if event["payload"].get("message") == expected:
                    return event
        event = self.until(submitted)
        self.until(lambda: (self.root / "fake-entered").exists())
        os.write(master, b"\x04")  # Empty composer: exit UI, not the daemon.
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        after = termios.tcgetattr(slave)
        self.assertEqual(before[3] & (termios.ECHO | termios.ICANON), after[3] & (termios.ECHO | termios.ICANON))
        self.assertIsNone(self.daemon.poll())
        # Re-enter while the daemon is still executing: resume polling, no manual refresh.
        master2, slave2, process2, screen2, stream2, read_until2, before2 = self.open_terminal()
        read_until2("正在继续等待最终回复")
        read_until2("已等待")
        os.write(master2, b"NEXT_DRAFT")
        (self.root / "fake-release").touch()
        read_until2("fake completion")
        read_until2("NEXT_DRAFT")
        self.until(lambda: self.api("events.show", {"event_id": event["event_id"]})["status"] == "succeeded")
        self.assertEqual(self.api("events.show", {"event_id": event["event_id"]})["attempt"], 1)
        self.assertEqual(len(self.api("runs.list")), 1)
        os.write(master2, b"\x15/quit\r")
        process2.wait(timeout=5)

    @unittest.skipUnless(pyte, "install .[test] to verify actual terminal screen cells")
    def test_pty_slash_completion_arrow_selector_and_reply_keep_next_draft(self):
        master, slave, process, screen, stream, read_until, before = self.open_terminal()
        os.write(master, b"/eve")
        read_until("查看 / 取消事件")
        os.write(master, b"\x1b[B\r")  # Select completion, Enter executes it once.
        read_until("没有匹配记录")
        os.write(master, b"/workflow\r")
        read_until("选择 workflow")
        os.write(master, b"\x1b[B\x1b[A\r")  # Choose by arrows, never numeric keys.
        read_until("已恢复最近对话")
        os.write(master, b"UI request\r")
        draft = "下一条草稿 q123"
        os.write(master, draft.encode())
        read_until("fake completion")
        rendered = read_until(draft)
        self.assertIn("输入请求或 / 命令", rendered)
        self.assertIn("fake completion", rendered)
        self.assertEqual(len(self.api("runs.list")), 1)
        os.write(master, b"\x03/quit\r")
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    @unittest.skipUnless(pyte, "install .[test] to verify actual terminal screen cells")
    def test_pty_memory_create_edit_and_scheduled_event_cancel_really_execute(self):
        master, slave, process, screen, stream, read_until, before = self.open_terminal()
        os.write(master, b"/memory add ui-note\r")
        read_until("笔记 key")
        os.write(master, b"\r")
        read_until("笔记标题")
        os.write(master, "中文笔记\r".encode())
        read_until("正文")
        os.write(master, "先口胡\n后编码\r".encode())
        read_until("fact / preference")
        os.write(master, b"\x15summary\r")
        read_until("优先加载")
        os.write(master, b"\r")
        read_until("有效期 RFC3339")
        os.write(master, b"\r")
        self.until(lambda: self.api("memory.show", {"workflow_id": "echo", "key": "ui-note"}))
        read_until("操作已保存")
        note = self.api("memory.show", {"workflow_id": "echo", "key": "ui-note"})
        self.assertEqual(note["content"], "先口胡\n后编码")
        self.assertEqual(note["kind"], "summary")
        os.write(master, b"/memory edit ui-note\r")
        read_until("笔记标题")
        os.write(master, b"\r")
        read_until("正文")
        os.write(master, "，尚未通过\r".encode())  # Preloaded editor cursor must be at the end.
        read_until("fact / preference")
        os.write(master, b"\r")
        read_until("优先加载")
        os.write(master, b"\r")
        read_until("有效期 RFC3339")
        os.write(master, b"\r")
        self.until(lambda: self.api("memory.show", {"workflow_id": "echo", "key": "ui-note"})["version"] == 2)
        self.assertEqual(self.api("memory.show", {"workflow_id": "echo", "key": "ui-note"})["content"], "先口胡\n后编码，尚未通过")
        read_until("操作已保存")
        os.write(master, b"/schedule\r")
        read_until("RFC3339（含时区）或 now")
        os.write(master, b"\x152099-01-01T00:00:00Z\r")
        read_until("请求内容")
        os.write(master, "明天检查\r".encode())
        rows = self.until(lambda: self.api("events.list"))
        event_id = rows[0]["event_id"]
        read_until("操作已保存")
        os.write(master, ("/events cancel " + event_id + "\r").encode())
        read_until("输入 yes 确认")
        os.write(master, b"yes\r")
        self.until(lambda: self.api("events.show", {"event_id": event_id})["status"] == "cancelled")
        self.assertEqual(self.api("runs.list"), [])
        read_until("操作已保存")
        os.write(master, b"/quit\r")
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
