"""Shared v1 vocabulary, timestamps and scope validation."""

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum

from .errors import RuntimeContractError

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
RFC3339_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:[Zz]|[+-]\d{2}:\d{2})\Z"
)


class EventStatus(StrEnum):
    WAITING_ANCHOR = "waiting_anchor"
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"
    DEAD_LETTER = "dead_letter"


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise RuntimeContractError("IDs must be 1-128 ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit")
    return value


def parse_timestamp(value: str) -> datetime:
    """Accept RFC3339 with an explicit zone and at most microsecond precision."""
    if not isinstance(value, str) or not RFC3339_PATTERN.fullmatch(value):
        raise RuntimeContractError("Time must be RFC3339 with an explicit timezone")
    # RFC3339 -00:00 means the local offset is unknown, unsuitable for scheduling.
    if value.endswith("-00:00"):
        raise RuntimeContractError("Unknown timezone offset -00:00 is not supported")
    if value[-1:].upper() != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise RuntimeContractError("Invalid RFC3339 timezone offset")
    try:
        return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RuntimeContractError("Invalid RFC3339 time") from None


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeContractError("Naive datetime is not allowed")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def session_key(workflow_id: str, task_id: str) -> str:
    validate_id(workflow_id)
    validate_id(task_id)
    return hashlib.sha256(f"{workflow_id}\0{task_id}".encode()).hexdigest()
