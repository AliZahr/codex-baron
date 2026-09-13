#!/usr/bin/env python3
"""Install, upgrade, or remove Codex Baron repository configuration safely."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
from typing import Literal


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "codex-baron"
CONFIG_RELATIVE = PurePosixPath(".codex/codex-baron.json")
MANIFEST_RELATIVE = PurePosixPath(".codex/codex-baron-managed.json")
MANIFEST_SCHEMA_VERSION = 1
SHA256_HEX_LENGTH = 64
MAX_MANAGED_FILE_BYTES = 1024 * 1024
ROLES = ("bulk_reader", "code_writer", "test_writer", "senior_reviewer")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class InstallError(Exception):
    """Expected safety, conflict, or transaction failure."""


@dataclass(frozen=True)
class Snapshot:
    data: bytes | None
    mode: int | None = None

    @property
    def exists(self) -> bool:
        return self.data is not None


@dataclass(frozen=True)
class Action:
    kind: Literal["write", "remove"]
    relative: PurePosixPath
    before: Snapshot
    data: bytes | None = None
    mode: int = 0o644


@dataclass(frozen=True)
class ManagedRecord:
    sha256: str
    mode: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_text(relative: PurePosixPath) -> str:
    return relative.as_posix()


def _validate_relative(relative: PurePosixPath, *, allow_manifest: bool = False) -> None:
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise InstallError(f"unsafe managed path: {_relative_text(relative)}")
    allowed = (
        relative == CONFIG_RELATIVE
        or (
            len(relative.parts) == 3
            and relative.parts[:2] == (".codex", "agents")
            and relative.suffix == ".toml"
        )
        or (allow_manifest and relative == MANIFEST_RELATIVE)
    )
    if not allowed:
        raise InstallError(f"manifest contains an unmanaged path: {_relative_text(relative)}")


def _target(repo: Path, relative: PurePosixPath, *, allow_manifest: bool = False) -> Path:
    _validate_relative(relative, allow_manifest=allow_manifest)
    target = repo.joinpath(*relative.parts)
    try:
        target.resolve(strict=False).relative_to(repo)
    except (OSError, ValueError) as exc:
        raise InstallError(f"managed path escapes repository: {_relative_text(relative)}") from exc
    return target


def _open_root(repo: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(repo, flags)
    except OSError as exc:
        raise InstallError(f"cannot open repository safely: {repo}: {exc}") from exc


def _open_parent(repo: Path, relative: PurePosixPath, *, create: bool) -> int | None:
    """Open a stable parent directory descriptor without following symlinks."""

    _target(repo, relative, allow_manifest=relative == MANIFEST_RELATIVE)
    descriptor = _open_root(repo)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise InstallError(
                    f"unsafe or inaccessible path component for {_relative_text(relative)}: {component}: {exc}"
                ) from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise InstallError(
            f"cannot traverse parent for {_relative_text(relative)} without following links: {exc}"
        ) from exc
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _snapshot(repo: Path, relative: PurePosixPath, *, allow_manifest: bool = False) -> Snapshot:
    _target(repo, relative, allow_manifest=allow_manifest)
    parent = _open_parent(repo, relative, create=False)
    if parent is None:
        return Snapshot(None)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(relative.name, flags, dir_fd=parent)
        except FileNotFoundError:
            return Snapshot(None)
        except OSError as exc:
            raise InstallError(f"cannot read {_relative_text(relative)} safely: {exc}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise InstallError(f"managed target is not a regular file: {_relative_text(relative)}")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_MANAGED_FILE_BYTES:
                    raise InstallError(
                        f"managed target exceeds {MAX_MANAGED_FILE_BYTES} bytes: {_relative_text(relative)}"
                    )
                chunks.append(chunk)
            return Snapshot(b"".join(chunks), stat.S_IMODE(metadata.st_mode))
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _atomic_write(repo: Path, relative: PurePosixPath, data: bytes, mode: int) -> None:
    """Write through stable directory descriptors and atomically replace the leaf."""

    _target(repo, relative, allow_manifest=relative == MANIFEST_RELATIVE)
    parent = _open_parent(repo, relative, create=True)
    if parent is None:  # pragma: no cover - create=True always returns a descriptor
        raise InstallError(f"cannot create parent for {_relative_text(relative)}")
    temporary = f".{relative.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, mode, dir_fd=parent)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except OSError as exc:
        raise InstallError(f"cannot write {_relative_text(relative)} safely: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(parent)


def _safe_unlink(repo: Path, relative: PurePosixPath) -> None:
    _target(repo, relative, allow_manifest=relative == MANIFEST_RELATIVE)
    parent = _open_parent(repo, relative, create=False)
    if parent is None:
        return
    try:
        try:
            metadata = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise InstallError(f"refusing to unlink non-regular target: {_relative_text(relative)}")
        os.unlink(relative.name, dir_fd=parent)
        os.fsync(parent)
    except OSError as exc:
        raise InstallError(f"cannot remove {_relative_text(relative)} safely: {exc}") from exc
    finally:
        os.close(parent)


def _same_snapshot(left: Snapshot, right: Snapshot) -> bool:
    return left.data == right.data and left.mode == right.mode


def _apply_transaction(repo: Path, actions: list[Action]) -> None:
    applied: list[Action] = []
    try:
        for action in actions:
            current = _snapshot(
                repo,
                action.relative,
                allow_manifest=action.relative == MANIFEST_RELATIVE,
            )
            if not _same_snapshot(current, action.before):
                raise InstallError(
                    f"{_relative_text(action.relative)} changed after preflight; no further changes were applied"
                )
            # Record the rollback snapshot before starting the mutation. The
            # low-level helper can fail after an atomic replace/unlink (for
            # example while syncing the directory), and that still needs to be
            # rolled back.
            applied.append(action)
            if action.kind == "write":
                if action.data is None:  # pragma: no cover - planner invariant
                    raise InstallError(f"missing write data for {_relative_text(action.relative)}")
                _atomic_write(repo, action.relative, action.data, action.mode)
            else:
                _safe_unlink(repo, action.relative)
    except Exception as exc:
        rollback_errors: list[str] = []
        for action in reversed(applied):
            try:
                if action.before.exists:
                    _atomic_write(
                        repo,
                        action.relative,
                        action.before.data or b"",
                        action.before.mode if action.before.mode is not None else 0o644,
                    )
                else:
                    _safe_unlink(repo, action.relative)
            except Exception as rollback_exc:  # pragma: no cover - exceptional disk failure
                rollback_errors.append(f"{_relative_text(action.relative)}: {rollback_exc}")
        message = str(exc)
        if rollback_errors:
            message += "; rollback incomplete: " + "; ".join(rollback_errors)
        raise InstallError(message) from exc


def _plugin_identity() -> tuple[str, str]:
    manifest_path = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"bundled plugin manifest is unavailable or invalid: {manifest_path}") from exc
    if not isinstance(value, dict) or value.get("name") != PLUGIN_NAME:
        raise InstallError("bundled plugin manifest name does not match Codex Baron")
    version = value.get("version")
    if not isinstance(version, str) or not version:
        raise InstallError("bundled plugin manifest has no valid version")
    return PLUGIN_NAME, version


def _profile_bytes(data: bytes, role: str, models: dict[str, str], reasoning: dict[str, str], fast: bool) -> bytes:
    text = data.decode("utf-8")
    if role in models:
        text = text.replace(f'model = "{_toml_value(text, "model")}"', f'model = "{models[role]}"', 1)
    if role in reasoning:
        text = text.replace(
            f'model_reasoning_effort = "{_toml_value(text, "model_reasoning_effort")}"',
            f'model_reasoning_effort = "{reasoning[role]}"',
            1,
        )
    lines = text.splitlines()
    lines = [line for line in lines if not line.startswith("service_tier =")]
    if fast:
        for index, line in enumerate(lines):
            if line.startswith("model_reasoning_effort ="):
                lines.insert(index + 1, 'service_tier = "fast"')
                break
    return ("\n".join(lines) + "\n").encode("utf-8")


def _toml_value(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{key} = "):
            return line.split('"', 2)[1]
    raise InstallError(f"bundled agent profile has no {key}: {key}")


def _source_files(
    models: dict[str, str] | None = None,
    reasoning: dict[str, str] | None = None,
    fast: bool = False,
) -> dict[PurePosixPath, tuple[bytes, int]]:
    models = models or {}
    reasoning = reasoning or {}
    sources = sorted((PLUGIN_ROOT / "agents").glob("*.toml"))
    if not sources:
        raise InstallError("Bundled agent profiles are missing; reinstall Codex Baron.")
    default_config = PLUGIN_ROOT / "config" / "router.json"
    if not default_config.is_file():
        raise InstallError("Bundled router config is missing; reinstall Codex Baron.")
    result: dict[PurePosixPath, tuple[bytes, int]] = {}
    try:
        for source in sources:
            relative = PurePosixPath(".codex/agents") / source.name
            result[relative] = (
                _profile_bytes(source.read_bytes(), source.stem, models, reasoning, fast),
                stat.S_IMODE(source.stat().st_mode),
            )
        config_data = default_config.read_bytes()
        if "bulk_reader" in models:
            config = json.loads(config_data.decode("utf-8"))
            config["bulk_reader_model"] = models["bulk_reader"]
            config_data = (json.dumps(config, indent=2) + "\n").encode("utf-8")
        result[CONFIG_RELATIVE] = (config_data, stat.S_IMODE(default_config.stat().st_mode))
    except OSError as exc:
        raise InstallError(f"cannot read bundled repository configuration: {exc}") from exc
    return result


def _manifest_bytes(version: str, sources: dict[PurePosixPath, tuple[bytes, int]]) -> bytes:
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "plugin": {"name": PLUGIN_NAME, "version": version},
        "managed_files": {
            _relative_text(relative): {"sha256": _sha256(data), "mode": mode}
            for relative, (data, mode) in sorted(sources.items(), key=lambda item: _relative_text(item[0]))
        },
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_managed_manifest(repo: Path) -> tuple[Snapshot, str | None, dict[PurePosixPath, ManagedRecord]]:
    snapshot = _snapshot(repo, MANIFEST_RELATIVE, allow_manifest=True)
    if not snapshot.exists:
        return snapshot, None, {}
    try:
        value = json.loads((snapshot.data or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(
            f"managed install manifest is invalid: {_relative_text(MANIFEST_RELATIVE)}; remove it only after review"
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise InstallError("managed install manifest has an unsupported schema")
    plugin = value.get("plugin")
    if not isinstance(plugin, dict) or plugin.get("name") != PLUGIN_NAME:
        raise InstallError("managed install manifest belongs to a different plugin")
    version = plugin.get("version")
    if not isinstance(version, str) or not version:
        raise InstallError("managed install manifest has no valid plugin version")
    raw_files = value.get("managed_files")
    if not isinstance(raw_files, dict):
        raise InstallError("managed install manifest has no valid file inventory")
    files: dict[PurePosixPath, ManagedRecord] = {}
    for raw_relative, metadata in raw_files.items():
        if not isinstance(raw_relative, str) or not isinstance(metadata, dict):
            raise InstallError("managed install manifest contains an invalid file entry")
        relative = PurePosixPath(raw_relative)
        _validate_relative(relative)
        digest = metadata.get("sha256")
        mode = metadata.get("mode")
        if (
            not isinstance(digest, str)
            or len(digest) != SHA256_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise InstallError(f"invalid digest for {_relative_text(relative)}")
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise InstallError(f"invalid mode for {_relative_text(relative)}")
        files[relative] = ManagedRecord(digest, mode)
    return snapshot, version, files


def _plan_install(
    repo: Path,
    sources: dict[PurePosixPath, tuple[bytes, int]],
    version: str,
    force: bool,
) -> tuple[list[Action], list[str], list[str]]:
    manifest_snapshot, previous_version, previous = _load_managed_manifest(repo)
    actions: list[Action] = []
    messages: list[str] = []
    conflicts: list[str] = []

    for relative, (source_data, source_mode) in sorted(sources.items(), key=lambda item: _relative_text(item[0])):
        current = _snapshot(repo, relative)
        current_digest = _sha256(current.data) if current.exists else None
        source_digest = _sha256(source_data)
        previous_record = previous.get(relative)
        target = _target(repo, relative)
        if not current.exists:
            actions.append(Action("write", relative, current, source_data, source_mode))
            messages.append(f"install {_relative_text(relative)} -> {target}")
        elif current_digest == source_digest and current.mode == source_mode:
            messages.append(f"unchanged {target}")
        elif force or (
            previous_record is not None
            and current_digest == previous_record.sha256
            and current.mode == previous_record.mode
        ):
            actions.append(Action("write", relative, current, source_data, source_mode))
            messages.append(f"upgrade {_relative_text(relative)} -> {target}")
        else:
            context = "untracked existing file" if previous_version is None else "locally customized file"
            conflicts.append(f"{target} ({context})")

    for relative, previous_record in sorted(previous.items(), key=lambda item: _relative_text(item[0])):
        if relative in sources:
            continue
        current = _snapshot(repo, relative)
        if not current.exists:
            continue
        if force or (
            _sha256(current.data or b"") == previous_record.sha256
            and current.mode == previous_record.mode
        ):
            actions.append(Action("remove", relative, current))
            messages.append(f"remove obsolete {_target(repo, relative)}")
        else:
            conflicts.append(f"{_target(repo, relative)} (customized obsolete managed file)")

    if conflicts:
        return [], messages, conflicts
    manifest_data = _manifest_bytes(version, sources)
    if manifest_snapshot.data != manifest_data or manifest_snapshot.mode != 0o600:
        actions.append(Action("write", MANIFEST_RELATIVE, manifest_snapshot, manifest_data, 0o600))
        messages.append(f"record managed state -> {_target(repo, MANIFEST_RELATIVE, allow_manifest=True)}")
    return actions, messages, []


def _plan_remove(
    repo: Path,
    sources: dict[PurePosixPath, tuple[bytes, int]],
    force: bool,
) -> tuple[list[Action], list[str], list[str]]:
    manifest_snapshot, _previous_version, previous = _load_managed_manifest(repo)
    actions: list[Action] = []
    messages: list[str] = []
    conflicts: list[str] = []
    candidates = set(previous)
    if not previous:
        candidates.update(sources)
    if force:
        candidates.update(sources)

    for relative in sorted(candidates, key=_relative_text):
        current = _snapshot(repo, relative)
        if not current.exists:
            continue
        expected = previous.get(relative)
        if expected is None and relative in sources:
            expected = ManagedRecord(_sha256(sources[relative][0]), sources[relative][1])
        if force or (
            expected is not None
            and _sha256(current.data or b"") == expected.sha256
            and current.mode == expected.mode
        ):
            actions.append(Action("remove", relative, current))
            messages.append(f"remove {_target(repo, relative)}")
        else:
            conflicts.append(f"{_target(repo, relative)} (locally customized file)")

    if conflicts:
        return [], messages, conflicts
    if manifest_snapshot.exists:
        actions.append(Action("remove", MANIFEST_RELATIVE, manifest_snapshot))
        messages.append(f"remove {_target(repo, MANIFEST_RELATIVE, allow_manifest=True)}")
    return actions, messages, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="Repository root to configure")
    parser.add_argument("--force", action="store_true", help="Replace or remove known managed targets after review")
    parser.add_argument("--dry-run", action="store_true", help="Show the complete plan without writing")
    parser.add_argument(
        "--model", action="append", default=[], metavar="ROLE=MODEL", help="Override a role's model"
    )
    parser.add_argument(
        "--reasoning",
        action="append",
        default=[],
        metavar="ROLE=EFFORT",
        help="Override a role's reasoning effort",
    )
    speed = parser.add_mutually_exclusive_group()
    speed.add_argument(
        "--fast",
        action="store_true",
        help='Add service_tier = "fast" to all profiles (Codex maps it to priority processing)',
    )
    speed.add_argument("--no-fast", action="store_true", help="Use the standard service tier")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove files unchanged since their recorded managed version (customized files require --force)",
    )
    args = parser.parse_args(argv)
    models: dict[str, str] = {}
    reasoning: dict[str, str] = {}
    def assignments(values: list[str], label: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for value in values:
            if value.count("=") != 1:
                parser.error(f"invalid {label} assignment {value!r}; expected ROLE=VALUE")
            role, setting = value.split("=", 1)
            if role not in ROLES:
                parser.error(f"unknown role {role!r}; expected one of: {', '.join(ROLES)}")
            if not setting:
                parser.error(f"empty {label} for role {role!r}")
            if label == "model" and not MODEL_ID_PATTERN.fullmatch(setting):
                parser.error(
                    f"invalid model ID {setting!r}; use letters, digits, '.', '_', ':', or '-'"
                )
            result[role] = setting
        return result
    models = assignments(args.model, "model")
    reasoning = assignments(args.reasoning, "reasoning")
    for role, effort in reasoning.items():
        if effort not in REASONING_EFFORTS:
            parser.error(f"unsupported reasoning value {effort!r}; expected one of: {', '.join(REASONING_EFFORTS)}")
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        parser.error(f"repository directory does not exist: {repo}")

    try:
        sources = _source_files(models, reasoning, args.fast)
        _name, version = _plugin_identity()
        if args.remove:
            actions, messages, conflicts = _plan_remove(repo, sources, args.force)
        else:
            actions, messages, conflicts = _plan_install(repo, sources, version, args.force)
        if conflicts:
            operation = "remove customized managed files" if args.remove else "overwrite existing customized files"
            print(f"Refusing to {operation}; pass --force only after review:", file=sys.stderr)
            for conflict in conflicts:
                print(f"  {conflict}", file=sys.stderr)
            return 2
        for message in messages:
            print(message)
        if args.dry_run:
            print("Dry run complete; no files or directories were changed.")
            return 0
        _apply_transaction(repo, actions)
    except (InstallError, OSError) as exc:
        print(f"Codex Baron repository configuration failed: {exc}", file=sys.stderr)
        return 2

    if args.remove:
        print("Removed Baron-managed repository configuration. Empty directories were preserved.")
    else:
        print(
            "Configured transactionally. Review and commit .codex/ if these policies should apply to the whole team."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
