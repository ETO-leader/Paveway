"""Native tools and slash commands share the existing durable queue."""

from datetime import timedelta
import hashlib
import math

from .errors import ConflictError, RuntimeContractError
from .frontend import Frontend, alive
from .models import parse_timestamp, utc_text
from .store import canonical_json


class Wakeup:
    def __init__(self, store):
        self.store = store

    def call(self, operation, owner, parameters):
        record = Frontend(self.store).authenticate(**owner)
        if record["status"] not in ("active", "idle") or not alive(record.get("omp_pid"), record.get("omp_identity")):
            raise ConflictError("Wakeup requires a live, attested native owner")
        if not isinstance(parameters, dict):
            raise RuntimeContractError("Wakeup parameters must be an object")
        workflow, task = record["workflow_id"], record["task_id"]
        if operation == "list":
            if parameters:
                raise RuntimeContractError("Wakeup list has no scope arguments")
            return self.store.upcoming(workflow, task)[:20]
        if operation in ("cancel", "reschedule"):
            expected = {"event_id", "expected_version"} | ({"at"} if operation == "reschedule" else set())
            if set(parameters) != expected:
                raise RuntimeContractError("Wakeup modification requires an event and CAS version")
            event = self.store.event(parameters["event_id"])
            if (event["workflow_id"], event["task_id"]) != (workflow, task):
                raise ConflictError("Wakeup belongs to another native scope")
            return self.store.modify(**parameters, cancel=operation == "cancel")
        if operation != "schedule":
            raise RuntimeContractError("Unknown wakeup operation")
        if set(parameters) != {"when", "reason", "dedupe_key", "intent", "authorized_by_user"}:
            raise RuntimeContractError("Wakeup requires timing, intent, authorization and a dedupe key")
        if parameters["authorized_by_user"] is not True:
            raise RuntimeContractError("Wakeup requires an explicit user request or existing opt-in; no autonomous loop is enabled")
        if parameters["intent"] not in ("reminder", "agent_wake"):
            raise RuntimeContractError("Choose reminder or agent_wake intent")
        when = parameters["when"]
        if not isinstance(when, dict):
            raise RuntimeContractError("Wakeup timing must be an object")
        if when.get("kind") == "after_turn_settled":
            raise RuntimeContractError("Native after_turn_settled is unavailable; strict completion evidence is missing. No fallback was scheduled")
        if when.get("kind") == "at" and set(when) == {"kind", "at"}:
            due = utc_text(parse_timestamp(when["at"]))
            semantic = {"kind": "at", "at": due}
        elif when.get("kind") == "after_now" and set(when) == {"kind", "seconds"}:
            seconds = when["seconds"]
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 31536000:
                raise RuntimeContractError("after_now must be 0–31536000 finite seconds")
            due = utc_text(self.store.clock() + timedelta(seconds=seconds))
            semantic = {"kind": "after_now", "seconds": float(seconds)}
        else:
            raise RuntimeContractError("Specify at with timezone or after_now seconds")
        payload = {"mode": "checkin", "auto_followup": False, "native_wakeup": True,
                   "intent": parameters["intent"], "conversation_id": record["conversation_id"],
                   "wakeup_when": semantic, "message": parameters["reason"]}
        fingerprint = hashlib.sha256(canonical_json([workflow, task, semantic, payload]).encode()).hexdigest()
        event = self.store.enqueue(workflow, task, parameters["reason"], parameters["dedupe_key"], payload,
                                   at=due, request_identity=fingerprint)
        key = "native-wakeup-config:v1:" + event["event_id"]
        if self.store.metadata(key) is None:
            snapshot = self.store.metadata(record["config_key"])
            if not snapshot:
                raise ConflictError("Native launch snapshot is missing; inspect the queued event before retrying")
            with self.store.transaction() as db:
                self.store._set_metadata(db, key, {**snapshot,
                    "binding_task_id": "native-" + hashlib.sha256(record["conversation_id"].encode()).hexdigest()})
        return event
