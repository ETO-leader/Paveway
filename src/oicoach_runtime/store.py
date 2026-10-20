"""Transactional queue. All mutations and their audit records commit together."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ConflictError, NotFoundError, RuntimeContractError, StorageError
from .models import EventStatus, parse_timestamp, session_key, utc_text, validate_id
from .supervisor import RestartPolicy


def now_utc():
    return datetime.now(timezone.utc)


def bounded_text(value, limit, name):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or "\0" in value:
        raise RuntimeContractError(f"Invalid {name}")
    return value


def canonical_json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        encoded.encode("utf-8")
        return encoded
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise RuntimeContractError("Payload must be finite JSON data") from None


class Store:
    def __init__(self, config, clock=now_utc, policy=None):
        self.config = config
        self.clock = clock
        self.policy = policy or RestartPolicy(config.restart)
        self.connection = sqlite3.connect(config.database_path.as_uri() + "?mode=rw", uri=True,
                                          isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA synchronous=FULL")
        if self.connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            self.close()
            raise StorageError("Initialize the database in WAL mode first")

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def timestamp(self):
        return utc_text(self.clock())

    def audit(self, db, event_id, run_id, transition, error=None):
        db.execute("INSERT INTO audit_log(timestamp_utc,component,event_id,run_id,transition,error_code) VALUES (?,?,?,?,?,?)",
                   (self.timestamp(), "store", event_id, run_id, transition, error))

    def metadata(self, key, default=None):
        row = self.connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            raise StorageError("Invalid runtime metadata") from None

    @staticmethod
    def _set_metadata(db, key, value):
        db.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, canonical_json(value)))

    def supervisor_state(self):
        return self.policy.validate(self.metadata("supervisor_v1", self.policy.empty()))

    def supervisor_failure(self, error):
        with self.transaction() as db:
            self._set_metadata(db, "supervisor_v1", self.policy.failure(self.supervisor_state(), self.clock(), error))
            self.audit(db, None, None, "supervisor_failure", error)

    def reset_supervisor(self):
        with self.transaction() as db:
            self._set_metadata(db, "supervisor_v1", self.policy.empty())
            self.audit(db, None, None, "supervisor_reset")

    def _get(self, table, key, identifier):
        # table/key are internal literals, never client-controlled identifiers.
        validate_id(identifier)
        row = self.connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (identifier,)).fetchone()
        if row is None:
            raise NotFoundError("Requested record does not exist")
        result = dict(row)
        if table == "events":
            result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def event(self, event_id):
        return self._get("events", "event_id", event_id)

    def run(self, run_id):
        return self._get("runs", "run_id", run_id)

    @staticmethod
    def validate_offset(offset):
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise RuntimeContractError("offset must be an integer between 0 and 1000000")

    def events(self, status=None, offset=0):
        self.validate_offset(offset)
        if status is not None and status not in {s.value for s in EventStatus}:
            raise RuntimeContractError("Unknown event status")
        rows = self.connection.execute(
            "SELECT event_id FROM events WHERE (? IS NULL OR status=?) ORDER BY created_at,event_id LIMIT 100 OFFSET ?",
            (status, status, offset)).fetchall()
        return [self.event(row[0]) for row in rows]

    def runs(self, task=None, offset=0):
        self.validate_offset(offset)
        if task is not None:
            validate_id(task)
        rows = self.connection.execute(
            "SELECT r.run_id FROM runs r JOIN events e ON e.event_id=r.event_id WHERE (? IS NULL OR e.task_id=?) ORDER BY r.started_at,r.run_id LIMIT 100 OFFSET ?",
            (task, task, offset)).fetchall()
        return [self.run(row[0]) for row in rows]

    def enqueue(self, workflow_id, task_id, reason, dedupe_key, payload=None, at=None,
                after_turn=None, run_id=None):
        validate_id(workflow_id)
        validate_id(task_id)
        bounded_text(reason, 4096, "reason")
        bounded_text(dedupe_key, 256, "dedupe key")
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise RuntimeContractError("Payload must be a JSON object")
        encoded = canonical_json(payload)
        if len(encoded.encode()) > 65536:
            raise RuntimeContractError("Payload exceeds 64 KiB")
        if (at is None) == (after_turn is None):
            raise RuntimeContractError("Specify exactly one of at or after_turn")
        if at is not None:
            if run_id is not None:
                raise RuntimeContractError("at cannot specify an anchor run")
            due = self.timestamp() if at == "now" else utc_text(parse_timestamp(at))
            mode, status = "at", "queued"
        else:
            if type(after_turn) not in (int, float) or not 0 <= after_turn <= 31_536_000:
                raise RuntimeContractError("after_turn must be 0-31536000 finite seconds")
            validate_id(run_id)
            after_turn = float(after_turn)
            due, mode, status = None, "after_turn", "waiting_anchor"
        fingerprint = hashlib.sha256(canonical_json([reason, payload, mode, "now" if at == "now" else due, after_turn, run_id]).encode()).hexdigest()
        with self.transaction() as db:
            existing = db.execute("SELECT event_id,request_fingerprint FROM events WHERE workflow_id=? AND task_id=? AND dedupe_key=?",
                                  (workflow_id, task_id, dedupe_key)).fetchone()
            if existing:
                if existing[1] != fingerprint:
                    raise ConflictError("Dedupe key already exists with a different request")
                return self.event(existing[0])
            if mode == "after_turn":
                anchor = db.execute("SELECT r.status,e.workflow_id,e.task_id FROM runs r JOIN events e ON e.event_id=r.event_id WHERE r.run_id=?", (run_id,)).fetchone()
                if not anchor or tuple(anchor) != ("running", workflow_id, task_id):
                    raise ConflictError("Anchor must be an active run in the same workflow/task")
            event_id = str(uuid.uuid4())
            stamp = self.timestamp()
            db.execute("""INSERT INTO events(event_id,workflow_id,task_id,kind,reason,payload_json,
                schedule_mode,not_before_utc,relative_seconds,anchor_run_id,status,max_attempts,
                dedupe_key,request_fingerprint,created_at,updated_at)
                VALUES (?,?,?,'wake',?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (event_id, workflow_id, task_id, reason, encoded, mode, due, after_turn, run_id,
                        status, self.config.queue.default_max_attempts, dedupe_key, fingerprint, stamp, stamp))
            self.audit(db, event_id, None, status)
        return self.event(event_id)

    def modify(self, event_id, expected_version, at=None, cancel=False):
        if type(expected_version) is not int or expected_version < 1:
            raise RuntimeContractError("expected_version must be a positive integer")
        due = (self.timestamp() if at == "now" else utc_text(parse_timestamp(at))) if at is not None else None
        with self.transaction() as db:
            event = self.event(event_id)
            if event["version"] != expected_version or event["status"] not in ("queued", "waiting_anchor"):
                raise ConflictError("Event version changed or event is no longer editable")
            stamp = self.timestamp()
            if cancel:
                db.execute("UPDATE events SET status='cancelled',version=version+1,updated_at=?,completed_at=? WHERE event_id=?", (stamp, stamp, event_id))
                transition = "cancelled"
            else:
                if due is None:
                    raise RuntimeContractError("Reschedule requires at")
                db.execute("""UPDATE events SET schedule_mode='at',not_before_utc=?,relative_seconds=NULL,
                    anchor_run_id=NULL,status='queued',version=version+1,updated_at=? WHERE event_id=?""", (due, stamp, event_id))
                transition = "rescheduled"
            self.audit(db, event_id, None, transition)
        return self.event(event_id)

    def claim_due(self, owner):
        stamp = self.timestamp()
        with self.transaction() as db:
            if not self.policy.allowed(self.supervisor_state(), self.clock()):
                return None
            if db.execute("SELECT 1 FROM runs WHERE status IN ('leased','running')").fetchone():
                return None
            row = db.execute("SELECT event_id FROM events WHERE status='queued' AND not_before_utc<=? ORDER BY not_before_utc,created_at,event_id LIMIT 1", (stamp,)).fetchone()
            if not row:
                return None
            event_id, run_id = row[0], str(uuid.uuid4())
            db.execute("INSERT INTO runs(run_id,event_id,request_id,status,started_at) VALUES (?,?,?,'leased',?)", (run_id, event_id, run_id, stamp))
            lease = utc_text(self.clock() + timedelta(seconds=self.config.agent.startup_timeout_seconds + self.config.agent.turn_timeout_seconds + self.config.agent.settle_timeout_seconds + 60))
            db.execute("UPDATE events SET status='leased',run_id=?,attempt=attempt+1,lease_owner=?,lease_expires_utc=?,version=version+1,updated_at=? WHERE event_id=?",
                       (run_id, owner, lease, stamp, event_id))
            self.audit(db, event_id, run_id, "leased")
        return self.event(event_id)

    def bind(self, event, workflow):
        key = session_key(event["workflow_id"], event["task_id"])
        directory = self.config.state_dir / "sessions" / key
        with self.transaction() as db:
            row = db.execute("SELECT * FROM session_bindings WHERE binding_id=?", (key,)).fetchone()
            if row:
                if row["status"] in ("blocked", "uncertain") or row["workspace"] != str(workflow.workspace) or row["workflow_revision"] != workflow.revision:
                    raise ConflictError("Session requires repair or its workspace/workflow revision changed")
                if row["session_file"] and (not directory.is_dir() or not self._session_file(row["session_file"], directory).is_file()):
                    raise ConflictError("Bound session file is missing")
            else:
                from .config import ensure_private_directory
                ensure_private_directory(directory)
                stamp = self.timestamp()
                db.execute("INSERT INTO session_bindings(binding_id,workflow_id,task_id,session_dir,workspace,workflow_revision,status,created_at,updated_at) VALUES (?,?,?,?,?,?,'new',?,?)",
                           (key, event["workflow_id"], event["task_id"], str(directory), str(workflow.workspace), workflow.revision, stamp, stamp))
            db.execute("UPDATE runs SET binding_id=? WHERE run_id=?", (key, event["run_id"]))
            self.audit(db, event["event_id"], event["run_id"], "session_bound")
        return dict(self.connection.execute("SELECT * FROM session_bindings WHERE binding_id=?", (key,)).fetchone())

    @staticmethod
    def _session_file(value, directory):
        if not isinstance(value, str) or not value:
            raise ConflictError("Agent did not report a session file")
        path = Path(value).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ConflictError("Agent session file escaped its assigned directory")
        return path

    def record_session(self, run_id, state):
        run = self.run(run_id)
        binding = self.connection.execute("SELECT * FROM session_bindings WHERE binding_id=?", (run["binding_id"],)).fetchone()
        directory = self.config.state_dir / "sessions" / binding["binding_id"]
        path = self._session_file(state.get("sessionFile"), directory)
        if binding["session_file"] and str(path) != binding["session_file"]:
            raise ConflictError("Agent resumed a different session")
        session_id = state.get("sessionId")
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
            raise ConflictError("Agent did not report a valid session ID")
        if binding["session_id"] and session_id != binding["session_id"]:
            raise ConflictError("Agent resumed a different session ID")
        with self.transaction() as db:
            db.execute("UPDATE session_bindings SET session_file=?,session_id=?,updated_at=? WHERE binding_id=?",
                       (str(path), session_id, self.timestamp(), binding["binding_id"]))
            self.audit(db, run["event_id"], run_id, "session_verified")

    def record_process(self, run_id, pid, identity):
        with self.transaction() as db:
            db.execute("UPDATE runs SET agent_pid=?,agent_start_identity=? WHERE run_id=? AND status='leased'", (pid, identity, run_id))
            self.audit(db, self.run(run_id)["event_id"], run_id, "agent_spawned")

    def dispatch(self, run_id):
        with self.transaction() as db:
            run = self.run(run_id)
            if run["status"] != "leased":
                raise ConflictError("Run cannot be dispatched")
            stamp = self.timestamp()
            db.execute("UPDATE runs SET status='running',dispatch_intent_at=?,possible_side_effects=1 WHERE run_id=?", (stamp, run_id))
            db.execute("UPDATE events SET status='running',version=version+1,updated_at=? WHERE event_id=?", (stamp, run["event_id"]))
            self.audit(db, run["event_id"], run_id, "dispatch_intent")

    def evidence(self, run_id, *, acknowledged=False, result=None):
        with self.transaction() as db:
            if acknowledged:
                db.execute("UPDATE runs SET acknowledged_at=? WHERE run_id=?", (self.timestamp(), run_id))
                self.audit(db, self.run(run_id)["event_id"], run_id, "prompt_acknowledged")
            if result is not None:
                if result not in ("completed", "error", "aborted"):
                    raise RuntimeContractError("Unknown prompt result")
                db.execute("UPDATE runs SET prompt_result_status=? WHERE run_id=?", (result, run_id))
                self.audit(db, self.run(run_id)["event_id"], run_id, "prompt_result_" + result)

    def output(self, run_id):
        run = self.run(run_id)
        saved = self.metadata("output:" + run_id)
        return saved if saved is not None else {"status": "pending" if run["status"] in ("leased", "running") else "unavailable",
                                               "text": None, "truncated": False}

    def finish(self, run_id, status, *, result=None, settled=False, error=None, error_message=None, exit_code=None,
               automatic_retry=False, record_failure=False, output=None):
        if status not in ("succeeded", "failed", "blocked", "uncertain"):
            raise RuntimeContractError("Invalid terminal run status")
        with self.transaction() as db:
            run = self.run(run_id)
            if run["status"] not in ("leased", "running"):
                raise ConflictError("Run already completed")
            if automatic_retry and (run["dispatch_intent_at"] is not None or status != "failed"):
                raise ConflictError("Automatic retry requires a proven pre-dispatch failure")
            if status == "succeeded" and (not settled or result != "completed" or not run["dispatch_intent_at"]
                                           or not run["acknowledged_at"] or run["prompt_result_status"] != "completed"):
                raise ConflictError("Success requires dispatched prompt completion and settled evidence")
            stamp = self.timestamp()
            event = self.event(run["event_id"])
            event_status = ("queued" if event["attempt"] < event["max_attempts"] else "dead_letter") if automatic_retry else status
            if record_failure or automatic_retry:
                self._set_metadata(db, "supervisor_v1", self.policy.failure(self.supervisor_state(), self.clock(), error))
            elif status == "succeeded":
                self._set_metadata(db, "supervisor_v1", self.policy.empty())
            if output is not None and status == "succeeded":
                self._set_metadata(db, "output:" + run_id, output)
            db.execute("UPDATE runs SET status=?,prompt_result_status=COALESCE(?,prompt_result_status),settled_at=?,completed_at=?,error_code=?,error_message=?,agent_exit_code=? WHERE run_id=?",
                       (status, result, stamp if settled else None, stamp, error, error_message, exit_code, run_id))
            db.execute("UPDATE events SET status=?,completed_at=?,updated_at=?,lease_owner=NULL,lease_expires_utc=NULL,last_error_code=?,last_error_message=?,version=version+1 WHERE event_id=?",
                       (event_status, None if event_status == "queued" else stamp, stamp, error, error_message, run["event_id"]))
            if run["binding_id"]:
                binding_status = "ready" if settled else ("uncertain" if run["dispatch_intent_at"] else "blocked")
                if automatic_retry:
                    binding_status = "ready"
                    self._reset_provisional(db, run["binding_id"], event["event_id"], run_id)
                db.execute("UPDATE session_bindings SET status=?,last_successful_run_id=CASE WHEN ?='succeeded' THEN ? ELSE last_successful_run_id END,updated_at=? WHERE binding_id=?",
                           (binding_status, status, run_id, stamp, run["binding_id"]))
            anchors = db.execute("SELECT event_id,relative_seconds FROM events WHERE anchor_run_id=? AND status='waiting_anchor'", (run_id,)).fetchall()
            for anchor in anchors:
                if status == "succeeded":
                    due = utc_text(parse_timestamp(stamp) + timedelta(seconds=anchor["relative_seconds"]))
                    db.execute("UPDATE events SET status='queued',not_before_utc=?,version=version+1,updated_at=? WHERE event_id=?", (due, stamp, anchor["event_id"]))
                    transition = "queued"
                else:
                    db.execute("UPDATE events SET status='blocked',last_error_code='anchor_failed',version=version+1,updated_at=? WHERE event_id=?", (stamp, anchor["event_id"]))
                    transition = "blocked"
                self.audit(db, anchor["event_id"], run_id, transition)
            self.audit(db, run["event_id"], run_id, event_status, error)

    def reconcile(self):
        # Only call after acquiring the runtime lock and confirming no old managed group survives.
        active = self.connection.execute("SELECT run_id,dispatch_intent_at FROM runs WHERE status IN ('leased','running')").fetchall()
        for row in active:
            self.finish(row["run_id"], "uncertain" if row["dispatch_intent_at"] else "failed", error="daemon_interrupted",
                        automatic_retry=not row["dispatch_intent_at"], record_failure=True)
        job_id = self.metadata("last_restart_id")
        if job_id:
            job = self.restart_job(job_id)
            if job["status"] in ("requested", "running"):
                self.update_restart(job_id, "interrupted", error="daemon_interrupted")

    def _reset_provisional(self, db, binding_id, event_id, run_id):
        binding = db.execute("SELECT * FROM session_bindings WHERE binding_id=?", (binding_id,)).fetchone()
        dispatched = db.execute("SELECT 1 FROM runs WHERE binding_id=? AND dispatch_intent_at IS NOT NULL LIMIT 1", (binding_id,)).fetchone()
        if binding["session_file"] and not Path(binding["session_file"]).is_file() and not dispatched:
            db.execute("UPDATE session_bindings SET session_file=NULL,session_id=NULL WHERE binding_id=?", (binding_id,))
            self.audit(db, event_id, run_id, "provisional_session_reset")

    def retry(self, event_id, confirmed=False):
        with self.transaction() as db:
            event = self.event(event_id)
            if event["status"] not in ("failed", "blocked", "uncertain", "dead_letter"):
                raise ConflictError("Event is not recoverable")
            if event["status"] == "uncertain" and not confirmed:
                raise ConflictError("Review external effects and pass --confirm-duplicate-risk")
            if event["not_before_utc"] is None:
                raise ConflictError("An unanchored event cannot be retried; create a new at event")
            if event["attempt"] >= event["max_attempts"]:
                raise ConflictError("Maximum attempts reached; review before creating a new event")
            if event["run_id"]:
                run = self.run(event["run_id"])
                if run["binding_id"]:
                    binding = db.execute("SELECT * FROM session_bindings WHERE binding_id=?", (run["binding_id"],)).fetchone()
                    if binding["status"] == "uncertain" and not confirmed:
                        raise ConflictError("Session is uncertain; review external effects and confirm duplicate risk")
                    self._reset_provisional(db, run["binding_id"], event_id, event["run_id"])
                    db.execute("UPDATE session_bindings SET status='ready',updated_at=? WHERE binding_id=?", (self.timestamp(), run["binding_id"]))
            db.execute("UPDATE events SET status='queued',completed_at=NULL,last_error_code=NULL,version=version+1,updated_at=? WHERE event_id=?", (self.timestamp(), event_id))
            self.audit(db, event_id, event["run_id"], "manual_retry")
        return self.event(event_id)

    def binding_for_task(self, task_id, workflow_id=None):
        validate_id(task_id)
        if workflow_id is not None:
            validate_id(workflow_id)
        rows = self.connection.execute("SELECT * FROM session_bindings WHERE task_id=? AND (? IS NULL OR workflow_id=?)", (task_id, workflow_id, workflow_id)).fetchall()
        if not rows:
            raise NotFoundError("Task has no session binding")
        if len(rows) != 1:
            raise ConflictError("Task ID is ambiguous; specify --workflow")
        return dict(rows[0])

    def request_restart(self, task_id, workflow_id=None, reason="manual restart", reset_circuit=False):
        bounded_text(reason, 4096, "restart reason")
        if type(reset_circuit) is not bool:
            raise RuntimeContractError("reset_circuit must be boolean")
        with self.transaction() as db:
            previous = self.metadata("last_restart_id")
            if previous and self.restart_job(previous)["status"] in ("requested", "running"):
                raise ConflictError("A restart is already pending")
            binding = self.binding_for_task(task_id, workflow_id)
            job_id = str(uuid.uuid4())
            job = {"restart_id": job_id, "workflow_id": binding["workflow_id"], "task_id": task_id,
                   "status": "requested", "reason": reason, "reset_circuit": reset_circuit,
                   "created_at": self.timestamp(), "updated_at": self.timestamp(), "error_code": None,
                   "prompt_replayed": False}
            self._set_metadata(db, "restart:" + job_id, job)
            self._set_metadata(db, "last_restart_id", job_id)
            self.audit(db, None, job_id, "agent_restart_requested")
        return job

    def restart_job(self, job_id):
        validate_id(job_id)
        job = self.metadata("restart:" + job_id)
        if job is None:
            raise NotFoundError("Restart job does not exist")
        return job

    def update_restart(self, job_id, status, error=None):
        if status not in ("running", "succeeded", "failed", "interrupted"):
            raise RuntimeContractError("Invalid restart job status")
        with self.transaction() as db:
            job = self.restart_job(job_id)
            job.update(status=status, error_code=error, updated_at=self.timestamp())
            self._set_metadata(db, "restart:" + job_id, job)
            self.audit(db, None, job_id, "agent_restart_" + status, error)
        return job

    def status(self):
        counts = {status.value: 0 for status in EventStatus}
        counts.update(dict(self.connection.execute("SELECT status,count(*) FROM events GROUP BY status").fetchall()))
        last_success = self.connection.execute("SELECT max(completed_at) FROM runs WHERE status='succeeded'").fetchone()[0]
        recent_error = self.connection.execute("SELECT error_code FROM runs WHERE error_code IS NOT NULL ORDER BY completed_at DESC LIMIT 1").fetchone()
        return {"counts": counts, "active_runs": [dict(r) for r in self.connection.execute("SELECT * FROM runs WHERE status IN ('leased','running')")],
                "supervisor": self.policy.describe(self.supervisor_state(), self.clock()),
                "last_restart": self.restart_job(self.metadata("last_restart_id")) if self.metadata("last_restart_id") else None,
                "last_success_at": last_success, "recent_error_code": recent_error[0] if recent_error else None}
