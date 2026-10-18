"""Versioned, transactional SQLite bootstrap; queue operations live in store.py."""

import os
import sqlite3
from contextlib import closing
from importlib.resources import files
from pathlib import Path

from .errors import StorageError

SCHEMA_VERSION = 2
V1_TABLES = {"runtime_metadata", "session_bindings", "events", "runs", "audit_log"}
REQUIRED_TABLES = V1_TABLES | {"memory_notes"}


def inspect_database(path: Path) -> dict:
    """Read-only inspection: doctor must not initialize or migrate a database."""
    if not path.is_file():
        raise StorageError("Database does not exist; run init")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if version != SCHEMA_VERSION or not REQUIRED_TABLES <= tables or integrity != "ok":
            raise StorageError("Database schema or integrity is incompatible; preserve the file for recovery")
        return {"schema_version": version, "integrity": integrity, "journal_mode": journal}
    except sqlite3.Error:
        raise StorageError("Cannot inspect database; preserve it for recovery") from None


def initialize_database(path: Path) -> dict:
    """Migrate known schemas transactionally; refuse foreign/future databases."""
    if path.is_symlink():
        raise StorageError("Database path must not be a symlink")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StorageError("Database must be owned by the current user with mode 0600")
    finally:
        os.close(fd)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {row[0] for row in tables}
        required = REQUIRED_TABLES if version == 2 else V1_TABLES
        if version not in (0, 1, 2) or (version == 0 and tables) or (version > 0 and not required <= names):
            raise StorageError("Refusing an unknown or newer database schema; no migration applied")
        for target, filename in ((1, "001_initial.sql"), (2, "002_memory.sql")):
            if target <= version:
                continue
            sql = files("oicoach_runtime").joinpath("migrations/" + filename).read_text(encoding="utf-8")
            # execute individually: executescript would implicitly commit the transaction.
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise StorageError("Incomplete packaged migration")
            connection.execute(f"PRAGMA user_version={target}")
        connection.execute("COMMIT")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
    except sqlite3.Error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise StorageError("Database initialization failed; migration was rolled back") from None
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return inspect_database(path)
