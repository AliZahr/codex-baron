#!/usr/bin/env python3
"""Codex hook for deterministic routing and metadata-only telemetry."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

PLUGIN_ROOT = Path(os.environ.get("PLUGIN_ROOT", Path(__file__).resolve().parents[1])).resolve()
DEFAULT_CONFIG = PLUGIN_ROOT / "config" / "router.json"
ROUTE_MODELS = {
    "bulk_reader": "gpt-5.6-terra",
    "code_writer": "gpt-5.6-luna",
    "test_writer": "gpt-5.6-luna",
    "senior_reviewer": "gpt-5.6-sol",
}
StateResult = TypeVar("StateResult")
LOCAL_FALLBACK_NOTICE = (
    "Codex Baron found no started worker after the spawn attempt; continuing locally with one bounded discovery "
    "command. No worker handoff is available."
)
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCK_LOCAL_STATE = threading.local()


def inspect_unified_exec_evidence(
    root_records: list[object],
    worker_records: list[object],
    root_thread_id: str,
    expected_worker_id: str,
    trusted_skill_path: str | Path | None = None,
) -> dict[str, object]:
    """Extract raw evidence from two authoritative, session-bound rollouts."""

    def payload(record: object, record_type: str | None = None) -> dict[str, Any] | None:
        if not isinstance(record, dict) or (record_type and record.get("type") != record_type):
            return None
        value = record.get("payload")
        return value if isinstance(value, dict) else None

    root_meta = [payload(record, "session_meta") for record in root_records]
    root_meta = [value for value in root_meta if value is not None]
    worker_meta = [payload(record, "session_meta") for record in worker_records]
    worker_meta = [value for value in worker_meta if value is not None]
    root_bound = len(root_meta) == 1 and (
        root_meta[0].get("id") == root_thread_id
        and root_meta[0].get("session_id") == root_thread_id
        and root_meta[0].get("parent_thread_id") is None
    )
    role = None
    if len(worker_meta) == 1:
        source = worker_meta[0].get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        role = spawn.get("agent_role") if isinstance(spawn, dict) else None
    worker_bound = len(worker_meta) == 1 and (
        worker_meta[0].get("id") == expected_worker_id
        and worker_meta[0].get("session_id") == root_thread_id
        and worker_meta[0].get("parent_thread_id") == root_thread_id
        and role == "bulk_reader"
    )

    pending: dict[str, str] = {}
    successful_wait = False
    spawn_calls = 0
    mailbox_waits = 0
    verification_commands = 0
    unauthorized_pre_handoff = 0
    skill_reads = 0
    for record in root_records:
        item = payload(record, "response_item")
        if item is not None:
            item_type = item.get("type")
            if item_type == "function_call" and item.get("name") in {"spawn_agent", "wait_agent"}:
                call_id = item.get("call_id")
                if isinstance(call_id, str) and call_id:
                    pending[call_id] = str(item["name"])
            elif item_type == "function_call_output":
                call_id = item.get("call_id")
                name = pending.pop(call_id, None) if isinstance(call_id, str) else None
                output = item.get("output")
                try:
                    decoded = json.loads(output) if isinstance(output, str) else output
                except json.JSONDecodeError:
                    decoded = None
                if name == "spawn_agent" and isinstance(decoded, dict) and decoded.get("task_name"):
                    spawn_calls += 1
                elif (
                    name == "wait_agent"
                    and spawn_calls == 1
                    and isinstance(decoded, dict)
                    and decoded.get("timed_out") is False
                ):
                    mailbox_waits += 1
                    successful_wait = True
        event = payload(record, "event_msg")
        command_item = event.get("item") if event is not None and event.get("type") == "item_completed" else None
        if isinstance(command_item, dict) and command_item.get("type") == "CommandExecution":
            if successful_wait:
                verification_commands += 1
            else:
                command = command_item.get("command")
                shell_command = command[-1] if isinstance(command, list) and command else command
                successful_command = (
                    command_item.get("status") == "completed"
                    and command_item.get("exit_code") == 0
                )
                if (
                    successful_command
                    and isinstance(shell_command, str)
                    and skill_reads == 0
                    and is_baron_instruction_read(
                        shell_command,
                        trusted_skill_path=trusted_skill_path,
                        require_complete=True,
                    )
                ):
                    skill_reads += 1
                else:
                    unauthorized_pre_handoff += 1

    worker_commands = 0
    worker_final = False
    for record in worker_records:
        event = payload(record, "event_msg")
        command_item = event.get("item") if event is not None and event.get("type") == "item_completed" else None
        if isinstance(command_item, dict) and command_item.get("type") == "CommandExecution":
            worker_commands += 1
        if event is not None and event.get("type") == "task_complete":
            worker_final = worker_final or bool(str(event.get("last_agent_message") or "").strip())

    return {
        "session_bound": root_bound and worker_bound,
        "bulk_identities": 1 if worker_bound else 0,
        "spawn_calls": spawn_calls,
        "mailbox_waits": mailbox_waits,
        "skill_reads": skill_reads,
        "unauthorized_pre_handoff": unauthorized_pre_handoff,
        "worker_commands": worker_commands,
        "verification_commands": verification_commands,
        "worker_final": worker_final,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_config(cwd: Path) -> dict[str, Any]:
    config = _read_json(DEFAULT_CONFIG)
    override = _read_json(cwd / ".codex" / "codex-baron.json")
    for key in config:
        if key in override and isinstance(override[key], type(config[key])):
            config[key] = override[key]
    env_threshold = os.environ.get("ENGINEERING_ROUTER_LARGE_FILE_LINES")
    if env_threshold and env_threshold.isdigit():
        config["large_file_lines"] = max(50, int(env_threshold))
    telemetry = os.environ.get("ENGINEERING_ROUTER_TELEMETRY", "").lower()
    if telemetry in {"0", "off", "false"}:
        config["telemetry_enabled"] = False
    elif telemetry in {"1", "on", "true"}:
        config["telemetry_enabled"] = True
    return config


def _matches(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z0-9_])", text) for word in words)


def _session_hash(session_id: object) -> str:
    return hashlib.sha256(str(session_id or "").encode()).hexdigest()[:16]


def _data_root(cwd: Path) -> Path:
    configured = os.environ.get("PLUGIN_DATA")
    if configured:
        root = Path(configured)
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "codex-baron"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "codex-baron"
    return root.expanduser().resolve()


def _lock_options(config: dict[str, Any] | None = None) -> tuple[float, float]:
    def number(name: str, default: float) -> float:
        try:
            value = float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    config = config or {}
    timeout = config.get("state_lock_timeout_seconds", 0.5)
    stale = config.get("state_lock_stale_seconds", 30.0)
    timeout = timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0 else 0.5
    stale = stale if isinstance(stale, (int, float)) and not isinstance(stale, bool) and stale > 0 else 30.0
    # State and optional telemetry can lock sequentially inside a three-second
    # host hook, so no individual acquisition may consume that deadline.
    return min(number("ENGINEERING_ROUTER_LOCK_TIMEOUT", float(timeout)), 0.5), number("ENGINEERING_ROUTER_LOCK_STALE", float(stale))


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


@contextmanager
def _directory_lock(path: Path, timeout: float, stale_after: float):
    """Acquire a bounded kernel-owned lock on supported macOS/Linux hosts.

    The lock file remains on disk. Kernel ownership is tied to this open file
    descriptor and disappears on process exit, eliminating stale leases and
    pathname ABA cleanup races. A directory at this path belongs to an older
    Baron build; wait for that owner rather than deleting it speculatively.
    """

    del stale_after  # Kernel locks do not require lease-age recovery.
    deadline = time.monotonic() + timeout
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor: int | None = None
    while True:
        try:
            if path.is_dir():
                raise BlockingIOError
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.chmod(path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except (BlockingIOError, FileExistsError, IsADirectoryError):
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring lock {path.name}")
            time.sleep(0.02)
    try:
        yield
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@contextmanager
def _state_lock(path: Path, config: dict[str, Any] | None = None):
    timeout, stale_after = _lock_options(config)
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        local = _LOCAL_LOCKS.setdefault(key, threading.Lock())
    held = set(getattr(_LOCK_LOCAL_STATE, "held", set()))
    if key in held:
        raise RuntimeError(f"reentrant state mutation refused for {path.name}")
    if not local.acquire(timeout=timeout):
        raise TimeoutError(f"timed out acquiring local lock {path.name}")
    held.add(key)
    _LOCK_LOCAL_STATE.held = held
    try:
        with _directory_lock(path, timeout, stale_after):
            yield
    finally:
        held = set(getattr(_LOCK_LOCAL_STATE, "held", set()))
        held.discard(key)
        _LOCK_LOCAL_STATE.held = held
        local.release()


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _prune_state(state_root: Path, current: Path, config: dict[str, Any] | None) -> None:
    config = config or {}
    ttl = config.get("state_ttl_seconds", 7 * 24 * 60 * 60)
    max_files = config.get("state_max_files", 256)
    ttl = ttl if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else 7 * 24 * 60 * 60
    max_files = max_files if isinstance(max_files, int) and not isinstance(max_files, bool) and max_files > 0 else 256
    now = time.time()
    files = []
    for path in state_root.glob("*.json"):
        if path == current:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime > ttl:
            try:
                path.unlink()
            except OSError:
                pass
        else:
            files.append((stat.st_mtime, path))
    files.sort()
    for _, path in files[: max(0, len(files) - max_files + 1)]:
        try:
            path.unlink()
        except OSError:
            pass


def mutate_route_state(
    cwd: Path,
    session_id: object,
    mutation: Callable[[dict[str, Any]], StateResult],
    config: dict[str, Any] | None = None,
    error_result: StateResult | None = None,
) -> StateResult | None:
    """Atomically update privacy-safe per-session routing state."""

    if not session_id:
        return None
    data_root = _data_root(cwd)
    state_root = data_root / "state"
    key = _session_hash(session_id)
    state_path = state_root / f"{key}.json"
    lock_path = state_root / f"{key}.lock"
    try:
        _secure_directory(data_root)
        _secure_directory(state_root)
        with _state_lock(lock_path, config):
            state = _read_json(state_path)
            if state_path.exists() and not state:
                # A corrupt state file must not silently disable the guard.
                state = {"__invalid_state__": True}
            result = mutation(state)
            state["updated_at"] = int(time.time())
            temporary = state_root / f".{key}.{os.getpid()}.tmp"
            temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            temporary.chmod(0o600)
            if temporary.stat().st_size > 64 * 1024:
                raise OSError("routing state exceeds 64 KiB")
            os.replace(temporary, state_path)
            state_path.chmod(0o600)
            _prune_state(state_root, state_path, config)
            return result
    except (OSError, TimeoutError, ValueError, TypeError, RuntimeError):
        return error_result


SENIOR_WORDS = (
    "security", "secure", "vulnerability", "auth", "oauth", "permission", "crypto",
    "secret", "payment", "billing", "race condition", "deadlock", "concurrency",
    "transaction", "migration", "architecture", "root cause", "production incident",
)
DEBUG_WORDS = ("debug", "bug", "fix", "crash", "failure", "regression", "incorrect", "why")
TEST_WORDS = ("test", "tests", "fixture", "fixtures", "mock", "mocks", "snapshot", "coverage")
DISCOVERY_WORDS = ("explore", "analyze", "analyse", "trace", "find", "locate", "map", "understand", "summarize")
GENERATION_WORDS = ("scaffold", "boilerplate", "generate", "stub", "config", "documentation", "docs", "translate")
CHANGE_WORDS = ("implement", "fix", "change", "modify", "update", "add", "remove", "refactor", "migrate", "patch")
SECURITY_REVIEW_WORDS = ("security review", "security audit", "threat model", "vulnerability", "exploit")
BROAD_SCOPE_WORDS = (
    "all", "every", "complete", "entire", "whole", "across", "repository", "codebase",
    "call path", "call-path", "dependency map", "multiple", "end-to-end", "end to end",
)
EXPLICIT_DELEGATION_WORDS = (
    "delegate", "subagent", "sub-agent", "parallel", "bulk reader", "bulk_reader",
    "code writer", "code_writer", "test writer", "test_writer", "senior reviewer", "senior_reviewer",
)
NAMED_ROUTES = (
    (("bulk reader", "bulk_reader"), "bulk_reader"),
    (("code writer", "code_writer"), "code_writer"),
    (("test writer", "test_writer"), "test_writer"),
    (("senior reviewer", "senior_reviewer"), "senior_reviewer"),
)


def _sensitive_decision(text: str) -> bool:
    """Whether a prompt asks for a sensitive change or conclusion.

    Read-only discovery of a sensitive area remains eligible for bulk_reader;
    named lower-tier routes cannot downgrade an actual sensitive decision.
    """

    named_write_worker = _matches(
        text,
        ("code writer", "code_writer", "test writer", "test_writer"),
    )
    return _matches(text, SENIOR_WORDS) and (
        _matches(text, CHANGE_WORDS)
        or _matches(text, SECURITY_REVIEW_WORDS)
        or _matches(text, DEBUG_WORDS)
        or named_write_worker
    )


def classify_prompt(prompt: str) -> tuple[str, str]:
    text = prompt.lower()
    if _matches(text, DEBUG_WORDS):
        return "senior_reviewer", "debugging or root-cause analysis"
    if _matches(text, DISCOVERY_WORDS):
        sensitive_decision = _matches(text, SENIOR_WORDS) and (
            _matches(text, CHANGE_WORDS) or _matches(text, SECURITY_REVIEW_WORDS)
        )
        if sensitive_decision:
            return "senior_reviewer", "sensitive change or security review"
        return "bulk_reader", "repository discovery or reading"
    if _matches(text, SENIOR_WORDS):
        return "senior_reviewer", "high-risk or deep-reasoning keywords"
    if _matches(text, TEST_WORDS):
        return "test_writer", "test-focused request"
    if _matches(text, GENERATION_WORDS):
        return "code_writer", "predictable generation or documentation"
    return "primary", "general engineering task"


def recommend_route(
    prompt: str,
    benefit_gate_enabled: bool = True,
    primary_model: str | None = None,
) -> tuple[str, str]:
    """Return the route worth recommending after applying a cheap benefit gate."""

    text = prompt.lower()
    if _sensitive_decision(text):
        if (
            primary_model == ROUTE_MODELS["senior_reviewer"]
            and not _matches(text, ("senior reviewer", "senior_reviewer"))
        ):
            return "primary", "primary already uses the required senior-review model"
        route, reason = "senior_reviewer", "sensitive change or security review"
        return route, reason
    named_route = next((route for names, route in NAMED_ROUTES if _matches(text, names)), None)
    explicitly_named = named_route is not None
    if explicitly_named:
        route, reason = named_route, "explicit named-agent delegation"
    else:
        route, reason = classify_prompt(prompt)
    if not explicitly_named and primary_model and ROUTE_MODELS.get(route) == primary_model:
        return "primary", "primary already uses the recommended worker model"
    if route in {"primary", "senior_reviewer"} or not benefit_gate_enabled:
        return route, reason
    if _matches(text, EXPLICIT_DELEGATION_WORDS):
        return route, reason
    if route in {"code_writer", "test_writer"}:
        return "primary", "write delegation requires an explicit request"
    if route == "bulk_reader" and _matches(text, BROAD_SCOPE_WORDS):
        return route, reason
    return "primary", "delegation overhead likely exceeds the bounded task"


def _safe_repo_file(cwd: Path, token: str) -> Path | None:
    cleaned = token.strip("'\"")
    if not cleaned or cleaned == "-" or cleaned.startswith("-"):
        return None
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(cwd.resolve())
        return resolved if resolved.is_file() else None
    except (OSError, ValueError):
        return None


def find_large_full_read(command: str, cwd: Path, threshold: int) -> tuple[Path, int] | None:
    # Only classify simple, unpiped `cat file...` commands. Targeted reads and pipelines stay allowed.
    if any(operator in command for operator in ("|", ">", "<", ";", "&&", "||")):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    if executable not in {"cat", "bat"}:
        return None
    for token in tokens[1:]:
        path = _safe_repo_file(cwd, token)
        if path is None:
            continue
        try:
            with path.open("rb") as handle:
                lines = sum(1 for _ in handle)
        except OSError:
            continue
        if lines > threshold:
            return path, lines
    return None


def is_repository_discovery_command(command: str) -> bool:
    """Return whether a Bash command is a repository read/search operation."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = Path(tokens[0]).name.lower()
    if executable == "git":
        return len(tokens) > 1 and tokens[1].lower() in {"grep", "ls-files", "log", "show", "status", "diff", "rev-parse", "branch", "tag"}
    if executable in {"rg", "grep", "find", "fd", "ls", "tree", "cat", "bat", "head", "tail", "sed", "awk", "wc"}:
        return True
    return False


def is_baron_instruction_read(
    command: str,
    *,
    trusted_skill_path: str | Path | None = None,
    require_complete: bool = False,
) -> bool:
    """Allow narrowly scoped reads used to load this plugin's own SKILL.md."""

    if any(marker in command for marker in ("`", "$(", "\n", "\r", "|", ";", ">", "<")):
        return False
    cache_scope = PLUGIN_ROOT.parent.parent.resolve()
    expected_skill = Path(
        trusted_skill_path
        if trusted_skill_path is not None
        else PLUGIN_ROOT / "skills" / "codex-baron" / "SKILL.md"
    ).expanduser().resolve(strict=False)

    def trusted_executable(value: str, name: str) -> bool:
        if value not in {name, f"/usr/bin/{name}"}:
            return False
        resolved = shutil.which(value)
        return resolved is not None and Path(resolved).resolve() == Path(f"/usr/bin/{name}")

    def skill_path(value: str) -> Path | None:
        if not value.startswith("/"):
            return None
        try:
            resolved = Path(value).resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        return resolved if resolved == expected_skill else None

    segments = command.split("&&")
    if len(segments) not in {1, 2} or "||" in command:
        return False
    parsed: list[list[str]] = []
    try:
        parsed = [shlex.split(segment.strip()) for segment in segments]
    except ValueError:
        return False
    if any(not tokens for tokens in parsed):
        return False

    def is_sed(tokens: list[str]) -> tuple[Path, int, int] | None:
        if len(tokens) != 4 or not trusted_executable(tokens[0], "sed") or tokens[1] != "-n":
            return None
        match = re.fullmatch(r"(\d+),(\d+)p", tokens[2])
        if not match or int(match.group(1)) < 1 or int(match.group(2)) < int(match.group(1)) or int(match.group(2)) > 1000:
            return None
        path = skill_path(tokens[3])
        return (path, int(match.group(1)), int(match.group(2))) if path is not None else None

    def is_wc(tokens: list[str]) -> Path | None:
        return skill_path(tokens[2]) if len(tokens) == 3 and trusted_executable(tokens[0], "wc") and tokens[1] == "-l" else None

    def is_complete_sed(value: tuple[Path, int, int] | None) -> bool:
        if value is None or value[1] != 1:
            return False
        try:
            with value[0].open("rb") as handle:
                line_count = sum(1 for _ in handle)
        except OSError:
            return False
        return 0 < line_count <= 1000 and value[2] >= line_count

    if len(parsed) == 2:
        wc_path, sed_result = is_wc(parsed[0]), is_sed(parsed[1])
        return (
            wc_path is not None
            and sed_result is not None
            and wc_path == sed_result[0]
            and (not require_complete or is_complete_sed(sed_result))
        )
    tokens = parsed[0]
    sed_result = is_sed(tokens)
    if sed_result is not None:
        return not require_complete or is_complete_sed(sed_result)
    if is_wc(tokens) is not None:
        return not require_complete
    if require_complete:
        return False
    if (
        len(tokens) < 2
        or not trusted_executable(tokens[0], "find")
        or Path(tokens[1]).resolve(strict=False) != cache_scope
    ):
        return False
    selectors = tokens[2:]
    return selectors in (
        ["-name", "SKILL.md", "-print"],
        ["-name", "SKILL.md", "-path", "*/skills/codex-baron/*", "-print"],
    )


def _stable_agent_id(payload: dict[str, Any]) -> str | None:
    for field in ("agent_id", "subagent_id", "thread_id", "id"):
        value = payload.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    return None


def _is_spawn_agent_tool(tool_name: str) -> bool:
    normalized = tool_name.strip().lower().replace("-", "_")
    return (
        normalized in {"agent", "spawn_agent"}
        or normalized.endswith(".spawn_agent")
        or normalized.endswith("__spawn_agent")
    )


def is_bulk_reader_role_marker(*, agent_type: object = None, task_name: object = None) -> bool:
    """Recognize the one auditable structured role marker used by Baron."""

    def normalize(value: object) -> str:
        return str(value).strip().lower().replace("-", "_") if isinstance(value, str) else ""

    return normalize(agent_type) == "bulk_reader" or normalize(task_name) == "bulk_reader"


def _spawn_targets_bulk_reader(tool_input: dict[str, Any]) -> bool:
    for value in tool_input.values():
        if isinstance(value, dict) and _spawn_targets_bulk_reader(value):
            return True
    if is_bulk_reader_role_marker(
        agent_type=tool_input.get("agent_type"), task_name=tool_input.get("task_name")
    ):
        return True
    for field in ("agent", "profile", "worker", "route", "name"):
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip().lower().replace("-", "_") in {"bulk_reader", "bulk reader"}:
            return True
    for field in ("prompt", "task", "brief", "instructions"):
        value = tool_input.get(field)
        if isinstance(value, str) and _matches(value.lower(), ("bulk_reader", "bulk reader")):
            return True
    return False


def _is_wait_tool(tool_name: str) -> bool:
    normalized = tool_name.strip().lower().replace("-", "_")
    return (
        normalized in {"wait", "wait_agent", "wait_threads"}
        or normalized.endswith(".wait_agent")
        or normalized.endswith("__wait_agent")
        or normalized.endswith(".wait_threads")
        or normalized.endswith("__wait_threads")
    )


def _is_mailbox_wait_tool(tool_name: str) -> bool:
    normalized = tool_name.strip().lower().replace("-", "_")
    return normalized == "wait_agent" or normalized.endswith(".wait_agent") or normalized.endswith("__wait_agent")


def patch_paths(command: str) -> list[str]:
    return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, flags=re.MULTILINE)


def sensitive_path(paths: list[str], patterns: list[str]) -> bool:
    joined = " ".join(paths).lower()
    return any(pattern.lower() in joined for pattern in patterns)


def _bucket(value: int) -> str:
    if value <= 500:
        return "0-500"
    if value <= 1000:
        return "501-1000"
    if value <= 2500:
        return "1001-2500"
    return "2500+"


def _bounded_int(config: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, minimum), maximum)


def write_telemetry(event: dict[str, Any], config: dict[str, Any]) -> None:
    if not config.get("telemetry_enabled", False):
        return
    base = _data_root(Path(event.get("cwd") or "."))
    try:
        _secure_directory(base)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event.get("event"),
            "route": event.get("route"),
            "reason": event.get("reason"),
            "model": event.get("model"),
            "tool": event.get("tool"),
            "agent_type": event.get("agent_type"),
            "session_hash": _session_hash(event.get("session_id")),
        }
        if event.get("line_count") is not None:
            record["line_bucket"] = _bucket(int(event["line_count"]))
        if event.get("extension"):
            record["extension"] = event["extension"]
        line = (json.dumps({k: v for k, v in record.items() if v is not None}, sort_keys=True) + "\n").encode()
        max_line = _bounded_int(config, "telemetry_max_line_bytes", 4096, 256, 64 * 1024)
        max_file = _bounded_int(config, "telemetry_max_bytes", 1_048_576, max_line, 64 * 1024 * 1024)
        if len(line) > max_line:
            return
        telemetry_path = base / "events.jsonl"
        _timeout, stale_after = _lock_options(config)
        with _directory_lock(base / "events.lock", min(_timeout, 0.05), stale_after):
            try:
                current_size = telemetry_path.stat().st_size
            except FileNotFoundError:
                current_size = 0
            if current_size + len(line) > max_file:
                return
            with telemetry_path.open("ab") as handle:
                handle.write(line)
            telemetry_path.chmod(0o600)
    except (OSError, TimeoutError, ValueError):
        pass  # Telemetry must never break an engineering task.


def context_output(event_name: str, message: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": message}}


def deny_tool(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def reset_route_state(
    cwd: Path,
    session_id: object,
    route: str,
    primary_model: str,
    config: dict[str, Any] | None = None,
) -> bool:
    def reset(state: dict[str, Any]) -> bool:
        state.clear()
        state.update({
            "route": route,
            "primary_model": primary_model,
            "phase": "awaiting_worker" if route == "bulk_reader" else "awaiting_senior_review" if route == "senior_reviewer" else "inactive",
            "senior_review_required": route == "senior_reviewer",
            "pre_worker_denials": 0,
            "worker_starts": 0,
            "worker_commands": 0,
            "verification_commands": 0,
        })
        return True

    return bool(mutate_route_state(cwd, session_id, reset, config, error_result=False))


def require_senior_review(cwd: Path, session_id: object, config: dict[str, Any]) -> bool:
    """Persist a review requirement after a sensitive path is selected."""

    def mark(state: dict[str, Any]) -> bool:
        state["senior_review_required"] = True
        state["phase"] = "awaiting_senior_review"
        return True

    return bool(mutate_route_state(cwd, session_id, mark, config, error_result=False))


def mark_spawn_attempted(cwd: Path, session_id: object, config: dict[str, Any]) -> bool:
    def mark(state: dict[str, Any]) -> bool:
        if (
            state.get("route") != "bulk_reader"
            or state.get("phase") != "awaiting_worker"
            or int(state.get("spawn_attempts", 0)) != 0
        ):
            return False
        state["phase"] = "spawn_attempted"
        state["requested_worker_role"] = "bulk_reader"
        state["pre_worker_denials"] = 0
        state["spawn_attempts"] = int(state.get("spawn_attempts", 0)) + 1
        return True

    return bool(mutate_route_state(cwd, session_id, mark, config, error_result=False))


def _wait_target_ids(tool_input: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def visit(value: object, key: str = "") -> None:
        normalized = key.lower().replace("-", "_")
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif normalized in {
            "target", "targets", "agent_id", "agent_ids", "thread_id", "thread_ids",
            "receiver_thread_id", "receiver_thread_ids", "id", "ids",
            "agentid", "agentids", "threadid", "threadids", "receiverthreadid", "receiverthreadids",
        } and isinstance(value, (str, int)) and not isinstance(value, bool):
            found.add(str(value))

    visit(tool_input)
    return found


def wait_guard(
    cwd: Path,
    session_id: object,
    config: dict[str, Any],
    tool_input: dict[str, Any] | None = None,
    tool_name: str = "wait_threads",
) -> str | None:
    def inspect(state: dict[str, Any]) -> str | None:
        if state.get("route") != "bulk_reader":
            return None
        phase = state.get("phase")
        if phase in {"awaiting_worker", "spawn_attempted"}:
            return (
                "Codex Baron cannot verify a worker handoff yet. Wait only after a nonempty bulk_reader worker "
                "ID is returned; an empty wait is not a handoff."
            )
        if phase != "worker_active":
            return "Codex Baron permits discovery waits only while one verified bulk_reader worker is active."
        if not state.get("worker_id"):
            return "Codex Baron cannot wait for a discovery worker without a verified nonempty worker ID."
        expected = str(state.get("worker_id"))
        if _is_mailbox_wait_tool(tool_name):
            return None
        if _wait_target_ids(tool_input or {}) != {expected}:
            return "Codex Baron waits must target exactly the active bulk_reader worker ID."
        return None

    return mutate_route_state(
        cwd,
        session_id,
        inspect,
        config,
        error_result="Codex Baron could not verify worker state; do not wait.",
    )


def discovery_guard(
    cwd: Path,
    session_id: object,
    model: str,
    config: dict[str, Any],
    command: str = "",
    agent_type: str = "",
    agent_id: str | None = None,
) -> str | None:
    """Return a denial reason when exclusive discovery ownership would be violated."""

    if not config.get("exclusive_discovery_guard_enabled", True):
        return None
    worker_limit = _bounded_int(config, "discovery_worker_command_limit", 4, 1, 100)
    verification_limit = _bounded_int(config, "primary_verification_command_limit", 1, 0, 100)
    fallback_limit = _bounded_int(config, "local_fallback_command_limit", 1, 1, 10)

    def inspect(state: dict[str, Any]) -> str | None:
        if state.get("__invalid_state__"):
            return "Codex Baron routing state is corrupt; refusing repository discovery until the session is restarted."
        if state.get("route") != "bulk_reader":
            return None
        phase = state.get("phase")
        primary_model = state.get("primary_model")
        worker_model = state.get("worker_model")
        if command and is_baron_instruction_read(command):
            return None
        if phase != "worker_active" and command and not is_repository_discovery_command(command):
            return None
        if phase == "awaiting_worker" and model == primary_model:
            state["pre_worker_denials"] = int(state.get("pre_worker_denials", 0)) + 1
            return (
                "Codex Baron reserved repository discovery for one bulk_reader. Delegate now with no inherited "
                "history. Verify a nonempty worker ID before waiting. If spawning fails, retry this command once "
                "after the spawn attempt to continue locally."
            )
        if phase == "spawn_attempted" and model == primary_model:
            if int(state.get("local_fallback_commands", 0)) == 0:
                state["pre_worker_denials"] = 1
                state["phase"] = "local_fallback"
                state["local_fallback_commands"] = 1
                return LOCAL_FALLBACK_NOTICE
            return "Codex Baron already consumed the bounded local fallback; do not repeat broad discovery."
        if phase in {"awaiting_worker", "spawn_attempted"} and model != primary_model:
            return "Codex Baron has not observed a verified bulk_reader SubagentStart lifecycle event."
        if phase == "worker_active":
            if model != worker_model:
                return "Codex Baron assigned discovery to the active bulk_reader; wait for its nonempty response."
            if state.get("worker_agent_type") != "bulk_reader":
                return "Codex Baron assigned discovery only to bulk_reader; stop this repository scan."
            expected_id = state.get("worker_id")
            if not expected_id or not agent_id or str(expected_id) != str(agent_id):
                return "Codex Baron assigned discovery to another bulk_reader; stop this repository scan."
            commands = int(state.get("worker_commands", 0)) + 1
            state["worker_commands"] = commands
            if commands > worker_limit:
                return (
                    f"Codex Baron capped bulk_reader discovery at {worker_limit} commands. Return the verified "
                    "findings and unresolved questions now."
                )
        if phase == "worker_complete":
            if model == worker_model:
                return "Codex Baron stopped the bulk_reader; return control to the primary agent."
            commands = int(state.get("verification_commands", 0)) + 1
            state["verification_commands"] = commands
            if commands > verification_limit:
                return (
                    f"Codex Baron capped primary verification at {verification_limit} commands after the verified worker response. "
                    "Use the worker's verified cited findings or ask a focused follow-up instead of repeating discovery."
                )
        if phase == "local_fallback":
            commands = int(state.get("local_fallback_commands", 0)) + 1
            state["local_fallback_commands"] = commands
            if commands > fallback_limit:
                return (
                    f"Codex Baron exhausted the bounded local fallback ({fallback_limit} discovery command); "
                    "do not repeat broad discovery."
                )
        return None

    return mutate_route_state(
        cwd,
        session_id,
        inspect,
        config,
        error_result=(
            "Codex Baron could not safely update its routing state. Retry after resolving the local state-lock "
            "or data-directory problem; repository discovery is denied to prevent duplicate work."
        ),
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    event_name = str(payload.get("hook_event_name", ""))
    cwd = Path(payload.get("cwd") or ".").resolve()
    config = load_config(cwd)
    base_event = {
        "event": event_name,
        "session_id": payload.get("session_id"),
        "model": payload.get("model"),
        "cwd": str(cwd),
    }

    if event_name == "UserPromptSubmit" and config.get("prompt_routing_enabled", True):
        primary_model = str(payload.get("model", ""))
        route, reason = recommend_route(
            str(payload.get("prompt", "")),
            benefit_gate_enabled=bool(config.get("delegation_benefit_gate_enabled", True)),
            primary_model=primary_model,
        )
        state_ready = reset_route_state(cwd, payload.get("session_id"), route, primary_model, config)
        if route == "bulk_reader" and not state_ready:
            route, reason = "primary", "routing state unavailable; continuing locally without delegation"
        write_telemetry({**base_event, "route": route, "reason": reason}, config)
        if route == "primary":
            return 0
        if route == "bulk_reader":
            skill_path = PLUGIN_ROOT / "skills" / "codex-baron" / "SKILL.md"
            message = (
                "Codex Baron route: first read the installed instructions at the exact runtime path "
                f"`{shlex.quote(str(skill_path))}`; do not reconstruct or shorten that path from a skill-root alias. "
                "Then call `spawn_agent` exactly once for `bulk_reader` with `fork_turns=\"none\"` and a minimal "
                "self-contained brief before using repository tools. Do not call any wait or claim that scanning "
                "started until spawn returns a nonempty worker ID. Wait only after that result, consume the worker's "
                "nonempty compact cited findings, and perform at most one focused verification command. If the "
                "spawn tool is unavailable or the call fails, say so and retry the first denied repository command "
                "once for bounded local fallback."
            )
        else:
            message = (
                f"Codex Baron route: use `{route}` for bounded {reason}. Pass a minimal self-contained brief; "
                "the primary owns integration and focused verification."
            )
        print(json.dumps(context_output(event_name, message)))
        return 0

    if event_name == "PreToolUse":
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        if _is_spawn_agent_tool(tool_name) and _spawn_targets_bulk_reader(tool_input):
            if not mark_spawn_attempted(cwd, payload.get("session_id"), config):
                print(json.dumps(deny_tool(
                    "Codex Baron allows exactly one bulk_reader spawn attempt for this routed turn."
                )))
                return 0
            write_telemetry({**base_event, "route": "bulk_reader", "reason": "bulk_reader spawn attempted", "tool": tool_name}, config)
            return 0
        if _is_wait_tool(tool_name):
            waiting_denial = wait_guard(cwd, payload.get("session_id"), config, tool_input, tool_name)
            if waiting_denial:
                write_telemetry({**base_event, "route": "bulk_reader", "reason": "worker handoff not verified", "tool": tool_name}, config)
                print(json.dumps(deny_tool(waiting_denial)))
                return 0
        if tool_name == "Bash":
            ownership_denial = discovery_guard(
                cwd,
                payload.get("session_id"),
                str(payload.get("model", "")),
                config,
                command=command,
                agent_type=str(payload.get("agent_type") or ""),
                agent_id=_stable_agent_id(payload),
            )
            if ownership_denial:
                if ownership_denial == LOCAL_FALLBACK_NOTICE:
                    print(json.dumps(context_output("PreToolUse", ownership_denial)))
                    return 0
                write_telemetry({
                    **base_event,
                    "route": "bulk_reader",
                    "reason": "exclusive discovery guard",
                    "tool": tool_name,
                }, config)
                print(json.dumps(deny_tool(ownership_denial)))
                return 0
        if tool_name == "Bash" and config.get("large_read_guard_enabled", True):
            threshold = _bounded_int(config, "large_file_lines", 500, 50, 10_000_000)
            result = find_large_full_read(command, cwd, threshold)
            if result:
                path, lines = result
                write_telemetry({
                    **base_event, "route": "bulk_reader", "reason": "large full-file read blocked",
                    "tool": tool_name, "line_count": lines, "extension": path.suffix.lower() or "[none]",
                }, config)
                reason = (
                    f"Codex Baron blocked a full read of a {lines}-line file. Use `bulk_reader` with a focused "
                    "question, use `rg`, or read a targeted line range. Do not retry the same full-file command."
                )
                print(json.dumps(deny_tool(reason)))
                return 0
        if tool_name == "apply_patch" and config.get("sensitive_review_enabled", True):
            paths = patch_paths(command)
            if paths and sensitive_path(paths, list(config.get("sensitive_patterns", []))):
                require_senior_review(cwd, payload.get("session_id"), config)
                write_telemetry({**base_event, "route": "senior_reviewer", "reason": "sensitive path changed", "tool": tool_name}, config)
                print(json.dumps(context_output("PreToolUse", (
                    "Codex Baron detected a sensitive-area change. After applying the patch, require a "
                    "`senior_reviewer` pass and focused tests before declaring completion."
                ))))
                return 0
        write_telemetry({**base_event, "tool": tool_name, "reason": "tool observed"}, config)
        return 0

    if event_name in {"SubagentStart", "SubagentStop"}:
        lifecycle_message: str | None = None
        if config.get("exclusive_discovery_guard_enabled", True):
            def update_lifecycle(state: dict[str, Any]) -> str | None:
                if state.get("route") != "bulk_reader":
                    return None
                if event_name == "SubagentStart":
                    if state.get("phase") not in {"awaiting_worker", "spawn_attempted"}:
                        return "Codex Baron ignored a late or duplicate discovery worker; ownership is already closed."
                    agent_type = str(payload.get("agent_type") or "")
                    agent_id = _stable_agent_id(payload)
                    observed_model = str(payload.get("model") or "")
                    expected_model = str(config.get("bulk_reader_model", "gpt-5.6-terra") or "")
                    requested_role = state.get("requested_worker_role")
                    lifecycle_role = payload.get("task_name")
                    inferred_default_role = (
                        agent_type in {"", "default"}
                        and bool(expected_model)
                        and observed_model == expected_model
                        and observed_model != str(state.get("primary_model") or "")
                        and (
                            requested_role == "bulk_reader"
                            or is_bulk_reader_role_marker(task_name=lifecycle_role)
                        )
                    )
                    if agent_type != "bulk_reader" and not inferred_default_role:
                        return "Codex Baron did not grant repository discovery ownership: only bulk_reader may scan."
                    if not agent_id:
                        return "Codex Baron did not grant discovery ownership: the bulk_reader worker ID is missing."
                    starts = int(state.get("worker_starts", 0)) + 1
                    state["worker_starts"] = starts
                    if starts > 1:
                        return "Codex Baron allows one discovery worker; do not repeat the repository scan."
                    state["phase"] = "worker_active"
                    state["worker_model"] = observed_model
                    state["worker_agent_type"] = "bulk_reader"
                    state["observed_agent_type"] = agent_type
                    state["worker_id"] = agent_id
                    state["identity_confidence"] = "stable"
                    state["spawn_observed_via_lifecycle"] = True
                    state["spawn_attempts"] = max(1, int(state.get("spawn_attempts", 0)))
                    return (
                        "You are the sole discovery owner. Use at most four focused commands, cap search output at "
                        "160 lines per command, return a nonempty response with your worker ID and compact cited "
                        "findings, and do not ask the primary to repeat your scan."
                    )
                agent_type = str(payload.get("agent_type") or "")
                agent_id = _stable_agent_id(payload)
                if state.get("phase") == "worker_active":
                    expected_id = state.get("worker_id")
                    if expected_id and (not agent_id or str(expected_id) != str(agent_id)):
                        return None
                    state["phase"] = "worker_complete"
                    return (
                        "The worker stopped. Confirm a nonempty final response before treating discovery as complete; "
                        "run at most one focused verification command and do not repeat broad discovery."
                    )
                return None

            lifecycle_message = mutate_route_state(
                cwd,
                payload.get("session_id"),
                update_lifecycle,
                config,
                error_result="Codex Baron could not update discovery lifecycle state; stop discovery and return control.",
            )
        write_telemetry({**base_event, "agent_type": payload.get("agent_type"), "reason": "subagent lifecycle"}, config)
        print(json.dumps(context_output(event_name, lifecycle_message)) if lifecycle_message else "{}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
