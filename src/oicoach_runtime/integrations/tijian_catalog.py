"""Tijian JSON CLI contract, inspected at the pinned upstream revision below.

This catalog exposes CLI capabilities, not every Desktop UI feature.
"""

import math
from dataclasses import dataclass

from ..errors import RuntimeContractError

UPSTREAM_REVISION = "b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f"
UPSTREAM_URL = "https://github.com/zjpanhongxuan/Tijian"
STATUSES = ["unseen", "explored", "attempted", "solved", "无", "胡", "错", "过"]
TARGETS = ["explore", "solve", "胡", "写"]


def string(description, enum=None):
    value = {"type": "string", "minLength": 1, "maxLength": 8192,
             "pattern": r"^[^\x00-\x1f-][^\x00-\x1f]*$", "description": description}
    if enum:
        value["enum"] = enum
    return value


PROBLEM = string("Tijian problem ID or supported OJ URL")
PROBLEMS = {"type": "array", "items": PROBLEM, "minItems": 1, "maxItems": 100}
DATE = string("Tijian local calendar date, preferably YYYY-MM-DD; timezone/day boundary follow Tijian settings")
PATH = string("Local filesystem path, resolved by Tijian relative to the MCP/CLI working directory")
BOOL = {"type": "boolean"}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 10000}
SCORE = {"type": "number", "minimum": 0, "maximum": 10}


@dataclass(frozen=True)
class Operation:
    name: str
    words: tuple[str, ...]
    description: str
    properties: dict
    required: tuple[str, ...] = ()
    positional: tuple[str, ...] = ()
    flags: tuple[tuple[str, str], ...] = ()
    mutates: bool = False
    destructive: bool = False
    network: bool = False

    def schema(self):
        properties = dict(self.properties)
        if self.mutates:
            properties["dry_run"] = {**BOOL, "description": "Ask Tijian to preview; changed may describe projected changes"}
        if self.destructive:
            properties["confirm"] = {**BOOL, "description": "Must be true for destructive execution; dry_run needs no confirmation"}
        return {"type": "object", "properties": properties, "required": list(self.required),
                "additionalProperties": False}

    def tool(self):
        return {"name": "tijian_" + self.name, "description": self.description,
                "inputSchema": self.schema(), "annotations": {
                    "readOnlyHint": not self.mutates, "destructiveHint": self.destructive,
                    "idempotentHint": False, "openWorldHint": self.network}}

    def arguments(self, values):
        schema = self.schema()
        if not isinstance(values, dict) or set(values) - set(schema["properties"]) or set(self.required) - set(values):
            raise RuntimeContractError("Unknown or missing Tijian operation arguments")
        for key, value in values.items():
            validate_value(value, schema["properties"][key])
        if self.destructive and not (values.get("confirm") or values.get("dry_run")):
            raise RuntimeContractError("Destructive Tijian operation requires confirm=true or dry_run=true")
        if self.name in ("problem_refetch", "problem_vector_compute"):
            if bool(values.get("problems")) == bool(values.get("all", False)):
                raise RuntimeContractError("Specify either problems or all=true")
            if "oj" in values and not values.get("all"):
                raise RuntimeContractError("oj requires all=true")
        if self.name == "problem_edit" and not ({"difficulty", "quality"} & values.keys()):
            raise RuntimeContractError("Provide difficulty or quality")
        args = list(self.words)
        for key in self.positional:
            value = values.get(key)
            if value is not None:
                args.extend(value if isinstance(value, list) else [str(value)])
        for key, flag in self.flags:
            if key not in values:
                continue
            value = values[key]
            if type(value) is bool:
                if value:
                    args.append(flag)
            else:
                args.extend([flag, str(value)])
        if self.name in ("problem_remove", "plan_clear"):
            args.append("--yes")
        if values.get("dry_run"):
            args.append("--dry-run")
        return args


def validate_value(value, schema):
    kind = schema["type"]
    valid = False
    if kind == "string":
        valid = (isinstance(value, str) and 1 <= len(value) <= 8192 and
                 not value.startswith("-") and not any(ord(c) < 32 for c in value))
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind in ("integer", "number"):
        valid = (type(value) is int if kind == "integer" else type(value) in (int, float))
        valid = valid and schema["minimum"] <= value <= schema["maximum"] and math.isfinite(value)
    elif kind == "array":
        valid = isinstance(value, list) and schema["minItems"] <= len(value) <= schema["maxItems"]
        if valid:
            for item in value:
                validate_value(item, schema["items"])
    if not valid or ("enum" in schema and value not in schema["enum"]):
        raise RuntimeContractError("Invalid Tijian argument type, value, size or flag-like token")


def op(name, command, description, properties=None, required=(), positional=(), flags=(), **options):
    return Operation(name, tuple(command.split()), description, properties or {}, required, positional, flags, **options)


OPERATIONS = {item.name: item for item in [
    op("help", "help", "Read the installed Tijian command reference"),
    op("problem_get", "problem get", "Read the locally stored problem and statement without fetching the OJ again. Use this first for collected problems; preserves tags, status history, translations and vectors",
       {"problem": PROBLEM}, ("problem",), ("problem",)),
    op("problem_list", "problem list", "List collected problems with filters",
       {"status": string("Problem status", STATUSES), "has_statement": BOOL, "original": PROBLEM, "limit": LIMIT},
       flags=(("status", "--status"), ("has_statement", "--has-statement"), ("original", "--original"), ("limit", "--limit"))),
    op("problem_stats", "problem stats", "Read local problem library statistics"),
    op("problem_add", "problem add", "Collect missing problems through Tijian's OJ fetchers and persist statements locally for later problem_get calls. Already collected IDs are skipped without fetching; individual failures may coexist with successes",
       {"problems": PROBLEMS}, ("problems",), ("problems",), mutates=True, network=True),
    op("problem_status", "problem status", "Record a status for one or more problems. explored/胡 means explored, not proof of independent AC or solution reading",
       {"problems": PROBLEMS, "status": string("Problem status", STATUSES)}, ("problems", "status"), ("problems",),
       (("status", "--to"),), mutates=True),
    op("problem_refetch", "problem refetch", "Explicitly fetch collected problems again to update stored data, optionally by OJ. Not needed for normal statement reads; use problem_get instead. Use for missing statements or requested refreshes",
       {"problems": PROBLEMS, "all": BOOL, "oj": string("Tijian OJ selector"), "statement_only": BOOL},
       positional=("problems",), flags=(("all", "--all"), ("oj", "--oj"), ("statement_only", "--statement-only")), mutates=True, network=True),
    op("problem_vector_compute", "problem vector compute", "Force vector recomputation using Tijian's configured models; execution requires the running Tijian Desktop executor",
       {"problems": PROBLEMS, "all": BOOL, "oj": string("Tijian OJ selector")}, positional=("problems",),
       flags=(("all", "--all"), ("oj", "--oj")), mutates=True, network=True),
    op("problem_edit", "problem edit", "Update difficulty and/or quality ratings (0..10)",
       {"problem": PROBLEM, "difficulty": SCORE, "quality": SCORE}, ("problem",), ("problem",),
       (("difficulty", "--difficulty"), ("quality", "--quality")), mutates=True),
    op("problem_remove", "problem remove", "Remove a problem from Tijian",
       {"problem": PROBLEM}, ("problem",), ("problem",), mutates=True, destructive=True),
    op("history_import", "history import", "Import a historical status with a Tijian local date/time; not a Runtime wake event",
       {"problem": PROBLEM, "at": string("Local date/time accepted by Tijian, e.g. 2026-10-18 14:30"), "status": string("Problem status", STATUSES)},
       ("problem", "at", "status"), ("problem",), (("at", "--at"), ("status", "--to")), mutates=True),
    op("search", "search", "Search only already collected LOCAL problems. Empty results do not mean an OJ problem cannot be collected: discover verified IDs/URLs with web tools, then problem_add once. Not a web or Desktop semantic search",
       {"query": string("Search text"), "limit": LIMIT}, ("query",), ("query",), (("limit", "--limit"),)),
    op("plan_add", "plan add", "Add problems to a Tijian daily training plan; does not schedule Runtime wakes",
       {"problems": PROBLEMS, "target": string("explore/胡 or solve/写", TARGETS), "date": DATE}, ("problems",), ("problems",),
       (("target", "--target"), ("date", "--date")), mutates=True),
    op("plan_remove", "plan remove", "Remove one problem from a daily plan",
       {"problem": PROBLEM, "date": DATE}, ("problem",), ("problem",), (("date", "--date"),), mutates=True),
    op("plan_target", "plan target", "Set a planned problem's exploration/coding target",
       {"problem": PROBLEM, "target": string("Plan target", TARGETS), "date": DATE}, ("problem", "target"), ("problem", "target"),
       (("date", "--date"),), mutates=True),
    op("plan_show", "plan show", "Read a daily plan; default day follows Tijian settings", {"date": DATE}, flags=(("date", "--date"),)),
    op("plan_carry", "plan carry", "Carry unfinished work using Tijian's own daily-plan semantics", {"date": DATE}, flags=(("date", "--date"),), mutates=True),
    op("plan_clear", "plan clear", "Clear a daily plan", {"date": DATE}, flags=(("date", "--date"),), mutates=True, destructive=True),
    op("daily_show", "daily show", "Read daily training summaries within an optional date interval",
       {"from": DATE, "to": DATE}, flags=(("from", "--from"), ("to", "--to"))),
    op("backup_export", "backup export", "Export a native ZIP or readable Markdown backup to a local path",
       {"path": PATH, "format": string("Backup format", ["native", "markdown"])}, ("path",), ("path",), (("format", "--format"),), mutates=True, destructive=True),
    op("backup_import", "backup import", "Import a backup through Tijian's canonical application path",
       {"path": PATH}, ("path",), ("path",), mutates=True, destructive=True),
    op("backup_verify", "backup verify", "Verify a backup without importing it", {"path": PATH}, ("path",), ("path",)),
    op("article_import", "article import", "Import a Markdown problem list through Tijian",
       {"path": PATH}, ("path",), ("path",), mutates=True),
    op("article_list", "article list", "List articles/problem lists exposed by the CLI"),
    op("article_show", "article show", "Read an article/problem list by ID; file export is a separate operation",
       {"id": string("Article ID")}, ("id",), ("id",)),
    op("article_export", "article show", "Export an article/problem list to a Markdown path",
       {"id": string("Article ID"), "output": PATH}, ("id", "output"), ("id",), (("output", "--output"),), mutates=True, destructive=True),
]}
