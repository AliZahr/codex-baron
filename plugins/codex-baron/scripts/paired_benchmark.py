#!/usr/bin/env python3
"""Run a controlled baseline/Baron pair and enforce token-efficiency gates.

The comparison manifest is intentionally an attestation: it contains hashes,
bounded metadata, and aggregate counters, never the prompt, commands, or raw
Codex event payloads.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib

from usage_meter import (
    build_comparison,
    combine_usage,
    parse_rollout_usage_file,
    parse_usage_file,
    render_meter,
)
from router import recommend_route
from router_hook import _data_root, inspect_unified_exec_evidence, is_bulk_reader_role_marker


PLUGIN_ID = "codex-baron@codex-baron"
PLUGIN_OVERRIDE = f"plugins.{PLUGIN_ID}.enabled"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
OUTPUT_MIN_WAIT_SECONDS = 15.0
OUTPUT_SETTLE_SECONDS = 5.0
OUTPUT_SETTLE_TIMEOUT_SECONDS = 120.0
DEFAULT_RUN_TIMEOUT_SECONDS = 4 * 60 * 60
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")
PLUGIN_LIST_ROW = re.compile(
    rf"^{re.escape(PLUGIN_ID)}\s+installed(?:,\s+\S+)?\s+(?P<version>\S+)\s+(?P<path>.+?)\s*$",
    re.MULTILINE,
)
MARKETPLACE_HEADER = re.compile(r"^Marketplace `(?P<name>[A-Za-z0-9_-]+)`$")
RUNTIME_COMPONENTS = (".codex-plugin/plugin.json", "hooks", "skills", "agents", "config", "scripts")
SUPPORTED_REASONING = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
DIRECTIVE_WORDS = re.compile(
    r"(?i)(?<![a-z0-9_])(?:codex\s+baron|baron|bulk[_ -]?reader|code[_ -]?writer|test[_ -]?writer|"
    r"senior[_ -]?reviewer|subagents?|sub-agents?|delegate|delegation|parallel\s+worker)(?![a-z0-9_])"
)
COLLAB_SPAWN_TOOLS = {"spawn", "spawn_agent"}
COLLAB_WAIT_TOOLS = {"wait", "wait_agent", "wait_threads"}
REQUIRED_HOOK_EVENTS = ("pre_tool_use", "user_prompt_submit", "subagent_start", "subagent_stop")


@dataclass(frozen=True)
class RunRecord:
    label: str
    elapsed_seconds: float
    jsonl: str
    stderr: str


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return completed.stdout


def repository_fingerprint(repo: Path) -> dict[str, str]:
    """Return a path-free fingerprint of the checkout and its status."""

    try:
        head = _git(repo, "rev-parse", "--verify", "HEAD").strip()
        status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Not a usable Git repository: {repo}") from exc
    return {
        "head": head,
        "status": "clean" if not status else "dirty",
        # Proves the same status was observed without copying filenames into
        # a manifest.
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def require_clean_repository(repo: Path) -> str:
    """Return HEAD when repo is clean; refuse incomparable mutable inputs."""

    fingerprint = repository_fingerprint(repo)
    if fingerprint["status"] != "clean":
        raise ValueError(
            "Repository must be clean so both runs see identical files. Commit, stash, or use a disposable worktree."
        )
    return fingerprint["head"]


def repository_status_sha256(repo: Path) -> str:
    try:
        status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Cannot inspect repository status: {repo}") from exc
    return hashlib.sha256(status.encode("utf-8")).hexdigest()


def plugin_version() -> str:
    try:
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        if manifest.get("name") != "codex-baron":
            raise ValueError("plugin manifest name mismatch")
        version = manifest.get("version")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read plugin version: {PLUGIN_MANIFEST}") from exc
    if not isinstance(version, str) or not version:
        raise ValueError(f"Plugin manifest has no valid version: {PLUGIN_MANIFEST}")
    return version


def _codex_plugin_list() -> str:
    # Scope the query to the local Baron marketplace. An unscoped list also
    # contacts the remote catalog and can block attestation when offline.
    try:
        completed = subprocess.run(
            ["codex", "plugin", "list", "--marketplace", "codex-baron"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Cannot verify the installed Codex Baron version") from exc
    if completed.returncode:
        raise ValueError("Codex plugin list failed; cannot verify the installed plugin")
    return completed.stdout


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def verify_plugin_toggle() -> None:
    """Prove that both benchmark overrides select the requested plugin state."""

    for enabled in (False, True):
        override = f"{PLUGIN_OVERRIDE}={str(enabled).lower()}"
        try:
            completed = subprocess.run(
                [
                    "codex", "plugin", "list", "--marketplace", "codex-baron",
                    "--json", "-c", override,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Cannot verify the Codex Baron enable/disable override") from exc
        if completed.returncode:
            raise ValueError("Codex Baron enable/disable override check failed")
        try:
            payload = json.loads(completed.stdout, object_pairs_hook=_strict_json_object)
            matches = [
                entry for entry in payload.get("installed", [])
                if entry.get("pluginId") == PLUGIN_ID
            ]
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("Codex Baron enable/disable override returned invalid state") from exc
        if len(matches) != 1 or matches[0].get("enabled") is not enabled:
            raise ValueError("Codex Baron enable/disable override did not take effect")


def verify_hooks_enabled() -> dict[str, object]:
    """Fail closed unless every Baron hook is explicitly trusted and enabled."""

    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    config_path = codex_home / "config.toml"
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
        states = config["hooks"]["state"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(
            "Cannot verify Baron hooks; enable and trust all Codex Baron hooks in Codex settings"
        ) from exc
    if not isinstance(states, dict):
        raise ValueError("Codex hook state is malformed")
    verified: list[str] = []
    for event in REQUIRED_HOOK_EVENTS:
        matches = [
            value for key, value in states.items()
            if isinstance(key, str) and key.startswith(f"{PLUGIN_ID}:") and f":{event}:" in key
        ]
        if len(matches) != 1 or not isinstance(matches[0], dict):
            raise ValueError(
                f"Codex Baron {event} hook is missing or ambiguous; re-enable and trust the plugin hooks"
            )
        state = matches[0]
        trusted_hash = state.get("trusted_hash")
        if state.get("enabled") is not True or not isinstance(trusted_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", trusted_hash):
            raise ValueError(
                f"Codex Baron {event} hook is disabled or untrusted; enable it in Codex settings"
            )
        verified.append(event)
    return {"configured_enabled": True, "verified_events": verified}


def installed_plugin_info() -> tuple[str, Path]:
    """Return the installed version and the cache tree Codex executes."""

    output = _codex_plugin_list()
    marketplace: str | None = None
    match: re.Match[str] | None = None
    current_marketplace: str | None = None
    for line in output.splitlines():
        header = MARKETPLACE_HEADER.fullmatch(line.strip())
        if header:
            current_marketplace = header.group("name")
            continue
        candidate = PLUGIN_LIST_ROW.fullmatch(line)
        if candidate:
            match = candidate
            marketplace = current_marketplace
            break
    if match is None or marketplace is None:
        raise ValueError(
            "Codex Baron is not installed from the expected marketplace; run `codex plugin list` and reinstall it"
        )
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    runtime = codex_home / "plugins" / "cache" / marketplace / "codex-baron" / match.group("version")
    if not runtime.is_dir():
        raise ValueError("Codex Baron installed runtime path is missing or unverifiable")
    return match.group("version"), runtime.resolve()


def installed_plugin_version() -> str:
    """Return the version Codex will actually load, or fail the attestation."""

    return installed_plugin_info()[0]


def canonical_runtime_digest(root: str | Path) -> str:
    """Digest the exact plugin runtime bytes in a stable path order."""

    base = Path(root).expanduser().resolve()
    files: list[tuple[str, Path]] = []
    for component in RUNTIME_COMPONENTS:
        component_path = base / component
        if component_path.is_symlink():
            raise ValueError(f"Plugin runtime contains unverifiable symlink: {component}")
        if component_path.is_file():
            candidates = [component_path]
        elif component_path.is_dir():
            candidates = [
                path for path in component_path.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
            ]
        else:
            raise ValueError(f"Plugin runtime component is missing: {component}")
        for path in candidates:
            if path.is_symlink():
                raise ValueError(f"Plugin runtime contains unverifiable symlink: {component}")
            files.append((path.relative_to(base).as_posix(), path))
    if not files:
        raise ValueError("Plugin runtime contains no attested files")
    digest = hashlib.sha256()
    for relative, path in sorted(files):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def codex_cli_version() -> str:
    try:
        completed = subprocess.run(
            ["codex", "--version"], check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Cannot verify Codex CLI version") from exc
    version = (completed.stdout or completed.stderr).strip().splitlines()
    if completed.returncode or not version or not version[0].strip():
        raise ValueError("Codex CLI version is unavailable")
    return version[0].strip()


def validate_prompt_neutrality(prompt: str) -> None:
    if DIRECTIVE_WORDS.search(prompt):
        raise ValueError("benchmark prompt contains Baron/worker/delegation directives; use a neutral prompt")


def runtime_attestation() -> dict[str, str]:
    """Verify source bytes are exactly the bytes Codex will load."""

    source_digest = canonical_runtime_digest(PLUGIN_ROOT)
    installed_version, installed_root = installed_plugin_info()
    installed_digest = canonical_runtime_digest(installed_root)
    version = plugin_version()
    if installed_version != version:
        raise ValueError(
            f"Installed Codex Baron version {installed_version} does not match runner version {version}"
        )
    if installed_digest != source_digest:
        raise ValueError("Installed Codex Baron runtime digest is stale; reinstall before benchmarking")
    return {
        "source_runtime_sha256": source_digest,
        "installed_runtime_sha256": installed_digest,
        "installed_plugin_version": installed_version,
        "codex_cli_version": codex_cli_version(),
    }


def attested_skill_path(attestation: dict[str, str]) -> Path:
    """Resolve the one installed skill path bound to a runtime attestation."""

    installed_version, installed_root = installed_plugin_info()
    if installed_version != attestation.get("installed_plugin_version"):
        raise ValueError("Installed Codex Baron version changed after runtime attestation")
    if canonical_runtime_digest(installed_root) != attestation.get("installed_runtime_sha256"):
        raise ValueError("Installed Codex Baron runtime changed after runtime attestation")
    skill = installed_root / "skills" / "codex-baron" / "SKILL.md"
    if not skill.is_file() or skill.is_symlink():
        raise ValueError("Installed Codex Baron instruction path is missing or unverifiable")
    return skill.resolve()


def verify_agent_profile(repo: Path, name: str = "bulk_reader") -> dict[str, object]:
    """Verify the effective custom-agent profile matches the reviewed bundle."""

    bundled = PLUGIN_ROOT / "agents" / f"{name}.toml"
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    project_profile = repo / ".codex" / "agents" / f"{name}.toml"
    user_profile = codex_home / "agents" / f"{name}.toml"
    effective = project_profile if project_profile.exists() else user_profile
    source = "project" if project_profile.exists() else "user"
    try:
        if not bundled.is_file() or not effective.is_file() or effective.is_symlink():
            raise ValueError
        expected = hashlib.sha256(bundled.read_bytes()).hexdigest()
        observed = hashlib.sha256(effective.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Codex Baron requires a reviewed {name} profile in .codex/agents or ~/.codex/agents"
        ) from exc
    if not hmac.compare_digest(expected, observed):
        raise ValueError(
            f"Effective {name} profile differs from the installed Codex Baron bundle; rerun configure_repo.py"
        )
    return {"required": True, "name": name, "source": source, "sha256": observed, "verified": True}


def normalize_tools(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value.split(",") if isinstance(value, str) else list(value)
    tools = tuple(item.strip() for item in values if item.strip())
    if any(not TOOL_NAME.fullmatch(item) for item in tools):
        raise ValueError("tools must be comma-separated names (letters, digits, ., _, :, or -)")
    return tuple(dict.fromkeys(tools))


def build_codex_command(
    repo: Path,
    prompt: str,
    model: str,
    sandbox: str,
    baron_enabled: bool,
    ephemeral: bool,
    *,
    reasoning_effort: str | None = None,
    tools: tuple[str, ...] = (),
) -> list[str]:
    """Build an argument-based command; prompt is always one final argument."""

    command = [
        "codex", "exec", "--json", "-C", str(repo), "--sandbox", sandbox,
        "-m", model,
        "-c", f"{PLUGIN_OVERRIDE}={str(baron_enabled).lower()}",
    ]
    # These are Codex configuration keys, rather than guessed CLI switches.
    if reasoning_effort is not None:
        command.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
    # Tool availability is inherited from the Codex runtime. Do not emit a
    # guessed `tools=[]` config key: unsupported overrides make pairs lie about
    # their effective tool set.
    if ephemeral:
        command.append("--ephemeral")
    command.append(prompt)
    return command


def wait_for_quiescent_output(
    path: Path,
    minimum_wait: float = OUTPUT_MIN_WAIT_SECONDS,
    settle_seconds: float = OUTPUT_SETTLE_SECONDS,
    timeout: float = OUTPUT_SETTLE_TIMEOUT_SECONDS,
) -> None:
    """Wait for detached agent writers that can outlive ``codex exec``."""

    if min(minimum_wait, settle_seconds, timeout) < 0 or timeout < minimum_wait:
        raise ValueError("invalid output settle timeouts")
    started = time.monotonic()
    last_change = started
    try:
        signature = (path.stat().st_size, path.stat().st_mtime_ns)
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect benchmark output: {exc}") from exc
    while True:
        now = time.monotonic()
        try:
            current = (path.stat().st_size, path.stat().st_mtime_ns)
        except OSError as exc:
            raise RuntimeError(f"Cannot inspect benchmark output: {exc}") from exc
        if current != signature:
            signature = current
            last_change = now
        if now - started >= minimum_wait and now - last_change >= settle_seconds:
            return
        if now - started >= timeout:
            raise RuntimeError(f"Benchmark output did not settle within {timeout:.0f}s")
        time.sleep(0.25)


def run_one(
    label: str,
    repo: Path,
    prompt: str,
    model: str,
    sandbox: str,
    output_dir: Path,
    ephemeral: bool,
    *,
    reasoning_effort: str | None = None,
    tools: tuple[str, ...] = (),
    timeout: float = DEFAULT_RUN_TIMEOUT_SECONDS,
) -> RunRecord:
    if timeout <= 0:
        raise ValueError("run timeout must be positive")
    enabled = label == "baron"
    jsonl_path = output_dir / f"{label}.jsonl"
    stderr_path = output_dir / f"{label}.stderr.txt"
    started = time.monotonic()
    try:
        with jsonl_path.open("wb") as output:
            completed = subprocess.run(
                build_codex_command(
                    repo, prompt, model, sandbox, enabled, ephemeral,
                    reasoning_effort=reasoning_effort, tools=tools,
                ), stdout=output, stderr=subprocess.PIPE, text=True, timeout=timeout
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} run timed out after {timeout:.0f}s") from exc
    wait_for_quiescent_output(jsonl_path)
    elapsed = time.monotonic() - started
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        tail = completed.stderr.strip()[-1000:]
        raise RuntimeError(f"{label} run failed with exit {completed.returncode}: {tail}")
    return RunRecord(label, elapsed, str(jsonl_path), str(stderr_path))


def artifact_descriptor(path: str | Path, kind: str) -> dict[str, object]:
    """Describe an artifact without exposing its path or contents."""

    source = Path(path)
    try:
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return {"kind": kind, "name": source.name, "size_bytes": size, "sha256": digest.hexdigest()}
    except OSError:
        # Useful for mocked callers and incomplete failure reports. Real runs
        # validate the JSONL before writing a successful comparison manifest.
        return {"kind": kind, "name": source.name, "available": False}


def _inspect_jsonl_delegation_evidence(path: str | Path) -> dict[str, object]:
    """Return privacy-safe evidence for a completed worker handoff.

    Hooks are advisory on runtimes that do not expose every collaboration call
    to ``PreToolUse``. The benchmark therefore verifies the emitted JSONL too:
    one completed spawn must return a nonempty worker ID, and a subsequent wait
    must either target exactly that worker or use the mailbox-only wait API.
    """

    completed_spawns = 0
    completed_waits = 0
    valid_spawn_ids: set[str] = set()
    bulk_reader_spawns = 0
    valid_waits = 0
    empty_target_waits = 0
    invalid_waits = 0
    failed_collaboration_calls = 0
    collaboration_records = 0
    malformed_records = 0
    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot inspect delegation evidence: {source.name}") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line, object_pairs_hook=_strict_json_object)
        except (json.JSONDecodeError, ValueError):
            malformed_records += 1
            continue
        if not isinstance(record, dict) or record.get("type") not in {"item.started", "item.completed"}:
            continue
        item = record.get("item")
        if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
            continue
        collaboration_records += 1
        if record.get("type") != "item.completed":
            continue
        tool = str(item.get("tool") or "").strip().lower().replace("-", "_")
        receivers = item.get("receiver_thread_ids")
        receiver_ids = {
            value for value in receivers
            if isinstance(receivers, list) and isinstance(value, str) and value.strip()
        } if isinstance(receivers, list) else set()
        if item.get("status") == "failed":
            failed_collaboration_calls += 1
            continue
        if tool in COLLAB_SPAWN_TOOLS:
            completed_spawns += 1
            if is_bulk_reader_role_marker(
                agent_type=item.get("agent_type"), task_name=item.get("task_name")
            ):
                bulk_reader_spawns += 1
                valid_spawn_ids.update(receiver_ids)
        elif tool in COLLAB_WAIT_TOOLS:
            completed_waits += 1
            if tool == "wait_agent":
                if valid_spawn_ids:
                    valid_waits += 1
                else:
                    invalid_waits += 1
            elif receiver_ids and receiver_ids == valid_spawn_ids:
                valid_waits += 1
            else:
                invalid_waits += 1
                empty_target_waits += int(not receiver_ids)
    reasons: list[str] = []
    if malformed_records:
        reasons.append("malformed JSONL records prevent delegation verification")
    if completed_spawns != 1 or bulk_reader_spawns != 1 or len(valid_spawn_ids) != 1:
        reasons.append("expected exactly one completed bulk_reader spawn with one nonempty worker ID")
    if valid_waits < 1:
        reasons.append("no completed wait was bound to the spawned worker")
    if empty_target_waits:
        reasons.append("generic collaboration wait had no worker target")
    if invalid_waits:
        reasons.append("one or more waits occurred before spawn or targeted the wrong worker")
    if failed_collaboration_calls:
        reasons.append("one or more collaboration calls failed")
    return {
        "verified": not reasons,
        "completed_spawns": completed_spawns,
        "bulk_reader_spawns": bulk_reader_spawns,
        "spawned_workers": len(valid_spawn_ids),
        "completed_waits": completed_waits,
        "valid_waits": valid_waits,
        "empty_target_waits": empty_target_waits,
        "invalid_waits": invalid_waits,
        "failed_collaboration_calls": failed_collaboration_calls,
        "collaboration_records": collaboration_records,
        "malformed_records": malformed_records,
        "failures": reasons,
    }


def _thread_id(path: str | Path) -> str | None:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for _ in range(32):
                line = handle.readline(1024 * 1024)
                if not line:
                    break
                record = json.loads(line, object_pairs_hook=_strict_json_object)
                if isinstance(record, dict) and record.get("type") == "thread.started":
                    value = record.get("thread_id")
                    return value if isinstance(value, str) and value else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return None


def _hook_state_source(jsonl_path: str | Path) -> Path | None:
    thread_id = _thread_id(jsonl_path)
    if thread_id is None:
        return None
    state_name = f"{hashlib.sha256(thread_id.encode()).hexdigest()[:16]}.json"
    configured = os.environ.get("PLUGIN_DATA")
    if configured:
        candidates = [Path(configured).expanduser().resolve() / "state" / state_name]
    else:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        candidates = [
            codex_home / "plugins" / "data" / PLUGIN_ID.replace("@", "-") / "state" / state_name,
            _data_root(Path.cwd()) / "state" / state_name,
        ]
    existing = list(dict.fromkeys(path for path in candidates if path.is_file()))
    if not existing:
        return None
    if len(existing) > 1:
        try:
            digests = {hashlib.sha256(path.read_bytes()).digest() for path in existing}
        except OSError as exc:
            raise ValueError("cannot disambiguate Baron hook lifecycle state") from exc
        if len(digests) != 1:
            raise ValueError("ambiguous Baron hook lifecycle state")
    return existing[0]


def _authoritative_rollout_path(session_id: str) -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    session_root = codex_home / "sessions"
    matches = list(session_root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
    valid: list[Path] = []
    root_resolved = session_root.resolve()
    for candidate in matches:
        try:
            relative = candidate.relative_to(session_root)
            if candidate.is_symlink() or any(
                (session_root / Path(*relative.parts[:index])).is_symlink()
                for index in range(1, len(relative.parts))
            ):
                continue
            resolved = candidate.resolve(strict=True)
            if resolved.is_relative_to(root_resolved) and resolved.is_file():
                valid.append(resolved)
        except (OSError, ValueError):
            continue
    if len(valid) != 1:
        raise ValueError(f"cannot identify one authoritative rollout for {session_id}")
    return valid[0]


def _rollout_records(path: Path) -> list[object]:
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("authoritative rollout exceeds 64 MiB")
    records: list[object] = []
    with path.open("rb") as handle:
        for raw in handle:
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("authoritative rollout line exceeds 2 MiB")
            if not raw.strip():
                continue
            records.append(json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_json_object))
            if len(records) > 100_000:
                raise ValueError("authoritative rollout has too many records")
    return records


def _copy_rollout(source: Path, destination: Path) -> Path:
    data = source.read_bytes()
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("authoritative rollout exceeds 64 MiB")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    destination.chmod(0o600)
    return destination


def root_descendant_ids(path: str | Path) -> set[str]:
    """Return stable child IDs observed by an authoritative root rollout."""

    children: set[str] = set()
    for record in _rollout_records(Path(path)):
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        item = payload.get("item") if isinstance(payload, dict) and payload.get("type") == "item_completed" else None
        if not isinstance(item, dict) or item.get("type") != "SubAgentActivity" or item.get("kind") != "started":
            continue
        child = item.get("agent_thread_id")
        if not isinstance(child, str) or not child or child in children:
            raise ValueError("authoritative root rollout has an invalid or duplicate descendant start")
        children.add(child)
    return children


def capture_root_rollout(jsonl_path: str | Path, output_dir: str | Path, label: str) -> Path:
    thread_id = _thread_id(jsonl_path)
    if thread_id is None:
        raise ValueError(f"cannot identify {label} root session")
    return _copy_rollout(
        _authoritative_rollout_path(thread_id), Path(output_dir) / f"{label}-root-rollout.jsonl"
    )


def capture_hook_state(
    jsonl_path: str | Path,
    output_dir: str | Path,
    trusted_skill_path: str | Path | None = None,
) -> Path | None:
    """Capture bounded hook state in a session-bound portable receipt."""

    source = _hook_state_source(jsonl_path)
    thread_id = _thread_id(jsonl_path)
    if source is None or thread_id is None:
        return None
    try:
        data = source.read_bytes()
        if len(data) > 64 * 1024:
            raise ValueError("Baron hook state exceeds 64 KiB")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_json_object)
        if not isinstance(value, dict):
            raise ValueError("Baron hook state is malformed")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("cannot capture Baron hook lifecycle state") from exc
    if value.get("route") != "bulk_reader":
        return None
    worker_id = value.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("cannot capture Baron hook lifecycle state: worker ID is missing")
    try:
        root_source = _authoritative_rollout_path(thread_id)
        worker_source = _authoritative_rollout_path(worker_id)
        root_artifact = Path(output_dir) / "baron-root-rollout.jsonl"
        if not root_artifact.is_file():
            root_artifact = _copy_rollout(root_source, root_artifact)
        worker_artifact = _copy_rollout(worker_source, Path(output_dir) / "baron-worker-1.jsonl")
        root_records = _rollout_records(root_artifact)
        worker_records = _rollout_records(worker_artifact)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("cannot capture authoritative rollout evidence") from exc
    unified = inspect_unified_exec_evidence(
        root_records,
        worker_records,
        thread_id,
        worker_id,
        trusted_skill_path,
    )
    if not unified["session_bound"]:
        raise ValueError("authoritative rollout evidence is not session-bound")
    phase = value.get("phase")
    if phase not in {"awaiting_worker", "spawn_attempted", "worker_active", "worker_complete", "local_fallback"}:
        raise ValueError("cannot capture Baron hook lifecycle state: invalid phase")
    allowed_state: dict[str, object] = {"route": "bulk_reader", "phase": phase}
    for field in ("worker_starts", "spawn_attempts", "worker_commands", "verification_commands"):
        field_value = value.get(field, 0)
        if isinstance(field_value, bool) or not isinstance(field_value, int) or not 0 <= field_value <= 100:
            raise ValueError(f"cannot capture Baron hook lifecycle state: invalid {field}")
        allowed_state[field] = field_value
    identity = value.get("identity_confidence")
    if identity not in {None, "stable"}:
        raise ValueError("cannot capture Baron hook lifecycle state: invalid identity confidence")
    lifecycle_observed = value.get("spawn_observed_via_lifecycle")
    if not isinstance(lifecycle_observed, (bool, type(None))):
        raise ValueError("cannot capture Baron hook lifecycle state: invalid lifecycle flag")
    allowed_state["identity_confidence"] = identity
    allowed_state["spawn_observed_via_lifecycle"] = lifecycle_observed
    allowed_state["worker_commands"] = unified["worker_commands"]
    allowed_state["verification_commands"] = unified["verification_commands"]
    allowed_state["worker_identity_observed"] = unified["bulk_identities"] == 1
    allowed_state["spawn_calls"] = unified["spawn_calls"]
    allowed_state["mailbox_waits"] = unified["mailbox_waits"]
    allowed_state["skill_reads"] = unified["skill_reads"]
    allowed_state["unauthorized_pre_handoff"] = unified["unauthorized_pre_handoff"]
    allowed_state["worker_final"] = unified["worker_final"]
    receipt = {
        "schema_version": 2,
        "root_session_sha256": hashlib.sha256(thread_id.encode()).hexdigest(),
        "worker_session_sha256": hashlib.sha256(worker_id.encode()).hexdigest(),
        "worker_artifact": artifact_descriptor(worker_artifact, "bound_worker_rollout"),
        "state": allowed_state,
    }
    destination = Path(output_dir) / "baron-routing-state.json"
    _write_manifest(destination, receipt)
    destination.chmod(0o600)
    return destination


def _inspect_hook_delegation_receipt(jsonl_path: str | Path, state_path: str | Path | None) -> dict[str, object]:
    failures: list[str] = []
    thread_id = _thread_id(jsonl_path)
    state: dict[str, object] = {}
    session_bound = False
    if thread_id is None or state_path is None:
        failures.append("session-bound hook lifecycle receipt is unavailable")
    else:
        try:
            data = Path(state_path).read_bytes()
            if len(data) > 64 * 1024:
                raise ValueError("oversized")
            parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_json_object)
            if not isinstance(parsed, dict) or parsed.get("schema_version") != 2:
                raise ValueError("not an object")
            expected_session_hash = hashlib.sha256(thread_id.encode()).hexdigest()
            if not hmac.compare_digest(str(parsed.get("root_session_sha256", "")), expected_session_hash):
                failures.append("hook lifecycle receipt belongs to a different root session")
            else:
                session_bound = True
            nested_state = parsed.get("state")
            if not isinstance(nested_state, dict):
                raise ValueError("missing state")
            state = nested_state
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            failures.append("hook lifecycle receipt is malformed")
    if state:
        if state.get("route") != "bulk_reader" or state.get("phase") != "worker_complete":
            failures.append("hook lifecycle did not complete the bulk_reader route")
        if state.get("worker_starts") != 1 or state.get("spawn_attempts") != 1:
            failures.append("hook lifecycle did not observe exactly one worker spawn")
        if state.get("spawn_calls") != 1:
            failures.append("authoritative rollout did not observe exactly one successful worker spawn")
        if state.get("skill_reads") != 1:
            failures.append("authoritative rollout did not observe exactly one installed Baron instruction read")
        if state.get("unauthorized_pre_handoff") != 0:
            failures.append("primary ran repository commands before the verified worker handoff")
        if state.get("worker_identity_observed") is not True:
            failures.append("hook lifecycle worker identity is missing")
        if state.get("identity_confidence") != "stable" or state.get("spawn_observed_via_lifecycle") is not True:
            failures.append("hook lifecycle worker identity is not verified")
        mailbox_waits = state.get("mailbox_waits")
        if isinstance(mailbox_waits, bool) or not isinstance(mailbox_waits, int) or mailbox_waits < 1:
            failures.append("authoritative rollout did not observe a successful worker wait")
        if state.get("worker_final") is not True:
            failures.append("authoritative rollout did not observe a nonempty worker final response")
        for field, minimum, maximum in (
            ("worker_commands", 1, 4), ("verification_commands", 0, 1)
        ):
            value = state.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
                failures.append(f"hook lifecycle {field} is invalid")
    return {
        "verified": not failures,
        "available": state_path is not None,
        "session_bound": session_bound,
        "phase": state.get("phase"),
        "worker_starts": state.get("worker_starts"),
        "spawn_attempts": state.get("spawn_attempts"),
        "worker_commands": state.get("worker_commands"),
        "verification_commands": state.get("verification_commands"),
        "failures": failures,
    }


def inspect_delegation_evidence(
    path: str | Path,
    hook_state_path: str | Path | None = None,
) -> dict[str, object]:
    jsonl = _inspect_jsonl_delegation_evidence(path)
    hook = _inspect_hook_delegation_receipt(path, hook_state_path)
    jsonl_verified = jsonl["verified"] is True
    hook_verified = hook["verified"] is True
    jsonl_spawn_unobservable = (
        jsonl.get("completed_spawns") == 0
        and jsonl.get("bulk_reader_spawns") == 0
        and jsonl.get("completed_waits", 0) >= 1
        and jsonl.get("collaboration_records") == 2 * jsonl.get("completed_waits", 0)
        and jsonl.get("failed_collaboration_calls") == 0
        and jsonl.get("malformed_records") == 0
    )
    receipt_supplied = hook_state_path is not None
    if jsonl_verified:
        verified = hook_verified if receipt_supplied else True
        verification_mode = "jsonl+hook_receipt" if verified and receipt_supplied else "jsonl" if verified else "none"
    elif (jsonl.get("collaboration_records") == 0 or jsonl_spawn_unobservable) and hook_verified:
        verified = True
        verification_mode = "hook_receipt"
    else:
        verified = False
        verification_mode = "none"
    result = dict(jsonl)
    result.update({
        "verified": verified,
        "verification_mode": verification_mode,
        "jsonl_failures": jsonl["failures"],
        "hook_receipt": hook,
        "failures": [] if verified else [*jsonl["failures"], *hook["failures"]],
    })
    return result


def _command_hash(command: list[str]) -> str:
    return hashlib.sha256(json.dumps(command, separators=(",", ":")).encode()).hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="Repository root to benchmark")
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt", help="identical prompt used for both runs")
    prompt.add_argument("--prompt-file", type=Path, help="UTF-8 prompt file")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument(
        "--reasoning-effort", choices=sorted(SUPPORTED_REASONING), default=None,
        help="explicit Codex reasoning setting; omit only to record inherited/unverified settings",
    )
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only")
    parser.add_argument("--order", choices=("baseline-first", "baron-first"), default="baseline-first")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument(
        "--accept-inherited-tools",
        action="store_true",
        help="explicitly accept that both runs inherit the same tool policy while the effective set remains unverified",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_RUN_TIMEOUT_SECONDS)
    parser.add_argument(
        "--max-command-increase", type=int, default=0,
        help="allowed additional Baron commands before the efficiency gate fails (default: 0)",
    )
    return parser.parse_args(argv)


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _failure(code: str, message: str, output_dir: Path | None = None) -> None:
    # Failure manifests are safe to share as well; detailed diagnostics remain
    # on stderr where an operator can inspect them locally.
    payload = {"error": {"code": code, "message": "benchmark failed; see stderr"}}
    if output_dir is not None:
        try:
            _write_manifest(output_dir / "failure.json", payload)
        except OSError:
            pass
    print(json.dumps({"error": {"code": code, "message": message}}, sort_keys=True), file=sys.stderr)
    if output_dir is not None:
        print(f"artifacts: {output_dir}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo.expanduser().resolve()
    try:
        head = require_clean_repository(repo)
        initial = {"head": head, "status": "clean", "status_sha256": repository_status_sha256(repo)}
        prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
        validate_prompt_neutrality(prompt)
        expected_route, route_reason = recommend_route(
            prompt,
            benefit_gate_enabled=True,
            primary_model=args.model,
        )
        if args.max_command_increase < 0:
            raise ValueError("max command increase must be non-negative")
        attestation = runtime_attestation()
        trusted_skill_path = attested_skill_path(attestation)
        verify_plugin_toggle()
        hook_attestation = verify_hooks_enabled()
        version = attestation["installed_plugin_version"]
        agent_attestation = (
            verify_agent_profile(repo) if expected_route == "bulk_reader"
            else {"required": False, "verified": True}
        )
        tools: tuple[str, ...] = ()
    except (OSError, ValueError) as exc:
        _failure("invalid_inputs", str(exc))
        return 2

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir else Path(tempfile.mkdtemp(prefix="codex-baron-benchmark-"))
    )
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            output_dir.relative_to(repo)
        except ValueError:
            pass
        else:
            raise ValueError("output directory must not be inside the benchmark repository")
        existing = [output_dir / name for name in (
            "baseline.jsonl", "baron.jsonl", "baseline.stderr.txt", "baron.stderr.txt",
            "baseline-root-rollout.jsonl", "baron-root-rollout.jsonl", "baron-worker-1.jsonl",
            "baron-routing-state.json", "comparison.json", "failure.json",
        ) if (output_dir / name).exists()]
        if existing:
            raise ValueError("output directory already contains benchmark artifacts; choose a new directory")
        labels = ("baseline", "baron") if args.order == "baseline-first" else ("baron", "baseline")
        records: dict[str, RunRecord] = {}
        commands: dict[str, list[str]] = {}
        for label in labels:
            current_head = require_clean_repository(repo)
            current = {"head": current_head, "status": "clean", "status_sha256": repository_status_sha256(repo)}
            if current != initial:
                raise RuntimeError("Repository revision or status changed between paired runs")
            commands[label] = build_codex_command(
                repo, prompt, args.model, args.sandbox, label == "baron", args.ephemeral,
                reasoning_effort=args.reasoning_effort, tools=tools,
            )
            if args.reasoning_effort is None and args.timeout == DEFAULT_RUN_TIMEOUT_SECONDS:
                # Preserve the small, positional API used by embedders that
                # mock the runner for a dry-run.
                records[label] = run_one(
                    label, repo, prompt, args.model, args.sandbox, output_dir, args.ephemeral
                )
            else:
                records[label] = run_one(
                    label, repo, prompt, args.model, args.sandbox, output_dir, args.ephemeral,
                    reasoning_effort=args.reasoning_effort, tools=tools, timeout=args.timeout,
                )
        current_head = require_clean_repository(repo)
        current = {"head": current_head, "status": "clean", "status_sha256": repository_status_sha256(repo)}
        if current != initial:
            raise RuntimeError("Repository revision or status changed during paired runs")
        baseline_public = parse_usage_file(records["baseline"].jsonl, release_gate=True)
        baron_public = parse_usage_file(records["baron"].jsonl, release_gate=True)
        baseline_root_artifact = capture_root_rollout(records["baseline"].jsonl, output_dir, "baseline")
        baron_root_artifact = capture_root_rollout(records["baron"].jsonl, output_dir, "baron")
        baseline_thread_id = _thread_id(records["baseline"].jsonl)
        baron_thread_id = _thread_id(records["baron"].jsonl)
        baseline_root = parse_rollout_usage_file(
            baseline_root_artifact,
            expected_thread_id=baseline_thread_id,
            expected_root_session_id=baseline_thread_id,
            release_gate=True,
        )
        baron_root = parse_rollout_usage_file(
            baron_root_artifact,
            expected_thread_id=baron_thread_id,
            expected_root_session_id=baron_thread_id,
            release_gate=True,
        )
        for label, public, authoritative in (
            ("baseline", baseline_public, baseline_root), ("baron", baron_public, baron_root)
        ):
            if any(getattr(public, field) != getattr(authoritative, field) for field in (
                "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"
            )):
                raise ValueError(
                    f"{label} public usage disagrees with its authoritative root rollout; "
                    "the runtime accounting contract may have changed"
                )
        hook_state_artifact = capture_hook_state(
            records["baron"].jsonl,
            output_dir,
            trusted_skill_path,
        )
        delegation = inspect_delegation_evidence(records["baron"].jsonl, hook_state_artifact)
        delegation_required = expected_route == "bulk_reader"
        baseline_delegation = _inspect_jsonl_delegation_evidence(records["baseline"].jsonl)
        if baseline_delegation.get("completed_spawns", 0) != 0 or root_descendant_ids(baseline_root_artifact):
            raise ValueError("baseline spawned a descendant; this benchmark requires a single-thread baseline")
        worker_artifact = output_dir / "baron-worker-1.jsonl"
        worker_usage = None
        if delegation_required:
            if hook_state_artifact is None or not worker_artifact.is_file():
                raise ValueError("verified delegation has no portable worker rollout artifact")
            worker_usage = parse_rollout_usage_file(
                worker_artifact,
                expected_root_session_id=baron_thread_id,
                release_gate=True,
            )
            receipt_value = json.loads(hook_state_artifact.read_text(encoding="utf-8"))
            observed_children = root_descendant_ids(baron_root_artifact)
            observed_hashes = {hashlib.sha256(value.encode()).hexdigest() for value in observed_children}
            if observed_hashes != {receipt_value.get("worker_session_sha256")}:
                raise ValueError("Baron authoritative root and worker receipt disagree")
            baron_measured = combine_usage(baron_public, worker_usage, path="baron+workers")
        else:
            baron_measured = baron_public
        comparison = build_comparison(baseline_public, baron_measured)
        run_artifacts = {
            label: {
                "jsonl": artifact_descriptor(records[label].jsonl, "codex_jsonl"),
                "stderr": artifact_descriptor(records[label].stderr, "stderr"),
                "root_rollout": artifact_descriptor(
                    baseline_root_artifact if label == "baseline" else baron_root_artifact,
                    "authoritative_root_rollout",
                ),
            }
            for label in ("baseline", "baron")
        }
        if hook_state_artifact is not None:
            run_artifacts["baron"]["routing_state"] = artifact_descriptor(
                hook_state_artifact, "baron_hook_state"
            )
        if worker_usage is not None:
            run_artifacts["baron"]["descendants"] = [
                artifact_descriptor(worker_artifact, "bound_worker_rollout")
            ]
        artifacts_verified = all(
            isinstance(artifact.get("sha256"), str)
            for run in run_artifacts.values()
            for value in run.values()
            for artifact in (value if isinstance(value, list) else [value])
        )
        efficiency_gate_eligible = (
            args.reasoning_effort is not None
            and args.sandbox == "read-only"
            and artifacts_verified
            and args.accept_inherited_tools
            and (not delegation_required or delegation["verified"] is True)
        )
        command_increase = comparison["baron"]["commands_started"] - comparison["baseline"]["commands_started"]
        command_failure_increase = comparison["baron"]["commands_failed"] - comparison["baseline"]["commands_failed"]
        efficiency_failures: list[str] = []
        if not efficiency_gate_eligible:
            efficiency_failures.append(
                "comparability is unverified: use read-only sandbox, an explicit supported --reasoning-effort, "
                "complete artifacts, and --accept-inherited-tools after reviewing the inherited tool policy"
            )
        if delegation_required and delegation["verified"] is not True:
            efficiency_failures.append(
                "Baron did not produce a verified bulk_reader spawn-and-wait handoff"
            )
        if comparison["difference_tokens"] <= 0:
            efficiency_failures.append("Baron did not save total tokens")
        if command_increase > args.max_command_increase:
            efficiency_failures.append(
                f"Baron started {command_increase} additional commands (limit {args.max_command_increase})"
            )
        if command_failure_increase > 0:
            efficiency_failures.append(
                f"Baron had {command_failure_increase} additional failed command(s)"
            )
        metadata: dict[str, object] = {
            "schema_version": 3,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "plugin": {"id": PLUGIN_ID, "version": version},
            "runtime": attestation,
            "plugin_toggle_verified": True,
            "hooks": hook_attestation,
            "agents": agent_attestation,
            "repository": initial,
            "routing": {
                "expected_route": expected_route,
                "reason": route_reason,
                "delegation_required": delegation_required,
                "delegation": delegation,
            },
            "inputs": {
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "model": args.model,
                "reasoning_effort": {
                    "value": args.reasoning_effort,
                    "source": "explicit_cli" if args.reasoning_effort else "inherited_codex_config",
                    "verified": args.reasoning_effort is not None,
                },
                "sandbox": args.sandbox,
                "tools": {
                    "mode": "same_inherited_codex_runtime",
                    "pairwise_held_constant": True,
                    "effective_set_verified": False,
                    "operator_accepted_unverified": bool(args.accept_inherited_tools),
                },
                "ephemeral": bool(args.ephemeral),
                "order": args.order,
            },
            "runs": {
                label: {
                    "label": label,
                    "elapsed_seconds": records[label].elapsed_seconds,
                    "command_sha256": _command_hash(commands[label]),
                    "artifacts": run_artifacts[label],
                }
                for label in ("baseline", "baron")
            },
            "comparison": comparison,
            "accounting": {
                "mode": "root_plus_bound_descendants",
                "baseline": {
                    "root": baseline_public.to_dict(),
                    "descendants": [],
                    "aggregate": baseline_public.to_dict(),
                },
                "baron": {
                    "root": baron_public.to_dict(),
                    "descendants": [] if worker_usage is None else [worker_usage.to_dict()],
                    "aggregate": baron_measured.to_dict(),
                },
            },
            "comparability_verified": False,
            "efficiency_gate_eligible": efficiency_gate_eligible,
            "efficiency_gate_passed": not efficiency_failures,
            "efficiency_gate": {
                "eligible": efficiency_gate_eligible,
                "passed": not efficiency_failures,
                "command_increase": command_increase,
                "max_command_increase": args.max_command_increase,
                "command_failure_increase": command_failure_increase,
                "max_command_failure_increase": 0,
            },
        }
        # Stable top-level aliases keep the manifest easy to consume by older
        # operators while the nested blocks remain the canonical schema.
        metadata.update({
            "prompt_sha256": metadata["inputs"]["prompt_sha256"],
            "repository_head": initial["head"],
            "repository_status": initial["status"],
            "repository_status_sha256": initial["status_sha256"],
            "model": args.model,
            "reasoning_effort": {
                "value": args.reasoning_effort,
                "source": "explicit_cli" if args.reasoning_effort else "inherited_codex_config",
                "verified": args.reasoning_effort is not None,
            },
            "sandbox": args.sandbox,
            "tools": {
                "mode": "same_inherited_codex_runtime",
                "pairwise_held_constant": True,
                "effective_set_verified": False,
                "operator_accepted_unverified": bool(args.accept_inherited_tools),
            },
            "plugin_version": version,
            "order": args.order,
        })
        _write_manifest(output_dir / "comparison.json", metadata)
    except (OSError, RuntimeError, ValueError) as exc:
        _failure("paired_run_failed", str(exc), output_dir)
        return 2

    print(render_meter(comparison))
    print(
        f"  Elapsed seconds   {records['baseline'].elapsed_seconds:>12.1f} -> "
        f"{records['baron'].elapsed_seconds:>12.1f}"
    )
    print(f"  Artifacts: {output_dir}")
    if efficiency_failures:
        print("Efficiency gate failed: " + "; ".join(efficiency_failures), file=sys.stderr)
        return 1
    print("Efficiency gate passed: Baron saved tokens within the command budget; production approval still requires quality evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
