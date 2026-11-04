import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

from test_native_omp import NativeFixture
from oicoach_runtime import coaches, customization
from oicoach_runtime.ipc import request

from test_native_mcp import FAKE_MCP_SERVER_CODE

try:
    import pyte
    from terminal_screen import WideScreen
except ImportError:
    pyte = None


class NativeManagementTests(NativeFixture):
    def open_ui(self, launcher=False, *, columns=120, screen=None):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, columns, 0, 0))
        stream = pyte.ByteStream(screen) if screen is not None else None
        argv = [sys.executable, "-m", "oicoach_runtime", "native"]
        if launcher:
            argv = [str(Path(__file__).resolve().parents[1] / "paveway.sh")]
        process = subprocess.Popen([*argv, "--config", str(self.path), "--workflow", "native-test"],
            env={**self.env, "PAVEWAY_PYTHON": sys.executable}, cwd=self.root, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.set_blocking(master, False)
        raw = bytearray()
        def cleanup():
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            os.close(master)
        self.addCleanup(cleanup)
        def pump(timeout=0.1):
            if select.select([master], [], [], timeout)[0]:
                try: chunk = os.read(master, 65536)
                except OSError: return
                raw.extend(chunk)
                if stream is not None:
                    stream.feed(chunk)
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
        def until(text, predicate=lambda: True):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if text.encode() in raw and predicate(): return
                pump()
            self.fail("Native management did not reach state: " + raw.decode(errors="replace")[-3000:])
        def send(text):
            pending = text if isinstance(text, bytes) else text.encode()
            deadline = time.monotonic() + 20
            while pending and time.monotonic() < deadline:
                pump(0)
                if select.select([], [master], [], .1)[1]:
                    try:
                        count = os.write(master, pending[:4096])
                        pending = pending[count:]
                    except BlockingIOError:
                        pass
            self.assertFalse(pending, "native fixture input did not drain")
        def exit_ui():
            send("/exit\r")
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                pump()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, raw.decode(errors="replace")[-2000:])
        until("OMP native")
        return process, raw, send, until, pump, exit_ui

    def open_settings_ui(self, *, columns=120):
        if pyte is None:
            self.skipTest("install .[test] to verify actual terminal screen cells")
        screen = WideScreen(columns, 40)
        process, raw, send, _until, pump, exit_ui = self.open_ui(columns=columns, screen=screen)

        def until(text, predicate=lambda: True):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                rendered = "\n".join(screen.display)
                if text in rendered and predicate():
                    return rendered
                self.assertIsNone(process.poll(), rendered)
                pump()
            self.fail("Native settings did not render " + text + ":\n" + "\n".join(screen.display))

        return screen, raw, send, until, pump, exit_ui

    def tijian_fixture(self, name="fake_tijian_mcp.py"):
        executable = self.root / name
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(FAKE_MCP_SERVER_CODE)
        executable.chmod(0o755)
        return executable

    @property
    def tijian_config_path(self):
        return self.root / "workspace/.omp/mcp.json"

    def seed_tijian(self, executable, permission="managed"):
        self.tijian_config_path.parent.mkdir(mode=0o700, exist_ok=True)
        self.tijian_config_path.write_text(json.dumps({"mcpServers": {
            "tijian": {"command": str(executable), "args": ["--write-policy=" + permission]},
        }}))
        self.tijian_config_path.chmod(0o600)

    def assert_tijian(self, executable, permission):
        data = json.loads(self.tijian_config_path.read_text())
        self.assertEqual(data["mcpServers"]["tijian"], {
            "command": str(executable), "args": ["--write-policy=" + permission],
        })
        self.assertNotIn("tijian", data.get("disabledServers", []))

    def assert_no_settings_chat(self):
        self.assertEqual(self.server.requests, [])
        for conversation in request(self.config.server.socket_path, "frontend.list"):
            archive = request(self.config.server.socket_path, "frontend.messages",
                {"conversation_id": conversation["conversation_id"]})
            self.assertFalse([row for row in archive["messages"] if row["role"] == "user"], archive)
        for path in (self.config.state_dir / "native").rglob("*.jsonl"):
            for line in path.read_text().splitlines():
                record = json.loads(line)
                if record.get("type") == "message":
                    self.assertNotEqual(record.get("message", {}).get("role"), "user", str(path))

    def test_settings_is_reachable_from_native_paveway_menu_without_chat(self):
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway\r")
        until("Paveway")  # A random startup tip can contain "prompt" before the menu is mounted.
        # Existing native menu order: prompt through vjudge, then settings.
        send(b"\x1b[B" * 10)
        until("settings")
        send("\r")
        rendered = until("Thinking preference")
        self.assertIn("Tijian MCP", rendered)
        self.assertIn("Model preference", rendered)
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        self.assertFalse(self.tijian_config_path.exists())
        exit_ui()
        self.assert_no_settings_chat()

    def test_settings_model_and_thinking_dialogs_return_to_selected_root_row(self):
        before = customization.show(self.config, "native-test")["settings"]
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway settings\r")
        until("Thinking preference")
        send(b"\x1b[B\r")
        until("Portable model preference (next launch)")
        send(b"\x1b")
        until("Thinking preference")
        # Cancel restores the selected Model row, rather than resetting to Tijian.
        send("\r")
        until("Portable model preference (next launch)")
        send(b"\x1b")
        until("Thinking preference")
        send(b"\x1b[B\r")
        until("Portable thinking preference (next launch)")
        send(b"\x1b")
        until("Model preference")
        send("\r")
        until("Portable thinking preference (next launch)")
        send(b"\x1b")
        until("Model preference")
        self.assertEqual(customization.show(self.config, "native-test")["settings"], before)
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        exit_ui()
        self.assert_no_settings_chat()

    def test_settings_navigation_alias_and_first_tijian_path_save_survive_reopen(self):
        executable = self.tijian_fixture()
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway settings\r")
        rendered = until("Thinking preference")
        self.assertIn("Tijian MCP", rendered)
        self.assertIn("Model preference", rendered)
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        self.assertFalse(self.tijian_config_path.exists())

        send("/paveway tijian\r")
        rendered = until("MCP executable")
        self.assertIn("Permission", rendered)
        self.assertIn("Configuration", rendered)
        self.assertIn("Not configured", rendered)
        self.assertIn("Managed", rendered)
        send(b"\x1b[B\r")
        until("Tijian service permission")
        send("\r")  # Explicitly keep the recommended Managed selection before setting a path.
        until("MCP executable")
        self.assertFalse(self.tijian_config_path.exists())
        send(b"\x1b[A\r")
        until("Absolute native tijian-mcp executable path")
        send(str(executable) + "\r")
        until("MCP executable", lambda: self.tijian_config_path.exists())
        self.assert_tijian(executable, "managed")
        # Notifications may wrap, so inspect fresh bytes as well as the live page.
        self.assertIn(b"Saved. Restart the Paveway native OMP session to apply Tijian MCP changes.", raw)
        self.assertIn(str(executable), "\n".join(screen.display))
        send(b"\x1b")
        until("Thinking preference")
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        exit_ui()
        self.assert_no_settings_chat()

        # A new native process must read the saved file, not an in-memory UI default.
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway settings\r")
        until("Thinking preference")
        send("\r")
        rendered = until("MCP executable")
        self.assertIn(str(executable), rendered)
        self.assertIn("Managed", rendered)
        send("\r")
        until("Absolute native tijian-mcp executable path")
        before = self.tijian_config_path.read_bytes()
        send("\r")  # Empty replacement keeps the existing executable without retyping it.
        until("MCP executable")
        self.assertEqual(self.tijian_config_path.read_bytes(), before)
        self.assert_tijian(executable, "managed")
        send(b"\x1b")
        until("Thinking preference")
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        exit_ui()
        self.assert_no_settings_chat()

    def test_tijian_permission_none_and_explicit_full_confirmation_keep_path(self):
        executable = self.tijian_fixture()
        self.seed_tijian(executable)
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway tijian\r")
        until("MCP executable")
        send(b"\x1b[B\r")
        until("Tijian service permission")
        # Managed is initially selected: Up chooses None, not the default row.
        send(b"\x1b[A\r")
        until("MCP executable", lambda:
            json.loads(self.tijian_config_path.read_text())["mcpServers"]["tijian"]["args"] == ["--write-policy=none"])
        self.assert_tijian(executable, "none")
        before = self.tijian_config_path.read_bytes()

        # The Permission row remains selected when its dialog closes.
        send("\r")
        until("Tijian service permission")
        send(b"\x1b[B\x1b[B\r")
        until("Enable Full Tijian access?")
        send(b"\x1b")
        until("MCP executable")
        self.assertEqual(self.tijian_config_path.read_bytes(), before)
        self.assert_tijian(executable, "none")

        send("\r")
        until("Tijian service permission")
        send(b"\x1b[B\x1b[B\r")
        until("Enable Full Tijian access?")
        until("Yes")
        send("\r")  # Accept the native dialog's Yes option, not the permission picker.
        until("MCP executable", lambda:
            json.loads(self.tijian_config_path.read_text())["mcpServers"]["tijian"]["args"] == ["--write-policy=full"])
        self.assert_tijian(executable, "full")
        self.assertIn("Full", "\n".join(screen.display))
        self.assertIn(b"Saved. Restart the Paveway native OMP session to apply Tijian MCP changes.", raw)
        before = self.tijian_config_path.read_bytes()
        send("\r")
        until("Tijian service permission")
        # Reaccepting the initialized Full selection neither downgrades nor reconfirms.
        raw.clear()
        send("\r")
        until("MCP executable")
        self.assertEqual(self.tijian_config_path.read_bytes(), before)
        self.assertNotIn(b"Enable Full Tijian access?", raw)
        send(b"\x1b")
        until("Thinking preference")
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        exit_ui()
        self.assert_no_settings_chat()

    def test_tijian_path_input_escape_and_path_only_edit_preserve_full_policy(self):
        executable = self.tijian_fixture()
        replacement = self.tijian_fixture("replacement_tijian_mcp.py")
        self.seed_tijian(executable, "full")
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway tijian\r")
        rendered = until("MCP executable")
        self.assertIn("Full", rendered)
        before = self.tijian_config_path.read_bytes()
        send("\r")
        until("Absolute native tijian-mcp executable path")
        send(b"\x01\x0b")
        send(str(replacement))
        send(b"\x1b")
        until("MCP executable")
        self.assertEqual(self.tijian_config_path.read_bytes(), before)
        send("\r")
        until("Absolute native tijian-mcp executable path")
        send(b"\x01\x0b")
        send(str(replacement) + "\r")
        until("MCP executable", lambda:
            json.loads(self.tijian_config_path.read_text())["mcpServers"]["tijian"]["command"] == str(replacement))
        self.assert_tijian(replacement, "full")
        self.assertNotIn(b"Enable Full Tijian access?", raw)
        send(b"\x1b")
        until("Thinking preference")
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        exit_ui()
        self.assert_no_settings_chat()

    def test_tijian_save_errors_restore_page_and_conflict_does_not_overwrite(self):
        executable = self.tijian_fixture()
        replacement = self.tijian_fixture("replacement_tijian_mcp.py")
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway tijian\r")
        until("MCP executable")
        send("\r")
        until("Absolute native tijian-mcp executable path")
        raw.clear()
        send(str(self.root / "missing-tijian-mcp") + "\r")
        until("MCP executable", lambda: b"not found or not executable" in raw)
        self.assertFalse(self.tijian_config_path.exists())
        self.assertNotIn(b"Saved. Restart", raw)

        # Failure must not drop back into chat or lose the selected path row.
        send("\r")
        until("Absolute native tijian-mcp executable path")
        send(str(executable) + "\r")
        until("MCP executable", lambda: self.tijian_config_path.exists())
        self.assert_tijian(executable, "managed")
        send("\r")
        until("Absolute native tijian-mcp executable path")
        concurrent = json.loads(self.tijian_config_path.read_text())
        concurrent["mcpServers"]["unrelated"] = {"command": "/fixture/unrelated", "args": []}
        original = json.dumps(concurrent, indent=2).encode()
        self.tijian_config_path.write_bytes(original)
        raw.clear()
        send(b"\x01\x0b")
        send(str(replacement) + "\r")
        until("MCP executable", lambda: b"changed since it was shown" in raw)
        self.assertEqual(self.tijian_config_path.read_bytes(), original)
        self.assertNotIn(b"Saved. Restart", raw)

        # Retrying after the page reload uses its fresh revision and preserves peers.
        send("\r")
        until("Absolute native tijian-mcp executable path")
        send(b"\x01\x0b")
        send(str(replacement) + "\r")
        until("MCP executable", lambda:
            json.loads(self.tijian_config_path.read_text())["mcpServers"]["tijian"]["command"] == str(replacement))
        self.assert_tijian(replacement, "managed")
        self.assertEqual(json.loads(self.tijian_config_path.read_text())["mcpServers"]["unrelated"],
            concurrent["mcpServers"]["unrelated"])
        send(b"\x1b")
        until("Thinking preference")
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        exit_ui()
        self.assert_no_settings_chat()

    def test_tijian_narrow_terminal_long_path_keeps_rows_and_navigation_usable(self):
        executable = self.tijian_fixture("/".join(["long-path-segment-" * 3] * 3) + "/fake_tijian_mcp.py")
        self.seed_tijian(executable)
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui(columns=52)
        send("/paveway settings\r")
        rendered = until("Thinking preference")
        self.assertIn("Tijian MCP", rendered)
        send("\r")
        rendered = until("MCP executable")
        self.assertIn("Permission", rendered)
        self.assertIn("Configuration", rendered)
        self.assertIn("Managed", rendered)
        self.assertNotIn("\ufffd", rendered)
        send("\r")
        until("Absolute native tijian-mcp")
        send(str(executable))
        send(b"\x05")
        rendered = until("fake_tijian_mcp.py")
        self.assertNotIn("\ufffd", rendered)
        send(b"\x1b")
        until("MCP executable")
        send(b"\x1b[B\r")
        until("Tijian service permission")
        send(b"\x1b[A\r")
        rendered = until("MCP executable", lambda:
            json.loads(self.tijian_config_path.read_text())["mcpServers"]["tijian"]["args"] == ["--write-policy=none"])
        self.assertIn("Permission", rendered)
        self.assertIn("Configuration", rendered)
        self.assertIn("None", rendered)
        self.assert_tijian(executable, "none")
        send("\r")
        until("Tijian service permission")
        send(b"\x1b")
        until("MCP executable")
        send(b"\x1b")
        until("Thinking preference")
        send(b"\x1b")
        until("OMP native", lambda: "Thinking preference" not in "\n".join(screen.display))
        exit_ui()
        self.assert_no_settings_chat()

    def test_one_key_native_launcher_from_another_directory(self):
        original = self.path.read_text() + '\n[tijian]\nenabled = true\nexecutable = "/fixture/retired-cli-must-not-run"\n'
        self.path.write_text(original)
        process, raw, send, until, pump, exit_ui = self.open_ui(launcher=True)
        exit_ui()
        self.assertEqual(self.server.requests, [])
        self.assertIn("旧 [tijian] 配置已保留但不再执行".encode(), raw)
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")

    def test_prompt_dialog_saves_privately_without_a_model_turn(self):
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway prompt\r")
        until("Prompt")
        send(b"\x1b[B\r")
        until("Custom user append system prompt")
        send("PRIVATE_PROMPT_UI_MARKER")
        send(b"\x11")  # OMP hook editor's native submit binding.
        until("Saved.", lambda: customization.show(self.config, "native-test")["settings"]["revision"] == 1)
        self.assertEqual(customization.show(self.config, "native-test")["settings"]["system_prompt"]["text"], "PRIVATE_PROMPT_UI_MARKER")
        exit_ui()
        self.assertEqual(self.server.requests, [])
        for path in (self.config.state_dir / "native").rglob("*.jsonl"):
            self.assertNotIn("PRIVATE_PROMPT_UI_MARKER", path.read_text())

    def test_vjudge_secret_dialog_masks_input_and_cancel_does_not_save(self):
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway vjudge\r")
        until("VJudge login")
        send(b"\x1b[B\x1b[B\r")
        until("VJudge username")
        send("fixture-user\r")
        until("VJudge password")
        send("NOT_REAL_SECRET_8274")
        for _ in range(5): pump(0.1)
        self.assertNotIn(b"NOT_REAL_SECRET_8274", raw)
        send(b"\x1b")
        for _ in range(5): pump(0.1)
        exit_ui()
        self.assertEqual(self.server.requests, [])
        for path in self.root.rglob("*"):
            if path.is_file() and path.stat().st_size < 32 * 1024 * 1024:
                self.assertNotIn(b"NOT_REAL_SECRET_8274", path.read_bytes(), str(path))

    def test_skill_dialog_preserves_category_and_body_without_chat_submission(self):
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway skills\r")
        until("Create Skill")
        send("\r")
        until("Skill name")
        send("private-ui-skill\r")
        until("Skill category")
        send(b"\x1b[B\r")
        until("When should GPT read this Skill?")
        send(b"Native skill description\x11")
        until("Skill body")
        body = "PRIVATE_SKILL_UI_BODY\n" + "中文 Skill body line\n" * 1800
        self.assertGreater(len(body.encode()), 32768)
        self.assertLess(len(body.encode()), 65536)
        send(b"\x1b[200~" + body.encode() + b"\x1b[201~")
        send(b"\x11")
        until("Saved.", lambda: customization.show(self.config, "native-test")["settings"]["revision"] == 1)
        skill = customization.show(self.config, "native-test")["settings"]["skills"][0]
        self.assertEqual(skill["category"], "user-information")
        self.assertEqual(skill["content"], body)
        exit_ui()
        self.assertEqual(self.server.requests, [])
        for path in (self.config.state_dir / "native").rglob("*.jsonl"):
            self.assertNotIn("PRIVATE_SKILL_UI_BODY", path.read_text())

    def test_profile_switch_releases_old_process_before_new_scope(self):
        profile = coaches.create(self.config, "beta", "native-test", "BETA_MANAGEMENT_PERSONA")
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway profile\r")
        until("Switch profile")
        send("\r")
        until("Select GPT profile")
        index = [row["coach_id"] for row in coaches.profiles(self.config)].index("beta")
        send(b"\x1b[B" * index + b"\r")
        until("beta/training", lambda: any(row["workflow_id"] == profile["runtime_workflow_id"] and row["status"] == "idle"
            for row in request(self.config.server.socket_path, "frontend.list")))
        rows = request(self.config.server.socket_path, "frontend.list")
        self.assertEqual(next(row for row in rows if row["workflow_id"] == "native-test")["status"], "released")
        self.assertIsNone(process.poll())
        exit_ui()

    def test_legacy_management_roundtrip_preserves_native_session_and_daemon(self):
        process, raw, send, until, pump, exit_ui = self.open_ui()
        before = request(self.config.server.socket_path, "frontend.list")[0]
        send("/paveway legacy\r")
        until("Enter 发送")
        record = request(self.config.server.socket_path, "frontend.show", {"conversation_id": before["conversation_id"]})
        self.assertEqual(record["status"], "released")
        send("/quit\r")
        until("OMP native", lambda: request(self.config.server.socket_path, "frontend.show",
            {"conversation_id": before["conversation_id"]})["generation"] > before["generation"] and
            request(self.config.server.socket_path, "frontend.show", {"conversation_id": before["conversation_id"]})["status"] == "idle")
        exit_ui()
        self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")
