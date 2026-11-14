"""Persistent message projection. Offering a message never completes its Run."""

import hashlib

from .errors import ConflictError, NotFoundError, RuntimeContractError
from .store import canonical_json

PREFIX = "delivery:v1:"


def occurrence_id(event):
    from .models import parse_timestamp, utc_text
    return hashlib.sha256(canonical_json([event["event_id"], utc_text(parse_timestamp(event["not_before_utc"]))]).encode()).hexdigest()


class Inbox:
    def __init__(self, store):
        self.store = store

    def create(self, event, kind, text, run_id=None):
        with self.store.transaction() as db:
            return self._create(db, event, kind, text, run_id)

    def _create(self, db, event, kind, text, run_id=None):
        legacy_key = hashlib.sha256(canonical_json([event["event_id"], kind, run_id]).encode()).hexdigest()
        key = occurrence_id(event) if kind == "reminder" else legacy_key
        if kind == "reminder":
            alias = self.store.metadata("delivery-occurrence:v1:" + key)
            if alias:
                return self.store.metadata(PREFIX + alias)
            legacy = self.store.metadata(PREFIX + legacy_key)
            if legacy and not legacy.get("not_before_utc"):
                # Legacy Inbox rows lacked a due time. Desktop evidence can
                # identify it exactly; otherwise only an unchanged schedule can.
                desktop = self.store.metadata("desktop-attempt:v1:" + event["event_id"])
                if desktop and desktop.get("delivery_id") == legacy_key:
                    legacy["not_before_utc"] = desktop["not_before_utc"]
                elif not db.execute("SELECT 1 FROM audit_log WHERE event_id=? AND transition='rescheduled' AND timestamp_utc>=?",
                                    (event["event_id"], legacy["created_at"])).fetchone():
                    legacy["not_before_utc"] = event["not_before_utc"]
                if legacy.get("not_before_utc"):
                    self.store._set_metadata(db, PREFIX + legacy_key, legacy)
            if legacy and legacy.get("not_before_utc") == event["not_before_utc"]:
                legacy["occurrence_id"] = key
                self.store._set_metadata(db, PREFIX + legacy_key, legacy)
                self.store._set_metadata(db, "delivery-occurrence:v1:" + key, legacy_key)
                return legacy
        existing = self.store.metadata(PREFIX + key)
        if existing:
            return existing
        record = {"delivery_id": key, "event_id": event["event_id"], "run_id": run_id,
            "workflow_id": event["workflow_id"], "task_id": event["task_id"],
            "conversation_id": event["payload"].get("conversation_id"), "kind": kind,
            "text": text, "state": "created", "version": 1, "created_at": self.store.timestamp()}
        if kind == "reminder":
            record.update(occurrence_id=key, not_before_utc=event["not_before_utc"])
        self.store._set_metadata(db, PREFIX + key, record)
        return record

    def due(self, event, *, db=None):
        if event["payload"].get("native_wakeup") and event["payload"].get("intent") == "reminder":
            value = event["payload"].get("message")
            text = value if isinstance(value, str) and value else event["reason"]
            binding_id = event["payload"].get("wake_binding_id")
            if binding_id:
                try:
                    binding = self.store.wakebinding(binding_id)
                except Exception:
                    binding = None
                if binding is not None:
                    title = binding.get("todo_title") or binding.get("todo_id")
                    text = f"Tijian Todo：{title}\n计划提醒：{event['not_before_utc']}"
            if db is None:
                with self.store.transaction() as db:
                    return self.due(event, db=db)
            delivery = self._create(db, event, "reminder", text)
            self.store._set_metadata(db, "inbox-due:v1:" + event["event_id"], event["not_before_utc"])
            return delivery

    def project_due(self, *, complete=False):
        rows = self.store.connection.execute("""SELECT event_id FROM events e
            WHERE json_extract(payload_json,'$.native_wakeup')=1 AND json_extract(payload_json,'$.intent')='reminder'
            AND json_extract(payload_json,'$.wake_binding_id') IS NULL
            AND status!='cancelled' AND not_before_utc<=?
            AND ((? AND status='queued') OR NOT EXISTS(SELECT 1 FROM runtime_metadata m WHERE m.key='inbox-due:v1:' || e.event_id
                AND json_extract(m.value,'$')=e.not_before_utc))
            ORDER BY not_before_utc,event_id LIMIT 100""", (self.store.timestamp(), complete)).fetchall()
        for row in rows:
            with self.store.transaction() as db:
                event = self.store.event(row[0])
                if event["status"] != "cancelled" and event["not_before_utc"] <= self.store.timestamp():
                    self.due(event, db=db)
                    if complete:
                        self.store.complete_reminder(db, event)

    def bound_due(self, *, limit=20, retry_after=15):
        """Due Tijian-bound reminders that still need a gated Inbox projection.

        The binding's last_attempt_at provides bounded backoff for Tijian
        outages, so a repeated reconciliation pass cannot hammer the MCP.
        """
        from datetime import timedelta
        from .models import utc_text
        cutoff = utc_text(self.store.clock() - timedelta(seconds=retry_after))
        rows = self.store.connection.execute("""SELECT e.event_id FROM events e
            JOIN wake_bindings b ON b.event_id=e.event_id
            WHERE json_extract(e.payload_json,'$.native_wakeup')=1
            AND json_extract(e.payload_json,'$.intent')='reminder'
            AND json_extract(e.payload_json,'$.wake_binding_id') IS NOT NULL
            AND e.status='queued' AND e.not_before_utc<=?
            AND b.status IN ('active','pending')
            AND (b.last_error_code IS NULL OR b.last_attempt_at IS NULL OR b.last_attempt_at<=?)
            ORDER BY e.not_before_utc,e.created_at,e.event_id LIMIT ?""",
            (self.store.timestamp(), cutoff, limit)).fetchall()
        return [self.store.event(row[0]) for row in rows]

    async def project_bound_due(self, service, *, limit=20):
        """Gate, then atomically project and settle each due bound reminder.

        Tijian remains authoritative: no Inbox row or event settlement happens
        before the delivery gate re-reads the Todo. This never creates a Run.
        """
        delivered = []
        for event in self.bound_due(limit=limit):
            try:
                allowed = await service.gate(event)
            except ConflictError:
                # A CAS/source conflict only defers this occurrence. Re-read
                # happens next pass; the binding backoff prevents a hot loop.
                service.defer(event["payload"]["wake_binding_id"], "binding_verify_conflict")
                continue
            except RuntimeContractError:
                continue
            if not allowed:
                continue
            with self.store.transaction() as db:
                current = self.store.event(event["event_id"])
                if (current["status"] != "queued"
                        or current["not_before_utc"] > self.store.timestamp()
                        or not service.local_gate(current)):
                    continue
                delivery = self.due(current, db=db)
                self.store.complete_reminder(db, current)
                delivered.append(delivery["delivery_id"])
        return delivered

    def collect(self):
        # Project terminal evidence only; this does not start or replay a prompt.
        rows = self.store.connection.execute("""SELECT r.run_id,r.event_id FROM runs r JOIN events e ON r.event_id=e.event_id
            WHERE r.status IN ('succeeded','failed','blocked','uncertain')
            AND json_extract(e.payload_json,'$.native_wakeup')=1
            AND NOT EXISTS (SELECT 1 FROM runtime_metadata m WHERE m.key='inbox-projected:v1:' || r.run_id)""").fetchall()
        for row in rows:
            event = self.store.event(row["event_id"])
            output = self.store.output(row["run_id"])
            run = self.store.run(row["run_id"])
            text = output.get("text") or ""
            quiet = event["payload"].get("intent") == "agent_wake" and run["status"] == "succeeded" and not text.strip()
            if not quiet:
                message = text or ("GPT execution status: " + run["status"] + "; no saved final reply")
                self.create(event, "result", message, row["run_id"])
            with self.store.transaction() as db:
                self.store._set_metadata(db, "inbox-projected:v1:" + row["run_id"], True)

    def list(self, workflow_id, task_id=None, offset=0):
        from .memory import scope, clip
        import json
        scope(workflow_id, task_id)
        self.store.validate_offset(offset)
        self.collect()
        rows = [json.loads(row[0]) for row in self.store.connection.execute(
            "SELECT value FROM runtime_metadata WHERE key LIKE ?", (PREFIX + "%",)).fetchall()]
        rows = sorted((row for row in rows if row["workflow_id"] == workflow_id and
            (task_id is None or row["task_id"] == task_id)), key=lambda row: (row["created_at"], row["delivery_id"]), reverse=True)
        result = []
        for row in rows[offset:offset + 20]:
            excerpt, truncated = clip(row["text"], 4096)
            result.append({**row, "text": excerpt, "truncated": truncated})
        return {"messages": result, "has_more": len(rows) > offset + 20,
                "unacknowledged": sum(row["state"] != "acknowledged" for row in rows)}

    def acknowledge(self, workflow_id, delivery_id, expected_version, task_id=None):
        from .memory import scope
        scope(workflow_id, task_id)
        self.show(workflow_id, delivery_id, task_id)
        with self.store.transaction() as db:
            row = self.store.metadata(PREFIX + delivery_id)
            if not row:
                raise NotFoundError("Inbox message does not exist")
            if row["workflow_id"] != workflow_id or (task_id is not None and row["task_id"] != task_id):
                raise ConflictError("Inbox message belongs to another scope")
            if type(expected_version) is not int or expected_version != row["version"]:
                raise ConflictError("Inbox version changed; reload before acknowledging")
            row.update(state="acknowledged", version=row["version"] + 1, acknowledged_at=self.store.timestamp())
            self.store._set_metadata(db, PREFIX + delivery_id, row)
        return row

    def offer(self, workflow_id, task_id=None, offset=0):
        page = self.list(workflow_id, task_id, offset)
        with self.store.transaction() as db:
            for item in page["messages"]:
                if item["state"] == "created":
                    row = self.store.metadata(PREFIX + item["delivery_id"])
                    row.update(state="offered", version=row["version"] + 1, offered_at=self.store.timestamp())
                    self.store._set_metadata(db, PREFIX + row["delivery_id"], row)
                    item.update(state=row["state"], version=row["version"])
        return page

    def show(self, workflow_id, delivery_id, task_id=None):
        from .memory import scope
        scope(workflow_id, task_id)
        if not isinstance(delivery_id, str) or len(delivery_id) != 64:
            raise RuntimeContractError("Invalid delivery identity")
        row = self.store.metadata(PREFIX + delivery_id)
        if not row:
            raise NotFoundError("Inbox message does not exist")
        if row["workflow_id"] != workflow_id or (task_id is not None and row["task_id"] != task_id):
            raise ConflictError("Inbox message belongs to another scope")
        return row
