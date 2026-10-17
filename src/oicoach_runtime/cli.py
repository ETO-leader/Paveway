import argparse
import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import (absolute_path, config_text, default_config_path, defaults,
                     ensure_private_directory, load_config, ServerConfig)
from .db import initialize_database
from .doctor import diagnose
from .errors import ConfigError, RuntimeContractError
from .ipc import request
from .models import EventStatus


def initialize(path: Path, state_dir: str | None = None, workflow_root: str | None = None) -> dict:
    # Never truncate an existing config, including a dangling symlink.
    if path.exists() or path.is_symlink():
        if state_dir is not None or workflow_root is not None:
            raise ConfigError("Configuration already exists; edit it explicitly instead of overriding paths with init")
        config = load_config(path)
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ConfigError("Existing config must be privately owned with mode 0600 and not a symlink")
        created = False
    else:
        config = defaults()
        if state_dir is not None:
            state = absolute_path(state_dir, "state_dir")
            config = replace(config, state_dir=state, server=ServerConfig(socket_path=str(state / "runtime.sock")))
        if workflow_root is not None:
            config = replace(config, workflow_root=absolute_path(workflow_root, "workflow_root"))
        ensure_private_directory(path.parent)
        # Validate the complete generated config before exposing the final filename.
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(config_text(config))
                stream.flush()
                os.fsync(stream.fileno())
                config = load_config(temporary)
                # Atomic, no overwrite even if another init won the race.
                os.link(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        created = True
    ensure_private_directory(config.state_dir)
    ensure_private_directory(config.state_dir / "sessions")
    # The workflow registry can be a trusted pre-existing shared/readable directory.
    if not config.workflow_root.exists():
        config.workflow_root.mkdir(mode=0o700, parents=True)
    if not config.workflow_root.is_dir():
        raise ConfigError("workflow_root must be a directory")
    schema = initialize_database(config.database_path)
    return {"api_version": 1, "milestone": "M1", "config_created": created, "config": str(path),
            "database": str(config.database_path), **schema, "daemon_started": False}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="oicoach-runtime", description="Persistent OMP runtime and local control CLI")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create private configuration and a versioned empty database")
    init.add_argument("--state-dir", help="Absolute private state directory (new config only)")
    init.add_argument("--workflow-root", help="Absolute trusted workflow root (new config only)")
    doctor = commands.add_parser("doctor", help="Inspect configuration, database, workflows and OMP CLI capabilities")
    def common(command):
        command.add_argument("--config", type=Path, help="TOML configuration path (default: XDG_CONFIG_HOME)")
        command.add_argument("--json", action="store_true", help="Machine-readable v1 JSON output")
    for command in (init, doctor):
        common(command)
    common(commands.add_parser("serve", help="Run the persistent foreground daemon"))
    common(commands.add_parser("status", help="Query daemon, queue and adapter status"))
    schedule = commands.add_parser("schedule", help="Persist a wake event through the daemon")
    common(schedule)
    schedule.add_argument("--workflow", required=True)
    schedule.add_argument("--task", required=True)
    mode = schedule.add_mutually_exclusive_group(required=True)
    mode.add_argument("--at")
    mode.add_argument("--after-turn", type=float)
    schedule.add_argument("--run-id", default=os.environ.get("OICOACH_RUN_ID"))
    schedule.add_argument("--reason", required=True)
    schedule.add_argument("--dedupe-key", required=True)
    schedule.add_argument("--payload-file", type=Path)
    for category in ("events", "runs", "recover"):
        group = commands.add_parser(category)
        children = group.add_subparsers(dest="operation", required=True)
        listing = children.add_parser("list")
        common(listing)
        listing.add_argument("--offset", type=int, default=0, help="Skip records; each page contains at most 100 per status")
        if category == "events":
            listing.add_argument("--status", choices=[s.value for s in EventStatus])
        elif category == "runs":
            listing.add_argument("--task")
        if category != "recover":
            show = children.add_parser("show")
            show.add_argument("identifier")
            common(show)
        if category == "events":
            for action in ("cancel", "reschedule"):
                edit = children.add_parser(action)
                edit.add_argument("identifier")
                edit.add_argument("--expected-version", required=True, type=int)
                if action == "reschedule":
                    edit.add_argument("--at", required=True)
                common(edit)
        if category == "recover":
            retry = children.add_parser("retry")
            retry.add_argument("identifier")
            retry.add_argument("--confirm-duplicate-risk", action="store_true")
            common(retry)
    return result


def remote_arguments(args):
    if args.command == "schedule":
        payload = {}
        if args.payload_file:
            try:
                with args.payload_file.open("rb") as stream:
                    data = stream.read(65537)
                if len(data) > 65536:
                    raise RuntimeContractError("Payload file exceeds 64 KiB")
                payload = json.loads(data)
            except (ValueError, UnicodeError, RecursionError):
                raise RuntimeContractError("Payload file is not valid JSON") from None
        raw = {"workflow_id": args.workflow, "task_id": args.task, "reason": args.reason,
               "dedupe_key": args.dedupe_key, "payload": payload}
        if args.at is not None:
            raw["at"] = args.at
            # An injected run id is only an after-turn default, not an at anchor.
        else:
            raw.update(after_turn=args.after_turn, run_id=args.run_id)
        return "schedule", raw
    if args.command == "status":
        return "status", {}
    raw = {}
    if args.operation == "list":
        raw["offset"] = args.offset
        if args.command == "events" and args.status:
            raw["status"] = args.status
        if args.command == "runs" and args.task:
            raw["task"] = args.task
    else:
        raw["run_id" if args.command == "runs" else "event_id"] = args.identifier
        if args.operation in ("cancel", "reschedule"):
            raw["expected_version"] = args.expected_version
        if args.operation == "reschedule":
            raw["at"] = args.at
        if args.operation == "retry":
            raw["confirmed"] = args.confirm_duplicate_risk
    return f"{args.command}.{args.operation}", raw


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        path = (args.config or (Path(os.environ["OICOACH_CONFIG"]) if os.environ.get("OICOACH_CONFIG") else default_config_path())).absolute()
        if args.command == "init":
            report = initialize(path, args.state_dir, args.workflow_root)
        elif args.command == "doctor":
            report = diagnose(load_config(path), path)
        elif args.command == "serve":
            from .scheduler import Runtime
            asyncio.run(Runtime(load_config(path), path).serve())
            return 0
        else:
            config = load_config(path)
            command, raw = remote_arguments(args)
            report = {"api_version": 1, "result": request(config.server.socket_path, command, raw)}
        if args.json or args.command != "doctor":
            print(json.dumps(report, ensure_ascii=False))
        else:
            for check in report["checks"]:
                print(f"{check['status']:4} {check['check']}: {check['detail']}")
        return 0 if report.get("ok", True) else 1
    except RuntimeContractError as error:
        output = {"api_version": 1, "error": {"code": error.code, "message": str(error)}}
    except (OSError, sqlite3.Error):
        # Do not leak arbitrary filesystem contents or SQLite statements through errors.
        output = {"api_version": 1, "error": {"code": "io_error", "message": "Filesystem or database operation failed; check paths and permissions"}}
    if args.json:
        print(json.dumps(output), file=sys.stderr)
    else:
        print(f"{output['error']['code']}: {output['error']['message']}", file=sys.stderr)
    return 1
