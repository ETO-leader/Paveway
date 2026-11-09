"""Content-free diagnostics derived only from the persisted OMP session journal.

``session_usage(path, expected_session_id=None)`` reads an already authorized
path; runtime callers must enforce their own session-directory boundary. It does
not follow parent-session files, start OMP, or mutate the journal. Cumulative
usage covers every persisted call in this file, including compacted and inactive
branches, rather than the current prompt context. Only the latest 20 call records
are returned so the diagnostic fits the bounded IPC transport.

OMP initializes normalized cache and cost fields to zero, losing whether the
provider supplied them. Positive values are evidence; zero/missing values remain
unavailable. Costs are OMP calculations, never actual provider invoices.
"""

import json
import math
import os
import re
import stat
from collections import deque
from pathlib import Path

from .errors import ConflictError, NotFoundError, RuntimeContractError


MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_ENTRY_BYTES = 1024 * 1024
CALL_LIMIT = 20
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}\Z")
_METRICS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "omp_normalized_input_tokens", "omp_calculated_cost_usd",
)


def _identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _tokens(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _positive_tokens(value):
    value = _tokens(value)
    return value if value else None


def _cost(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) and value > 0 else None
    except OverflowError:
        return None


def _call(entry):
    if entry.get("type") == "model_usage":
        message = entry
    elif entry.get("type") == "message":
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
    else:
        return None
    usage = message.get("usage")
    usage = usage if isinstance(usage, dict) and _positive_tokens(usage.get("totalTokens")) else {}
    read = _positive_tokens(usage.get("cacheRead"))
    write = _positive_tokens(usage.get("cacheWrite"))
    input_tokens = _tokens(usage.get("input"))
    denominator = input_tokens + read + write if all(v is not None for v in (input_tokens, read, write)) else None
    cost = usage.get("cost")
    return {
        "entry_id": _identifier(entry.get("id")),
        "entry_type": entry["type"],
        "provider": _identifier(message.get("provider")),
        "model": _identifier(message.get("model")),
        "api": _identifier(message.get("api")),
        "input_tokens": input_tokens,
        "output_tokens": _tokens(usage.get("output")),
        "cache_read_tokens": read,
        "cache_write_tokens": write,
        "omp_normalized_input_tokens": denominator,
        "cache_hit_ratio": read / denominator if denominator else None,
        "omp_calculated_cost_usd": _cost(cost.get("total")) if isinstance(cost, dict) else None,
    }


def _summary(calls, scope):
    count = len(calls)
    result = {"scope": scope, "call_count": count}
    coverage = {}
    observed = {}
    for metric in _METRICS:
        values = [call[metric] for call in calls if call[metric] is not None]
        coverage[metric] = len(values)
        try:
            total = math.fsum(values) if metric == "omp_calculated_cost_usd" else sum(values)
        except OverflowError:
            total = None
        if total is not None and metric == "omp_calculated_cost_usd" and not math.isfinite(total):
            total = None
        observed[metric] = total if values else None
        result[metric] = total if count and len(values) == count else None
    result["available_call_counts"] = coverage
    result["observed_subtotals"] = observed
    result["status"] = "available" if any(value is not None for value in observed.values()) else "unavailable"
    denominator = result["omp_normalized_input_tokens"]
    read = result["cache_read_tokens"]
    result["cache_hit_ratio"] = read / denominator if denominator and read is not None else None
    return result


def _read_archive(path, expected_session_id):
    header = None
    calls = []
    parents = {}
    leaf = None
    branch_available = True
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > MAX_SESSION_BYTES:
                raise RuntimeContractError("Session archive must be an owned regular JSONL file at most 16 MiB")
            size = 0
            while line := stream.readline(MAX_ENTRY_BYTES + 1):
                size += len(line)
                if size > MAX_SESSION_BYTES:
                    raise RuntimeContractError("Session archive exceeds 16 MiB")
                if len(line) > MAX_ENTRY_BYTES or not line.endswith(b"\n"):
                    raise RuntimeContractError("Session archive has an oversized or incomplete entry")
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    raise RuntimeContractError("Session archive is not supported OMP JSONL") from None
                if not isinstance(entry, dict):
                    raise RuntimeContractError("Invalid session archive entry")
                # OMP stores a padded physical title slot before its logical
                # session header; it is presentation metadata, not ancestry.
                if entry.get("type") == "title":
                    if header is not None or size != len(line):
                        raise RuntimeContractError("Invalid session archive title slot")
                    continue
                if entry.get("type") == "session":
                    if header is not None:
                        raise RuntimeContractError("Session archive has duplicate headers")
                    version = entry.get("version", 1)
                    if (_identifier(entry.get("id")) is None or type(version) is not int or version not in (1, 2, 3)
                            or not isinstance(entry.get("cwd"), str) or not Path(entry["cwd"]).is_absolute()):
                        raise RuntimeContractError("Session archive header is not supported")
                    if expected_session_id is not None and entry["id"] != expected_session_id:
                        raise ConflictError("Session archive header does not match the recorded session")
                    header = {"id": entry["id"], "version": version}
                    branch_available = version != 1
                    continue
                if header is None:
                    raise RuntimeContractError("Session archive header is missing")
                identity = _identifier(entry.get("id"))
                parent = entry.get("parentId")
                if identity is None or "parentId" not in entry:
                    branch_available = False
                else:
                    if identity in parents or (parent is not None and _identifier(parent) is None):
                        raise RuntimeContractError("Invalid session archive entry identity")
                    # OMP parents always precede their children in the journal.
                    if parent is not None and parent not in parents:
                        raise RuntimeContractError("Session archive branch ancestry is incomplete")
                    parents[identity] = parent
                    leaf = entry.get("activeLeafId", identity)
                    if leaf is not None and (_identifier(leaf) is None or leaf not in parents):
                        raise RuntimeContractError("Invalid session archive active leaf")
                call = _call(entry)
                if call is not None:
                    calls.append(call)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ConflictError("Session archive changed while reading; retry after the session settles")
    except FileNotFoundError:
        raise NotFoundError("Session archive is unavailable") from None
    except OSError:
        raise RuntimeContractError("Session archive cannot be read safely; check paths and permissions") from None
    if header is None:
        raise RuntimeContractError("Session archive header is missing")
    branch = set()
    if branch_available:
        while leaf is not None:
            branch.add(leaf)
            leaf = parents[leaf]
    return header, calls, branch if branch_available else None


def session_usage(path, expected_session_id=None):
    """Return bounded safe call metrics and full-file totals, without raw content.

    ``None`` means unavailable, not a reported zero. ``observed_subtotals`` sum
    only available metrics; ``available_call_counts`` distinguish those partial
    observations from complete totals. The active branch is persisted ancestry,
    not a claim about an in-memory OMP leaf or compacted context. Legacy v1
    journals have no reliable persisted tree and expose no active-branch total.
    Invalid/unsafe archives raise fixed, content-free RuntimeContractError
    subclasses, including NotFoundError and ConflictError.
    """
    if expected_session_id is not None and _identifier(expected_session_id) is None:
        raise RuntimeContractError("Invalid expected session identity")
    header, calls, branch = _read_archive(path, expected_session_id)
    for call in calls:
        call["on_active_branch"] = call["entry_id"] in branch if branch is not None else None
    recent = list(deque(calls, maxlen=CALL_LIMIT))
    active = _summary([call for call in calls if call["on_active_branch"]], "persisted_active_branch_ancestry") if branch is not None else {
        "scope": "persisted_active_branch_ancestry", "status": "unavailable", "reason": "persisted_tree_unavailable", "call_count": None,
    }
    return {
        "source": "omp_session_jsonl",
        "session_id": header["id"],
        "semantics": {
            "tokens": "OMP normalized usage; input_tokens excludes cache reads and writes",
            "cache": "Positive OMP cache values are evidence; normalized zero or missing values are unavailable, not provider-reported zero",
            "input_denominator": "OMP normalized input_tokens + cache_read_tokens + cache_write_tokens; unavailable if any component is ambiguous",
            "cost": "OMP calculated USD cost, not an actual provider invoice; zero or missing cost is unavailable",
            "cumulative": "All persisted model-call records in this session file, all branches, including before compaction; no parent-session or subagent files followed",
            "active_branch": "Persisted ID ancestry only; excludes unrelated fork and off-branch records and may differ from a live in-memory leaf",
        },
        "calls": recent,
        "calls_scope": "latest_persisted_calls_all_branches",
        "calls_truncated": len(calls) > CALL_LIMIT,
        "call_limit": CALL_LIMIT,
        "cumulative": _summary(calls, "session_file_all_calls_all_branches"),
        "active_branch": active,
    }
