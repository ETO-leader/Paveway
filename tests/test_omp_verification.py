import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from oicoach_runtime.config import Config, AgentConfig
from oicoach_runtime.errors import AgentError
from oicoach_runtime.smoke import verify_omp
from oicoach_runtime.adapters.omp_rpc import OmpRpc
import json
import time


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    def config(self, root):
        return Config(root / "state", root / "workflows", agent=AgentConfig(
            executable=str(Path(__file__).with_name("fake_omp.py").resolve()),
            startup_timeout_seconds=3, ack_timeout_seconds=2, turn_timeout_seconds=3,
            settle_timeout_seconds=2, graceful_stop_seconds=1))

    async def test_short_stop_budget_force_kills_a_noncooperative_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config = replace(config, agent=replace(config.agent, graceful_stop_seconds=0.05))
            session_dir = root / "sessions"
            session_dir.mkdir()
            adapter = OmpRpc(config)
            try:
                await adapter.start({"workspace": str(root), "session_dir": str(session_dir), "session_file": None}, "stop-test")
                await adapter.ready()
                await adapter.write({"type": "prompt", "id": "stop-test", "message": json.dumps({"payload": {"fake_mode": "hold"}})})
                async with asyncio.timeout(3):
                    while not (root / "fake-entered").exists():
                        await asyncio.sleep(0.01)
                started = time.monotonic()
                await adapter.stop(abort=True)
                self.assertNotEqual(adapter.process.returncode, 0)
                self.assertLess(time.monotonic() - started, 3)
            finally:
                await adapter.stop(abort=True)

    async def test_cancelled_stop_waiter_does_not_cancel_process_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config = replace(config, agent=replace(config.agent, graceful_stop_seconds=0.1))
            session_dir = root / "sessions"
            session_dir.mkdir()
            adapter = OmpRpc(config)
            try:
                await adapter.start({"workspace": str(root), "session_dir": str(session_dir), "session_file": None}, "cancel-stop")
                await adapter.ready()
                await adapter.write({"type": "prompt", "id": "cancel-stop", "message": json.dumps({"payload": {"fake_mode": "hold"}})})
                async with asyncio.timeout(3):
                    while not (root / "fake-entered").exists():
                        await asyncio.sleep(0.01)
                waiter = asyncio.create_task(adapter.stop(abort=True))
                await asyncio.sleep(0.01)
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                await asyncio.wait_for(adapter.stop(abort=True), 3)
                self.assertIsNotNone(adapter.process.returncode)
                self.assertNotEqual(adapter.process.returncode, 0)
                self.assertTrue(adapter.stop_task.done())
                self.assertIsNone(adapter.process_fd)
            finally:
                await adapter.stop(abort=True)

    async def test_transport_only_does_not_invoke_model_or_modify_source_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            before = list(root.iterdir())
            report = await verify_omp(config)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["checks"][-1]["status"], "SKIP")
            self.assertEqual(list(root.iterdir()), before)
            self.assertTrue(config.agent.tools_enabled)

    async def test_full_verification_uses_daemon_resume_and_task_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            report = await verify_omp(self.config(Path(directory)), with_model=True)
            self.assertTrue(report["ok"], report)
            passed = {c["check"] for c in report["checks"] if c["status"] == "PASS"}
            self.assertTrue({"same_task_resume", "task_isolation"} <= passed)

    async def test_missing_executable_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            config = replace(config, agent=replace(config.agent, executable="/missing/omp"))
            report = await verify_omp(config)
            self.assertFalse(report["ok"])
            self.assertNotIn("Traceback", str(report))


    async def test_guard_first_and_rpc_first_orders_succeed_and_read_state(self):
        for mode in ("guard-first", "rpc-first", "intermediate-frames", "delayed-guard"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "fake-mode").write_text(mode)
                config = self.config(root)
                session_dir = root / "sessions"
                session_dir.mkdir()
                adapter = OmpRpc(config)
                try:
                    await adapter.start({"workspace": str(root), "session_dir": str(session_dir), "session_file": None}, f"test-{mode}")
                    await adapter.ready()
                    state = await adapter.state()
                    self.assertTrue(state.get("isSettled"))
                finally:
                    await adapter.stop(abort=True)

    async def test_native_ready_does_not_require_retired_budget_guard(self):
        for mode in ("rpc-ready-only", "guard-invalid-limit"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "fake-mode").write_text(mode)
                config = self.config(root)
                session_dir = root / "sessions"
                session_dir.mkdir()
                adapter = OmpRpc(config)
                try:
                    await adapter.start({"workspace": str(root), "session_dir": str(session_dir), "session_file": None}, "native-ready")
                    await adapter.ready()
                    self.assertTrue((await adapter.state()).get("isSettled"))
                finally:
                    await adapter.stop(abort=True)

    async def test_missing_rpc_ready_fails_with_agent_rpc_ready_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fake-mode").write_text("guard-ready-only")
            config = self.config(root)
            config = replace(config, agent=replace(config.agent, startup_timeout_seconds=0.3))
            session_dir = root / "sessions"
            session_dir.mkdir()
            adapter = OmpRpc(config)
            try:
                await adapter.start({"workspace": str(root), "session_dir": str(session_dir), "session_file": None}, "test-missing-rpc")
                with self.assertRaises(AgentError) as caught:
                    await adapter.ready()
                self.assertEqual(caught.exception.code, "agent_rpc_ready_missing")
            finally:
                await adapter.stop(abort=True)

    async def test_unsupported_rpc_protocol_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fake-mode").write_text("unsupported-protocol")
            config = self.config(root)
            session_dir = root / "sessions"
            session_dir.mkdir()
            adapter = OmpRpc(config)
            try:
                await adapter.start({"workspace": str(root), "session_dir": str(session_dir), "session_file": None}, "test-bad-proto")
                with self.assertRaises(AgentError) as caught:
                    await adapter.ready()
                self.assertEqual(caught.exception.code, "agent_protocol_version_unsupported")
            finally:
                await adapter.stop(abort=True)

    async def test_verify_omp_diagnostics_report_safe_categories(self):
        for mode, expected_code in (
            ("guard-ready-only", "agent_rpc_ready_missing"),
            ("unsupported-protocol", "agent_protocol_version_unsupported"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = self.config(root)
                config = replace(config, agent=replace(config.agent, startup_timeout_seconds=0.3))
                with patch.dict(os.environ, {"OICOACH_FAKE_OMP_MODE": mode}):
                    report = await verify_omp(config)
                self.assertFalse(report["ok"])
                self.assertIn(f"[{expected_code}]", report["checks"][0]["detail"])

@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "Set OICOACH_TEST_REAL_OMP_CONFIG for an opt-in real transport test")
class RealTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_omp_transport(self):
        from oicoach_runtime.config import load_config
        report = await verify_omp(load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"])))
        self.assertTrue(report["ok"], report)


if __name__ == "__main__":
    unittest.main()
