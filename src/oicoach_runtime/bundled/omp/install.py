#!/usr/bin/env python3
"""Provision Paveway's pinned OMP source bridge without modifying global installs."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

VERSION = "18.16.0"
COMMIT = "650f3a108229ee11bf68ba3377f4e8c3391cd424"
BUN_VERSION = "1.4.4"
RESOURCES = Path(__file__).resolve().parent
PATCHES = ("statusline.patch", "conversation.patch", "composer.patch", "banner.patch")
BUN_URL = "https://registry.npmjs.org/@oven/bun-linux-x64-baseline/-/bun-linux-x64-baseline-1.4.4.tgz"
BUN_INTEGRITY = "FBU6T21NT+i+rESbi0Z8A8G5xN51c+WZ0v6pIjI6szSzOlqVNv64nf7FPnwNIyxKD1M7Wg5cEobLC/QbzCeLtA=="
NATIVE_URL = "https://registry.npmjs.org/@oh-my-pi/pi-natives-linux-x64/-/pi-natives-linux-x64-18.16.0.tgz"
NATIVE_INTEGRITY = "hd0RhNV6S1tDttmirwKXSZ91uiGN2XLF0qMNtpK1uY6Pav+ZUkyM4GGBdQST530IzrsUCn5E6zjkX98o0QPoBQ=="


def patch_digest() -> str:
    digest = hashlib.sha256()
    for name in PATCHES:
        digest.update(name.encode() + b"\0")
        digest.update((RESOURCES / name).read_bytes())
    return digest.hexdigest()


def cache_root() -> Path:
    return Path(os.environ.get("PAVEWAY_OMP_CACHE", str(Path.home() / ".cache/paveway/omp" / VERSION))).expanduser().resolve()


def download_package(url: str, integrity: str, destination: Path) -> None:
    print(f"Paveway OMP: downloading {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read()
    actual = base64.b64encode(hashlib.sha512(payload).digest()).decode()
    if actual != integrity:
        raise RuntimeError(f"SHA-512 mismatch for {url}")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            if not relative.parts or relative.parts[0] != "package":
                raise RuntimeError(f"Unexpected npm archive member: {member.name}")
            relative = Path(*relative.parts[1:])
            if not relative.parts:
                continue
            if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk():
                raise RuntimeError(f"Unsafe npm archive member: {member.name}")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True, stdout=sys.stderr, stderr=sys.stderr)


def prepared_runtime() -> dict[str, str] | None:
    root = cache_root()
    manifest = root / "runtime.json"
    if not manifest.is_file():
        return None
    data = json.loads(manifest.read_text())
    patch_hash = patch_digest()
    if data.get("commit") != COMMIT or data.get("patch") != patch_hash:
        return None
    if not Path(data["bun"]).is_file() or not (Path(data["source"]) / "packages/coding-agent/src/cli.ts").is_file():
        return None
    return {"bun": data["bun"], "source": data["source"], "launcher": str(RESOURCES / "omp")}


def ensure_runtime(source: Path | None = None) -> dict[str, str]:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise RuntimeError("The pinned Paveway OMP launcher currently supports Linux x86-64 (glibc).")
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = prepared_runtime()
        if existing:
            return existing
        bun_dir = root / f"bun-{BUN_VERSION}"
        bun = bun_dir / "bin/bun"
        if not bun.is_file():
            download_package(BUN_URL, BUN_INTEGRITY, bun_dir)
        patch_hash = patch_digest()
        target = root / f"source-{COMMIT[:12]}-{patch_hash[:12]}"
        if not target.is_dir():
            with tempfile.TemporaryDirectory(prefix="prepare-", dir=root) as temporary:
                temporary_path = Path(temporary)
                tree = temporary_path / "source"
                tree.mkdir()
                if source is not None:
                    # Export the pinned commit, not the user's modified checkout.
                    archive_path = temporary_path / "source.tar"
                    with archive_path.open("wb") as output:
                        subprocess.run(["git", "-C", str(source.resolve()), "archive", COMMIT], check=True, stdout=output)
                    with tarfile.open(archive_path) as archive:
                        archive.extractall(tree, filter="data")
                else:
                    run(["git", "init", "--quiet", str(tree)])
                    run(["git", "remote", "add", "origin", "https://github.com/can1357/oh-my-pi.git"], cwd=tree)
                    run(["git", "fetch", "--depth=1", "origin", COMMIT], cwd=tree)
                    run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=tree)
                for name in PATCHES:
                    run(["git", "apply", "--whitespace=nowarn", str(RESOURCES / name)], cwd=tree)
                env = dict(os.environ)
                env["PATH"] = str(bun.parent) + os.pathsep + env.get("PATH", "")
                # Keep the host reproducible and independent of global Bun caches.
                env["BUN_INSTALL_CACHE_DIR"] = str(root / "dependency-cache")
                # The workspace lock pins JS dependencies. No setup/link scripts or Rust build.
                run([str(bun), "install", "--frozen-lockfile", "--ignore-scripts"], cwd=tree, env=env)
                native_package = temporary_path / "native-package"
                download_package(NATIVE_URL, NATIVE_INTEGRITY, native_package)
                native_dir = tree / "packages/natives/native"
                for native_file in native_package.iterdir():
                    if native_file.name != "package.json":
                        if native_file.is_dir():
                            shutil.copytree(native_file, native_dir / native_file.name, dirs_exist_ok=True)
                        else:
                            shutil.copy2(native_file, native_dir / native_file.name)
                # Paths in workspace links remain relative when the tree is promoted.
                tree.rename(target)
        result = {"bun": str(bun), "source": str(target), "launcher": str(RESOURCES / "omp")}
        manifest_data = dict(result, commit=COMMIT, patch=patch_hash)
        pending = root / "runtime.json.pending"
        pending.write_text(json.dumps(manifest_data) + "\n")
        pending.replace(root / "runtime.json")
        return result


def launch() -> None:
    runtime = prepared_runtime()
    if runtime is None:
        raise RuntimeError(f"Paveway OMP is not prepared. Run: {sys.executable} {RESOURCES / 'install.py'}")
    bun = runtime["bun"]
    env = dict(os.environ)
    env["PATH"] = str(Path(bun).parent) + os.pathsep + env.get("PATH", "")
    # An absolute entrypoint deliberately preserves the caller's project cwd.
    entrypoint = str(Path(runtime["source"]) / "packages/coding-agent/src/cli.ts")
    # The fixed foreground preload registers welcome rendering before CLI prepaint.
    # Worker/RPC launches do not carry native admission data and never load it.
    preload = ["--preload", str(RESOURCES.parent / "paveway-banner-host.mjs")] if env.get("PAVEWAY_NATIVE_DATA") else []
    os.execve(bun, [bun, *preload, entrypoint, *sys.argv[1:]], env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Export the pinned commit from an existing local git checkout")
    arguments = parser.parse_args()
    print(json.dumps(ensure_runtime(arguments.source)))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Paveway OMP: {error}", file=sys.stderr)
        raise SystemExit(1)
