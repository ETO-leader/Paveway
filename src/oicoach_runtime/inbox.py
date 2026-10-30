"""Persistent message projection. Offering a message never completes its Run."""

import hashlib

from .errors import ConflictError, NotFoundError, RuntimeContractError
from .store import canonical_json

PREFIX = "delivery:v1:"


class Inbox:
    def __init__(self, store):
        self.store = store

    def create(self, event, kind, text, run_id=None):
        key = hashlib.sha256(canonical_json([event["event_id"], kind, run_id]).encode()).hexdigest()
        with self.store.transaction() as db:
            existing = self.store.metadata(PREFIX + key)
            if existing:
                return existing
            record = {"delivery_id": key, "event_id": event["event_id"], "run_id": run_id,
                "workflow_id": event["workflow_id"], "task_id": event["task_id"],
                "conversation_id": event["payload"].get("conversation_id"), "kind": kind,
                "text": text, "state": "created", "version": 1, "created_at": self.store.timestamp()}
            self.store._set_metadata(db, PREFIX + key, record)
        return record

    def due(self, event):
        if event["payload"].get("native_wakeup") and event["payload"].get("intent") == "reminder":
            value = event["payload"].get("message")
            return self.create(event, "reminder", value if isinstance(value, str) and value else event["reason"])

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
            message = output.get("text") or ("GPT execution status: " + run["status"] + "; no saved final reply")
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
