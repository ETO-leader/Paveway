import fcntl
import json
import os
import zipfile
from pathlib import Path
from dataclasses import replace
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import threading

from test_native_omp import NativeFixture
from oicoach_runtime import coaches, customization
from oicoach_runtime.config import config_text
from oicoach_runtime.compatibility import probe_daemon
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request

from test_native_mcp import FAKE_MCP_SERVER_CODE

try:
    import pyte
    from terminal_screen import WideScreen
except ImportError:
    pyte = None


class NativeManagementTests(NativeFixture):
    def test_sync_status_shows_incomplete_history_without_model_request(self):
        from oicoach_runtime import git_sync
        process, raw, send, until, pump, exit_ui = self.open_ui()
        git_sync.set_enabled(self.config, False)
        git_sync.write_json(git_sync.local_root(self.config) / "status.json", {"state": "ok", "remote_synced": True,
            "session_archives": {"exported": 2, "skipped": 2, "deferred": 1, "attention": 1,
                "reasons": {"size_limit": 1, "active": 1}}})
        send("/paveway sync\r"); until("Private user-data Git")
        send("\r")
        until("本次历史备份不完整")
        until("size_limit")
        self.assertEqual(self.server.requests, [])
        exit_ui()

    def test_local_multi_skill_zip_selects_two_with_conflict_and_no_model_request(self):
        from multi_skill_fixture import write_eight_skills
        path = self.root / "Paveway-missing-skills.zip"
        bodies = write_eight_skills(path)
        customization.update(self.config, "native-test", 0, "skill-put", name="missing-skill-0", description="Old", content="Old body")
        customization.update(self.config, "native-test", 1, "skill-disable", name="missing-skill-0")
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway skills\r")
        until("Import standard")
        send(b"\x1b[B\r")
        until("本地 Skill 文件路径")
        send(str(path) + "\r")
        until("本地直导")
        until("替换同名")
        raw.clear(); send("\r"); until("1/8")
        raw.clear(); send(b"\x1b[B\r"); until("2/8")
        raw.clear(); send(b"\x1b[B" * 12 + b"\r"); until("确认原子安装")
        until("完整托管包属于 Paveway 用户数据")
        send("\r")
        until("Saved.", lambda: customization.show(self.config, "native-test")["settings"]["revision"] == 3)
        skills = customization.show(self.config, "native-test")["settings"]["skills"]
        self.assertEqual({s["name"] for s in skills}, {"missing-skill-0", "missing-skill-1"})
        self.assertTrue(all("package" in s and "content" not in s for s in skills))
        self.assertFalse(next(s for s in skills if s["name"] == "missing-skill-0")["enabled"])
        session = self.root / "session"
        session.mkdir(mode=0o700)
        settings = customization.show(self.config, "native-test")["settings"]
        argv = customization.snapshot(settings, session, "native-import", True,
                                      package_root=self.config.state_dir / "skill-packages")
        native = Path(json.loads(Path(argv[argv.index("--config") + 1]).read_text())["skills"]["customDirectories"][0])
        for relative, payload in bodies["missing-skill-1"].items():
            self.assertEqual((native / "missing-skill-1" / relative).read_bytes(), payload)
        self.assertFalse((native / "missing-skill-0").exists())
        self.assertEqual(self.server.requests, [])
        exit_ui()

    def test_packaged_skill_read_only_view_lists_and_shows_files(self):
        from oicoach_runtime import skill_import, skill_packages
        package = self.root / "view.zip"
        entry = b"---\nname: package-view-skill\ndescription: Viewable package\n---\nPRIVATE_PACKAGE_ENTRY_TOKEN\n"
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("package-view-skill/SKILL.md", entry)
            archive.writestr("package-view-skill/references/a.md", b"# A\nREFERENCE_TOKEN\n")
            archive.writestr("package-view-skill/assets/x.bin", bytes(range(256)))
        preview = skill_import.preview(self.config, "native-test", package)
        skill_import.install(self.config, "native-test", preview["revision"], {
            "file": preview["file"], "sha256": preview["sha256"], "replace": [],
            "skills": [{"name": "package-view-skill"}]})
        settings = customization.show(self.config, "native-test")["settings"]
        record = settings["skills"][0]
        skill_packages.verify(self.config, record["package"]["digest"])

        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway skills\r")
        until("完整包 3 文件")
        raw.clear()
        send(b"\x1b[B\x1b[B\x1b[B\r")
        until("查看入口 SKILL.md")
        raw.clear()
        send("\r")
        until("PRIVATE_PACKAGE_ENTRY_TOKEN")
        raw.clear()
        send(b"\x11")
        until("查看入口 SKILL.md")
        raw.clear()
        send(b"\x1b[B\r")
        until("references/a.md")
        until("二进制")
        raw.clear()
        send(b"\x11")
        until("查看入口 SKILL.md")
        raw.clear()
        send(b"\x1b[B\x1b[B\r")
        until("包内相对路径")
        send("../SKILL.md\r")
        until("unsafe")
        # The warning can render before the selection regains focus. Wait for
        # the menu to return before dismissing; /exit must reach the composer.
        until("查看入口 SKILL.md")
        send(b"\x1b")
        until("PAVEWAY ", lambda: "查看入口 SKILL.md" not in "\n".join(screen.display))
        self.assertEqual(customization.show(self.config, "native-test")["settings"], settings)
        skill_packages.verify(self.config, record["package"]["digest"])
        self.assertEqual(self.server.requests, [])
        exit_ui()

    def test_local_multi_skill_zip_select_all_then_cancel_writes_nothing(self):
        from multi_skill_fixture import write_eight_skills
        path = self.root / "Paveway-missing-skills.zip"
        write_eight_skills(path)
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway skills\r"); until("Import standard")
        send(b"\x1b[B\r"); until("本地 Skill 文件路径")
        send(str(path) + "\r"); until("本地直导")
        raw.clear(); send(b"\x1b[B" * 8 + b"\r"); until("8/8")
        send(b"\x1b")
        for _ in range(3): pump(.05)
        self.assertEqual(customization.show(self.config, "native-test")["settings"]["revision"], 0)
        self.assertEqual(self.server.requests, [])
        exit_ui()

    def test_memory_context_explicit_off_and_on_changes_next_model_request(self):
        manifest = self.config.workflow_root / "native-test/workflow.toml"
        manifest.write_text(manifest.read_text().replace("memory_enabled = false", "memory_enabled = true"))
        request(self.config.server.socket_path, "memory.put", {"workflow_id": "native-test", "task_id": "training",
            "key": "bypass-fixture", "title": "Fixture", "content": "MEMORY_EXPLICIT_BYPASS_PROBE"})
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway-memory-context off\r")
        until('"bypass":true')
        send("NO_MEMORY_TURN\r")
        until("M0_REPLY", lambda: bool(self.prompt_requests("NO_MEMORY_TURN")))
        self.assertNotIn("MEMORY_EXPLICIT_BYPASS_PROBE", json.dumps(self.prompt_requests("NO_MEMORY_TURN")[-1]["messages"]))
        until("M0_REPLY", lambda: request(self.config.server.socket_path, "frontend.list", {"workflow_id": "native-test"})[0]["status"] == "idle")
        send("/paveway-memory-context on\r")
        until('"bypass":false')
        send("WITH_MEMORY_TURN\r")
        until("M0_REPLY", lambda: bool(self.prompt_requests("WITH_MEMORY_TURN")))
        self.assertIn("MEMORY_EXPLICIT_BYPASS_PROBE", json.dumps(self.prompt_requests("WITH_MEMORY_TURN")[-1]["messages"]))
        until("M0_REPLY", lambda: request(self.config.server.socket_path, "frontend.list", {"workflow_id": "native-test"})[0]["status"] == "idle")
        exit_ui()
        self.assertEqual(request(self.config.server.socket_path, "memory.show", {"workflow_id": "native-test", "task_id": "training", "key": "bypass-fixture"})["version"], 1)

    def open_ui(self, launcher=False, recover=False, *, columns=120, screen=None, args=(),
                workflow="native-test", ready=None):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, columns, 0, 0))
        stream = pyte.ByteStream(screen) if screen is not None else None
        argv = [sys.executable, "-m", "oicoach_runtime", "native"]
        if launcher:
            argv = [str(Path(__file__).resolve().parents[1] / "paveway.sh")]
        scope = ["--workflow", workflow] if workflow else []
        process = subprocess.Popen([*argv, "--config", str(self.path), *scope, *args],
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
                start = max(0, len(raw) - 3)
                raw.extend(chunk)
                if stream is not None:
                    stream.feed(chunk)
                if raw.find(b"\x1b[6n", start) != -1:
                    os.write(master, b"\x1b[1;1R")
        def until(text, predicate=lambda: True):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if text.encode() in raw and predicate(): return
                pump()
            self.fail("Native management did not reach state: " + raw.decode(errors="replace")[-3000:])
        def frontend_rows():
            try:
                return request(self.config.server.socket_path, "frontend.list")
            except RuntimeContractError:
                return []
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
        if recover:
            until("Session recovery")
            send(b" \t\r")
        if ready is None:
            ready = lambda rows: any(row["workflow_id"] == workflow and row["status"] == "idle"
                                     and row.get("session_id") for row in rows)
        until("PAVEWAY" if columns >= 100 else "", lambda: ready(frontend_rows()))
        if columns < 100:
            pump(.15)
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
        until("persona")  # A menu row, not the Paveway heading from the welcome banner.
        # Existing native menu order: prompt through vjudge, then settings.
        send(b"\x1b[B" * 10)
        until("settings")
        send("\r")
        rendered = until("Thinking preference")
        self.assertIn("Tijian MCP", rendered)
        self.assertIn("Model preference", rendered)
        send(b"\x1b")
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
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
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
        exit_ui()
        self.assert_no_settings_chat()

    def test_settings_accepts_a_future_thinking_level_and_can_reset_it(self):
        screen, raw, send, until, pump, exit_ui = self.open_settings_ui()
        send("/paveway settings\r")
        until("Thinking preference")
        send(b"\x1b[B\x1b[B\r")
        until("Enter a level manually")
        # The fixture's non-reasoning model has no catalog efforts.
        send(b"\x1b[B\r")
        until("OMP thinking level")
        send("ultra-v2\r")
        until("Paveway / Settings", lambda: "ultra-v2" in "\n".join(screen.display))
        self.assertEqual(customization.show(self.config, "native-test")["settings"]["inference"]["thinking"], "ultra-v2")
        send("\r")
        until("inherit (Runtime / OMP default)")
        send("\r")
        until("Paveway / Settings", lambda: any("Thinking preference" in row and "inherit" in row
              for row in screen.display) and customization.show(self.config, "native-test")["settings"]["revision"] == 2)
        self.assertEqual(customization.show(self.config, "native-test")["settings"]["inference"]["thinking"], "")
        send(b"\x1b")
        until("", lambda: "Paveway / Settings" not in "\n".join(screen.display))
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
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
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
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
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
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
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
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
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
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
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
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
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
        until("", lambda: "Thinking preference" not in "\n".join(screen.display))
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

    def test_shutdown_on_exit_closes_a_launcher_created_runtime(self):
        # No pre-existing daemon: the one-key launcher creates and owns it.
        self.stop_daemon()
        process, raw, send, until, pump, exit_ui = self.open_ui(
            launcher=True, args=("--shutdown-on-exit",))
        self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")
        exit_ui()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and probe_daemon(self.config.server.socket_path) is not None:
            time.sleep(0.05)
        self.assertIsNone(probe_daemon(self.config.server.socket_path), raw.decode(errors="replace")[-2000:])

    def test_due_inbox_notice_preserves_draft_and_does_not_ack_or_replay(self):
        def response(body):
            background = "UI_NOTICE_FIXTURE" in json.dumps(body["messages"][-1])
            return [({"role": "assistant", "content": "BACKGROUND_UI_REPLY_SENTINEL" if background else "M0_REPLY"}, "stop")]
        self.server.response_factory = response
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/wakeup in 1s UI_NOTICE_FIXTURE\r")
        until("native_wakeup")
        draft = "UNSENT_DRAFT_MUST_SURVIVE_NOTICE"
        send(draft)
        until("新的唤醒消息", lambda: len(request(self.config.server.socket_path, "inbox.list", {"workflow_id": "native-test"})["messages"]) >= 1)
        until("BACKGROUND_UI_REPLY_SENTINEL")
        self.assertIn("GPT · 后台定时任务回复".encode(), raw)
        self.assertFalse(any(draft in json.dumps(body["messages"]) for body in self.server.requests))
        send("\r")
        until("M0_REPLY", lambda: any(draft in json.dumps(body["messages"]) for body in self.server.requests) and
            request(self.config.server.socket_path, "frontend.list")[0]["status"] == "idle")
        self.assertFalse(any("新的唤醒消息" in json.dumps(body["messages"], ensure_ascii=False) for body in self.server.requests))
        main = [body for body in self.server.requests if self.is_prompt_request(body, draft)]
        self.assertTrue(main)
        self.assertTrue(all("BACKGROUND_UI_REPLY_SENTINEL" not in json.dumps(body["messages"]) for body in main))
        send("/inbox\r")
        until("Paveway Inbox")
        send("\r")
        until("Acknowledge this message?")
        send(b"\x1b")
        for _ in range(3): pump(.1)
        exit_ui()
        rows = request(self.config.server.socket_path, "inbox.list", {"workflow_id": "native-test"})["messages"]
        self.assertEqual({row["kind"] for row in rows}, {"result"})
        self.assertTrue(all(row["state"] != "acknowledged" for row in rows))
        process, raw, send, until, pump, exit_ui = self.open_ui()
        for _ in range(3): pump(.1)
        self.assertNotIn("新的唤醒消息".encode(), raw)
        self.assertNotIn(b"BACKGROUND_UI_REPLY_SENTINEL", raw)
        exit_ui()

    def test_desktop_notification_failure_is_visible_without_a_model_turn(self):
        from oicoach_runtime.notifications import configure
        configure(self.config, True, backend="linux")
        binary = self.root / "bin/notify-send"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\necho org.freedesktop.Notifications.Rejected >&2\nexit 2\n")
        binary.chmod(0o700)
        probe = binary.with_name("gdbus")
        probe.write_text("#!/bin/sh\necho '(true,)'\n")
        probe.chmod(0o700)
        self.env["PATH"] = str(binary.parent) + os.pathsep + self.env["PATH"]
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway notifications\r")
        until("定时任务桌面弹窗")
        send(b"\x1b[B\r")
        until("系统未接受桌面通知")
        self.assertEqual(self.server.requests, [])
        exit_ui()

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

    def skill_import_fixture(self, response=None):
        path = self.root / "import-source.skill"
        path.write_text("---\nname: original-skill\n---\nPRIVATE_IMPORT_SOURCE_MARKER\nKeep verified facts separate from hypotheses.")
        draft = {"name": "imported-fixture", "description": "Use when reviewing fixture facts", "category": "user-information",
                 "content": "# Imported fixture\nPRIVATE_IMPORTED_BODY\nSeparate facts and hypotheses.",
                 "analysis": {key: "IMPORT_ANALYSIS_" + key.upper() for key in
                    ("purpose", "triggers", "inputs", "workflow", "outputs", "constraints", "dependencies", "warnings")}}
        def factory(body):
            if "PRIVATE_IMPORT_SOURCE_MARKER" in json.dumps(body.get("messages")):
                return [({"role": "assistant", "content": response if response is not None else json.dumps(draft)}, "stop")]
            return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]
        self.server.response_factory = factory
        return path, draft

    def test_skill_file_import_analyzes_reviews_and_saves_without_chat_history(self):
        path, draft = self.skill_import_fixture()
        path.write_text(path.read_text() + "\n" + "参考资料，用于摘要。\n" * 4000 + "SKILL_IMPORT_SOURCE_END")
        self.assertGreater(path.stat().st_size, 65536)
        original = path.read_bytes()
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway skills\r")
        until("Import from file")
        send(b"\x1b[B\x1b[B\r")
        until("Skill 文件路径")
        send(str(path) + "\r")
        until("Skill 导入草稿", lambda: len(self.server.requests) == 1)
        self.assertEqual(customization.show(self.config, "native-test")["settings"]["skills"], [])
        send("\r")
        until("GPT 分析摘要")
        until("IMPORT_ANALYSIS_PURPOSE")
        raw.clear()
        send(b"\x11")
        until("Skill 导入草稿")
        send(b"\x1b[B" * 5 + b"\r")
        until("保存导入 Skill")
        send("\r")
        until("Saved.", lambda: customization.show(self.config, "native-test")["settings"]["revision"] == 1)
        stored = customization.show(self.config, "native-test")["settings"]["skills"][0]
        self.assertEqual({key: stored[key] for key in ("name", "description", "category", "content")},
                         {key: draft[key] for key in ("name", "description", "category", "content")})
        self.assertTrue(stored["enabled"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.server.requests), 1)
        self.assertFalse(self.server.requests[0].get("tools"))
        self.assertEqual(len(self.server.requests[0]["messages"]), 2)
        self.assertIn("SKILL_IMPORT_SOURCE_END", json.dumps(self.server.requests[0]["messages"]))
        exit_ui()
        for session in (self.config.state_dir / "native").rglob("*.jsonl"):
            self.assertNotIn("PRIVATE_IMPORT_SOURCE_MARKER", session.read_text())
            self.assertNotIn("PRIVATE_IMPORTED_BODY", session.read_text())

    def test_skill_import_invalid_analysis_does_not_save(self):
        path, draft = self.skill_import_fixture(response="not a valid Skill proposal")
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway skills\r")
        until("Import from file")
        send(b"\x1b[B\x1b[B\r")
        until("Skill 文件路径")
        send(str(path) + "\r")
        until("不是有效 JSON")
        self.assertEqual(customization.show(self.config, "native-test")["settings"]["skills"], [])
        self.assertEqual(len(self.server.requests), 1)
        exit_ui()

    def test_skill_import_conflict_keeps_draft_and_preserves_concurrent_settings(self):
        path, draft = self.skill_import_fixture()
        response = self.server.response_factory
        release = threading.Event()
        self.addCleanup(release.set)
        def delayed(body):
            if "PRIVATE_IMPORT_SOURCE_MARKER" in json.dumps(body.get("messages")):
                release.wait(15)
            return response(body)
        self.server.response_factory = delayed
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway skills\r")
        until("Import from file")
        send(b"\x1b[B\x1b[B\r")
        until("Skill 文件路径")
        send(str(path) + "\r")
        until("正在分析 Skill", lambda: len(self.server.requests) == 1)
        customization.update(self.config, "native-test", 0, "prompt", content="CONCURRENT_IMPORT_PROMPT")
        release.set()
        until("Skill 导入草稿")
        send(b"\x1b[B" * 5 + b"\r")
        until("保存导入 Skill")
        send("\r")
        until("Customization changed")
        saved = customization.show(self.config, "native-test")["settings"]
        self.assertEqual(saved["skills"], [])
        self.assertEqual(saved["system_prompt"]["text"], "CONCURRENT_IMPORT_PROMPT")
        send(b"\x1b")
        for _ in range(3): pump(.05)
        raw.clear()
        send("/paveway skills\r")
        until("Review import draft")
        raw.clear()
        send(b"\x1b[B\x1b[B\x1b[B\r")
        until("Skill 导入草稿")
        raw.clear()
        send(b"\x1b[B" * 5 + b"\r")
        until("保存导入 Skill")
        send("\r")
        until("Saved.", lambda: customization.show(self.config, "native-test")["settings"]["revision"] == 2)
        saved = customization.show(self.config, "native-test")["settings"]
        self.assertEqual(saved["skills"][0]["content"], draft["content"])
        self.assertEqual(saved["system_prompt"]["text"], "CONCURRENT_IMPORT_PROMPT")
        self.assertEqual(len(self.server.requests), 1)
        exit_ui()

    def cwd_record(self, conversation_id):
        return request(self.config.server.socket_path, "frontend.show", {"conversation_id": conversation_id})

    def assert_conversation_cwd(self, row, cwd):
        self.assertEqual(row["workspace"], str(self.root / "workspace"))
        self.assertEqual(row["cwd"], str(cwd))
        self.assertEqual(Path(f'/proc/{row["omp_pid"]}/cwd').resolve(strict=True), cwd)

    def switched_cwd_record(self, until, previous, cwd):
        def find():
            return next((row for row in request(self.config.server.socket_path, "frontend.list")
                         if row["conversation_id"] != previous["conversation_id"] and row["cwd"] == str(cwd)
                         and row["status"] == "idle" and row["session_id"]), None)
        until("PAVEWAY", lambda: find() is not None)
        current = find()
        self.assert_conversation_cwd(current, cwd)
        self.assertEqual(self.cwd_record(previous["conversation_id"])["status"], "released")
        self.assertNotEqual(current["omp_pid"], previous["omp_pid"])
        self.assertFalse(Path(f'/proc/{previous["omp_pid"]}/cwd').exists())
        self.assertNotEqual(current["session_id"], previous["session_id"])
        self.assertEqual(current["workflow_id"], previous["workflow_id"])
        self.assertEqual(current["task_id"], previous["task_id"])
        self.assertEqual(current["coach_id"], previous["coach_id"])
        return current

    def test_cwd_dialog_cancels_or_opens_a_new_conversation_with_resumable_history(self):
        if pyte is None:
            self.skipTest("install .[test] to verify the native cwd input and confirmation")
        workspace = self.root / "workspace"
        target = self.root / "checkout with spaces"
        target.mkdir()
        alias = self.root / "checkout alias"
        alias.symlink_to(target, target_is_directory=True)
        other = self.root / "home checkout"
        other.mkdir()
        denied = self.root / "unreadable checkout"
        denied.mkdir(mode=0)
        self.addCleanup(denied.chmod, 0o700)
        manifest = self.config.workflow_root / "native-test/workflow.toml"
        original_manifest = manifest.read_bytes()
        self.env["REDUCED_MOTION"] = "1"
        screen = WideScreen(240, 40)
        process, raw, send, until, pump, exit_ui = self.open_ui(columns=240, screen=screen)
        original, = request(self.config.server.socket_path, "frontend.list")
        self.assert_conversation_cwd(original, workspace)
        self.assertEqual(self.server.requests, [])

        def rendered():
            return "\n".join(screen.display)

        def open_input(current):
            send("/paveway cwd\r")
            until("", lambda: f"Current directory: {current}" in rendered()
                  and f"Workflow workspace: {workspace}" in rendered())

        confirm = "Start a new conversation here?"
        send("/paveway\r")
        until("", lambda: "Paveway" in rendered() and "persona" in rendered())
        send(b"\x1b[B" * 11)
        until("", lambda: "cwd" in rendered())
        send("\r")
        until("", lambda: f"Current directory: {workspace}" in rendered()
              and f"Workflow workspace: {workspace}" in rendered())
        send(b"\x1b")
        until("", lambda: "Workflow workspace" not in rendered())
        argument = '"../checkout alias"'
        for dismiss in (b"\x1b", b"\x1b[B\r"):
            open_input(workspace)
            send(argument + "\r")
            until("", lambda: all(text in rendered() for text in (confirm, "Yes", "No")))
            send(dismiss)
            until("", lambda: confirm not in rendered())
            unchanged, = request(self.config.server.socket_path, "frontend.list")
            self.assertEqual(unchanged["conversation_id"], original["conversation_id"])
            self.assertEqual(unchanged["omp_pid"], original["omp_pid"])
            self.assert_conversation_cwd(unchanged, workspace)

        invalid_paths = [("./missing", "Directory does not exist"), (str(manifest), "Not a directory")]
        if not os.access(denied, os.R_OK | os.X_OK):
            invalid_paths.append((str(denied), "Permission denied for working directory"))
        for path, message in invalid_paths:
            raw.clear()
            send(f"/paveway cwd {path}\r")
            until(message)
        for command in (f"/move {other}", f'!cd "{other}"'):
            raw.clear()
            send(command + "\r")
            until("Use /paveway cwd")
        unchanged, = request(self.config.server.socket_path, "frontend.list")
        self.assertEqual(unchanged["conversation_id"], original["conversation_id"])
        self.assertEqual(unchanged["omp_pid"], original["omp_pid"])
        self.assert_conversation_cwd(unchanged, workspace)
        self.assertEqual(self.server.requests, [])

        send("ORIGINAL_DIRECTORY_TURN\r")
        until("M0_REPLY", lambda: self.prompt_requests("ORIGINAL_DIRECTORY_TURN")
              and self.cwd_record(original["conversation_id"])["status"] == "idle")
        original_history = Path(original["session_file"])
        self.assertIn("ORIGINAL_DIRECTORY_TURN", original_history.read_text())

        open_input(workspace)
        send(argument + "\r")
        until("", lambda: all(text in rendered() for text in (confirm, "Yes", "No", str(target))))
        send("\r")  # Yes is the first option in OMP's native confirmation.
        moved = self.switched_cwd_record(until, original, target)
        saved_original = original_history.read_bytes()
        self.assertIn(b"ORIGINAL_DIRECTORY_TURN", saved_original)
        raw.clear()
        send("MOVED_DIRECTORY_TURN")
        until("MOVED_DIRECTORY_TURN")
        send("\r")
        until("M0_REPLY", lambda: self.prompt_requests("MOVED_DIRECTORY_TURN")
              and self.cwd_record(moved["conversation_id"])["status"] == "idle")
        self.assertNotIn("ORIGINAL_DIRECTORY_TURN", json.dumps(self.prompt_requests("MOVED_DIRECTORY_TURN")[-1]))
        moved_history = Path(moved["session_file"])
        self.assertNotEqual(moved_history, original_history)
        with moved_history.open() as history:
            header = next(row for row in map(json.loads, history) if row["type"] == "session")
            self.assertEqual(header["cwd"], str(target))
        self.assertIn("MOVED_DIRECTORY_TURN", moved_history.read_text())
        self.assertNotIn("ORIGINAL_DIRECTORY_TURN", moved_history.read_text())

        open_input(target)
        send(b"\x1b")
        until("", lambda: "Workflow workspace" not in rendered())
        raw.clear()
        send("/paveway cwd ~/home checkout\r")  # Unquoted spaces are part of the path.
        home = self.switched_cwd_record(until, moved, other)
        self.assertNotIn(confirm.encode(), raw)
        self.assertIsNone(process.poll())
        exit_ui()

        process, raw, send, until, pump, exit_ui = self.open_ui(
            launcher=True, workflow=None, args=("--conversation", moved["conversation_id"]),
            ready=lambda rows: any(row["conversation_id"] == moved["conversation_id"] and row["status"] == "idle"
                                   for row in rows))
        resumed = self.cwd_record(moved["conversation_id"])
        self.assert_conversation_cwd(resumed, target)
        self.assertEqual(resumed["session_id"], moved["session_id"])
        self.assertEqual(resumed["session_file"], moved["session_file"])
        self.assertGreater(resumed["generation"], moved["generation"])
        self.assertEqual(self.cwd_record(home["conversation_id"])["status"], "released")
        send("RESUMED_DIRECTORY_TURN")
        until("RESUMED_DIRECTORY_TURN")
        send("\r")
        until("M0_REPLY", lambda: self.prompt_requests("RESUMED_DIRECTORY_TURN")
              and self.cwd_record(moved["conversation_id"])["status"] == "idle")
        self.assertIn("MOVED_DIRECTORY_TURN", json.dumps(self.prompt_requests("RESUMED_DIRECTORY_TURN")[-1]))
        raw.clear()
        send("/paveway profile\r")
        until("Restart native session")
        send(b"\x1b[B" * 4 + b"\r")
        until(home["conversation_id"])
        send("\r")
        until("", lambda: self.cwd_record(home["conversation_id"])["status"] == "idle")
        reopened = self.cwd_record(home["conversation_id"])
        self.assert_conversation_cwd(reopened, other)
        self.assertEqual(reopened["session_id"], home["session_id"])
        self.assertEqual(self.cwd_record(moved["conversation_id"])["status"], "released")
        exit_ui()
        self.assertIn("RESUMED_DIRECTORY_TURN", moved_history.read_text())
        self.assertEqual(original_history.read_bytes(), saved_original)
        self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_direct_cwd_loads_target_project_context_without_losing_profile_and_inference(self):
        self.config = replace(self.config, agent=replace(self.config.agent, tools_enabled=True, skills_enabled=True))
        self.path.write_text(config_text(self.config))
        profile = coaches.create(self.config, "cwd-profile", "native-test", "RETAINED_CWD_PERSONA")
        workflow = profile["runtime_workflow_id"]
        settings = customization.update(self.config, workflow, profile["revision"], "prompt", content="RETAINED_CWD_PROMPT")
        customization.update(self.config, workflow, settings["settings"]["revision"], "skill-put",
                             name="retained-personal-cwd-skill", description="RETAINED_CWD_PERSONAL_SKILL",
                             content="Fixture personal skill.")
        manifest = self.config.workflow_root / workflow / "workflow.toml"
        original_manifest = manifest.read_bytes()

        workspace = self.root / "workspace"
        target = self.root / "absolute checkout"
        target.mkdir()
        (workspace / "AGENTS.md").write_text("OLD_DIRECTORY_AGENT_INSTRUCTIONS\n")
        (target / "AGENTS.md").write_text("TARGET_DIRECTORY_AGENT_INSTRUCTIONS\n")
        project = target / ".omp"
        project.mkdir()
        (project / "RULES.md").write_text("TARGET_DIRECTORY_STICKY_RULE\n")
        (project / "agents").mkdir()
        (project / "agents/target-cwd-agent.md").write_text(
            "---\nname: target-cwd-agent\ndescription: Target project fixture\n---\nFixture agent.\n")
        extra_skills = self.root / "extra cwd fixture skills"
        for parent, name, marker in (
            (project / "skills", "target-native-skill", "TARGET_DIRECTORY_NATIVE_SKILL"),
            (extra_skills, "target-configured-skill", "TARGET_DIRECTORY_CONFIGURED_SKILL"),
        ):
            path = parent / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {name}\ndescription: {marker}\n---\nFixture skill.\n")
        project_config = project / "config.yml"
        project_settings = json.dumps({"skills": {"customDirectories": [str(extra_skills)]}})
        project_config.write_text(project_settings)

        models = self.root / "agent/models.yml"
        model_config = json.loads(models.read_text())
        registered = model_config["providers"]["paveway-m0"]["models"]
        registered.append({**registered[0], "id": "cwd-reasoning-probe", "name": "cwd-reasoning-probe",
            "reasoning": True, "compat": {"supportsReasoningEffort": True, "thinkingFormat": "openai"}})
        models.write_text(json.dumps(model_config))
        self.env["REDUCED_MOTION"] = "1"

        process, raw, send, until, pump, exit_ui = self.open_ui(workflow=workflow, columns=180)
        original, = request(self.config.server.socket_path, "frontend.list")
        self.assert_conversation_cwd(original, workspace)
        self.assertEqual(original["coach_id"], "cwd-profile")
        send("/switch paveway-m0/cwd-reasoning-probe:medium\r")
        until("Session-only model: paveway-m0/cwd-reasoning-probe")
        send("BEFORE_PROJECT_CWD_TURN\r")
        until("M0_REPLY", lambda: self.prompt_requests("BEFORE_PROJECT_CWD_TURN")
              and self.cwd_record(original["conversation_id"])["status"] == "idle")

        raw.clear()
        send(f"/paveway cwd {target}\r")
        moved = self.switched_cwd_record(until, original, target)
        self.assertNotIn(b"Start a new conversation here?", raw)
        raw.clear()
        # This regular file exists relative to the conversation, not the Workflow.
        send("/paveway cwd .omp/config.yml\r")
        until("Not a directory")
        self.assert_conversation_cwd(self.cwd_record(moved["conversation_id"]), target)
        send("!!test -f .omp/RULES.md && printf '%s%s\\n' TARGET_ TERMINAL_RESULT\r")
        until("TARGET_TERMINAL_RESULT")
        raw.clear()
        send("AFTER_PROJECT_CWD_TURN\r")
        until("M0_REPLY", lambda: self.prompt_requests("AFTER_PROJECT_CWD_TURN")
              and self.cwd_record(moved["conversation_id"])["status"] == "idle")
        before = self.prompt_requests("BEFORE_PROJECT_CWD_TURN")[-1]
        after = self.prompt_requests("AFTER_PROJECT_CWD_TURN")[-1]
        before_context = json.dumps(before["messages"])
        after_context = json.dumps(after["messages"])
        for body in (before, after):
            self.assertEqual(body["model"], "cwd-reasoning-probe")
            self.assertEqual(body.get("reasoning_effort"), "medium")
            self.assertIn("RETAINED_CWD_PERSONA", json.dumps(body["messages"]))
            self.assertIn("RETAINED_CWD_PROMPT", json.dumps(body["messages"]))
            self.assertIn("retained-personal-cwd-skill", json.dumps(body["messages"]))
        self.assertIn("OLD_DIRECTORY_AGENT_INSTRUCTIONS", before_context)
        self.assertNotIn("OLD_DIRECTORY_AGENT_INSTRUCTIONS", after_context)
        for marker in ("TARGET_DIRECTORY_AGENT_INSTRUCTIONS", "TARGET_DIRECTORY_STICKY_RULE",
                       "TARGET_DIRECTORY_NATIVE_SKILL", "TARGET_DIRECTORY_CONFIGURED_SKILL"):
            self.assertNotIn(marker, before_context)
            self.assertIn(marker, after_context)
        self.assertEqual(manifest.read_bytes(), original_manifest)
        self.assertEqual(project_config.read_text(), project_settings)
        raw.clear()
        send("/agents\r")
        until("target-cwd-agent")
        raw.clear()
        send(b"\x1b")
        until("\x1b[?1049l")  # The alternate screen closes without repainting the composer.
        exit_ui()

    def test_cwd_waits_for_foreground_activity_without_interrupting_running_background(self):
        workspace = self.root / "workspace"
        target = self.root / "independent checkout"
        target.mkdir()
        foreground_prompt = "HOLD_FOREGROUND_CWD_TURN"
        background_prompt = "HOLD_BACKGROUND_CWD_TURN"
        foreground_started = threading.Event()
        foreground_release = threading.Event()
        background_started = threading.Event()
        background_release = threading.Event()

        def response(body):
            last = json.dumps(body["messages"][-1])
            if foreground_prompt in last:
                foreground_started.set()
                foreground_release.wait(40)
            elif background_prompt in last:
                background_started.set()
                background_release.wait(40)
            return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]

        self.server.response_factory = response
        process, raw, send, until, pump, exit_ui = self.open_ui()
        self.addCleanup(foreground_release.set)
        self.addCleanup(background_release.set)
        original, = request(self.config.server.socket_path, "frontend.list")
        send(f"/wakeup in 1s {background_prompt}\r")
        until("native_wakeup", background_started.is_set)
        event, = request(self.config.server.socket_path, "events.upcoming",
                         {"workflow_id": "native-test"})

        def background():
            return request(self.config.server.socket_path, "events.show", {"event_id": event["event_id"]})

        running = background()
        self.assertEqual(running["status"], "running")
        send(f"{foreground_prompt}\r")
        until("", lambda: foreground_started.is_set() and
              self.cwd_record(original["conversation_id"])["status"] == "active")
        send(f"/paveway cwd {target}\r")
        until("Wait for the current activity before switching/restarting")
        unchanged, = request(self.config.server.socket_path, "frontend.list")
        self.assertEqual(unchanged["conversation_id"], original["conversation_id"])
        self.assertEqual(unchanged["omp_pid"], original["omp_pid"])
        self.assertEqual(unchanged["status"], "active")
        self.assert_conversation_cwd(unchanged, workspace)
        foreground_release.set()
        until("M0_REPLY", lambda: self.cwd_record(original["conversation_id"])["status"] == "idle")

        send(f"/paveway cwd {target}\r")
        self.switched_cwd_record(until, original, target)
        surviving = background()
        self.assertEqual(surviving["run_id"], running["run_id"])
        self.assertEqual(surviving["status"], "running")
        background_release.set()
        until("", lambda: background()["status"] == "succeeded")
        run = request(self.config.server.socket_path, "runs.show", {"run_id": running["run_id"]})
        directory = self.config.state_dir / "sessions" / run["binding_id"]
        history, = (path for path in directory.glob("*.jsonl") if background_prompt in path.read_text())
        with history.open() as source:
            header = next(row for row in map(json.loads, source) if row["type"] == "session")
            self.assertEqual(header["cwd"], str(workspace))
        exit_ui()

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
        until("", lambda: request(self.config.server.socket_path, "frontend.show",
            {"conversation_id": before["conversation_id"]})["generation"] > before["generation"] and
            request(self.config.server.socket_path, "frontend.show", {"conversation_id": before["conversation_id"]})["status"] == "idle")
        exit_ui()
        self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")
