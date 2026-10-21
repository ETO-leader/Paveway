import argparse
import asyncio
import json
import os
import sqlite3
import sys
import uuid
import math
import time
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import (absolute_path, config_text, default_config_path, defaults,
                     ensure_private_directory, load_config, ServerConfig)
from .db import initialize_database
from .doctor import diagnose
from .errors import ConfigError, RuntimeContractError, DaemonError
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
    return {"api_version": 1, "milestone": "M4", "config_created": created, "config": str(path),
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
    proxy = commands.add_parser("proxy", help="Synchronize local proxy settings and check proxy TCP reachability")
    proxy_commands = proxy.add_subparsers(dest="operation", required=True)
    for action in ("sync", "status"):
        common(proxy_commands.add_parser(action))
    verify = commands.add_parser("verify-omp", help="Inspect real OMP in a disposable workspace; model turns are opt-in")
    common(verify)
    verify.add_argument("--with-model", action="store_true", help="Run three small model turns to verify resume and task isolation")
    common(commands.add_parser("serve", help="Run the persistent foreground daemon"))
    common(commands.add_parser("status", help="Query daemon, queue and adapter status"))
    ui = commands.add_parser("ui", help="Open the interactive terminal interface backed by CLI subprocesses")
    common(ui)
    ui.add_argument("--workflow", help="Initially selected workflow; otherwise choose in the UI")
    ui.add_argument("--task", default="training")
    service = commands.add_parser("service", help="Render, install and manage owned systemd user units")
    service_commands = service.add_subparsers(dest="operation", required=True)
    for name in ("render", "install", "status", "logs", "start", "stop", "restart", "enable", "disable", "uninstall"):
        child = service_commands.add_parser(name)
        common(child)
        child.add_argument("--name", default="oicoach-runtime.service")
        if name in ("render", "install"):
            child.add_argument("--working-directory", type=Path)
        if name == "install":
            child.add_argument("--replace", action="store_true", help="Update a unit already owned by this configuration")
            child.add_argument("--capture-env", action="append", default=[], metavar="NAME",
                               help="Persist this shell variable in the private service environment file; repeat for each name")
        if name in ("enable", "disable"):
            child.add_argument("--now", action="store_true")
        if name == "logs":
            child.add_argument("--lines", type=int, default=50)
    workflow = commands.add_parser("workflow", help="Create and inspect editable external workflows")
    workflow_commands = workflow.add_subparsers(dest="operation", required=True)
    for name in ("create", "list", "show", "models", "connect-tijian", "customize"):
        child = workflow_commands.add_parser(name)
        common(child)
        if name == "customize":
            actions = child.add_subparsers(dest="customize_operation", required=True)
            for action in ("show", "prompt", "inference", "skill-put", "skill-delete", "skill-enable", "skill-disable"):
                operation = actions.add_parser(action)
                common(operation)
                operation.add_argument("identifier")
                if action != "show":
                    operation.add_argument("--expected-revision", type=int, required=True)
                if action == "prompt":
                    operation.add_argument("--mode", choices=("append", "replace"), default="append")
                if action == "inference":
                    from .customization import THINKING_LEVELS
                    operation.add_argument("--model", default=None)
                    operation.add_argument("--thinking", choices=("", *THINKING_LEVELS), default=None)
                if action.startswith("skill-"):
                    operation.add_argument("--name", required=True)
                if action == "skill-put":
                    operation.add_argument("--description", required=True)
                    from .customization import SKILL_CATEGORIES
                    operation.add_argument("--category", choices=SKILL_CATEGORIES, default=None)
                if action in ("prompt", "skill-put"):
                    operation.add_argument("--content-file", type=Path, required=True)
            continue
        if name != "list":
            child.add_argument("identifier")
        if name == "create":
            child.add_argument("--workspace", type=Path, required=True)
            child.add_argument("--template", choices=("coach", "example"), default="coach")
    sync = commands.add_parser("sync", help="Independent Git storage for user settings and archived history")
    sync_commands = sync.add_subparsers(dest="operation", required=True)
    for name in ("setup", "remote", "status", "now", "run", "pause", "resume", "conflicts", "resolve", "bind", "history", "show", "messages"):
        child = sync_commands.add_parser(name)
        common(child)
        if name == "remote":
            child.add_argument("--remote", required=True)
        if name == "setup":
            child.add_argument("--repository", type=Path, required=True)
            child.add_argument("--remote", default="")
            child.add_argument("--branch", default="main")
            child.add_argument("--interval", type=int, default=60)
        if name in ("resolve", "bind"):
            child.add_argument("--workflow", required=True)
        if name == "resolve":
            choice = child.add_mutually_exclusive_group(required=True)
            choice.add_argument("--head")
            choice.add_argument("--keep-local", action="store_true")
        if name == "bind":
            child.add_argument("--workspace", type=Path, required=True)
        if name == "history":
            child.add_argument("--query", default="")
            child.add_argument("--workflow")
            child.add_argument("--offset", type=int, default=0)
        if name in ("show", "messages"):
            child.add_argument("--device", required=True)
        if name == "show":
            child.add_argument("--run", required=True)
        if name == "messages":
            child.add_argument("--session", required=True)
            child.add_argument("--query", default="")
            child.add_argument("--offset", type=int, default=0)
    ask = commands.add_parser("ask", help="Queue an immediate workflow request; read its result with runs output")
    common(ask)
    ask.add_argument("--workflow", required=True)
    ask.add_argument("--task", required=True)
    message = ask.add_mutually_exclusive_group(required=True)
    message.add_argument("--message")
    message.add_argument("--message-file", type=Path)
    ask.add_argument("--mode", choices=("plan", "checkin", "review"), default="plan")
    ask.add_argument("--dedupe-key")
    ask.add_argument("--wait", action="store_true", help="Wait for completion and print the saved final reply")
    ask.add_argument("--timeout", type=float, default=180, help="Wait budget in seconds; timing out does not cancel the event")
    memory = commands.add_parser("memory", help="Maintain scoped persistent notes with optimistic version checks")
    memory_commands = memory.add_subparsers(dest="operation", required=True)
    for name in ("put", "show", "list", "delete", "recall"):
        child = memory_commands.add_parser(name)
        common(child)
        child.add_argument("--workflow", required=True)
        child.add_argument("--task", required=name == "recall", help="Omit for workflow-wide notes")
        if name in ("put", "show", "delete"):
            child.add_argument("--key", required=True)
        if name == "put":
            child.add_argument("--title", required=True)
            content = child.add_mutually_exclusive_group(required=True)
            content.add_argument("--content")
            content.add_argument("--content-file", type=Path)
            child.add_argument("--kind", choices=("fact", "preference", "summary", "hypothesis"), default="fact")
            child.add_argument("--pinned", action="store_true")
            child.add_argument("--source-event")
            child.add_argument("--expires-at")
        if name in ("put", "delete"):
            child.add_argument("--expected-version", type=int, required=name == "delete", default=0,
                               help="0 creates; updates/deletes require the observed version")
        if name == "list":
            child.add_argument("--query", default="")
            child.add_argument("--offset", type=int, default=0)
    history = commands.add_parser("history", help="Search stored requests/replies and read native OMP session text")
    history_commands = history.add_subparsers(dest="operation", required=True)
    for name in ("search", "show", "messages"):
        child = history_commands.add_parser(name)
        common(child)
        if name == "show":
            child.add_argument("identifier")
        else:
            child.add_argument("--workflow", required=True)
            child.add_argument("--task", required=name == "messages")
            child.add_argument("--query", default="")
            child.add_argument("--offset", type=int, default=0)
    tijian = commands.add_parser("tijian", help="Tijian JSON CLI adapter and optional stdio MCP bridge")
    tijian_commands = tijian.add_subparsers(dest="operation", required=True)
    for name in ("setup", "tools", "doctor", "call", "mcp-config", "mcp"):
        child = tijian_commands.add_parser(name)
        common(child)
        if name == "setup":
            child.add_argument("--executable", required=True)
            child.add_argument("--read-only", action=argparse.BooleanOptionalAction, default=None,
                               help="Preserve the existing setting when omitted")
        if name == "doctor":
            child.add_argument("--with-data", action="store_true", help="Also verify three read queries against local Tijian data")
        if name == "call":
            child.add_argument("name", help="Operation from tijian tools, e.g. problem_get")
            data = child.add_mutually_exclusive_group()
            data.add_argument("--arguments", default="{}", help="Operation arguments as a JSON object")
            data.add_argument("--arguments-file", type=Path, help="JSON object file, at most 64 KiB")
    agent = commands.add_parser("agent", help="Control managed agent processes without replaying prompts")
    agent_commands = agent.add_subparsers(dest="operation", required=True)
    restart = agent_commands.add_parser("restart", help="Stop a task's active run and probe a fresh process using its session")
    common(restart)
    restart.add_argument("--task", required=True)
    restart.add_argument("--workflow")
    restart.add_argument("--reason", default="manual restart")
    restart.add_argument("--reset-circuit", action="store_true", help="Reset supervisor cooldown and failure history after a successful process probe")
    show = agent_commands.add_parser("show", help="Inspect a durable restart job")
    common(show)
    show.add_argument("identifier")
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
            upcoming = children.add_parser("upcoming", help="Up to 100 pending events ordered by due time; no payloads")
            common(upcoming)
            upcoming.add_argument("--workflow", required=True)
            upcoming.add_argument("--task")
        elif category == "runs":
            listing.add_argument("--task")
        if category != "recover":
            show = children.add_parser("show")
            show.add_argument("identifier")
            common(show)
        if category == "runs":
            output = children.add_parser("output", help="Read the captured final assistant reply, at most 16 KiB UTF-8")
            output.add_argument("identifier")
            common(output)
        if category == "events":
            wait = children.add_parser("wait", help="Wait for an event to reach a terminal state without cancelling it")
            wait.add_argument("identifier")
            wait.add_argument("--timeout", type=float, default=180)
            common(wait)
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
    if args.command in ("memory", "history"):
        if args.command == "history" and args.operation == "show":
            return "history.show", {"run_id": args.identifier}
        raw = {"workflow_id": args.workflow}
        if args.task is not None:
            raw["task_id"] = args.task
        if hasattr(args, "key"):
            raw["key"] = args.key
        if hasattr(args, "query"):
            raw.update(query=args.query, offset=args.offset)
        if hasattr(args, "expected_version"):
            raw["expected_version"] = args.expected_version
        if args.operation == "put":
            content = args.content
            if args.content_file:
                with args.content_file.open("rb") as stream:
                    try:
                        content = stream.read(4097).decode("utf-8")
                    except UnicodeError:
                        raise RuntimeContractError("Memory content must be UTF-8 text") from None
            raw.update(title=args.title, content=content, kind=args.kind, pinned=args.pinned,
                       source_event_id=args.source_event, expires_at=args.expires_at)
        return args.command + "." + args.operation, raw
    if args.command == "ask":
        text = args.message
        if args.message_file:
            with args.message_file.open("rb") as stream:
                encoded = stream.read(32769)
            try:
                text = encoded.decode("utf-8")
            except UnicodeError:
                raise RuntimeContractError("Message file must be UTF-8 text") from None
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 32768 or "\0" in text:
            raise RuntimeContractError("Message must be nonempty UTF-8 text at most 32 KiB")
        return "schedule", {"workflow_id": args.workflow, "task_id": args.task, "reason": "user " + args.mode,
            "dedupe_key": args.dedupe_key or str(uuid.uuid4()), "payload": {"mode": args.mode, "message": text},
            "at": "now"}
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
    if args.command == "agent":
        if args.operation == "show":
            return "agent.show", {"restart_id": args.identifier}
        raw = {"task_id": args.task, "reason": args.reason, "reset_circuit": args.reset_circuit}
        if args.workflow:
            raw["workflow_id"] = args.workflow
        return "agent.restart", raw
    raw = {}
    if args.command == "events" and args.operation == "upcoming":
        raw["workflow_id"] = args.workflow
        if args.task is not None:
            raw["task_id"] = args.task
        return "events.upcoming", raw
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


def wait_for_event(config, event_id, timeout, *, with_output=False):
    if not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise RuntimeContractError("Wait timeout must be finite and between 0 and 86400 seconds")
    deadline = time.monotonic() + timeout
    while True:
        try:
            event = request(config.server.socket_path, "events.show", {"event_id": event_id})
            if event["status"] in ("succeeded", "failed", "blocked", "uncertain", "dead_letter", "cancelled"):
                result = {"event": event} if with_output else event
                if with_output:
                    result["output"] = request(config.server.socket_path, "runs.output", {"run_id": event["run_id"]}) if event["run_id"] else None
                return {"api_version": 1, "ok": event["status"] == "succeeded", "result": result}
        except DaemonError:
            pass  # A service restart does not cancel the durable event.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = RuntimeContractError("Event is still pending or daemon is unavailable; inspect events show " + event_id + ". It was not cancelled")
            error.code = "wait_timeout"
            raise error
        time.sleep(min(0.2, remaining))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        path = (args.config or (Path(os.environ["OICOACH_CONFIG"]) if os.environ.get("OICOACH_CONFIG") else default_config_path())).absolute()
        if args.command == "ui":
            if args.json:
                raise RuntimeContractError("The terminal UI is interactive; use the individual CLI commands for JSON")
            from .tui import run
            return run(path, workflow=args.workflow, task=args.task)
        elif args.command == "init":
            report = initialize(path, args.state_dir, args.workflow_root)
        elif args.command == "doctor":
            report = diagnose(load_config(path), path)
        elif args.command == "proxy":
            from .proxy import status, synchronize
            report = synchronize(path) if args.operation == "sync" else status(path)
        elif args.command == "serve":
            from .proxy import synchronize
            proxy = synchronize(path)
            print(json.dumps({"component": "proxy", "transition": "startup_check", **proxy}), flush=True)
            from .scheduler import Runtime
            asyncio.run(Runtime(load_config(path), path).serve())
            return 0
        elif args.command == "verify-omp":
            from .smoke import verify_omp
            from .proxy import child_environment
            os.environ.update(child_environment(path))
            report = asyncio.run(verify_omp(load_config(path), with_model=args.with_model))
        elif args.command == "service":
            from . import service
            service.unit_name(args.name)
            if args.operation == "render":
                report = {"api_version": 1, "unit": args.name,
                          "unit_text": service.render(path, working_directory=args.working_directory)}
            elif args.operation == "install":
                report = service.install(path, name=args.name, working_directory=args.working_directory, replace=args.replace,
                                         capture_env=args.capture_env)
            elif args.operation == "status":
                report = service.status(path, name=args.name)
            elif args.operation == "logs":
                report = service.logs(path, name=args.name, lines=args.lines)
            else:
                report = service.control(path, args.operation, name=args.name, now=getattr(args, "now", False))
        elif args.command == "sync":
            from . import git_sync
            config = load_config(path)
            if args.operation == "setup":
                report = git_sync.configure(config, args.repository, args.remote, args.branch, args.interval)
            elif args.operation == "remote":
                report = git_sync.set_remote(config, args.remote)
            elif args.operation == "status":
                report = git_sync.status(config)
            elif args.operation == "now":
                report = {"api_version": 1, "result": request(config.server.socket_path, "sync.request")}
            elif args.operation == "run":
                result = git_sync.cycle(config)
                report = {"api_version": 1, "ok": result["state"] in ("ok", "paused"), "result": result}
            elif args.operation in ("pause", "resume"):
                report = git_sync.set_enabled(config, args.operation == "resume")
            elif args.operation == "conflicts":
                report = {"api_version": 1, "conflicts": git_sync.conflicts(config)}
            elif args.operation == "resolve":
                report = git_sync.resolve(config, args.workflow, args.head, args.keep_local)
            elif args.operation == "bind":
                from .workflow_setup import create
                if not any(c["workflow_id"] == args.workflow for c in git_sync.conflicts(config)):
                    raise RuntimeContractError("No unbound remote user configuration for this workflow")
                report = create(config, args.workflow, args.workspace)
            elif args.operation == "history":
                report = git_sync.history(config, args.query, args.workflow, args.offset)
            elif args.operation == "messages":
                report = git_sync.messages(config, args.device, args.session, args.query, args.offset)
            else:
                report = git_sync.history_show(config, args.device, args.run)
        elif args.command == "workflow":
            from .workflow_setup import create, describe, connect_tijian
            from .workflows import WorkflowProvider
            config = load_config(path)
            provider = WorkflowProvider(config.workflow_root)
            if args.operation == "customize":
                from . import customization
                if args.customize_operation == "show":
                    report = customization.show(config, args.identifier)
                else:
                    values = {}
                    if args.customize_operation in ("prompt", "skill-put"):
                        if not args.content_file.is_file() or args.content_file.stat().st_size > 65536:
                            raise RuntimeContractError("Customization content must be a UTF-8 file at most 64 KiB")
                        try:
                            values["content"] = args.content_file.read_text(encoding="utf-8")
                        except UnicodeError:
                            raise RuntimeContractError("Customization content must be valid UTF-8") from None
                    if args.customize_operation == "prompt":
                        values["mode"] = args.mode
                    elif args.customize_operation == "inference":
                        values.update({key: getattr(args, key) for key in ("model", "thinking") if getattr(args, key) is not None})
                    else:
                        values["name"] = args.name
                        if args.customize_operation == "skill-put":
                            values["description"] = args.description
                            values["category"] = args.category
                    report = customization.update(config, args.identifier, args.expected_revision, args.customize_operation, **values)
            elif args.operation == "models":
                from .model_selection import catalog
                report = catalog(config, args.identifier, config_path=path)
            elif args.operation == "create":
                report = create(config, args.identifier, args.workspace, args.template)
            elif args.operation == "connect-tijian":
                report = connect_tijian(config, path, args.identifier)
            elif args.operation == "show":
                report = {"api_version": 1, "workflow": describe(provider.describe(args.identifier))}
            else:
                report = {"api_version": 1, "workflows": [describe(provider.describe(p.name)) for p in sorted(config.workflow_root.iterdir()) if p.is_dir()]}
        elif args.command == "tijian":
            from .integrations.tijian import TijianClient, catalog, mcp_configuration
            if args.operation == "tools":
                report = catalog()
            elif args.operation == "setup":
                from .integrations.tijian import configure
                report = asyncio.run(configure(path, args.executable, args.read_only))
            elif args.operation == "mcp-config":
                report = mcp_configuration(path)
            else:
                tijian_config = load_config(path).tijian
                if args.operation == "mcp":
                    from .integrations.tijian_mcp import serve
                    asyncio.run(serve(tijian_config))
                    return 0
                client = TijianClient(tijian_config)
                if args.operation == "doctor":
                    report = asyncio.run(client.diagnose(args.with_data))
                else:
                    data = args.arguments
                    if args.arguments_file:
                        with args.arguments_file.open("rb") as stream:
                            data = stream.read(65537)
                    if len(data.encode() if isinstance(data, str) else data) > 65536:
                        raise RuntimeContractError("Tijian arguments exceed 64 KiB")
                    try:
                        values = json.loads(data)
                    except (ValueError, UnicodeError, RecursionError):
                        raise RuntimeContractError("Tijian arguments must be a JSON object") from None
                    report = {"api_version": 1, "result": asyncio.run(client.call(args.name, values))}
        else:
            config = load_config(path)
            if args.command == "events" and args.operation == "wait":
                report = wait_for_event(config, args.identifier, args.timeout)
            else:
                if args.command == "ask" and args.wait and (not math.isfinite(args.timeout) or not 0 < args.timeout <= 86400):
                    raise RuntimeContractError("Wait timeout must be finite and between 0 and 86400 seconds")
                command, raw = remote_arguments(args)
                result = request(config.server.socket_path, command, raw)
                report = wait_for_event(config, result["event_id"], args.timeout, with_output=True) if args.command == "ask" and args.wait else {"api_version": 1, "result": result}
        if args.command == "service" and args.operation == "render" and not args.json:
            print(report["unit_text"], end="")
        elif args.command == "runs" and args.operation == "output" and not args.json:
            print(report["result"]["text"] if report["result"]["status"] == "available" else report["result"]["status"])
            if report["result"].get("truncated"):
                print("Final reply truncated to 16 KiB; the full session remains in the private sessions directory", file=sys.stderr)
        elif args.command == "ask" and args.wait and not args.json:
            output = report["result"]["output"]
            print(output["text"] if output and output["status"] == "available" else json.dumps(report["result"], ensure_ascii=False))
            if output and output.get("truncated"):
                print("Final reply truncated to 16 KiB; the full session remains in the private sessions directory", file=sys.stderr)
        elif args.json or args.command != "doctor":
            print(json.dumps(report, ensure_ascii=False))
        else:
            for check in report["checks"]:
                print(f"{check['status']:4} {check['check']}: {check['detail']}")
        return 0 if report.get("ok", True) else 1
    except RuntimeContractError as error:
        output = {"api_version": 1, "error": {"code": error.code, "message": str(error)}}
        if hasattr(error, "outcome"):
            output["error"]["outcome"] = error.outcome
    except (OSError, sqlite3.Error):
        # Do not leak arbitrary filesystem contents or SQLite statements through errors.
        output = {"api_version": 1, "error": {"code": "io_error", "message": "Filesystem or database operation failed; check paths and permissions"}}
    if args.json:
        print(json.dumps(output), file=sys.stderr)
    else:
        print(f"{output['error']['code']}: {output['error']['message']}", file=sys.stderr)
    return 1
