#!/usr/bin/env python3
"""Deterministically stamp the Codex Baron plugin manifest."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
import re

PLUGIN = Path("plugins/codex-baron")
MARKETPLACE = Path(".agents/plugins/marketplace.json")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _manifest_data(path: Path) -> bytes:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid plugin manifest: {path}") from exc
    if not isinstance(value, dict) or value.get("name") != "codex-baron":
        raise ValueError("plugin manifest name must be codex-baron")
    version = value.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version.split("+", 1)[0]):
        raise ValueError("plugin manifest version must have a SemVer base (MAJOR.MINOR.PATCH)")
    value["version"] = version.split("+", 1)[0]
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _files(repo: Path) -> list[Path]:
    plugin = repo / PLUGIN
    symlinks = [path for path in plugin.rglob("*") if path.is_symlink()]
    if symlinks:
        raise ValueError(f"plugin release input must not be a symlink: {symlinks[0]}")
    paths = [
        path
        for path in plugin.rglob("*")
        if path.is_file()
    ]
    marketplace = repo / MARKETPLACE
    if not marketplace.is_file() or marketplace.is_symlink():
        raise ValueError(f"marketplace manifest is missing or unsafe: {marketplace}")
    paths.append(marketplace)
    return sorted(path for path in paths if "__pycache__" not in path.parts and path.suffix != ".pyc")


def fingerprint(repo: Path) -> str:
    digest = hashlib.sha256()
    for path in _files(repo):
        relative = path.relative_to(repo).as_posix()
        try:
            data = (
                _manifest_data(path)
                if path == repo / PLUGIN / ".codex-plugin" / "plugin.json"
                else path.read_bytes()
            )
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
        except OSError as exc:
            raise ValueError(f"cannot read release input: {path}") from exc
        git_mode = b"100755" if executable else b"100644"
        digest.update(relative.encode("utf-8") + b"\0" + git_mode + b"\0" + data + b"\0")
    return f"sha256-{digest.hexdigest()}"


def stamp(repo: Path) -> bool:
    manifest = repo / PLUGIN / ".codex-plugin" / "plugin.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid plugin manifest: {manifest}") from exc
    if not isinstance(value, dict) or value.get("name") != "codex-baron":
        raise ValueError("plugin manifest name must be codex-baron")
    version = value.get("version")
    if not isinstance(version, str):
        raise ValueError("plugin manifest version must be a string")
    base = version.split("+", 1)[0]
    if not SEMVER.fullmatch(base):
        raise ValueError("plugin manifest version must have a SemVer base (MAJOR.MINOR.PATCH)")
    updated = f"{base}+codex.{fingerprint(repo)[7:]}"
    if version == updated:
        return False
    value["version"] = updated
    data = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    try:
        mode = manifest.stat().st_mode & 0o777
    except OSError as exc:
        raise ValueError(f"cannot stat plugin manifest: {manifest}") from exc
    descriptor, temporary = tempfile.mkstemp(prefix=f".{manifest.name}.", dir=manifest.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, manifest)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        changed = stamp(args.repo.resolve())
    except ValueError as exc:
        parser.error(str(exc))
    print("stamped plugin manifest" if changed else "plugin manifest already stamped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
