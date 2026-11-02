"""Deterministic build fingerprint for oicoach_runtime package code and resources."""

import hashlib
from pathlib import Path

EXCLUDED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".runtime",
    ".git",
    "tests",
}

EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".swp",
    ".tmp",
    "~",
}

# Supported code and asset suffixes in the distribution
INCLUDED_SUFFIXES = {
    ".py",
    ".mjs",
    ".js",
    ".md",
    ".sql",
    ".json",
    ".toml",
    ".so",
    ".pyd",
    ".dylib",
}


def compute_build_id(package_root: Path | None = None) -> str:
    """Compute deterministic SHA-256 digest of runtime code and bundled assets.

    Files are sorted by normalized relative posix path. The digest includes
    both relative posix paths and file contents.
    """
    if package_root is None:
        package_root = Path(__file__).resolve().parent

    digest = hashlib.sha256()
    entries: list[tuple[str, Path]] = []

    for path in package_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(package_root)
        if any(part in EXCLUDED_PARTS or part.startswith(".git") for part in rel.parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES or path.name.endswith("~"):
            continue
        if path.suffix not in INCLUDED_SUFFIXES:
            continue
        entries.append((rel.as_posix(), path))

    entries.sort(key=lambda item: item[0])

    for rel_posix, file_path in entries:
        digest.update(rel_posix.encode("utf-8"))
        digest.update(b"\0")
        with file_path.open("rb") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
        digest.update(b"\0")

    return digest.hexdigest()


_CURRENT_BUILD_ID: str | None = None


def runtime_build_id() -> str:
    """Return the cached or freshly computed build fingerprint for the runtime."""
    global _CURRENT_BUILD_ID
    if _CURRENT_BUILD_ID is None:
        _CURRENT_BUILD_ID = compute_build_id()
    return _CURRENT_BUILD_ID
