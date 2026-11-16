"""Store-level capacity, physical-session exclusivity and migration tests."""

import concurrent.futures
from contextlib import closing
from importlib.resources import files
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.db import initialize_database, inspect_database
from oicoach_runtime.errors import StorageError
from oicoach_runtime.models import execution_scope, utc_text
from oicoach_runtime.store import Store


class _MigratingTo:
    """Serve a broken 004 migration after a valid v3 database exists."""

    def __init__(self, text):
        self.text = text

    def joinpath(self, _):
        return self

    def read_text(self, **_):
        return self.text


class StoreConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-store-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.path)
        self.stores = []
        self.addCleanup(self.close)

    def close(self):
        for store in self.stores:
            store.close()
        self.stores.clear()

    def config_for(self, concurrency=1):
        return replace(self.config, server=replace(self.config.server,
                                                   max_concurrent_runs=concurrency))

    def store(self, concurrency=1):
        store = Store(self.config_for(concurrency))
        self.stores.append(store)
        return store

    @staticmethod
    def enqueue(store, task, key):
        return store.enqueue("echo", task, "fixture", key, payload={}, at="now")

    @staticmethod
    def abort(store, event, *, keep=False):
        store.abort_claim(event["run_id"], "fixture_release", keep_event=keep)

    def test_one_slot_keeps_the_previous_serial_contract(self):
        store = self.store(1)
        first = self.enqueue(store, "alpha", "one")
        second = self.enqueue(store, "beta", "two")
        claimed = store.claim_due("worker")
        self.assertEqual(claimed["event_id"], first["event_id"])
        self.assertEqual(claimed["execution_scope"], execution_scope("echo", "alpha"))
        self.assertIsNone(store.claim_due("worker"), "a second Run was leased with one slot")
        self.abort(store, claimed)
        claimed = store.claim_due("worker")
        self.assertEqual(claimed["event_id"], second["event_id"])

    def test_independent_scopes_concurrently_share_capacity_and_same_scope_excludes(self):
        store = self.store(2)
        alpha = self.enqueue(store, "alpha", "one")
        alpha_again = self.enqueue(store, "alpha", "two")
        beta = self.enqueue(store, "beta", "three")
        first = store.claim_due("worker")
        second = store.claim_due("worker")
        self.assertEqual(first["event_id"], alpha["event_id"])
        self.assertEqual(second["event_id"], beta["event_id"],
                         "the scheduler did not skip the blocked head-of-line event")
        self.assertEqual(store.event(alpha_again["event_id"])["status"], "queued")
        self.assertIsNone(store.claim_due("worker"), "capacity was exceeded")
        self.abort(store, first)
        third = store.claim_due("worker")
        self.assertEqual(third["event_id"], alpha_again["event_id"])

    def test_concurrent_claims_never_exceed_capacity_or_duplicate_a_scope(self):
        store = self.store(2)
        for index in range(6):
            self.enqueue(store, f"task{index}", f"key{index}")
        store.close()
        self.stores.clear()

        config = self.config_for(2)

        def claim(worker):
            # A Store is thread-affine: open and close it inside the worker.
            connection = Store(config)
            try:
                return connection.claim_due(worker)
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = [future.result() for future in
                       [executor.submit(claim, f"worker{index}") for index in range(4)]]
        claimed = [event for event in results if event]
        self.assertEqual(len(claimed), 2)
        self.assertEqual(len({event["execution_scope"] for event in claimed}), 2)
        active = self.store(2)
        self.assertEqual(active.connection.execute(
            "SELECT count(*) FROM runs WHERE status IN ('leased','running')").fetchone()[0], 2)

    def test_terminal_and_waiting_events_do_not_hold_a_slot(self):
        store = self.store(2)
        anchor = self.enqueue(store, "alpha", "anchor")
        claimed = store.claim_due("worker")
        self.assertEqual(claimed["event_id"], anchor["event_id"])
        store.bind(claimed, type("Workflow", (), {"workspace": self.root, "revision": "1"})())
        store.dispatch(claimed["run_id"])
        waiting = store.enqueue("echo", "alpha", "anchored", "anchored", payload={},
                                after_turn=0, run_id=claimed["run_id"])
        self.assertEqual(waiting["status"], "waiting_anchor")
        # The waiting event is never leased; the free slot goes to a due event.
        other = self.enqueue(store, "gamma", "other")
        second = store.claim_due("worker")
        self.assertEqual(second["event_id"], other["event_id"])
        self.assertIsNone(store.claim_due("worker"))
        self.assertEqual(store.event(waiting["event_id"])["status"], "waiting_anchor")
        self.assertEqual(store.connection.execute(
            "SELECT count(*) FROM runs WHERE status IN ('leased','running')").fetchone()[0], 2)

    def test_reconcile_settles_every_active_run(self):
        store = self.store(2)
        dispatched = self.enqueue(store, "alpha", "one")
        leased = self.enqueue(store, "beta", "two")
        first = store.claim_due("worker")
        second = store.claim_due("worker")
        store.bind(first, type("Workflow", (), {"workspace": self.root, "revision": "1"})())
        store.dispatch(first["run_id"])
        store.reconcile()
        self.assertEqual(store.run(first["run_id"])["status"], "uncertain")
        self.assertIn(store.run(second["run_id"])["status"], ("failed", "blocked"))
        self.assertEqual(store.connection.execute(
            "SELECT count(*) FROM runs WHERE status IN ('leased','running')").fetchone()[0], 0)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-migrate-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "runtime.sqlite3"

    def legacy_database(self, *, active=True):
        """Build a real schema-v3 database with history and one active Run."""
        with closing(sqlite3.connect(self.database)) as connection:
            for name in ("001_initial.sql", "002_memory.sql", "003_wakebinding.sql"):
                connection.executescript(
                    files("oicoach_runtime").joinpath("migrations/" + name).read_text())
            connection.execute("INSERT INTO runtime_metadata VALUES ('keep','\"value\"')")
            connection.execute("""INSERT INTO events
                (event_id,workflow_id,task_id,kind,reason,payload_json,schedule_mode,not_before_utc,
                 status,max_attempts,dedupe_key,request_fingerprint,created_at,updated_at)
                VALUES ('old-event','coach','training','wake','preserve','{}','at',
                 '2026-10-19T00:00:00.000000Z','leased',3,'old','fingerprint',
                 '2026-10-18T00:00:00.000000Z','2026-10-18T00:00:00.000000Z')""")
            connection.execute("""INSERT INTO session_bindings
                (binding_id,workflow_id,task_id,session_dir,workspace,workflow_revision,status,
                 created_at,updated_at)
                VALUES ('old-binding','coach','training','/private/session','/private/workspace','1',
                 'ready','2026-10-18T00:00:00.000000Z','2026-10-18T00:00:00.000000Z')""")
            status = "leased" if active else "failed"
            connection.execute("""INSERT INTO runs
                (run_id,event_id,binding_id,request_id,status,started_at)
                VALUES ('old-run','old-event','old-binding','old-request',?,
                 '2026-10-18T00:00:00.000000Z')""", (status,))
            connection.execute("""INSERT INTO wake_bindings
                (binding_id,workflow_id,task_id,todo_source_id,todo_source_label,todo_id,name,
                 mode,time_source,reminder_at_utc,eligibility,resume_policy,status,event_id,
                 created_at,updated_at)
                VALUES ('old-binding','coach','training',?,'tijian:tijian','todo-1','default',
                 'independent','due','2026-10-19T00:00:00.000000Z','open','manual','active',
                 'old-event','2026-10-18T00:00:00.000000Z','2026-10-18T00:00:00.000000Z')""",
                             ("a" * 64,))
            connection.execute("PRAGMA user_version=3")
            connection.commit()
        self.database.chmod(0o600)

    def test_v3_database_upgrades_in_place_and_keeps_every_row(self):
        self.legacy_database()
        with closing(sqlite3.connect(self.database)) as connection:
            before = {}
            for table in ("events", "runs", "session_bindings", "runtime_metadata",
                          "wake_bindings", "audit_log"):
                columns = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
                before[table] = (columns, connection.execute(f"SELECT * FROM {table}").fetchall())
        report = initialize_database(self.database)
        self.assertEqual(report["schema_version"], 5)
        with closing(sqlite3.connect(self.database)) as connection:
            for table, (columns, rows) in before.items():
                projection = ",".join(columns)
                self.assertEqual(
                    connection.execute(f"SELECT {projection} FROM {table}").fetchall(),
                    rows, table)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='one_active_run'").fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='runs_active_scope'").fetchone())
            self.assertEqual(connection.execute(
                "SELECT execution_scope FROM runs WHERE run_id='old-run'").fetchone()[0],
                execution_scope("coach", "training"))
            binding = connection.execute("SELECT * FROM wake_bindings").fetchone()
            self.assertIsNotNone(binding)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(wake_bindings)")}
            self.assertTrue({"last_failure_at", "last_failure_code", "failure_count",
                             "next_attempt_at"} <= columns)
            self.assertEqual(connection.execute(
                "SELECT failure_count,next_attempt_at FROM wake_bindings").fetchone(), (0, None))
            # The backfilled scope now excludes a second active Run.
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""INSERT INTO runs(run_id,event_id,request_id,status,started_at,
                    execution_scope) VALUES ('new-run','old-event','new-request','leased','now',
                    'coach' || char(31) || 'training')""")
        self.assertEqual(inspect_database(self.database)["integrity"], "ok")

    def test_failed_migration_rolls_back_new_constraints(self):
        self.legacy_database(active=False)
        broken = "ALTER TABLE runs ADD COLUMN execution_scope TEXT;\nINVALID SQL;\n"
        with patch("oicoach_runtime.db.files", return_value=_MigratingTo(broken)), \
                self.assertRaises(StorageError):
            initialize_database(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            self.assertNotIn("execution_scope", columns)
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='one_active_run'").fetchone())
        self.assertEqual(initialize_database(self.database)["schema_version"], 5)


if __name__ == "__main__":
    unittest.main()
